"""Decide whether a job suits you: Kev for the judgments, code for the fixed rules.

The pipeline (monitor.py runs it, cheapest step first):

1. Code: location check.
2. Kev reads the job alone (title + the requirements part of the posting + the years
   code read from it): its field and its level. Is it in your fields? If not, stop
   here, before any Gemini quota is used.
3. Gemini writes a job card once per job (extract.py): must-have and nice-to-have
   requirements, years, batch, CGPA, degrees, licences.
4. Code checks the fixed rules: level vs. your stage, years, batch, CGPA, marks.
5. Kev checks each must-have against your profile card, one yes/no question each.
6. Eligible = in your fields, no rule broken, every must-have met. Otherwise Related,
   with what's missing named.

Kev only ever sees one short text and one clear question at a time. In tests on real
postings that shape was right 91-98% of the time, where asking Kev to compare a whole
resume with a whole posting was right 44% of the time (see the README).
"""
import os
import re

import httpx

from . import config, ratelimit

OVERQUALIFIED_YEARS = 3       # experienced people with this many years skip entry-level roles
LEVELS = ("internship", "fresher", "experienced", "senior")


class KevError(RuntimeError):
    """`stop_run` means later jobs would fail the same way (server down, wrong
    key, rate-limited), so the rest of this check should wait."""

    def __init__(self, message: str, stop_run: bool = False):
        super().__init__(message)
        self.stop_run = stop_run


# ---------------------------------------------------------------------------
# Kev API
# ---------------------------------------------------------------------------
def _call_kev(state: dict, questions: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if config.KEV_API_KEY:
        headers["Authorization"] = f"Bearer {config.KEV_API_KEY}"
    body = {"model": config.KEV_MODEL, "state": state, "questions": questions}
    url = f"{config.KEV_BASE_URL}/v1/systemone"

    try:
        with httpx.Client(timeout=config.KEV_TIMEOUT_SECONDS) as c:
            r = ratelimit.send(c, "POST", url, key="kev", json=body, headers=headers)
    except ratelimit.CoolingDown as e:
        raise KevError(f"Kev is busy: {e}", stop_run=True) from e
    except httpx.ConnectError as e:
        raise KevError(f"Couldn't reach Kev at {config.KEV_BASE_URL}. Is the Kev server running?",
                       stop_run=True) from e
    except httpx.HTTPError as e:
        raise KevError(f"Kev request failed: {e!r}") from e
    if r.status_code == 429:
        raise KevError("Kev kept answering 'busy'; pausing until the next check", stop_run=True)
    if r.status_code >= 400:
        raise KevError(f"Kev returned {r.status_code}: {r.text[:300]}", stop_run=r.status_code in (401, 403, 404))
    return r.json()


# ---------------------------------------------------------------------------
# Things code reads from the posting (numbers and dates stay out of Kev)
# ---------------------------------------------------------------------------
def required_years(description: str) -> int | None:
    """Largest 'N+ years ... experience' requirement found in the posting."""
    pattern = re.compile(
        r"(\d{1,2})\s*(?:\+|plus|-\s*\d{1,2}|to\s*\d{1,2})?\s*(?:\+)?\s*(?:years?|yrs?)\b[^.\n]{0,50}?experience",
        re.I,
    )
    found = [int(m.group(1)) for m in pattern.finditer(description or "")]
    found = [n for n in found if 0 < n <= 20]
    return max(found) if found else None


_SEP = r"\s*(?:-|–|to|/|&|and|,)\s*"
_BATCH_PATTERNS = [
    # "2025 batch", "2025/2026 pass-outs", "2024-2026 graduates"
    re.compile(rf"(?:(20\d\d){_SEP})?(20\d\d)\s*(?:batch|pass[\s-]?outs?|passing out|graduates?|graduating)", re.I),
    # "batch of 2025", "graduating in 2026", "class of 2025-26", "passing year: 2025"
    re.compile(rf"(?:batch|class|graduat\w*|pass(?:ing)?[\s-]?(?:out|year))\s*(?:of|in|year)?\s*:?\s*(20\d\d)(?:{_SEP}(20\d\d|\d\d))?", re.I),
]


def batch_years(description: str) -> set[int]:
    """Graduation years a fresher posting is open to, e.g. {2025, 2026}."""
    years: set[int] = set()
    for pattern in _BATCH_PATTERNS:
        for m in pattern.finditer(description or ""):
            nums = []
            for g in m.groups():
                if g:
                    n = int(g)
                    nums.append(n + 2000 if n < 100 else n)
            if not nums:
                continue
            lo, hi = min(nums), max(nums)
            if hi - lo <= 5:
                years.update(range(lo, hi + 1))
    return {y for y in years if 2015 <= y <= 2035}


_CGPA_PATTERNS = [
    re.compile(r"(?:cgpa|gpa|cpi)\s*(?:of\s*)?(?:(?:>=|≥|>|at least|minimum(?: of)?|min\.?|above|over|:)\s*)?(\d{1,2}(?:\.\d{1,2})?)", re.I),
    re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*\+?\s*(?:cgpa|gpa|cpi)", re.I),
]


def min_cgpa(description: str) -> float | None:
    found = []
    for pattern in _CGPA_PATTERNS:
        for m in pattern.finditer(description or ""):
            v = float(m.group(1))
            if 2 <= v <= 10:
                found.append(v)
    return min(found) if found else None


def _title_level(title: str) -> str | None:
    t = title.lower()
    if re.search(r"\b(intern|internship|summer analyst|co-?op)\b", t):
        return "internship"
    if re.search(r"\b(senior|sr\.?|lead|principal|staff|head|director|manager|architect|vp)\b", t):
        return "senior" if re.search(r"\b(principal|staff|head|director|vp)\b", t) else "experienced"
    if re.search(r"\b(graduate|new grad|fresher|entry[- ]level|trainee|apprentice|campus)\b", t):
        return "fresher"
    return None


def resolve_level(title: str, kev_level: str, kev_conf: float, need_years: float | None,
                  read_level: str | None = None) -> str:
    """Combine the model's reading of the level with hard signals from the title and years.
    `read_level` is Gemini's reading of the posting and wins over Kev's when present."""
    if read_level in LEVELS:
        kev_level, kev_conf = read_level, 1.0
    level = kev_level if kev_level in LEVELS else "experienced"
    from_title = _title_level(title)
    if from_title == "internship":
        return "internship"
    if from_title in ("experienced", "senior") and level in ("internship", "fresher"):
        level = from_title
    if need_years is not None:
        if need_years >= 2 and level in ("internship", "fresher"):
            level = "experienced"
        elif need_years >= 7 and level == "experienced":
            level = "senior"
        elif need_years <= 1 and level == "experienced" and kev_conf < 0.6:
            level = "fresher"
    if from_title == "fresher" and level == "experienced" and (need_years or 0) < 2:
        level = "fresher"
    return level


# ---------------------------------------------------------------------------
# Short texts for Kev (it was trained on inputs of up to ~384 tokens)
# ---------------------------------------------------------------------------
# Headings that start the part of a posting that says what the job needs. The
# strong ones win over "who you are"-style headings, which often open a culture blurb.
_STRONG_HEAD = re.compile(r"(minimum|basic|required|must[- ]have)\b.*|requirements|qualifications|"
                          r"what you('ll)? (need|bring)|what we('re| are) looking for|skills (&|and) experience", re.I)
_WEAK_HEAD = re.compile(r"(you (have|bring|are)|about you|who you are|experience you('ll)? need)", re.I)
_PREFERRED_HEAD = re.compile(r"(preferred|nice[- ]to[- ]have|bonus|plus|good to have)", re.I)
_END_HEAD = re.compile(r"^(about (us|the company|the team)|who we are|our (mission|story|culture)|benefits|perks|"
                       r"what we offer|why join|equal opportunity|we are an equal|eeo|compensation|pay range|salary)",
                       re.I)
_YEARS = re.compile(r"\b\d+\s*\+?\s*(?:-\s*\d+\s*)?(?:years?|yrs?)\b", re.I)


def _lines(text: str) -> list[str]:
    return [" ".join(l.split()) for l in (text or "").split("\n") if l.strip()]


def _requirements_start(lines: list[str]) -> int | None:
    for pattern in (_STRONG_HEAD, _WEAK_HEAD):
        for i, line in enumerate(lines):
            if len(line) < 80 and pattern.search(line):
                return i
    return None


def requirements_snippet(description: str, n: int = 800) -> str:
    """The requirements part of a posting, for Kev. Company blurbs mislead it: in tests,
    800 characters of requirements beat the first 800 characters of the posting (91% vs 76%)."""
    lines = _lines(description)
    start = _requirements_start(lines)
    if start is not None:
        return " ".join(lines[start:])[:n]
    return " ".join(l for l in lines if not _END_HEAD.match(l))[:n]


def requirement_lines(description: str, limit: int = 8) -> list[str]:
    """Must-have lines read by code, for when Gemini hasn't written a job card.
    Lines about years of experience are left out: code checks those."""
    lines = _lines(description)
    start = _requirements_start(lines)
    if start is None:
        return []
    out = []
    for line in lines[start + 1:]:
        if _END_HEAD.match(line) or (len(line) < 60 and _PREFERRED_HEAD.search(line)):
            break
        if 15 <= len(line) <= 250 and not _YEARS.search(line) and not line.endswith(":"):  # skip sub-headings
            out.append(line.rstrip(".;"))
        if len(out) >= limit:
            break
    return out


def split_alternatives(requirement: str) -> list[str]:
    """'Programming in Java, Ruby or Go' -> one requirement per option. Kev scored the whole
    list 15% for someone who knows Java; asked one option at a time it gets it right."""
    m = re.match(r"^(.*?)((?:[A-Z][\w+#./-]*)(?:, [A-Z][\w+#./-]*)*,? or [A-Z][\w+#./-]*)(.*)$", requirement)
    if not m:
        return [requirement]
    head, items, tail = m.groups()
    return [f"{head}{o}{tail}".strip() for o in re.split(r",? or |, ", items) if o]


# ---------------------------------------------------------------------------
# Questions for Kev: one short text each, one clear question each
# ---------------------------------------------------------------------------
FIELDS = {
    "software": "software engineering, backend, frontend, mobile, DevOps, SRE, security engineering, QA",
    "data_ml": "data engineering, data science, data analysis, machine learning, applied science",
    "product_design": "product management, UX or visual design",
    "sales_marketing": "sales, account management, business development, partnerships, marketing, communications",
    "finance_legal": "finance, accounting, tax, trading, risk, banking, compliance, legal",
    "other": "HR, recruiting, operations, procurement, strategy, anything else",
}
FIELD_LABELS = {
    "software": "Software engineering", "data_ml": "Data and machine learning", "product_design": "Product and design",
    "sales_marketing": "Sales and marketing", "finance_legal": "Finance and legal", "other": "Other (HR, operations, etc.)",
}
FIELD_QUESTION = {"type": "choice", "instructions": "Which field is this job in?", "criteria": FIELDS}
JOB_LEVEL_QUESTION = {
    "type": "choice",
    "instructions": "What level is this job?",
    "criteria": {
        "internship": "an internship, trainee or apprenticeship",
        "fresher": "entry level for new graduates or freshers, under 2 years of experience",
        "experienced": "mid level, about 2 to 6 years of experience",
        "senior": "senior, staff, principal, lead, manager, director or head, or 7+ years",
    },
}
PROFILE_FIELD_QUESTION = {"type": "choice", "instructions": "Which field is this person's education and work in?",
                          "criteria": FIELDS}


def classify_job(job: dict) -> dict:
    """Kev reads the job alone: its field and level. Doesn't depend on who is asking, so the
    result is kept on the job. Tested: field 91%, level 93% with the code corrections."""
    years = required_years(job.get("description") or "")
    state = {"title": job["title"],
             "requirements": requirements_snippet(job.get("description") or "") or "(no description)",
             "years_of_experience_required": f"{years:g}+ years" if years is not None else "not stated"}
    a = _call_kev(state, {"field": FIELD_QUESTION, "level": JOB_LEVEL_QUESTION}).get("answers", {})
    field = a.get("field", {})
    level = a.get("level", {})
    return {"fields": {k: float(v) for k, v in (field.get("probabilities") or {}).items()},
            "level": level.get("choice"), "level_confidence": float(level.get("confidence") or 0)}


def similarity(kev_job: dict, my_fields: list[str]) -> float:
    """Kev's probability that the job is in one of your fields."""
    return sum(p for f, p in (kev_job.get("fields") or {}).items() if f in my_fields)


def main_field(kev_job: dict) -> str:
    probs = kev_job.get("fields") or {}
    return max(probs, key=probs.get) if probs else "other"


def profile_card(prefs: dict, resume_text: str = "") -> dict:
    """Your confirmed profile as a short text for Kev: what you studied and what you've done."""
    card = {}
    edu = [prefs.get("degree")]
    if prefs.get("graduation_year"):
        edu.append(f"graduating {int(prefs['graduation_year'])}")
    if prefs.get("cgpa"):
        edu.append(f"CGPA {prefs['cgpa']:g}/{(prefs.get('cgpa_scale') or 10):g}")
    if any(edu):
        card["education"] = ", ".join(x for x in edu if x)
    work = []
    for w in prefs.get("work_history") or []:
        if w.get("title") or w.get("company"):
            span = " to ".join(x for x in (w.get("start"), w.get("end")) if x)
            kind = "internship" if w.get("internship") else "job"
            work.append(f"{w.get('title') or 'Role'} at {w.get('company') or 'a company'} ({kind}{', ' + span if span else ''})")
    if work:
        card["experience"] = "; ".join(work)
    for key in ("projects", "coursework", "skills"):
        if prefs.get(key):
            card[key] = "; ".join(prefs[key]) if key == "projects" else ", ".join(prefs[key])
    if not (card.get("skills") or card.get("experience") or card.get("projects")) and resume_text:
        card["resume"] = resume_text[:1500]  # no profile yet: a short slice of the resume
    return card


_profile_fields_cache: dict[str, list[str]] = {}


def profile_fields(prefs: dict, card: dict) -> list[str]:
    """The fields you chose in your profile. If you haven't chosen any, Kev reads them from
    your profile (cached), taking every field it gives at least a 25% chance."""
    chosen = [f for f in prefs.get("fields") or [] if f in FIELDS]
    if chosen:
        return chosen
    key = repr(sorted(card.items()))
    if key not in _profile_fields_cache:
        a = _call_kev(card or {"note": "empty profile"}, {"field": PROFILE_FIELD_QUESTION}).get("answers", {})
        probs = a.get("field", {}).get("probabilities") or {}
        _profile_fields_cache[key] = [f for f, p in probs.items() if p >= 0.25] or [max(probs, key=probs.get)]
    return _profile_fields_cache[key]


def check_requirements(card: dict, must: list[str], nice: list[str]) -> tuple[list, list]:
    """Kev checks each requirement against your profile, one yes/no question each, all in one
    request (your profile is read once). Either/or requirements count if you have any option.
    Tested: 93% right per requirement; one 'meets all requirements?' question was 32%."""
    questions, groups = {}, []
    for kind, items in (("must", must), ("nice", nice)):
        for item in items:
            keys = []
            for option in split_alternatives(item):
                k = f"q{len(questions)}"
                questions[k] = {"type": "noul", "instructions": f"Does this candidate have: {option}?"}
                keys.append(k)
            groups.append((kind, item, keys))
    if not questions:
        return [], []
    a = _call_kev(card, questions).get("answers", {})
    out = {"must": [], "nice": []}
    for kind, item, keys in groups:
        out[kind].append((item, max(float(a.get(k, {}).get("noul", 0)) for k in keys)))
    return out["must"], out["nice"]


def _location_fits(job_location: str, places: str) -> float:
    a = _call_kev({"job_location": job_location or "not stated", "places_the_candidate_can_work": places},
                  {"q": {"type": "noul", "instructions": "Can the candidate work in this job's location?"}})
    return float(a.get("answers", {}).get("q", {}).get("noul", 1.0))


def _num(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Evaluate one job
# ---------------------------------------------------------------------------
def effective_prefs(prefs: dict, resume_facts: dict | None) -> dict:
    """Your saved profile, with blanks filled in from what Gemini read on your resume."""
    out = dict(prefs or {})
    rf = resume_facts or {}
    for key in ("degree", "graduation_year", "cgpa", "percentage", "years_experience", "current_role"):
        if out.get(key) in (None, "") and rf.get(key) not in (None, ""):
            out[key] = rf[key]
    if not out.get("career_stage") and rf.get("career_stage"):
        out["career_stage"] = rf["career_stage"]
    return out


def _quote(facts: dict | None, key: str) -> str:
    q = ((facts or {}).get("evidence") or {}).get(key)
    return f' ("{q}")' if q else ""


def read_requirements(description: str, facts: dict | None) -> dict:
    """The posting's numeric requirements: Gemini's reading, or the text rules without it."""
    if facts:
        return {"years": facts.get("min_years"), "batches": set(facts.get("batch_years") or []),
                "cgpa": facts.get("min_cgpa"), "cgpa_scale": facts.get("cgpa_scale"),
                "percentage": facts.get("min_percentage"), "level": facts.get("level")}
    cgpa = min_cgpa(description)
    return {"years": required_years(description), "batches": batch_years(description), "cgpa": cgpa,
            "cgpa_scale": (4.0 if cgpa <= 4 else 10.0) if cgpa else None, "percentage": None, "level": None}


def job_requirements(description: str, facts: dict | None) -> tuple[list[str], list[str], bool]:
    """(must-haves, nice-to-haves, from_gemini). Gemini's job card when there is one, else the
    requirement lines code finds in the posting. Degrees and licences are must-haves too."""
    if facts and (facts.get("must_have") or facts.get("nice_to_have") or "must_have" in facts):
        must = list(facts.get("must_have") or [])
        must += [f"a degree in {d}" if "degree" not in d.lower() else d for d in facts.get("required_degrees") or []]
        must += list(facts.get("mandatory_requirements") or [])
        must = [m for m in must if not _YEARS.search(m)]  # years are checked by code
        return must[:12], list(facts.get("nice_to_have") or [])[:6], True
    return requirement_lines(description), [], False


def level_mismatch(prefs: dict, level: str, need_years: float | None, facts: dict | None = None) -> str | None:
    """Why this job's level isn't for you, or None. Such jobs are hidden, not shown as Related:
    a Staff Engineer role is no near-miss for a fresher."""
    stage = "fresher" if prefs.get("career_stage") == "fresher" else "experienced"
    if stage == "fresher":
        if level == "internship" and not prefs.get("include_internships", True):
            return "An internship, and you've turned internships off"
        if level == "senior":
            return "A senior role; your profile is a fresher's"
        if level == "experienced":
            return (f"Needs {need_years:g}+ years of work experience{_quote(facts, 'experience')}; your profile is "
                    "a fresher's" if need_years else "A role for experienced people; your profile is a fresher's")
        return None
    mine = _num(prefs.get("years_experience"))
    if level == "internship":
        return "An internship; your profile is an experienced one"
    if level == "fresher" and mine is not None and mine >= OVERQUALIFIED_YEARS:
        return f"An entry-level role; you have {mine:g} years of experience"
    return None


def early_level(job: dict, kev_job: dict) -> str:
    """The level before Gemini has read the posting: Kev's reading corrected by the title only.
    The years rule waits for Gemini, because the text rule can misread "15 years in fintech"
    as a requirement, and a job hidden by mistake here never gets a second look."""
    return resolve_level(job["title"], kev_job.get("level") or "", kev_job.get("level_confidence") or 0, None)


def standard_checks(stage: str, level: str, prefs: dict, req: dict, facts: dict | None) -> tuple[list, list]:
    """The small, fixed rules code decides: years, batch, CGPA, marks (the level is checked
    before this, by level_mismatch). Returns (blocking reasons, informational reasons)."""
    blocks, notes = [], []
    need = req["years"]
    if stage == "fresher":
        grad = _num(prefs.get("graduation_year"))
        if req["batches"] and grad:
            if int(grad) in req["batches"]:
                notes.append(f"Open to your batch ({int(grad)})")
            else:
                blocks.append(f"Open to {', '.join(map(str, sorted(req['batches'])))} batch; you graduate in "
                              f"{int(grad)}{_quote(facts, 'batch')}")
        need_cgpa, my_cgpa = req["cgpa"], _num(prefs.get("cgpa"))
        my_scale = 4.0 if my_cgpa and my_cgpa <= 4 else 10.0
        if need_cgpa and my_cgpa and req["cgpa_scale"] == my_scale:  # only compare on the same scale
            if my_cgpa < need_cgpa:
                blocks.append(f"Asks for a CGPA of {need_cgpa:g}; yours is {my_cgpa:g}{_quote(facts, 'grades')}")
            else:
                notes.append(f"CGPA requirement ({need_cgpa:g}) met")
        need_pct, my_pct = req["percentage"], _num(prefs.get("percentage"))
        if need_pct:
            if my_pct is None:
                notes.append(f"Asks for {need_pct:g}% marks; add your percentage to your profile to check it")
            elif my_pct < need_pct:
                blocks.append(f"Asks for {need_pct:g}% marks; yours is {my_pct:g}%{_quote(facts, 'grades')}")
            else:
                notes.append(f"Marks requirement ({need_pct:g}%) met")
    else:
        mine = _num(prefs.get("years_experience"))
        if need is not None and mine is not None:
            if mine + 1 < need:
                blocks.append(f"Asks for {need:g}+ years of experience; you have {mine:g}{_quote(facts, 'experience')}")
            else:
                notes.append(f"Experience requirement ({need:g}+ years) looks fine")
    return blocks, notes


def evaluate(resume_text: str, company: dict, job: dict, prefs: dict, facts: dict | None = None,
             resume_facts: dict | None = None, kev_job: dict | None = None) -> dict:
    """Return verdict fields ready to be stored on the job row.

    Kev: is the job in your fields (from `kev_job`, see classify_job), and do you have each
    must-have. Code: level vs. your stage, years, batch, CGPA, marks. `facts` is Gemini's job
    card (None = requirement lines and numbers read by code from the posting)."""
    prefs = effective_prefs(prefs, resume_facts)
    stage = "fresher" if prefs.get("career_stage") == "fresher" else "experienced"
    description = job.get("description") or ""
    kev_job = kev_job or classify_job(job)
    card = profile_card(prefs, resume_text)
    mine = profile_fields(prefs, card)
    similar = similarity(kev_job, mine)
    field = main_field(kev_job)
    req = read_requirements(description, facts)
    level = resolve_level(job["title"], kev_job.get("level") or "", kev_job.get("level_confidence") or 0,
                          req["years"], req["level"])
    model = config.model_label() + (" + gemini" if facts else "")

    if similar < config.RELATED_THRESHOLD:
        return {"verdict": "not_related", "job_level": level, "related_p": similar, "eligible_p": None,
                "blocker_p": None, "fit_score": 4 * similar, "seniority": None, "model": model,
                "reasons": [f"Outside your fields: looks like {FIELD_LABELS.get(field, field)} "
                            f"({similar:.0%} match with yours)"]}

    wrong = level_mismatch(prefs, level, req["years"], facts)
    if wrong:
        return {"verdict": "wrong_level", "job_level": level, "related_p": similar, "eligible_p": None,
                "blocker_p": None, "fit_score": 0.0, "seniority": None, "model": model, "reasons": [wrong]}

    reasons = [f"In your fields ({similar:.0%}): looks like {FIELD_LABELS.get(field, field)}"]
    blocks, notes = standard_checks(stage, level, prefs, req, facts)

    must, nice, from_gemini = job_requirements(description, facts)
    must_p, nice_p = check_requirements(card, must, nice)
    missing = [(r, p) for r, p in must_p if p < config.ELIGIBLE_THRESHOLD]
    have = [(r, p) for r, p in must_p if p >= config.ELIGIBLE_THRESHOLD]
    if must_p:
        reasons.append(f"Has {len(have)} of {len(must_p)} must-have requirements"
                       + ("" if from_gemini else " (read from the posting without Gemini)"))
    else:
        # Nothing was checked, so it can't be called Eligible (and emailed): often it isn't
        # even a job, but a link a careers page reader picked up.
        blocks.append("Couldn't find this job's requirements, so it isn't marked Eligible. Open it to check.")
    reasons += [f"Missing: {r} ({p:.0%})" for r, p in missing]
    reasons += [f"Has: {r} ({p:.0%})" for r, p in have]
    reasons += [f"Nice to have, has it: {r}" for r, p in nice_p if p >= config.ELIGIBLE_THRESHOLD]

    places = (prefs.get("locations") or "").strip()
    if places:
        loc_p = _location_fits(job.get("location", ""), places)
        if loc_p < 0.5:
            blocks.append(f"Location doesn't match your preferences ({loc_p:.0%})")
        else:
            notes.append("Location fits your preferences")

    reasons += blocks + notes
    scores = [p for _, p in must_p]
    fit_parts = [p for _, p in must_p] + [p * 0.5 + 0.5 for _, p in nice_p]  # nice-to-haves weigh less
    return {
        "verdict": "related" if blocks or missing else "eligible",
        "job_level": level,
        "related_p": similar,
        "eligible_p": min(scores) if scores else None,
        "blocker_p": None,
        "fit_score": 4 * (sum(fit_parts) / len(fit_parts) if fit_parts else similar),
        "seniority": None,
        "reasons": reasons,
        "model": model,
    }
