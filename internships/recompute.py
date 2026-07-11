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
import re
from concurrent.futures import ThreadPoolExecutor

from internships import desc_store
from internships.filters import company_priority, year_relevance
from internships.models import normalize_url
from internships.service import ROOT, _fetch_raw_page

# Pull a posting id out of an apply URL when present so company+title+location
# dedup does not merge distinct openings that share a generic title
# (e.g. three NXP "System Engineer Intern" roles in Bucharest with different R- ids).
_JOB_TOKEN_RE = re.compile(
    r"(?:/jobs/|/job/|[?&](?:gh_jid|jr_id|jobId|job_id)=|_R-|JR)([A-Za-z0-9-]{4,})",
    re.I,
)

ALL_JSON_PATH = ROOT / "out" / "all.json"
DESCRIPTIONS_DIR = desc_store.DESCRIPTIONS_DIR


def _atomic_write(obj, path):
    """Atomic JSON write for lists (all.json) or dicts (descriptions.json)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
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


def content_key(row):
    """Exact company + title + location identity (case/whitespace-normalized).

    Used to collapse the same posting scraped from different sources/URLs
    (e.g. github_readme vs ats_boards) when the human-visible fields match."""
    return (
        (row.get("company") or "").strip().lower(),
        (row.get("title") or "").strip().lower(),
        (row.get("location") or "").strip().lower(),
    )


def job_token(url):
    """Best-effort posting id embedded in a URL, or None if none found."""
    if not url:
        return None
    matches = _JOB_TOKEN_RE.findall(url)
    return matches[-1].lower() if matches else None


def _keep_oldest(group):
    return sorted(group, key=lambda r: r.get("scraped_at") or "")[0]


def _collapse(groups_dict, *, mergeable=None):
    """groups_dict values are row lists; keep oldest per group. Returns
    (kept_rows, removed_count). If mergeable(group) is False, keep all rows."""
    kept, removed = [], 0
    for group in groups_dict.values():
        if len(group) <= 1:
            kept.extend(group)
            continue
        if mergeable is not None and not mergeable(group):
            kept.extend(group)
            continue
        removed += len(group) - 1
        kept.append(_keep_oldest(group))
    return kept, removed


def _content_group_mergeable(group):
    """Do not merge company+title+location groups when every row carries a
    *different* embedded job id (distinct openings, same generic title)."""
    tokens = [job_token(r.get("url")) for r in group]
    present = [t for t in tokens if t]
    if len(present) >= 2 and len(set(present)) == len(present):
        return False
    return True


def dedupe(path=ALL_JSON_PATH):
    """Merge rows that are the same job under today's identity rules:

    1. Same normalized URL (query-string variants, multi-source same link)
    2. Same company + title + location (exact, case-insensitive) even when
       URLs differ -- e.g. Point72 "Quantitative Developer Intern" listed
       twice from vanshb03. Skips groups where URLs embed distinct job ids
       (multiple NXP "System Engineer Intern" roles in one city).

    Always keeps the OLDEST record per merged group (by scraped_at)."""
    rows = json.loads(path.read_text())
    total = len(rows)

    # Pass 1: by normalized URL
    by_url = {}
    no_url = []
    for row in rows:
        key = normalize_url(row["url"]) if row.get("url") else None
        if key:
            by_url.setdefault(key, []).append(row)
        else:
            no_url.append(row)
    after_url, removed_url = _collapse(by_url)
    after_url.extend(no_url)

    # Pass 2: by company|title|location on the URL-collapsed set
    by_content = {}
    for row in after_url:
        by_content.setdefault(content_key(row), []).append(row)
    result_rows, removed_content = _collapse(by_content, mergeable=_content_group_mergeable)

    result_rows = sorted(result_rows, key=lambda r: r.get("scraped_at") or "")
    _atomic_write(result_rows, path)
    return removed_url + removed_content, total


def backfill_descriptions(path=ALL_JSON_PATH):
    """Download the full apply-page HTML for any listing missing a stored body
    under out/descriptions/<id>.html.gz. Same throttled raw-page fetch as
    service.py. Does not modify all.json rows (except peeling legacy inline
    description fields into the store once)."""
    rows = json.loads(path.read_text())
    # peel any legacy inline description fields into the gzip store first
    peeled = desc_store.peel_from_rows(rows)
    if peeled:
        desc_store.save(peeled, path)
        _atomic_write(rows, path)

    stale = [r for r in rows if r.get("id") and not desc_store.has(r["id"], path)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        texts = list(ex.map(lambda r: _fetch_raw_page(r.get("url")), stale))
    changed = 0
    for row, text in zip(stale, texts):
        if text:
            desc_store.put(row["id"], text, path)
            changed += 1
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

    # dedupe pass 1: same link (query string aside), different source/scrape time
    # -- keep the OLDEST record, never overwrite it with a newer one
    dupe_rows = [
        {"company": "A", "title": "T1", "location": "SF",
         "url": "http://a/1", "source": "ats_boards", "scraped_at": "2026-02-01T00:00:00"},
        {"company": "A", "title": "T1", "location": "SF",
         "url": "http://a/1?utm=x", "source": "github_readme", "scraped_at": "2026-01-01T00:00:00"},
        {"company": "A", "title": "T2", "location": "SF",
         "url": "http://a/2", "source": "ats_boards", "scraped_at": "2026-01-15T00:00:00"},
        {"company": "B", "title": "NoUrl1", "location": "",
         "source": "ats_boards", "scraped_at": "2026-01-01T00:00:00"},  # no url
        {"company": "C", "title": "NoUrl2", "location": "",
         "source": "github_readme", "scraped_at": "2026-01-02T00:00:00"},  # no url
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
    assert by_url["http://a/2"]["source"] == "ats_boards"
    assert sum(1 for r in result3 if not r.get("url")) == 2  # both no-url rows kept

    # dedupe pass 2: same company+title+location, different URLs/sources
    ctl_rows = [
        {"company": "Point72", "title": "Quantitative Developer Intern", "location": "New York, NY",
         "url": "http://readme/p72", "source": "github_readme", "scraped_at": "2026-07-10T00:00:00",
         "id": "new"},
        {"company": "Point72", "title": "Quantitative Developer Intern", "location": "New York, NY",
         "url": "http://boards/p72", "source": "github_readme", "scraped_at": "2026-07-09T00:00:00",
         "id": "old"},
        {"company": "Point72", "title": "Quantitative Software Developer Intern",
         "location": "New York, London, or Paris",
         "url": "http://gh/other", "source": "ats_boards", "scraped_at": "2026-07-09T01:00:00",
         "id": "different"},
    ]
    p_ctl = Path(tempfile.mkdtemp()) / "all.json"
    p_ctl.write_text(json.dumps(ctl_rows))
    removed, total = dedupe(p_ctl)
    assert removed == 1 and total == 3
    result_ctl = json.loads(p_ctl.read_text())
    assert len(result_ctl) == 2
    assert {r["id"] for r in result_ctl} == {"old", "different"}
    assert next(r for r in result_ctl if r["id"] == "old")["url"] == "http://boards/p72"

    # same title+location but distinct Workday R- job ids -- do NOT merge
    nxp_rows = [
        {"company": "NXP", "title": "System Engineer Intern", "location": "Bucharest",
         "url": "https://nxp.wd3.myworkdayjobs.com/careers/job/Bucharest/System-Engineer-Intern_R-10064103",
         "scraped_at": "2026-01-01", "id": "n1"},
        {"company": "NXP", "title": "System Engineer Intern", "location": "Bucharest",
         "url": "https://nxp.wd3.myworkdayjobs.com/careers/job/Bucharest/System-Engineer-Intern_R-10064102",
         "scraped_at": "2026-01-02", "id": "n2"},
    ]
    p_nxp = Path(tempfile.mkdtemp()) / "all.json"
    p_nxp.write_text(json.dumps(nxp_rows))
    removed, total = dedupe(p_nxp)
    assert removed == 0 and total == 2
    assert len(json.loads(p_nxp.read_text())) == 2

    import sys
    _self = sys.modules[__name__]  # module-level patch, not a fresh dotted
    # import -- see ats_boards.py's own selftest for why that matters when
    # this file runs as __main__ under --selftest.
    orig_fetch = _self._fetch_raw_page
    _self._fetch_raw_page = lambda url: "fetched: " + url if url else ""
    try:
        # descriptions live in descriptions.json keyed by id -- not on the row
        rows2 = [
            {"id": "a", "title": "a", "url": "http://x/1"},
            {"id": "b", "title": "b", "url": "http://x/2"},
            {"id": "c", "title": "c", "url": "http://x/3"},
            {"id": "d", "title": "d"},  # no url -- must not crash the whole run
        ]
        p2 = Path(tempfile.mkdtemp()) / "all.json"
        p2.write_text(json.dumps(rows2))
        # seed store: a already has a description
        desc_store.put("a", "already have one", p2)
        changed, stale_count = backfill_descriptions(p2)
        assert stale_count == 3 and changed == 2  # b,c fetched; d no url -> empty
        # all.json stays description-free
        result2 = json.loads(p2.read_text())
        assert all("description" not in r for r in result2)
        assert desc_store.get("a", p2) == "already have one"  # untouched
        assert desc_store.get("b", p2) == "fetched: http://x/2"
        assert desc_store.get("c", p2) == "fetched: http://x/3"
        assert not desc_store.has("d", p2)
    finally:
        _self._fetch_raw_page = orig_fetch
    print("recompute selftest OK")


if __name__ == "__main__":
    selftest()
