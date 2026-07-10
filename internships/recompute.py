"""Fix up stale rows already sitting in out/all.json without needing a whole
new scrape to rediscover them (SeenStore permanently excludes anything
already reported, so a normal run never revisits an old listing).

Usage:
    python3 -m internships --recompute is_2027
    python3 -m internships --recompute descriptions
    python3 -m internships --recompute is_2027 descriptions
"""
import json
from concurrent.futures import ThreadPoolExecutor

from internships.filters import year_relevance
from internships.service import ROOT, _fetch_raw_page

ALL_JSON_PATH = ROOT / "out" / "all.json"


def _atomic_write(rows, path):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    tmp.replace(path)


def recompute(path=ALL_JSON_PATH):
    """Recompute the stored `is_2027` flag against the current
    filters.year_relevance logic -- run after changing that logic, since
    is_2027 is baked into each row at scrape time and otherwise never
    revisited."""
    rows = json.loads(path.read_text())
    changed = 0
    for row in rows:
        # extra_text (description body) isn't persisted in all.json, only
        # title/location -- fine, since those alone are authoritative and
        # extra_text can only ever turn a "maybe" into a "yes".
        is_2027 = year_relevance(row["title"], row["location"]) != "no"
        if row["is_2027"] != is_2027:
            row["is_2027"] = is_2027
            changed += 1
    _atomic_write(rows, path)
    return changed, len(rows)


def backfill_descriptions(path=ALL_JSON_PATH):
    """Re-fetch a description for any row that doesn't have one -- e.g. it
    was scraped before description-capture existed, or an earlier fetch
    failed transiently. Same best-effort raw-page fetch used as the
    README-source fallback in service.py; doesn't touch rows that already
    have a description, even a short one."""
    rows = json.loads(path.read_text())
    stale = [r for r in rows if not r.get("description")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        texts = list(ex.map(lambda r: _fetch_raw_page(r["url"]), stale))
    changed = 0
    for row, text in zip(stale, texts):
        if text:
            row["description"] = text
            changed += 1
    _atomic_write(rows, path)
    return changed, len(stale)


def selftest():
    import tempfile
    from pathlib import Path

    rows = [
        {"title": "SWE Intern", "location": "SF", "is_2027": False},  # maybe -> now True
        {"title": "SWE Intern Summer 2026", "location": "SF", "is_2027": False},  # stays False
        {"title": "SWE Intern Summer 2027", "location": "SF", "is_2027": True},  # stays True
    ]
    p = Path(tempfile.mkdtemp()) / "all.json"
    p.write_text(json.dumps(rows))
    changed, total = recompute(p)
    assert changed == 1 and total == 3
    result = json.loads(p.read_text())
    assert result[0]["is_2027"] is True
    assert result[1]["is_2027"] is False
    assert result[2]["is_2027"] is True

    import sys
    _self = sys.modules[__name__]  # module-level patch, not a fresh dotted
    # import -- see ats_boards.py's own selftest for why that matters when
    # this file runs as __main__ under --selftest.
    orig_fetch = _self._fetch_raw_page
    _self._fetch_raw_page = lambda url: "fetched: " + url if url else ""
    try:
        rows2 = [
            {"title": "a", "url": "http://x/1", "description": "already have one"},
            {"title": "b", "url": "http://x/2", "description": ""},
            {"title": "c", "url": "http://x/3"},  # missing key entirely, not just empty
        ]
        p2 = Path(tempfile.mkdtemp()) / "all.json"
        p2.write_text(json.dumps(rows2))
        changed, stale_count = backfill_descriptions(p2)
        assert stale_count == 2 and changed == 2
        result2 = json.loads(p2.read_text())
        assert result2[0]["description"] == "already have one"  # untouched
        assert result2[1]["description"] == "fetched: http://x/2"
        assert result2[2]["description"] == "fetched: http://x/3"
    finally:
        _self._fetch_raw_page = orig_fetch
    print("recompute selftest OK")


if __name__ == "__main__":
    selftest()
