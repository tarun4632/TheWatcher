import pytest
from fastapi.testclient import TestClient

from app import config, db, extract, main, monitor, profile

RESUME = ("Priya Sharma. B.Tech Computer Science, 2025, CGPA 8.4. Python, SQL, React. " * 10).encode()
GEMINI_READ = {
    "source": "gemini", "career_stage": "fresher", "degree": "B.Tech, Computer Science", "graduation_year": 2025,
    "cgpa": 8.4, "cgpa_scale": 10.0, "percentage": None, "current_role": None, "years_experience": 0.0,
    "work_history": [{"title": "SDE Intern", "company": "Acme", "start": "2024-05", "end": "2024-07",
                      "internship": True}],
    "skills": ["Python", "SQL", "React"], "based_in": ["Pune"],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    monkeypatch.setattr(config, "LOAD_DEFAULT_COMPANIES", False)
    rescores = []
    monkeypatch.setattr(monitor, "submit_rescore", lambda: rescores.append(1))
    monkeypatch.setattr(extract, "resume_facts", lambda name, data, text: dict(GEMINI_READ))
    with TestClient(main.app) as c:
        c.post("/api/auth/signup", json={"username": "priya", "password": "correct horse"})
        c.rescores = rescores
        yield c


def _upload(c):
    return c.post("/api/resume", files={"file": ("cv.txt", RESUME, "text/plain")})


def test_upload_asks_for_review_before_scoring(client):
    r = _upload(client).json()
    assert r["read_by_gemini"] and r["profile"]["degree"] == "B.Tech, Computer Science"
    assert client.rescores == []  # nothing is re-scored until you confirm
    p = client.get("/api/profile").json()
    assert p["needs_review"] and p["confirmed"] is None
    assert p["form"]["graduation_year"] == 2025 and p["form"]["based_in"] == ["Pune"]
    assert client.get("/api/state").json()["profile"]["needs_review"] is True


def test_confirm_with_corrections(client):
    _upload(client)
    form = client.get("/api/profile").json()["form"]
    form.update(cgpa=8.1, career_stage="experienced", years_experience=None, work_history=[
        {"title": "Analyst", "company": "Beta", "start": "2023-01", "end": "2023-12", "internship": False},
        {"title": "SDE Intern", "company": "Acme", "start": "2022-05", "end": "2022-07", "internship": True},
        {"title": "", "company": "", "start": None, "end": None, "internship": False},  # empty row is dropped
    ])
    r = client.post("/api/profile", json=form)
    assert r.status_code == 200
    saved = r.json()["profile"]
    assert saved["cgpa"] == 8.1 and saved["career_stage"] == "experienced"
    assert saved["years_experience"] == 1.0  # worked out from the full-time job only
    assert len(saved["work_history"]) == 2
    assert client.rescores == [1]
    p = client.get("/api/profile").json()
    assert not p["needs_review"] and p["confirmed"]["cgpa"] == 8.1


def test_reupload_keeps_what_gemini_missed(client, monkeypatch):
    _upload(client)
    form = client.get("/api/profile").json()["form"]
    client.post("/api/profile", json={**form, "percentage": 78})
    monkeypatch.setattr(extract, "resume_facts", lambda n, d, t: {**GEMINI_READ, "degree": None, "cgpa": 9.0})
    _upload(client)
    form = client.get("/api/profile").json()["form"]
    assert form["degree"] == "B.Tech, Computer Science"  # kept from the confirmed profile
    assert form["cgpa"] == 9.0                            # new reading shown for you to check
    assert form["percentage"] == 78


def test_bad_values_are_rejected(client):
    _upload(client)
    form = client.get("/api/profile").json()["form"]
    bad = {**form, "work_history": [{"title": "X", "company": "Y", "start": "2023-13", "end": None}]}
    assert client.post("/api/profile", json=bad).status_code == 422
    assert client.post("/api/profile", json={**form, "cgpa": 11}).status_code == 422


def test_scoring_uses_confirmed_profile_and_keeps_job_preferences(client):
    _upload(client)
    form = client.get("/api/profile").json()["form"]
    client.post("/api/profile", json={**form, "graduation_year": 2024})
    client.post("/api/preferences", json={"locations": "Pune, remote", "include_internships": False,
                                          "india_only": True})
    merged = profile.for_matching(db.get_setting("preferences"))
    assert merged["graduation_year"] == 2024
    assert merged["locations"] == "Pune, remote" and merged["include_internships"] is False
    assert merged["based_in"] == ["Pune"]


def test_profile_from_older_version_is_carried_over(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "old.db"))
    db.init()
    db.set_setting("preferences", {"career_stage": "fresher", "degree": "BCA", "graduation_year": 2026,
                                   "india_only": True})
    p = profile.get()
    assert p["degree"] == "BCA" and p["career_stage"] == "fresher" and p["graduation_year"] == 2026
    assert db.get_setting("profile")["degree"] == "BCA"
