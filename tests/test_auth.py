import json

import pytest
from fastapi.testclient import TestClient

from app import auth, config, db, main, monitor, notifier

PRIYA = {"username": "priya", "password": "correct horse"}
RAHUL = {"username": "rahul", "password": "battery staple"}


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    monkeypatch.setattr(config, "LOAD_DEFAULT_COMPANIES", False)
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(monitor, "submit_check", lambda cid: None)       # no real scraping
    monkeypatch.setattr(monitor, "submit_check_all", lambda: None)
    monkeypatch.setattr(monitor, "submit_check_everyone", lambda: None)


@pytest.fixture
def client(app_db):
    with TestClient(main.app) as c:
        yield c


def other_browser():
    """A second, separate browser. The app is already started by `client`."""
    return TestClient(main.app)


# --- sign up, log in, log out ----------------------------------------------
def test_api_needs_login(client):
    assert client.get("/api/state").status_code == 401
    s = client.get("/api/auth/status").json()
    assert s == {"logged_in": False, "username": None, "signup_open": True, "has_users": False}


def test_signup_signs_you_in(client):
    r = client.post("/api/auth/signup", json=PRIYA)
    assert r.status_code == 200 and r.json()["username"] == "priya"
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/auth/status").json()["username"] == "priya"


def test_password_is_stored_hashed(client):
    client.post("/api/auth/signup", json=PRIYA)
    stored = db.get_user_by_name("priya")["password_hash"]
    assert "correct horse" not in stored and stored.startswith("scrypt$")


def test_signup_checks(client):
    assert client.post("/api/auth/signup", json={"username": "priya", "password": "short"}).status_code == 422
    assert client.post("/api/auth/signup", json={"username": "a b", "password": "long enough"}).status_code == 422
    assert client.post("/api/auth/signup", json={**PRIYA, "email": "nope"}).status_code == 422
    assert db.first_user_id() is None  # a bad email doesn't leave a half-made account
    client.post("/api/auth/signup", json=PRIYA)
    taken = other_browser().post("/api/auth/signup", json={**RAHUL, "username": "PRIYA"})
    assert taken.status_code == 409 and "taken" in taken.json()["detail"]


def test_login_logout(client):
    client.post("/api/auth/signup", json=PRIYA)
    client.post("/api/auth/logout")
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/auth/login", json={**PRIYA, "password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={**PRIYA, "username": "nobody"}).status_code == 401
    assert client.post("/api/auth/login", json={**PRIYA, "username": "Priya"}).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_old_cookie_stops_working_after_logout(client):
    client.post("/api/auth/signup", json=PRIYA)
    token = client.cookies.get(auth.COOKIE)
    client.post("/api/auth/logout")
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/api/state").status_code == 401


def test_lockout_after_repeated_wrong_passwords(client):
    client.post("/api/auth/signup", json=PRIYA)
    client.cookies.clear()
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/login", json={**PRIYA, "password": "nope nope"}).status_code == 401
    assert client.post("/api/auth/login", json=PRIYA).status_code == 429


def test_change_password_signs_out_other_sessions(client):
    client.post("/api/auth/signup", json=PRIYA)
    other = other_browser()
    other.post("/api/auth/login", json=PRIYA)
    bad = client.post("/api/account/password", json={"current_password": "x", "new_password": "new password 1"})
    assert bad.status_code == 400
    ok = client.post("/api/account/password",
                     json={"current_password": PRIYA["password"], "new_password": "new password 1"})
    assert ok.status_code == 200
    assert client.get("/api/state").status_code == 200   # this browser stays in
    assert other.get("/api/state").status_code == 401    # the other one is out
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json=PRIYA).status_code == 401
    assert client.post("/api/auth/login", json={**PRIYA, "password": "new password 1"}).status_code == 200


def test_signup_can_be_turned_off_after_the_first_account(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    assert client.get("/api/auth/status").json()["signup_open"] is True  # nobody yet: first account allowed
    assert client.post("/api/auth/signup", json=PRIYA).status_code == 200
    other = other_browser()
    assert other.get("/api/auth/status").json()["signup_open"] is False
    assert other.post("/api/auth/signup", json=RAHUL).status_code == 403


# --- each account has its own data -------------------------------------------
def test_accounts_do_not_see_each_other(client):
    client.post("/api/auth/signup", json=PRIYA)
    added = client.post("/api/companies", json={"name": "Acme", "url": "https://jobs.lever.co/acme"}).json()
    client.post("/api/resume", files={"file": ("cv.txt", b"Priya. Python developer. " * 20, "text/plain")})
    client.post("/api/preferences", json={"locations": "Pune", "include_internships": False, "india_only": True})
    client.post("/api/alert-emails", json={"emails": ["priya@example.com"]})

    rahul = other_browser()
    rahul.post("/api/auth/signup", json=RAHUL)
    s = rahul.get("/api/state").json()
    assert s["companies"] == [] and s["resume"] is None and s["preferences"] == {}
    assert s["settings"]["alert_emails"] == []
    assert not any("Acme" in e["message"] or "priya" in e["message"] for e in s["events"])
    assert rahul.get("/api/jobs").json() == []
    # Rahul can't touch Priya's company, even knowing its id.
    assert rahul.delete(f"/api/companies/{added['id']}").status_code == 404
    assert rahul.post(f"/api/companies/{added['id']}/check").status_code == 404
    assert rahul.post(f"/api/companies/{added['id']}/active", json={"active": False}).status_code == 404
    # Both can watch the same page, as separate entries.
    assert rahul.post("/api/companies", json={"name": "Acme", "url": "https://jobs.lever.co/acme"}).status_code == 200

    p = client.get("/api/state").json()
    assert [c["name"] for c in p["companies"]] == ["Acme"] and p["companies"][0]["active"]
    assert p["resume"]["filename"] == "cv.txt" and p["preferences"]["locations"] == "Pune"
    assert p["settings"]["alert_emails"] == ["priya@example.com"]


def test_background_check_uses_the_owners_resume(client, monkeypatch):
    for creds, cv in ((PRIYA, b"PRIYA RESUME " * 30), (RAHUL, b"RAHUL RESUME " * 30)):
        b = other_browser()
        b.post("/api/auth/signup", json=creds)
        b.post("/api/companies", json={"name": f"Co {creds['username']}", "url": f"https://x.com/{creds['username']}"})
        b.post("/api/resume", files={"file": ("cv.txt", cv, "text/plain")})
    seen = {}
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([{
        "external_id": "1", "title": "Engineer", "location": "Pune, India", "url": "https://x.com/j/1",
        "description": "Build things. " * 20, "posted_at": ""}], None))
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)

    monkeypatch.setattr(monitor.matcher, "classify_job",
                        lambda job: {"fields": {"software": 1.0}, "level": "fresher", "level_confidence": 1.0})
    monkeypatch.setattr(monitor.matcher, "profile_fields", lambda prefs, card: ["software"])

    def evaluate(resume_text, company, job, prefs, facts=None, **kw):
        seen[company["name"]] = resume_text.split()[0]
        return {"verdict": "related", "job_level": "fresher", "related_p": .8, "eligible_p": .4,
                "blocker_p": 0, "fit_score": 2, "seniority": None, "reasons": [], "model": "test"}

    monkeypatch.setattr(monitor.matcher, "evaluate", evaluate)
    monitor.check_all()  # the timer, running outside any request
    assert seen == {"Co priya": "PRIYA", "Co rahul": "RAHUL"}


def test_signup_email_becomes_the_alert_address(client):
    client.post("/api/auth/signup", json={**RAHUL, "email": "rahul@example.com"})
    assert client.get("/api/state").json()["settings"]["alert_emails"] == ["rahul@example.com"]


def test_env_address_is_only_for_the_first_account(client, monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "owner@example.com")
    client.post("/api/auth/signup", json=PRIYA)
    assert client.get("/api/state").json()["settings"]["alert_emails"] == ["owner@example.com"]
    rahul = other_browser()
    rahul.post("/api/auth/signup", json=RAHUL)
    assert rahul.get("/api/state").json()["settings"]["alert_emails"] == []


def test_one_account_database_moves_to_the_first_user(app_db):
    """A database from the single-account version: login in settings, unowned data."""
    db.init()
    with db.conn() as c:
        c.execute("INSERT INTO settings (key, value) VALUES ('account', ?)", (json.dumps(
            {"username": "tarun", "password_hash": auth.hash_password("old password"),
             "created_at": "2026-09-01T00:00:00+00:00"}),))
        c.execute("INSERT INTO settings (key, value) VALUES ('preferences', ?)", (json.dumps({"locations": "Delhi"}),))
        c.execute("INSERT INTO resume (id, filename, text, uploaded_at) VALUES (1, 'old.pdf', 'old text', 'x')")
        c.execute("INSERT INTO companies (name, url, source, created_at) VALUES ('Old Co', 'https://o', 'lever', 'x')")
        c.execute("INSERT INTO events (ts, level, message) VALUES ('x', 'info', 'old activity')")
    with TestClient(main.app) as c:
        assert c.post("/api/auth/login", json={"username": "tarun", "password": "old password"}).status_code == 200
        s = c.get("/api/state").json()
        assert [x["name"] for x in s["companies"]] == ["Old Co"]
        assert s["resume"]["filename"] == "old.pdf" and s["preferences"] == {"locations": "Delhi"}
        assert any(e["message"] == "old activity" for e in s["events"])
        other = other_browser()
        other.post("/api/auth/signup", json=RAHUL)
        s2 = other.get("/api/state").json()
        assert s2["companies"] == [] and s2["resume"] is None
        assert not any(e["message"] == "old activity" for e in s2["events"])


def test_database_calls_without_a_user_fail_loudly(app_db):
    db.init()
    token = db._user.set(None)
    try:
        with pytest.raises(RuntimeError):
            db.list_companies()
    finally:
        db._user.reset(token)


# --- webhooks and email ------------------------------------------------------
def test_webhooks_use_their_secret_not_the_login(client):
    assert client.post("/webhook/check").status_code == 200


def test_alert_addresses_saved_from_dashboard(client, monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "env@example.com")
    client.post("/api/auth/signup", json=PRIYA)
    r = client.post("/api/alert-emails", json={"emails": [" me@example.com", "Me@example.com", "mom@example.in"]})
    assert r.json()["emails"] == ["me@example.com", "mom@example.in"]
    s = client.get("/api/state").json()["settings"]
    assert s["alert_emails_saved"] is True and s["alert_email"] == "me@example.com, mom@example.in"
    assert client.post("/api/alert-emails", json={"emails": ["not-an-email"]}).status_code == 422
    assert client.get("/api/state").json()["settings"]["alert_emails"] == ["me@example.com", "mom@example.in"]
    client.post("/api/alert-emails", json={"emails": []})
    assert client.get("/api/state").json()["settings"]["alert_emails"] == ["env@example.com"]


def test_alert_goes_to_every_saved_address(client, monkeypatch):
    client.post("/api/auth/signup", json=PRIYA)
    client.post("/api/alert-emails", json={"emails": ["a@example.com", "b@example.com"]})
    monkeypatch.setattr(config, "SMTP_USER", "bot@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg["To"])

    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    assert client.post("/api/test-email").status_code == 200
    assert sent == ["a@example.com, b@example.com"]


# --- forgot password -----------------------------------------------------------
@pytest.fixture
def mailbox(monkeypatch):
    """Reset emails land here instead of Gmail; the background send runs at once."""
    monkeypatch.setattr(config, "SMTP_USER", "bot@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "")
    sent = []
    monkeypatch.setattr(notifier, "send_password_reset",
                        lambda to, username, link, minutes: sent.append({"to": to, "user": username, "link": link}))

    class NowThread:
        def __init__(self, target, daemon=None): self.target = target
        def start(self): self.target()

    monkeypatch.setattr(main.threading, "Thread", NowThread)
    return sent


def _token(mail):
    return mail["link"].split("#reset=")[1]


def test_forgot_password_emails_a_link_that_works_once(client, mailbox):
    client.post("/api/auth/signup", json={**PRIYA, "email": "priya@example.com"})
    other = other_browser()
    other.post("/api/auth/login", json=PRIYA)
    client.post("/api/auth/logout")

    r = client.post("/api/auth/forgot", json={"username": "Priya"})
    assert r.status_code == 200 and "reset link" in r.json()["message"]
    assert len(mailbox) == 1 and mailbox[0]["to"] == "priya@example.com"
    assert mailbox[0]["link"].startswith(config.DASHBOARD_URL + "/#reset=")

    token = _token(mailbox[0])
    assert client.post("/api/auth/reset", json={"token": token, "new_password": "brand new pass"}).status_code == 200
    assert client.get("/api/state").status_code == 200                     # signed in straight away
    assert other.get("/api/state").status_code == 401                      # everyone else signed out
    assert client.post("/api/auth/reset", json={"token": token, "new_password": "again again"}).status_code == 400
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json=PRIYA).status_code == 401
    assert client.post("/api/auth/login", json={**PRIYA, "password": "brand new pass"}).status_code == 200


def test_forgot_gives_the_same_answer_for_unknown_users(client, mailbox):
    client.post("/api/auth/signup", json={**PRIYA, "email": "priya@example.com"})
    known = client.post("/api/auth/forgot", json={"username": "priya"}).json()
    unknown = client.post("/api/auth/forgot", json={"username": "nobody"}).json()
    assert known == unknown and len(mailbox) == 1


def test_reset_address_rules(client, mailbox, monkeypatch):
    monkeypatch.setattr(config, "ALERT_EMAIL_TO", "owner@example.com")
    client.post("/api/auth/signup", json=PRIYA)                       # first account, no email
    other_browser().post("/api/auth/signup", json=RAHUL)             # second account, no email
    client.post("/api/auth/forgot", json={"username": "priya"})
    client.post("/api/auth/forgot", json={"username": "rahul"})
    assert [m["to"] for m in mailbox] == ["owner@example.com"]       # the owner falls back to .env; Rahul gets nothing
    rahul = other_browser()
    rahul.post("/api/auth/login", json=RAHUL)
    rahul.post("/api/account/email", json={"email": "rahul@example.com"})
    assert rahul.get("/api/state").json()["settings"]["account_email"] == "rahul@example.com"
    client.post("/api/auth/forgot", json={"username": "rahul"})
    assert mailbox[-1]["to"] == "rahul@example.com"


def test_alert_addresses_are_not_used_for_resets(client, mailbox):
    client.post("/api/auth/signup", json=PRIYA)
    other_browser().post("/api/auth/signup", json=RAHUL)
    rahul = other_browser()
    rahul.post("/api/auth/login", json=RAHUL)
    rahul.post("/api/alert-emails", json={"emails": ["mom@example.com"]})
    rahul.post("/api/auth/forgot", json={"username": "rahul"})
    assert mailbox == []


def test_bad_or_expired_reset_links(client, mailbox, monkeypatch):
    client.post("/api/auth/signup", json={**PRIYA, "email": "priya@example.com"})
    client.post("/api/auth/forgot", json={"username": "priya"})
    token = _token(mailbox[0])
    assert client.post("/api/auth/reset", json={"token": "x" * 40, "new_password": "brand new pass"}).status_code == 400
    # A too-short password is refused without using up the link.
    assert client.post("/api/auth/reset", json={"token": token, "new_password": "short"}).status_code == 400
    with db.conn() as c:
        c.execute("UPDATE password_resets SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client.post("/api/auth/reset", json={"token": token, "new_password": "brand new pass"}).status_code == 400


def test_reset_lifts_the_login_lockout(client, mailbox):
    client.post("/api/auth/signup", json={**PRIYA, "email": "priya@example.com"})
    client.post("/api/auth/logout")
    for _ in range(auth.MAX_FAILURES):
        client.post("/api/auth/login", json={**PRIYA, "password": "wrong guess"})
    assert client.post("/api/auth/login", json=PRIYA).status_code == 429
    client.post("/api/auth/forgot", json={"username": "priya"})
    client.post("/api/auth/reset", json={"token": _token(mailbox[0]), "new_password": "brand new pass"})
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={**PRIYA, "password": "brand new pass"}).status_code == 200


def test_forgot_requests_are_rate_limited(client, mailbox):
    for _ in range(auth.MAX_FAILURES):
        assert client.post("/api/auth/forgot", json={"username": "anyone"}).status_code == 200
    assert client.post("/api/auth/forgot", json={"username": "anyone"}).status_code == 429
