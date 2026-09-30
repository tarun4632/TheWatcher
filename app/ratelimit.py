"""Rate limits and retries for every outgoing request.

* Each key (a careers-site host, "kev", "gemini", "smtp") has its own limiter,
  shared by all worker threads, that spaces requests out evenly.
* `send()` retries network errors, 429 and 5xx with exponential backoff plus
  random jitter, honours Retry-After, and gives up after RETRY_MAX_ATTEMPTS
  tries or RETRY_MAX_WAIT_SECONDS of total waiting.
* A key that is still answering 429 after that, or asks us to wait longer than
  we're willing to, goes on cooldown. Calls to it then fail fast with
  `CoolingDown` instead of piling on for the rest of the check.
"""
import json
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import config

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Replaced in tests so nothing actually waits.
sleep = time.sleep
monotonic = time.monotonic


class CoolingDown(httpx.HTTPError):
    def __init__(self, key: str, seconds: float):
        super().__init__(f"{key} is rate-limiting requests; paused for another {seconds:.0f}s")
        self.key = key
        self.seconds = seconds


class _Limiter:
    """Hands out evenly spaced time slots, one request per slot."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.next_at = 0.0
        self.lock = threading.Lock()

    def reserve(self) -> float:
        with self.lock:
            now = monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
            return at - now


_limiters: dict[str, _Limiter] = {}
_cooldowns: dict[str, float] = {}
_lock = threading.Lock()


def _per_second(key: str) -> float:
    per_minute = {"kev": config.KEV_RPM, "gemini": config.GEMINI_RPM, "smtp": config.SMTP_PER_MINUTE}
    if key in per_minute:
        return per_minute[key] / 60
    return config.JOB_BOARD_RPS


def acquire(key: str) -> None:
    """Wait for this key's next free slot. Raises CoolingDown if the key is paused."""
    check_cooldown(key)
    with _lock:
        limiter = _limiters.get(key)
        if limiter is None:
            limiter = _limiters[key] = _Limiter(_per_second(key))
    wait = limiter.reserve()
    if wait > 0:
        sleep(wait)


def check_cooldown(key: str) -> None:
    with _lock:
        until = _cooldowns.get(key, 0.0)
    left = until - monotonic()
    if left > 0:
        raise CoolingDown(key, left)


def cool_down(key: str, seconds: float) -> None:
    with _lock:
        _cooldowns[key] = max(_cooldowns.get(key, 0.0), monotonic() + seconds)


def reset() -> None:
    """Forget all limiter state (used by tests)."""
    with _lock:
        _limiters.clear()
        _cooldowns.clear()


def backoff(attempt: int) -> float:
    """1s, 2s, 4s, ... capped at 30s, with jitter so threads don't retry in step."""
    ceiling = min(30.0, 2.0 ** (attempt - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait: the Retry-After header, or Google's RetryInfo."""
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    try:
        details = response.json().get("error", {}).get("details", [])
    except (ValueError, AttributeError, json.JSONDecodeError):
        return None
    for d in details if isinstance(details, list) else []:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str((d or {}).get("retryDelay", "")))
        if m:
            return float(m.group(1))
    return None


def send(client: httpx.Client, method: str, url: str, *, key: str | None = None,
         attempts: int | None = None, **kwargs) -> httpx.Response:
    """Rate-limited request with retries. Returns the last response (the caller
    still calls raise_for_status) and re-raises the last network error."""
    key = key or httpx.URL(url).host
    attempts = attempts or config.RETRY_MAX_ATTEMPTS
    budget = config.RETRY_MAX_WAIT_SECONDS
    waited = 0.0
    for attempt in range(1, attempts + 1):
        acquire(key)
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError:
            delay = backoff(attempt)
            if attempt == attempts or waited + delay > budget:
                raise
            sleep(delay)
            waited += delay
            continue

        if response.status_code not in RETRY_STATUS:
            return response
        hinted = retry_after(response)
        delay = max(hinted or 0.0, backoff(attempt))
        if attempt == attempts or waited + delay > budget:
            if response.status_code == 429:
                cool_down(key, max(hinted or 0.0, config.PROVIDER_COOLDOWN_SECONDS))
            return response
        sleep(delay)
        waited += delay
    raise AssertionError("unreachable")
