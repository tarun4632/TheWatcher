"""Tiny SQLite layer. One connection per call keeps it safe across threads.

Every account has its own companies (and so jobs), resume and settings. The
signed-in user for the current request or background check is held in a
context variable (see `as_user`). Functions that read or change one user's data
call `uid()`, which raises instead of guessing when no user is set, so a missing
`as_user` fails loudly rather than showing someone else's data.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    source          TEXT NOT NULL,          -- greenhouse | lever | ashby | smartrecruiters | workday | generic
    source_key      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    last_checked_at TEXT,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    is_baseline    INTEGER NOT NULL DEFAULT 0,  -- 1 = was already listed when you added the company
    status         TEXT NOT NULL DEFAULT 'open',
    verdict        TEXT NOT NULL DEFAULT 'pending', -- pending | eligible | related | not_related | error
    related_p      REAL,
    eligible_p     REAL,
    blocker_p      REAL,
    fit_score      REAL,
    seniority      TEXT,
    reasons        TEXT NOT NULL DEFAULT '[]',
    model          TEXT,
    evaluated_at   TEXT,
    emailed_at     TEXT,
    UNIQUE (company_id, external_id)
);

CREATE TABLE IF NOT EXISTS resume (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    filename    TEXT NOT NULL,
    text        TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes (
    user_id     INTEGER PRIMARY KEY,
    filename    TEXT NOT NULL,
    text        TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    facts       TEXT
);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,           -- sha256 of the emailed token, never the token itself
    user_id    INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,           -- sha256 of the cookie value, never the value itself
    user_id    INTEGER,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


_user: ContextVar[int | None] = ContextVar("thewatcher_user", default=None)


def uid() -> int:
    """The user whose data we're working with. Raises if none is set."""
    u = _user.get()
    if u is None:
        raise RuntimeError("No user set for this database call")
    return u


@contextmanager
def as_user(user_id: int):
    token = _user.set(user_id)
    try:
        yield
    finally:
        _user.reset(token)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# Columns added after the first release. init() adds any that are missing,
# so an existing data/thewatcher.db keeps working.
MIGRATIONS = {
    "companies": {
        "is_default": "INTEGER NOT NULL DEFAULT 0",  # came from the starter list
        "category": "TEXT NOT NULL DEFAULT ''",
        "active": "INTEGER NOT NULL DEFAULT 1",      # 0 = paused, not checked
        "snapshot": "TEXT",                          # starter-list stats as JSON
        "user_id": "INTEGER",                        # whose company this is
        "recipe": "TEXT",                            # how discover.py found the jobs, as JSON
    },
    "events": {
        "user_id": "INTEGER",             # NULL = app-wide message, shown to everyone
    },
    "sessions": {
        "user_id": "INTEGER",
    },
    "users": {
        "email": "TEXT",                  # for password resets; not where alerts go
    },
    "jobs": {
        "job_level": "TEXT",              # internship | fresher | experienced | senior
        "email_attempts": "INTEGER NOT NULL DEFAULT 0",
        "facts": "TEXT",                  # what Gemini read from the posting, as JSON
        "kev_job": "TEXT",                # Kev's reading of the job alone (field, level), as JSON
        "eval_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",        # scoring retry is held back until then
        "email_next_at": "TEXT",          # email retry is held back until then
    },
    "resume": {
        "facts": "TEXT",                  # what Gemini read from the resume, as JSON
    },
}

# Verdicts for jobs we deliberately don't score or show.
HIDDEN_VERDICTS = ("out_of_area", "skipped", "wrong_level")  # wrong_level: e.g. senior roles for a fresher

# Which job levels belong in each dashboard section.
LEVEL_GROUPS = {
    "fresher": ("internship", "fresher"),
    "experienced": ("experienced", "senior"),
}


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS companies_user ON companies (user_id)")
        c.execute("DELETE FROM sessions WHERE user_id IS NULL")  # from the one-account version
        # The one-account version kept its login in settings: make it the first user.
        row = c.execute("SELECT value FROM settings WHERE key = 'account'").fetchone()
        migrated = bool(row) and not c.execute("SELECT 1 FROM users").fetchone()
        if migrated:
            acc = json.loads(row["value"])
            c.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                      (acc["username"], acc["password_hash"], acc.get("created_at") or now()))
        c.execute("DELETE FROM settings WHERE key = 'account'")
    first = first_user_id()
    if first is not None:
        claim_unowned(first, events=migrated)


# Settings from before accounts existed. They belong to whoever signs up first.
LEGACY_USER_SETTINGS = ("preferences", "profile", "profile_needs_review", "alert_emails", "defaults_seeded")


def claim_unowned(user_id: int, events: bool = False):
    """Give data from before accounts existed to this user (the first account).
    `events` also hands over the old Activity list; it's only done once, because
    later app-wide messages (user_id NULL) are meant for everyone."""
    with conn() as c:
        c.execute("UPDATE companies SET user_id = ? WHERE user_id IS NULL", (user_id,))
        if events:
            c.execute("UPDATE events SET user_id = ? WHERE user_id IS NULL", (user_id,))
        old = c.execute("SELECT * FROM resume WHERE id = 1").fetchone()
        if old:
            c.execute("INSERT OR IGNORE INTO resumes (user_id, filename, text, uploaded_at, facts) "
                      "VALUES (?, ?, ?, ?, ?)", (user_id, old["filename"], old["text"], old["uploaded_at"],
                                                  old["facts"]))
            c.execute("DELETE FROM resume")
        for key in LEGACY_USER_SETTINGS:
            c.execute("UPDATE OR IGNORE settings SET key = ? WHERE key = ?", (_user_key(key, user_id), key))
            c.execute("DELETE FROM settings WHERE key = ?", (key,))


# --- events ----------------------------------------------------------------
_CURRENT = object()


def log(message: str, level: str = "info", user_id=_CURRENT):
    """Add to the Activity list of the current user (or of `user_id`; None = everyone)."""
    who = _user.get() if user_id is _CURRENT else user_id
    print(f"[{level}]{f' (user {who})' if who else ''} {message}", flush=True)
    with conn() as c:
        c.execute("INSERT INTO events (ts, level, message, user_id) VALUES (?, ?, ?, ?)",
                  (now(), level, message, who))
        c.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")


def recent_events(limit: int = 40):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, ts, level, message FROM events WHERE user_id = ? OR user_id IS NULL "
            "ORDER BY id DESC LIMIT ?", (uid(), limit))]


# --- settings --------------------------------------------------------------
# get_setting/set_setting are the current user's; *_global_setting are app-wide
# (only the shared Gemini budget today).
def _user_key(key: str, user_id: int) -> str:
    return f"u{user_id}:{key}"


def get_global_setting(key: str, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_global_setting(key: str, value):
    with conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def get_setting(key: str, default=None):
    return get_global_setting(_user_key(key, uid()), default)


def set_setting(key: str, value):
    set_global_setting(_user_key(key, uid()), value)


def delete_setting(key: str):
    with conn() as c:
        c.execute("DELETE FROM settings WHERE key = ?", (_user_key(key, uid()),))


# --- users -----------------------------------------------------------------
def create_user(username: str, password_hash: str) -> int:
    """Raises sqlite3.IntegrityError if the username is taken (any letter case)."""
    with conn() as c:
        cur = c.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                        (username, password_hash, now()))
        new_id = cur.lastrowid
    if first_user_id() == new_id:
        claim_unowned(new_id, events=True)
    return new_id


def get_user(user_id: int):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_name(username: str):
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    return dict(row) if row else None


def first_user_id() -> int | None:
    with conn() as c:
        row = c.execute("SELECT MIN(id) AS id FROM users").fetchone()
    return row["id"] if row else None


def list_users():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id, username, created_at FROM users ORDER BY id")]


def set_password_hash(user_id: int, password_hash: str):
    with conn() as c:
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def set_user_email(user_id: int, email: str | None):
    with conn() as c:
        c.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))


# --- password resets -------------------------------------------------------
def add_reset_token(token_hash: str, user_id: int, expires_at: str):
    with conn() as c:
        c.execute("DELETE FROM password_resets WHERE expires_at <= ?", (now(),))
        c.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                  (token_hash, user_id, expires_at))


def take_reset_token(token_hash: str) -> int | None:
    """The user a valid reset token belongs to. Using one cancels all of that user's tokens."""
    with conn() as c:
        row = c.execute("SELECT user_id FROM password_resets WHERE token_hash = ? AND expires_at > ?",
                        (token_hash, now())).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM password_resets WHERE user_id = ?", (row["user_id"],))
        return row["user_id"]


# --- login sessions --------------------------------------------------------
def add_session(token_hash: str, user_id: int, expires_at: str):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
        c.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                  (token_hash, user_id, now(), expires_at))


def session_user(token_hash: str) -> int | None:
    with conn() as c:
        row = c.execute("SELECT s.user_id FROM sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token_hash = ? AND s.expires_at > ?", (token_hash, now())).fetchone()
    return row["user_id"] if row else None


def delete_session(token_hash: str):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id: int):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


# --- resume ----------------------------------------------------------------
def save_resume(filename: str, text: str, facts: dict | None = None):
    with conn() as c:
        c.execute(
            "INSERT INTO resumes (user_id, filename, text, uploaded_at, facts) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET filename = excluded.filename, text = excluded.text, "
            "uploaded_at = excluded.uploaded_at, facts = excluded.facts",
            (uid(), filename, text, now(), json.dumps(facts) if facts else None),
        )


def get_resume():
    with conn() as c:
        row = c.execute("SELECT * FROM resumes WHERE user_id = ?", (uid(),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["facts"] = json.loads(d["facts"]) if d.get("facts") else None
    return d


# --- companies -------------------------------------------------------------
def add_company(name: str, url: str, source: str, source_key: str, *, is_default: bool = False,
                category: str = "", snapshot: dict | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO companies (name, url, source, source_key, created_at, is_default, category, snapshot, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, url, source, source_key, now(), int(is_default), category,
             json.dumps(snapshot) if snapshot else None, uid()),
        )
        return cur.lastrowid


def find_company_by_source(source: str, source_key: str):
    with conn() as c:
        row = c.execute("SELECT * FROM companies WHERE source = ? AND source_key = ? AND user_id = ?",
                        (source, source_key, uid())).fetchone()
    return dict(row) if row else None


def set_active(company_ids, active: bool):
    with conn() as c:
        me = uid()
        c.executemany("UPDATE companies SET active = ? WHERE id = ? AND user_id = ?",
                      [(int(active), i, me) for i in company_ids])


def unskip_jobs(company_id: int) -> int:
    """'Score current openings': queue jobs that were skipped as already listed."""
    with conn() as c:
        cur = c.execute("UPDATE jobs SET verdict = 'pending' WHERE company_id = ? AND status = 'open' "
                        "AND verdict = 'skipped' AND company_id IN (SELECT id FROM companies WHERE user_id = ?)",
                        (company_id, uid()))
        return cur.rowcount


def update_company(company_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE companies SET {cols} WHERE id = ? AND user_id = ?", (*fields.values(), company_id, uid()))


def delete_company(company_id: int):
    with conn() as c:
        c.execute("DELETE FROM companies WHERE id = ? AND user_id = ?", (company_id, uid()))


def get_company(company_id: int):
    """One of the current user's companies, or None."""
    with conn() as c:
        row = c.execute("SELECT * FROM companies WHERE id = ? AND user_id = ?", (company_id, uid())).fetchone()
    return dict(row) if row else None


def get_company_any(company_id: int):
    """Any user's company. For the background checker and webhooks only."""
    with conn() as c:
        row = c.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def all_companies_brief():
    """Every user's companies (id, owner, name, url, active). For the scheduler and webhooks."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, user_id, name, url, active FROM companies WHERE user_id IS NOT NULL ORDER BY id")]


def find_company_by_url(url: str):
    with conn() as c:
        row = c.execute("SELECT * FROM companies WHERE url = ? AND user_id = ?", (url, uid())).fetchone()
    return dict(row) if row else None


def list_companies():
    q = """
    SELECT c.*,
           SUM(CASE WHEN j.status = 'open' AND j.verdict NOT IN ('out_of_area', 'wrong_level') THEN 1 ELSE 0 END)
                                                                                              AS open_jobs,
           SUM(CASE WHEN j.status = 'open' AND j.verdict IN ('pending', 'error') THEN 1 ELSE 0 END) AS pending_jobs,
           SUM(CASE WHEN j.status = 'open' AND j.verdict = 'eligible' THEN 1 ELSE 0 END)     AS eligible_jobs,
           SUM(CASE WHEN j.status = 'open' AND j.verdict = 'skipped' THEN 1 ELSE 0 END)      AS skipped_jobs,
           SUM(CASE WHEN j.status = 'open' AND j.verdict = 'out_of_area' THEN 1 ELSE 0 END)  AS away_jobs,
           SUM(CASE WHEN j.status = 'open' AND j.verdict = 'wrong_level' THEN 1 ELSE 0 END)  AS other_level_jobs
    FROM companies c LEFT JOIN jobs j ON j.company_id = c.id
    WHERE c.user_id = ?
    GROUP BY c.id ORDER BY c.is_default ASC, c.name COLLATE NOCASE
    """
    with conn() as c:
        out = []
        for r in c.execute(q, (uid(),)):
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"]) if d.get("snapshot") else None
            d["recipe"] = json.loads(d["recipe"]) if d.get("recipe") else None
            out.append(d)
        return out


# --- jobs ------------------------------------------------------------------
def known_job_ids(company_id: int) -> dict:
    with conn() as c:
        rows = c.execute("SELECT external_id, status FROM jobs WHERE company_id = ?", (company_id,))
        return {r["external_id"]: r["status"] for r in rows}


def insert_job(company_id: int, job: dict, is_baseline: bool, verdict: str = "pending",
               reasons: list | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO jobs (company_id, external_id, title, location, url, description, "
            "first_seen_at, is_baseline, verdict, reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, job["external_id"], job["title"], job.get("location", ""), job["url"],
             job.get("description", ""), now(), int(is_baseline), verdict, json.dumps(reasons or [])),
        )
        return cur.lastrowid


def pending_jobs_brief(company_id: int):
    with conn() as c:
        rows = c.execute("SELECT id, location FROM jobs WHERE company_id = ? AND status = 'open' "
                         "AND verdict IN ('pending', 'error')", (company_id,))
        return [dict(r) for r in rows]


def set_job_status(company_id: int, external_ids, status: str):
    with conn() as c:
        c.executemany(
            "UPDATE jobs SET status = ? WHERE company_id = ? AND external_id = ?",
            [(status, company_id, e) for e in external_ids],
        )


def update_job(job_id: int, **fields):
    for key in ("reasons", "facts", "kev_job"):
        if key in fields and fields[key] is not None and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def count_to_evaluate(company_id: int) -> int:
    """How many jobs are waiting to be scored now (for the dashboard's 'x of y')."""
    with conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ? AND status = 'open' AND verdict IN ('pending', 'error') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)", (company_id, now())).fetchone()[0]


def jobs_to_evaluate(company_id: int, limit: int):
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE company_id = ? AND status = 'open' AND verdict IN ('pending', 'error') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY is_baseline ASC, id DESC LIMIT ?",
            (company_id, now(), limit),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["facts"] = json.loads(d["facts"]) if d.get("facts") else None
            d["kev_job"] = json.loads(d["kev_job"]) if d.get("kev_job") else None
            out.append(d)
        return out


def reset_verdicts():
    """Called when the resume or profile changes: everything open gets re-scored.
    Jobs skipped as 'already listed' stay skipped; out-of-area jobs are re-checked
    because the area setting may have changed. Jobs that failed get a fresh set
    of attempts. Facts already read from postings are kept (they don't depend on you)."""
    with conn() as c:
        c.execute("UPDATE jobs SET verdict = 'pending', eval_attempts = 0, next_attempt_at = NULL "
                  "WHERE status = 'open' AND verdict != 'skipped' "
                  "AND company_id IN (SELECT id FROM companies WHERE user_id = ?)", (uid(),))


def _level_clause(level_group: str | None):
    levels = LEVEL_GROUPS.get(level_group or "")
    if not levels:
        return "", []
    # Jobs not scored yet have no level, so they appear under All roles only.
    return f"j.job_level IN ({','.join('?' * len(levels))})", list(levels)


def list_jobs(verdict: str | None = None, company_id: int | None = None,
              level_group: str | None = None, limit: int = 300):
    where, args = ["j.status = 'open'", "c.user_id = ?"], [uid()]
    clause, largs = _level_clause(level_group)
    if clause:
        where.append(clause)
        args.extend(largs)
    if verdict:
        where.append("j.verdict = ?")
        args.append(verdict)
    else:
        where.append(f"j.verdict NOT IN ({','.join('?' * len(HIDDEN_VERDICTS))})")
        args.extend(HIDDEN_VERDICTS)
    if company_id:
        where.append("j.company_id = ?")
        args.append(company_id)
    q = f"""
    SELECT j.id, j.company_id, c.name AS company, j.title, j.location, j.url, j.first_seen_at,
           j.is_baseline, j.verdict, j.related_p, j.eligible_p, j.blocker_p, j.fit_score,
           j.seniority, j.job_level, j.reasons, j.model, j.evaluated_at, j.emailed_at
    FROM jobs j JOIN companies c ON c.id = j.company_id
    WHERE {' AND '.join(where)}
    ORDER BY CASE j.verdict WHEN 'eligible' THEN 0 WHEN 'related' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,
             j.first_seen_at DESC, j.fit_score DESC
    LIMIT ?
    """
    with conn() as c:
        out = []
        for r in c.execute(q, (*args, limit)):
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"] or "[]")
            out.append(d)
        return out


def verdict_counts(level_group: str | None = None):
    clause, args = _level_clause(level_group)
    where = ("status = 'open' AND company_id IN (SELECT id FROM companies WHERE user_id = ?)"
             + (f" AND {clause.replace('j.', '')}" if clause else ""))
    with conn() as c:
        rows = c.execute(f"SELECT verdict, COUNT(*) AS n FROM jobs WHERE {where} GROUP BY verdict", [uid(), *args])
        counts = {r["verdict"]: r["n"] for r in rows}
    counts["all"] = sum(n for v, n in counts.items() if v not in HIDDEN_VERDICTS)
    return counts


def level_counts():
    hidden = ",".join(f"'{v}'" for v in HIDDEN_VERDICTS)
    with conn() as c:
        rows = c.execute(f"SELECT job_level, COUNT(*) AS n FROM jobs WHERE status = 'open' "
                         f"AND verdict NOT IN ({hidden}) "
                         f"AND company_id IN (SELECT id FROM companies WHERE user_id = ?) GROUP BY job_level", (uid(),))
        raw = {r["job_level"]: r["n"] for r in rows}
    out = {g: sum(raw.get(l, 0) for l in levels) for g, levels in LEVEL_GROUPS.items()}
    out["all"] = sum(raw.values())
    return out


def jobs_needing_email(company_id: int, max_attempts: int | None = None):
    """New eligible jobs whose alert hasn't gone out yet (e.g. the mail server was down).
    A failed email waits until email_next_at before it is tried again."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE company_id = ? AND status = 'open' AND verdict = 'eligible' "
            "AND is_baseline = 0 AND emailed_at IS NULL AND email_attempts < ? "
            "AND (email_next_at IS NULL OR email_next_at <= ?) "
            "AND first_seen_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')",
            (company_id, max_attempts or config.MAX_EMAIL_ATTEMPTS, now()),
        )
        return [dict(r) for r in rows]
