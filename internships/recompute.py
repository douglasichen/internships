"""Fix up stale rows already sitting in out/all.json without needing a whole
new scrape to rediscover them (SeenStore permanently excludes anything
already reported, so a normal run never revisits an old listing).

Usage:
    python3 -m internships --recompute is_2027
    python3 -m internships --recompute descriptions
    python3 -m internships --recompute dedup
    python3 -m internships --recompute priority
    python3 -m internships --recompute dedup is_2027 priority descriptions

Also: clear_is_2027(id) for the UI — sets is_2027=False and is_2027_override=False
so a later is_2027 recompute does not restore the flag.
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
# Identity-bearing only: gh_jid / jobId / job_id / Greenhouse token= / Workday R- / JR.
# Do NOT include jr_id — that is a README-list click tracker (stripped by
# normalize_url) and would steal the last-match slot from a real gh_jid.
_JOB_TOKEN_RE = re.compile(
    r"(?:/jobs/|/job/|[?&](?:gh_jid|jobId|job_id|token)=|_R-|JR)([A-Za-z0-9-]{4,})",
    re.I,
)

ALL_JSON_PATH = ROOT / "out" / "all.json"
DESCRIPTIONS_PATH = desc_store.DESCRIPTIONS_PATH


def _atomic_write(obj, path):
    """Atomic JSON write for lists (all.json) or dicts (descriptions.json)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)


def recompute(path=ALL_JSON_PATH):
    """Recompute the stored `is_2027` flag against the current
    filters.year_relevance logic -- run after changing that logic, since
    is_2027 is baked into each row at scrape time and otherwise never
    revisited.

    Rows with is_2027_override=false were manually cleared in the UI; leave
    them off (and keep the override) so a bulk recompute does not undo it.
    """
    rows = json.loads(path.read_text())
    changed = 0
    for row in rows:
        if row.get("is_2027_override") is False:
            if row.get("is_2027"):
                row["is_2027"] = False
                changed += 1
            continue
        # extra_text (description body) isn't persisted in all.json, only
        # title/location -- fine, since those alone are authoritative and
        # extra_text can only ever turn a "maybe" into a "yes".
        is_2027 = year_relevance(row["title"], row["location"]) != "no"
        if row.get("is_2027") != is_2027:
            row["is_2027"] = is_2027
            changed += 1
    _atomic_write(rows, path)
    return changed, len(rows)


def clear_is_2027(listing_id: str, path=ALL_JSON_PATH) -> dict:
    """Manually clear is_2027 on one listing. Returns {ok, id} or raises KeyError.

    Sets is_2027=False and is_2027_override=False so later --recompute is_2027
    does not flip it back on.
    """
    if not listing_id or not isinstance(listing_id, str):
        raise ValueError("id required")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    rows = json.loads(path.read_text())
    found = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("id") == listing_id:
            found = row
            break
    if found is None:
        raise KeyError(listing_id)
    found["is_2027"] = False
    found["is_2027_override"] = False
    _atomic_write(rows, path)
    return {"ok": True, "id": listing_id, "is_2027": False}


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


def _fill_desc_from_dropped(descs, keep, group):
    """If the kept id has no description, copy one from any dropped sibling.

    Mirrors append_all_json's _fill_desc_for_group: only fill missing keys,
    never overwrite an existing body under the kept id. Returns True when a
    description was migrated.
    """
    kid = keep.get("id") if isinstance(keep, dict) else None
    if not kid or descs.get(kid):
        return False
    for r in group:
        if r is keep:
            continue
        rid = r.get("id") if isinstance(r, dict) else None
        text = descs.get(rid) if rid else None
        if text:
            descs[kid] = text
            return True
    return False


def _collapse(groups_dict, *, mergeable=None, descs=None):
    """groups_dict values are row lists; keep oldest per group. Returns
    (kept_rows, removed_count, migrated_count). If mergeable(group) is False,
    keep all rows. When descs is provided, migrate a dropped id's body onto
    the kept id if the kept id still lacks one (setdefault semantics)."""
    kept, removed, migrated = [], 0, 0
    for group in groups_dict.values():
        if len(group) <= 1:
            kept.extend(group)
            continue
        if mergeable is not None and not mergeable(group):
            kept.extend(group)
            continue
        removed += len(group) - 1
        keep = _keep_oldest(group)
        kept.append(keep)
        if descs is not None and _fill_desc_from_dropped(descs, keep, group):
            migrated += 1
    return kept, removed, migrated


def _content_group_mergeable(group):
    """Do not merge company+title+location groups when embedded job ids conflict.

    Any disagreement among present tokens means the group is not a single
    opening (e.g. tokens A,A,B must not collapse and drop B). Only merge when
    every extracted token agrees (or fewer than two rows carry a token).
    """
    tokens = [job_token(r.get("url")) for r in group]
    present = [t for t in tokens if t]
    if len(set(present)) > 1:
        return False
    return True


def dedupe(path=ALL_JSON_PATH):
    """Merge rows that are the same job under today's identity rules:

    1. Same normalized URL (query-string variants, multi-source same link)
    2. Same company + title + location (exact, case-insensitive) even when
       URLs differ -- e.g. Point72 "Quantitative Developer Intern" listed
       twice from vanshb03. Skips groups where URLs embed distinct job ids
       (multiple NXP "System Engineer Intern" roles in one city).

    Always keeps the OLDEST record per merged group (by scraped_at).

    Description store: when a group collapses and the kept id has no body but
    a dropped id does, copy that body onto the kept id (load once, setdefault
    per group, save once). Same idea as append_all_json's _fill_desc_for_group
    so dual-write bodies are not stranded on discarded listing ids.
    """
    rows = json.loads(path.read_text())
    total = len(rows)
    descs = desc_store.load(path)

    # Pass 1: by normalized URL
    by_url = {}
    no_url = []
    for row in rows:
        key = normalize_url(row["url"]) if row.get("url") else None
        if key:
            by_url.setdefault(key, []).append(row)
        else:
            no_url.append(row)
    after_url, removed_url, migrated_url = _collapse(by_url, descs=descs)
    after_url.extend(no_url)

    # Pass 2: by company|title|location on the URL-collapsed set
    by_content = {}
    for row in after_url:
        by_content.setdefault(content_key(row), []).append(row)
    result_rows, removed_content, migrated_content = _collapse(
        by_content, mergeable=_content_group_mergeable, descs=descs
    )

    result_rows = sorted(result_rows, key=lambda r: r.get("scraped_at") or "")
    # descriptions first, then all.json: crash between leaves bodies under the
    # kept id even if duplicate rows still exist (re-dedupe is a no-op fill).
    if migrated_url + migrated_content:
        desc_store.save(descs, path)
    _atomic_write(result_rows, path)
    return removed_url + removed_content, total


def backfill_descriptions(path=ALL_JSON_PATH):
    """Download the full apply-page HTML for any listing missing a body in
    out/descriptions.json.gz. Same throttled raw-page fetch as service.py.
    Does not modify all.json rows (except peeling legacy inline description
    fields into the store once)."""
    rows = json.loads(path.read_text())
    # peel any legacy inline description fields into the gzip store first —
    # merge into the existing store (never save(peeled) alone: that would
    # wipe every other id's body).
    peeled = desc_store.peel_from_rows(rows)
    descs = desc_store.load(path)
    if peeled:
        for k, v in peeled.items():
            if k not in descs:
                descs[k] = v
        desc_store.save(descs, path)
        _atomic_write(rows, path)

    stale = [r for r in rows if r.get("id") and not descs.get(r["id"])]
    with ThreadPoolExecutor(max_workers=8) as ex:
        texts = list(ex.map(lambda r: _fetch_raw_page(r.get("url")), stale))
    changed = 0
    for row, text in zip(stale, texts):
        if text:
            descs[row["id"]] = text
            changed += 1
    if changed:
        desc_store.save(descs, path)
    return changed, len(stale)


def selftest():
    import tempfile
    from pathlib import Path

    # clear_is_2027 + override holds across recompute
    td = Path(tempfile.mkdtemp())
    p = td / "all.json"
    p.write_text(json.dumps([
        {"id": "a1", "title": "SWE Intern Summer 2027", "location": "SF", "is_2027": True},
        {"id": "a2", "title": "SWE Intern", "location": "SF", "is_2027": True},
    ]))
    assert clear_is_2027("a1", p)["is_2027"] is False
    try:
        clear_is_2027("missing", p)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    recompute(p)
    cleared = json.loads(p.read_text())
    assert cleared[0]["is_2027"] is False and cleared[0]["is_2027_override"] is False
    assert cleared[1]["is_2027"] is True  # maybe intern still True after recompute

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

    # Greenhouse embed ?token= is identity-bearing (models.normalize_url keeps it);
    # content-key dedup must not collapse two tokens into one row.
    assert job_token(
        "https://boards.greenhouse.io/embed/job_app?for=gemini&token=1111111"
    ) == "1111111"
    gh_rows = [
        {"company": "Gemini", "title": "SWE Intern", "location": "NYC",
         "url": "https://boards.greenhouse.io/embed/job_app?for=gemini&token=1111111",
         "scraped_at": "2026-01-01", "id": "g1"},
        {"company": "Gemini", "title": "SWE Intern", "location": "NYC",
         "url": "https://boards.greenhouse.io/embed/job_app?for=gemini&token=2222222",
         "scraped_at": "2026-01-02", "id": "g2"},
    ]
    p_gh = Path(tempfile.mkdtemp()) / "all.json"
    p_gh.write_text(json.dumps(gh_rows))
    removed, total = dedupe(p_gh)
    assert removed == 0 and total == 2
    assert {r["id"] for r in json.loads(p_gh.read_text())} == {"g1", "g2"}

    # Conflicting tokens in one content-key group (A,A,B): must not drop B.
    # Previous guard only refused merge when *all* present tokens were unique,
    # so [A,A,B] merged and deleted the distinct opening.
    mixed_rows = [
        {"company": "NXP", "title": "SE Intern", "location": "X",
         "url": "https://x.example/careers/job/Loc/Role_R-1001",
         "scraped_at": "2026-01-01", "id": "a1"},
        {"company": "NXP", "title": "SE Intern", "location": "X",
         "url": "https://x.example/careers/job/Loc/Role_R-1001?utm=1",
         "scraped_at": "2026-01-02", "id": "a2"},
        {"company": "NXP", "title": "SE Intern", "location": "X",
         "url": "https://x.example/careers/job/Loc/Role_R-1002",
         "scraped_at": "2026-01-03", "id": "b1"},
    ]
    assert _content_group_mergeable(mixed_rows) is False
    p_mix = Path(tempfile.mkdtemp()) / "all.json"
    p_mix.write_text(json.dumps(mixed_rows))
    removed, total = dedupe(p_mix)
    # pass-1 URL-normalizes a1/a2 (utm stripped) -> merge to a1; pass-2 keeps
    # a1 vs b1 (distinct R- ids). Net: one URL dupe removed, B survives.
    assert removed == 1 and total == 3
    assert {r["id"] for r in json.loads(p_mix.read_text())} == {"a1", "b1"}

    # same embedded job id, different URL surface -- still mergeable
    same_tok = [
        {"company": "Co", "title": "Intern", "location": "SF",
         "url": "https://boards.greenhouse.io/co/jobs/99999",
         "scraped_at": "2026-01-01", "id": "s1"},
        {"company": "Co", "title": "Intern", "location": "SF",
         "url": "https://boards.greenhouse.io/embed/job_app?for=co&token=99999",
         "scraped_at": "2026-01-02", "id": "s2"},
    ]
    assert job_token(same_tok[0]["url"]) == job_token(same_tok[1]["url"]) == "99999"
    assert _content_group_mergeable(same_tok) is True
    p_same = Path(tempfile.mkdtemp()) / "all.json"
    p_same.write_text(json.dumps(same_tok))
    removed, total = dedupe(p_same)
    assert removed == 1 and total == 2
    assert json.loads(p_same.read_text())[0]["id"] == "s1"

    # jr_id is a click tracker, not a job id -- must not be preferred over gh_jid
    assert job_token(
        "https://www.jumptrading.com/hr/job?gh_jid=7565728&jr_id=tracker99"
    ) == "7565728"
    assert job_token("https://example.com/apply?jr_id=onlytracker") is None

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

        # peel of a legacy inline description must MERGE into the store, never
        # replace it (save(peeled) alone wiped every other id).
        p_peel = Path(tempfile.mkdtemp()) / "all.json"
        p_peel.write_text(json.dumps([
            {"id": "keep_me", "title": "a", "url": "http://x/1"},
            {"id": "peel_me", "title": "b", "url": "http://x/2",
             "description": "inline body"},
            {"id": "fetch_me", "title": "c", "url": "http://x/3"},
        ]))
        desc_store.put("keep_me", "IMPORTANT EXISTING DESC", p_peel)
        desc_store.put("fetch_me", "ALREADY FETCHED", p_peel)
        # fetch returns empty so we only exercise the peel/merge path
        _self._fetch_raw_page = lambda url: ""
        changed, stale_count = backfill_descriptions(p_peel)
        assert changed == 0
        store = desc_store.load(p_peel)
        assert store.get("keep_me") == "IMPORTANT EXISTING DESC"
        assert store.get("fetch_me") == "ALREADY FETCHED"
        assert store.get("peel_me") == "inline body"
        slim = json.loads(p_peel.read_text())
        assert all("description" not in r for r in slim)
    finally:
        _self._fetch_raw_page = orig_fetch
    print("recompute selftest OK")


if __name__ == "__main__":
    selftest()
