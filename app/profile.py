"""Your profile: the facts about you that jobs are compared against.

Flow: every resume upload is read by Gemini into a *draft* (kept on the resume
row) and the profile is marked as needing review. You check the draft on the
dashboard, fix anything wrong, and confirm. Only the confirmed profile is used
for scoring. Until the first confirmation the draft stands in, so jobs are
still scored on day one.

Job-search settings (internships on/off, India only, places you can work) are
separate: they live in the "preferences" setting.
"""
from . import db, extract

FIELDS = ("career_stage", "degree", "graduation_year", "cgpa", "cgpa_scale", "percentage",
          "current_role", "years_experience", "work_history", "skills", "projects", "coursework",
          "fields", "based_in")
# Fields that older versions stored under "preferences".
LEGACY_FIELDS = ("career_stage", "degree", "graduation_year", "cgpa", "percentage",
                 "years_experience", "current_role")


def get() -> dict | None:
    """The confirmed profile, or None if you have never confirmed one."""
    profile = db.get_setting("profile", None)
    if profile is None:
        profile = _from_legacy_preferences()
    return profile


def _from_legacy_preferences() -> dict | None:
    """Carry over a profile typed into the old "Your profile" form, once."""
    prefs = db.get_setting("preferences", {}) or {}
    if not any(prefs.get(k) not in (None, "") for k in LEGACY_FIELDS):
        return None
    profile = empty()
    profile.update({k: prefs.get(k) for k in LEGACY_FIELDS if prefs.get(k) not in (None, "")})
    profile.update(confirmed_at=db.now(), source="earlier version")
    db.set_setting("profile", profile)
    return profile


def empty() -> dict:
    return {"career_stage": "experienced", "degree": None, "graduation_year": None, "cgpa": None,
            "cgpa_scale": None, "percentage": None, "current_role": None, "years_experience": None,
            "work_history": [], "skills": [], "projects": [], "coursework": [], "fields": [], "based_in": []}


def draft() -> dict | None:
    """What Gemini read from the latest resume."""
    r = db.get_resume()
    return (r or {}).get("facts")


def needs_review() -> bool:
    return bool(db.get_setting("profile_needs_review", False))


def mark_uploaded() -> None:
    db.set_setting("profile_needs_review", True)


def for_review() -> dict:
    """The form's starting values: the new draft if there is one to check, else the saved profile.
    A value Gemini didn't find keeps what you confirmed before, so nothing is lost."""
    saved, new = get(), draft()
    base = {**empty(), **(saved or {})}
    if needs_review() and new:
        for k in FIELDS:
            if new.get(k) not in (None, "", []):
                base[k] = new[k]
    return {k: base.get(k) for k in FIELDS}


def confirm(data: dict) -> dict:
    profile = {**empty(), **{k: data.get(k) for k in FIELDS if k in data}}
    if profile["years_experience"] is None and profile["work_history"]:
        profile["years_experience"] = extract.years_of_experience(profile["work_history"])
    if profile["cgpa"] and not profile["cgpa_scale"]:
        profile["cgpa_scale"] = 4.0 if profile["cgpa"] <= 4 else 10.0
    profile.update(confirmed_at=db.now(), source="you")
    db.set_setting("profile", profile)
    db.set_setting("profile_needs_review", False)
    return profile


def for_matching(prefs: dict) -> dict:
    """Preferences plus profile, as one dict for the matcher. The confirmed profile
    wins; before the first confirmation the resume draft is used."""
    facts = get() or draft() or {}
    merged = dict(prefs or {})
    for k in FIELDS:
        if facts.get(k) not in (None, "", []):
            merged[k] = facts[k]
    merged.setdefault("career_stage", "experienced")
    return merged
