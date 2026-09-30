"""The scoring pipeline: Kev reads the job alone, Gemini only for jobs in your fields,
code checks the fixed rules, Kev checks each must-have against your profile."""
import pytest

from app import config, db, extract, matcher, monitor

POSTING = """About Acme
Acme is the world's favourite payments company, trusted by millions.
What you'll do
Build backend services.
Minimum requirements
Technical skills:
2+ years of software engineering experience
Strong Python or Go programming
Experience designing REST APIs
Preferred qualifications
Kubernetes
Benefits
Free lunch"""

PROFILE = {"career_stage": "fresher", "degree": "B.Tech Computer Science", "graduation_year": 2026, "cgpa": 8.3,
           "skills": ["Python", "SQL"], "projects": ["Expense tracker (React, Node.js)"],
           "coursework": ["DBMS", "Machine Learning"], "fields": ["software"],
           "work_history": [{"title": "SDE Intern", "company": "PayNest", "start": "2025-05", "end": "2025-07",
                             "internship": True}]}


class FakeKev:
    """Answers like Kev: `fields` for the job's field, `has` for which requirements the profile meets."""

    def __init__(self, field="software", level="fresher", has=()):
        self.field, self.level, self.has, self.calls = field, level, set(has), []

    def __call__(self, state, questions):
        self.calls.append((state, questions))
        answers = {}
        for k, q in questions.items():
            if q["type"] == "choice" and k == "level":
                answers[k] = {"type": "choice", "choice": self.level, "confidence": 0.9}
            elif q["type"] == "choice":
                answers[k] = {"type": "choice", "choice": self.field, "confidence": 0.9,
                              "probabilities": {f: (0.9 if f == self.field else 0.02) for f in matcher.FIELDS}}
            else:
                text = q["instructions"]
                answers[k] = {"type": "noul", "noul": 0.95 if any(h in text for h in self.has) else 0.05}
        return {"answers": answers, "model": "kev-test"}


@pytest.fixture
def kev(monkeypatch):
    def make(**kw):
        fake = FakeKev(**kw)
        monkeypatch.setattr(matcher, "_call_kev", fake)
        matcher._profile_fields_cache.clear()
        return fake
    return make


# --- reading the posting -------------------------------------------------------
def test_requirements_snippet_skips_the_company_blurb():
    snippet = matcher.requirements_snippet(POSTING)
    assert snippet.startswith("Minimum requirements") and "favourite payments" not in snippet


def test_requirement_lines_without_gemini():
    # Years lines are left to code; the preferred section and benefits aren't must-haves.
    assert matcher.requirement_lines(POSTING) == ["Strong Python or Go programming", "Experience designing REST APIs"]


def test_either_or_requirements_are_split():
    assert matcher.split_alternatives("Programming in Java, Ruby or Go") == [
        "Programming in Java", "Programming in Ruby", "Programming in Go"]
    assert matcher.split_alternatives("Python and SQL") == ["Python and SQL"]


def test_profile_card_is_short_and_complete():
    card = matcher.profile_card(PROFILE)
    assert card["education"] == "B.Tech Computer Science, graduating 2026, CGPA 8.3/10"
    assert "SDE Intern at PayNest (internship, 2025-05 to 2025-07)" in card["experience"]
    assert card["coursework"] == "DBMS, Machine Learning" and card["skills"] == "Python, SQL"


# --- the decision ----------------------------------------------------------------
JOB = {"title": "Backend Engineer, New Grad", "location": "Bengaluru", "description": POSTING}
CARD = {"source": "gemini", "min_years": None, "batch_years": [], "min_cgpa": None, "cgpa_scale": None,
        "min_percentage": None, "required_degrees": ["Computer Science"], "mandatory_requirements": [],
        "must_have": ["Python or Go programming", "Designing REST APIs", "3+ years with Kafka"],
        "nice_to_have": ["Kubernetes"], "level": "fresher", "india_eligible": True, "evidence": {}}


def test_job_outside_your_fields_is_not_a_fit(kev):
    fake = kev(field="sales_marketing")
    r = matcher.evaluate("", {"name": "Acme"}, JOB, PROFILE, None)
    assert r["verdict"] == "not_related" and "looks like Sales and marketing" in r["reasons"][0]
    assert len(fake.calls) == 1  # only the job was read; no requirement questions


def test_all_must_haves_met_is_eligible(kev):
    fake = kev(has=["Python", "REST APIs", "Computer Science"])
    r = matcher.evaluate("", {"name": "Acme"}, JOB, PROFILE, CARD)
    assert r["verdict"] == "eligible", r["reasons"]
    assert "Has 3 of 3 must-have requirements" in r["reasons"]  # the Kafka years item went to code
    req_state, req_questions = fake.calls[-1]
    assert req_state == matcher.profile_card(PROFILE)  # asked against your profile, not the posting
    asked = [q["instructions"] for q in req_questions.values()]
    assert "Does this candidate have: Python programming?" in asked and "Does this candidate have: Go programming?" in asked
    assert "Does this candidate have: Kubernetes?" in asked  # nice-to-haves count towards fit only


def test_a_missing_must_have_is_named(kev):
    kev(has=["Python", "Computer Science"])
    r = matcher.evaluate("", {"name": "Acme"}, JOB, PROFILE, CARD)
    assert r["verdict"] == "related"
    assert any(x.startswith("Missing: Designing REST APIs") for x in r["reasons"])


def test_fixed_rules_still_block(kev):
    kev(has=["Python", "REST APIs", "Computer Science"])
    r = matcher.evaluate("", {"name": "Acme"}, JOB, {**PROFILE, "graduation_year": 2024}, {**CARD, "batch_years": [2026]})
    assert r["verdict"] == "related" and any("Open to 2026 batch" in x for x in r["reasons"])


def test_your_fields_come_from_kev_when_you_havent_chosen(kev):
    fake = kev(field="software")
    assert matcher.profile_fields({**PROFILE, "fields": []}, matcher.profile_card(PROFILE)) == ["software"]
    matcher.profile_fields({**PROFILE, "fields": []}, matcher.profile_card(PROFILE))
    assert len(fake.calls) == 1  # read once, then cached
    assert matcher.profile_fields(PROFILE, {}) == ["software"]  # your own choice wins


# --- the monitor runs it cheapest step first -------------------------------------
@pytest.fixture
def one_job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    monkeypatch.setattr(config, "email_configured", lambda: False)
    monkeypatch.setattr(config, "kev_configured", lambda: True)
    db.init()
    db.save_resume("cv.txt", "resume text")
    db.set_setting("profile", PROFILE)
    cid = db.add_company("Acme", "https://acme.test/jobs", "greenhouse", "acme")
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: (
        [{"external_id": "1", **JOB, "url": "https://acme.test/jobs/1", "posted_at": ""}], None))
    gemini = []
    monkeypatch.setattr(monitor.extract, "job_facts", lambda company, job: gemini.append(job["title"]) or CARD)
    return cid, gemini


def _row():
    with db.conn() as c:
        return dict(c.execute("SELECT verdict, kev_job, facts FROM jobs").fetchone())


def test_jobs_outside_your_fields_never_reach_gemini(one_job, kev):
    cid, gemini = one_job
    kev(field="finance_legal")
    monitor.check_company(cid)
    assert _row()["verdict"] == "not_related" and gemini == []


def test_jobs_in_your_fields_get_a_job_card_once(one_job, kev):
    cid, gemini = one_job
    fake = kev(has=["Python", "REST APIs", "Computer Science"])
    monitor.check_company(cid)
    assert _row()["verdict"] == "eligible" and gemini == ["Backend Engineer, New Grad"]
    job_reads = sum(1 for _, q in fake.calls if "level" in q)
    db.reset_verdicts()  # e.g. you changed your profile
    monitor.check_company(cid)
    assert gemini == ["Backend Engineer, New Grad"]                         # no second Gemini call
    assert sum(1 for _, q in fake.calls if "level" in q) == job_reads       # nor a second job read
    assert _row()["verdict"] == "eligible"


def test_nothing_checked_is_never_eligible(kev):
    """A careers-page link that isn't a job ('Learn more about our alumni program') has no requirements."""
    kev(has=["anything"])
    blog = {"title": "Learn more about Alumni program", "location": "", "description": "Once an IBMer, always an IBMer."}
    r = matcher.evaluate("", {"name": "IBM"}, blog, PROFILE, {**CARD, "must_have": [], "nice_to_have": [],
                                                           "required_degrees": []})
    assert r["verdict"] == "related" and r["eligible_p"] is None
    assert any("Couldn't find this job's requirements" in x for x in r["reasons"])


def test_alert_email_without_a_requirement_score(monkeypatch):
    from app import notifier
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda to, subject, text, html: sent.append(html))
    monkeypatch.setattr(config, "SMTP_USER", "bot@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "x")
    monkeypatch.setattr(notifier, "recipients", lambda: ["me@example.com"])
    notifier.send_job_alert({"name": "Acme"}, {"title": "Engineer", "url": "https://acme.test/1"},
                            {"fit_score": 3, "eligible_p": None, "job_level": "fresher", "reasons": ["ok"]})
    assert sent and "Weakest requirement" not in sent[0]


# --- level: senior roles never reach a fresher's Related list ------------------------------
def test_senior_role_is_not_your_level_for_a_fresher(kev):
    fake = kev(level="senior", has=["Python", "REST APIs", "Computer Science"])
    staff = {**JOB, "title": "Staff Software Engineer"}
    r = matcher.evaluate("", {"name": "Acme"}, staff, PROFILE, CARD)
    assert r["verdict"] == "wrong_level" and r["reasons"] == ["A senior role; your profile is a fresher's"]
    assert not any("Does this candidate have" in q["instructions"] for _, qs in fake.calls for q in qs.values())


def test_years_make_a_role_experienced_for_a_fresher(kev):
    kev(level="fresher")
    r = matcher.evaluate("", {"name": "Acme"}, {**JOB, "title": "Data Engineer"}, PROFILE, {**CARD, "min_years": 3})
    assert r["verdict"] == "wrong_level" and r["reasons"][0].startswith("Needs 3+ years")


def test_experienced_people_skip_internships_and_entry_roles(kev):
    kev(level="fresher")
    senior_person = {**PROFILE, "career_stage": "experienced", "years_experience": 5}
    assert matcher.evaluate("", {"name": "A"}, {**JOB, "title": "Software Engineer, New Grad"}, senior_person,
                            CARD)["verdict"] == "wrong_level"
    assert matcher.level_mismatch(senior_person, "internship", None) == "An internship; your profile is an experienced one"
    assert matcher.level_mismatch(senior_person, "senior", 6) is None


def test_wrong_level_jobs_are_hidden_and_cost_no_gemini(one_job, kev, monkeypatch):
    cid, gemini = one_job
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: (
        [{"external_id": "9", **JOB, "title": "Principal Engineer", "url": "https://acme.test/jobs/9", "posted_at": ""}], None))
    kev(level="senior")
    monitor.check_company(cid)
    assert _row()["verdict"] == "wrong_level" and gemini == []
    assert db.list_jobs() == [] and db.verdict_counts().get("related", 0) == 0
