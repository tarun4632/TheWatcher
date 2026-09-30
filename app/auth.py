"""Accounts: sign up, log in, scrypt-hashed passwords and cookie sessions.

Each account has its own resume, profile, companies, jobs and alert addresses.
Anyone who can open the dashboard can create an account while ALLOW_SIGNUP=1;
set ALLOW_SIGNUP=0 once everyone who should have one has signed up.

Forgot a password? Click "Forgot password?" on the sign-in page: a one-time link
is emailed to the account's email. Without email set up, the owner can run
`python -m app.auth set-password USERNAME`.
"""
import getpass
import hashlib
import hmac
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, db

COOKIE = "tw_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8
RESET_MINUTES = 60            # how long an emailed reset link works
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{3,60}$")
MAX_FAILURES = 5              # wrong passwords (or sign-ups) from one address...
LOCKOUT_SECONDS = 15 * 60     # ...lock that address out for this long
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_DUMMY_HASH = None            # compared against when the username doesn't exist

_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


class AuthError(ValueError):
    """A sign-up or password change we refuse, with a message fit to show."""


# --- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Use at least {MIN_PASSWORD_LENGTH} characters for the password.")


# --- accounts --------------------------------------------------------------
def signup_open() -> bool:
    """The first account can always be made; later ones only while ALLOW_SIGNUP=1."""
    return config.ALLOW_SIGNUP or db.first_user_id() is None


def create_account(username: str, password: str) -> int:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames are 3 to 60 letters, digits or . _ @ + -")
    _check_new_password(password)
    try:
        user_id = db.create_user(username, hash_password(password))
    except sqlite3.IntegrityError as e:
        raise AuthError("That username is taken. Log in instead, or pick another.") from e
    db.log(f"Account created for {username}", user_id=user_id)
    return user_id


def check_login(username: str, password: str) -> int | None:
    """The user's id if the password is right, else None."""
    global _DUMMY_HASH
    user = db.get_user_by_name(username)
    if not user:
        # Hash anyway, so a wrong username takes as long as a wrong password.
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(password, _DUMMY_HASH)
        return None
    return user["id"] if verify_password(password, user["password_hash"]) else None


def change_password(user_id: int, current: str, new: str) -> None:
    user = db.get_user(user_id)
    if not user or not verify_password(current, user["password_hash"]):
        raise AuthError("Your current password is wrong.")
    _check_new_password(new)
    set_password(user_id, new)
    db.log("Password changed. Other sessions were signed out.", user_id=user_id)


def set_password(user_id: int, new: str) -> None:
    db.set_password_hash(user_id, hash_password(new))
    db.delete_user_sessions(user_id)  # signs out every browser


def reset_address(user: dict) -> str | None:
    """Where a reset link for this account goes: its own email. The first account
    (whoever set up .env) falls back to ALERT_EMAIL_TO, then SMTP_USER. Alert
    addresses are never used: they may belong to other people."""
    if user.get("email"):
        return user["email"]
    if user["id"] == db.first_user_id():
        fallback = [a.strip() for a in config.ALERT_EMAIL_TO.split(",") if a.strip()]
        return fallback[0] if fallback else None
    return None


def start_reset(username: str) -> tuple[dict, str, str] | None:
    """(user, address, token) for an account we can email, else None."""
    user = db.get_user_by_name(username)
    if not user or not config.email_configured():
        return None
    address = reset_address(user)
    if not address:
        return None
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(minutes=RESET_MINUTES)
    db.add_reset_token(_hash_token(token), user["id"], expires.isoformat(timespec="seconds"))
    return user, address, token


def finish_reset(token: str, new_password: str) -> int:
    """Set the new password with an emailed token. Returns the user's id."""
    _check_new_password(new_password)  # before the token is used up
    user_id = db.take_reset_token(_hash_token(token))
    if user_id is None:
        raise AuthError("This reset link is invalid or has expired. Ask for a new one.")
    set_password(user_id, new_password)
    db.log("Password reset with an emailed link. Other sessions were signed out.", user_id=user_id)
    return user_id


# --- sessions --------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.add_session(_hash_token(token), user_id, expires.isoformat(timespec="seconds"))
    return token


def session_user(token: str | None) -> int | None:
    return db.session_user(_hash_token(token)) if token else None


def end_session(token: str | None) -> None:
    if token:
        db.delete_session(_hash_token(token))


def cookie_options() -> dict:
    return {"max_age": SESSION_DAYS * 86400, "httponly": True, "samesite": "lax",
            "secure": config.DASHBOARD_URL.startswith("https://")}


# --- brute-force guard -----------------------------------------------------
def locked_out(address: str) -> int:
    """Seconds this address must still wait, or 0."""
    with _lock:
        recent = [t for t in _failures.get(address, []) if time.monotonic() - t < LOCKOUT_SECONDS]
        _failures[address] = recent
        if len(recent) < MAX_FAILURES:
            return 0
        return int(LOCKOUT_SECONDS - (time.monotonic() - recent[0])) + 1


def record_failure(address: str) -> None:
    with _lock:
        _failures.setdefault(address, []).append(time.monotonic())


def clear_failures(address: str) -> None:
    with _lock:
        _failures.pop(address, None)


def _cli(args: list[str]) -> int:
    db.init()
    if args == ["list"]:
        for u in db.list_users():
            print(f"{u['id']:>4}  {u['username']}  (since {u['created_at'][:10]})")
        return 0
    if len(args) == 2 and args[0] == "set-password":
        user = db.get_user_by_name(args[1])
        if not user:
            print(f"No account called {args[1]!r}. See: python -m app.auth list")
            return 1
        new = getpass.getpass("New password: ")
        if new != getpass.getpass("Type it again: "):
            print("The passwords don't match.")
            return 1
        try:
            _check_new_password(new)
        except AuthError as e:
            print(e)
            return 1
        set_password(user["id"], new)
        print(f"Password changed for {user['username']}. Their other sessions were signed out.")
        return 0
    print("Usage:\n  python -m app.auth list\n  python -m app.auth set-password USERNAME")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
