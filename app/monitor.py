"""The watch loop: find new jobs, score them with Kev, email the good ones."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from pathlib import Path

from . import area, config, db, discover, extract, matcher, notifier, profile, ratelimit, scrapers

DEFAULTS_FILE = Path(__file__).with_name("default_companies.json")
# How long a job or email waits after its 1st, 2nd, 3rd... failure (0 = next check).
RETRY_DELAYS_MINUTES = [0, 30, 120, 720]


def _retry_at(attempt: int) -> str:
    minutes = RETRY_DELAYS_MINUTES[min(attempt, len(RETRY_DELAYS_MINUTES)) - 1]
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="thewatcher")
_running: set[int] = set()
_running_lock = threading.Lock()


def is_running(company_id: int) -> bool:
    with _running_lock:
        return company_id in _running


def running_ids() -> list[int]:
    with _running_lock:
        return sorted(_running)


# What each running check is doing right now, for the dashboard: the stage, and while scoring,
# which job, which step, and how far through the queue it is.
_progress: dict[int, dict] = {}


def _set_progress(company_id: int, **fields) -> None:
    with _running_lock:
        _progress.setdefault(company_id, {}).update(fields, updated_at=datetime.now(timezone.utc).isoformat(
            timespec="seconds"))


def progress(company_ids) -> dict[int, dict]:
    with _running_lock:
        return {i: dict(_progress[i]) for i in company_ids if i in _progress}


def check_company(company_id: int) -> dict:
    """Check one company as its owner: their resume, profile and alert addresses."""
    company = db.get_company_any(company_id)
    if not company or company["user_id"] is None:
        return {"error": "company not found"}
    with _running_lock:
        if company_id in _running:
            return {"skipped": "already checking"}
        _running.add(company_id)
    try:
        with db.as_user(company["user_id"]):
            return _check(company_id)
    finally:
        with _running_lock:
            _running.discard(company_id)
            _progress.pop(company_id, None)


def score_company(company_id: int) -> dict:
    """Score the jobs already saved for a company, without reading its careers site again."""
    company = db.get_company_any(company_id)
    if not company or company["user_id"] is None:
        return {"error": "company not found"}
    with _running_lock:
        if company_id in _running:
            return {"skipped": "already checking"}
        _running.add(company_id)
    try:
        with db.as_user(company["user_id"]):
            company = db.get_company(company_id)
            return {"emailed": _evaluate_pending(company, {})}
    finally:
        with _running_lock:
            _running.discard(company_id)
            _progress.pop(company_id, None)


REDISCOVER_AFTER_HOURS = 6  # a recipe that broke is re-worked out at most this often
MAX_SCORED_PER_CHECK = 3000  # a safety stop; normally the queue simply runs empty


def _queue(company_id: int):
    """Jobs waiting to be scored, MAX_EVALS_PER_RUN at a time, until none are left. Each job is
    tried at most once per check, so a failed one waits for its retry time instead of looping."""
    tried: set[int] = set()
    while len(tried) < MAX_SCORED_PER_CHECK:
        batch = [j for j in db.jobs_to_evaluate(company_id, config.MAX_EVALS_PER_RUN + len(tried))
                 if j["id"] not in tried][:config.MAX_EVALS_PER_RUN]
        if not batch:
            return
        for job in batch:
            tried.add(job["id"])
            yield job


def _recipe(company: dict) -> dict:
    r = company.get("recipe")
    return json.loads(r) if isinstance(r, str) and r else (r or {})


def _needs_discovery(company: dict) -> bool:
    """A careers page we haven't worked out yet, or whose saved recipe stopped working."""
    recipe = _recipe(company)
    if recipe.get("stale"):
        at = recipe.get("discovered_at") or ""
        age = datetime.now(timezone.utc) - datetime.fromisoformat(at) if at else timedelta(days=1)
        return age >= timedelta(hours=REDISCOVER_AFTER_HOURS)
    return company["source"] == "generic" and not recipe.get("discovered_at")


def _discover(company: dict) -> None:
    found = discover.discover(company["url"], company["name"])
    db.update_company(company["id"], source=found["source"], source_key=found["source_key"],
                      recipe=json.dumps(found["recipe"]))
    company.update(source=found["source"], source_key=found["source_key"], recipe=found["recipe"])
    steps = found["recipe"].get("steps") or []
    db.log(f"{company['name']}: {steps[-1] if steps else 'checked the careers page'}"
           + ("" if found["source"] in ("generic", "unsupported") else f" (reading it as {found['source']})"),
           "warn" if found["source"] == "unsupported" or found["recipe"].get("not_found") else "info")


def _check(company_id: int) -> dict:
    company = db.get_company(company_id)
    if not company:
        return {"error": "company not found"}
    name = company["name"]
    if _needs_discovery(company):
        _set_progress(company_id, stage="Working out where the jobs are")
        try:
            _discover(company)
        except Exception as e:  # noqa: BLE001 - fall back to reading the page as it is
            db.log(f"{name}: couldn't work out where the jobs are: {e}", "warn")
    recipe = _recipe(company)
    if company["source"] == "json" and not recipe.get("feed", {}).get("order_checked"):
        try:
            recipe["feed"], note = discover.stable_order(recipe["feed"])
            db.update_company(company_id, recipe=json.dumps(recipe))
            company["recipe"] = recipe
            if note:
                db.log(f"{name}: {note}")
        except Exception as e:  # noqa: BLE001 - keep the recipe as it was
            db.log(f"{name}: couldn't check the job list's order: {e}", "warn")
    if company["source"] == "unsupported":  # e.g. jobs only on LinkedIn: said once, not every check
        db.update_company(company_id, last_checked_at=db.now(),
                          last_error=(_recipe(company).get("reason") or "Can't be read automatically")[:500])
        return {"skipped": "unsupported"}

    prefs = db.get_setting("preferences", {}) or {}
    india_only = prefs.get("india_only", True)
    _set_progress(company_id, stage="Reading the job list")
    try:
        jobs, upgraded = scrapers.fetch_jobs(company, india_only=india_only)
        jobs = [j for j in jobs if scrapers.is_recent(j.get("posted_at"))]
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, scrapers.Unsupported) else f"Couldn't read the careers page: {e}"
        recipe = _recipe(company)
        if recipe.get("discovered_at") and company["source"] != "unsupported" and not recipe.get("stale"):
            recipe["stale"] = True  # the site probably changed: work it out again on a later check
            db.update_company(company_id, recipe=json.dumps(recipe))
        db.update_company(company_id, last_checked_at=db.now(), last_error=msg[:500])
        db.log(f"{name}: {msg}", "error")
        return {"error": msg}

    if upgraded:
        db.update_company(company_id, source=upgraded[0], source_key=upgraded[1])
        company.update(source=upgraded[0], source_key=upgraded[1])
        db.log(f"{name}: found an embedded {upgraded[0]} job board, using its feed from now on")

    first_scan = company["last_checked_at"] is None
    known = db.known_job_ids(company_id)
    current = {j["external_id"]: j for j in jobs if j["title"] and j["url"]}

    new_ids = [e for e in current if e not in known]
    reopened = [e for e, s in known.items() if s == "closed" and e in current]
    closed = [e for e, s in known.items() if s == "open" and e not in current]
    for e in new_ids:
        job = current[e]
        if india_only and not area.in_area(job.get("location", "")):
            db.insert_job(company_id, job, is_baseline=False, verdict="out_of_area",
                          reasons=["Outside India and not open to remote work from India"])
        else:
            db.insert_job(company_id, job, is_baseline=False)
    db.set_job_status(company_id, reopened, "open")
    db.set_job_status(company_id, closed, "closed")

    if first_scan:
        db.log(f"{name}: first check found {len(current)} job(s) from the last "
               f"{config.MAX_JOB_AGE_DAYS} days")
    elif new_ids:
        db.log(f"{name}: {len(new_ids)} new job(s) posted")

    emailed = _evaluate_pending(company, current)
    db.update_company(company_id, last_checked_at=db.now(), last_error=None)
    return {"found": len(current), "new": len(new_ids), "closed": len(closed), "emailed": emailed}


def _evaluate_pending(company: dict, current: dict) -> int:
    resume = db.get_resume()
    if not resume:
        return 0
    if not config.kev_configured():
        db.log("KEV_BASE_URL is empty: jobs are saved but not scored. Set it in .env.", "warn")
        return 0

    prefs = profile.for_matching(db.get_setting("preferences", {}) or {})  # confirmed profile + job settings
    if prefs.get("india_only", True):
        # Cheap location check before any model call (matters after a settings change).
        for j in db.pending_jobs_brief(company["id"]):
            if not area.in_area(j["location"]):
                db.update_job(j["id"], verdict="out_of_area",
                              reasons=["Outside India and not open to remote work from India"])
    emailed = 0
    card = matcher.profile_card(prefs, resume["text"])
    my_fields = None  # your fields: from your profile, or read by Kev the first time they're needed
    total, done = db.count_to_evaluate(company["id"]), 0

    def step(text: str):
        _set_progress(company["id"], step=text)

    for job in _queue(company["id"]):
        done += 1
        _set_progress(company["id"], stage="Scoring", job_id=job["id"], job_title=job["title"],
                      done=done, total=max(total, done), step="Reading the posting")
        if not job["description"]:
            fresh = current.get(job["external_id"], {})
            desc = fresh.get("description") or scrapers.fetch_description(company, job)
            if desc:
                job["description"] = desc
                db.update_job(job["id"], description=desc)

        try:
            # Kev reads the job alone (field and level); kept on the job, since it doesn't depend on you.
            kev_job = job.get("kev_job")
            if kev_job is None:
                step("Kev is reading the job (field and level)")
                kev_job = matcher.classify_job(job)
                db.update_job(job["id"], kev_job=kev_job)
            if my_fields is None:
                my_fields = matcher.profile_fields(prefs, card)

            facts = job.get("facts")
            if (matcher.similarity(kev_job, my_fields) >= config.RELATED_THRESHOLD
                    and not matcher.level_mismatch(prefs, matcher.early_level(job, kev_job), None)):
                # In your fields and at your level: only now is it worth a Gemini call.
                if facts is None:
                    step("Gemini is writing the job card")
                    facts = extract.job_facts(company, job)
                    if facts:
                        db.update_job(job["id"], facts=facts)
                if facts and prefs.get("india_only", True) and facts.get("india_eligible") is False:
                    quote = (facts.get("evidence") or {}).get("location")
                    db.update_job(job["id"], verdict="out_of_area", evaluated_at=db.now(),
                                  reasons=["Not open to people in India" + (f' ("{quote}")' if quote else "")])
                    continue
            step("Kev is checking your profile against the requirements")
            result = matcher.evaluate(resume["text"], company, job, prefs, facts, kev_job=kev_job)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, matcher.KevError) and e.stop_run:
                # Kev is down, the key is wrong or it's busy: every later job would fail the
                # same way. Leave them queued (no attempt counted) for the next check.
                db.log(f"{company['name']}: scoring paused: {e}", "error")
                break
            attempts = (job.get("eval_attempts") or 0) + 1
            gave_up = attempts >= config.MAX_EVAL_ATTEMPTS
            db.update_job(job["id"], verdict="failed" if gave_up else "error", reasons=[str(e)[:300]],
                          evaluated_at=db.now(), eval_attempts=attempts,
                          next_attempt_at=None if gave_up else _retry_at(attempts))
            db.log(f"{company['name']}: couldn't score '{job['title']}' (attempt {attempts} of "
                   f"{config.MAX_EVAL_ATTEMPTS}{', giving up' if gave_up else ''}): {e}", "error")
            continue

        db.update_job(job["id"], evaluated_at=db.now(), eval_attempts=0, next_attempt_at=None, **result)
        if result["verdict"] == "eligible" and job["is_baseline"]:
            db.log(f"Eligible (already listed, no email): {job['title']} at {company['name']}")

    # Send alerts for new eligible jobs, including ones whose email failed last time.
    if config.email_configured() and notifier.recipients():
        for job in db.jobs_needing_email(company["id"]):
            if _send_alert(company, job):
                emailed += 1
    return emailed


def _send_alert(company: dict, job: dict) -> bool:
    result = {**job, "reasons": json.loads(job["reasons"] or "[]")}
    attempts = (job["email_attempts"] or 0) + 1
    try:
        ratelimit.acquire("smtp")
        notifier.send_job_alert(company, job, result)
    except Exception as e:  # noqa: BLE001
        gave_up = attempts >= config.MAX_EMAIL_ATTEMPTS
        db.update_job(job["id"], email_attempts=attempts, email_next_at=None if gave_up else _retry_at(attempts))
        db.log(f"Match found ({job['title']} at {company['name']}) but the email failed "
               f"(attempt {attempts} of {config.MAX_EMAIL_ATTEMPTS}{', giving up' if gave_up else ''}): {e}",
               "error")
        return False
    db.update_job(job["id"], emailed_at=db.now(), email_attempts=attempts, email_next_at=None)
    db.log(f"Emailed you about {job['title']} at {company['name']}")
    return True


def check_all() -> None:
    """The timer: every active company of every account."""
    for c in db.all_companies_brief():
        if c["active"]:
            check_company(c["id"])


def submit_check(company_id: int):
    return _pool.submit(check_company, company_id)


def submit_check_all():
    """The current user's active companies."""
    for c in db.list_companies():
        if c["active"]:
            _pool.submit(check_company, c["id"])


def submit_score(company_id: int):
    return _pool.submit(score_company, company_id)


def submit_score_all() -> int:
    """Score the current user's waiting jobs now, company by company. Returns how many companies."""
    waiting = [c for c in db.list_companies() if c.get("pending_jobs")]
    for c in waiting:
        _pool.submit(score_company, c["id"])
    return len(waiting)


def submit_check_everyone():
    for c in db.all_companies_brief():
        if c["active"]:
            _pool.submit(check_company, c["id"])


def load_default_companies() -> dict:
    return json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))


def seed_default_companies(force: bool = False) -> int:
    """Add the starter list once (or again with force=True, e.g. after removing some).
    Companies you already watch are left alone."""
    if not force and db.get_setting("defaults_seeded"):
        return 0
    data = load_default_companies()
    added = 0
    for c in data["companies"]:
        if db.find_company_by_source(c["source"], c["source_key"]) or db.find_company_by_url(c["url"]):
            continue
        snapshot = {"india_jobs": c["india_jobs"], "entry_level_india_jobs": c["entry_level_india_jobs"],
                    "top_cities": c["top_cities"], "date": data["snapshot_date"]}
        db.add_company(c["name"], c["url"], c["source"], c["source_key"],
                       is_default=True, category=c["category"], snapshot=snapshot)
        added += 1
    db.set_setting("defaults_seeded", True)
    if added:
        db.log(f"Added {added} companies from the starter list")
    return added


def submit_rescore():
    """After a new resume or new preferences: re-score every open job (no emails for old jobs)."""
    db.reset_verdicts()
    db.log("Re-scoring all open jobs against your latest resume and preferences")
    submit_check_all()
