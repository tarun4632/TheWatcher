import json

import httpx
import pytest

from app import config, db, extract, matcher


@pytest.fixture
def gemini(tmp_path, monkeypatch):
    """A fresh database and a fake Gemini that answers with `reply["json"]`."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(extract, "_thinking_supported", True)
    db.init()
    reply = {"json": {}, "requests": [], "status": []}
    real_client = httpx.Client

    def handler(request):
        body = json.loads(request.content)
        reply["requests"].append(body)
        if reply["status"]:
            return reply["status"].pop(0)
        return httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"text": json.dumps(reply["json"])}]}, "finishReason": "STOP"}]})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return reply


JOB_ANSWER = {
    "min_years_experience": None, "batch_years": [2025, 2026], "min_cgpa": 7.5, "cgpa_scale": 10,
    "min_percentage": 60, "required_degrees": ["B.Tech"], "mandatory_requirements": [],
    "level": "fresher", "india_eligible": True,
    "evidence": {"batch": "Open to 2025/26 pass-outs", "grades": "60% throughout, 7.5 CGPA"},
}


def test_job_facts_request_and_cleaning(gemini):
    gemini["json"] = JOB_ANSWER
    facts = extract.job_facts({"name": "Acme"}, {"title": "Graduate Engineer", "description": "..."})
    assert facts["batch_years"] == [2025, 2026]
    assert facts["min_percentage"] == 60 and facts["min_cgpa"] == 7.5 and facts["cgpa_scale"] == 10
    assert facts["evidence"]["batch"] == "Open to 2025/26 pass-outs"
    body = gemini["requests"][0]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"]["required"]
    assert extract.usage()["used"] == 1


def test_daily_budget_falls_back_to_text_rules(gemini, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_DAILY_LIMIT", 1)
    gemini["json"] = JOB_ANSWER
    assert extract.job_facts({"name": "Acme"}, {"title": "A", "description": ""})
    assert extract.job_facts({"name": "Acme"}, {"title": "B", "description": ""}) is None
    assert len(gemini["requests"]) == 1


def test_unsupported_thinking_setting_is_dropped(gemini):
    gemini["status"] = [httpx.Response(400, json={"error": {"message": "thinking_level is not supported"}})]
    gemini["json"] = JOB_ANSWER
    assert extract.job_facts({"name": "Acme"}, {"title": "A", "description": ""})
    assert "thinkingConfig" in gemini["requests"][0]["generationConfig"]
    assert "thinkingConfig" not in gemini["requests"][1]["generationConfig"]


def test_bad_numbers_are_dropped():
    facts = extract.clean_job_facts({**JOB_ANSWER, "min_years_experience": 250, "batch_years": [25, 2026],
                                     "level": "wizard", "india_eligible": "yes"})
    assert facts["min_years"] is None and facts["batch_years"] == [2026]
    assert facts["level"] is None and facts["india_eligible"] is None


def test_years_of_experience_skips_internships_and_overlaps():
    history = [
        {"start": "2020-01", "end": "2021-12", "internship": False},   # 24 months
        {"start": "2021-06", "end": "2022-05", "internship": False},   # overlaps, adds 5
        {"start": "2019-05", "end": "2019-07", "internship": True},    # ignored
    ]
    assert extract.years_of_experience(history) == round(29 / 12, 1)


def test_scanned_pdf_is_transcribed(gemini, monkeypatch):
    from app import resume
    monkeypatch.setattr(resume, "extract_text", lambda name, data: "")
    gemini["json"] = {"career_stage": "fresher", "degree": "B.Tech, CSE", "graduation_year": 2025, "cgpa": 8.2,
                      "cgpa_scale": 10, "percentage": None, "current_role": None, "work_history": [],
                      "skills": ["Python"], "locations": ["Pune"], "plain_text": "Resume text " * 30}
    text, facts = resume.read_resume("cv.pdf", b"%PDF-1.4 fake")
    assert text.startswith("Resume text") and "plain_text" not in facts
    assert facts["graduation_year"] == 2025
    parts = gemini["requests"][0]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "application/pdf"


# --- the matcher uses the facts ---------------------------------------------
def _kev(monkeypatch, level="fresher"):
    """Kev says: a software job at `level`, the profile is software, and every requirement is met."""
    def answer(key, q):
        if q["type"] == "noul":
            return {"type": "noul", "noul": 0.9}
        if key == "level":
            return {"type": "choice", "choice": level, "confidence": 0.9}
        return {"type": "choice", "choice": "software", "confidence": 0.9,
                "probabilities": {f: (0.9 if f == "software" else 0.02) for f in matcher.FIELDS}}
    monkeypatch.setattr(matcher, "_call_kev", lambda state, q: {
        "answers": {k: answer(k, v) for k, v in q.items()}, "model": "kev-test"})
    matcher._profile_fields_cache.clear()


def test_facts_block_wrong_batch_with_quote(monkeypatch):
    _kev(monkeypatch)
    facts = extract.clean_job_facts(JOB_ANSWER)
    prefs = {"career_stage": "fresher", "graduation_year": 2024, "cgpa": 8, "percentage": 70}
    r = matcher.evaluate("resume", {"name": "Acme"}, {"title": "Engineer", "description": "x"}, prefs, facts)
    assert r["verdict"] == "related"
    assert any("2025, 2026 batch" in x and "pass-outs" in x for x in r["reasons"])
    assert "Marks requirement (60%) met" in r["reasons"]


def test_facts_override_misleading_text(monkeypatch):
    """The text rule would read '15 years' as a requirement; Gemini knows it's about the company."""
    _kev(monkeypatch, "experienced")
    desc = "We have over 15 years of experience in fintech. You need 3+ years of experience in Python."
    facts = extract.clean_job_facts({**JOB_ANSWER, "min_years_experience": 3, "batch_years": [],
                                     "min_cgpa": None, "min_percentage": None, "level": "experienced"})
    prefs = {"career_stage": "experienced", "years_experience": 4}
    job = {"title": "Backend Engineer", "description": desc}
    assert matcher.evaluate("r", {"name": "A"}, job, prefs, facts)["verdict"] == "eligible"
    # Without Gemini the text rule reads "15 years" as a requirement, far beyond 4 years: hidden.
    assert matcher.evaluate("r", {"name": "A"}, job, prefs, None)["verdict"] == "wrong_level"


def test_resume_facts_fill_blank_profile(monkeypatch):
    _kev(monkeypatch)
    facts = extract.clean_job_facts(JOB_ANSWER)
    r = matcher.evaluate("r", {"name": "A"}, {"title": "Engineer", "description": ""},
                         {"career_stage": "fresher"}, facts, {"graduation_year": 2025, "percentage": 55})
    assert "Open to your batch (2025)" in r["reasons"]
    assert r["verdict"] == "related"  # 55% is under the 60% cutoff
