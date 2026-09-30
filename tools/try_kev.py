"""Score two sample jobs with Kev, without TheWatcher's server, database or email.

Start the Kev server first (see the README), then:

    python tools/try_kev.py                 # uses a sample fresher resume
    python tools/try_kev.py my_resume.pdf   # uses your resume (PDF, DOCX or TXT)

A good result: the matching job scores clearly higher than the unrelated one.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, matcher, resume  # noqa: E402

config.GEMINI_API_KEY = ""  # only Kev is asked

SAMPLE_RESUME = """Aarav Mehta. B.Tech in Computer Science, graduating 2026, CGPA 8.3/10. Based in Pune.
Skills: Python, Java, SQL, React, Node.js, Git, Docker, data structures and algorithms.
Internship: Software Engineering Intern at a fintech startup (May-Jul 2025), built REST APIs in Python/Flask
and wrote SQL reports. Projects: a React + Node.js expense tracker; a Python web scraper with PostgreSQL."""

JOBS = [
    ("should match", {"title": "Software Engineer I (2025/2026 graduates)", "location": "Bengaluru, India",
      "description": "Build backend services in Python or Java and web features in React. Freshers welcome. "
                     "Requirements: B.E./B.Tech in CS or related, strong data structures and algorithms, SQL, Git. "
                     "Internship experience is a plus."}),
    ("should NOT match", {"title": "Senior Staff Nurse, ICU", "location": "Mumbai, India",
      "description": "Registered nurse for the intensive care unit. Requirements: B.Sc Nursing, active nursing "
                     "council registration, 6+ years of ICU experience, BLS/ACLS certification."}),
]


def main():
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        text, _ = resume.read_resume(p.name, p.read_bytes())
        prefs = {"career_stage": "experienced"}  # your fields are read by Kev from the resume
        print(f"Resume: {p.name} ({len(text)} characters), judged as an experienced profile.")
        print("(Your confirmed dashboard profile isn't used here; tweak `prefs` in this file if needed.)\n")
    else:
        text = SAMPLE_RESUME
        prefs = {"career_stage": "fresher", "degree": "B.Tech Computer Science", "graduation_year": 2026,
                 "include_internships": True, "fields": ["software", "data_ml"],
                 "skills": ["Python", "Java", "SQL", "React", "Node.js", "Git", "Docker"],
                 "projects": ["React + Node.js expense tracker", "Python web scraper with PostgreSQL"],
                 "work_history": [{"title": "Software Engineering Intern", "company": "a fintech startup",
                                   "start": "2025-05", "end": "2025-07", "internship": True}]}
        print("Resume: built-in sample (fresher, B.Tech CS 2026)\n")

    print(f"Asking Kev at {config.KEV_BASE_URL} (model {config.KEV_MODEL})...\n")
    for label, job in JOBS:
        t = time.perf_counter()
        try:
            r = matcher.evaluate(text, {"name": "Sample Co"}, job, prefs)
        except matcher.KevError as e:
            print(f"Kev didn't answer: {e}")
            return 1
        secs = time.perf_counter() - t
        print(f"[{label}] {job['title']}")
        print(f"  verdict: {r['verdict']}   fit {r['fit_score']:.1f}/4   in your fields {r['related_p']:.0%}   "
              f"level {r['job_level']}   ({secs:.1f} s)")
        for reason in r["reasons"]:
            print(f"   - {reason}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
