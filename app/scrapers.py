"""Read job listings from a company's careers page.

Most companies host jobs on an applicant-tracking system (ATS) with a public JSON
feed, which is far more reliable than scraping HTML. We detect the ATS from the
URL (or from an embed inside the company's own careers page) and fall back to
reading job-like links from the page when there is no feed.

Every reader returns a list of dicts:
    {"external_id", "title", "location", "url", "description", "posted_at"}
`description` may be empty; `fetch_description()` fills it in later, only for
jobs we have not seen before. `posted_at` is an ISO timestamp when the board
provides one, or "" when the age is unknown.
"""
import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from . import config, ratelimit

TIMEOUT = httpx.Timeout(25.0, connect=10.0)
HEADERS = {"User-Agent": config.USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
MAX_DESC_CHARS = 12000


def parse_workday_posted_on(text: str, now: datetime | None = None) -> datetime | None:
    """Workday list rows use phrases like 'Posted Today' or 'Posted 30+ Days Ago'."""
    raw = (text or "").strip()
    if not raw:
        return None
    now = now or datetime.now(timezone.utc)
    lower = raw.lower()
    if "today" in lower:
        return now
    if "yesterday" in lower:
        return now - timedelta(days=1)
    plus = re.search(r"(\d+)\s*\+\s*days?", lower)
    if plus:
        return now - timedelta(days=int(plus.group(1)) + 1)
    days = re.search(r"(\d+)\s*days?", lower)
    if days:
        return now - timedelta(days=int(days.group(1)))
    return None


def parse_posted_at(value, now: datetime | None = None) -> datetime | None:
    """Turn a board timestamp into an aware datetime. Unknown values stay None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    text = str(value).strip()
    if re.search(r"posted|yesterday|\bdays?\b", text, re.I):
        parsed = parse_workday_posted_on(text, now)
        if parsed:
            return parsed
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def posted_iso(value, now: datetime | None = None) -> str:
    dt = parse_posted_at(value, now)
    return dt.isoformat() if dt else ""


def is_recent(posted_at, now: datetime | None = None) -> bool:
    """Keep jobs with no date. Drop jobs posted more than MAX_JOB_AGE_DAYS ago."""
    dt = posted_at if isinstance(posted_at, datetime) else parse_posted_at(posted_at, now)
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return dt >= now - timedelta(days=config.MAX_JOB_AGE_DAYS)


def recent_only(jobs: list[dict], now: datetime | None = None) -> list[dict]:
    return [j for j in jobs if is_recent(j.get("posted_at"), now)]


def page_is_all_old(jobs: list[dict], now: datetime | None = None) -> bool:
    """True when every row on a page has a date and all of them are past the window."""
    if not jobs:
        return False
    dates = [j.get("posted_at") for j in jobs]
    if any(not d for d in dates):
        return False
    return all(not is_recent(d, now) for d in dates)


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)


# Every request to a careers site goes through the shared per-host rate limit and retry policy.
def _get(c: httpx.Client, url: str, **kwargs) -> httpx.Response:
    return ratelimit.send(c, "GET", url, **kwargs)


def _post(c: httpx.Client, url: str, **kwargs) -> httpx.Response:
    return ratelimit.send(c, "POST", url, **kwargs)


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_DESC_CHARS]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def detect_source(url: str) -> tuple[str, str]:
    """Return (source, key) from a careers URL."""
    p = urlparse(url if "://" in url else "https://" + url)
    host, parts = p.netloc.lower(), [s for s in p.path.split("/") if s]

    if "greenhouse.io" in host:
        qs = parse_qs(p.query)
        if "for" in qs:  # boards.greenhouse.io/embed/job_board?for=acme
            return "greenhouse", qs["for"][0]
        if parts and parts[0] not in ("embed", "v1"):
            return "greenhouse", parts[0]
    if host.endswith("lever.co") and parts:
        return "lever", parts[0]
    if host.endswith("ashbyhq.com") and parts:
        return "ashby", parts[0]
    if "smartrecruiters.com" in host and parts:
        return "smartrecruiters", parts[0]
    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        site_parts = [s for s in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
        if site_parts:
            return "workday", f"{host}|{tenant}|{site_parts[0]}"
    return "generic", ""


EMBED_PATTERNS = [
    (re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)([\w-]+)"), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([\w-]+)"), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([\w.-]+)"), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"), "ashby"),
    (re.compile(r"jobs\.smartrecruiters\.com/([\w-]+)"), "smartrecruiters"),
]


def detect_embedded(page_html: str) -> tuple[str, str] | None:
    """Many careers pages embed an ATS board. Find it so we can use the JSON feed."""
    for pattern, source in EMBED_PATTERNS:
        m = pattern.search(page_html)
        if m and m.group(1).lower() not in ("embed", "js", "v1"):
            return source, m.group(1)
    m = re.search(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", page_html)
    if m:
        host = f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com"
        return "workday", f"{host}|{m.group(1)}|{m.group(3)}"
    return None


# ---------------------------------------------------------------------------
# ATS readers
# ---------------------------------------------------------------------------
def _greenhouse(key: str) -> list[dict]:
    # List ids only. Full HTML is fetched later, and only for jobs we will score.
    with _client() as c:
        r = _get(c, f"https://boards-api.greenhouse.io/v1/boards/{key}/jobs")
        r.raise_for_status()
    return recent_only([
        {
            "external_id": str(j["id"]),
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "description": "",
            "posted_at": posted_iso(j.get("updated_at")),
        }
        for j in r.json().get("jobs", [])
    ])


def _lever(key: str) -> list[dict]:
    with _client() as c:
        r = _get(c, f"https://api.lever.co/v0/postings/{key}", params={"mode": "json"})
        r.raise_for_status()
    jobs = []
    for j in r.json():
        parts = [j.get("descriptionPlain", "")]
        for section in j.get("lists", []):
            parts.append(section.get("text", ""))
            parts.append(html_to_text(section.get("content", "")))
        parts.append(j.get("additionalPlain", ""))
        cats = j.get("categories") or {}
        jobs.append({
            "external_id": j["id"],
            "title": j.get("text", "").strip(),
            "location": cats.get("location") or ", ".join(cats.get("allLocations", []) or []),
            "url": j.get("hostedUrl", ""),
            "description": "\n".join(p for p in parts if p)[:MAX_DESC_CHARS],
            "posted_at": posted_iso(j.get("createdAt")),
        })
    return recent_only(jobs)


def _ashby(key: str) -> list[dict]:
    with _client() as c:
        r = _get(c, f"https://api.ashbyhq.com/posting-api/job-board/{key}")
        r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "")
        if j.get("isRemote"):
            loc = f"{loc} (remote)".strip()
        jobs.append({
            "external_id": j["id"],
            "title": j.get("title", "").strip(),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "description": (j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")))[:MAX_DESC_CHARS],
            "posted_at": posted_iso(j.get("publishedAt")),
        })
    return recent_only(jobs)


def _smartrecruiters(key: str) -> list[dict]:
    jobs, offset = [], 0
    with _client() as c:
        while True:
            r = _get(c, f"https://api.smartrecruiters.com/v1/companies/{key}/postings",
                      params={"limit": 100, "offset": offset})
            r.raise_for_status()
            data = r.json()
            page = []
            for j in data.get("content", []):
                loc = j.get("location") or {}
                page.append({
                    "external_id": str(j["id"]),
                    "title": j.get("name", "").strip(),
                    "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
                                + (" (remote)" if loc.get("remote") else ""),
                    "url": f"https://jobs.smartrecruiters.com/{key}/{j['id']}",
                    "description": "",  # filled by fetch_description
                    "posted_at": posted_iso(j.get("releasedDate")),
                })
            if page_is_all_old(page):
                break
            jobs.extend(recent_only(page))
            offset += 100
            total = data.get("totalFound")
            if (not page or len(page) < 100 or offset >= 5000
                    or (total is not None and offset >= total)):
                break
    return jobs


def _find_country_facet(facets: list, country: str = "india") -> tuple[str, str] | None:
    """Workday returns filter options ("facets") with each board's job list. Find the
    option for a country, which may be nested inside a "locations" group."""
    fallback = None

    def walk(items, param):
        nonlocal fallback
        for f in items or []:
            p = f.get("facetParameter") or param
            for v in f.get("values") or []:
                if v.get("values"):
                    found = walk([v], v.get("facetParameter") or p)
                    if found:
                        return found
                elif (v.get("descriptor") or "").strip().lower() == country and v.get("id") and p:
                    if "country" in p.lower():
                        return p, v["id"]
                    fallback = fallback or (p, v["id"])
        return None

    return walk(facets, None) or fallback


def _workday(key: str, india_only: bool = False) -> list[dict]:
    host, tenant, site = key.split("|")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, offset, applied, total = [], 0, {}, 0
    with _client() as c:
        if india_only:
            # Ask Workday for India jobs only so a global board does not hide them.
            try:
                first = _post(c, api, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
                first.raise_for_status()
                facet = _find_country_facet(first.json().get("facets") or [])
                if facet:
                    applied = {facet[0]: [facet[1]]}
            except (httpx.HTTPError, ValueError):
                applied = {}
        while True:
            try:
                r = _post(c, api, json={"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""})
                r.raise_for_status()
            except httpx.HTTPStatusError:
                if applied and offset == 0:  # the filter was rejected; read unfiltered instead
                    applied = {}
                    continue
                raise
            data = r.json()
            postings = data.get("jobPostings", [])
            page = []
            for j in postings:
                path = j.get("externalPath", "")
                page.append({
                    "external_id": path or j.get("title", ""),
                    "title": j.get("title", "").strip(),
                    "location": j.get("locationsText", ""),
                    "url": f"https://{host}/{site}{path}",
                    "description": "",
                    "posted_at": posted_iso(j.get("postedOn")),
                })
            if page_is_all_old(page):
                break
            jobs.extend(recent_only(page))
            if data.get("total") is not None:
                total = data["total"]
            offset += 20
            # offset guard stops a feed that never reports an end. It is far above
            # the old 400-job cutoff, so a recent job is not dropped early.
            if not postings or (total and offset >= total) or len(postings) < 20 or offset >= 5000:
                break
    return jobs


# ---------------------------------------------------------------------------
# Generic careers pages
# ---------------------------------------------------------------------------
JOB_HREF = re.compile(
    r"(/jobs?/|/careers?/.+|/positions?/|/openings?/|/vacanc|/requisition|/opportunit|gh_jid=|/apply/|jobid=|job_id=)",
    re.I,
)
NAV_TEXT = re.compile(
    r"^(jobs?|careers?|all jobs|view all.*|see all.*|open (roles|positions)|apply( now)?|learn more.*|read more.*|"
    r"life at .*|benefits|our (team|culture|values)|students?|internships?|search.*|home|back|next|previous|"
    r"privacy.*|cookie.*|log ?in|sign ?in|sign ?up|join us|join our team|explore.*|faq|discover.*|"
    r"see (your|more|our|the) .*|find (out|more).*|turn on .*|get (job )?alerts.*|subscribe.*|watch .*|meet .*|"
    r"hear from .*|stories.*|blog.*|news.*|events?)$",
    re.I,
)
# Links on a careers page that are never job postings.
NOT_JOB_HREF = re.compile(r"(/blog|/stories|/story/|/news|/events?/|/press|/search\b|jobalerts?|/talent-?community|"
                          r"/faq|/benefits|/culture|/life-at|/alumni)", re.I)


def _fetch_html(url: str) -> str:
    if config.USE_PLAYWRIGHT:
        try:
            from playwright.sync_api import sync_playwright
            ratelimit.acquire(httpx.URL(url).host)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent=config.USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=45000)
                content = page.content()
                browser.close()
                return content
        except ImportError:
            pass
    with _client() as c:
        r = _get(c, url)
        r.raise_for_status()
        return r.text


def _generic_from_html(page_url: str, page_html: str) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    seen, jobs = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"].strip())
        if not href.startswith("http") or href.rstrip("/") == page_url.rstrip("/"):
            continue
        text = " ".join(a.get_text(" ").split())
        if not (4 <= len(text) <= 140) or NAV_TEXT.match(text):
            continue
        if not JOB_HREF.search(href) or NOT_JOB_HREF.search(href):
            continue
        key = href.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        # A card often puts the location next to the title; grab nearby short text.
        location = ""
        parent = a.find_parent(["li", "tr", "article", "div"])
        if parent:
            bits = [b for b in parent.stripped_strings if b != text and len(b) < 60]
            location = bits[0] if bits else ""
        jobs.append({"external_id": key, "title": text, "location": location, "url": key,
                     "description": "", "posted_at": ""})
    return jobs


def _eightfold(key: str) -> list[dict]:
    """Eightfold career sites (e.g. explore.jobs.netflix.net). key = "host|domain".
    Newest first, so paging stops at the first page that is all past the age window."""
    host, domain = key.split("|")
    jobs, start = [], 0
    with _client() as c:
        while start < 3000:
            # Eightfold returns at most 10 jobs a request, whatever `num` asks for, so page by what comes back.
            r = _get(c, f"https://{host}/api/apply/v2/jobs",
                     params={"domain": domain, "start": start, "num": 100, "sort_by": "new"})
            r.raise_for_status()
            data = r.json()
            page = [{"external_id": str(p["id"]), "title": p.get("name") or p.get("posting_name") or "",
                     "location": p.get("location") or ", ".join(p.get("locations") or []),
                     "url": p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                     "description": html_to_text(p.get("job_description") or ""),
                     "posted_at": posted_iso(p.get("t_create"))}
                    for p in data.get("positions") or []]
            jobs += page
            start += len(page)
            if not page or page_is_all_old(page) or start >= (data.get("count") or 0):
                break
    return jobs


# ---------------------------------------------------------------------------
# Any JSON job list a careers page loads (found by discover.py, replayed here)
# ---------------------------------------------------------------------------
TITLE_KEYS = ("title", "name", "jobtitle", "job_title", "posting_name", "positiontitle", "text", "jobname")
URL_KEYS = ("url", "absolute_url", "applyurl", "canonicalpositionurl", "hostedurl", "joburl", "link", "href",
            "externalpath", "detailurl", "apply_url")
ID_KEYS = ("id", "jobid", "job_id", "requisitionid", "reqid", "_id", "slug", "uuid", "gh_id", "externalid")
LOC_KEYS = ("location", "locations", "city", "locationname", "joblocation", "location_name", "primarylocation",
            "locationstext", "office", "offices")
DATE_KEYS = ("posted", "postedat", "posted_at", "dateposted", "created_at", "createdat", "t_create", "publishedat",
             "published_at", "posteddate", "postingdate", "first_published", "updated_at", "updatedat")
DESC_KEYS = ("description", "job_description", "content", "descriptionplain", "summary")
_LOCATION_VALUE = re.compile(r"^[A-Z][\w .'()-]{1,40}(, ?[\w .'()-]{2,40}){1,3}$")


def _flatten(item: dict) -> dict:
    """{'a': 1, 'b': {'c': 2}} -> {'a': 1, 'b.c': 2} (one level down, which covers '_source.title')."""
    out = {}
    for k, v in item.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if not isinstance(v2, (dict, list)):
                    out[f"{k}.{k2}"] = v2
        else:
            out[k] = v
    return out


def _as_text(v) -> str:
    if isinstance(v, list):
        return ", ".join(_as_text(x) for x in v if x)[:200]
    if isinstance(v, dict):
        return str(v.get("name") or v.get("city") or v.get("label") or "")
    return "" if v is None else str(v)


def _pick_key(rows: list[dict], names: tuple, ok) -> str | None:
    """The flattened key whose last part is in `names` and whose value is `ok` for most rows."""
    keys = {}
    for row in rows:
        for k, v in row.items():
            if k.rsplit(".", 1)[-1].lower() in names and ok(v):
                keys[k] = keys.get(k, 0) + 1
    good = [(n, -names.index(k.rsplit(".", 1)[-1].lower()), k) for k, n in keys.items() if n >= 0.6 * len(rows)]
    return max(good)[2] if good else None


def map_job_fields(items: list) -> dict | None:
    """Which keys of these JSON objects hold the title, link, location...? None if they don't look like jobs."""
    rows = [_flatten(i) for i in items if isinstance(i, dict)]
    if len(rows) < 2:
        return None
    title = _pick_key(rows, TITLE_KEYS, lambda v: isinstance(v, str) and 3 <= len(v.strip()) <= 150)
    url = _pick_key(rows, URL_KEYS, lambda v: isinstance(v, str) and ("/" in v or v.startswith("http")))
    ident = _pick_key(rows, ID_KEYS, lambda v: isinstance(v, (str, int)) and str(v) != "")
    if not title or not (url or ident):
        return None
    fields = {"title": title, "url": url, "id": ident}
    fields["location"] = _pick_key(rows, LOC_KEYS, lambda v: bool(_as_text(v)))
    if not fields["location"]:  # e.g. IBM's "field_keyword_19": "Zurich, CH"
        counts = {}
        for row in rows:
            for k, v in row.items():
                if k not in fields.values() and isinstance(v, str) and _LOCATION_VALUE.match(v.strip()):
                    counts[k] = counts.get(k, 0) + 1
        best = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
        fields["location"] = best[0] if best[1] >= 0.6 * len(rows) else None
    fields["posted"] = _pick_key(rows, DATE_KEYS, lambda v: parse_posted_at(v) is not None)
    fields["description"] = _pick_key(rows, DESC_KEYS, lambda v: isinstance(v, str) and len(v) > 40)
    if not (fields["location"] or fields["posted"]):
        return None  # a menu or a list of articles also has titles and links; jobs have a place or a date
    return fields


def find_job_list(data, path=()) -> tuple[tuple, list] | None:
    """The largest list of job-like objects anywhere in a JSON document: (path, items)."""
    best = None
    if isinstance(data, list):
        if len(data) >= 2 and map_job_fields(data):
            best = (path, data)
        for i, v in enumerate(data[:3]):  # lists of lists are rare; peek only
            found = find_job_list(v, path + (i,))
            if found and (not best or len(found[1]) > len(best[1])):
                best = found
    elif isinstance(data, dict):
        for k, v in data.items():
            found = find_job_list(v, path + (k,))
            if found and (not best or len(found[1]) > len(best[1])):
                best = found
    return best


def _at_path(data, path):
    for p in path:
        data = data[p]
    return data


def jobs_from_items(items: list, fields: dict, base_url: str) -> list[dict]:
    jobs = []
    for raw in items:
        row = _flatten(raw) if isinstance(raw, dict) else {}
        title = _as_text(row.get(fields["title"])).strip()
        if not title:
            continue
        url = _as_text(row.get(fields["url"])) if fields.get("url") else ""
        url = urljoin(base_url, url) if url else ""
        ident = _as_text(row.get(fields["id"])) if fields.get("id") else ""
        jobs.append({
            "external_id": ident or url, "title": title, "url": url or base_url,
            "location": _as_text(row.get(fields["location"])) if fields.get("location") else "",
            "description": html_to_text(_as_text(row.get(fields["description"]))) if fields.get("description") else "",
            "posted_at": posted_iso(row.get(fields["posted"])) if fields.get("posted") else "",
        })
    return jobs


def request_page(c: httpx.Client, feed: dict, page_no: int) -> httpx.Response:
    """Send the saved request for page `page_no` (0-based)."""
    params, body = dict(feed.get("params") or {}), feed.get("body")
    body = json.loads(json.dumps(body)) if body is not None else None
    pg = feed.get("page")
    if pg:
        value = pg["start"] + page_no * pg["step"]
        target = body if pg["where"] == "body" else params
        target[pg["param"]] = value
        if pg.get("size_param"):
            target[pg["size_param"]] = pg["size"]
    kwargs = {"params": params} if params else {}
    if body is not None:
        kwargs["json"] = body
    return ratelimit.send(c, feed["method"], feed["url"], **kwargs)


MAX_FEED_PAGES = 40


def _json_feed(feed: dict) -> list[dict]:
    """Replay a job-list request a careers page makes (see discover.py), page by page."""
    jobs, seen = [], set()
    with _client() as c:
        for page_no in range(MAX_FEED_PAGES if feed.get("page") else 1):
            r = request_page(c, feed, page_no)
            r.raise_for_status()
            try:
                items = _at_path(r.json(), feed["list_path"]) or []
            except (KeyError, IndexError, TypeError, ValueError):
                items = []
            if not items:
                break
            page = [j for j in jobs_from_items(items, feed["fields"], feed["base_url"])
                    if j["external_id"] not in seen]
            seen.update(j["external_id"] for j in page)
            jobs += page
            # A page of repeats doesn't mean the end (a shifting order can cause it); a short page does.
            size = (feed.get("page") or {}).get("size")
            if (size and len(items) < size) or (page and page_is_all_old(page)):
                break
    return jobs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
READERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workday": _workday,
    "eightfold": _eightfold,
}


class Unsupported(RuntimeError):
    """A site whose jobs can't be read automatically (the message says why)."""


def fetch_jobs(company: dict, india_only: bool = False) -> tuple[list[dict], tuple[str, str] | None]:
    """Return (jobs, upgraded_source). `upgraded_source` is set when a generic page
    turned out to embed a known job board, so the caller can save it."""
    source, key = company["source"], company["source_key"]
    recipe = company.get("recipe") or {}
    if isinstance(recipe, str):
        recipe = json.loads(recipe or "{}")
    if source == "unsupported":
        raise Unsupported(recipe.get("reason") or "This site's jobs can't be read automatically.")
    if source == "json":
        return recent_only(_json_feed(recipe["feed"])), None
    if source == "workday":
        return recent_only(_workday(key, india_only)), None
    if source in READERS:
        return recent_only(READERS[source](key)), None

    list_url = recipe.get("list_url") or company["url"]  # the page discovery found the job links on
    page_html = _fetch_html(list_url)
    embedded = detect_embedded(page_html)
    if embedded:
        try:
            jobs = (_workday(embedded[1], india_only) if embedded[0] == "workday"
                    else READERS[embedded[0]](embedded[1]))
            # A successful feed with nothing recent is still the right source.
            # Falling through to HTML would treat undated links as new jobs.
            return recent_only(jobs), embedded
        except httpx.HTTPError:
            pass  # fall back to reading links
    return _generic_from_html(list_url, page_html), None


def fetch_description(company: dict, job: dict) -> str:
    """Fill in the description for readers that only list titles."""
    try:
        if company["source"] == "greenhouse":
            with _client() as c:
                r = _get(c, f"https://boards-api.greenhouse.io/v1/boards/{company['source_key']}"
                          f"/jobs/{job['external_id']}")
                r.raise_for_status()
            return html_to_text(r.json().get("content", ""))
        if company["source"] == "smartrecruiters":
            with _client() as c:
                r = _get(c, f"https://api.smartrecruiters.com/v1/companies/{company['source_key']}"
                          f"/postings/{job['external_id']}")
                r.raise_for_status()
            sections = (r.json().get("jobAd") or {}).get("sections") or {}
            return html_to_text("\n".join((s or {}).get("text", "") for s in sections.values()))
        if company["source"] == "workday":
            host, tenant, site = company["source_key"].split("|")
            with _client() as c:
                r = _get(c, f"https://{host}/wday/cxs/{tenant}/{site}{job['external_id']}")
                r.raise_for_status()
            info = r.json().get("jobPostingInfo") or {}
            return html_to_text(info.get("jobDescription", ""))
        if company["source"] == "eightfold":
            host, domain = company["source_key"].split("|")
            with _client() as c:
                r = _get(c, f"https://{host}/api/apply/v2/jobs/{job['external_id']}", params={"domain": domain})
                r.raise_for_status()
            return html_to_text(r.json().get("job_description", ""))
        # json feeds and generic pages: read the posting page itself
        page = _fetch_html(job["url"])
        soup = BeautifulSoup(page, "html.parser")
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return html_to_text(str(main))
    except Exception:  # noqa: BLE001 - a missing description should not stop the run
        return ""
