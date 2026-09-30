"""Finding a company's jobs from any careers URL. Every site here is fake (httpx.MockTransport)."""
import json

import httpx
import pytest

from app import config, db, discover, monitor, scrapers


def fake_web(monkeypatch, routes):
    """routes: {(method, url-without-query): response or callable(request) -> response}. Others 404."""
    def handler(request):
        key = (request.method, str(request.url).split("?")[0])
        hit = routes.get(key)
        if hit is None:
            return httpx.Response(404, text="not found")
        return hit(request) if callable(hit) else hit
    real = httpx.Client
    monkeypatch.setattr(scrapers, "_client", lambda: real(transport=httpx.MockTransport(handler), follow_redirects=True))
    monkeypatch.setattr(discover.Discovery, "browse", lambda self: None)  # no headless browser in tests


def html(body):
    return httpx.Response(200, text=f"<html><body>{body}</body></html>", headers={"content-type": "text/html"})


# --- finding a job list in any JSON -------------------------------------------------
IBM_LIKE = {"hits": {"total": {"value": 3}, "hits": [
    {"_id": f"h{i}", "_source": {"title": t, "url": f"https://careers.example.com/JobDetail?jobId={i}",
                                 "field_keyword_19": loc, "field_keyword_08": "Engineering"}}
    for i, (t, loc) in enumerate([("Backend Engineer", "Bengaluru, IN"), ("Data Scientist", "Pune, IN"),
                                  ("Consultant", "Zurich, CH")])]}}
NAV_MENU = {"links": [{"title": "Products", "url": "/products"}, {"title": "About", "url": "/about"},
                      {"title": "Careers", "url": "/careers"}]}


def test_finds_the_job_list_and_its_fields():
    path, items = scrapers.find_job_list(IBM_LIKE)
    assert path == ("hits", "hits") and len(items) == 3
    fields = scrapers.map_job_fields(items)
    assert fields["title"] == "_source.title" and fields["location"] == "_source.field_keyword_19"
    jobs = scrapers.jobs_from_items(items, fields, "https://example.com/careers")
    assert jobs[0] == {"external_id": "h0", "title": "Backend Engineer", "location": "Bengaluru, IN",
                       "url": "https://careers.example.com/JobDetail?jobId=0", "description": "", "posted_at": ""}


def test_a_menu_is_not_a_job_list():
    assert scrapers.find_job_list(NAV_MENU) is None


# --- discovery -----------------------------------------------------------------------
def test_landing_page_to_a_confirmed_greenhouse_board(monkeypatch):
    """Databricks: the careers page links to 'See open jobs', which names no board; the board is guessed."""
    fake_web(monkeypatch, {
        ("GET", "https://acme.example/careers"): html('<a href="/careers/open-positions">See open jobs</a>'),
        ("GET", "https://acme.example/careers/open-positions"): html('<div id="app"></div>'),
        ("GET", "https://boards-api.greenhouse.io/v1/boards/acme"): httpx.Response(200, json={"name": "Acme"}),
        ("GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs"): httpx.Response(200, json={"jobs": [{"id": 1}]}),
    })
    r = discover.discover("https://acme.example/careers", "Acme")
    assert (r["source"], r["source_key"]) == ("greenhouse", "acme")
    assert any("See open jobs" in s or "open-positions" in s for s in r["recipe"]["steps"])


def test_a_guessed_board_of_another_company_is_rejected(monkeypatch):
    fake_web(monkeypatch, {
        ("GET", "https://acme.example/careers"): html("<p>Join us</p>"),
        ("GET", "https://boards-api.greenhouse.io/v1/boards/acme"): httpx.Response(200, json={"name": "Acme Plumbing Ltd"}),
        ("GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs"): httpx.Response(200, json={"jobs": [{"id": 5}]}),
    })
    r = discover.discover("https://acme.example/careers", "Zeta")
    assert r["source"] == "generic" and r["recipe"].get("not_found")


def test_eightfold_site(monkeypatch):
    fake_web(monkeypatch, {
        ("GET", "https://explore.jobs.acme.net/careers"): html(
            '<script src="https://static.vscdn.net/x.js"></script><script>{"domain": "acme.com"}</script>'),
        ("GET", "https://explore.jobs.acme.net/api/apply/v2/jobs"):
            lambda req: httpx.Response(200, json={"count": 2, "positions": [
                {"id": 7, "name": "SRE", "location": "Pune, India", "t_create": 1790640000,
                 "canonicalPositionUrl": "https://explore.jobs.acme.net/careers/job/7", "job_description": "<p>Run it</p>"}]}
                if req.url.params.get("domain") == "acme.com" else httpx.Response(200, json={"count": 0})),
    })
    r = discover.discover("https://explore.jobs.acme.net/careers", "Acme")
    assert (r["source"], r["source_key"]) == ("eightfold", "explore.jobs.acme.net|acme.com")
    jobs = scrapers._eightfold(r["source_key"])
    assert jobs[0]["title"] == "SRE" and jobs[0]["description"] == "Run it" and jobs[0]["posted_at"].startswith("2026-09")


def test_jobs_only_on_linkedin_are_unsupported(monkeypatch):
    fake_web(monkeypatch, {("GET", "https://careers.acme.example/"): html(
        '<a href="https://www.linkedin.com/company/acme/jobs/">See open jobs</a>')})
    r = discover.discover("https://careers.acme.example/", "Acme")
    assert r["source"] == "unsupported" and "LinkedIn" in r["recipe"]["reason"]
    with pytest.raises(scrapers.Unsupported):
        scrapers.fetch_jobs({"source": "unsupported", "source_key": "", "url": "x", "recipe": r["recipe"]})


def test_a_captured_job_request_is_replayed_with_paging(monkeypatch):
    """IBM: the page POSTs to a search service with a page size but no offset; 'from' is tried and confirmed."""
    all_jobs = [{"_id": f"j{i}", "_source": {"title": f"Engineer {i}", "url": f"/job/{i}",
                                             "field_keyword_19": "Pune, IN"}} for i in range(5)]

    def search(req):
        body = json.loads(req.content)
        start, size = body.get("from", 0), body["size"]
        return httpx.Response(200, json={"hits": {"hits": all_jobs[start:start + size]}})
    fake_web(monkeypatch, {("POST", "https://api.example.com/search"): search})
    d = discover.Discovery("https://example.com/careers", "Example")
    captured = [{"method": "POST", "url": "https://api.example.com/search",
                 "body": json.dumps({"query": {}, "size": 2}), "json": {"hits": {"hits": all_jobs[:2]}}}]
    feed = d.feed_from(captured)
    assert feed["page"]["param"] == "from" and feed["list_path"] == ["hits", "hits"]
    jobs = scrapers.fetch_jobs({"source": "json", "source_key": "", "url": "https://example.com/careers",
                                "recipe": {"feed": feed}})[0]
    assert [j["title"] for j in jobs] == [f"Engineer {i}" for i in range(5)]
    assert jobs[0]["url"] == "https://example.com/job/0"


# --- the monitor runs discovery once, and again when the recipe breaks ----------------
def test_monitor_discovers_once_then_follows_the_recipe(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thewatcher.db"))
    db.init()
    cid = db.add_company("Acme", "https://acme.example/careers", "generic", "")
    runs = []

    def fake_discover(url, name):
        runs.append(url)
        return {"source": "greenhouse", "source_key": "acme",
                "recipe": {"steps": ["Found the Greenhouse board 'acme'"], "discovered_at": "2026-09-01T00:00:00+00:00"}}
    monkeypatch.setattr(monitor.discover, "discover", fake_discover)
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([], None))
    monitor.check_company(cid)
    monitor.check_company(cid)
    c = db.get_company(cid)
    assert runs == ["https://acme.example/careers"] and c["source"] == "greenhouse"

    def broken(company, india_only=False):
        raise httpx.ConnectError("gone")
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", broken)
    monitor.check_company(cid)  # the recipe fails: marked stale
    monkeypatch.setattr(monitor.scrapers, "fetch_jobs", lambda company, india_only=False: ([], None))
    monitor.check_company(cid)  # stale and old enough: worked out again
    assert len(runs) == 2


def test_eightfold_pages_by_what_comes_back(monkeypatch):
    """Eightfold returns at most 10 jobs a request, whatever `num` asks for."""
    now = 1790700000
    positions = [{"id": i, "name": f"Job {i}", "location": "Mumbai,India", "t_create": now - i * 3600,
                  "canonicalPositionUrl": f"https://ef.test/careers/job/{i}"} for i in range(25)]

    def api(req):
        start = int(req.url.params["start"])
        return httpx.Response(200, json={"count": 25, "positions": positions[start:start + 10]})
    fake_web(monkeypatch, {("GET", "https://ef.test/api/apply/v2/jobs"): api})
    assert [j["title"] for j in scrapers._eightfold("ef.test|acme.com")] == [f"Job {i}" for i in range(25)]


def test_a_shuffling_search_is_asked_for_a_fixed_order(monkeypatch):
    """IBM sorts by popularity, which shifts between requests; sorted by id, paging is complete."""
    ids = [f"j{i:02d}" for i in range(12)]
    calls = {"n": 0}

    def search(req):
        body = json.loads(req.content)
        calls["n"] += 1
        order = sorted(ids) if body.get("sort") == [{"_id": "asc"}] else ids[calls["n"] % 3:] + ids[:calls["n"] % 3]
        start = body.get("from", 0)
        return httpx.Response(200, json={"hits": {"hits": [
            {"_id": i, "_source": {"title": f"Job {i}", "url": f"/j/{i}", "loc": "Pune, IN"}}
            for i in order[start:start + body["size"]]]}})
    fake_web(monkeypatch, {("POST", "https://api.example.com/search"): search})
    feed = {"method": "POST", "url": "https://api.example.com/search", "params": {},
            "body": {"query": {}, "size": 5, "sort": [{"pageviews": "desc"}]}, "list_path": ["hits", "hits"],
            "fields": {"title": "_source.title", "url": "_source.url", "id": "_id", "location": "_source.loc",
                       "posted": None, "description": None},
            "base_url": "https://example.com/", "page": {"where": "body", "param": "from", "start": 0, "step": 5,
                                                         "size_param": "size", "size": 5}}
    fixed, note = discover.stable_order(feed)
    assert fixed["body"]["sort"] == [{"_id": "asc"}] and fixed["order_checked"] and "fixed order" in note
    assert sorted(j["external_id"] for j in scrapers._json_feed(fixed)) == ids  # all 12, none repeated


def test_the_reader_goes_on_past_a_page_of_repeats(monkeypatch):
    pages = [["a", "b"], ["a", "b"], ["c", "d"], []]  # page 2 repeats page 1 (a shifting order)

    def search(req):
        page = json.loads(req.content)["from"] // 2
        return httpx.Response(200, json={"items": [{"id": i, "title": f"Job {i}", "location": "Pune, IN"}
                                                   for i in pages[page]]})
    fake_web(monkeypatch, {("POST", "https://api.example.com/jobs"): search})
    feed = {"method": "POST", "url": "https://api.example.com/jobs", "params": {}, "body": {"size": 2},
            "list_path": ["items"], "fields": {"title": "title", "url": None, "id": "id", "location": "location",
                                               "posted": None, "description": None},
            "base_url": "https://example.com/", "page": {"where": "body", "param": "from", "start": 0, "step": 2,
                                                         "size_param": "size", "size": 2}}
    assert [j["external_id"] for j in scrapers._json_feed(feed)] == ["a", "b", "c", "d"]
