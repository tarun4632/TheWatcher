import httpx
import pytest

from app import config, ratelimit


def _client(responses):
    """A client that answers with the given status codes in order."""
    calls = []

    def handler(request):
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_retries_server_errors_then_succeeds(monkeypatch):
    waits = []
    monkeypatch.setattr(ratelimit, "sleep", waits.append)
    monkeypatch.setattr(config, "JOB_BOARD_RPS", 0)  # no spacing, so only retry waits are counted
    c, calls = _client([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})])
    r = ratelimit.send(c, "GET", "https://api.example.com/x", key="test-retry")
    assert r.status_code == 200
    assert len(calls) == 3
    assert len(waits) == 2 and all(w > 0 for w in waits)


def test_client_errors_are_not_retried():
    c, calls = _client([httpx.Response(404)])
    assert ratelimit.send(c, "GET", "https://api.example.com/x").status_code == 404
    assert len(calls) == 1


def test_network_errors_retry_then_raise(monkeypatch):
    monkeypatch.setattr(config, "RETRY_MAX_ATTEMPTS", 3)
    c, calls = _client([httpx.ConnectError("down")])
    with pytest.raises(httpx.ConnectError):
        ratelimit.send(c, "GET", "https://api.example.com/x")
    assert len(calls) == 3


def test_repeated_429_puts_key_on_cooldown(monkeypatch):
    monkeypatch.setattr(config, "RETRY_MAX_ATTEMPTS", 2)
    c, calls = _client([httpx.Response(429)])
    r = ratelimit.send(c, "POST", "https://api.example.com/x", key="gemini")
    assert r.status_code == 429 and len(calls) == 2
    with pytest.raises(ratelimit.CoolingDown):
        ratelimit.send(c, "POST", "https://api.example.com/x", key="gemini")
    assert len(calls) == 2  # the cooldown stopped the request before it was sent


def test_long_retry_hint_is_not_waited_out(monkeypatch):
    waits = []
    monkeypatch.setattr(ratelimit, "sleep", waits.append)
    c, calls = _client([httpx.Response(429, headers={"retry-after": "3600"})])
    r = ratelimit.send(c, "GET", "https://api.example.com/x", key="kev")
    assert r.status_code == 429 and len(calls) == 1 and not waits
    with pytest.raises(ratelimit.CoolingDown):
        ratelimit.check_cooldown("kev")


def test_google_retry_delay_is_read():
    r = httpx.Response(429, json={"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}})
    assert ratelimit.retry_after(r) == 17


def test_limiter_spaces_requests(monkeypatch):
    waits = []
    monkeypatch.setattr(ratelimit, "sleep", waits.append)
    monkeypatch.setattr(ratelimit, "monotonic", lambda: 100.0)
    monkeypatch.setattr(config, "JOB_BOARD_RPS", 2)
    ratelimit.acquire("boards.example.com")
    ratelimit.acquire("boards.example.com")
    ratelimit.acquire("other.example.com")
    assert waits == [0.5]
