"""Orchestrator: fetch every source, filter to SWE internships, drop
anything already seen from a prior run, return what's new. One run = one
call to run(). Callers must commit_seen() only after new listings have
been successfully written (out/all.json / CSV) -- otherwise a crash or
write failure between marking ids seen and persisting listings would
permanently drop those listings (seen forever, never in the dataset).

ats_boards runs first, alone -- it already carries a full description from
each ATS's own JSON API, so its listings are the cheapest/most reliable to
get. The remaining (README-table) sources then run in parallel, each
skipping any listing whose link exactly matches one ats_boards already
found this run (same job, no need to re-report or re-fetch it), and
falling back to a raw, unparsed fetch of a listing's own page for whatever
survives that isn't a duplicate -- those sources have no description of
their own at all.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.request import Request, urlopen

from internships.filters import is_swe_internship, year_relevance
from internships.models import Listing, normalize_url
from internships.seen_store import SeenStore
from internships.sources.ats_boards import DomainThrottle, UA

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Only these sources lack any description of their own -- everything else
# (just ats_boards today) keeps its normal behavior untouched.
DESCRIPTION_FALLBACK_SOURCES = {"github_readme", "speedyapply", "sndsh404"}

_PAGE_FETCH_THROTTLE = DomainThrottle(1.0)
_PAGE_FETCH_TIMEOUT = 15
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)


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
    # (SeenStore, ids) pairs deferred until commit_seen() -- see module doc.
    _pending_seen: list = field(default_factory=list)

    def commit_seen(self):
        """Persist per-source seen-ids. Call only after new_listings have been
        successfully written to out/all.json (and CSV); skipping that order
        strands listings as permanently 'seen' but never recorded."""
        for store, ids in self._pending_seen:
            store.save(ids)
        self._pending_seen.clear()


def _fetch_raw_page(url: str) -> str:
    """Best-effort, mostly-unparsed fallback description: whatever text
    comes back for a listing's own page -- HTML tags, nav/footer noise and
    all -- is kept as-is, since this is meant to be pasted into an AI later,
    not read as-is. The one exception: <script>/<style> blocks are stripped.
    They're not description content, and they're where GitHub's secret
    scanner kept flagging real-looking-but-harmless strings baked into job
    board page templates (presigned S3 image URLs, client-side Google
    Analytics/Maps keys) -- already public on the source page either way,
    just noise we don't need to also carry around."""
    if not url:
        return ""
    _PAGE_FETCH_THROTTLE.wait(url)
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=_PAGE_FETCH_TIMEOUT) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best-effort fallback; any failure just means no description
        return ""
    return _SCRIPT_STYLE_RE.sub("", text)


def _with_raw_description(l):
    return l if l.extra_text else replace(l, extra_text=_fetch_raw_page(l.url))


def _run_one(source, data_dir, known_urls=frozenset(), fetch_missing_description=False):
    try:
        listings = source.fetch()
        # filtering also happens inside the try: a bad non-str field from a
        # misbehaving source (e.g. a list-valued location) can throw out of
        # year_relevance()'s regex calls, and letting that escape here would
        # crash the whole ex.map() loop in run() -- dropping other sources'
        # already-fetched new listings from the returned RunResult even
        # though their seen-ids are no longer persisted mid-run.
        swe = [l for l in listings if is_swe_internship(l.title)
               and year_relevance(l.title, l.location, l.extra_text) != "no"]
        swe = list({l.id(): l for l in swe}.values())  # dedupe within this batch (e.g. a listing appearing in 2 README table sections)
        if known_urls:
            swe = [l for l in swe if normalize_url(l.url) not in known_urls]
        if fetch_missing_description:
            with ThreadPoolExecutor(max_workers=8) as ex:
                swe = list(ex.map(_with_raw_description, swe))
        store = SeenStore(data_dir / "seen" / f"{source.name}.json")
        seen = store.load()
        new = [l for l in swe if l.id() not in seen]
        # Defer SeenStore.save until RunResult.commit_seen() -- after the
        # caller has successfully written new listings out. Saving here used
        # to permanently drop listings whenever the process died (or
        # append_all_json failed) between run() returning and the write.
        pending = (store, seen | {l.id() for l in swe})
    except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
        return source.name, [], [], SourceResult(error=str(e)), None
    return source.name, new, swe, SourceResult(fetched=len(listings), swe=len(swe), new=len(new)), pending


def run(sources, data_dir=DATA_DIR) -> RunResult:
    result = RunResult()
    ats = next((s for s in sources if s.name == "ats_boards"), None)
    rest = [s for s in sources if s is not ats]

    known_urls = frozenset()
    if ats is not None:
        name, new, swe, stats, pending = _run_one(ats, data_dir)
        result.per_source[name] = stats
        result.new_listings.extend(new)
        if pending is not None:
            result._pending_seen.append(pending)
        # exclude empty: normalize_url("") is "", and a url-less ats listing
        # (a job whose JSON had no url key) must not poison known_urls into
        # dropping every url-less fallback-source listing as a false duplicate.
        known_urls = frozenset(u for l in swe if (u := normalize_url(l.url)))

    def work(s):
        fallback = s.name in DESCRIPTION_FALLBACK_SOURCES
        return _run_one(s, data_dir, known_urls=known_urls if fallback else frozenset(),
                         fetch_missing_description=fallback)

    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as ex:
            for name, new, swe, stats, pending in ex.map(work, rest):
                result.per_source[name] = stats
                result.new_listings.extend(new)
                if pending is not None:
                    result._pending_seen.append(pending)

    result.new_listings.sort(key=lambda l: (not l.is_2027, l.company, l.title))
    return result


def selftest():
    class FakeSource:
        def __init__(self, listings, name="fake"):
            self.name = name
            self._listings = listings
        def fetch(self):
            return self._listings

    import tempfile
    d = Path(tempfile.mkdtemp())
    listings = [
        Listing("fake", "Acme", "Software Engineer Intern", "SF", "http://a/1"),
        Listing("fake", "Acme", "Marketing Intern", "SF", "http://a/2"),  # filtered: not SWE
        Listing("fake", "Acme", "SWE Intern Summer 2026", "SF", "http://a/3"),  # for-sure not 2027
    ]
    r1 = run([FakeSource(listings)], data_dir=d)
    assert len(r1.new_listings) == 1 and r1.new_listings[0].company == "Acme"
    assert r1.per_source["fake"] == SourceResult(fetched=3, swe=1, new=1)
    r1.commit_seen()

    r2 = run([FakeSource(listings)], data_dir=d)  # same listings again -> nothing new
    assert r2.new_listings == []
    assert r2.per_source["fake"] == SourceResult(fetched=3, swe=1, new=0)
    r2.commit_seen()

    class BrokenSource:
        name = "broken"
        def fetch(self):
            raise RuntimeError("boom")
    r3 = run([BrokenSource()], data_dir=d)
    assert r3.new_listings == [] and r3.per_source["broken"].error == "boom"
    assert r3._pending_seen == []  # failed source must not queue a seen save

    # a source returning the same listing twice in one fetch() (e.g. it appears
    # in two README table sections) must not produce duplicate new_listings
    dupe = Listing("fake", "Acme", "Software Engineer Intern", "SF", "http://a/1")
    r4 = run([FakeSource([dupe, dupe])], data_dir=Path(tempfile.mkdtemp()))
    assert len(r4.new_listings) == 1
    assert r4.per_source["fake"] == SourceResult(fetched=2, swe=1, new=1)
    r4.commit_seen()

    # a source returning a listing with a non-str field (e.g. a list-valued
    # location, as ats_boards._get_location can produce from some fallback
    # branches) must degrade to a per-source error, not crash run() and
    # drop other sources' already-fetched listings from the returned result.
    class BadFieldSource:
        name = "badfield"
        def fetch(self):
            return [Listing("badfield", "Weird Co", "Software Engineer Intern",
                             ["SF", "NYC"], "http://b/1")]
    r5 = run([FakeSource(listings), BadFieldSource()], data_dir=d)
    assert r5.per_source["badfield"].error
    assert r5.per_source["fake"] == SourceResult(fetched=3, swe=1, new=0)  # already seen from r1/r2
    r5.commit_seen()

    # ats_boards runs first and its listings become known_urls: a
    # github_readme listing with the exact same link (tracking param and
    # all -- normalize_url strips that) is skipped outright, while a
    # genuinely different listing falls back to a raw page fetch.
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
        ats_listing = Listing("ats_boards", "Acme", "Software Engineer Intern", "SF", "http://ats/1?utm=x")
        dupe_listing = Listing("github_readme", "Acme", "Software Engineer Intern", "SF", "http://ats/1")
        new_listing = Listing("github_readme", "Beta", "Software Engineer Intern", "NYC", "http://beta/1")
        r6 = run([FakeSource([ats_listing], name="ats_boards"),
                  FakeSource([dupe_listing, new_listing], name="github_readme")],
                 data_dir=Path(tempfile.mkdtemp()))
    finally:
        _self._fetch_raw_page = orig_fetch_raw_page

    assert len(r6.new_listings) == 2
    by_company = {l.company: l for l in r6.new_listings}
    assert by_company["Acme"].source == "ats_boards"  # the github_readme dupe was skipped
    assert by_company["Beta"].source == "github_readme"
    assert by_company["Beta"].extra_text == "raw page text"  # fell back to raw fetch
    assert calls == ["http://beta/1"]  # never fetched for the skipped duplicate
    r6.commit_seen()

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
        _self._fetch_raw_page = orig_fetch_raw_page
    assert len(r7.new_listings) == 2, r7.new_listings  # neither url-less listing dropped
    assert {l.company for l in r7.new_listings} == {"Acme", "Gamma"}
    r7.commit_seen()

    # Crash / write-failure safety: run() must NOT persist seen-ids itself.
    # If the caller dies (or append_all_json fails) before commit_seen(), the
    # next run must still surface those listings as new -- otherwise they are
    # permanently stranded (seen forever, never written to out/all.json).
    d_crash = Path(tempfile.mkdtemp())
    crash_listings = [
        Listing("fake", "CrashCo", "Software Engineer Intern", "SF", "http://crash/1"),
    ]
    rc1 = run([FakeSource(crash_listings)], data_dir=d_crash)
    assert len(rc1.new_listings) == 1
    # simulate crash: no commit_seen(), no all.json write
    rc2 = run([FakeSource(crash_listings)], data_dir=d_crash)
    assert len(rc2.new_listings) == 1, "uncommitted seen must not strand listings"
    assert rc2.new_listings[0].company == "CrashCo"
    rc2.commit_seen()  # successful write path would call this after append_all_json
    rc3 = run([FakeSource(crash_listings)], data_dir=d_crash)
    assert rc3.new_listings == []
    print("service selftest OK")


if __name__ == "__main__":
    selftest()
