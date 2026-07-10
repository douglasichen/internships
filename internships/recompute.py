"""Fix up stale rows already sitting in out/all.json without needing a whole
new scrape to rediscover them (SeenStore permanently excludes anything
already reported, so a normal run never revisits an old listing).

Usage:
    python3 -m internships --recompute is_2027
    python3 -m internships --recompute descriptions
    python3 -m internships --recompute dedup
    python3 -m internships --recompute priority
    python3 -m internships --recompute dedup is_2027 priority descriptions
"""
import json
from concurrent.futures import ThreadPoolExecutor

from internships.filters import company_priority, year_relevance
from internships.models import normalize_url
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


def recompute_priority(path=ALL_JSON_PATH):
    """Recompute the stored `priority` field (1/2/3, per
    filters.company_priority) against the current classification -- handy
    after the tier lists change, since priority is otherwise also baked in
    at scrape time."""
    rows = json.loads(path.read_text())
    changed = 0
    for row in rows:
        priority = company_priority(row.get("company", ""))
        if row.get("priority") != priority:
            row["priority"] = priority
            changed += 1
    _atomic_write(rows, path)
    return changed, len(rows)


def dedupe(path=ALL_JSON_PATH):
    """Merge rows that are the same job posting under today's identity rules
    but weren't recognized as duplicates when they were scraped -- e.g. a
    query-string variant of an earlier link (normalize_url wasn't applied
    yet), or the same job found by both ats_boards and a README-table
    source before cross-source dedup existed. Rows with no url at all are
    never merged (nothing reliable to match on).

    Always keeps the OLDEST record for a given link and drops the rest --
    never overwrites an existing record with a newer one, even if the
    newer one happens to have more/better data (e.g. a backfilled
    description)."""
    rows = json.loads(path.read_text())
    groups = {}
    singles = []
    for row in rows:
        key = normalize_url(row["url"]) if row.get("url") else None
        if key:
            groups.setdefault(key, []).append(row)
        else:
            singles.append(row)

    kept = []
    removed = 0
    for group in groups.values():
        if len(group) > 1:
            group = sorted(group, key=lambda r: r.get("scraped_at", ""))
            removed += len(group) - 1
        kept.append(group[0])

    result_rows = sorted(kept + singles, key=lambda r: r.get("scraped_at", ""))
    _atomic_write(result_rows, path)
    return removed, len(rows)


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

    # priority: delegates to filters.company_priority(company) -- the
    # classification itself is filters.py's own selftest's job
    import sys
    _self = sys.modules[__name__]
    orig_priority = _self.company_priority
    _self.company_priority = lambda company: 1 if company == "Notable Co" else 3
    try:
        rows_pri = [
            {"company": "Notable Co", "priority": 3},  # stale -> 1
            {"company": "Nobody Inc", "priority": 1},  # stale -> 3
            {"company": "Notable Co", "priority": 1},  # already correct
        ]
        p_pri = Path(tempfile.mkdtemp()) / "all.json"
        p_pri.write_text(json.dumps(rows_pri))
        changed, total = recompute_priority(p_pri)
        assert changed == 2 and total == 3
        result_pri = json.loads(p_pri.read_text())
        assert result_pri[0]["priority"] == 1
        assert result_pri[1]["priority"] == 3
        assert result_pri[2]["priority"] == 1
    finally:
        _self.company_priority = orig_priority

    # dedupe: same link (query string aside), different source/scrape time --
    # keep the OLDEST record, never overwrite it with a newer one
    dupe_rows = [
        {"url": "http://a/1", "source": "ats_boards", "scraped_at": "2026-02-01T00:00:00",
         "description": ""},
        {"url": "http://a/1?utm=x", "source": "github_readme", "scraped_at": "2026-01-01T00:00:00",
         "description": "has a description"},
        {"url": "http://a/2", "source": "ats_boards", "scraped_at": "2026-01-15T00:00:00"},
        {"source": "ats_boards", "scraped_at": "2026-01-01T00:00:00"},  # no url -- never merged
        {"source": "github_readme", "scraped_at": "2026-01-02T00:00:00"},  # no url -- never merged
    ]
    p3 = Path(tempfile.mkdtemp()) / "all.json"
    p3.write_text(json.dumps(dupe_rows))
    removed, total = dedupe(p3)
    assert removed == 1 and total == 5
    result3 = json.loads(p3.read_text())
    assert len(result3) == 4
    by_url = {r.get("url"): r for r in result3}
    # the older record's own url field is untouched -- still has ?utm=x
    assert by_url["http://a/1?utm=x"]["source"] == "github_readme"
    assert by_url["http://a/1?utm=x"]["description"] == "has a description"
    assert by_url["http://a/2"]["source"] == "ats_boards"
    assert sum(1 for r in result3 if not r.get("url")) == 2  # both no-url rows kept, untouched

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
