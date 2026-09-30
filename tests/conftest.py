import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import types  # noqa: E402

from app import auth, config, db, monitor, ratelimit  # noqa: E402


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No real Gemini key or sign-up setting from a local .env, and no real waiting in
    rate limits or retries. Code called straight from a test runs as user 1."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "ALLOW_SIGNUP", True)
    monkeypatch.setattr(ratelimit, "sleep", lambda s: None)
    # Background checks never go looking for a company's jobs on the real internet in tests.
    # (test_discover.py calls app.discover directly, against fake sites.)
    monkeypatch.setattr(monitor, "discover", types.SimpleNamespace(discover=lambda url, name: {
        "source": "generic", "source_key": "",
        "recipe": {"steps": ["(discovery is off in tests)"], "discovered_at": "2026-01-01T00:00:00+00:00"}}))
    ratelimit.reset()
    auth._failures.clear()  # login and sign-up limits
    with db.as_user(1):
        yield
    ratelimit.reset()
