"""Rebuild app/default_companies.json from fresh public data.

Uses the daily snapshot published by the job-board-aggregator project
(github.com/Feashliaa/job-board-aggregator, data licensed CC BY-NC 4.0, so
personal, non-commercial use only). It finds which companies on Greenhouse,
Lever, Ashby and Workday currently have openings in India and keeps the ones
with the most India openings.

    python tools/build_starter_list.py                 # download and rebuild
    python tools/build_starter_list.py --per-board 40  # bigger list
    python tools/build_starter_list.py --keep-current  # refresh counts, keep the same companies

The download is about 75 MB. Companies you've already added in the app are
never removed; newly listed ones appear after "Add the starter list" or on a
fresh database.
"""
import argparse
import collections
import datetime as dt
import gzip
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.area import INDIA  # noqa: E402

DATA_ZIP = "https://codeload.github.com/Feashliaa/job-board-data/zip/refs/heads/main"
CODE_ZIP = "https://codeload.github.com/Feashliaa/job-board-aggregator/zip/refs/heads/main"
OUT = Path(__file__).resolve().parent.parent / "app" / "default_companies.json"

# Job aggregators, staffing firms and internal-only boards: not real employers to watch.
EXCLUDE = {"jobgether", "weekdayworks", "smart-working-solutions", "agency", "pragmatike", "bjakcareer",
           "coupanginternal", "thoughtworksreferral", "alphasenseindia"}
BAD_WORKDAY_SITE = re.compile(r"nonpublic|confiden|manual|internal|employee|only_|referral", re.I)
CITY = re.compile(r"(bengaluru|bangalore|mumbai|pune|hyderabad|chennai|gurugram|gurgaon|noida|delhi|remote)", re.I)


def download(url: str) -> zipfile.ZipFile:
    print(f"Downloading {url} ...", flush=True)
    with httpx.Client(timeout=300, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(r.content))


def board_key(job: dict, greenhouse_slugs: set):
    u = urlparse(job["url"])
    parts = [p for p in u.path.split("/") if p]
    ats = job.get("ats")
    if ats == "Lever" and u.netloc.endswith("lever.co") and parts:
        return "lever", parts[0]
    if ats == "Ashby" and "ashbyhq.com" in u.netloc and parts:
        return "ashby", parts[0]
    if ats == "Workday" and "myworkdayjobs.com" in u.netloc:
        site = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if site and not BAD_WORKDAY_SITE.search(site[0]):
            return "workday", f"{u.netloc}|{u.netloc.split('.')[0]}|{site[0]}"
    if ats == "Greenhouse":
        if "greenhouse.io" in u.netloc:
            qs = parse_qs(u.query)
            if "for" in qs:
                return "greenhouse", qs["for"][0]
            if parts and parts[0] != "embed":
                return "greenhouse", parts[0]
        slug = re.sub(r"[^a-z0-9]", "", (job.get("company") or "").lower())
        if slug in greenhouse_slugs:
            return "greenhouse", slug
    return None


def board_url(source: str, key: str) -> str:
    if source == "workday":
        host, _tenant, site = key.split("|")
        return f"https://{host}/{site}"
    return {"greenhouse": f"https://job-boards.greenhouse.io/{key}", "lever": f"https://jobs.lever.co/{key}",
            "ashby": f"https://jobs.ashbyhq.com/{key}"}[source]


def pretty(name: str) -> str:
    return name if any(ch.isupper() for ch in name) else name.replace("-", " ").title()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-board", type=int, default=25, help="companies to keep per job board (default 25)")
    ap.add_argument("--keep-current", action="store_true", help="keep today's companies, only refresh their numbers")
    ap.add_argument("--data-dir", help="use an already-downloaded job-board-data folder instead of downloading")
    args = ap.parse_args()

    code = download(CODE_ZIP)
    gh_name = next(n for n in code.namelist() if n.endswith("data/greenhouse_companies.json"))
    greenhouse_slugs = set(json.loads(code.read(gh_name)))

    if args.data_dir:
        chunks = [p.read_bytes() for p in Path(args.data_dir).glob("**/jobs_chunk_*.json.gz")]
        snapshot = dt.date.today().isoformat()
    else:
        data = download(DATA_ZIP)
        chunks = [data.read(n) for n in data.namelist() if re.search(r"jobs_chunk_\d+\.json\.gz$", n)]
        meta = next((n for n in data.namelist() if n.endswith("data/metadata.json")), None)
        snapshot = json.loads(data.read(meta))["last_updated"][:10] if meta else dt.date.today().isoformat()

    boards: dict = {}
    for raw in chunks:
        for job in json.loads(gzip.decompress(raw)):
            if not INDIA.search(job.get("location") or ""):
                continue
            key = board_key(job, greenhouse_slugs)
            if not key or key[1].split("|")[0] in EXCLUDE:
                continue
            b = boards.setdefault(key, {"names": collections.Counter(), "india": 0, "entry": 0, "recruiter": 0,
                                        "cities": collections.Counter()})
            b["recruiter"] += bool(job.get("is_recruiter"))
            b["names"][job.get("company") or key[1]] += 1
            b["india"] += 1
            b["entry"] += job.get("skill_level") in ("entry", "intern")
            m = CITY.search(job["location"])
            if m:
                city = m.group(1).lower().replace("bangalore", "bengaluru").replace("gurgaon", "gurugram")
                b["cities"][city] += 1
    print(f"Found {len(boards)} job boards with India openings", flush=True)

    current = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"companies": []}
    by_key = {(c["source"], c["source_key"]): c for c in current["companies"]}

    if args.keep_current:
        chosen = list(by_key)  # companies with no India openings today keep a count of 0
    else:
        chosen = []
        # The dataset's "recruiter" flag has false positives, so it only screens new picks:
        # boards that are mostly flagged as agency postings are skipped.
        for source in ("greenhouse", "lever", "ashby", "workday"):
            ranked = sorted((k for k in boards if k[0] == source and boards[k]["recruiter"] * 2 < boards[k]["india"]),
                            key=lambda k: -boards[k]["india"])
            if source == "workday":  # one board per employer
                seen, uniq = set(), []
                for k in ranked:
                    tenant = k[1].split("|")[1]
                    if tenant not in seen:
                        seen.add(tenant)
                        uniq.append(k)
                ranked = uniq
            chosen += ranked[: args.per_board]

    companies = []
    empty = {"names": collections.Counter(), "india": 0, "entry": 0, "cities": collections.Counter()}
    for k in chosen:
        b, old = boards.get(k, empty), by_key.get(k, {})
        companies.append({
            "name": old.get("name") or pretty((b["names"].most_common(1) or [(k[1].split("|")[0], 0)])[0][0]),
            "category": old.get("category") or "More companies hiring in India",
            "source": k[0], "source_key": k[1], "url": board_url(*k),
            "india_jobs": b["india"], "entry_level_india_jobs": b["entry"],
            "top_cities": [c for c, _ in b["cities"].most_common(3)],
        })
    doc = {"snapshot_date": snapshot,
           "source": "Derived from the job-board-aggregator dataset by Riley Dorrington "
                     "(github.com/Feashliaa/job-board-aggregator), CC BY-NC 4.0. Personal, non-commercial use.",
           "companies": companies}
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(companies)} companies to {OUT}")


if __name__ == "__main__":
    main()
