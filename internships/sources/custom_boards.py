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


_TEMPLATE_RE = re.compile(r"\{([\w.]+)\}")


def _fill(template, job):
    """Build a URL from a job dict: '{k}' -> str(job's dotted key k)."""
    return _TEMPLATE_RE.sub(lambda m: str(_dig(job, m.group(1)) or ""), template)


def fetch_text(spec):
    """Raw response text for a spec (GET, or POST with an optional body)."""
    method = spec.get("method", "GET").upper()
    headers = {"User-Agent": UA, "Accept": "application/json", **spec.get("headers", {})}
    body = spec.get("body")
    data = body.encode() if (method == "POST" and body) else None
    if data and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    req = Request(spec["url"], data=data, method=method, headers=headers)
    with urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def spec_to_listings(spec):
    """Run one CONFIG spec -> list[Listing]. Any transport/parse failure or a
    shape that no longer matches degrades to [] rather than raising -- one
    dead company must not sink the whole source (service.py also guards, but
    keeping it here means a stale spec just yields nothing)."""
    try:
        raw = fetch_text(spec)
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
        if not isinstance(job, dict):
            continue
        title = _str(_dig(job, spec["title_key"]))
        if not title:
            continue
        if spec.get("link_template"):
            url = _fill(spec["link_template"], job)
        else:
            url = _str(_dig(job, spec.get("link_key", "")))
        loc = _str(_dig(job, spec.get("loc_key", "")))
        desc = _str(_dig(job, spec.get("desc_key", "")))
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
            throttle.wait(spec["url"])
            return spec_to_listings(spec)

        listings = []
        if self.config:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                for got in ex.map(run, self.config):
                    listings.extend(got)
        for company, (url, fn) in DECODERS.items():
            throttle.wait(url)
            try:
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
    assert len(got2) == 1 and got2[0].title == "ML Intern" and got2[0].url == "/j/1", got2

    # a spec whose shape no longer matches (wrong list_path) degrades to []
    _self.fetch_text = lambda s: json.dumps(payload)
    try:
        assert spec_to_listings({**spec, "list_path": "data.nope"}) == []
    finally:
        _self.fetch_text = orig

    # a link_key that resolves to a non-string leaves url empty, not crashing
    _self.fetch_text = lambda s: json.dumps({"jobs": [{"t": "Intern", "u": {"nested": 1}}]})
    try:
        g = spec_to_listings({"company": "C", "url": "https://c.test", "list_path": "jobs",
                              "title_key": "t", "link_key": "u"})
    finally:
        _self.fetch_text = orig
    assert len(g) == 1 and g[0].url == "", g

    print("custom_boards selftest OK")


if __name__ == "__main__":
    selftest()
