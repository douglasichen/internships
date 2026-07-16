"""Source: startup.jobs internship search (https://startup.jobs/internships).

Listings come from the site's Algolia index, hit directly via the same
multi-query endpoint the frontend calls.

The x-algolia-api-key is a *secured* Algolia key (HMAC-signed, restricted to
the Post/Places/Company indices) with a short validUntil -- a couple of hours,
minted fresh on every page load. So we can't hardcode it. The site serves the
current key in a <meta name="current-algolia-api-key-search"> tag, but the
page is Cloudflare-challenged and stdlib urllib gets a 403 -- _fresh_key()
scrapes it with curl_cffi's Chrome TLS impersonation, which clears the
challenge. Key precedence (see _fresh_key):

    STARTUPJOBS_ALGOLIA_KEY env override  (paste a meta-tag value by hand)
    > live scrape via curl_cffi           (the normal path)
    > _DEFAULT_KEY                         (last-resort; likely expired -> [])

curl_cffi is an optional dep: without it (or if the scrape fails) we fall back
rather than break the run, degrading to the -- probably stale -- default key.

Response shape: {results: [{hits: [...], nbPages, page, ...}]}. Each hit has
company_name, title, location, path (relative job URL) and
published_at_iso8601. query="software" plus the employment_type:internship
facet filter mirrors what the site's own /internships?q=software search
sends -- this project only tracks SWE internships.
"""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.ats_boards import DomainThrottle, TIMEOUT, UA

NAME = "startupjobs"
APP_ID = "4CQMTMMK73"
# Raw (not URL-encoded) meta-tag value; _api_url() quotes it. Stale by now --
# only a fallback for when the live scrape and env override both miss.
_DEFAULT_KEY = "YmRiYzczYjlmNzMzNjc0MmRhMmIzZmRjOGJjNWY0YjJkMDY5YTkyMzAyNTk3ZDRlNzJlN2E3NTVlMWM3MTEyYnJlc3RyaWN0SW5kaWNlcz1Qb3N0X3Byb2R1Y3Rpb24lMkNQbGFjZXNfcHJvZHVjdGlvbiUyQ0NvbXBhbnlfcHJvZHVjdGlvbiZ2YWxpZFVudGlsPTE3ODQxOTc4ODg="
PAGE_URL = "https://startup.jobs/internships?c=internship&q=software"
_META_KEY_RE = re.compile(r'current-algolia-api-key-search"[^>]*\bcontent="([^"]+)"')
JOB_BASE_URL = "https://startup.jobs"
INDEX_NAME = "Post_production"
QUERY = "software"
PAGE_SIZE = 100  # Algolia caps hitsPerPage at 100 regardless of what we ask
MAX_PAGES = 20  # safety cap; ~330 hits / 100 per page today
THROTTLE = DomainThrottle(1.0)


def _fresh_key():
    """Current search key by precedence: env override > live scrape > default.
    Never raises -- any failure falls through to the next source (see module
    docstring)."""
    override = os.environ.get("STARTUPJOBS_ALGOLIA_KEY")
    if override:
        return override
    try:
        from curl_cffi import requests as creq
        with THROTTLE.hold(PAGE_URL):  # startup.jobs netloc, separate from the API host
            html = creq.get(PAGE_URL, impersonate="chrome", timeout=TIMEOUT).text
        m = _META_KEY_RE.search(html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return _DEFAULT_KEY


def _api_url(key):
    return ("https://4cqmtmmk73-3.algolianet.com/1/indexes/*/queries"
            "?x-algolia-agent=Algolia%20for%20JavaScript%20(4.24.0)%3B%20Browser%20(lite)"
            f"&x-algolia-api-key={quote(key, safe='')}"
            f"&x-algolia-application-id={APP_ID}")


def _params(page):
    # query is encoded here (authoritative for /indexes/*/queries); the
    # request body doesn't repeat it.
    return urlencode({
        "query": QUERY,
        "attributesToRetrieve": json.dumps(
            ["company_name", "location", "path", "published_at_iso8601", "title"]),
        "hitsPerPage": PAGE_SIZE,
        "page": page,
        "facetFilters": json.dumps([["employment_type:internship"]]),
    })


def parse_hits(hits):
    """Map Algolia hits -> Listing. Skips rows missing company/title/path."""
    out = []
    if not isinstance(hits, list):
        return out
    for h in hits:
        if not isinstance(h, dict):
            continue
        company = (h.get("company_name") or "").strip()
        title = (h.get("title") or "").strip()
        path = (h.get("path") or "").strip()
        if not company or not title or not path:
            continue
        url = JOB_BASE_URL + path if path.startswith("/") else f"{JOB_BASE_URL}/{path}"
        posted = (h.get("published_at_iso8601") or "")[:10]  # YYYY-MM-DD prefix
        out.append(Listing(
            source=NAME, company=company, title=title,
            location=(h.get("location") or "").strip(), url=url, posted=posted))
    return out


def _fetch_page(page, key, opener=None):
    url = _api_url(key)
    body = json.dumps({"requests": [
        {"indexName": INDEX_NAME, "params": _params(page)}]}).encode()
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            # Matches the site's own frontend request, which avoids a CORS
            # preflight on this endpoint despite the body being JSON.
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://startup.jobs/",
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


def fetch_all(opener=None, key=None):
    if key is None:
        key = _fresh_key()
    listings = []
    page = 0
    while page < MAX_PAGES:
        try:
            data = _fetch_page(page, key, opener=opener)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
            break
        results = data.get("results") if isinstance(data, dict) else None
        result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
        if result is None:
            break
        hits = result.get("hits")
        if not isinstance(hits, list) or not hits:
            break
        listings.extend(parse_hits(hits))
        page += 1
        nb_pages = result.get("nbPages")
        if isinstance(nb_pages, int) and page >= nb_pages:
            break
    return listings


class StartupJobsSource:
    name = NAME

    def fetch(self) -> list:
        return fetch_all()


def selftest():
    fixture_page0 = {"results": [{
        "hits": [
            {"company_name": "Acme", "title": "Software Engineer Intern",
             "location": "Remote", "path": "/software-engineer-intern-acme-123",
             "published_at_iso8601": "2026-07-15T11:11:23Z"},
            {"company_name": "Beta Corp", "title": "SWE Intern",
             "location": "NYC", "path": "/swe-intern-beta-456",
             "published_at_iso8601": "2026-07-14T00:00:00Z"},
        ],
        "nbPages": 2, "page": 0,
    }]}
    fixture_page1 = {"results": [{
        "hits": [
            {"company_name": "Gamma", "title": "Software Intern",
             "location": "Austin, TX", "path": "/software-intern-gamma-789",
             "published_at_iso8601": "2026-07-13T00:00:00Z"},
        ],
        "nbPages": 2, "page": 1,
    }]}

    calls = []

    def opener(req):
        calls.append(req.data)
        if b"page=0" in req.data:
            return json.dumps(fixture_page0)
        if b"page=1" in req.data:
            return json.dumps(fixture_page1)
        return json.dumps({"results": [{"hits": [], "nbPages": 2}]})

    # key= is passed so _fresh_key()/curl_cffi/network is never touched offline.
    rows = fetch_all(opener=opener, key="test")
    assert len(rows) == 3, rows
    assert rows[0].source == "startupjobs"
    assert rows[0].company == "Acme"
    assert rows[0].url == "https://startup.jobs/software-engineer-intern-acme-123"
    assert rows[0].posted == "2026-07-15"
    assert rows[1].company == "Beta Corp"
    assert rows[2].company == "Gamma"
    assert len(calls) == 2

    # pure parse unit: rows missing company/title/path never ship
    assert parse_hits([{"company_name": "", "title": "x", "path": "/y"}]) == []
    assert parse_hits([{"company_name": "x", "title": "y", "path": ""}]) == []
    only = parse_hits([{"company_name": "C", "title": "T", "path": "no-leading-slash"}])
    assert only[0].url == "https://startup.jobs/no-leading-slash"

    # raw base64 key (with = padding) must be percent-encoded into the URL
    assert "x-algolia-api-key=abc%3D%3D" in _api_url("abc==")
    # env override wins and is returned verbatim, no network
    os.environ["STARTUPJOBS_ALGOLIA_KEY"] = "override=="
    try:
        assert _fresh_key() == "override=="
    finally:
        del os.environ["STARTUPJOBS_ALGOLIA_KEY"]
    # meta-tag regex pulls the key out of the page markup, tolerating any
    # attributes interposed between name and content
    assert _META_KEY_RE.search(
        '<meta name="current-algolia-api-key-search" content="ZZZ=">').group(1) == "ZZZ="
    assert _META_KEY_RE.search(
        '<meta name="current-algolia-api-key-search" id="x" content="QQ==">').group(1) == "QQ=="

    assert StartupJobsSource().name == "startupjobs"
    print("startupjobs selftest OK")


if __name__ == "__main__":
    selftest()
