"""Source: Jobright SWE intern minisite board
(https://jobright.ai/minisites-jobs/intern/us/swe).

The page is Next.js; listings come from a stable JSON API:

    POST /swan/mini-sites/list?position=N&count=PAGE
    body: {"category": "intern:us:swe"}

Response shape: {success, result: {jobList: [...], total: int}}.
Each job has jobId + properties.{title, company, location, qualifications, ...}
and postedAt (ms epoch). Apply links are Jobright detail pages
(https://jobright.ai/jobs/info/{jobId}) — the board does not expose the
upstream company apply URL in the list payload.
"""
import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.ats_boards import DomainThrottle, TIMEOUT, UA

NAME = "jobright"
API_URL = "https://jobright.ai/swan/mini-sites/list"
CATEGORY = "intern:us:swe"
JOB_URL = "https://jobright.ai/jobs/info/{job_id}"
PAGE_SIZE = 500
# Safety cap: ~1900 listings today; 20 pages of 500 is far more than needed.
MAX_PAGES = 20
THROTTLE = DomainThrottle(1.0)


def _posted(ms):
    """postedAt is ms since epoch; return YYYY-MM-DD or ''."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _job_url(job_id):
    jid = str(job_id or "").strip().strip('"')
    if not jid:
        return ""
    return JOB_URL.format(job_id=jid)


def parse_jobs(job_list):
    """Map API jobList rows -> Listing. Skips rows missing company/title/id."""
    out = []
    if not isinstance(job_list, list):
        return out
    for job in job_list:
        if not isinstance(job, dict):
            continue
        props = job.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        company = (props.get("company") or "").strip()
        title = (props.get("title") or "").strip()
        url = _job_url(job.get("jobId"))
        if not company or not title or not url:
            continue
        location = (props.get("location") or "").strip()
        # Prefer qualifications (list payload) for 2027 detection; fall back
        # to hireTime when present (e.g. "2026-Fall").
        extra_parts = []
        quals = props.get("qualifications")
        if isinstance(quals, str) and quals.strip():
            extra_parts.append(quals.strip())
        hire = props.get("hireTime")
        if isinstance(hire, str) and hire.strip():
            extra_parts.append(hire.strip())
        out.append(Listing(
            source=NAME,
            company=company,
            title=title,
            location=location,
            url=url,
            posted=_posted(job.get("postedAt")),
            extra_text="\n".join(extra_parts),
        ))
    return out


def _fetch_page(category, position, count, opener=None):
    """One POST page. opener is for selftests (callable(req) -> bytes/str)."""
    url = f"{API_URL}?position={int(position)}&count={int(count)}"
    body = json.dumps({"category": category}).encode()
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    if opener is not None:
        raw = opener(req)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)
    with THROTTLE.hold(url):
        with urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))


def fetch_all(category=CATEGORY, page_size=PAGE_SIZE, opener=None):
    """Paginate the minisite list API -> list[Listing]."""
    listings = []
    position = 0
    for _ in range(MAX_PAGES):
        try:
            data = _fetch_page(category, position, page_size, opener=opener)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            break
        if not isinstance(data, dict) or not data.get("success"):
            break
        result = data.get("result") or {}
        page = result.get("jobList") or []
        if not isinstance(page, list) or not page:
            break
        listings.extend(parse_jobs(page))
        total = result.get("total")
        position += len(page)
        if len(page) < page_size:
            break
        if isinstance(total, int) and position >= total:
            break
    return listings


class JobrightSource:
    name = NAME

    def __init__(self, category=CATEGORY, page_size=PAGE_SIZE):
        self.category = category
        self.page_size = page_size

    def fetch(self) -> list:
        return fetch_all(category=self.category, page_size=self.page_size)


def selftest():
    # page_size=2: first response is a full page, second is a short tail.
    fixture_page0 = {
        "success": True,
        "errorCode": 10000,
        "errorMsg": None,
        "result": {
            "total": 3,
            "jobList": [
                {
                    "jobId": "abc123",
                    "properties": {
                        "title": "Software Engineering Intern - Summer 2027",
                        "company": "Acme",
                        "location": "San Francisco, CA, United States",
                        "qualifications": "Pursuing BS in CS. Graduating 2027.",
                        "hireTime": "2027-Summer",
                    },
                    "postedAt": 1720000000000,
                },
                {
                    "jobId": "def456",
                    "properties": {
                        "title": "Backend Intern",
                        "company": "Beta Corp",
                        "location": "Remote",
                        "qualifications": "",
                        "hireTime": "",
                    },
                    "postedAt": 1720001000000,
                },
            ],
        },
    }
    fixture_page1 = {
        "success": True,
        "result": {
            "total": 3,
            "jobList": [
                {
                    "jobId": "ghi789",
                    "properties": {
                        "title": "SWE Intern Fall",
                        "company": "Gamma",
                        "location": "Austin, TX, United States",
                        "qualifications": "Junior standing.",
                    },
                    "postedAt": 1720002000000,
                },
                # missing jobId -> parse skips, still counts toward API page len
                {
                    "jobId": "",
                    "properties": {
                        "title": "No ID Intern",
                        "company": "Ghost",
                        "location": "NYC",
                    },
                    "postedAt": 0,
                },
            ],
        },
    }

    calls = []

    def opener(req):
        calls.append(req.full_url)
        if "position=0" in req.full_url:
            return json.dumps(fixture_page0)
        if "position=2" in req.full_url:
            return json.dumps(fixture_page1)
        return json.dumps({"success": True, "result": {"jobList": [], "total": 3}})

    rows = fetch_all(category=CATEGORY, page_size=2, opener=opener)
    assert len(rows) == 3, rows
    assert rows[0].source == "jobright"
    assert rows[0].company == "Acme"
    assert rows[0].title.startswith("Software Engineering Intern")
    assert rows[0].url == "https://jobright.ai/jobs/info/abc123"
    assert rows[0].url.startswith("https://")
    assert "2027" in rows[0].extra_text
    assert rows[0].posted == "2024-07-03"  # 1720000000000 ms UTC
    assert rows[1].company == "Beta Corp"
    assert rows[2].company == "Gamma"
    assert rows[2].url == "https://jobright.ai/jobs/info/ghi789"
    assert any("position=0" in u for u in calls)
    assert any("position=2" in u for u in calls)
    assert JobrightSource().name == "jobright"

    # pure parse unit: relative/missing ids never ship
    assert parse_jobs([{"jobId": None, "properties": {"title": "x", "company": "y"}}]) == []
    assert parse_jobs([{"jobId": "z", "properties": {"title": "", "company": "y"}}]) == []
    only = parse_jobs([{
        "jobId": '"quoted"',
        "properties": {"title": "T", "company": "C", "location": "L"},
        "postedAt": None,
    }])
    assert only[0].url == "https://jobright.ai/jobs/info/quoted"
    assert only[0].posted == ""

    print("jobright selftest OK")


if __name__ == "__main__":
    selftest()
