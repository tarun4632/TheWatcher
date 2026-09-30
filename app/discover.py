"""Find where a company's jobs really are, starting from any careers URL.

Runs once when a company is added (and again if its recipe stops working), so the
30-minute checks just follow the saved recipe. Steps, cheapest first:

1. The URL itself is a known job board (Greenhouse, Lever, Ashby, SmartRecruiters, Workday).
2. The page embeds one, or is an Eightfold career site.
3. Follow links like "See open jobs" or "Search jobs", up to two clicks, checking each page.
4. Guess the board name (e.g. greenhouse.io/databricks) and confirm it: the board's company
   name matches, or its job titles/ids appear on the pages we saw.
5. Open the pages in a headless browser (needs Playwright) and watch the JSON they load:
   either it reveals a job board (step 2 again), or it *is* the job list, and we save that
   request to replay.
6. Plain job links on the page (the old generic reader), as a last resort.

Sites whose jobs live on LinkedIn or Naukri aren't read: those sites don't allow it.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from . import config, scrapers

# Links that lead to a list of jobs (not to one job, and not to navigation).
LIST_LINK_TEXT = re.compile(
    r"(open (positions|jobs|roles)|see (all |open )?(jobs|roles|positions|openings)|view (all |open )?(jobs|roles|"
    r"positions|openings)|search (all )?jobs|job search|all jobs|job openings|current openings|explore (jobs|roles|"
    r"opportunities)|find (a |your )?(job|role)|browse (jobs|roles)|career opportunities|job opportunities|"
    r"apply now|join us|work with us)", re.I)
LIST_LINK_HREF = re.compile(r"(open-?positions|/jobs/?$|/jobs\?|job-?search|/careers/search|/search-?jobs|/openings|"
                            r"/opportunities|/vacancies|/positions/?$)", re.I)
UNSUPPORTED = {
    "linkedin.com": "This company lists its jobs on LinkedIn, which doesn't allow automated reading. "
                    "Use LinkedIn's own job alerts for it.",
    "naukri.com": "This company lists its jobs on Naukri, which doesn't allow automated reading. "
                  "Use Naukri's own job alerts for it.",
}
GH_ID = re.compile(r"(?:gh_jid=|Greenhouse__Job__|\"gh_Id\"\s*:\s*)(\d{6,})")
OFFSET_PARAMS = ("from", "start", "offset", "skip", "startindex", "startIndex")
PAGE_PARAMS = ("page", "pagenumber", "pageNumber", "pageNo", "p")
SIZE_PARAMS = ("size", "num", "limit", "rows", "count", "per_page", "pageSize", "pagesize", "hitsPerPage")


def _page_key(url: str) -> str:
    """Tracking and filter parameters don't make a different page."""
    return url.split("?")[0].split("#")[0].rstrip("/")


class Found(Exception):
    """Raised inside discovery as soon as a recipe is found."""

    def __init__(self, source: str, key: str = "", recipe: dict | None = None):
        super().__init__(source)
        self.result = {"source": source, "source_key": key, "recipe": recipe or {}}


class Discovery:
    def __init__(self, url: str, name: str):
        self.url, self.name = url, name
        self.steps: list[str] = []          # what was tried, shown on the dashboard
        self.seen_text: list[str] = []      # page HTML and JSON seen, to confirm guessed boards
        self.visited: set[str] = set()
        self.unsupported: str | None = None
        self.best_links_page: tuple[int, str, str] | None = None  # (job links, url, html)
        self.client = scrapers._client()

    def log(self, message: str):
        self.steps.append(message)

    # --- helpers -----------------------------------------------------------------
    def fetch(self, url: str) -> str | None:
        try:
            r = scrapers._get(self.client, url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            self.log(f"Couldn't open {url}: {e}")
            return None
        self.seen_text.append(r.text[:3_000_000])
        return r.text

    def check_url(self, url: str):
        source, key = scrapers.detect_source(url)
        if source != "generic":
            self.log(f"{url} is a {source} job board")
            raise Found(source, key)
        host = urlparse(url).netloc.lower()
        for domain, reason in UNSUPPORTED.items():
            if host.endswith(domain) and "/jobs" in url:
                self.unsupported = reason

    def check_html(self, url: str, page: str):
        embedded = scrapers.detect_embedded(page)
        if embedded:
            self.log(f"{url} embeds a {embedded[0]} job board ({embedded[1]})")
            raise Found(*embedded)
        if "eightfold" in page.lower() or "vscdn.net" in page:
            host = urlparse(url).netloc
            for domain in self._eightfold_domains(page, host):
                try:
                    r = scrapers._get(self.client, f"https://{host}/api/apply/v2/jobs",
                                      params={"domain": domain, "start": 0, "num": 1})
                    if r.status_code == 200 and r.json().get("count"):
                        self.log(f"{url} is an Eightfold career site ({r.json()['count']} jobs)")
                        raise Found("eightfold", f"{host}|{domain}")
                except (httpx.HTTPError, ValueError):
                    continue
        links = scrapers._generic_from_html(url, page)
        if not self.best_links_page or len(links) > self.best_links_page[0]:
            self.best_links_page = (len(links), url, page)

    def _eightfold_domains(self, page: str, host: str) -> list[str]:
        found = re.findall(r'["\']domain["\']\s*:\s*["\']([a-z0-9.-]+\.[a-z]{2,})["\']', page)
        parts = host.split(".")
        guesses = [".".join(parts[-2:])] + [f"{p}.com" for p in parts if p not in ("www", "jobs", "explore", "careers")]
        return list(dict.fromkeys(found + guesses))

    def list_links(self, url: str, page: str) -> list[str]:
        """Links on a page that probably lead to the job list, best first."""
        soup = BeautifulSoup(page, "html.parser")
        scored = []
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"].strip())
            if not href.startswith("http") or href.split("#")[0].rstrip("/") == url.rstrip("/"):
                continue
            text = " ".join(a.get_text(" ").split())
            score = 2 * bool(LIST_LINK_TEXT.search(text)) + bool(LIST_LINK_HREF.search(href))
            if scrapers.detect_source(href)[0] != "generic":
                score += 3
            if score:
                scored.append((score, href))
        out = []
        for _, href in sorted(scored, key=lambda x: -x[0]):
            if href not in out:
                out.append(href)
        return out[:4]

    # --- steps -------------------------------------------------------------------
    def visit(self, url: str, depth: int):
        """Steps 1-3 for one page, then follow its job-list links."""
        if _page_key(url) in {_page_key(u) for u in self.visited} or len(self.visited) >= 8:
            return
        self.visited.add(url)
        self.check_url(url)
        page = self.fetch(url)
        if page is None:
            return
        self.check_html(url, page)
        if depth < 2:
            for link in self.list_links(url, page):
                self.check_url(link)
                if urlparse(link).netloc.lower().endswith(tuple(UNSUPPORTED)) or _page_key(link) in {
                        _page_key(u) for u in self.visited}:
                    continue
                self.log(f"Following '{link}'")
                self.visit(link, depth + 1)

    def slugs(self) -> list[str]:
        host = urlparse(self.url).netloc.lower()
        parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "com", "co", "in", "io", "net",
                                                         "org", "explore", "ai")]
        name = re.sub(r"[^a-z0-9 ]", "", self.name.lower())
        cands = [name.replace(" ", ""), name.replace(" ", "-")] + parts
        return [c for c in dict.fromkeys(cands) if len(c) >= 2]

    def guess_boards(self):
        """Step 4: try the company's name as a board name on the common job boards, and keep it
        only if something confirms it's really this company's board."""
        seen = "\n".join(self.seen_text)
        gh_ids = set(GH_ID.findall(seen))
        want = re.sub(r"[^a-z0-9]", "", self.name.lower())
        for slug in self.slugs():
            try:
                r = scrapers._get(self.client, f"https://boards-api.greenhouse.io/v1/boards/{slug}")
                if r.status_code == 200:
                    board = re.sub(r"[^a-z0-9]", "", (r.json().get("name") or "").lower())
                    ids = {str(j["id"]) for j in scrapers._get(
                        self.client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs").json().get("jobs", [])}
                    if (gh_ids and gh_ids & ids) or (board and (board == want or want in board or board in want)):
                        self.log(f"Found the Greenhouse board '{slug}' ({len(ids)} jobs"
                                 + (", job ids match the site)" if gh_ids & ids else ", name matches)"))
                        raise Found("greenhouse", slug)
            except (httpx.HTTPError, ValueError):
                pass
            for source, url in (("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
                                ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}")):
                try:
                    r = scrapers._get(self.client, url)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    titles = [j.get("text") or j.get("title") or "" for j in (data if isinstance(data, list)
                                                                             else data.get("jobs", []))]
                    confirmed = [t for t in titles if len(t) > 8 and t in seen]
                    if confirmed:
                        self.log(f"Found the {source} board '{slug}' (job titles match the site)")
                        raise Found(source, slug)
                except (httpx.HTTPError, ValueError):
                    pass

    def browse(self):
        """Step 5: render pages in a headless browser and look at the JSON they load."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log("Skipped the headless browser: Playwright isn't installed "
                     "(pip install playwright, then playwright install chromium)")
            return
        pages = [self.url] + [u for u in self.visited if u != self.url]
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                i = 0
                while i < len(pages) and i < 5:
                    url = pages[i]
                    i += 1
                    captured = self._render(browser, url)
                    if captured is None:
                        continue
                    html, responses = captured
                    self.seen_text.append(html)
                    self.check_html(url, html)
                    for resp in responses:
                        text = json.dumps(resp["json"])
                        self.seen_text.append(text[:3_000_000])
                        embedded = scrapers.detect_embedded(text)
                        if embedded:
                            self.log(f"{url} loads jobs from a {embedded[0]} board ({embedded[1]})")
                            raise Found(*embedded)
                    self.guess_boards()
                    feed = self.feed_from(responses)
                    if feed:
                        raise Found("json", "", {"feed": feed})
                    for link in self.list_links(url, html):  # rendered pages have links plain HTML didn't
                        self.check_url(link)
                        if link not in pages and not urlparse(link).netloc.lower().endswith(tuple(UNSUPPORTED)):
                            self.log(f"Following '{link}' (found after rendering)")
                            pages.append(link)
            finally:
                browser.close()

    def _render(self, browser, url: str):
        responses = []
        page = browser.new_page(user_agent=scrapers.HEADERS["User-Agent"])

        def on_response(r):
            if r.request.resource_type not in ("xhr", "fetch") or "json" not in r.headers.get("content-type", ""):
                return
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                return
            responses.append({"method": r.request.method, "url": r.url, "body": r.request.post_data, "json": data})

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(7000)
            html = page.content()
        except Exception as e:  # noqa: BLE001
            self.log(f"The headless browser couldn't open {url}: {e}")
            return None
        finally:
            page.close()
        self.log(f"Rendered {url} in a headless browser ({len(responses)} JSON responses)")
        return html, responses

    def feed_from(self, responses: list[dict]) -> dict | None:
        """The captured response that is a job list, as a request we can replay without a browser."""
        best = None
        for resp in responses:
            found = scrapers.find_job_list(resp["json"])
            if found and (not best or len(found[1]) > len(best[1][1])):
                best = (resp, found)
        if not best:
            return None
        resp, (path, items) = best
        fields = scrapers.map_job_fields(items)
        parsed = urlparse(resp["url"])
        body = None
        if resp["body"]:
            try:
                body = json.loads(resp["body"])
            except ValueError:
                return None  # form-encoded bodies aren't replayed
        feed = {"method": resp["method"], "url": urlunparse(parsed._replace(query="")),
                "params": {k: v[0] for k, v in parse_qs(parsed.query).items()}, "body": body,
                "list_path": list(path), "fields": fields, "base_url": self.url, "page": None}
        try:
            first = self._replay(feed, 0)
        except httpx.HTTPError as e:
            self.log(f"Found a job list at {resp['url']}, but it can't be read outside a browser ({e})")
            return None
        if not first:
            self.log(f"Found a job list at {resp['url']}, but it came back empty outside a browser")
            return None
        feed["page"] = self._paging(feed, first)
        feed, note = stable_order(feed, self.client)
        if note:
            self.log(note)
        self.log(f"Found the job list the page loads: {feed['method']} {feed['url']} ({len(items)} jobs a page, "
                 f"titles from '{fields['title']}', locations from '{fields.get('location')}'"
                 + (f", paged by '{feed['page']['param']}'" if feed["page"] else ", one page") + ")")
        return feed

    def _replay(self, feed: dict, page_no: int) -> list[dict]:
        r = scrapers.request_page(self.client, feed, page_no)
        r.raise_for_status()
        try:
            items = scrapers._at_path(r.json(), feed["list_path"])
        except (KeyError, IndexError, TypeError, ValueError):
            return []
        return scrapers.jobs_from_items(items or [], feed["fields"], feed["base_url"])

    def _paging(self, feed: dict, first: list[dict]) -> dict | None:
        """Work out how to ask for the next page, and only trust it if page 2 really differs."""
        body, params = feed.get("body"), feed.get("params") or {}
        size = len(first)
        candidates = []
        for where, target in (("body", body if isinstance(body, dict) else None), ("query", params)):
            if target is None:
                continue
            size_param = next((k for k in SIZE_PARAMS if k in target), None)
            for k in OFFSET_PARAMS:
                if k in target:
                    candidates.append({"where": where, "param": k, "start": int(target[k] or 0), "step": size})
            for k in PAGE_PARAMS:
                if k in target:
                    candidates.append({"where": where, "param": k, "start": int(target[k] or 1), "step": 1})
            if size_param and not any(c["where"] == where for c in candidates):
                # Search services often take "from" even when the page didn't send it (e.g. IBM).
                for k in ("from", "start", "offset"):
                    candidates.append({"where": where, "param": k, "start": 0, "step": size})
            for c in candidates:
                if c["where"] == where and size_param:
                    c["size_param"], c["size"] = size_param, int(target[size_param])
        ids = {j["external_id"] for j in first}
        for c in candidates:
            try:
                second = self._replay({**feed, "page": c}, 1)
            except httpx.HTTPError:
                continue
            if second and not ({j["external_id"] for j in second} <= ids):
                bigger = self._bigger_pages(feed, c)
                return bigger or c
        return None

    def _bigger_pages(self, feed: dict, paging: dict) -> dict | None:
        """Ask for 100 jobs a page if the site allows it: fewer requests every check."""
        if not paging.get("size_param") or paging.get("size", 0) >= 100:
            return None
        trial = {**paging, "size": 100, "step": 100 if paging["step"] != 1 else 1}
        try:
            jobs = self._replay({**feed, "page": trial}, 0)
        except httpx.HTTPError:
            return None
        return trial if len(jobs) > paging["size"] else None

    def run(self) -> dict:
        try:
            self.visit(self.url, 0)
            self.guess_boards()
            if not self.unsupported:  # no point rendering pages that only point to LinkedIn
                self.browse()
        except Found as f:
            f.result["recipe"]["steps"] = self.steps
            return f.result
        finally:
            self.client.close()
        if self.unsupported:
            self.log(self.unsupported)
            return {"source": "unsupported", "source_key": "", "recipe": {"reason": self.unsupported,
                                                                          "steps": self.steps}}
        if self.best_links_page and self.best_links_page[0] >= 3:
            n, url, _ = self.best_links_page
            self.log(f"No job board or job feed found; reading the {n} job links on {url}")
            return {"source": "generic", "source_key": "", "recipe": {"list_url": url, "steps": self.steps}}
        self.log("Couldn't find this company's job list")
        return {"source": "generic", "source_key": "", "recipe": {"list_url": self.url, "steps": self.steps,
                                                                  "not_found": True}}


STABLE_SORTS = ([{"_id": "asc"}], [{"_doc": "asc"}])  # Elasticsearch-style search services (e.g. IBM)


def stable_order(feed: dict, client: httpx.Client | None = None) -> tuple[dict, str | None]:
    """Ask a paged search for a fixed order when it accepts one. Sorted by relevance or popularity,
    results shift between requests, so paging skips some jobs and repeats others (IBM: two full
    reads differed by 124 jobs; sorted by id they were identical). Returns (feed, log note)."""
    feed = {**feed, "order_checked": True}
    body = feed.get("body")
    if not (feed.get("page") and isinstance(body, dict) and ("sort" in body or "query" in body)):
        return feed, None
    own = client or scrapers._client()
    try:
        for sort in STABLE_SORTS:
            trial = {**feed, "body": {**body, "sort": sort}}
            try:
                first = [j["external_id"] for j in _read_page(own, trial, 0)]
                second = [j["external_id"] for j in _read_page(own, trial, 1)]
            except httpx.HTTPError:
                continue
            if first and second and not set(second) <= set(first):
                return trial, f"Asking the job list for a fixed order ({json.dumps(sort)}), so paging doesn't skip jobs"
    finally:
        if client is None:
            own.close()
    return feed, None


def _read_page(client: httpx.Client, feed: dict, page_no: int) -> list[dict]:
    r = scrapers.request_page(client, feed, page_no)
    r.raise_for_status()
    try:
        items = scrapers._at_path(r.json(), feed["list_path"])
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    return scrapers.jobs_from_items(items or [], feed["fields"], feed["base_url"])


def discover(url: str, name: str) -> dict:
    """{"source", "source_key", "recipe"} for a careers URL. recipe["steps"] explains how."""
    result = Discovery(url, name).run()
    result["recipe"]["discovered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result
