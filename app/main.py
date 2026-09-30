"""TheWatcher: web server, dashboard API, incoming webhooks and the scheduler."""
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import auth, config, db, extract, matcher, monitor, notifier, profile, resume, scrapers

STATIC = Path(__file__).resolve().parent.parent / "static"
scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    for u in db.list_users():
        with db.as_user(u["id"]):
            profile.get()  # carries a profile typed into an older version over, once
            if config.LOAD_DEFAULT_COMPANIES and monitor.seed_default_companies():
                monitor.submit_check_all()  # first check scores jobs posted within MAX_JOB_AGE_DAYS
    scheduler.add_job(monitor.check_all, "interval", minutes=config.CHECK_INTERVAL_MINUTES,
                      id="check_all", max_instances=1, coalesce=True)
    scheduler.start()
    db.log(f"TheWatcher started, checking every {config.CHECK_INTERVAL_MINUTES} minutes", user_id=None)
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="TheWatcher", lifespan=lifespan)


# --- accounts --------------------------------------------------------------
# Every /api route needs a signed-in session, except the routes under /api/auth.
# The request then runs as that user, so every database call sees only their
# data. Webhooks are for other tools, so they keep using WEBHOOK_SECRET.
@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    user_id = auth.session_user(request.cookies.get(auth.COOKIE))
    if user_id is None:
        return JSONResponse({"detail": "Please log in."}, status_code=401)
    request.state.user_id = user_id
    with db.as_user(user_id):
        return await call_next(request)


class CredentialsIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class SignupIn(CredentialsIn):
    email: str = Field("", max_length=254)  # optional: where this account's alerts go


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _signed_in(response: Response, user_id: int) -> dict:
    response.set_cookie(auth.COOKIE, auth.start_session(user_id), **auth.cookie_options())
    return {"ok": True, "username": db.get_user(user_id)["username"]}


@app.get("/api/auth/status")
def auth_status(request: Request):
    user_id = auth.session_user(request.cookies.get(auth.COOKIE))
    user = db.get_user(user_id) if user_id else None
    return {"logged_in": user is not None, "username": user["username"] if user else None,
            "signup_open": auth.signup_open(), "has_users": db.first_user_id() is not None}


@app.post("/api/auth/signup")
def auth_signup(body: SignupIn, request: Request, response: Response):
    """Create an account with its own, empty dashboard and sign in to it."""
    if not auth.signup_open():
        raise HTTPException(403, "New accounts are turned off here. Ask the owner, or log in.")
    guard = _client_address(request) + ":signup"
    wait = auth.locked_out(guard)
    if wait:
        raise HTTPException(429, f"Too many new accounts from here. Try again in {wait // 60 + 1} min.")
    try:
        emails = notifier.clean_recipients([body.email]) if body.email.strip() else []
        user_id = auth.create_account(body.username, body.password)
    except ValueError as e:  # AuthError is a ValueError too
        raise HTTPException(409 if "taken" in str(e) else 422, str(e)) from e
    auth.record_failure(guard)  # counts towards the sign-up limit, not the login one
    if emails:
        db.set_user_email(user_id, emails[0])  # also where a password-reset link goes
    with db.as_user(user_id):
        if emails:
            db.set_setting("alert_emails", emails)
        if config.LOAD_DEFAULT_COMPANIES and monitor.seed_default_companies():
            monitor.submit_check_all()
    return _signed_in(response, user_id)


@app.post("/api/auth/login")
def auth_login(body: CredentialsIn, request: Request, response: Response):
    address = _client_address(request)
    wait = auth.locked_out(address)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {wait // 60 + 1} min, "
                                 "or use Forgot password.")
    user_id = auth.check_login(body.username, body.password)
    if user_id is None:
        auth.record_failure(address)
        target = db.get_user_by_name(body.username)
        if target:  # only the account's owner sees this in their Activity
            db.log(f"Failed login from {address}", "warn", user_id=target["id"])
        raise HTTPException(401, "Wrong username or password.")
    auth.clear_failures(address)
    return _signed_in(response, user_id)


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


class ForgotIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


FORGOT_REPLY = ("If that account has an email address, a reset link is on its way. "
                "It works once, for {minutes} minutes. Check your spam folder too.")


@app.post("/api/auth/forgot")
def auth_forgot(body: ForgotIn, request: Request):
    """Email a one-time reset link. The reply is the same whether or not the
    account exists, so it can't be used to find out who has an account."""
    guard = _client_address(request) + ":forgot"
    wait = auth.locked_out(guard)
    if wait:
        raise HTTPException(429, f"Too many reset requests. Try again in {wait // 60 + 1} min.")
    auth.record_failure(guard)
    started = auth.start_reset(body.username)
    if started:
        user, address, token = started
        link = f"{config.DASHBOARD_URL}/#reset={token}"

        def send():
            try:
                notifier.send_password_reset(address, user["username"], link, auth.RESET_MINUTES)
                db.log(f"Password reset link emailed to {address}", user_id=user["id"])
            except Exception as e:  # noqa: BLE001
                db.log(f"Couldn't email a password reset link: {e}", "error", user_id=user["id"])

        threading.Thread(target=send, daemon=True).start()  # same reply time either way
    return {"ok": True, "message": FORGOT_REPLY.format(minutes=auth.RESET_MINUTES)}


@app.post("/api/auth/reset")
def auth_reset(body: ResetIn, request: Request, response: Response):
    try:
        user_id = auth.finish_reset(body.token, body.new_password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    auth.clear_failures(_client_address(request))  # lift a lockout from the forgotten password
    return _signed_in(response, user_id)


class AccountEmailIn(BaseModel):
    email: str = Field("", max_length=254)


@app.post("/api/account/email")
def account_email(body: AccountEmailIn, request: Request):
    """The account's own address, used for password resets (not for job alerts)."""
    try:
        emails = notifier.clean_recipients([body.email]) if body.email.strip() else []
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.set_user_email(request.state.user_id, emails[0] if emails else None)
    return {"ok": True, "email": emails[0] if emails else None}


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


@app.post("/api/account/password")
def account_password(body: PasswordIn, request: Request, response: Response):
    user_id = request.state.user_id
    try:
        auth.change_password(user_id, body.current_password, body.new_password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    return _signed_in(response, user_id)  # this browser stays signed in


# --- dashboard -------------------------------------------------------------
@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state(level: str | None = None):
    r = db.get_resume()
    job = scheduler.get_job("check_all")
    companies = db.list_companies()
    mine = {c["id"] for c in companies}
    me = db.get_user(db.uid())
    return {
        "companies": companies,
        "checking": [i for i in monitor.running_ids() if i in mine],
        "progress": monitor.progress(mine),  # {company id: {stage, job_id, job_title, step, done, total}}
        "resume": {"filename": r["filename"], "uploaded_at": r["uploaded_at"], "chars": len(r["text"]),
                   "facts": r["facts"]} if r else None,
        "preferences": db.get_setting("preferences", {}) or {},
        "profile": {"needs_review": profile.needs_review(), "confirmed": profile.get(),
                    "career_stage": profile.for_matching({}).get("career_stage")},
        "counts": db.verdict_counts(level),
        "level_counts": db.level_counts(),
        "events": db.recent_events(30),
        "settings": {
            "kev_ready": config.kev_configured(),
            "email_ready": config.email_configured(),
            "alert_email": ", ".join(notifier.recipients()),
            "alert_emails": notifier.recipients(),
            "alert_emails_saved": bool(db.get_setting("alert_emails")),
            "account_email": me.get("email"),
            "reset_email": auth.reset_address(me) if config.email_configured() else None,
            "interval_minutes": config.CHECK_INTERVAL_MINUTES,
            "next_check_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
            "model": config.model_label(),
            "gemini_ready": config.gemini_configured(),
            "gemini_model": config.GEMINI_MODEL,
            "gemini_usage": extract.usage(),
        },
    }


@app.get("/api/jobs")
def jobs(verdict: str | None = None, company_id: int | None = None, level: str | None = None):
    return db.list_jobs(verdict=verdict or None, company_id=company_id, level_group=level or None)


# --- companies -------------------------------------------------------------
class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=4, max_length=600)


@app.post("/api/companies")
def add_company(body: CompanyIn):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if db.find_company_by_url(url):
        raise HTTPException(409, "You're already watching this careers page.")
    source, key = scrapers.detect_source(url)
    cid = db.add_company(body.name.strip(), url, source, key)
    db.log(f"Now watching {body.name.strip()} ({source})")
    monitor.submit_check(cid)
    return db.get_company(cid)


@app.delete("/api/companies/{company_id}")
def remove_company(company_id: int):
    c = db.get_company(company_id)
    if not c:
        raise HTTPException(404, "Company not found.")
    db.delete_company(company_id)
    db.log(f"Stopped watching {c['name']}")
    return {"ok": True}


@app.post("/api/companies/{company_id}/check")
def check_company(company_id: int):
    if not db.get_company(company_id):
        raise HTTPException(404, "Company not found.")
    monitor.submit_check(company_id)
    return {"ok": True}


@app.post("/api/companies/{company_id}/score")
def score_company(company_id: int):
    """Score this company's waiting jobs now, without reading its careers site again."""
    if not db.get_company(company_id):
        raise HTTPException(404, "Company not found.")
    monitor.submit_score(company_id)
    return {"ok": True}


@app.post("/api/score-now")
def score_now():
    return {"ok": True, "companies": monitor.submit_score_all()}


class ActiveIn(BaseModel):
    active: bool


@app.post("/api/companies/{company_id}/active")
def set_company_active(company_id: int, body: ActiveIn):
    c = db.get_company(company_id)
    if not c:
        raise HTTPException(404, "Company not found.")
    db.set_active([company_id], body.active)
    if body.active and not c["active"]:
        monitor.submit_check(company_id)
    return {"ok": True}


class CategoryActiveIn(BaseModel):
    category: str
    active: bool


@app.post("/api/categories/active")
def set_category_active(body: CategoryActiveIn):
    ids = [c["id"] for c in db.list_companies() if c["is_default"] and c["category"] == body.category]
    if not ids:
        raise HTTPException(404, "No starter-list companies in that group.")
    db.set_active(ids, body.active)
    db.log(f"{'Resumed' if body.active else 'Paused'} {len(ids)} companies in '{body.category}'")
    if body.active:
        for i in ids:
            monitor.submit_check(i)
    return {"ok": True, "changed": len(ids)}


@app.post("/api/companies/{company_id}/score-current")
def score_current(company_id: int):
    c = db.get_company(company_id)
    if not c:
        raise HTTPException(404, "Company not found.")
    n = db.unskip_jobs(company_id)
    db.log(f"{c['name']}: scoring {n} current openings")
    monitor.submit_check(company_id)
    return {"ok": True, "queued": n}


@app.post("/api/defaults/restore")
def restore_defaults():
    added = monitor.seed_default_companies(force=True)
    for c in db.list_companies():
        if c["is_default"] and c["last_checked_at"] is None:
            monitor.submit_check(c["id"])
    return {"ok": True, "added": added}


@app.post("/api/check-all")
def check_all():
    monitor.submit_check_all()
    return {"ok": True}


# --- resume, profile & preferences ------------------------------------------
# A plain `def` so FastAPI runs it in a worker thread: reading the resume can wait on Gemini.
@app.post("/api/resume")
def upload_resume(file: UploadFile = File(...)):
    """Save the resume and have Gemini read it into a draft profile. Nothing is
    re-scored yet: you check the draft first, then confirm it at /api/profile."""
    data = file.file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "Resume files must be under 8 MB.")
    name = file.filename or "resume"
    try:
        text, facts = resume.read_resume(name, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.save_resume(name, text, facts)
    profile.mark_uploaded()
    db.log(f"Resume updated ({name}). Check your profile to start scoring with it.")
    return {"ok": True, "chars": len(text), "read_by_gemini": facts is not None, "profile": profile.for_review()}


@app.get("/api/profile")
def get_profile():
    r = db.get_resume()
    return {
        "needs_review": profile.needs_review(),
        "read_by_gemini": bool(r and r.get("facts")),
        "resume_file": r["filename"] if r else None,
        "confirmed": profile.get(),
        "form": profile.for_review(),
        "field_options": matcher.FIELD_LABELS,
    }


MONTH = r"^(\d{4}-(0[1-9]|1[0-2])|present)?$"


class WorkIn(BaseModel):
    title: str = Field("", max_length=120)
    company: str = Field("", max_length=120)
    start: str | None = Field(None, pattern=MONTH)
    end: str | None = Field(None, pattern=MONTH)
    internship: bool = False


class ProfileIn(BaseModel):
    career_stage: Literal["fresher", "experienced"] = "experienced"
    degree: str | None = Field(None, max_length=200)
    graduation_year: int | None = Field(None, ge=1970, le=2040)
    cgpa: float | None = Field(None, ge=0, le=10)
    cgpa_scale: Literal[4, 10] | None = None
    percentage: float | None = Field(None, ge=0, le=100)
    current_role: str | None = Field(None, max_length=200)
    years_experience: float | None = Field(None, ge=0, le=60)  # blank = worked out from work history
    work_history: list[WorkIn] = Field(default_factory=list, max_length=30)
    skills: list[str] = Field(default_factory=list, max_length=60)
    projects: list[str] = Field(default_factory=list, max_length=15)
    coursework: list[str] = Field(default_factory=list, max_length=30)
    fields: list[Literal[tuple(matcher.FIELDS)]] = Field(default_factory=list, max_length=6)  # type: ignore[valid-type]
    based_in: list[str] = Field(default_factory=list, max_length=10)


@app.post("/api/profile")
def confirm_profile(body: ProfileIn):
    data = body.model_dump()
    data["work_history"] = [w for w in data["work_history"] if w["title"] or w["company"]]
    data["skills"] = [s.strip()[:80] for s in data["skills"] if s.strip()]
    data["projects"] = [s.strip()[:200] for s in data["projects"] if s.strip()]
    data["coursework"] = [s.strip()[:80] for s in data["coursework"] if s.strip()]
    data["fields"] = list(dict.fromkeys(data["fields"]))
    data["based_in"] = [s.strip()[:80] for s in data["based_in"] if s.strip()]
    for k in ("degree", "current_role"):
        data[k] = (data[k] or "").strip() or None
    saved = profile.confirm(data)
    db.log("Profile confirmed")
    monitor.submit_rescore()
    return {"ok": True, "profile": saved}


class PreferencesIn(BaseModel):
    locations: str = Field("", max_length=300)
    include_internships: bool = True
    india_only: bool = True


@app.post("/api/preferences")
def save_preferences(body: PreferencesIn):
    db.set_setting("preferences", body.model_dump())
    monitor.submit_rescore()
    return {"ok": True}


class AlertEmailsIn(BaseModel):
    emails: list[str] = Field(default_factory=list, max_length=50)


@app.post("/api/alert-emails")
def save_alert_emails(body: AlertEmailsIn):
    """Where alerts go. An empty list falls back to ALERT_EMAIL_TO in .env."""
    try:
        emails = notifier.clean_recipients(body.emails)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if emails:
        db.set_setting("alert_emails", emails)
        db.log(f"Alerts now go to {', '.join(emails)}")
    else:
        db.delete_setting("alert_emails")
        db.log("Alert addresses cleared; using ALERT_EMAIL_TO from .env")
    return {"ok": True, "emails": notifier.recipients()}


@app.post("/api/test-email")
def test_email():
    try:
        notifier.send_test_email()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Test email failed: {e}") from e
    return {"ok": True}


# --- incoming webhooks -----------------------------------------------------
# Point changedetection.io, a cron job, Zapier, etc. at these to trigger an
# immediate check instead of waiting for the next scheduled run.
def _require_secret(header_secret: str | None, query_secret: str | None):
    if config.WEBHOOK_SECRET and config.WEBHOOK_SECRET not in (header_secret, query_secret):
        raise HTTPException(401, "Missing or wrong webhook secret.")


@app.post("/webhook/check/{company_id}")
def webhook_company(company_id: int, x_webhook_secret: str | None = Header(None),
                    secret: str | None = Query(None)):
    _require_secret(x_webhook_secret, secret)
    if not db.get_company_any(company_id):
        raise HTTPException(404, "Company not found.")
    monitor.submit_check(company_id)
    return {"ok": True, "queued": company_id}


@app.post("/webhook/check")
async def webhook_any(request: Request, x_webhook_secret: str | None = Header(None),
                      secret: str | None = Query(None)):
    """Body can be empty (check everything) or JSON with "url" or "company"."""
    _require_secret(x_webhook_secret, secret)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    wanted_url = (payload or {}).get("url")
    wanted_name = ((payload or {}).get("company") or "").lower()
    matched = [c for c in db.all_companies_brief()
               if (wanted_url and wanted_url.rstrip("/") in c["url"])
               or (wanted_name and wanted_name == c["name"].lower())]
    if wanted_url or wanted_name:
        if not matched:
            raise HTTPException(404, "No watched company matches that url or name.")
        for c in matched:
            monitor.submit_check(c["id"])
        return {"ok": True, "queued": [c["id"] for c in matched]}
    monitor.submit_check_everyone()
    return {"ok": True, "queued": "all"}
