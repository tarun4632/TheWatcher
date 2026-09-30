import json
from datetime import datetime, timedelta, timezone

import httpx

from app import scrapers


def _client(handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler))
    return factory


def test_greenhouse_list_skips_content_and_old_jobs(monkeypatch):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/jobs/1"):
            return httpx.Response(200, json={"content": "<p>Build services</p>"})
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "location": {"name": "Bengaluru"}, "updated_at": recent},
            {"id": 2, "title": "Old role", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
             "location": {"name": "Bengaluru"}, "updated_at": old},
        ]})

    monkeypatch.setattr(scrapers, "_client", _client(handler))
    jobs = scrapers._greenhouse("acme")
    assert [j["external_id"] for j in jobs] == ["1"]
    assert jobs[0]["description"] == ""
    assert "content" not in seen[0].url.params
    text = scrapers.fetch_description({"source": "greenhouse", "source_key": "acme"}, jobs[0])
    assert "Build services" in text
    assert seen[1].url.path.endswith("/jobs/1")


def test_workday_stops_paging_when_a_page_is_old(monkeypatch):
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offsets.append(body["offset"])
        if body["offset"] == 0:
            postings = [
                {"title": f"New {i}", "externalPath": f"/job/{i}", "locationsText": "Pune",
                 "postedOn": "Posted 2 Days Ago"}
                for i in range(20)
            ]
        elif body["offset"] == 20:
            postings = [
                {"title": f"Old {i}", "externalPath": f"/job/old{i}", "locationsText": "Pune",
                 "postedOn": "Posted 30+ Days Ago"}
                for i in range(20)
            ]
        else:
            raise AssertionError(f"unexpected offset {body['offset']}")
        return httpx.Response(200, json={"total": 80, "jobPostings": postings})

    monkeypatch.setattr(scrapers, "_client", _client(handler))
    jobs = scrapers._workday("acme.wd1.myworkdayjobs.com|acme|careers", india_only=False)
    assert offsets == [0, 20]
    assert len(jobs) == 20
    assert all(j["external_id"].startswith("/job/") and "old" not in j["external_id"] for j in jobs)


def test_generic_page_skips_navigation_links():
    """IBM's careers page: every link the old reader took for a job was navigation or a blog post."""
    from app import scrapers
    page = """<html><body>
      <a href="/careers/JobAlerts?source=WEB">Turn on Job Alerts</a>
      <a href="/careers/blog">Discover more stories</a>
      <a href="/careers/blog/once-an-ibmer-always-an-ibmer">Learn more about Alumni program</a>
      <a href="/careers/search">Search Jobs</a>
      <a href="/in-en/careers/">See your results</a>
      <li><a href="/careers/jobs/12345-software-engineer">Software Engineer - Backend</a><span>Bengaluru</span></li>
    </body></html>"""
    jobs = scrapers._generic_from_html("https://www.example.com/careers", page)
    assert [j["title"] for j in jobs] == ["Software Engineer - Backend"]
