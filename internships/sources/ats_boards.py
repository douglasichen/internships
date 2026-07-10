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
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from internships.filters import is_swe_internship
from internships.models import Listing

ROOT = Path(__file__).resolve().parent.parent.parent
CSV_PATH = ROOT / "companies.csv"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) internships-service"
TIMEOUT = 25
URL_RE = re.compile(r"https?://\S+")

TITLE_KEYS = ("title", "text", "name", "jobTitle", "job_title")
# job_path: Amazon.jobs search.json (relative "/en/jobs/..."); resolved via
# _resolve_url against the API origin. Prefer it over any later absolute keys
# that are login/apply redirects rather than the public posting page.
URL_KEYS = ("absolute_url", "hostedUrl", "jobUrl", "applyUrl", "externalPath",
            "job_path", "url", "canonicalUrl")
LOC_KEYS = ("location", "city", "locationName", "primaryLocation", "locationsText")
BODY_KEYS = ("descriptionPlain", "descriptionBodyPlain", "content",
             "description", "openingPlain")
# "content": SmartRecruiters (/v1/companies/.../postings). Must stay after
# "jobs"/"data"/etc. -- Amazon.jobs also has a top-level "content" object
# (a dict, not a list), which isinstance(v, list) correctly skips.
LIST_KEYS = ("jobs", "jobPostings", "postings", "data", "results", "content")


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


def request_for(url, search_text="", offset=0, limit=20):
    """Workday cxs search and Uber want a POST body; everything else is a
    GET -- including Workday's own per-job detail page, which also lives
    under /wday/cxs/ but (unlike the .../jobs search endpoint) wants a
    plain GET. search_text/offset/limit only matter for the Workday branch
    (see fetch_workday_postings) -- an unfiltered searchText="" would only
    ever return the site's first 20 postings unsorted, which for a big
    company almost never includes an internship."""
    if "/wday/cxs/" in url and url.split("?")[0].endswith("/jobs"):
        body = json.dumps({"appliedFacets": {}, "limit": limit, "offset": offset,
                            "searchText": search_text}).encode()
        return Request(url, data=body, method="POST",
                       headers={"User-Agent": UA, "Content-Type": "application/json",
                                "Accept": "application/json"})
    if "loadSearchJobsResults" in url:
        body = json.dumps({"limit": 10, "page": 1, "params": {}}).encode()
        return Request(url, data=body, method="POST",
                       headers={"User-Agent": UA, "Content-Type": "application/json",
                                "Accept": "application/json"})
    return Request(url, headers={"User-Agent": UA, "Accept": "application/json"})


def fetch_json(url, **kwargs):
    """Return (ok, parsed_json_or_None, note). kwargs forwarded to
    request_for (search_text/offset/limit -- meaningful for Workday only)."""
    try:
        with urlopen(request_for(url, **kwargs), timeout=TIMEOUT) as r:
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


def _str_field(*values):
    """First non-empty string among values; never returns a non-str (list/
    dict locations must not escape into Listing.location -- year_relevance
    and friends call re on these fields and a list crashes the whole
    ats_boards batch at filter time)."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _get_location(job):
    v = _first(job, LOC_KEYS)
    if v:
        return v
    loc = job.get("location")
    if isinstance(loc, dict):
        # Greenhouse: {name}; SmartRecruiters: {city, fullLocation} (no name)
        return _str_field(loc.get("name"), loc.get("locationName"),
                          loc.get("city"), loc.get("fullLocation"))
    cats = job.get("categories")
    if isinstance(cats, dict):
        cl = cats.get("location")
        if isinstance(cl, str) and cl.strip():
            return cl.strip()
        if isinstance(cl, list):
            return "; ".join(x.strip() for x in cl if isinstance(x, str) and x.strip())
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
    # deliberately untouched (HTML tags/entities and all) -- this is meant to
    # be copy-pasted into an AI later, which handles markup noise just fine
    return " ".join(v for k in BODY_KEYS if isinstance((v := job.get(k)), str))


def extract_postings(data):
    """Find the array of job dicts inside an arbitrary ATS JSON response.
    Covers every shape seen so far: Ashby/Greenhouse ({"jobs": [...]}),
    Workday ({"jobPostings": [...]}), Lever (bare list), SmartRecruiters
    ({"content": [...]})."""
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
    """Turn a board-relative path into an absolute apply URL.

    Workday's externalPath ('/job/...') is relative to the tenant's public
    career-site base (.../wday/cxs/<tenant>/<site>/jobs -> https://<host>/<site>).
    Other boards (Amazon.jobs job_path) are just origin-relative, so fall
    back to scheme://netloc + path when the Workday pattern doesn't match."""
    if not url or not url.startswith("/"):
        return url or ""
    m = WDAY_CXS_RE.search(api_url)
    if m:
        host, site = m.groups()
        return f"{host}/{site}{url}"
    origin = urlparse(api_url)
    if origin.scheme and origin.netloc:
        return f"{origin.scheme}://{origin.netloc}{url}"
    return url


def _fallback_url(job):
    """SmartRecruiters list rows carry no absolute apply link -- only
    company.identifier + id. Reconstruct the public jobs.smartrecruiters.com
    URL so the listing has a clickable link and a stable dedup identity."""
    company = job.get("company")
    jid = job.get("id")
    if isinstance(company, dict) and company.get("identifier") and jid:
        return f"https://jobs.smartrecruiters.com/{company['identifier']}/{jid}"
    return ""


def job_to_listing(company, job, api_url=""):
    title = _first(job, TITLE_KEYS)
    if not title:
        return None
    url = _resolve_url(_first(job, URL_KEYS), api_url) or _fallback_url(job)
    return Listing(source="ats_boards", company=company, title=title,
                    location=_get_location(job), url=url,
                    extra_text=_get_blob(job))


def workday_detail_url(api_url, external_path):
    """Workday's list/search endpoint (.../wday/cxs/<tenant>/<site>/jobs)
    carries no description -- only the per-job detail endpoint does, which is
    that same cxs base with '/jobs' swapped for the job's externalPath."""
    base = api_url.split("?")[0]
    if not (base.endswith("/jobs") and external_path):
        return None
    return base[:-len("/jobs")] + external_path


def fetch_workday_description(api_url, external_path):
    detail_url = workday_detail_url(api_url, external_path)
    if not detail_url:
        return ""
    ok, data, _ = fetch_json(detail_url)
    if not ok or not isinstance(data, dict):
        return ""
    info = data.get("jobPostingInfo")
    if not isinstance(info, dict):
        return ""
    desc = info.get("jobDescription", "")
    return desc if isinstance(desc, str) else ""


WORKDAY_PAGE_LIMIT = 20
# ponytail: 5 pages/term (100 postings) is a hard ceiling so one company
# can't make the sweep fetch unboundedly; raise if real intern/co-op hits
# start showing up past page 5 for some company (Workday's searchText is a
# loose relevance-ranked substring match, so in practice they cluster early).
WORKDAY_MAX_PAGES = 5
# "intern" alone misses postings titled bare "Co-Op" with no "intern"
# substring (confirmed live: Analog Devices "Embedded Software Co-Op"), so
# both terms are searched and merged rather than guessing one covers both.
WORKDAY_SEARCH_TERMS = ("intern", "co-op")


def _dedupe_key(job):
    return _first(job, URL_KEYS) or id(job)


def fetch_workday_postings(url, throttle):
    """Search a Workday cxs /jobs endpoint for internship/co-op postings,
    paginating each search term until a short page (fewer than the limit --
    no more results) or WORKDAY_MAX_PAGES, merging + deduping across terms.
    Returns (jobs, ok) -- ok is True iff at least one request succeeded, so a
    company with zero current matches still counts as a live endpoint."""
    seen, jobs, any_ok = set(), [], False
    for term in WORKDAY_SEARCH_TERMS:
        offset = 0
        for _ in range(WORKDAY_MAX_PAGES):
            throttle.wait(url)
            ok, data, note = fetch_json(url, search_text=term, offset=offset,
                                         limit=WORKDAY_PAGE_LIMIT)
            if not ok:
                break
            any_ok = True
            page = extract_postings(data)
            for j in page:
                key = _dedupe_key(j)
                if key not in seen:
                    seen.add(key)
                    jobs.append(j)
            if len(page) < WORKDAY_PAGE_LIMIT:
                break
            offset += WORKDAY_PAGE_LIMIT
    return jobs, any_ok


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
                if WDAY_CXS_RE.search(url):
                    jobs, ok = fetch_workday_postings(url, throttle)
                else:
                    throttle.wait(url)
                    ok, data, note = fetch_json(url)
                    jobs = extract_postings(data) if ok else []
                if ok:
                    listings = []
                    for j in jobs:
                        try:
                            l = job_to_listing(company, j, api_url=url)
                        except Exception:  # noqa: BLE001 - one malformed
                            # record from a company's board shouldn't sink
                            # the whole batch; skip just that job.
                            continue
                        if l is not None:
                            # Workday's list endpoint has no description --
                            # only fetch the per-job detail page (one extra
                            # request each) for postings that already look
                            # like an SWE internship, to keep the added
                            # request volume small.
                            if not l.extra_text and WDAY_CXS_RE.search(url) and is_swe_internship(l.title):
                                external_path = _first(j, ("externalPath",))
                                throttle.wait(url)
                                desc = fetch_workday_description(url, external_path)
                                if desc:
                                    l = replace(l, extra_text=desc)
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
    # the /jobs search endpoint POSTs a query body; a Workday detail page
    # (same /wday/cxs/ prefix, but not ending in /jobs) must stay a plain GET
    assert request_for("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Ext/jobs").get_method() == "POST"
    assert request_for("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Ext/job/y").get_method() == "GET"

    # search_text/offset/limit thread through into the POST body -- this is
    # what makes pagination + keyword search possible instead of always
    # fetching the same unfiltered first 20 postings.
    req = request_for("https://x.wd1.myworkdayjobs.com/wday/cxs/x/Ext/jobs",
                       search_text="intern", offset=20, limit=30)
    assert json.loads(req.data) == {"appliedFacets": {}, "limit": 30, "offset": 20,
                                     "searchText": "intern"}, req.data

    assert parse_urls("https://x.io/a (dead) ; https://y.io/b") == \
        [("https://x.io/a", "dead"), ("https://y.io/b", None)]
    assert extract_postings({"jobs": [{"title": "a"}]}) == [{"title": "a"}]
    assert extract_postings([{"title": "a"}]) == [{"title": "a"}]
    assert extract_postings({"jobPostings": [{"title": "a"}]}) == [{"title": "a"}]
    assert extract_postings({"nope": 1}) == []
    # SmartRecruiters: postings live under "content", not jobs/data/results.
    # Without this key, every SmartRecruiters board in companies.csv (16 of
    # them, status "ok") silently yielded zero listings.
    assert extract_postings({"content": [{"name": "SWE Intern"}], "totalFound": 1}) == \
        [{"name": "SWE Intern"}]
    # Amazon.jobs has a top-level "content" *dict* alongside "jobs" -- must
    # still pick jobs, and must not treat the dict as a posting list.
    assert extract_postings({"content": {"sidebar": {}}, "jobs": [{"title": "a"}]}) == \
        [{"title": "a"}]
    assert extract_postings({"content": {"sidebar": {}}}) == []

    l = job_to_listing("Acme", {"title": "SWE Intern", "absolute_url": "http://x/1",
                                 "location": {"name": "SF"}})
    assert l.title == "SWE Intern" and l.location == "SF" and l.url == "http://x/1"
    assert job_to_listing("Acme", {"no_title": True}) is None

    # address.postalAddress can legitimately be a bare string, not a dict --
    # must not crash job_to_listing (was AttributeError, killed whole batch)
    weird = job_to_listing("Weird Co", {"title": "SWE Intern",
                                         "address": {"postalAddress": "123 Main St"}})
    assert weird.title == "SWE Intern" and weird.location == "123 Main St", weird

    # SmartRecruiters location is {city, fullLocation} with no "name" -- was
    # blanking every SR location. Also reconstruct the public apply URL from
    # company.identifier + id (list payload has no absolute_url).
    sr = job_to_listing("ServiceNow", {
        "name": "Software Engineer Intern",
        "id": "744000137189553",
        "company": {"identifier": "servicenow", "name": "ServiceNow"},
        "location": {"city": "New York", "region": "New York", "country": "us",
                     "fullLocation": "New York, New York, United States"},
    })
    assert sr is not None and sr.location == "New York", sr
    assert sr.url == "https://jobs.smartrecruiters.com/servicenow/744000137189553", sr.url

    # categories.location as a list (or any non-str) must become a str -- a
    # list-valued Listing.location TypeErrors year_relevance and, via
    # service._run_one's listcomp, kills the entire ats_boards source for the run.
    multi = job_to_listing("Acme", {"title": "SWE Intern", "absolute_url": "http://x/m",
                                     "categories": {"location": ["SF", "NYC"]}})
    assert isinstance(multi.location, str) and multi.location == "SF; NYC", multi.location
    bad_loc = job_to_listing("Acme", {"title": "SWE Intern", "absolute_url": "http://x/b",
                                       "location": {"name": ["Remote", "SF"]}})
    assert isinstance(bad_loc.location, str) and bad_loc.location == "", bad_loc.location

    # Amazon.jobs: job_path is origin-relative; must resolve against the API
    # host rather than being left as "/en/jobs/..." (broken apply link + weak id).
    amz = job_to_listing(
        "Amazon",
        {"title": "SDE Intern", "job_path": "/en/jobs/10418355/2027-software-dev-engineer-intern",
         "location": "US, WA, Seattle"},
        api_url="https://www.amazon.jobs/en/search.json",
    )
    assert amz.url == (
        "https://www.amazon.jobs/en/jobs/10418355/2027-software-dev-engineer-intern"
    ), amz.url

    assert not _is_dead_response([])
    assert not _is_dead_response({})
    assert _is_dead_response(None)
    assert _is_dead_response("")

    api_url = "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext/jobs"
    wd = job_to_listing("Autodesk", {"title": "Intern", "externalPath": "/job/Toronto/Intern_26WD1"},
                         api_url=api_url)
    assert wd.url == "https://autodesk.wd1.myworkdayjobs.com/Ext/job/Toronto/Intern_26WD1", wd.url

    # Workday's plain jobPosting shape uses locationsText, not location/city/etc.
    wd_loc = job_to_listing("NVIDIA", {"title": "Intern", "externalPath": "/job/x",
                                        "locationsText": "Israel, Yokneam"}, api_url=api_url)
    assert wd_loc.location == "Israel, Yokneam", wd_loc.location

    assert workday_detail_url(api_url, "/job/Toronto/Intern_26WD1") == \
        "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext/job/Toronto/Intern_26WD1"
    assert workday_detail_url(api_url, "") is None
    assert workday_detail_url("https://api.ashbyhq.com/posting-api/job-board/openai", "/job/x") is None

    import sys
    _self = sys.modules[__name__]  # not a fresh dotted import: this file runs
    # as __main__ under its own selftest, which is a different module object
    # than "internships.sources.ats_boards" -- patching that one wouldn't
    # affect the fetch_json name this file's own functions actually look up.
    orig_fetch_json = _self.fetch_json
    _self.fetch_json = lambda url: (True, {"jobPostingInfo": {"jobDescription": "full text"}}, "ok")
    try:
        assert fetch_workday_description(api_url, "/job/x") == "full text"
        assert fetch_workday_description(api_url, "") == ""  # no externalPath -> no detail_url
    finally:
        _self.fetch_json = orig_fetch_json

    # non-str jobDescription (null/list) must not leak into Listing.extra_text
    _self.fetch_json = lambda url: (True, {"jobPostingInfo": {"jobDescription": None}}, "ok")
    try:
        assert fetch_workday_description(api_url, "/job/x") == ""
    finally:
        _self.fetch_json = orig_fetch_json

    # fetch_workday_postings: pages each search term until a short page,
    # dedupes across terms by URL/externalPath, caps at WORKDAY_MAX_PAGES,
    # and reports ok=False only when every request failed.
    throttle = DomainThrottle(0)
    calls = []

    def fake_paginated(url, search_text="", offset=0, limit=20):
        calls.append((search_text, offset))
        if search_text == "intern":
            n = 20 if offset == 0 else 5  # 2nd page short (5 < 20) -> stop
            return True, {"jobPostings": [{"externalPath": f"/job/i{offset + i}"}
                                           for i in range(n)]}, "ok"
        if search_text == "co-op":
            # one dup of an "intern" hit (i0) + one co-op-only hit -- proves
            # cross-term dedup and that "co-op" isn't just wasted requests
            return True, {"jobPostings": [{"externalPath": "/job/i0"},
                                           {"externalPath": "/job/c0"}]}, "ok"
        return False, None, "err"

    _self.fetch_json = fake_paginated
    try:
        jobs, ok = fetch_workday_postings(api_url, throttle)
        assert ok
        assert len(jobs) == 26, len(jobs)  # 20 + 5 intern, +1 new (i0 deduped) from co-op
        assert calls == [("intern", 0), ("intern", 20), ("co-op", 0)], calls

        # every page comes back full -> pagination stops at the hard cap
        # instead of continuing forever
        _self.fetch_json = lambda url, search_text="", offset=0, limit=20: (
            True, {"jobPostings": [{"externalPath": f"/job/{search_text}-{offset}-{i}"}
                                    for i in range(limit)]}, "ok")
        jobs, ok = fetch_workday_postings(api_url, throttle)
        assert ok
        assert len(jobs) == WORKDAY_MAX_PAGES * len(WORKDAY_SEARCH_TERMS) * WORKDAY_PAGE_LIMIT

        # every request fails -> dead endpoint, not a false "ok, zero hits"
        _self.fetch_json = lambda url, search_text="", offset=0, limit=20: (False, None, "http 403")
        jobs, ok = fetch_workday_postings(api_url, throttle)
        assert not ok and jobs == []
    finally:
        _self.fetch_json = orig_fetch_json
    print("ats_boards selftest OK")


if __name__ == "__main__":
    selftest()
