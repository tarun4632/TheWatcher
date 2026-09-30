"""All settings come from environment variables (or a .env file next to the app)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# --- Kev: the decision model that scores jobs --------------------------------
# Kev (github.com/jaredpalmer/kev) is an open-source decision model you run
# yourself; `python -m kev.serve` answers the same /v1/systemone requests this
# app sends. Kev-0.8B runs on a laptop CPU at ~2-3 s per job.
KEV_BASE_URL = os.getenv("KEV_BASE_URL", "http://127.0.0.1:8009").rstrip("/")
KEV_MODEL = os.getenv("KEV_MODEL", "kev-latest")  # the name the Kev server answers to
KEV_API_KEY = os.getenv("KEV_API_KEY", "")         # only if the Kev server was started with KEV_API_KEY
KEV_TIMEOUT_SECONDS = _float("KEV_TIMEOUT_SECONDS", 120)  # CPU inference is slow; one job must finish in this

# --- Gemini (reads facts from resumes and job postings) --------------------
# Free tier only. Gemini pulls out years, batch, grades, degrees and location
# eligibility; plain code still does every comparison. Without a key (or once
# the daily quota is used up) the app falls back to its built-in text rules.
# Get a key at https://aistudio.google.com/apikey and check your model's free
# limits at https://aistudio.google.com/rate-limit.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "minimal")  # blank = model default
GEMINI_RPM = _float("GEMINI_RPM", 10)              # stay under the free tier's requests per minute
GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_LIMIT", "500"))  # resets at midnight Pacific time

# --- Rate limits and retries (every outgoing request) ----------------------
JOB_BOARD_RPS = _float("JOB_BOARD_RPS", 2)         # requests per second to any one careers site
KEV_RPM = _float("KEV_RPM", 60)
SMTP_PER_MINUTE = _float("SMTP_PER_MINUTE", 20)
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "4"))      # per request
RETRY_MAX_WAIT_SECONDS = _float("RETRY_MAX_WAIT_SECONDS", 90)        # total backoff per request
PROVIDER_COOLDOWN_SECONDS = _float("PROVIDER_COOLDOWN_SECONDS", 300)  # pause after repeated 429s
MAX_EVAL_ATTEMPTS = int(os.getenv("MAX_EVAL_ATTEMPTS", "4"))    # then the job is marked "failed"
MAX_EMAIL_ATTEMPTS = int(os.getenv("MAX_EMAIL_ATTEMPTS", "4"))

# --- Monitoring ------------------------------------------------------------
# Add ~100 companies hiring in India (app/default_companies.json) on first start.
LOAD_DEFAULT_COMPANIES = os.getenv("LOAD_DEFAULT_COMPANIES", "1") == "1"
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
MAX_JOB_AGE_DAYS = int(os.getenv("MAX_JOB_AGE_DAYS", "10"))  # ignore postings older than this
MAX_EVALS_PER_RUN = int(os.getenv("MAX_EVALS_PER_RUN", "150"))  # cost guard per company check
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "0") == "1"  # for JavaScript-only career pages

# --- Decision thresholds ---------------------------------------------------
# How sure Kev must be that a job is in your fields (below it: Not a fit, and no Gemini call).
RELATED_THRESHOLD = _float("RELATED_THRESHOLD", 0.5)
# How sure Kev must be that you meet each must-have requirement (below it: named as missing).
ELIGIBLE_THRESHOLD = _float("ELIGIBLE_THRESHOLD", 0.5)

# --- Email (SMTP) ----------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# Default recipients; the dashboard can replace them (stored in the database).
# Comma-separate several addresses.
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "") or SMTP_USER

# --- App -------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "data/thewatcher.db")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8000").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# 1 = anyone who can open the dashboard can create an account (each gets its own data).
# 0 = no new accounts; the very first account can always be created.
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "1") == "1"
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36 TheWatcher/1.0",
)


def email_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def kev_configured() -> bool:
    return bool(KEV_BASE_URL)


def gemini_configured() -> bool:
    return bool(GEMINI_API_KEY)


def model_label() -> str:
    return "kev" if KEV_MODEL == "kev-latest" else KEV_MODEL
