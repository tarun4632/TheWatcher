import httpx
import pytest

from app import config, matcher


def _client_with(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))


def test_sends_a_systemone_request_to_the_kev_server(monkeypatch):
    monkeypatch.setattr(config, "KEV_BASE_URL", "http://127.0.0.1:8009")
    monkeypatch.setattr(config, "KEV_API_KEY", "")
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"model": "kev-latest", "answers": {"x": {"type": "noul", "noul": 0.9}}})

    _client_with(monkeypatch, handler)
    out = matcher._call_kev({"job": "j"}, {"x": {"type": "noul", "instructions": "?"}})
    assert out["answers"]["x"]["noul"] == 0.9
    assert seen["url"] == "http://127.0.0.1:8009/v1/systemone"
    assert seen["auth"] is None and b'"model":"kev-latest"' in seen["body"].replace(b" ", b"")


def test_api_key_is_sent_when_set(monkeypatch):
    monkeypatch.setattr(config, "KEV_API_KEY", "secret")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"answers": {}})

    _client_with(monkeypatch, handler)
    matcher._call_kev({}, {})
    assert seen["auth"] == "Bearer secret"


def test_server_down_pauses_the_check_instead_of_failing_each_job(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    _client_with(monkeypatch, handler)
    with pytest.raises(matcher.KevError) as e:
        matcher._call_kev({}, {})
    assert e.value.stop_run and "Is the Kev server running?" in str(e.value)


def test_bad_request_is_a_per_job_error(monkeypatch):
    _client_with(monkeypatch, lambda request: httpx.Response(422, json={"detail": "bad question"}))
    with pytest.raises(matcher.KevError) as e:
        matcher._call_kev({}, {})
    assert not e.value.stop_run and "422" in str(e.value)
