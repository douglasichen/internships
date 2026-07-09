"""Orchestrator: fetch every source (in parallel), filter to SWE internships,
drop anything already seen from a prior run, persist the updated seen-ids,
return what's new. One run = one call to run().
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from internships.filters import is_swe_internship, year_relevance
from internships.models import Listing
from internships.seen_store import SeenStore

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


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


def _run_one(source, data_dir):
    try:
        listings = source.fetch()
    except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
        return source.name, [], SourceResult(error=str(e))
    swe = [l for l in listings if is_swe_internship(l.title)
           and year_relevance(l.title, l.location, l.extra_text) != "no"]
    store = SeenStore(data_dir / "seen" / f"{source.name}.json")
    seen = store.load()
    new = [l for l in swe if l.id() not in seen]
    store.save(seen | {l.id() for l in swe})
    return source.name, new, SourceResult(fetched=len(listings), swe=len(swe), new=len(new))


def run(sources, data_dir=DATA_DIR) -> RunResult:
    result = RunResult()
    with ThreadPoolExecutor(max_workers=len(sources) or 1) as ex:
        for name, new, stats in ex.map(lambda s: _run_one(s, data_dir), sources):
            result.per_source[name] = stats
            result.new_listings.extend(new)
    result.new_listings.sort(key=lambda l: (not l.is_2027, l.company, l.title))
    return result


def selftest():
    class FakeSource:
        name = "fake"
        def __init__(self, listings):
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

    r2 = run([FakeSource(listings)], data_dir=d)  # same listings again -> nothing new
    assert r2.new_listings == []
    assert r2.per_source["fake"] == SourceResult(fetched=3, swe=1, new=0)

    class BrokenSource:
        name = "broken"
        def fetch(self):
            raise RuntimeError("boom")
    r3 = run([BrokenSource()], data_dir=d)
    assert r3.new_listings == [] and r3.per_source["broken"].error == "boom"
    print("service selftest OK")


if __name__ == "__main__":
    selftest()
