"""Orchestrator: fetch every source, filter to SWE internships, drop
anything already seen from a prior run, return what's new, and stage
seen-id updates for persist_seen(). One run = one call to run().

ats_boards runs first, alone -- it usually carries a description from each
ATS's own JSON API (Workday detail-fetched when needed). Remaining sources
run in parallel; README sources skip URLs ats_boards already found this run.
For every source, any listing that still has empty extra_text gets a full
apply-page HTML download (throttled per domain) stored as its description.

Seen-id writes are deferred until RunResult.persist_seen() so a crash or
failed out/all.json write after a successful fetch cannot permanently
strand new listings as "already seen" without ever recording them.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from internships.filters import is_swe_internship, year_relevance
from internships.models import Listing, normalize_url
from internships.seen_store import SeenStore
from internships.sources.ats_boards import DomainThrottle, UA


class _Redirect308(HTTPRedirectHandler):
    """Python 3.10's urllib follows 301/302/303/307 but not 308. Many career
    sites (Instacart, SentinelOne, D.E. Shaw, …) use 308 Permanent Redirect;
    without this, description fetches raise and we store nothing.

    Must also allow 308 in redirect_request — calling http_error_302 alone is
    not enough because redirect_request rejects code 308 on 3.10."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code == 308:
            code = 307  # same semantics for our GET fetches
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, 307, msg, headers)


_OPENER = build_opener(_Redirect308)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# README-table sources have no body in the table; they also use ats known_urls
# cross-source skip. Description page-fetch now runs for *every* source when
# extra_text is empty (including custom_boards / sparse ATS rows).
README_SOURCES = {"github_readme", "speedyapply", "sndsh404"}
# Back-compat alias used by older call sites / docs.
DESCRIPTION_FALLBACK_SOURCES = README_SOURCES

_PAGE_FETCH_THROTTLE = DomainThrottle(1.0)
_PAGE_FETCH_TIMEOUT = 25


@dataclass
class SourceResult:
    fetched: int = 0
    swe: int = 0
    new: int = 0
    error: str = ""


@dataclass
class RunResult:
    new_listings: list = field(default_factory=list)
    per_source: dict = field(default_factory=dict)  # name -> SourceResult
    # Pending seen-id writes, one (path, id-set) per successful source. Applied
    # by persist_seen() -- either at the end of run() (default) or by the
    # caller after a successful out/all.json write (persist_seen=False), so a
    # crash/exception between fetch and the dataset write cannot permanently
    # strand new listings as "already seen" without ever recording them.
    _seen_updates: list = field(default_factory=list)

    def persist_seen(self):
        for path, ids in self._seen_updates:
            SeenStore(path).save(ids)


def _fetch_raw_page(url: str) -> str:
    """Best-effort full-page fetch of a listing apply URL.

    Stores whatever HTML/text the server returns (whole page) for later use
    (e.g. paste into an AI). Empty string on any failure. Per-domain throttle
    via DomainThrottle.hold so we don't stampede boards. Follows 308 redirects
    (not handled by Python 3.10's default urllib opener)."""
    if not url:
        return ""
    try:
        with _PAGE_FETCH_THROTTLE.hold(url):
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            with _OPENER.open(req, timeout=_PAGE_FETCH_TIMEOUT) as r:
                return r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort; missing page just means no description
        return ""


def _with_raw_description(l):
    """If the listing has no body yet, download the full apply-page HTML."""
    return l if l.extra_text else replace(l, extra_text=_fetch_raw_page(l.url))


def _run_one(source, data_dir, known_urls=frozenset(), fetch_missing_description=True):
    try:
        listings = source.fetch()
        # filtering also happens inside the try: a bad non-str field from a
        # misbehaving source (e.g. a list-valued location) can throw out of
        # year_relevance()'s regex calls, and letting that escape here would
        # crash the whole ex.map() loop in run(). Seen-id writes are deferred
        # (returned as pending, not saved here) so a mid-run failure or a
        # crash before out/all.json is updated cannot strand new listings.
        swe = [l for l in listings if is_swe_internship(l.title)
               and year_relevance(l.title, l.location, l.extra_text) != "no"]
        swe = list({l.id(): l for l in swe}.values())  # dedupe within this batch (e.g. a listing appearing in 2 README table sections)
        if known_urls:
            swe = [l for l in swe if normalize_url(l.url) not in known_urls]
        # Always page-fetch when extra_text is still empty (API/spec had no body).
        if fetch_missing_description:
            with ThreadPoolExecutor(max_workers=8) as ex:
                swe = list(ex.map(_with_raw_description, swe))
        store = SeenStore(data_dir / "seen" / f"{source.name}.json")
        seen = store.load()
        new = [l for l in swe if l.id() not in seen]
        pending = (store.path, seen | {l.id() for l in swe})
    except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
        return source.name, [], [], SourceResult(error=str(e)), None
    return (source.name, new, swe,
            SourceResult(fetched=len(listings), swe=len(swe), new=len(new)), pending)


def run(sources, data_dir=DATA_DIR, persist_seen=True) -> RunResult:
    """Fetch/filter/dedup every source and return what's new.

    Seen-id updates are computed per source but not written until
    `RunResult.persist_seen()`. By default that runs at the end of this
    function (so selftests and one-shot callers keep working). Pass
    `persist_seen=False` when the caller still has to write out/all.json --
    then call `result.persist_seen()` only after that write succeeds, so a
    failed/crashed append cannot permanently drop the new rows.
    """
    result = RunResult()
    ats = next((s for s in sources if s.name == "ats_boards"), None)
    rest = [s for s in sources if s is not ats]

    known_urls = frozenset()
    if ats is not None:
        # Page-fetch any ATS row whose JSON/detail body is still empty.
        name, new, swe, stats, pending = _run_one(ats, data_dir, fetch_missing_description=True)
        result.per_source[name] = stats
        result.new_listings.extend(new)
        if pending is not None:
            result._seen_updates.append(pending)
        # exclude empty: normalize_url("") is "", and a url-less ats listing
        # (a job whose JSON had no url key) must not poison known_urls into
        # dropping every url-less fallback-source listing as a false duplicate.
        known_urls = frozenset(u for l in swe if (u := normalize_url(l.url)))

    def work(s):
        # README sources skip URLs already found by ats_boards this run.
        # Every source page-fetches when extra_text is empty (incl. custom_boards).
        use_known = s.name in README_SOURCES
        return _run_one(s, data_dir, known_urls=known_urls if use_known else frozenset(),
                         fetch_missing_description=True)

    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as ex:
            for name, new, swe, stats, pending in ex.map(work, rest):
                result.per_source[name] = stats
                result.new_listings.extend(new)
                if pending is not None:
                    result._seen_updates.append(pending)

    result.new_listings.sort(key=lambda l: (not l.is_2027, l.company, l.title))
    if persist_seen:
        result.persist_seen()
    return result


def selftest():
    class FakeSource:
        def __init__(self, listings, name="fake"):
            self.name = name
            self._listings = listings
        def fetch(self):
            return self._listings

    import sys
    import tempfile
    # Offline by default: page-fetch would hit fake http://a/* URLs.
    _self = sys.modules[__name__]
    _orig_fetch = _self._fetch_raw_page
    _self._fetch_raw_page = lambda url: ""
    d = Path(tempfile.mkdtemp())
    listings = [
        Listing("fake", "Acme", "Software Engineer Intern", "SF", "http://a/1"),
        Listing("fake", "Acme", "Marketing Intern", "SF", "http://a/2"),  # filtered: not SWE
        Listing("fake", "Acme", "SWE Intern Summer 2026", "SF", "http://a/3"),  # for-sure not 2027
    ]
    r1 = run([FakeSource(listings)], data_dir=d)
    assert len(r1.new_listings) == 1 and r1.new_listings[0].company == "Acme"
    assert r1.per_source["fake"] == SourceResult(fetched=3, swe=1, new=1)

    r2 = run([FakeSource(listings)], data_dir=d)  # same listings again -> nothing new
    assert r2.new_listings == []
    assert r2.per_source["fake"] == SourceResult(fetched=3, swe=1, new=0)

    class BrokenSource:
        name = "broken"
        def fetch(self):
            raise RuntimeError("boom")
    r3 = run([BrokenSource()], data_dir=d)
    assert r3.new_listings == [] and r3.per_source["broken"].error == "boom"

    # a source returning the same listing twice in one fetch() (e.g. it appears
    # in two README table sections) must not produce duplicate new_listings
    dupe = Listing("fake", "Acme", "Software Engineer Intern", "SF", "http://a/1")
    r4 = run([FakeSource([dupe, dupe])], data_dir=Path(tempfile.mkdtemp()))
    assert len(r4.new_listings) == 1
    assert r4.per_source["fake"] == SourceResult(fetched=2, swe=1, new=1)

    # a source returning a listing with a non-str field (e.g. a list-valued
    # location, as ats_boards._get_location can produce from some fallback
    # branches) must degrade to a per-source error, not crash run() and
    # strand other sources' already-persisted seen-ids.
    class BadFieldSource:
        name = "badfield"
        def fetch(self):
            return [Listing("badfield", "Weird Co", "Software Engineer Intern",
                             ["SF", "NYC"], "http://b/1")]
    r5 = run([FakeSource(listings), BadFieldSource()], data_dir=d)
    assert r5.per_source["badfield"].error
    assert r5.per_source["fake"] == SourceResult(fetched=3, swe=1, new=0)  # already seen from r1/r2

    # ats_boards runs first and its listings become known_urls: a
    # github_readme listing with the exact same link (tracking param and
    # all -- normalize_url strips utm_*/ref/etc. but keeps gh_jid) is
    # skipped outright, while a genuinely different listing falls back to
    # a raw page fetch.
    import sys
    _self = sys.modules[__name__]  # not a fresh dotted import -- see ats_boards.py's
    # own selftest for why: this file runs as __main__ under --selftest, a
    # different module object than "internships.service" would be.
    orig_fetch_raw_page = _self._fetch_raw_page
    calls = []
    def fake_fetch(url):
        calls.append(url)
        return "raw page text"
    _self._fetch_raw_page = fake_fetch
    try:
        # ATS already has a JSON body -- should not page-fetch. Empty-body
        # github row for a new URL should page-fetch once.
        ats_listing = Listing("ats_boards", "Acme", "Software Engineer Intern", "SF",
                              "http://ats/1?utm=x", extra_text="from ats json")
        dupe_listing = Listing("github_readme", "Acme", "Software Engineer Intern", "SF", "http://ats/1")
        new_listing = Listing("github_readme", "Beta", "Software Engineer Intern", "NYC", "http://beta/1")
        r6 = run([FakeSource([ats_listing], name="ats_boards"),
                  FakeSource([dupe_listing, new_listing], name="github_readme")],
                 data_dir=Path(tempfile.mkdtemp()))
    finally:
        _self._fetch_raw_page = _orig_fetch

    assert len(r6.new_listings) == 2
    by_company = {l.company: l for l in r6.new_listings}
    assert by_company["Acme"].source == "ats_boards"  # the github_readme dupe was skipped
    assert by_company["Beta"].source == "github_readme"
    assert by_company["Beta"].extra_text == "raw page text"  # fell back to raw fetch
    assert calls == ["http://beta/1"]  # never fetched for the skipped duplicate

    # a url-less ats listing (ats_boards.job_to_listing emits url="" when a
    # job's JSON has no url key) must NOT poison known_urls: normalize_url("")
    # is "", so without excluding it every url-less fallback-source listing
    # (github_readme rows whose application cell has no <a href>) would be
    # dropped as a false "duplicate" of it. Both distinct listings must survive.
    _self._fetch_raw_page = lambda url: ""
    try:
        ats_nourl = Listing("ats_boards", "Acme", "Software Engineer Intern", "SF", "")
        gh_nourl = Listing("github_readme", "Gamma", "Software Engineer Intern", "LA", "")
        r7 = run([FakeSource([ats_nourl], name="ats_boards"),
                  FakeSource([gh_nourl], name="github_readme")],
                 data_dir=Path(tempfile.mkdtemp()))
    finally:
        _self._fetch_raw_page = _orig_fetch
    assert len(r7.new_listings) == 2, r7.new_listings  # neither url-less listing dropped
    assert {l.company for l in r7.new_listings} == {"Acme", "Gamma"}

    # persist_seen=False: seen ids must NOT be written until the caller says
    # so. Simulates the production path where out/all.json is written after
    # run() returns -- a crash before that write used to permanently drop
    # the new listings (they were already in the seen store).
    d_defer = Path(tempfile.mkdtemp())
    listing = Listing("fake", "Acme", "Software Engineer Intern", "SF", "http://defer/1",
                      extra_text="body")
    r8 = run([FakeSource([listing])], data_dir=d_defer, persist_seen=False)
    assert len(r8.new_listings) == 1
    assert SeenStore(d_defer / "seen" / "fake.json").load() == set()  # not written yet
    # "crash" -- caller never writes all.json and never calls persist_seen.
    # Next run must still surface the listing as new.
    r9 = run([FakeSource([listing])], data_dir=d_defer, persist_seen=False)
    assert len(r9.new_listings) == 1
    r9.persist_seen()
    assert SeenStore(d_defer / "seen" / "fake.json").load() == {listing.id()}
    r10 = run([FakeSource([listing])], data_dir=d_defer, persist_seen=False)
    assert r10.new_listings == []

    # 308 Permanent Redirect is handled (Python 3.10 default opener is not)
    assert callable(getattr(_Redirect308, "http_error_308", None))

    print("service selftest OK")


if __name__ == "__main__":
    selftest()
