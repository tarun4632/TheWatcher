"""Read facts out of job postings and resumes with Gemini (free tier).

Gemini only *reads*: it turns "2025/26 pass-outs", "minimum five years" or
"60% aggregate throughout" into numbers, and a long posting into a short job card
(summary, must-haves, nice-to-haves). The comparisons with your profile happen in
matcher.py: code for the fixed rules, Kev for the judgments. Dates are returned as text and turned into years of
experience here, because models are unreliable at date arithmetic.

Everything degrades gracefully: with no key, a used-up daily quota, a cooldown
after 429s, or any error, these functions return None and the caller falls
back to the built-in text rules.
"""
import base64
import json
import threading
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config, db, ratelimit
from .matcher import FIELDS

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
JOB_CHARS = 12000
RESUME_CHARS = 20000
LEVELS = ("internship", "fresher", "experienced", "senior")


class GeminiError(RuntimeError):
    pass


def _nullable(kind: str, description: str) -> dict:
    return {"type": kind, "nullable": True, "description": description}


def _strings(description: str) -> dict:
    return {"type": "ARRAY", "items": {"type": "STRING"}, "description": description}


JOB_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "min_years_experience": _nullable(
            "NUMBER", "Minimum years of professional work experience the posting REQUIRES. For a range like "
                      "'2-4 years' use 2. Null if not stated. Ignore years describing the company, team or "
                      "product, and ignore preferred or nice-to-have experience."),
        "batch_years": {"type": "ARRAY", "items": {"type": "INTEGER"},
                        "description": "Graduation years (batches) the role is open to, e.g. '2025/26 pass-outs' "
                                       "-> [2025, 2026]. Empty if not stated."},
        "min_cgpa": _nullable("NUMBER", "Minimum CGPA/GPA required. Null if not stated."),
        "cgpa_scale": _nullable("NUMBER", "Scale of min_cgpa, usually 10 or 4. Null if not stated."),
        "min_percentage": _nullable("NUMBER", "Minimum percentage or aggregate marks required, e.g. '60% "
                                              "throughout' -> 60. Null if not stated."),
        "required_degrees": _strings("Degrees or fields of study that are strictly required. Empty if any "
                                     "degree is fine or none is stated."),
        "mandatory_requirements": _strings("Licenses, certifications, security clearances, citizenship or work "
                                           "permits that are strictly required. Empty if none."),
        "summary": {"type": "STRING", "description": "One sentence on what the person in this job does."},
        "must_have": _strings(
            "The skills, knowledge and experience the posting REQUIRES, one short item each (under 12 words), "
            "at most 8. Split combined requirements into separate items ('Python and SQL' stays one item only if "
            "both are needed together). When the posting accepts alternatives, keep them in one item joined with "
            "'or', e.g. 'Java or Go programming'. Leave out years of experience, degrees, grades, batches, "
            "locations, licences and personality traits (like 'curious' or 'good communicator')."),
        "nice_to_have": _strings("Skills or experience listed as preferred, nice to have or a bonus, one short item "
                                 "each, at most 6. Same rules as must_have."),
        "level": {"type": "STRING", "enum": list(LEVELS),
                  "description": "internship = internship/co-op/temporary student role; fresher = open to new "
                                 "graduates or under 1 year of experience; experienced = needs a few years; "
                                 "senior = senior, lead, principal or management."},
        "india_eligible": _nullable(
            "BOOLEAN", "True if someone living in India could take this job: it is located in India, or it is "
                       "remote and open to India (worldwide, APAC including India, or India). False if it is "
                       "on-site or remote only in other countries. Null if the posting does not say."),
        "evidence": {
            "type": "OBJECT",
            "description": "Short exact quotes (under 120 characters) from the posting behind each answer.",
            "properties": {
                "experience": _nullable("STRING", "Quote behind min_years_experience"),
                "batch": _nullable("STRING", "Quote behind batch_years"),
                "grades": _nullable("STRING", "Quote behind min_cgpa or min_percentage"),
                "location": _nullable("STRING", "Quote behind india_eligible"),
            },
        },
    },
    "required": ["min_years_experience", "batch_years", "min_cgpa", "cgpa_scale", "min_percentage",
                 "required_degrees", "mandatory_requirements", "summary", "must_have", "nice_to_have", "level",
                 "india_eligible", "evidence"],
}

JOB_INSTRUCTIONS = (
    "You extract facts from job postings. Report only what the posting states; never guess. "
    "Use null or an empty list when something is not stated. Requirements under 'preferred', "
    "'nice to have' or 'bonus' are not required."
)

RESUME_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "career_stage": {"type": "STRING", "enum": ["fresher", "experienced"],
                         "description": "fresher = student or recent graduate with under 1 year of full-time "
                                        "work (internships don't count); experienced = otherwise."},
        "degree": _nullable("STRING", "Highest degree and branch, e.g. 'B.Tech, Computer Science'."),
        "graduation_year": _nullable("INTEGER", "Year the highest degree was or will be completed."),
        "cgpa": _nullable("NUMBER", "CGPA/GPA of the highest degree as written."),
        "cgpa_scale": _nullable("NUMBER", "Scale of that CGPA, usually 10 or 4."),
        "percentage": _nullable("NUMBER", "Percentage marks of the highest degree, if given instead of a CGPA."),
        "current_role": _nullable("STRING", "Most recent full-time job title. Null for students."),
        "work_history": {
            "type": "ARRAY",
            "description": "Every job and internship listed, in any order.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "company": {"type": "STRING"},
                    "start": _nullable("STRING", "Start as YYYY-MM (use -01 if only the year is given)."),
                    "end": _nullable("STRING", "End as YYYY-MM, or 'present'."),
                    "internship": {"type": "BOOLEAN", "description": "True for internships, trainee stints "
                                                                    "during studies, co-ops."},
                },
                "required": ["title", "company", "start", "end", "internship"],
            },
        },
        "skills": _strings("Main technical and professional skills, at most 30."),
        "projects": _strings("Each project, one short line with what it does and the tools used, at most 8."),
        "coursework": _strings("Courses or subjects studied, if listed, at most 15."),
        "fields": {"type": "ARRAY", "items": {"type": "STRING", "enum": list(FIELDS)},
                   "description": "The fields of work this person is prepared for, by their education and "
                                  "experience; usually one or two. " + "; ".join(f"{k} = {v}" for k, v in FIELDS.items())},
        "locations": _strings("City where the candidate lives, plus any stated preferred locations."),
    },
    "required": ["career_stage", "degree", "graduation_year", "cgpa", "cgpa_scale", "percentage",
                 "current_role", "work_history", "skills", "projects", "coursework", "fields", "locations"],
}

RESUME_INSTRUCTIONS = (
    "You extract facts from a resume. Report only what the resume states; never guess. "
    "Use null or an empty list when something is not stated."
)


# ---------------------------------------------------------------------------
# Daily free-tier budget (Google resets it at midnight Pacific time)
# ---------------------------------------------------------------------------
_budget_lock = threading.Lock()


def _pacific_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # noqa: BLE001 - no tz database (common on Windows): PST is close enough
        return (datetime.now(timezone.utc) - timedelta(hours=8)).date().isoformat()


def usage() -> dict:
    day = _pacific_day()
    u = db.get_global_setting("gemini_usage", {}) or {}  # one free-tier budget for every account
    used = u.get("count", 0) if u.get("day") == day else 0
    return {"day": day, "used": used, "limit": config.GEMINI_DAILY_LIMIT}


def _take_budget(force: bool = False) -> bool:
    """Count one request against today's quota. False when it's used up."""
    with _budget_lock:
        u = usage()
        if not force and u["used"] >= u["limit"]:
            return False
        db.set_global_setting("gemini_usage", {"day": u["day"], "count": u["used"] + 1})
        return True


def _exhaust_budget() -> None:
    with _budget_lock:
        u = usage()
        db.set_global_setting("gemini_usage", {"day": u["day"], "count": max(u["used"], u["limit"])})


def available() -> bool:
    if not config.gemini_configured():
        return False
    try:
        ratelimit.check_cooldown("gemini")
    except ratelimit.CoolingDown:
        return False
    return usage()["used"] < config.GEMINI_DAILY_LIMIT


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------
_thinking_supported = True


def _generate(parts: list, schema: dict, instructions: str, *, force: bool = False) -> dict:
    global _thinking_supported
    if not config.gemini_configured():
        raise GeminiError("GEMINI_API_KEY isn't set")
    if not _take_budget(force):
        raise GeminiError(f"today's Gemini quota ({config.GEMINI_DAILY_LIMIT} requests) is used up")

    gen = {"responseMimeType": "application/json", "responseSchema": schema, "temperature": 0}
    if config.GEMINI_THINKING_LEVEL and _thinking_supported:
        gen["thinkingConfig"] = {"thinkingLevel": config.GEMINI_THINKING_LEVEL}
    body = {"systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen}
    url = API.format(model=config.GEMINI_MODEL)
    headers = {"x-goog-api-key": config.GEMINI_API_KEY}

    with httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0)) as c:
        r = ratelimit.send(c, "POST", url, key="gemini", json=body, headers=headers)
        if r.status_code == 400 and "thinking" in r.text.lower() and "thinkingConfig" in gen:
            _thinking_supported = False  # this model doesn't take the setting; don't send it again
            del gen["thinkingConfig"]
            r = ratelimit.send(c, "POST", url, key="gemini", json=body, headers=headers)

    if r.status_code == 429:
        if "perday" in r.text.lower().replace(" ", "").replace("_", ""):
            _exhaust_budget()
        raise GeminiError("Gemini rate limit reached; paused for a while")
    if r.status_code >= 400:
        raise GeminiError(f"Gemini returned {r.status_code}: {r.text[:300]}")
    data = r.json()
    try:
        cand = data["candidates"][0]
        text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        reason = (data.get("candidates") or [{}])[0].get("finishReason") or data.get("promptFeedback")
        raise GeminiError(f"Gemini gave no usable answer ({reason})") from e


# ---------------------------------------------------------------------------
# Cleaning answers (never trust a number blindly)
# ---------------------------------------------------------------------------
def _number(value, lo: float, hi: float) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _text(value, limit: int = 200) -> str | None:
    s = str(value or "").strip()
    return s[:limit] or None


def _list(value, limit: int = 30) -> list[str]:
    return [s for s in (_text(v, 150) for v in (value or [])[:limit]) if s]


def clean_job_facts(raw: dict) -> dict:
    years = _number(raw.get("min_years_experience"), 0, 30)
    cgpa = _number(raw.get("min_cgpa"), 1, 10)
    scale = _number(raw.get("cgpa_scale"), 4, 10)
    if cgpa and not scale:
        scale = 4.0 if cgpa <= 4 else 10.0
    evidence = raw.get("evidence") or {}
    level = raw.get("level")
    india = raw.get("india_eligible")
    return {
        "source": "gemini",
        "min_years": years,
        "batch_years": sorted({int(y) for y in (raw.get("batch_years") or [])
                               if isinstance(y, (int, float)) and 2000 <= y <= 2040}),
        "min_cgpa": cgpa,
        "cgpa_scale": scale if cgpa else None,
        "min_percentage": _number(raw.get("min_percentage"), 30, 100),
        "required_degrees": _list(raw.get("required_degrees"), 10),
        "mandatory_requirements": _list(raw.get("mandatory_requirements"), 10),
        "summary": _text(raw.get("summary"), 300),
        "must_have": [x for x in _list(raw.get("must_have"), 8) if len(x) <= 160],
        "nice_to_have": [x for x in _list(raw.get("nice_to_have"), 6) if len(x) <= 160],
        "level": level if level in LEVELS else None,
        "india_eligible": india if isinstance(india, bool) else None,
        "evidence": {k: _text(evidence.get(k), 160) for k in ("experience", "batch", "grades", "location")
                     if _text(evidence.get(k), 160)},
    }


def _month(value) -> date | None:
    s = str(value or "").strip().lower()
    if s in ("present", "current", "now", "till date", "ongoing"):
        return date.today().replace(day=1)
    try:
        return datetime.strptime(s[:7], "%Y-%m").date()
    except ValueError:
        try:
            return date(int(s[:4]), 1, 1)
        except ValueError:
            return None


def years_of_experience(history: list[dict]) -> float:
    """Full-time months across all jobs, counting overlapping jobs once."""
    spans = []
    for h in history or []:
        if h.get("internship"):
            continue
        start, end = _month(h.get("start")), _month(h.get("end") or "present")
        if start and end and end >= start:
            spans.append((start, end))
    months, cur = 0, None
    for start, end in sorted(spans):
        s = start.year * 12 + start.month
        e = end.year * 12 + end.month + 1  # the end month counts
        if cur and s <= cur[1]:
            cur = (cur[0], max(cur[1], e))
            continue
        if cur:
            months += cur[1] - cur[0]
        cur = (s, e)
    if cur:
        months += cur[1] - cur[0]
    return round(months / 12, 1)


def clean_resume_facts(raw: dict) -> dict:
    history = [
        {"title": _text(h.get("title"), 120) or "", "company": _text(h.get("company"), 120) or "",
         "start": _text(h.get("start"), 20), "end": _text(h.get("end"), 20), "internship": bool(h.get("internship"))}
        for h in (raw.get("work_history") or [])[:30] if isinstance(h, dict)
    ]
    cgpa = _number(raw.get("cgpa"), 1, 10)
    scale = _number(raw.get("cgpa_scale"), 4, 10)
    stage = raw.get("career_stage")
    grad = _number(raw.get("graduation_year"), 1970, 2040)
    return {
        "source": "gemini",
        "career_stage": stage if stage in ("fresher", "experienced") else None,
        "degree": _text(raw.get("degree")),
        "graduation_year": int(grad) if grad else None,
        "cgpa": cgpa,
        "cgpa_scale": (scale or (4.0 if cgpa <= 4 else 10.0)) if cgpa else None,
        "percentage": _number(raw.get("percentage"), 30, 100),
        "current_role": _text(raw.get("current_role")),
        "years_experience": years_of_experience(history),
        "work_history": history,
        "skills": _list(raw.get("skills")),
        "projects": _list(raw.get("projects"), 8),
        "coursework": _list(raw.get("coursework"), 15),
        "fields": [f for f in _list(raw.get("fields"), 6) if f in FIELDS],
        "based_in": _list(raw.get("locations"), 10),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def job_facts(company: dict, job: dict) -> dict | None:
    """Facts from one posting, or None to fall back to the text rules."""
    if not available():
        return None
    posting = (f"Company: {company['name']}\nTitle: {job['title']}\n"
               f"Listed location: {job.get('location') or 'not given'}\n\n"
               f"Posting:\n{(job.get('description') or '(no description; use the title and location)')[:JOB_CHARS]}")
    try:
        return clean_job_facts(_generate([{"text": posting}], JOB_SCHEMA, JOB_INSTRUCTIONS))
    except (GeminiError, httpx.HTTPError) as e:
        db.log(f"Gemini couldn't read '{job['title']}' at {company['name']}, using text rules: {e}", "warn")
        return None


def resume_facts(filename: str, data: bytes, text: str | None) -> dict | None:
    """Facts from a resume. A PDF goes to Gemini as-is, so columns, tables and
    scanned pages are read properly. When `text` is None (local extraction
    failed) Gemini also returns the resume as plain text under `plain_text`."""
    if not config.gemini_configured():
        return None
    schema, prompt = RESUME_SCHEMA, "Extract the facts from this resume."
    if text is None:
        schema = json.loads(json.dumps(RESUME_SCHEMA))
        schema["properties"]["plain_text"] = {
            "type": "STRING", "description": "The full resume as plain text, word for word, in reading order."}
        schema["required"] = schema["required"] + ["plain_text"]
        prompt += " Also transcribe the whole resume as plain text."

    if filename.lower().endswith(".pdf"):
        parts = [{"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(data).decode()}},
                 {"text": prompt}]
    elif text:
        parts = [{"text": f"{prompt}\n\nResume:\n{text[:RESUME_CHARS]}"}]
    else:
        return None
    try:
        # A resume upload always gets a request, even when job scoring used up today's budget.
        raw = _generate(parts, schema, RESUME_INSTRUCTIONS, force=True)
    except (GeminiError, httpx.HTTPError) as e:
        db.log(f"Gemini couldn't read your resume, using the text rules: {e}", "warn")
        return None
    facts = clean_resume_facts(raw)
    if text is None and isinstance(raw.get("plain_text"), str):
        facts["plain_text"] = raw["plain_text"].strip()
    return facts
