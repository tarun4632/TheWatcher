from datetime import datetime, timedelta, timezone

import pytest

from app import config, db, monitor


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    # Kev's reading of the job: in your field, so every job goes on to the full check.
    monkeypatch.setattr(monitor.matcher, "classify_job",
                        lambda job: {"fields": {"software": 1.0}, "level": "fresher", "level_confidence": 1.0})
    monkeypatch.setattr(monitor.matcher, "profile_fields", lambda prefs, card: ["software"])
    db.init()


def _job(ext, posted="", location="Bengaluru, India"):
    return {
        "external_id": ext,
        "title": f"Role {ext}",
        "location": location,
        "url": f"https://example.com/jobs/{ext}",
        "description": "",
        "posted_at": posted,
    }


def _rows():
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT external_id, is_baseline, verdict, status FROM jobs ORDER BY external_id")]


def test_first_check_treats_recent_jobs_as_new(fresh_db, monkeypatch):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([
        _job("recent", recent),
        _job("old", old),
        _job("nodate", ""),
    ], None))
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: False)
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")

    result = monitor.check_company(cid)

    rows = {r["external_id"]: r for r in _rows()}
    assert set(rows) == {"recent", "nodate"}
    assert rows["recent"]["is_baseline"] == 0
    assert rows["recent"]["verdict"] == "pending"
    assert result["new"] == 2


def test_later_check_fetches_description_only_for_new_ids(fresh_db, monkeypatch):
    calls = []
    state = {"jobs": [_job("a")]}

    def fetch_jobs(company, india_only=False):
        return list(state["jobs"]), None

    def fetch_description(company, job):
        calls.append(job["external_id"])
        return "Built backend services and APIs. " * 10

    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", fetch_jobs)
    monkeypatch.setattr(monitor.scrapers, "fetch_description", fetch_description)
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.matcher, "evaluate", lambda *args, **kwargs: {
        "verdict": "related",
        "job_level": "experienced",
        "related_p": 0.8,
        "eligible_p": 0.4,
        "blocker_p": 0.1,
        "fit_score": 2,
        "seniority": "match",
        "reasons": ["partial match"],
        "model": "test",
    })
    db.save_resume("cv.txt", "engineer " * 40)
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")

    monitor.check_company(cid)
    assert calls == ["a"]

    calls.clear()
    state["jobs"] = [_job("b")]
    monitor.check_company(cid)

    assert calls == ["b"]
    rows = {r["external_id"]: r for r in _rows()}
    assert rows["a"]["status"] == "closed"
    assert rows["b"]["status"] == "open"
    assert rows["b"]["is_baseline"] == 0


def test_scoring_failures_back_off_then_give_up(fresh_db, monkeypatch):
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([_job("a")], None))
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Some description")
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.config, "MAX_EVAL_ATTEMPTS", 3)
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("model hiccup")

    monkeypatch.setattr(monitor.matcher, "evaluate", boom)
    db.save_resume("cv.txt", "engineer " * 40)
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")

    monitor.check_company(cid)             # attempt 1 fails: retried on the next check
    monitor.check_company(cid)             # attempt 2 fails: now waits 30 minutes
    monitor.check_company(cid)             # still waiting, not tried
    assert len(calls) == 2
    with db.conn() as c:
        c.execute("UPDATE jobs SET next_attempt_at = NULL")  # pretend the 30 minutes passed
    monitor.check_company(cid)             # attempt 3 fails: gives up
    with db.conn() as c:
        row = dict(c.execute("SELECT verdict, eval_attempts, next_attempt_at FROM jobs").fetchone())
    assert row["verdict"] == "failed" and row["eval_attempts"] == 3 and row["next_attempt_at"] is None
    n = len(calls)
    monitor.check_company(cid)             # given up: not tried again
    assert len(calls) == n


def test_rate_limited_scoring_waits_without_counting(fresh_db, monkeypatch):
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([_job("a")], None))
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Some description")
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)

    def limited(*args, **kwargs):
        raise monitor.matcher.KevError("rate-limited", stop_run=True)

    monkeypatch.setattr(monitor.matcher, "evaluate", limited)
    db.save_resume("cv.txt", "engineer " * 40)
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")
    monitor.check_company(cid)
    with db.conn() as c:
        row = dict(c.execute("SELECT verdict, eval_attempts FROM jobs").fetchone())
    assert row == {"verdict": "pending", "eval_attempts": 0}


def test_gemini_location_check_skips_scoring(fresh_db, monkeypatch):
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs",
                        lambda company, india_only=False: ([_job("a", location="Remote")], None))
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Remote, US only")
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.extract, "job_facts", lambda company, job: {
        "source": "gemini", "india_eligible": False, "evidence": {"location": "Remote, US only"}})
    monkeypatch.setattr(monitor.matcher, "evaluate", lambda *a, **k: pytest.fail("should not score"))
    db.save_resume("cv.txt", "engineer " * 40)
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")
    monitor.check_company(cid)
    with db.conn() as c:
        row = dict(c.execute("SELECT verdict, reasons, facts FROM jobs").fetchone())
    assert row["verdict"] == "out_of_area"
    assert "US only" in row["reasons"] and row["facts"]


def test_one_check_scores_the_whole_queue_in_batches(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "MAX_EVALS_PER_RUN", 2)
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: (
        [_job(f"j{i}") for i in range(5)], None))
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Build APIs. " * 10)
    monkeypatch.setattr(monitor.extract, "job_facts", lambda company, job: None)
    scored = []

    def evaluate(resume_text, company, job, prefs, facts=None, **kw):
        scored.append(job["external_id"])
        if job["external_id"] == "j3":
            raise RuntimeError("temporary failure")
        return {"verdict": "related", "job_level": "fresher", "related_p": .8, "eligible_p": .4, "blocker_p": None,
                "fit_score": 2, "seniority": None, "reasons": [], "model": "test"}
    monkeypatch.setattr(monitor.matcher, "evaluate", evaluate)
    db.save_resume("cv.txt", "resume")
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")
    monitor.check_company(cid)
    assert sorted(scored) == ["j0", "j1", "j2", "j3", "j4"]  # 3 batches of 2, one check
    assert {r["verdict"] for r in _rows()} == {"related", "error"}  # j3 waits for its retry, not retried in a loop


def test_progress_shows_which_job_is_being_scored(fresh_db, monkeypatch):
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([_job("a"), _job("b")], None))
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Build APIs. " * 10)
    monkeypatch.setattr(monitor.extract, "job_facts", lambda company, job: None)
    seen = []

    def evaluate(resume_text, company, job, prefs, facts=None, **kw):
        seen.append(monitor.progress([company["id"]])[company["id"]])
        return {"verdict": "related", "job_level": "fresher", "related_p": .8, "eligible_p": .4, "blocker_p": None,
                "fit_score": 2, "seniority": None, "reasons": [], "model": "test"}
    monkeypatch.setattr(monitor.matcher, "evaluate", evaluate)
    db.save_resume("cv.txt", "resume")
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")
    monitor.check_company(cid)
    assert [(p["stage"], p["done"], p["total"]) for p in seen] == [("Scoring", 1, 2), ("Scoring", 2, 2)]
    assert seen[0]["job_title"] in ("Role a", "Role b") and seen[0]["step"].startswith("Kev is checking")
    assert monitor.progress([cid]) == {}  # cleared when the check ends


def test_score_now_scores_saved_jobs_without_reading_the_site(fresh_db, monkeypatch):
    monkeypatch.setattr(monitor.config, "kev_configured", lambda: True)
    monkeypatch.setattr(monitor.config, "email_configured", lambda: False)
    monkeypatch.setattr(monitor.scrapers, "fetch_description", lambda company, job: "Build APIs. " * 10)
    monkeypatch.setattr(monitor.extract, "job_facts", lambda company, job: None)
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda *a, **k: pytest.fail("must not read the careers site"))
    monkeypatch.setattr(monitor.matcher, "evaluate", lambda *a, **k: {
        "verdict": "related", "job_level": "fresher", "related_p": .8, "eligible_p": .4, "blocker_p": None,
        "fit_score": 2, "seniority": None, "reasons": [], "model": "test"})
    db.save_resume("cv.txt", "resume")
    cid = db.add_company("Acme", "https://example.com/jobs", "greenhouse", "acme")
    for e in ("x", "y"):
        db.insert_job(cid, _job(e), is_baseline=False)
    assert db.list_companies()[0]["pending_jobs"] == 2
    assert len(db.list_jobs(verdict="pending")) == 2                       # unscored: under All roles
    assert db.list_jobs(level_group="fresher", verdict="pending") == []  # not in a level section until scored
    monitor.score_company(cid)
    assert {r["verdict"] for r in _rows()} == {"related"} and db.list_companies()[0]["pending_jobs"] == 0
