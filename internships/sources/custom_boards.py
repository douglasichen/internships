"""Source: bespoke company career-site endpoints that are NOT a standard ATS
board (Greenhouse/Lever/Ashby/Workday/Oracle/Eightfold all live in
ats_boards). Each company here is a verified fetch spec in CONFIG; the engine
is generic, so adding a company is a config row rather than new code.

A spec is expressive enough to cover the shapes seen in the wild: GET or POST,
an optional JSON request body, custom headers (e.g. Algolia app-id/key), an
optional regex to pull an embedded JSON blob out of an HTML response (Next.js
__NEXT_DATA__ etc.), a dotted path to the array of job dicts, and dotted keys
(or a URL template) for each field. The genuinely un-generalizable shapes get
a plain callable in CONFIG instead of a spec dict -- see DECODERS.
"""
import html
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.ats_boards import DomainThrottle, UA, TIMEOUT

NAME = "custom_boards"


def _dig(obj, path):
    """Follow a dotted path (supports integer list indices). Returns None if
    any step is missing/mistyped -- callers treat None as 'not present'."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def _str(v):
    return v.strip() if isinstance(v, str) and v.strip() else ""


def _loc(v):
    """A location field is sometimes a list (e.g. Eightfold 'locations' /
    'standardizedLocations') -- take the first usable string rather than
    silently blanking it."""
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, str) and x.strip()), "")
    return _str(v)


_ORIGIN_RE = re.compile(r"(https?://[^/]+)")


def _abs_url(url, spec):
    """Make a relative apply link absolute: prepend url_prefix if set, else the
    origin of the spec's own endpoint. Applies in both HTML and JSON modes, so
    a bare relative link_key (e.g. '/careers/job/123') doesn't ship unusable."""
    if url.startswith("/"):
        prefix = spec.get("url_prefix")
        if not prefix:
            m = _ORIGIN_RE.match(spec.get("url", ""))
            prefix = m.group(1) if m else ""
        url = prefix.rstrip("/") + url
    return url


_TEMPLATE_RE = re.compile(r"\{([\w.]+)\}")


def _fill(template, job):
    """Build a URL from a job dict: '{k}' -> str(job's dotted key k)."""
    return _TEMPLATE_RE.sub(lambda m: str(_dig(job, m.group(1)) or ""), template)


def fetch_text(spec):
    """Raw response text for a spec (GET, or POST with an optional body).
    HTML-mode specs (row_regex, or extract_regex pulling JSON out of a page)
    must NOT advertise Accept: application/json -- some sites content-negotiate
    and return a stripped/error body for it, breaking the parse."""
    method = spec.get("method", "GET").upper()
    html_mode = spec.get("row_regex") or spec.get("extract_regex")
    accept = ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
              if html_mode else "application/json")
    headers = {"User-Agent": UA, "Accept": accept, **spec.get("headers", {})}
    body = spec.get("body")
    data = body.encode() if (method == "POST" and body) else None
    if data and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    req = Request(spec["url"], data=data, method=method, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _rows_from_html(raw, spec):
    """row_regex mode: a server-rendered HTML board with a repeating job-card
    pattern. The regex is applied with finditer; each match's named groups
    (title required; url/loc/desc optional) become one Listing. url_prefix is
    prepended to a relative url. This is the generic 'raw HTML' path for sites
    with no JSON feed at all."""
    out = []
    for m in re.finditer(spec["row_regex"], raw, re.S):
        g = m.groupdict()
        title = html.unescape(_str(re.sub(r"<[^>]+>", "", g.get("title", "") or ""))).strip()
        if not title:
            continue
        url = _abs_url(html.unescape(_str(g.get("url", ""))), spec)
        loc = html.unescape(_str(re.sub(r"<[^>]+>", "", g.get("loc", "") or ""))).strip()
        desc = _str(g.get("desc", ""))
        out.append(Listing(NAME, spec["company"], title, loc, url, extra_text=desc))
    return out


def spec_to_listings(spec):
    """Run one CONFIG spec -> list[Listing]. Any transport/parse failure or a
    shape that no longer matches degrades to [] rather than raising -- one
    dead company must not sink the whole source (service.py also guards, but
    keeping it here means a stale spec just yields nothing)."""
    try:
        raw = fetch_text(spec)
        if spec.get("row_regex"):
            return _rows_from_html(raw, spec)
        rx = spec.get("extract_regex")
        if rx:
            m = re.search(rx, raw, re.S)
            if not m:
                return []
            raw = m.group(1)
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - best-effort external fetch
        return []
    arr = _dig(data, spec.get("list_path", ""))
    if not isinstance(arr, list):
        return []
    out = []
    for job in arr:
        # a "job" is usually a dict, but some sites (e.g. Google's cportal)
        # encode each posting as a positional array -- _dig handles integer
        # path segments, so allow lists too. The whole body is guarded so a
        # single malformed row (or a spec that omits title_key) skips that row
        # instead of raising out of the ThreadPoolExecutor and zeroing the run.
        if not isinstance(job, (dict, list)):
            continue
        try:
            title = _str(_dig(job, spec.get("title_key", "")))
            if not title:
                continue
            if spec.get("link_template"):
                url = _fill(spec["link_template"], job)
            else:
                url = _abs_url(_str(_dig(job, spec.get("link_key", ""))), spec)
            loc = _loc(_dig(job, spec.get("loc_key", "")))
            desc = _str(_dig(job, spec.get("desc_key", "")))
        except Exception:  # noqa: BLE001 - one malformed row must not sink the spec
            continue
        out.append(Listing(NAME, spec["company"], title, loc, url, extra_text=desc))
    return out


# ---------------------------------------------------------------------------
# Bespoke decoders: shapes a generic spec can't express (non-JSON payloads
# like React-Router turbo-stream). Each takes no args and returns list[Listing].
# Kept as plain callables in CONFIG alongside the spec dicts.
# ---------------------------------------------------------------------------
DECODERS = {}


def decoder(company, url):
    """Register a bespoke decoder and expose its domain for throttling."""
    def wrap(fn):
        DECODERS[company] = (url, fn)
        return fn
    return wrap


# ---------------------------------------------------------------------------
# Tesla: compressed-key state payload (not a generic list-of-jobs shape).
# Endpoint: GET https://www.tesla.com/cua-api/apps/careers/state
# Shape verified against Wayback (2025-05) + public first-party scrapers:
#   lookup.locations / .departments / .types  — id -> display string
#   listings[]  — {id, t title, dp dept id, l location id, y type id}
# Detail URL: https://www.tesla.com/careers/search/job/<slug-title>-<id>
# Akamai Bot Manager often 403/429's non-browser clients (cpr_chlge). Live
# scrape needs a non-blocked network; CI uses fixture-based decode only.
# ---------------------------------------------------------------------------
_TESLA_STATE_URL = "https://www.tesla.com/cua-api/apps/careers/state"
_TESLA_DETAIL_ROOT = "https://www.tesla.com/careers/search/job"
_TESLA_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _tesla_job_url(title, job_id):
    slug = _TESLA_SLUG_RE.sub("-", title.casefold()).strip("-") or "job"
    return f"{_TESLA_DETAIL_ROOT}/{slug}-{job_id}"


def listings_from_tesla_state(payload):
    """Decode Tesla careers state JSON -> list[Listing]. Pure function so
    selftests don't need a live (Akamai-gated) fetch."""
    if not isinstance(payload, dict):
        return []
    lookup = payload.get("lookup") if isinstance(payload.get("lookup"), dict) else {}
    locs = lookup.get("locations") if isinstance(lookup.get("locations"), dict) else {}
    depts = lookup.get("departments") if isinstance(lookup.get("departments"), dict) else {}
    loc_map = {str(k): _str(v) for k, v in locs.items()}
    dept_map = {str(k): _str(v) for k, v in depts.items()}

    out = []
    for row in payload.get("listings") or []:
        if not isinstance(row, dict):
            continue
        title = _str(row.get("t"))
        # Live payloads have used both string and numeric ids; _str alone
        # dropped every row when id was an int (and would yield zero Tesla
        # listings for the whole run).
        raw_id = row.get("id")
        if isinstance(raw_id, str):
            job_id = raw_id.strip()
        elif isinstance(raw_id, (int, float)) and not isinstance(raw_id, bool):
            job_id = str(int(raw_id)) if isinstance(raw_id, float) and raw_id == int(raw_id) else str(raw_id)
        else:
            job_id = ""
        if not title or not job_id:
            continue
        raw_l = row.get("l")
        if isinstance(raw_l, list):
            loc = ", ".join(
                p for p in (loc_map.get(str(x), "") for x in raw_l) if p
            )
        elif raw_l is None or raw_l == "":
            loc = ""
        else:
            loc = loc_map.get(str(raw_l), "")
        dept = dept_map.get(str(row.get("dp")), "") if row.get("dp") is not None else ""
        out.append(Listing(
            NAME, "Tesla", title, loc, _tesla_job_url(title, job_id),
            extra_text=dept,
        ))
    return out


@decoder("Tesla", _TESLA_STATE_URL)
def _tesla():
    """Fetch Tesla /cua-api/apps/careers/state and decode compact listings.
    Transport/shape failures yield [] so one blocked edge never sinks the
    whole custom_boards source."""
    try:
        req = Request(_TESLA_STATE_URL, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tesla.com/careers/search/",
            "Origin": "https://www.tesla.com",
        })
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - Akamai 403/429 / network; degrade to []
        return []
    # Akamai challenge is small JSON ({cpr_chlge,t}) without listings
    if not isinstance(payload, dict) or "listings" not in payload:
        return []
    return listings_from_tesla_state(payload)


# CONFIG: verified fetch specs (dicts) + bespoke decoder companies. Populated
# from the endpoint-recovery sweep; every entry was curl-verified to return
# real postings before being added. See docs/endpoint_recovery.md.
CONFIG = [
    {'company': 'Groq', 'url': 'https://jobs.gem.com/api/public/graphql', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"operationName":"JobBoardList","variables":{"boardId":"groq"},"query":"query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) { jobPostings { id extId title locations { id name city isoCountry isRemote extId } job { id department { id name extId } locationType employmentType } } } }"}', 'list_path': 'data.oatsExternalJobPostings.jobPostings', 'title_key': 'title', 'link_template': 'https://job-boards.gem.com/groq/job/{id}', 'loc_key': 'locations.0.name'},
    {'company': 'Rippling', 'url': 'https://6FNAX3TBEF-dsn.algolia.net/1/indexes/careers_en-US_production/query', 'method': 'POST', 'headers': {'X-Algolia-Application-Id': '6FNAX3TBEF', 'X-Algolia-API-Key': '416caa4690f002ff6fe4a2097623640b', 'Content-Type': 'application/json'}, 'body': '{"params":"query=&hitsPerPage=1000"}', 'list_path': 'hits', 'title_key': 'name', 'link_key': 'url', 'loc_key': 'locationNames.0'},
    {'company': 'GitHub', 'url': 'https://www.github.careers/api/jobs?limit=20', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.location_name', 'desc_key': 'data.description'},
    {'company': 'D.E. Shaw', 'url': 'https://www.deshaw.com/careers', 'method': 'GET', 'extract_regex': '<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', 'list_path': 'props.pageProps.internships', 'title_key': 'data.displayName', 'link_template': 'https://www.deshaw.com/careers/{data.jobUrl}', 'loc_key': 'data.jobMetadata.jobLocations.0.name', 'desc_key': 'data.jobDescription.websiteDescription'},
    {'company': 'Apple', 'url': 'https://jobs.apple.com/api/v1/search', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"query":"","filters":{},"page":1,"locale":"en-us","sort":"newest","format":{"longDate":"MMMM D, YYYY","mediumDate":"MMM D, YYYY"}}', 'list_path': 'res.searchResults', 'title_key': 'postingTitle', 'link_template': 'https://jobs.apple.com/en-us/details/{positionId}/{transformedPostingTitle}', 'loc_key': 'locations.0.name', 'desc_key': 'jobSummary'},
    {'company': 'Goldman Sachs', 'url': 'https://api-higher.gs.com/gateway/api/v1/graphql', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"operationName":"GetCampusRoles","query":"query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) { roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { roleId jobTitle jobFunction locations { primary state country city } division jobType { code description } } } }","variables":{"searchQueryInput":{"page":{"pageSize":50,"pageNumber":0},"filters":[],"experiences":["CAMPUS"],"searchTerm":""}}}', 'list_path': 'data.roleSearch.items', 'title_key': 'jobTitle', 'link_template': 'https://higher.gs.com/roles/{roleId}', 'loc_key': 'locations.0.city', 'desc_key': 'jobFunction'},
    {'company': 'Retool', 'url': 'https://jobs.gem.com/api/public/graphql', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"operationName":"JobBoardList","variables":{"boardId":"retool"},"query":"query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) { jobPostings { id extId title locations { id name city isoCountry isRemote extId } job { id department { id name extId } locationType employmentType } } } }"}', 'list_path': 'data.oatsExternalJobPostings.jobPostings', 'title_key': 'title', 'link_template': 'https://jobs.gem.com/retool/{extId}', 'loc_key': 'locations.0.name'},
    {'company': 'Sea Limited', 'url': 'https://career.sea.com/api/user/job/list?externalEntityId=3&limit=1000&offset=0&postType=1', 'method': 'GET', 'list_path': 'data.job_list', 'title_key': 'job_name', 'link_template': 'https://career.sea.com/jobs/{id}', 'loc_key': 'city_id', 'desc_key': 'job_description'},
    {'company': 'Rivian', 'url': 'https://rivian.jibeapply.com/api/jobs?keywords=intern&limit=50&offset=0', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.full_location', 'desc_key': 'data.description'},
    {'company': 'Skyworks', 'url': 'https://careers.skyworksinc.com/services/recruiting/v1/jobs', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"keywords":"","locale":"en_US","location":"","pageNumber":0,"sortBy":"recent"}', 'list_path': 'jobSearchResult', 'title_key': 'response.unifiedStandardTitle', 'link_template': 'https://careers.skyworksinc.com/job/{response.urlTitle}', 'loc_key': 'response.jobLocationShort'},
    {'company': 'Luma AI', 'url': 'https://jobs.gem.com/api/public/graphql', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"operationName":"JobBoardList","variables":{"boardId":"lumalabs-ai"},"query":"query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) { jobPostings { id extId title locations { id name city isoCountry isRemote extId } job { id department { id name extId } locationType employmentType } } } }"}', 'list_path': 'data.oatsExternalJobPostings.jobPostings', 'title_key': 'title', 'link_template': 'https://jobs.lever.co/lumalabs.ai/{extId}', 'loc_key': 'locations.0.name'},
    {'company': 'DocuSign', 'url': 'https://careers.docusign.com/api/jobs?cid=docusign', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.full_location', 'desc_key': 'data.description'},
    {'company': 'ByteDance', 'url': 'https://jobs.bytedance.com/api/v1/public/supplier/search/job/posts', 'method': 'POST', 'headers': {'accept-language': 'en-US', 'website-path': 'en', 'Content-Type': 'application/json'}, 'body': '{"keyword":"intern","limit":10,"offset":0,"job_category_id_list":[],"tag_id_list":[],"location_code_list":[],"subject_id_list":[],"recruitment_id_list":[]}', 'list_path': 'data.job_post_list', 'title_key': 'title', 'link_template': 'https://jobs.bytedance.com/en/position/{id}/detail', 'loc_key': 'city_info.en_name', 'desc_key': 'description'},
    {'company': 'Teradata', 'url': 'https://careers.teradata.com/graphql', 'method': 'POST', 'headers': {'Content-Type': 'application/json'}, 'body': '{"operationName":"searchJobs","query":"query searchJobs($query: String, $filters: GoogleJobDiscoverySearchFiltersInput, $first: Int) { searchJobs: searchGoogleJobDiscovery(query: $query, filters: $filters, first: $first) { results { totalCount nodes { id title postedOn workplaceType primaryPlace { name } } } } }","variables":{"query":"","first":50}}', 'list_path': 'data.searchJobs.results.nodes', 'title_key': 'title', 'link_template': 'https://careers.teradata.com/jobs/{id}', 'loc_key': 'primaryPlace.name'},
    {'company': 'Group One Trading', 'url': 'https://group1.applicantpro.com/core/jobs/489?getParams=%7B%22cityUrl%22%3A%22%22%2C%22countryAbbreviation%22%3A%22%22%2C%22stateAbbreviation%22%3A%22%22%2C%22isInternal%22%3A0%7D', 'method': 'GET', 'list_path': 'data.jobs', 'title_key': 'title', 'link_key': 'jobUrl', 'loc_key': 'jobLocation'},
    {'company': 'Yelp', 'url': 'https://www.yelp.careers/us/en/search-results?keywords=intern&format=json', 'method': 'GET', 'list_path': 'ddoResults.eagerLoadRefineSearch.data.jobs', 'title_key': 'title', 'link_key': 'applyUrl', 'loc_key': 'cityState', 'desc_key': 'descriptionTeaser'},
    {'company': 'Wayfair', 'url': 'https://www.wayfair.com/a/careers/careers/job_search_data', 'method': 'POST', 'headers': {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*', 'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://www.wayfair.com/careers/jobs'}, 'body': '{"categoryIds":[],"teamIds":[],"locationIds":[],"countryIds":[],"teamCategoryIds":[],"stateIds":[],"selectedJobTypeIds":[],"keywords":""}', 'list_path': 'jobListData', 'title_key': 'title', 'link_key': 'applyLink', 'loc_key': 'location.name', 'desc_key': 'description'},
    {'company': 'Susquehanna International Group', 'url': 'https://careers.sig.com/api/jobs?page=1&limit=100', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.city', 'desc_key': 'data.description'},
    {'company': 'Alibaba', 'url': 'https://talent.alibaba.com/position/search', 'method': 'POST', 'headers': {'Content-Type': 'application/json', 'Cookie': 'XSRF-TOKEN=fixed-scraper-token', 'X-XSRF-TOKEN': 'fixed-scraper-token', 'Referer': 'https://careers.alibaba.com/', 'Origin': 'https://careers.alibaba.com'}, 'body': '{"batchId":"","corpCode":"","categoryType":"social","pageIndex":1,"pageSize":50,"channel":"group_overseas_official_site","language":"en"}', 'list_path': 'content.datas', 'title_key': 'name', 'link_template': 'https://talent.alibaba.com{positionUrl}', 'loc_key': 'workLocations', 'desc_key': 'description'},
    {'company': 'Color Health', 'url': 'https://api.careerpuck.com/v1/public/job-boards/color-health', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'title', 'link_key': 'publicUrl', 'loc_key': 'location', 'desc_key': 'content'},
    {'company': 'Naver', 'url': 'https://recruit.navercorp.com/rcrt/loadJobList.do?lang=en&firstIndex=0&sw=', 'method': 'GET', 'list_path': 'list', 'title_key': 'annoSubject', 'link_key': 'jobDetailLink'},
    {'company': 'LINE', 'url': 'https://careers.linecorp.com/page-data/jobs/page-data.json', 'method': 'GET', 'list_path': 'result.data.allStrapiJobs.edges', 'title_key': 'node.title', 'link_template': 'https://careers.linecorp.com/jobs/{node.strapiId}', 'loc_key': 'node.cities.0.name'},
    {'company': 'Shein', 'url': 'https://careers.shein.com/api/v1/open/grw/front/jobPage', 'method': 'POST', 'body': '{"current":1,"size":100,"cityName":"","jobCategoryIds":[],"cityIds":[],"jobTypeIds":[],"key":"intern","langCode":"EN"}', 'list_path': 'info.records', 'title_key': 'jobTitle', 'link_key': 'jobDetailUrl', 'loc_key': 'countryName', 'desc_key': 'description'},
    {'company': 'Yandex', 'url': 'https://yandex.com/jobs/api/publications', 'method': 'GET', 'list_path': 'results', 'title_key': 'title', 'link_template': 'https://yandex.com/jobs/vacancies/{publication_slug_url}', 'loc_key': 'vacancy.cities.0.name', 'desc_key': 'short_summary'},
    {'company': 'Garmin', 'url': 'https://careers.garmin.com/api/jobs?limit=100', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.short_location', 'desc_key': 'data.description'},
    {'company': 'Baidu', 'url': 'https://talent.baidu.com/httservice/getPostListNew', 'method': 'POST', 'headers': {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8', 'Referer': 'https://talent.baidu.com/jobs/campus'}, 'body': 'recruitType=INTERN&pageSize=10&keyWord=&curPage=1&projectType=', 'list_path': 'data.list', 'title_key': 'name', 'link_template': 'https://talent.baidu.com/jobs/detail/INTERN/{postId}', 'loc_key': 'workPlace', 'desc_key': 'workContent'},
    {'company': 'Two Sigma', 'url': 'https://careers.twosigma.com/careers/OpenRoles', 'method': 'GET', 'row_regex': '<article class="article article--result".*?href="(?P<url>[^"]+)">\s*(?P<title>[^<]+?)\s*</a>.*?<span class="paragraph_inner-span">(?P<loc>[^<]+)</span>', 'url_prefix': 'https://careers.twosigma.com'},
    {'company': 'Citadel', 'url': 'https://www.citadel.com/career-sitemap.xml', 'method': 'GET', 'row_regex': '<loc>(?P<url>https://www\.citadel\.com/careers/details/(?P<title>[^/<]+))/</loc>'},
    {'company': 'SAP', 'url': 'https://jobs.sap.com/search/?q=intern', 'method': 'GET', 'row_regex': '<td class="colTitle"[^>]*>\s*<span class="jobTitle hidden-phone">\s*<a href="(?P<url>[^"]+)" class="jobTitle-link">(?P<title>[^<]+)</a>\s*</span>.*?<span class="jobLocation">\s*(?P<loc>[^<]+?)\s*</span>', 'url_prefix': 'https://jobs.sap.com'},
    {'company': 'Synopsys', 'url': 'https://synopsys.avature.net/careers/SearchJobs/feed/?jobRecordsPerPage=20', 'method': 'GET', 'row_regex': '<item>\s*<title><!\[CDATA\[(?P<title>.*?)\]\]></title>.*?<link>(?P<url>.*?)</link>', 'url_prefix': 'https://synopsys.avature.net'},
    {'company': 'Renaissance Technologies', 'url': 'https://www.rentec.com/Careers.action?jobs=true', 'method': 'GET', 'row_regex': '<a class="Link--primary color-fg-default" href="(?P<url>/Careers\.action\?jobs=true&selectedPosition=[^"]+)">(?P<title>[^<]+)</a>\s*</div>\s*<div>(?P<loc>[^<]+)</div>', 'url_prefix': 'https://www.rentec.com'},
    {'company': 'Intuit', 'url': 'https://jobs.intuit.com/search-jobs?k=intern&p=1&orgIds=27595', 'method': 'GET', 'row_regex': '<a href="(?P<url>/job/[^"]+)" data-job-id="\d+" class="sr-item"[^>]*data-title="(?P<title>[^"]+)"[^>]*>\s*<h2>[^<]*</h2>\s*(?:<span class="job-location">(?P<loc>[^<]*)</span>)?', 'url_prefix': 'https://jobs.intuit.com'},
    {'company': 'Bloomberg', 'url': 'https://bloomberg.avature.net/careers/SearchJobs/?search=intern&jobRecordsPerPage=50&jobOffset=0', 'method': 'GET', 'row_regex': '<a class="link" href="(?P<url>https://bloomberg\.avature\.net/careers/JobDetail/[^"]+)">\s*(?P<title>[^<]+?)\s*</a>\s*</h3>.*?<span class="list-item-location">(?P<loc>[^<]*)</span>', 'url_prefix': 'https://bloomberg.avature.net'},
    {'company': 'AMD', 'url': 'https://amd.jibeapply.com/api/jobs?limit=100&keywords=intern', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.short_location', 'desc_key': 'data.description'},
    {'company': 'Sakana AI', 'url': 'https://sakana.ai/careers/', 'method': 'GET', 'row_regex': '<a class="career-card" href="(?P<url>/careers/[^"]+)">.*?<h3 class="career-card-title">(?P<title>[^<]+)</h3>\s*<p class="career-card-desc">(?P<desc>.*?)</p>\s*<ul class="career-card-meta">\s*<li>(?P<loc>[^<]*)</li>', 'url_prefix': 'https://sakana.ai'},
    {'company': 'Qualcomm', 'url': 'https://app.eightfold.ai/api/pcsx/search?domain=qualcomm.com&query=intern&location=&start=0&num=50', 'method': 'GET', 'list_path': 'data.positions', 'title_key': 'name', 'link_template': 'https://app.eightfold.ai{positionUrl}', 'loc_key': 'locations'},
    {'company': 'Lattice Semiconductor', 'url': 'https://careers-latticesemi.icims.com/jobs/search?ss=1&in_iframe=1', 'method': 'GET', 'row_regex': '<li class="iCIMS_JobCardItem">.*?Job Locations</span>\s*<span[^>]*>\s*(?P<loc>.*?)</span>.*?<a href="(?P<url>[^"]+)"[^>]*>.*?<h3[^>]*>\s*(?P<title>.*?)</h3>.*?<div class="col-xs-12 description">(?P<desc>.*?)</div>'},
    {'company': 'Qorvo', 'url': 'https://careers.qorvo.com/search/?q=intern', 'method': 'GET', 'row_regex': '<tr class="data-row">.*?<a href="(?P<url>/job/[^"]+)" class="jobTitle-link">(?P<title>[^<]+)</a>.*?class="colLocation hidden-phone"[^>]*>\s*<span class="jobLocation">\s*(?P<loc>[^<]+?)\s*</span>', 'url_prefix': 'https://careers.qorvo.com'},
    {'company': 'Procore', 'url': 'https://careers.procore.com/jobs/search', 'method': 'GET', 'row_regex': 'data-job-url="(?P<url>[^"]+)"[^>]*>\s*<td class="job-search-results-title">\s*<a[^>]*aria-label="Title: (?P<title>[^"]+)"[^>]*href="[^"]*"[^>]*>.*?<td class="job-search-results-location">\s*<ul>\s*<li[^>]*>(?P<loc>[^<]*)</li>', 'url_prefix': 'https://careers.procore.com'},
    {'company': 'Seagate', 'url': 'https://seagatecareers.com/search/?q=intern', 'method': 'GET', 'row_regex': '<li class="job-tile job-id-\d+[^"]*" data-url="(?P<url>[^"]+)".*?class="jobTitle-link[^"]*"[^>]*>\s*(?P<title>[^<]+?)\s*</a>.*?section-location-value">(?P<loc>[^<]+)', 'url_prefix': 'https://seagatecareers.com'},
    {'company': 'Microsoft', 'url': 'https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&start=0&num=10&query=intern', 'method': 'GET', 'list_path': 'data.positions', 'title_key': 'name', 'link_key': 'positionUrl', 'loc_key': 'locations'},
    {'company': 'Atlassian', 'url': 'https://join.atlassian.com/api/jobs?limit=100&keywords=intern', 'method': 'GET', 'list_path': 'jobs', 'title_key': 'data.title', 'link_key': 'data.apply_url', 'loc_key': 'data.full_location', 'desc_key': 'data.description'},
    {'company': 'Lam Research', 'url': 'https://lamresearch.eightfold.ai/api/pcsx/search?domain=lamresearch.com&start=0&num=100&query=intern', 'method': 'GET', 'list_path': 'data.positions', 'title_key': 'name', 'link_key': 'positionUrl', 'link_template': 'https://lamresearch.eightfold.ai{positionUrl}', 'loc_key': 'standardizedLocations'},
    {'company': 'Wise', 'url': 'https://wise.jobs/jobs?q=intern', 'method': 'GET', 'row_regex': 'attrax-vacancy-tile__title[^"]*" href="(?P<url>/job/[^"]+)"[^>]*>(?P<title>[^<]+)</a>.*?attrax-vacancy-tile__location-freetext.*?attrax-vacancy-tile__item-value">\s*(?P<loc>[^<]+?)\s*</p>', 'url_prefix': 'https://wise.jobs'},
    {'company': 'Fidelity Investments', 'url': 'https://jobs.fidelity.com/en/jobs/', 'method': 'GET', 'headers': {'User-Agent': 'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)'}, 'row_regex': '<div class="card card-job"[^>]*>.*?<a class="stretched-link js-view-job" href="(?P<url>/en/jobs/[^"]+)">(?P<title>[^<]+)</a>.*?<li class="list-inline-item">\s*(?P<loc>[A-Za-z][^<]*?)\s*</li>\s*<li class="list-inline-item work-pattern">', 'url_prefix': 'https://jobs.fidelity.com'},
    {'company': 'Google', 'url': 'https://www.google.com/about/careers/applications/jobs/results/?q=intern', 'method': 'GET', 'extract_regex': "key:\s*'ds:1'.*?data:(\[.*\]),\s*sideChannel", 'list_path': '0', 'title_key': '1', 'link_key': '2', 'loc_key': '9.0.0', 'desc_key': '3.1'},
    {'company': 'Valve', 'url': 'https://www.valvesoftware.com/en/jobs', 'method': 'GET', 'row_regex': '<div class="job_opening[^"]*">\s*<a href="(?P<url>[^"]+)">\s*<h5 class="job_title">\s*(?P<title>[^<]+?)\s*</h5>'},
    {'company': 'Opendoor', 'url': 'https://www.opendoor.com/careers/open-positions', 'method': 'GET', 'row_regex': '<a[^>]*href="(?P<url>/careers/open-positions/jobs/[^"]+)"[^>]*><div><h3[^>]*>(?P<title>[^<]+)</h3></div><p[^>]*>(?P<loc>[^<]+)</p></a>', 'url_prefix': 'https://www.opendoor.com'},
    {'company': 'G-Research', 'url': 'https://www.gresearch.com/vacancies/', 'method': 'GET', 'row_regex': '<a href="(?P<url>[^"]+)" class="c-vacancy-result">\s*<span class="c-vacancy-result__title">(?P<title>[^<]+)</span>\s*(?:<span class="c-vacancy-result__location">(?P<loc>[^<]*)</span>)?'},
    {'company': 'Nutanix', 'url': 'https://careers.nutanix.com/sitemap.xml', 'method': 'GET', 'row_regex': '<loc>(?P<url>https://careers\.nutanix\.com/en/jobs/\d+/(?P<title>[^/<]+))/</loc>', 'url_prefix': 'https://careers.nutanix.com'},
    {'company': 'Siemens EDA', 'url': 'https://prod-search-api.jobsyn.org/api/v1/solr/search?num_items=50&q=intern', 'method': 'GET', 'headers': {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Origin': 'jobs.sw.siemens.com'}, 'list_path': 'jobs', 'title_key': 'title_exact', 'link_template': 'https://jobs.sw.siemens.com/{title_slug}/{title_slug}/{guid}/job/', 'loc_key': 'location_exact', 'desc_key': 'description'},
]


class CustomBoardsSource:
    name = NAME

    def __init__(self, config=None, interval=1.0, workers=12):
        self.config = CONFIG if config is None else config
        self.interval = interval
        self.workers = workers

    def fetch(self) -> list:
        throttle = DomainThrottle(self.interval)

        def run(spec):
            with throttle.hold(spec["url"]):
                return spec_to_listings(spec)

        listings = []
        if self.config:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                for got in ex.map(run, self.config):
                    listings.extend(got)
        for company, (url, fn) in DECODERS.items():
            try:
                with throttle.hold(url):
                    listings.extend(fn())
            except Exception:  # noqa: BLE001 - a broken decoder yields nothing, not a crash
                pass
        return listings


def selftest():
    import sys
    _self = sys.modules[__name__]

    assert _dig({"a": {"b": [{"c": 1}]}}, "a.b.0.c") == 1
    assert _dig({"a": 1}, "a.b") is None
    assert _dig({"x": [1, 2]}, "x.5") is None
    assert _fill("https://h/job/{id}", {"id": 42}) == "https://h/job/42"
    assert _fill("https://h/{a.b}", {"a": {"b": "x"}}) == "https://h/x"

    # a location that digs to a list (Eightfold shape) takes the first string
    assert _loc(["NYC", "SF"]) == "NYC"
    assert _loc("SF") == "SF"
    assert _loc([]) == "" and _loc(None) == ""

    # a relative link_key/url is made absolute -- via url_prefix, else the
    # origin of the spec's own endpoint
    assert _abs_url("/careers/job/1", {"url": "https://x.test/api/s"}) == "https://x.test/careers/job/1"
    assert _abs_url("/j/1", {"url": "https://x.test/api", "url_prefix": "https://apply.x.test"}) == "https://apply.x.test/j/1"
    assert _abs_url("https://x.test/j/1", {"url": "https://x.test/api"}) == "https://x.test/j/1"  # already absolute, untouched

    # generic spec: dig to a nested array, map dotted keys + a URL template
    payload = {"data": {"jobs": [
        {"t": "SWE Intern", "id": 7, "city": "SF", "body": "desc"},
        {"t": "", "id": 8},          # blank title -> skipped
        {"notitle": True},           # missing title -> skipped
    ]}}
    spec = {"company": "Acme", "url": "https://acme.test/api", "list_path": "data.jobs",
            "title_key": "t", "loc_key": "city", "desc_key": "body",
            "link_template": "https://acme.test/job/{id}"}
    orig = _self.fetch_text
    _self.fetch_text = lambda s: json.dumps(payload)
    try:
        got = spec_to_listings(spec)
    finally:
        _self.fetch_text = orig
    assert len(got) == 1, got
    l = got[0]
    assert (l.source, l.company, l.title, l.location, l.url, l.extra_text) == \
        ("custom_boards", "Acme", "SWE Intern", "SF", "https://acme.test/job/7", "desc"), l

    # extract_regex: pull an embedded JSON blob out of an HTML response first
    html = '<html><script id="d">{"jobs":[{"t":"ML Intern","u":"/j/1"}]}</script></html>'
    spec2 = {"company": "Beta", "url": "https://beta.test/careers",
             "extract_regex": r'id="d">(.*?)</script>', "list_path": "jobs",
             "title_key": "t", "link_key": "u"}
    _self.fetch_text = lambda s: html
    try:
        got2 = spec_to_listings(spec2)
    finally:
        _self.fetch_text = orig
    # relative link_key is absolutized to the spec endpoint's origin
    assert len(got2) == 1 and got2[0].title == "ML Intern" and got2[0].url == "https://beta.test/j/1", got2

    # a spec whose shape no longer matches (wrong list_path) degrades to []
    _self.fetch_text = lambda s: json.dumps(payload)
    try:
        assert spec_to_listings({**spec, "list_path": "data.nope"}) == []
    finally:
        _self.fetch_text = orig

    # positional-array jobs (Google cportal shape): list_path lands on a list
    # of lists, each job addressed by numeric index rather than dict key
    _self.fetch_text = lambda s: json.dumps([[["x", "SWE Intern", "http://g/1", "SF"]]])
    try:
        gg = spec_to_listings({"company": "G", "url": "https://g.test", "list_path": "0",
                               "title_key": "1", "link_key": "2", "loc_key": "3"})
    finally:
        _self.fetch_text = orig
    assert len(gg) == 1 and gg[0].title == "SWE Intern" and gg[0].url == "http://g/1" and gg[0].location == "SF", gg

    # row_regex mode: parse a repeating job-card pattern out of raw HTML,
    # strip inner tags from the title, and prepend url_prefix to relative links
    html2 = ('<li class="card"><a href="/job/1">SWE <b>Intern</b></a>'
             '<span class="loc">NYC</span></li>'
             '<li class="card"><a href="/job/2">PM Intern</a>'
             '<span class="loc">SF</span></li>')
    spec3 = {"company": "Gamma", "url": "https://gamma.test/jobs", "url_prefix": "https://gamma.test",
             "row_regex": r'<a href="(?P<url>[^"]+)">(?P<title>.*?)</a>'
                          r'<span class="loc">(?P<loc>[^<]+)</span>'}
    _self.fetch_text = lambda s: html2
    try:
        g3 = spec_to_listings(spec3)
    finally:
        _self.fetch_text = orig
    assert len(g3) == 2, g3
    assert g3[0].title == "SWE Intern" and g3[0].url == "https://gamma.test/job/1" and g3[0].location == "NYC", g3[0]
    assert g3[1].title == "PM Intern", g3[1]

    # a link_key that resolves to a non-string leaves url empty, not crashing
    _self.fetch_text = lambda s: json.dumps({"jobs": [{"t": "Intern", "u": {"nested": 1}}]})
    try:
        g = spec_to_listings({"company": "C", "url": "https://c.test", "list_path": "jobs",
                              "title_key": "t", "link_key": "u"})
    finally:
        _self.fetch_text = orig
    assert len(g) == 1 and g[0].url == "", g

    # Tesla state decoder: compressed keys + location/dept lookup tables.
    # Fixture mirrors the live /cua-api/apps/careers/state shape (Wayback-
    # verified 2025-05); live fetch is Akamai-gated so CI never depends on it.
    tesla_payload = {
        "lookup": {
            "locations": {"401022": "Palo Alto, California", "16964": "Fremont, California"},
            "departments": {"9": "AI & Robotics", "2": "Sales & Customer Support"},
            "types": {"1": "fulltime", "3": "intern"},
        },
        "listings": [
            {"id": "243692", "t": "Software Engineer, Mobile App, Vehicle Software",
             "dp": "9", "l": "401022", "y": 1},
            {"id": "242901", "t": "Internship, Software Integration Engineer, Service (Fall 2025)",
             "dp": "2", "l": "16964", "y": 3},
            {"id": "999", "t": "", "dp": "9", "l": "401022", "y": 3},  # blank title -> skip
            {"id": "260509", "t": "Co-op Firmware Engineer",
             "dp": "9", "l": ["401022", "16964"], "y": 3},  # multi-location
        ],
    }
    tg = listings_from_tesla_state(tesla_payload)
    assert len(tg) == 3, tg
    assert all(l.source == NAME and l.company == "Tesla" for l in tg), tg
    assert tg[0].title.startswith("Software Engineer") and tg[0].location == "Palo Alto, California"
    assert tg[0].url == (
        "https://www.tesla.com/careers/search/job/"
        "software-engineer-mobile-app-vehicle-software-243692"
    ), tg[0].url
    assert tg[0].extra_text == "AI & Robotics"
    assert "internship-software-integration" in tg[1].url and tg[1].location == "Fremont, California"
    assert tg[2].url.endswith("-260509") and "Palo Alto" in tg[2].location and "Fremont" in tg[2].location
    assert listings_from_tesla_state({"cpr_chlge": "true"}) == []
    assert listings_from_tesla_state({"listings": "nope"}) == []
    assert "Tesla" in DECODERS and DECODERS["Tesla"][0] == _TESLA_STATE_URL
    # numeric id (JSON number) must still produce a listing + stable URL
    tg_num = listings_from_tesla_state({
        "lookup": {"locations": {"1": "Austin, Texas"}, "departments": {}},
        "listings": [{"id": 260509, "t": "Firmware Intern", "dp": None, "l": 1, "y": 3}],
    })
    assert len(tg_num) == 1 and tg_num[0].url.endswith("-260509"), tg_num
    assert tg_num[0].location == "Austin, Texas", tg_num[0].location

    print("custom_boards selftest OK")


if __name__ == "__main__":
    selftest()
