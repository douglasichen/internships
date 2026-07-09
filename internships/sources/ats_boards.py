"""Source: Ashby/Lever/Greenhouse/Workday job-board JSON APIs listed in
companies.csv. Ported from the old sweep.py, but instead of writing a raw
JSON snapshot to disk and diffing it against the last run, it extracts
Listings directly -- dedup now happens once, centrally, in service.py via
seen_store, so there's no more responses/ + reports/ snapshot tree.

companies.csv is still the config: one row per company, `api_urls` holds
';'-separated endpoints (optionally annotated "(dead)"/"(unverified)").
api_status is still written back so known-dead endpoints are skipped fast
on the next run.
"""
import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from internships.models import Listing

ROOT = Path(__file__).resolve().parent.parent.parent
CSV_PATH = ROOT / "companies.csv"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) internships-service"
TIMEOUT = 25
URL_RE = re.compile(r"https?://\S+")

TITLE_KEYS = ("title", "text", "name", "jobTitle", "job_title")
URL_KEYS = ("absolute_url", "hostedUrl", "jobUrl", "applyUrl", "externalPath",
            "url", "canonicalUrl")
LOC_KEYS = ("location", "city", "locationName", "primaryLocation")
BODY_KEYS = ("descriptionPlain", "descriptionBodyPlain", "content",
             "description", "openingPlain")
LIST_KEYS = ("jobs", "jobPostings", "postings", "data", "results")


def parse_urls(cell):
    out = []
    for part in cell.split(";"):
        m = URL_RE.search(part)
        if not m:
            continue
        url = m.group(0).rstrip(").,")
        note = re.search(r"\(([^)]+)\)", part[m.end():])
        out.append((url, note.group(1).strip().lower() if note else None))
    return out


def request_for(url):
    """Workday cxs and Uber want a POST body; everything else is a GET."""
    if "/wday/cxs/" in url:
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0,
                            "searchText": ""}).encode()
        return Request(url, data=body, method="POST",
                       headers={"User-Agent": UA, "Content-Type": "application/json",
                                "Accept": "application/json"})
    if "loadSearchJobsResults" in url:
        body = json.dumps({"limit": 10, "page": 1, "params": {}}).encode()
        return Request(url, data=body, method="POST",
                       headers={"User-Agent": UA, "Content-Type": "application/json",
                                "Accept": "application/json"})
    return Request(url, headers={"User-Agent": UA, "Accept": "application/json"})


def fetch_json(url):
    """Return (ok, parsed_json_or_None, note)."""
    try:
        with urlopen(request_for(url), timeout=TIMEOUT) as r:
            raw = r.read()
    except HTTPError as e:
        return False, None, f"http {e.code}"
    except (URLError, TimeoutError) as e:
        return False, None, f"err {getattr(e, 'reason', e)}"
    except Exception as e:  # noqa: BLE001 - any transport oddity = dead
        return False, None, f"err {e}"
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False, None, "not json"
    if _is_dead_response(data):
        return False, None, "empty"
    return True, data, "ok"


def _is_dead_response(data):
    """A syntactically valid empty list/dict is a live board with 0 current
    postings, not a dead endpoint -- only a genuinely missing/blank body
    should count as dead."""
    return data is None or data == ""


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _get_location(job):
    v = _first(job, LOC_KEYS)
    if v:
        return v
    loc = job.get("location")
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("locationName") or ""
    cats = job.get("categories")
    if isinstance(cats, dict) and cats.get("location"):
        return cats["location"]
    addr = job.get("address")
    if isinstance(addr, dict):
        pa = addr.get("postalAddress", {})
        if isinstance(pa, dict):
            return ", ".join(x for x in (pa.get("addressLocality", ""),
                                          pa.get("addressCountry", "")) if x)
        if isinstance(pa, str):
            return pa.strip()
    return ""


def _get_blob(job):
    return " ".join(v for k in BODY_KEYS if isinstance((v := job.get(k)), str))


def extract_postings(data):
    """Find the array of job dicts inside an arbitrary ATS JSON response.
    Covers every shape seen so far: Ashby/Greenhouse ({"jobs": [...]}),
    Workday ({"jobPostings": [...]}), Lever (bare list)."""
    if isinstance(data, list):
        return [j for j in data if isinstance(j, dict)]
    if isinstance(data, dict):
        for key in LIST_KEYS:
            v = data.get(key)
            if isinstance(v, list):
                return [j for j in v if isinstance(j, dict)]
    return []


WDAY_CXS_RE = re.compile(r"(https://[^/]+)/wday/cxs/[^/]+/([^/]+)/jobs")


def _resolve_url(url, api_url):
    """Workday's externalPath ('/job/...') has no domain -- it's relative to
    the tenant's public career-site base, which we can derive from the cxs
    API URL: .../wday/cxs/<tenant>/<site>/jobs -> https://<host>/<site>."""
    if not url.startswith("/"):
        return url
    m = WDAY_CXS_RE.search(api_url)
    if not m:
        return url
    host, site = m.groups()
    return f"{host}/{site}{url}"


def job_to_listing(company, job, api_url=""):
    title = _first(job, TITLE_KEYS)
    if not title:
        return None
    return Listing(source="ats_boards", company=company, title=title,
                    location=_get_location(job), url=_resolve_url(_first(job, URL_KEYS), api_url),
                    extra_text=_get_blob(job))


class DomainThrottle:
    """Serialize + space requests per netloc so one domain's boards don't get
    hit concurrently, while different domains still fetch in parallel.
    ponytail: per-domain lock, fine until one domain needs parallelism
    within itself (it won't here)."""
    def __init__(self, interval):
        self.interval = interval
        self._lock = threading.Lock()
        self._domains = {}

    def wait(self, url):
        d = urlparse(url).netloc
        with self._lock:
            slot = self._domains.setdefault(d, [threading.Lock(), 0.0])
        slot[0].acquire()
        try:
            gap = self.interval - (time.monotonic() - slot[1])
            if gap > 0:
                time.sleep(gap)
        finally:
            slot[1] = time.monotonic()
            slot[0].release()


def load_rows(csv_path):
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames)
    if "api_status" not in fields:
        fields.append("api_status")
    return rows, fields


def save_rows(rows, fields, csv_path):
    tmp = csv_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    tmp.replace(csv_path)


class AtsBoardsSource:
    name = "ats_boards"

    def __init__(self, csv_path=CSV_PATH, interval=1.0, workers=16, retry_dead=False):
        self.csv_path = csv_path
        self.interval = interval
        self.workers = workers
        self.retry_dead = retry_dead

    def fetch(self) -> list:
        rows, fields = load_rows(self.csv_path)
        throttle = DomainThrottle(self.interval)

        targets = []
        for row in rows:
            urls = parse_urls(row.get("api_urls", ""))
            if not self.retry_dead and row.get("api_status", "").strip() == "dead":
                urls = []
            urls = [u for u, note in urls if self.retry_dead or note != "dead"]
            if urls:
                targets.append((row, urls))
            else:
                row["api_status"] = row.get("api_status") or "skipped"

        def work(item):
            row, urls = item
            company = row["company"]
            for url in urls:
                throttle.wait(url)
                ok, data, note = fetch_json(url)
                if ok:
                    listings = []
                    for j in extract_postings(data):
                        try:
                            l = job_to_listing(company, j, api_url=url)
                        except Exception:  # noqa: BLE001 - one malformed
                            # record from a company's board shouldn't sink
                            # the whole batch; skip just that job.
                            continue
                        if l is not None:
                            listings.append(l)
                    return company, "ok", listings
            return company, "dead", []

        listings = []
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for company, status, company_listings in ex.map(work, targets):
                for row in rows:
                    if row["company"] == company:
                        row["api_status"] = status
                        break
                listings.extend(company_listings)

        save_rows(rows, fields, self.csv_path)
        return listings


def selftest():
    assert parse_urls("https://x.io/a (dead) ; https://y.io/b") == \
        [("https://x.io/a", "dead"), ("https://y.io/b", None)]
    assert extract_postings({"jobs": [{"title": "a"}]}) == [{"title": "a"}]
    assert extract_postings([{"title": "a"}]) == [{"title": "a"}]
    assert extract_postings({"jobPostings": [{"title": "a"}]}) == [{"title": "a"}]
    assert extract_postings({"nope": 1}) == []
    l = job_to_listing("Acme", {"title": "SWE Intern", "absolute_url": "http://x/1",
                                 "location": {"name": "SF"}})
    assert l.title == "SWE Intern" and l.location == "SF" and l.url == "http://x/1"
    assert job_to_listing("Acme", {"no_title": True}) is None

    # address.postalAddress can legitimately be a bare string, not a dict --
    # must not crash job_to_listing (was AttributeError, killed whole batch)
    weird = job_to_listing("Weird Co", {"title": "SWE Intern",
                                         "address": {"postalAddress": "123 Main St"}})
    assert weird.title == "SWE Intern" and weird.location == "123 Main St", weird

    assert not _is_dead_response([])
    assert not _is_dead_response({})
    assert _is_dead_response(None)
    assert _is_dead_response("")

    api_url = "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext/jobs"
    wd = job_to_listing("Autodesk", {"title": "Intern", "externalPath": "/job/Toronto/Intern_26WD1"},
                         api_url=api_url)
    assert wd.url == "https://autodesk.wd1.myworkdayjobs.com/Ext/job/Toronto/Intern_26WD1", wd.url
    print("ats_boards selftest OK")


if __name__ == "__main__":
    selftest()
