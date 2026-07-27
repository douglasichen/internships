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


# --- Canonical content identity (fuzzy dedup) -------------------------------
# The exact company|title|location key misses the bulk of real duplicates: the
# same posting arrives from jobright/startupjobs/ats_boards/speedyapply with the
# location string spelled a dozen ways ("Dallas, TX" / "Dallas, TX, United
# States" / "Multi Locations: Dallas, TX; ...") and the title carrying trailing
# season/year/remote tags. Canonicalizing both, then clustering on a real
# embedded job-id guard + location overlap, is what actually collapses them.

# Real ATS posting ids only -- gh_jid / Workday R- / JR / greenhouse numeric
# /jobs/<n> / /position/<n> / token= / jobId / Lever+Ashby path UUIDs /
# DESHAW careers trailing id. Digit-leading numeric ids keep out path words
# that are not identities: jobright's "/jobs/info" (-> constant) and
# workday's "/job/Dallas-TX" (a location slug) would otherwise spuriously
# block or allow cross-source merges. jr_id (a README click tracker) is not in
# the pattern at all, so a real gh_jid alongside it still wins.
# Note the JR branch is "(?:_|\b)JR": Workday writes the req as "_JR0285543",
# and \b alone never fires there (underscore is a word char, so there is no
# boundary between "_" and "J") -- which silently dropped every _JR id.
#
# Patterns are applied together; when several match, the rightmost capture
# wins (same as the old single-regex findall[-1] rule).
_REAL_TOKEN_RE = re.compile(
    r"(?:[?&](?:gh_jid|jobId|job_id|token)=|_R-|(?:_|\b)JR-?|/jobs/|/position/)"
    r"(\d[A-Za-z0-9-]{3,})",
    re.I,
)
# Lever (jobs.lever.co/<co>/<uuid>) and Ashby (jobs.ashbyhq.com/<co>/<uuid>)
# put a standard UUID in the path. UUIDs may start with a letter, so they
# cannot ride the digit-leading numeric branch. Require dash-separated form
# so jobright's bare hex (/jobs/info/6a511ea5...) never matches.
_UUID_TOKEN_RE = re.compile(
    r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:[/?#]|$)",
    re.I,
)
# DESHAW: bare /careers/<id> or slug ending in -<id>
# (software-developer-intern-...-2027-5894). real_job_token rejects bare 20xx
# on this pattern only so a year-only slug never becomes an identity token;
# other token sources keep year-shaped values (prior semantics).
_DESHAW_TOKEN_RE = re.compile(
    r"deshaw\.com/careers/(?:.+-)?(\d{3,6})(?:[/?#]|$)",
    re.I,
)
_YEAR_TOKEN_RE = re.compile(r"20\d{2}\Z")

# A trailing SEASON / YEAR / WORK-MODE tag that doesn't change the role
# identity. Deliberately narrow: it strips only the matched tag (and an
# optional year right after a season, plus surrounding punctuation) at the very
# END of the string -- NOT ".*$". Peeling to end-of-string was a real bug: it
# let "Software Engineer, Internship - Defense Tech" collapse to "software
# engineer" and merged distinct Palantir/Citadel roles (and even different
# countries) into one row. Role words (intern/internship), level (phd/ms/bs)
# and region (us/asia/europe) are NOT tags here -- they distinguish postings.
_TITLE_TAIL_RE = re.compile(
    r"[\s\-–—(),|/]+"
    r"(?:summer|fall|autumn|spring|winter|remote|hybrid|on-?site)"
    r"(?:[\s\-,]+20\d\d)?"
    r"[\s\-–—(),|/]*$",
    re.I,
)
# A bare trailing year, e.g. "SWE Intern 2027".
_TITLE_YEAR_TAIL_RE = re.compile(r"[\s\-–—(),|/]+20\d\d[\s\-–—(),|/]*$")

# Country / region / facility noise stripped from each location segment.
_LOC_NOISE_RE = re.compile(
    r",?\s*\b(united states of america|united states|u\.s\.a?\.?|usa|us|"
    r"remote|onsite|hybrid|headquarters|hq|office|multiple locations|"
    r"multi locations)\b",
    re.I,
)
_US_STATE_ABBR = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms "
    "mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv "
    "wi wy dc".split()
)


def _squash(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def canon_title(title):
    """Role title with trailing season/year/work-mode tags peeled off and
    punctuation flattened, so "Software Engineer Intern - Summer 2027" and
    "Software Engineer Intern (Remote)" share one identity -- but role/region
    qualifiers ("... - Defense Tech", "... - Europe") are preserved so distinct
    postings stay distinct."""
    t = _squash(title)
    prev = None
    while prev != t:  # peel repeatedly: "... (Remote) - Summer 2027"
        prev = t
        t = _TITLE_TAIL_RE.sub("", t)
        t = _TITLE_YEAR_TAIL_RE.sub("", t)
        t = t.strip(" -–—(),|/")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", t)).strip()


def location_tokens(location):
    """Set of canonical city tokens for a location string. Collapses country
    suffixes, "Multi Locations:" prefixes, and facility tags so the many
    spellings of one city reduce to the same token. Empty set when the string
    names no city (bare state, "United States", "Remote", "")."""
    loc = _squash(location)
    loc = re.sub(r"^multi(ple)? locations?\s*:", "", loc)
    cities = set()
    for piece in re.split(r"[;/]| and ", loc):
        piece = _LOC_NOISE_RE.sub("", piece).replace("(", " ").replace(")", " ")
        segs = [s.strip() for s in piece.split(",") if s.strip()]
        if not segs:
            continue
        city = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", segs[0])).strip()
        if len(city) > 1 and city not in _US_STATE_ABBR:
            cities.add(city)
    return frozenset(cities)


def real_job_token(url):
    """Real ATS posting id in a URL, or None.

    Covers query ids (gh_jid/token/jobId), Workday _R- / _JR, Greenhouse-style
    /jobs/<n>, Jane Street /position/<n>, Lever/Ashby path UUIDs, and DESHAW
    careers trailing ids. Year-shaped tokens (20xx) are rejected only for
    DESHAW careers captures so season/year tails never become identities;
    numeric patterns (gh_jid, token=, _R-, /jobs/, …) keep prior semantics.
    """
    if not url:
        return None
    best = None  # (end_index, token)
    for cre in (_REAL_TOKEN_RE, _UUID_TOKEN_RE, _DESHAW_TOKEN_RE):
        for m in cre.finditer(url):
            tok = m.group(1)
            # Year filter is a DESHAW safety rail only -- do not skip 20xx on
            # gh_jid / token= / _R- / /jobs/ / etc. (preserves rightmost winner).
            if cre is _DESHAW_TOKEN_RE and _YEAR_TOKEN_RE.fullmatch(tok):
                continue
            end = m.end(1)
            if best is None or end >= best[0]:
                best = (end, tok.lower())
    return best[1] if best else None


def _can_join(cluster, row):
    """True if `row` is the same posting as an existing `cluster` of rows that
    already share (company, canon_title).

    Blocks the join when the row's real job id conflicts with ANY member
    (distinct reqs like NXP _R-...102 vs 103, or Gemini token=1 vs 2 stay apart;
    checking every member, not just one, stops a token-less row from
    transitively bridging two distinct ids).

    For location it compares against the UNION of the cluster's city tokens, not
    each member in isolation: an empty-location member contributes no city, so
    it can never bridge two genuinely different cities (e.g. a Citadel row with
    no location must not let New York and Houston collapse into one). A row with
    no city of its own, or a cluster that names none yet, is treated as
    compatible."""
    rtok = real_job_token(row.get("url"))
    rloc = location_tokens(row.get("location"))
    cluster_cities = set()
    for m in cluster:
        mtok = real_job_token(m.get("url"))
        if rtok and mtok and rtok != mtok:
            return False
        cluster_cities |= location_tokens(m.get("location"))
    return not rloc or not cluster_cities or bool(rloc & cluster_cities)


def cluster_content(rows):
    """Group rows that are the same human-visible posting. Buckets by
    (company, canon_title) then greedily first-fits each row into a compatible
    cluster (see _can_join). Returns a list of row-lists.

    ponytail: greedy first-fit is order-sensitive and O(n^2) within a bucket;
    buckets are tiny (a handful of rows per company+role) so it does not
    matter. Upgrade to real connected-components only if a bucket ever gets big.
    """
    buckets = {}
    for r in rows:
        buckets.setdefault((_squash(r.get("company")), canon_title(r.get("title"))),
                           []).append(r)
    groups = []
    for members in buckets.values():
        clusters = []
        for row in members:
            for c in clusters:
                if _can_join(c, row):
                    c.append(row)
                    break
            else:
                clusters.append([row])
        groups.extend(clusters)
    return groups


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

    # Pass 2: cluster same-posting rows on the URL-collapsed set. Groups by
    # (company, canonical title) then merges rows whose locations overlap and
    # whose embedded real job ids don't conflict -- collapses the same opening
    # arriving from many sources with drifting location/title spellings, while
    # keeping genuinely distinct reqs (NXP _R- ids) and distinct cities apart.
    content_groups = {i: g for i, g in enumerate(cluster_content(after_url))}
    result_rows, removed_content, migrated_content = _collapse(
        content_groups, descs=descs
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

    # canon_title: trailing season / year / work-mode tags collapse to one role
    assert canon_title("Software Engineer Intern - Summer 2027") == "software engineer intern"
    assert canon_title("Software Engineer Intern (Remote)") == "software engineer intern"
    assert canon_title("SWE Intern, Fall 2026") == "swe intern"
    assert canon_title("SWE Intern 2027") == "swe intern"
    assert canon_title("ML Intern (Remote) - Summer 2027") == "ml intern"
    # ...but a distinguishing word in the middle of the role is NOT a tag
    assert canon_title("Quantitative Developer Intern") != canon_title(
        "Quantitative Software Developer Intern")
    # ...and a role/region qualifier AFTER the word "Internship" must survive --
    # peeling to end-of-string collapsed distinct Palantir/Citadel roles (and
    # different countries) into one.
    assert canon_title("Software Engineer, Internship - Defense Tech") \
        != canon_title("Software Engineer, Internship - Infrastructure")
    assert canon_title("Software Engineer, Internship") \
        != canon_title("Software Engineer, Internship - Defense Tech")
    assert canon_title("SWE Intern - US") != canon_title("SWE Intern - Europe")
    # a level/tech qualifier is not a season tag either
    assert canon_title("Data Analyst, MS SQL Server") == "data analyst ms sql server"

    # location_tokens: the many spellings of one city reduce to one token;
    # bare state / country / remote name no city (empty set)
    assert location_tokens("Dallas, TX") == location_tokens("Dallas, TX, United States") \
        == location_tokens("Dallas, TX - Headquarters, United States of America") \
        == frozenset({"dallas"})
    assert location_tokens("Multi Locations: Dallas, TX; Dallas, TX, United States") \
        == frozenset({"dallas"})
    assert location_tokens("Prague, Czech Republic") == location_tokens("Prague, Czechia")
    assert location_tokens("United States - Remote") == location_tokens("") == frozenset()
    assert location_tokens("San Francisco, CA; Palo Alto, CA; Seattle, WA") \
        == frozenset({"san francisco", "palo alto", "seattle"})

    # cluster_content: same posting from many sources with drifting location
    # spellings collapses to one cluster; distinct cities stay separate...
    same_role = [
        {"company": "Optiver", "title": "Software Engineer Intern", "location": "Austin, TX",
         "url": "https://a/1"},
        {"company": "Optiver", "title": "Software Engineer Intern - Summer 2027",
         "location": "Austin, Texas, United States", "url": "https://a/2"},
        {"company": "Optiver", "title": "Software Engineer Intern", "location": "Chicago, IL",
         "url": "https://a/3"},
    ]
    groups = cluster_content(same_role)
    assert sorted(len(g) for g in groups) == [1, 2], groups  # Austin x2 merged, Chicago alone
    # ...and distinct real job ids never merge even when title+city match
    two_reqs = [
        {"company": "NXP", "title": "SE Intern", "location": "Bucharest",
         "url": "https://x/job/Bucharest/SE_R-1001"},
        {"company": "NXP", "title": "SE Intern", "location": "Bucharest",
         "url": "https://x/job/Bucharest/SE_R-1002"},
    ]
    assert sorted(len(g) for g in cluster_content(two_reqs)) == [1, 1]
    # a token-less row must not transitively bridge two conflicting job ids
    bridged = two_reqs + [{"company": "NXP", "title": "SE Intern",
                           "location": "Bucharest", "url": "https://x/no-token"}]
    assert len(cluster_content(bridged)) >= 2
    # an EMPTY-location row must not bridge two distinct cities either: New York
    # and Houston stay separate even with location-less rows in the same bucket.
    city_bridge = [
        {"company": "Citadel", "title": "Software Engineer", "location": "New York, NY",
         "url": "https://c/1"},
        {"company": "Citadel", "title": "Software Engineer", "location": "",
         "url": "https://c/2"},
        {"company": "Citadel", "title": "Software Engineer", "location": "Houston, TX",
         "url": "https://c/3"},
    ]
    cb = cluster_content(city_bridge)
    ny = next(r for g in cb for r in g if r["url"] == "https://c/1")
    hou = next(r for g in cb for r in g if r["url"] == "https://c/3")
    assert not any(ny in g and hou in g for g in cb)  # never in the same cluster

    # real_job_token must catch Workday _JR ids -- \b never fires after "_"
    assert real_job_token("https://x.wd1.myworkdayjobs.com/Careers/job/Penang/_JR0285543") \
        == "0285543"
    assert real_job_token(".../_JR0285543") != real_job_token(".../_JR0285538-1")
    assert sorted(len(g) for g in cluster_content([
        {"company": "Intel", "title": "SW Intern", "location": "Penang",
         "url": ".../Penang/SW-Intern_JR0285543"},
        {"company": "Intel", "title": "SW Intern", "location": "Penang",
         "url": ".../Penang/SW-Intern_JR0285538"},
    ])) == [1, 1]  # distinct JR reqs never merge

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
    # content clustering must not collapse two tokens into one row.
    assert real_job_token(
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
    assert (real_job_token(same_tok[0]["url"])
            == real_job_token(same_tok[1]["url"]) == "99999")
    p_same = Path(tempfile.mkdtemp()) / "all.json"
    p_same.write_text(json.dumps(same_tok))
    removed, total = dedupe(p_same)
    assert removed == 1 and total == 2
    assert json.loads(p_same.read_text())[0]["id"] == "s1"

    # real_job_token: gh_jid is an identity, jr_id (a click tracker) is not --
    # so it never gets picked over a real gh_jid, and alone yields None.
    assert real_job_token(
        "https://www.jumptrading.com/hr/job?gh_jid=7565728&jr_id=tracker99"
    ) == "7565728"
    assert real_job_token("https://example.com/apply?jr_id=onlytracker") is None
    # jobright "/jobs/info/<hash>" and workday "/job/Dallas-TX" are NOT ids
    # (would misfire the old path-word regex); real_job_token returns None.
    assert real_job_token("https://jobright.ai/jobs/info/6a511ea50252") is None
    assert real_job_token(
        "https://jobright.ai/jobs/info/6a511ea50252abcdef") is None
    assert real_job_token(
        "https://copart.wd12.myworkdayjobs.com/en-US/copart/job/Dallas-TX") is None

    # Lever path UUIDs (Palantir etc.) -- previously always None.
    lever_a = "https://jobs.lever.co/palantir/373eb939-6f57-4836-8479-be79a5e07249"
    lever_b = "https://jobs.lever.co/palantir/1b6f1d82-d459-4dea-8bc2-8d2ffe6f881a"
    lever_a_q = lever_a + "?lever-source=LinkedIn&utm=x"
    assert real_job_token(lever_a) == "373eb939-6f57-4836-8479-be79a5e07249"
    assert real_job_token(lever_a_q) == "373eb939-6f57-4836-8479-be79a5e07249"
    assert real_job_token(lever_b) == "1b6f1d82-d459-4dea-8bc2-8d2ffe6f881a"
    assert real_job_token(lever_a) != real_job_token(lever_b)
    # Same company+title+city, different Lever UUIDs: stay distinct.
    lever_rows = [
        {"company": "Palantir", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": lever_a,
         "scraped_at": "2026-01-01", "id": "lev1"},
        {"company": "Palantir", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": lever_b,
         "scraped_at": "2026-01-02", "id": "lev2"},
        # same UUID + tracking noise still merges with lev1
        {"company": "Palantir", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": lever_a_q,
         "scraped_at": "2026-01-03", "id": "lev1b"},
    ]
    p_lev = Path(tempfile.mkdtemp()) / "all.json"
    p_lev.write_text(json.dumps(lever_rows))
    removed, total = dedupe(p_lev)
    assert removed == 1 and total == 3  # lev1b merges into lev1; lev2 stays
    assert {r["id"] for r in json.loads(p_lev.read_text())} == {"lev1", "lev2"}

    # Ashby path UUIDs (may start with a letter -- digit-leading branch misses them)
    assert real_job_token(
        "https://jobs.ashbyhq.com/circleback/2bb6be67-d1a8-42f7-bb1b-64ee36bf613f"
    ) == "2bb6be67-d1a8-42f7-bb1b-64ee36bf613f"
    assert real_job_token(
        "https://jobs.ashbyhq.com/deepgram/dc8693b5-72ce-4ca3-ab15-9c8434d35da1"
    ) == "dc8693b5-72ce-4ca3-ab15-9c8434d35da1"
    assert real_job_token(
        "https://jobs.ashbyhq.com/notion/5b15697c-fa91-4511-9482-c98a6ff29f90"
        "?utm_source=github"
    ) == "5b15697c-fa91-4511-9482-c98a6ff29f90"

    # Greenhouse path / embed / gh_jid still work (regression)
    assert real_job_token(
        "https://job-boards.greenhouse.io/andurilindustries/jobs/5148079007"
    ) == "5148079007"
    assert real_job_token(
        "https://boards.greenhouse.io/andurilindustries/jobs/5148079007"
        "?gh_jid=5148079007"
    ) == "5148079007"
    assert real_job_token(
        "https://boards.greenhouse.io/embed/job_app?for=gemini&token=1111111"
    ) == "1111111"
    # www vs non-www same gh_jid surface -- same token
    assert (real_job_token("https://www.jumptrading.com/hr/job?gh_jid=7565728")
            == real_job_token("https://jumptrading.com/hr/job?gh_jid=7565728")
            == "7565728")

    # Numeric path job ids on career sites
    assert real_job_token("https://careers.sig.com/jobs/10838") == "10838"
    assert real_job_token(
        "https://careers.sig.com/intern-co-op-technology/jobs/10838"
    ) == "10838"
    assert real_job_token(
        "https://www.imc.com/us/careers/jobs/4823924101"
    ) == "4823924101"
    # Jane Street uses /position/<id>, not /jobs/
    assert real_job_token(
        "https://www.janestreet.com/join-jane-street/position/8599644002"
    ) == "8599644002"
    assert real_job_token(
        "https://www.janestreet.com/join-jane-street/position/8419303002/"
    ) == "8419303002"

    # DESHAW trailing careers id (bare and slug forms share identity)
    assert real_job_token("https://www.deshaw.com/careers/5894") == "5894"
    assert real_job_token(
        "https://www.deshaw.com/careers/"
        "software-developer-intern-new-york-summer-2027-5894"
    ) == "5894"
    assert real_job_token(
        "https://www.deshaw.com/careers/"
        "Software-Developer-Ph-D-Intern-New-York-Summer-2027-5893"
    ) == "5893"
    assert real_job_token(
        "https://www.deshaw.com/careers/5894"
    ) == real_job_token(
        "https://www.deshaw.com/careers/"
        "software-developer-intern-new-york-summer-2027-5894"
    )
    # DESHAW year-only tails are not identities (season/year FP rail)
    assert real_job_token("https://www.deshaw.com/careers/2027") is None
    assert real_job_token("https://www.deshaw.com/careers/summer-2027") is None
    assert real_job_token(
        "https://www.deshaw.com/careers/software-developer-intern-2027"
    ) is None
    # Year rejection is DESHAW-only: non-DESHAW 20xx ids keep prior semantics
    assert real_job_token("https://example.com/?gh_jid=2027") == "2027"
    assert real_job_token("https://boards.greenhouse.io/co/jobs/2027") == "2027"
    # multi-match rightmost preserved even when later id is year-shaped
    assert real_job_token(
        "https://x.wd1.myworkdayjobs.com/job/_R-1001?gh_jid=2002"
    ) == "2002"
    # jobright bare-hex still never becomes a token
    assert real_job_token("https://jobright.ai/jobs/info/6a511ea50252") is None

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
