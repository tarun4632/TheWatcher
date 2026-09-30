# TheWatcher

**Watches company careers pages, reads every new job, and emails you only the ones you're actually eligible for.**

Paste any careers link — a Greenhouse board, a Workday site, or a plain marketing page like `databricks.com/company/careers` — and TheWatcher works out where the jobs really are, checks it every 30 minutes, and scores each new posting against your resume with a small local decision model ([Kev](https://github.com/jaredpalmer/kev)) plus a few fixed rules in code. Eligible jobs land in your inbox with the reasons; everything else waits on a dashboard, sorted and explained.

Built for freshers and experienced people in India first (it ships with ~100 companies hiring in India), but nothing in it is India-only.

---

## Contents

- [Why it exists](#why-it-exists)
- [What it does](#what-it-does)
- [How a job is scored](#how-a-job-is-scored)
- [How it finds the jobs on any careers page](#how-it-finds-the-jobs-on-any-careers-page)
- [How it works, in detail](#how-it-works-in-detail)
- [Quick start](#quick-start)
- [Set up Kev](#set-up-kev)
- [Configuration](#configuration)
- [Using the dashboard](#using-the-dashboard)
- [Accounts and login](#accounts-and-login)
- [Email alerts](#email-alerts)
- [The starter list](#the-starter-list)
- [Webhooks](#webhooks)
- [Running it all the time](#running-it-all-the-time)
- [API](#api)
- [Project structure](#project-structure)
- [Tests](#tests)
- [Limits](#limits)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

---

## Why it exists

Job boards and LinkedIn alerts match on keywords. A fresher searching "software engineer" gets Staff and Principal roles, sales engineers and "8+ years" postings, and has to open each one to find out. TheWatcher reads the posting for you and answers the questions you'd ask:

- Is this job in my field at all?
- Is it for my level (intern / fresher / experienced / senior)?
- Is it open to my batch, my CGPA, my location?
- Which of its must-have requirements do I already meet, and which am I missing?

Only when all of that checks out does it email you.

## What it does

- **Any careers link.** Known job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Eightfold) are read through their JSON feeds. Anything else is worked out once — following "See open jobs" links, guessing and confirming the board, or opening the page in a headless browser to find the job list it loads — and saved as a recipe.
- **Only new jobs.** Postings older than `MAX_JOB_AGE_DAYS` (default 10) are ignored. After the first check, only job ids it hasn't seen are read and scored.
- **Scores each new job** with Kev (a local decision model) for the judgment calls and plain code for the fixed rules. Every verdict comes with reasons: *"Has 4 of 5 must-have requirements · Missing: Kafka (8%)"*.
- **Fresher and experienced profiles.** Batch, CGPA and percentage checks for freshers; years of experience for experienced people. Roles for the wrong level never show up.
- **Your profile, confirmed by you.** Upload a resume; Gemini (free tier, optional) reads it into a profile — degree, batch, CGPA, internships, skills, projects, coursework, fields — which you check and save.
- **Email alerts** for eligible new jobs, to one or several addresses per account.
- **Several accounts**, each with its own resume, profile, companies, jobs and alerts. Password reset by email.
- **A starter list** of ~100 companies hiring in India, so it's useful before you add anything.
- **Webhooks** to trigger a check from other tools, and a **dashboard** to browse everything.

## How a job is scored

Kev is a small *System One* decision model: it reads a text and answers typed questions (yes/no, multiple choice, a score) with probabilities, in one pass, without generating text. It is good at **one short text and one clear question**, and bad at comparing a whole resume with a whole posting. So each job goes through these steps, cheapest first:

```
new job
  │
  ├─ 1. Location (code) ─────────────── outside India, not remote-for-India → hidden
  │
  ├─ 2. Kev reads the job alone ─────── title + the requirements part of the posting
  │        field?  level?                 (company blurbs cut; they mislead it)
  │
  ├─ 3. In your fields? (Kev) ───────── no → Not a fit (no Gemini call spent)
  ├─ 4. Your level? (Kev + title) ───── senior role for a fresher → hidden as "Not your level"
  │
  ├─ 5. Gemini writes a job card ────── summary, must-haves, nice-to-haves, years, batch,
  │        (once per job, cached)         CGPA, degrees, licences, India eligibility
  │
  ├─ 6. Fixed rules (code) ──────────── years, batch, CGPA, marks; the level again with Gemini's years
  │
  ├─ 7. Each must-have vs your profile (Kev, one yes/no question each, one request)
  │        "Does this candidate have: Designing REST APIs?"  → 0.94
  │        "Java or Go" is split; having either counts
  │
  └─ 8. Verdict
         Eligible  = in your fields, your level, no rule broken, every must-have met → emailed
         Related   = in your field and level, but something's missing (named)
         Not a fit = outside your fields
```

Without a Gemini key (or once its free daily budget is used), step 5 is skipped: requirements are read from the posting's "Requirements" section by code, and the numbers by text rules. Scoring never stops.

**How well it works.** Measured on real postings from 29 companies' job boards (labelled by hand, borderline roles left out), through this code, with Kev-0.8B on a laptop CPU:

| Step | Right |
|---|---|
| The job's field (86 jobs) | 91% |
| "Is it in my fields?" | 97% |
| The job's level, with code corrections (92 jobs) | 92% |
| Each must-have requirement vs a sample fresher profile (54 requirements) | 96% |

For comparison, one request with the full resume and full posting asking "is the candidate related?" was right **44%** of the time and took ~19 s a job; the pipeline above takes ~3 s to read a job and 1–2 s to check its requirements. The reasoning behind each design choice — job-only questions, one question per requirement, the level checked before Gemini — is in [Why it's built this way](#why-its-built-this-way-the-experiments), with every measurement.

## How it finds the jobs on any careers page

Most careers pages are marketing pages; the jobs sit one or two clicks away, on a job board, or behind JavaScript. On a company's first check TheWatcher runs **discovery** and saves a **recipe**; every later check just follows the recipe.

1. **A known job board in the link** — Greenhouse, Lever, Ashby, SmartRecruiters, Workday.
2. **A job board inside the page**, or an **Eightfold** career site.
3. **Follow links** like "See open jobs" or "Search jobs", up to two clicks, checking each page again.
4. **Guess the board name** (`greenhouse.io/databricks`) and keep it only if something confirms it: the board's company name matches, or its job ids or titles appear on the company's own pages.
5. **Open the pages in a headless browser** (Playwright) and watch the JSON they load. If a response is a list of jobs (titles plus a location or date), save that request, work out how to ask for the next page, and replay it on every check — no browser needed after this.
6. **Plain job links** on the page, as a last resort.

Open **How the jobs were found** on a company's card to see each step. If a recipe stops working, it's worked out again on a later check (at most every 6 hours).

Tested on 30 September 2026:

| Careers link | Found | Time |
|---|---|---|
| `explore.jobs.netflix.net/careers` | Eightfold career site, 468 jobs | 2 s |
| `databricks.com/company/careers` | followed "See open jobs" → Greenhouse board `databricks` (confirmed by name), 883 jobs | 4 s |
| `ibm.com/in-en/careers` | headless browser → IBM's job search service, paged, ~1,800 jobs | 26 s |
| `careers.linkedin.com` | jobs live on linkedin.com → marked unsupported | 3 s |

## How it works, in detail

Everything below is what the code does today; file names point to where each part lives.

### The pieces

```
                         ┌──────────────────────────── TheWatcher (FastAPI, one process) ───────────────────────────┐
 careers sites ◄── HTTP ─┤ scrapers.py  readers per job board, replayed JSON feeds, HTML links                     │
 (Greenhouse, Workday,   │ discover.py  works out where a company's jobs are (once), saves a recipe                 │
  Eightfold, IBM, …)     │ monitor.py   the check loop: read → filter → score → email, 3 worker threads             │
                         │ matcher.py   the scoring pipeline: Kev questions + fixed rules → verdict + reasons      │
 Kev server  ◄── HTTP ───┤ extract.py   Gemini: job cards and resume reading, free daily budget                    │
 (localhost:8009)        │ notifier.py  email (SMTP)            auth.py  accounts, sessions, resets                  │
 Gemini API  ◄── HTTP ───┤ db.py        SQLite (data/thewatcher.db)   main.py  web server, API, scheduler, webhooks  │
 Gmail SMTP  ◄── SMTP ───┤ static/index.html  the dashboard (one page, no build step)                               │
                         └────────────────────────────────────────────────────────────────────────────────────────┘
```

A scheduler (APScheduler) runs a check of every active company of every account every `CHECK_INTERVAL_MINUTES`. **Check now**, webhooks, adding a company and saving your profile also queue checks. At most 3 companies are checked at once, and a company already being checked is never checked twice in parallel.

### One check, start to finish

For one company ([`monitor.py`](app/monitor.py)):

1. **Discovery, if needed.** A company added as a plain careers link, or whose recipe broke more than 6 hours ago, runs discovery first and saves the recipe (see below).
2. **Read the job list** by following the recipe: a job board's JSON feed, a replayed JSON request, or the links on a page. Readers return the same shape for every site: id, title, location, link, description (if the list has it), posted date (if the site gives one).
3. **Drop old postings.** Anything posted more than `MAX_JOB_AGE_DAYS` ago is ignored here and never saved. Jobs with no date are kept, because their age can't be known.
4. **Compare with what's stored.** Each job has a stable id (the board's id, or its link). New ids are saved; stored ids that disappeared are marked **closed** and hidden; closed ids that come back are **reopened**.
5. **Location.** With "only India or remote-for-India" on, new jobs elsewhere are saved as *outside your area* at once — no model ever sees them.
6. **Score the queue.** Up to `MAX_EVALS_PER_RUN` unscored jobs are scored (the pipeline in the next part). Descriptions are downloaded only now, only for jobs being scored.
7. **Email** every newly Eligible job that hasn't been emailed, to the owner's alert addresses.

### What happens to one job

```
           saved as "Not scored yet"
                    │
  1. location ──────┼──► outside your area                 (hidden)
                    │
  2. Kev reads the job: field + level      (answer kept on the job)
                    │
  3. in your fields? ─── no ──► Not a fit                  (no Gemini call)
                    │
  4. your level? ─────── no ──► Not your level             (hidden, no Gemini call)
                    │
  5. Gemini job card (once per job, kept)  ── says "not open to India" ──► outside your area
                    │
  6. fixed rules: years, batch, CGPA, marks, level with Gemini's years
                    │
  7. Kev checks each must-have against your profile card
                    │
  8. ┌── every must-have met, no rule broken ──► Eligible ──► emailed once
     └── otherwise ────────────────────────────► Related (what's missing is named)
```

If a step fails — Kev busy, a network error — the job becomes *Will retry* and is tried again on the next check, then after 30 minutes, 2 hours and 12 hours. After `MAX_EVAL_ATTEMPTS` failures it's *Scoring failed* until your profile changes. If Kev can't be reached at all, the whole check pauses and nothing counts as an attempt.

Saving your profile or job preferences marks every open job *Not scored yet* again. Kev's reading of the job (step 2) and Gemini's job card (step 5) don't depend on you, so they are **not** redone — only steps 3–8 run again.

### Verdicts

| Verdict | Shown as | Meaning | Where you see it | Emailed |
|---|---|---|---|---|
| `eligible` | **Eligible** | In your fields, your level, no rule broken, every must-have met | Eligible tab, All | yes, once |
| `related` | **Related** | In your field and level, but a must-have or a rule (batch, CGPA, years…) is missing — named in the reasons | Related tab, All | no |
| `not_related` | **Not a fit** | Kev reads the job as outside the fields in your profile | All | no |
| `wrong_level` | **Not your level** | e.g. a senior or "3+ years" role for a fresher, an internship for someone experienced | hidden | no |
| `out_of_area` | – | Not in India and not remote-for-India (by the listed location, or by Gemini) | hidden | no |
| `pending` | **Not scored yet** | Waiting in the queue | Not scored yet tab | – |
| `error` / `failed` | **Will retry** / **Scoring failed** | Scoring hit an error (see above) | All | no |

### Reading a company's card

Take IBM after its first check: *"657 open, 0 eligible (hidden: 1074 outside your area)"*.

- IBM's search returned **1,731** jobs (it has no dates, so all count as current).
- **1,074** are outside India and not remote-for-India: saved as *outside your area*, hidden, never scored.
- **657 open** are in India (or give no location). "Open" means *still listed and in your area* — it includes jobs **not scored yet**.
- **0 eligible** right after the first check is normal: scoring works through the queue `MAX_EVALS_PER_RUN` at a time. Most jobs stop at step 3 or 4 in a few seconds; the rest get the full check. Watch the **Not scored yet** tab count down.
- Once scored, *Not your level* jobs join the hidden count on the card.

### What Kev is asked

Kev only ever sees one short text, with one clear question, at a time ([`matcher.py`](app/matcher.py)). All questions about the same text go in one request, so the text is read once.

| When | Input (the "state") | Question | Type |
|---|---|---|---|
| Step 2, per job | job title · the requirements part of the posting (≤ 800 characters, found by headings like "Requirements", "What you'll need"; company blurbs cut) · the years required, as read by code | *Which field is this job in?* — six options, each described (software · data & ML · product & design · sales & marketing · finance & legal · other) | choice |
| Step 2, same request | same | *What level is this job?* — internship · fresher (under 2 years) · experienced (2–6 years) · senior (senior/staff/principal/lead/manager, or 7+ years) | choice |
| Step 3 | – | Your fields come from your profile; "in your fields" = Kev's probability summed over them. If you ticked none, Kev reads your fields from your profile card once (*Which field is this person's education and work in?*) | – |
| Step 7, per job | your profile card | *Does this candidate have: {requirement}?* — one per must-have and nice-to-have. "Java, Ruby or Go" becomes three questions; having any one counts | yes/no each |
| Step 7, if you set places to work | job location · your places | *Can the candidate work in this job's location?* | yes/no |

Kev's answers are probabilities. Code turns them into decisions with `RELATED_THRESHOLD` (in your fields) and `ELIGIBLE_THRESHOLD` (each must-have), and corrects the level with hard signals: "Intern" in the title is always an internship; "Senior", "Lead", "Staff", "Principal", "Manager" is never entry level; "2+ years required" is never a fresher role.

### The job card (Gemini)

Once per job, only for jobs that passed steps 3 and 4, Gemini reads the full posting and returns ([`extract.py`](app/extract.py)):

| Field | Example |
|---|---|
| `summary` | "Builds backend services for payment processing." |
| `must_have` | `["Python or Go programming", "Designing REST APIs"]` — one short item each, alternatives joined by "or"; years, degrees, grades, locations and personality traits left out |
| `nice_to_have` | `["Kubernetes"]` |
| `min_years_experience` | `2` (not "15 years in fintech", which describes the company) |
| `batch_years` | `[2025, 2026]` from "2025/26 pass-outs" |
| `min_cgpa`, `cgpa_scale`, `min_percentage` | `7.5`, `10`, `60` |
| `required_degrees`, `mandatory_requirements` | `["Computer Science"]`, `["Valid driving licence"]` — both become must-haves |
| `level` | internship · fresher · experienced · senior |
| `india_eligible` | `false` for "Remote, US only" |
| `evidence` | short quotes behind the years, batch, grades and location answers, shown next to any block |

Gemini only **reads**. The comparisons are made by code (numbers) and Kev (fields, requirements). Without Gemini, must-haves are the lines under the posting's requirements heading and the numbers come from text rules.

### Your profile card

What Kev sees about you in step 7, built from your **confirmed** profile:

```
education:   B.Tech Computer Science, graduating 2026, CGPA 8.3/10
experience:  Software Engineering Intern at PayNest (internship, 2025-05 to 2025-07)
projects:    Expense tracker (React, Node.js, Express, MongoDB); Job scraper (Python, BeautifulSoup, PostgreSQL)
coursework:  Data Structures, Algorithms, DBMS, Operating Systems, Machine Learning
skills:      Python, Java, C++, JavaScript, SQL, React, Node.js, Flask, Git, Docker
```

Kev only knows what's here. A skill that isn't on the card is a missing requirement — so the projects and coursework boxes matter.

### What is kept, and reused

| Kept | Where | Reused when |
|---|---|---|
| Kev's reading of a job (field, level) | on the job | re-scoring after a profile change |
| Gemini's job card | on the job | re-scoring; never asked twice for one job |
| The job description | on the job | fetched once, only for jobs being scored |
| A company's recipe | on the company | every check; worked out again only if it breaks |
| Your fields, if Kev read them | in memory | the rest of that check |
| The Gemini daily count | settings (shared by all accounts) | resets at midnight Pacific time |

### Discovery recipes

Discovery ([`discover.py`](app/discover.py)) saves one of these on the company:

| Recipe | Example | Read every check by |
|---|---|---|
| Job board | `greenhouse` · `databricks` | the board's public JSON feed |
| Eightfold | `eightfold` · `explore.jobs.netflix.net\|netflix.com` | `/api/apply/v2/jobs`, newest first, 10 a request, stopping at the first page past the age window |
| JSON feed | IBM, below | replaying the captured request, page by page (up to 25 pages) |
| Page links | `generic` + the page that lists the jobs | reading job-like links, skipping navigation ("Learn more", "Search", blogs…) |
| Unsupported | LinkedIn-only companies | nothing; the reason is shown on the card |

A JSON-feed recipe, as saved for IBM (shortened):

```json
{
  "feed": {
    "method": "POST",
    "url": "https://www-api.ibm.com/search/api/v2",
    "body": {"appId": "careers", "scopes": ["careers2"], "size": 30, "_source": ["title", "url", "..."]},
    "list_path": ["hits", "hits"],
    "fields": {"title": "_source.title", "url": "_source.url", "id": "_id",
               "location": "_source.field_keyword_19", "posted": null, "description": "_source.description"},
    "page": {"where": "body", "param": "from", "start": 0, "step": 30, "size_param": "size", "size": 30}
  },
  "steps": ["Following 'https://www.ibm.com/in-en/careers/search'", "Rendered … in a headless browser", "..."],
  "discovered_at": "2026-09-30T10:12:44+00:00"
}
```

How the fields were found: in the JSON the page loaded, the largest list of objects that have a title, a link or id, **and** a location or date (a menu has titles and links, but no location). The location key `field_keyword_19` was recognised by its values ("Bengaluru, IN", "Zurich, CH"). Paging was found by trying `from` = 30 and checking that page 2 really held different jobs.

A board guessed from the company's name is kept only if something confirms it: the board's name matches the company, or its job ids or titles appear on the company's own pages. A board named "acme" that belongs to Acme Plumbing is rejected for Acme Corp.

### Accounts and data

- Companies belong to an account; jobs belong to a company; resumes, profiles, preferences and alert addresses are stored per account. Every database call that reads one account's data needs to know whose request it is, and **fails** instead of guessing — a missing check is an error, never another account's data.
- The background check runs each company as its owner: that owner's resume, profile and alert addresses.
- Tables: `users`, `sessions` (hashed tokens), `password_resets` (hashed, 60 minutes), `companies` (with `recipe`), `jobs` (with `kev_job`, `facts`, verdict and reasons), `resumes`, `settings`, `events` (Activity).

### Being polite to careers sites

Every outgoing request goes through one policy ([`ratelimit.py`](app/ratelimit.py)): at most `JOB_BOARD_RPS` requests a second to any one site; `KEV_RPM`, `GEMINI_RPM` and `SMTP_PER_MINUTE` for the services; retries with exponential backoff and jitter on network errors, 429 and 5xx, honouring `Retry-After`; a service still answering 429 after that is paused for `PROVIDER_COOLDOWN_SECONDS`. The headless browser runs only while a recipe is being worked out, never on routine checks.

### Why it's built this way: the experiments

The design came from measuring, not guessing. All tests used real postings from live job boards, labels written by hand with borderline roles left out, and Kev-0.8B on a laptop CPU. The sets are small (19–92 items), so treat the numbers as directions, not guarantees.

**Asking "is this job related to me?" (27 jobs)**

| How Kev was asked | Right | Ranking (1 = perfect) | Time a job |
|---|---|---|---|
| Full resume + full posting, "is the candidate related?" (the first design) | 44% | 0.71 | 19.3 s |
| Short profile + first 1,500 characters of the posting | 44% | 0.72 | 5.6 s |
| The job's field only, from the posting | – | 0.98 | 5.0 s |
| The job's field only, from the **title** | 100% | 1.00 | 1.9 s |

→ Kev can't compare two documents; it classifies one short text well. Shortening alone didn't help.

**The job's field (86 jobs)**

| Variant | Field right | "CS job?" right | Time |
|---|---|---|---|
| Title, options described | 86% | 95% | 1.9 s |
| Title, bare option names | 59% | 93% | 1.1 s |
| Title as plain text instead of JSON | 87% | 95% | 1.9 s |
| **Title + requirements section (800 chars)** | **91%** | **98%** | 3.5 s |
| Title + first 800 chars (mostly company blurb) | 76% | 87% | 3.6 s |
| Six yes/no questions instead of one choice | 78% | 92% | 3.1 s |
| Averaged over 4 option orders | 85% | 95% | 6.6 s |

→ Describe every option; ask one choice question; show Kev the *requirements*, not the start of the posting.

**The job's level (92 jobs)**

| Variant | Right |
|---|---|
| Code alone (title words + years) | 64% |
| Kev, title only | 82% |
| Kev, title + requirements section | 91% |
| **Kev, title + years read by code, then code's corrections** | **93%** |

→ Give Kev the facts code already knows.

**"Similar to my background?" from short cards (19 jobs)**

| Variant | Right | Ranking |
|---|---|---|
| Job card + full profile card, "same field?" | 58% | 0.83 |
| Same, as a statement | 58% | 0.73 |
| Same, as a choice: same / adjacent / different | 74% | 0.92 |
| Job card + the candidate's field in one line | 79% (89% at a tuned cutoff) | 0.98 |

→ Represent yourself by your fields, not your whole profile; the field-per-job approach above was better still (98%).

**Does the profile meet each requirement? (54 requirements)**

| Variant | Right | Ranking |
|---|---|---|
| **Profile only, one yes/no question per requirement** | 91% | 0.96 |
| Same, a job's questions in one request | 91% | 0.96 (0.2 s each) |
| + splitting "X or Y" and adding coursework to the profile | **93%** | **0.98** |
| Profile + job title | 91% | 0.94 (5× slower) |
| Worded as a statement ("The candidate has …") | 83% | 0.91 |
| One question: "meets all requirements?" (19 jobs) | 32% | – |
| One question per requirement, weakest answer decides (19 jobs) | 84% | – |

→ One requirement per question, against your profile, worded as a question.

**Through the finished code** (the numbers in [How a job is scored](#how-a-job-is-scored)): field 91%, in-my-fields 97%, level 92%, requirements 96%.

**Model size.** Kev-0.8B was chosen because it runs on a laptop CPU (~2–4 s a job). Kev's authors report Kev-4B at ~0.82 on unseen tasks against ~0.65 for 0.8B; it needs a ~9 GB GPU. In these tests, how Kev was asked mattered far more than its size.

## Quick start

You need **Python 3.10+**, **git**, and about **4 GB of free RAM** for Kev-0.8B. No GPU needed.

```bash
git clone <this repo> thewatcher
cd thewatcher
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium   # headless browser for JavaScript careers pages (~150 MB)
cp .env.example .env                 # Windows: copy .env.example .env   — then fill it in
```

Start Kev ([next section](#set-up-kev)) in one terminal, then TheWatcher in another:

```bash
uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>, **create an account**, upload your resume, check the profile it reads (tick **Fields you work in**), save, and add a company.

## Set up Kev

[Kev](https://github.com/jaredpalmer/kev) (Apache 2.0) runs as its own server; TheWatcher sends it one request per batch of questions. Install it once, **outside** any synced folder (OneDrive, Dropbox) so its large environment isn't uploaded:

```bash
git clone https://github.com/jaredpalmer/kev.git
cd kev
uv python pin 3.12        # the repo's .python-version says 3.13; pin so `uv run` matches the install
uv sync --extra serve
```

Start it every time before TheWatcher, and leave that terminal open:

```bash
cd kev
uv run --extra serve python -m kev.serve --run jaredpalmer/kev-0.8b --port 8009
```

The first start downloads the model (~1.6 GB). It's ready when it prints `serving jaredpalmer/kev-0.8b ... 127.0.0.1:8009`. Check it from TheWatcher's folder:

```bash
python tools/try_kev.py                  # a software job should come out Eligible, a nurse job Not a fit
python tools/try_kev.py my_resume.pdf    # try your own resume
```

**Which size?** Kev-0.8B runs on an ordinary laptop CPU (~2–4 s per job). Kev-4B and larger need a GPU with ~9 GB+ of memory or a 32 GB Mac; run one on another machine or a cloud GPU (Kev's repo supports Modal) and point `KEV_BASE_URL` / `KEV_API_KEY` at it. If TheWatcher runs in Docker and Kev on the same computer, use `KEV_BASE_URL=http://host.docker.internal:8009`.

If Kev isn't running, jobs stay "Not scored yet" and Activity says *"Couldn't reach Kev … Is the Kev server running?"*. Nothing is used up; scoring resumes on the first check after Kev is back.

## Configuration

Everything is set in `.env` (see [`.env.example`](.env.example)). Restart after changes.

| Setting | Default | What it does |
|---|---|---|
| `KEV_BASE_URL` | `http://127.0.0.1:8009` | Where the Kev server is |
| `KEV_MODEL` | `kev-latest` | Model name the Kev server answers to |
| `KEV_API_KEY` | – | Only if the Kev server requires one |
| `KEV_TIMEOUT_SECONDS` | `120` | Longest wait for one Kev request |
| `GEMINI_API_KEY` | – | Free-tier key; enables job cards and resume reading |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model |
| `GEMINI_RPM` / `GEMINI_DAILY_LIMIT` | `10` / `500` | Keep under your free-tier limits (aistudio.google.com/rate-limit) |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | Mail server |
| `SMTP_USER` / `SMTP_PASSWORD` | – | The account alerts are sent **from** (Gmail: an App Password) |
| `ALERT_EMAIL_TO` | `SMTP_USER` | Where the first account's alerts go until addresses are saved on the dashboard |
| `LOAD_DEFAULT_COMPANIES` | `1` | Give each new account the starter list |
| `CHECK_INTERVAL_MINUTES` | `30` | How often every company is checked |
| `MAX_JOB_AGE_DAYS` | `10` | Ignore postings older than this (undated jobs are kept) |
| `MAX_EVALS_PER_RUN` | `150` | Jobs scored per company check; the rest wait |
| `RELATED_THRESHOLD` | `0.5` | How sure Kev must be that a job is in your fields |
| `ELIGIBLE_THRESHOLD` | `0.5` | How sure Kev must be that you meet each must-have |
| `JOB_BOARD_RPS` | `2` | Requests per second to any one careers site |
| `KEV_RPM` / `SMTP_PER_MINUTE` | `60` / `20` | Caps for Kev and email |
| `RETRY_MAX_ATTEMPTS` / `RETRY_MAX_WAIT_SECONDS` | `4` / `90` | Per-request retries with backoff |
| `PROVIDER_COOLDOWN_SECONDS` | `300` | Pause a service that keeps answering 429 |
| `MAX_EVAL_ATTEMPTS` / `MAX_EMAIL_ATTEMPTS` | `4` / `4` | Then a job is marked "Scoring failed" / an email is given up |
| `DASHBOARD_URL` | `http://localhost:8000` | Link used in emails; `https://` also marks the login cookie Secure |
| `ALLOW_SIGNUP` | `1` | `0` = no new accounts (the first can always be made) |
| `WEBHOOK_SECRET` | – | Required on webhooks when set |
| `DB_PATH` | `data/thewatcher.db` | SQLite database file |

**Tuning.** Jobs outside your field getting through? Raise `RELATED_THRESHOLD` to `0.6` or untick fields in your profile. Good jobs marked Related because a requirement looked missing? Add that skill, project or course to your profile — Kev only knows what your profile says — or lower `ELIGIBLE_THRESHOLD` to `0.4`.

## Using the dashboard

- **Your resume and profile.** Drop a PDF/DOCX. Gemini reads it once (two-column layouts and scanned PDFs work) and a form opens asking *"Is this right?"*: career stage, degree, batch, CGPA or percentage, work history with internships, skills, projects, coursework, **fields you work in**, and where you're based. Only the confirmed profile is used, and saving it re-scores every open job. Without a Gemini key the same form opens empty for you to fill in.
- **Job preferences.** Internships on/off, "only jobs in India or remote-for-India", places you can work.
- **Companies.** Add any careers link; watch its counts; **Check now**; see **How the jobs were found**.
- **Starter list.** Pause or resume companies one by one or a whole group; search by name.
- **Jobs.** Split into *Fresher & internship roles* and *Experienced roles*, with tabs for **Eligible**, **Related**, **All open jobs** and **Not scored yet**. Click a job for its reasons and the probabilities behind them. Jobs outside India, and roles not for your level, are hidden.
- **Activity.** Every check, discovery step, scoring error and email.

## Accounts and login

The sign-in page has **Log in** and **Create account** tabs. Every account has its own resume, profile, preferences, companies, jobs, Activity and alert addresses; nobody sees anyone else's.

- Passwords are stored as scrypt hashes; sessions are 30-day HttpOnly cookies (Secure over https).
- Five wrong passwords (or five sign-ups) from one address lock it out for 15 minutes.
- **Forgot password?** emails a one-time link (valid 60 minutes) to the account's email — never to alert addresses, which may belong to someone else. The first account falls back to `ALERT_EMAIL_TO`.
- **Account** (header) sets the account email and changes the password; changing it signs out every other browser.
- Set `ALLOW_SIGNUP=0` once everyone has an account.
- Server-side reset: `python -m app.auth list`, then `python -m app.auth set-password USERNAME`.
- **Shared between accounts:** the Kev server, the Gemini key and its daily budget, and the Gmail account that sends alerts.

## Email alerts

Every alert is sent **from** `SMTP_USER` and goes only to the owner's alert addresses (set at sign-up or under **Email alerts**, up to 10). Receiving needs no password. For Gmail, turn on 2-Step Verification and create an **App Password** (Google Account → Security → App passwords); put its 16 characters in `SMTP_PASSWORD`. **Send a test email** confirms it works. Failed emails are retried on later checks (up to `MAX_EMAIL_ATTEMPTS`); each job is emailed at most once. A normal Gmail account sends ~500 emails a day.

## The starter list

New accounts get **97 companies that were hiring in India** (snapshot of 25 September 2026), all on Greenhouse, Lever, Ashby or Workday:

| Group | Companies | Examples |
|---|---|---|
| India-based companies and startups | 33 | Razorpay, PhonePe, Paytm, Meesho, CRED, Zeta, InMobi, Atlan |
| Global tech with India offices | 38 | Stripe, MongoDB, Databricks, Okta, Zscaler, Snowflake, OpenAI, UiPath |
| Large employers with fresher roles | 26 | Johnson Controls, Fiserv, GE HealthCare, BlackRock, Wells Fargo, Cisco, Visa |

Together they had ~8,500 India openings, 965 entry-level, on the snapshot date. Turn the list off with `LOAD_DEFAULT_COMPANIES=0`; bring deleted ones back with **Add the starter list**. Hiring changes, so rebuild it every month or two (downloads ~75 MB):

```bash
python tools/build_starter_list.py --keep-current   # same companies, fresh numbers
python tools/build_starter_list.py --per-board 25   # re-pick the companies with the most India openings
```

## Webhooks

Careers sites don't send webhooks, so TheWatcher polls — but other tools can trigger a check at once:

| Request | Checks |
|---|---|
| `POST /webhook/check/{company_id}` | one company (each card shows its link) |
| `POST /webhook/check` | every company of every account |
| `POST /webhook/check` with `{"url": "..."}` or `{"company": "Acme"}` | the matching companies |

With `WEBHOOK_SECRET` set, add `?secret=…` or an `X-Webhook-Secret` header. A good pairing is [changedetection.io](https://github.com/dgtlmoon/changedetection.io) watching a careers page with the notification `json://your-server:8000/webhook/check/3?secret=...`.

## Running it all the time

TheWatcher only watches while it runs. For always-on monitoring use a small server (any VPS, a Raspberry Pi, Oracle Cloud or Google Cloud free tiers), remembering Kev needs ~4 GB RAM on CPU.

**Docker** (TheWatcher only; run Kev separately):

```bash
docker build -t thewatcher .
docker run -d --name thewatcher -p 8000:8000 --env-file .env \
  -e KEV_BASE_URL=http://host.docker.internal:8009 \
  -v $(pwd)/data:/app/data thewatcher
```

**If it's reachable from the internet:** create your account first (until then, whoever opens the page first can claim it), serve it over HTTPS (Caddy, a Cloudflare tunnel) with `DASHBOARD_URL=https://…`, set `WEBHOOK_SECRET`, and consider `ALLOW_SIGNUP=0` — or keep it on a private network such as Tailscale.

## API

All `/api/*` routes need a signed-in session cookie except `/api/auth/*`. Webhooks use `WEBHOOK_SECRET` instead.

| Method & path | Purpose |
|---|---|
| `GET /api/auth/status` · `POST /api/auth/signup` · `POST /api/auth/login` · `POST /api/auth/logout` | Accounts |
| `POST /api/auth/forgot` · `POST /api/auth/reset` | Password reset by email |
| `POST /api/account/email` · `POST /api/account/password` | Account email, password change |
| `GET /api/state` | Everything the dashboard shows: companies, counts, settings, Activity |
| `GET /api/jobs?verdict=&level=&company_id=` | Jobs with reasons |
| `POST /api/companies` · `DELETE /api/companies/{id}` | Add / remove a company |
| `POST /api/companies/{id}/check` · `/active` · `/score-current` | Check now, pause/resume, score listed jobs |
| `POST /api/categories/active` · `POST /api/defaults/restore` · `POST /api/check-all` | Starter-list groups, restore, check everything |
| `POST /api/resume` · `GET/POST /api/profile` · `POST /api/preferences` | Resume, profile, job preferences |
| `POST /api/alert-emails` · `POST /api/test-email` | Alert addresses, test email |
| `POST /webhook/check` · `POST /webhook/check/{id}` | Webhooks |

## Project structure

```
app/
  main.py        Web server, dashboard API, accounts, webhooks, scheduler
  monitor.py     The watch loop: discover, read, filter, score, email — cheapest step first
  discover.py    Works out where a company's jobs are from any careers link; saves the recipe
  scrapers.py    Readers: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Eightfold,
                 replayed JSON feeds, plain HTML links
  matcher.py     The scoring pipeline: Kev questions, fixed rules, verdicts and reasons
  extract.py     Gemini: job cards from postings, profiles from resumes, the daily free budget
  profile.py     Resume draft → reviewed → confirmed profile
  auth.py        Accounts: sign-up, scrypt passwords, sessions, lockout, password reset
  notifier.py    Email alerts and reset emails
  ratelimit.py   Per-host and per-service rate limits, retries, cooldowns
  resume.py      PDF / DOCX / TXT text extraction
  area.py        "India or remote-for-India" location check
  db.py          SQLite storage, one user's data at a time
  config.py      Settings from .env
  default_companies.json   The starter list
static/index.html            The dashboard (a single page, no build step)
tools/try_kev.py             Score two sample jobs with Kev — checks your setup
tools/build_starter_list.py  Rebuilds the starter list from fresh data
tests/                       90 tests; no network, no Kev, no Gemini needed
```

## Tests

```bash
python -m pytest
```

The tests use a temporary database, fake careers sites (httpx `MockTransport`) and a fake Kev, so they need no network, no Kev server and no API keys. They cover the scoring pipeline, discovery, every reader, accounts and resets, multi-user isolation, rate limits and retries.

## Limits

- **LinkedIn and Naukri** don't allow automated reading, so companies whose jobs live only there are marked unsupported (with the reason on their card). Use those sites' own alerts.
- **Bot protection and logins.** Sites that block automated browsers or need you to log in can't be read.
- **Some sites have no posting dates** (IBM's search, for one): on the first check every current job counts as new; after that only new ones do.
- **Kev-0.8B is small.** It's right ~91–97% of the time on the questions it's asked here, not 100%. Borderline roles (a product manager for an API platform, a trader who codes) can be judged either way. Treat Eligible as a strong hint and read the posting.
- **Gemini's free tier** may use your requests, including your resume, to improve Google's products.
- **Kev only knows your profile.** A skill that isn't in your profile is a missing requirement.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Jobs stay "Not scored yet"; Activity says *Couldn't reach Kev* | Start the Kev server; check `KEV_BASE_URL` |
| `uv run` says *Access is denied* | Another Kev server is running from the same environment; close it (or free port 8009) |
| *Username and Password not accepted* on test email | Use a Gmail App Password with 2-Step Verification on |
| A company shows "Couldn't find this company's job list" | Paste the page that lists the jobs (or its job board link) instead of the homepage |
| *database is locked* | Keep `data/` outside OneDrive/Dropbox (`DB_PATH=C:/thewatcher/thewatcher.db`) |
| Everything is slow | Lower `MAX_EVALS_PER_RUN`; Kev on CPU takes a few seconds per job |

## Credits

- [Kev](https://github.com/jaredpalmer/kev) by Jared Palmer (Apache 2.0) — the decision model.
- Gemini (Google) — reads postings and resumes on the free tier.
- Starter-list company data derived from [job-board-aggregator](https://github.com/Feashliaa/job-board-aggregator) by Riley Dorrington, licensed **CC BY-NC 4.0**: fine for personal use; ask the author before using it commercially.
