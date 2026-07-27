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
_TITLE_TAG = r"(?:summer|fall|autumn|spring|winter|remote|hybrid|on-?site)"
_TITLE_TAIL_RE = re.compile(
    r"[\s\-–—(),|/]+"
    + _TITLE_TAG
    + r"(?:[\s\-,]+20\d\d)?"
    r"[\s\-–—(),|/]*$",
    re.I,
)
# A bare trailing year, e.g. "SWE Intern 2027".
_TITLE_YEAR_TAIL_RE = re.compile(r"[\s\-–—(),|/]+20\d\d[\s\-–—(),|/]*$")
# Leading season+year / work-mode / bare year. Season at the start requires a
# year ("Spring 2027 ...") so tech words like "Spring Boot" are not peeled.
# Trailing peels still allow bare season (end-of-title " - Summer" is a tag).
_TITLE_HEAD_SEASON_RE = re.compile(
    r"^(?:summer|fall|autumn|spring|winter)[\s\-,]+20\d\d[\s\-–—(),|/]+",
    re.I,
)
_TITLE_HEAD_MODE_RE = re.compile(
    r"^(?:remote|hybrid|on-?site)[\s\-–—(),|/]+",
    re.I,
)
_TITLE_YEAR_HEAD_RE = re.compile(r"^20\d\d[\s\-–—(),|/]+")
# Whole-word intern/internship synonym (final step only). Merges
# "Engineering Internship" vs "Engineering Intern" across sources without
# touching mid-title qualifiers like Defense Tech / Europe / PhD.
_INTERNSHIP_WORD_RE = re.compile(r"\binternship\b", re.I)
# Protect C++ so the punctuation strip does not turn it into bare "c".
_CPP_PROTECT_RE = re.compile(r"c\+\+", re.I)
_CPP_PLACEHOLDER = "cxxplusplus"

# Facility / work-mode words stripped from location segments (not countries —
# those live in _COUNTRY_NOISE so country-first patterns can be detected).
_LOC_FACILITY_RE = re.compile(
    r",?\s*\b(remote|on-?site|onsite|hybrid|headquarters|hq|office)\b",
    re.I,
)
# Pure noise location strings with no city list (no "Multi Locations: ..." colon).
_LOC_PURE_NOISE_RE = re.compile(
    r"^(?:\d+\s+)?(?:multi(?:ple)?\s+)?locations?$",
    re.I,
)
# Trailing "+N" multi-location counters: "Hillsboro, OR +1".
_LOC_PLUS_N_RE = re.compile(r"\s*\+\d+\s*$")
# Leading "or " left after splitting "New York, London, or Paris" on commas.
_LOC_OR_PREFIX_RE = re.compile(r"^or\s+", re.I)

_US_STATE_ABBR = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms "
    "mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv "
    "wi wy dc".split()
)
# Full US state names — not cities when they appear as a segment (except the
# city-states below). "US, Arizona, Phoenix" must skip Arizona and keep Phoenix.
# MUST be explicit multi-word strings: never space-join then .split(), or
# multi-word states become fragments (new/york/north/carolina/...) and leak as
# city tokens that bridge distinct cities (Buffalo↔NYC via "new york", etc.).
_US_STATE_NAMES = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
})
# States that are also major cities commonly listed alone or in city lists.
# "New York, Chicago" must keep new york; bare "Arizona" must not.
_CITY_STATE_NAMES = frozenset({"new york", "washington"})
# Country / region tokens — never city identity. Expanded so country-first
# strings like "Poland, Gdansk" and "US, Arizona, Phoenix" resolve to the city.
_COUNTRY_NOISE = frozenset({
    "united states of america", "united states", "usa", "us", "u s", "u s a",
    "united kingdom", "uk", "great britain", "england", "scotland", "wales",
    "canada", "mexico", "india", "china", "japan", "germany", "france",
    "spain", "italy", "netherlands", "sweden", "norway", "denmark", "finland",
    "switzerland", "ireland", "australia", "new zealand", "singapore",
    "hong kong", "taiwan", "south korea", "korea", "brazil", "poland",
    "czech republic", "czechia", "romania", "hungary", "austria", "belgium",
    "portugal", "israel", "uae", "united arab emirates", "philippines",
    "malaysia", "indonesia", "thailand", "vietnam", "turkey", "greece",
    "argentina", "chile", "colombia", "south africa", "egypt", "nigeria",
    "pakistan", "bangladesh", "russia", "ukraine", "serbia", "croatia",
    "slovakia", "slovenia", "bulgaria", "lithuania", "latvia", "estonia",
    "iceland", "luxembourg", "malta", "cyprus", "costa rica", "panama",
    "puerto rico", "europe", "asia", "emea", "apac", "latam", "north america",
    "south america", "global", "worldwide",
})


def _squash(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# Trailing legal / generic corporate tokens peeled from company names for
# content-bucket keys. Only these -- "Google Cloud" must not collapse to
# "Google", and meaningful second words (Trading, Capital, Investment) stay.
_COMPANY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "corporation", "technologies", "technology",
    "company", "co", "group", "holdings",
    # optional legal tails (under-merge only if missing): UK plc, US long forms
    "plc", "incorporated", "pbc", "limited",
})

# Short forms that drop meaningful words (not just a legal suffix). Key and
# value are already alphanum-squashed (post-suffix-peel). Keep this tiny --
# do not alias renames (Fab2 ≠ Atomic Semi) or unrelated firms.
_COMPANY_ALIASES = {
    "imc": "imctrading",
    "towerresearch": "towerresearchcapital",
    "aquatic": "aquaticcapitalmanagement",
    "aquaticcapital": "aquaticcapitalmanagement",
    "oldmission": "oldmissioncapital",
    # SIG: live data uses bare / Investment Group / International Group (majority).
    # After Group peel: susquehanna | susquehannainvestment | susquehannainternational
    "susquehannainvestment": "susquehanna",
    "susquehannainternational": "susquehanna",
}


def canon_company(name):
    """Company identity for content-bucket keys.

    Lowercases, strips leading "the ", peels trailing legal/generic suffixes
    (Inc/LLC/Technologies/Group/...), flattens to alphanumerics so punctuation
    variants match ("D.E. Shaw" ≡ "D. E. Shaw" ≡ "D. E. Shaw & Co."), then
    applies a small short-form alias map (IMC → IMC Trading, etc.).

    Safety: only peels generic trailing tokens -- "Google Cloud" stays distinct
    from "Google". Does not invent renames (Fab2 stays fab2, Atomic Semi stays
    atomicsemi). If every token was a suffix (e.g. bare "Inc"), fall back to
    the pre-peel alphanum form so those names do not share one empty bucket.
    """
    t = _squash(name)
    if t.startswith("the "):
        t = t[4:]
    tokens = re.findall(r"[a-z0-9]+", t)
    raw = "".join(tokens)
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    key = "".join(tokens) or raw
    return _COMPANY_ALIASES.get(key, key)


def canon_title(title):
    """Role title with leading/trailing season/year/work-mode tags peeled off
    and punctuation flattened, so "Software Engineer Intern - Summer 2027",
    "2027 Software Engineer Intern", and "Software Engineer Intern (Remote)"
    share one identity -- but role/region qualifiers ("... - Defense Tech",
    "... - Europe") are preserved so distinct postings stay distinct.

    Final whole-word `internship` → `intern` merges cross-source wording
    variants without touching mid-title role words."""
    t = _squash(title)
    t = _CPP_PROTECT_RE.sub(_CPP_PLACEHOLDER, t)
    prev = None
    while prev != t:  # peel repeatedly: "Spring 2027 ... (Remote) - Summer 2027"
        prev = t
        t = _TITLE_HEAD_SEASON_RE.sub("", t)
        t = _TITLE_HEAD_MODE_RE.sub("", t)
        t = _TITLE_YEAR_HEAD_RE.sub("", t)
        t = _TITLE_TAIL_RE.sub("", t)
        t = _TITLE_YEAR_TAIL_RE.sub("", t)
        t = t.strip(" -–—(),|/")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = t.replace(_CPP_PLACEHOLDER, "c++")
    t = _INTERNSHIP_WORD_RE.sub("intern", t)
    return re.sub(r"\s+", " ", t).strip()


def _clean_loc_seg(seg):
    """Lower/strip one comma-segment; normalize DC spellings to `washington`."""
    s = _LOC_OR_PREFIX_RE.sub("", (seg or "").strip())
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()
    if s in ("washington d c", "washington dc", "d c", "dc",
             "district of columbia"):
        return "washington"
    return s


def _cities_from_segments(segs):
    """Pick city tokens from already-cleaned comma-segments of one location
    piece. Handles City/ST, country-first (Poland, Gdansk / US, Arizona,
    Phoenix / US, Washington, Redmond), multi-city lists (New York, Chicago),
    and bare state/country.

    Country segments must still be present in `segs` so country-first structure
    can be detected. City-states (new york, washington) act as states when a
    later real city follows them in country-first form, but remain cities when
    they are the last meaningful segment or appear in multi-city lists."""
    segs = [s for s in segs if s]
    if not segs:
        return []

    def is_country(s):
        return s in _COUNTRY_NOISE

    def is_state_abbr(s):
        return s in _US_STATE_ABBR

    def is_state_name(s):
        return s in _US_STATE_NAMES

    def is_pure_state(s):
        # Full state name that is not also a major city listing.
        return is_state_name(s) and s not in _CITY_STATE_NAMES

    def is_city_state(s):
        return s in _CITY_STATE_NAMES

    has_state_abbr = any(is_state_abbr(s) for s in segs)

    # Country- or pure-state-first: "Poland, Gdansk", "US, Arizona, Phoenix",
    # "US, Washington, Redmond", "US, New York, Buffalo", "Arizona, Phoenix".
    # Skip leading geo noise and intermediate state names (including city-states
    # that are followed by a later real city); keep city segment(s).
    if is_country(segs[0]) or is_pure_state(segs[0]):
        out = []
        for s in segs[1:]:
            if is_country(s) or is_state_abbr(s) or is_pure_state(s):
                continue
            if len(s) > 1:
                out.append(s)
        # City-states act as states when a later non-city-state city exists
        # ("US, New York, Buffalo" → buffalo; "US, Washington, Redmond" →
        # redmond) but stay as the city when they are alone ("US, New York").
        if any(is_city_state(s) for s in out) and any(not is_city_state(s) for s in out):
            out = [s for s in out if not is_city_state(s)]
        return out

    # Classic "City, ST" / "City, ST, Country" — first segment is the city.
    if has_state_abbr:
        head = segs[0]
        if (len(head) > 1 and not is_country(head) and not is_state_abbr(head)):
            return [head]
        return []

    # "City, FullStateName[, Country]" — including city-states in the *state*
    # position ("Seattle, Washington") so we do not emit a spurious washington
    # city token that would bridge Seattle postings with DC.
    if len(segs) >= 2 and is_state_name(segs[1]):
        head = segs[0]
        if len(head) > 1 and not is_country(head) and not is_state_abbr(head):
            return [head]
        return []

    # No state structure: multi-city list ("New York, Chicago") or "City, Country".
    # Drop countries and pure-only state names; keep city-states (new york).
    out = []
    for s in segs:
        if is_country(s) or is_state_abbr(s) or is_pure_state(s):
            continue
        if len(s) > 1:
            out.append(s)
    return out


def location_tokens(location):
    """Set of canonical city tokens for a location string. Collapses country
    suffixes, "Multi Locations:" prefixes, facility tags, and country-first
    orderings so the many spellings of one city reduce to the same token.
    Empty set when the string names no city (bare state, "United States",
    "Remote", "2 Locations", "")."""
    loc = _squash(location)
    if not loc or _LOC_PURE_NOISE_RE.match(loc):
        return frozenset()
    # Bare country / work-mode with no city.
    if loc in _COUNTRY_NOISE or loc in ("remote", "hybrid", "onsite", "on site",
                                         "on-site"):
        return frozenset()
    loc = re.sub(r"^multi(ple)? locations?\s*:", "", loc).strip()
    cities = set()
    for piece in re.split(r"[;/]| and ", loc):
        piece = piece.strip()
        if not piece:
            continue
        piece = _LOC_PLUS_N_RE.sub("", piece).strip()
        piece = _LOC_FACILITY_RE.sub(" ", piece)
        piece = piece.replace("(", " ").replace(")", " ")
        # Also drop inline country phrases so "Dallas, TX, United States" segs
        # cleanly become [dallas, tx].
        raw_segs = [s.strip() for s in piece.split(",") if s.strip()]
        segs = []
        for raw in raw_segs:
            # Country phrases may still contain punctuation (u.s.a.) — clean first.
            # Keep country segments: _cities_from_segments needs them to detect
            # country-first form so city-states can act as states when a later
            # city follows ("US, Washington, Redmond" → redmond only).
            cleaned = _clean_loc_seg(raw)
            if not cleaned:
                continue
            segs.append(cleaned)
        for city in _cities_from_segments(segs):
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


# Tokens this long are treated as global ATS ids (Greenhouse gh_jid /jobs/N,
# Workday _JR / _R-, embed token=). Cross-company collision risk is negligible,
# so company string drift ("IMC" vs "IMC Trading") must not block the merge.
_STRONG_TOKEN_LEN = 7


# Classic legal-entity trailers peeled when comparing company names for the
# short-token guard. Keep this set tight: branding words like "group",
# "capital", "technologies" are part of firm identity and must stay so
# "Capital One" / "Capital Group" do not collapse via a peeled ["capital"].
_COMPANY_LEGAL_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "corp", "corporation", "company", "co",
    "limited", "plc", "lp", "llp", "pllc", "pc",
})


def _company_words(name):
    """Normalize a company string to significant word tokens.

    Lowercase, strip leading "the ", split on non-alphanumerics, peel trailing
    legal suffixes (Inc/LLC/...). Returns [] for empty/missing names.
    """
    t = _squash(name)
    if not t:
        return []
    if t.startswith("the "):
        t = t[4:]
    words = re.findall(r"[a-z0-9]+", t)
    while words and words[-1] in _COMPANY_LEGAL_SUFFIXES:
        words.pop()
    return words


def companies_compatible(a, b):
    """True when two company strings look like the same firm under light drift.

    Used only as a guard for short real_job_tokens (< _STRONG_TOKEN_LEN): two
    unrelated companies could theoretically reuse a small numeric id like
    "10838". Long tokens skip this check entirely.

    Match is *word-sequence* based (not unanchored substring / shared first
    word alone), so Meta≠Metabase, Apple≠Pineapple, AMD≠Ramda, SAP≠ASAP,
    Bank of America≠Bank of Montreal, Capital One≠Capital Group:
      - both non-empty after normalize
      - exact equal word lists, OR
      - one list is a whole-word prefix of the other
        (e.g. ["imc"] ⊏ ["imc","trading"], ["susquehanna"] ⊏
        ["susquehanna","investment","group"]), requiring first word length
        >= 2 and shorter length >= 1
    Empty / missing company never counts as compatible (fail closed).
    """
    wa, wb = _company_words(a), _company_words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    # Word-sequence prefix: shorter list must equal the longer's leading words.
    if len(wa) > len(wb):
        wa, wb = wb, wa
    if len(wa[0]) < 2:
        return False
    return wb[:len(wa)] == wa


def token_can_join(token, cluster, row):
    """True if `row` may merge into `cluster` under real_job_token identity.

    Same non-null token is required by the caller. Strong tokens (len >= 7)
    always join. Short tokens also need company compatibility with every
    existing member so a shared short id across unrelated firms stays separate.
    """
    if not token:
        return False
    if len(token) >= _STRONG_TOKEN_LEN:
        return True
    for m in cluster:
        if not companies_compatible(m.get("company"), row.get("company")):
            return False
    return True


def cluster_by_job_token(rows):
    """Group rows that share the same real_job_token (with short-token company
    guard). Rows without a token are returned as singletons. Returns a list of
    row-lists suitable for _collapse.

    This is the pass that collapses "IMC" vs "IMC Trading" / title seasoning
    when both URLs embed the same gh_jid -- content clustering cannot see them
    because it buckets on exact (company, canon_title).
    """
    # token -> list of clusters (multiple when a short token collides across
    # incompatible companies)
    by_tok = {}
    no_tok = []
    for row in rows:
        tok = real_job_token(row.get("url"))
        if not tok:
            no_tok.append([row])
            continue
        clusters = by_tok.setdefault(tok, [])
        for c in clusters:
            if token_can_join(tok, c, row):
                c.append(row)
                break
        else:
            clusters.append([row])
    groups = []
    for clusters in by_tok.values():
        groups.extend(clusters)
    groups.extend(no_tok)
    return groups


def _can_join(cluster, row):
    """True if `row` is the same posting as an existing `cluster` of rows that
    already share (canon_company, canon_title).

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
    (canon_company, canon_title) then greedily first-fits each row into a
    compatible cluster (see _can_join). Returns a list of row-lists.

    ponytail: greedy first-fit is order-sensitive and O(n^2) within a bucket;
    buckets are tiny (a handful of rows per company+role) so it does not
    matter. Upgrade to real connected-components only if a bucket ever gets big.
    """
    buckets = {}
    for r in rows:
        buckets.setdefault((canon_company(r.get("company")),
                            canon_title(r.get("title"))), []).append(r)
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
    2. Same real_job_token (ATS id in the URL) regardless of company/title
       string drift -- e.g. "IMC" vs "IMC Trading" sharing gh_jid 4823924101.
       None never merges. Short tokens (< 7 chars) also require company
       compatibility so unrelated firms cannot collide on a small numeric id.
    3. Content clustering: (company, canon_title) buckets, then location
       overlap + non-conflicting job ids -- collapses multi-source near-dupes
       with drifting location/title spellings while keeping distinct reqs
       (NXP _R- ids) and distinct cities apart.

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

    # Pass 2: same real_job_token is the same posting even when company/title
    # strings drift across sources (content clustering never sees them).
    token_groups = {i: g for i, g in enumerate(cluster_by_job_token(after_url))}
    after_token, removed_token, migrated_token = _collapse(
        token_groups, descs=descs
    )

    # Pass 3: cluster same-posting rows on the token-collapsed set. Groups by
    # (canon_company, canon_title) then merges rows whose locations overlap and
    # whose embedded real job ids don't conflict -- collapses the same opening
    # arriving from many sources with drifting company/location/title spellings,
    # while keeping genuinely distinct reqs (NXP _R- ids) and distinct cities
    # apart.
    content_groups = {i: g for i, g in enumerate(cluster_content(after_token))}
    result_rows, removed_content, migrated_content = _collapse(
        content_groups, descs=descs
    )

    result_rows = sorted(result_rows, key=lambda r: r.get("scraped_at") or "")
    # descriptions first, then all.json: crash between leaves bodies under the
    # kept id even if duplicate rows still exist (re-dedupe is a no-op fill).
    if migrated_url + migrated_token + migrated_content:
        desc_store.save(descs, path)
    _atomic_write(result_rows, path)
    return removed_url + removed_token + removed_content, total


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

    # canon_company: legal suffixes + punctuation + short-form aliases
    assert canon_company("Palantir") == canon_company("Palantir Technologies") == "palantir"
    assert canon_company("Intel") == canon_company("Intel Corporation") == "intel"
    assert canon_company("ETCHED") == canon_company("Etched") == "etched"
    assert canon_company("D. E. Shaw") == canon_company("D.E. Shaw") \
        == canon_company("D. E. Shaw & Co.") == canon_company("The D. E. Shaw Group")
    assert canon_company("BAE Systems") == canon_company("BAE Systems, Inc.")
    assert canon_company("Ginkgo Bioworks") == canon_company("Ginkgo Bioworks Inc.")
    assert canon_company("STOKE Space Technologies") == canon_company("Stoke Space")
    assert canon_company("IPConfigure") == canon_company("IPConfigure Inc.")
    assert canon_company("Agilent") == canon_company("Agilent Technologies")
    assert canon_company("Pony.ai") == canon_company("pony.ai")
    assert canon_company("Persona AI") == canon_company("Persona AI Inc")
    assert (canon_company("Voloridge Investment Management")
            == canon_company("Voloridge Investment Management, LLC"))
    assert canon_company("IMC") == canon_company("IMC Trading") == "imctrading"
    assert canon_company("Tower Research") == canon_company("Tower Research Capital")
    assert (canon_company("Aquatic") == canon_company("Aquatic Capital")
            == canon_company("Aquatic Capital Management"))
    assert canon_company("Old Mission") == canon_company("Old Mission Capital")
    # SIG: bare / Investment Group / International Group all share one key
    assert (canon_company("Susquehanna")
            == canon_company("Susquehanna Investment Group")
            == canon_company("Susquehanna International Group")
            == "susquehanna")
    # optional legal tails
    assert canon_company("BAE Systems") == canon_company("BAE Systems plc")
    assert canon_company("Acme") == canon_company("Acme Incorporated")
    # safety: meaningful second words and renames stay distinct
    assert canon_company("Google") != canon_company("Google Cloud")
    assert canon_company("Fab2") != canon_company("Atomic Semi")
    assert canon_company("Acme") == canon_company("Acme Technologies")  # suffix peel intended
    # non-suffix second words must NOT alias (high-volume internship employers)
    assert canon_company("Citadel") != canon_company("Citadel Securities")
    assert canon_company("Citadel") == "citadel"
    assert canon_company("Citadel Securities") == "citadelsecurities"
    # Jump Trading Group peels Group; bare "Jump" (not in live data) stays distinct
    assert canon_company("Jump Trading") == canon_company("Jump Trading Group") == "jumptrading"
    assert canon_company("Jump") != canon_company("Jump Trading")
    # suffix-only names fall back to pre-peel alphanum (not a shared "" bucket)
    assert canon_company("Inc") == "inc"
    assert canon_company("Group") == "group"
    assert canon_company("Technologies") == "technologies"
    assert canon_company("") == ""

    # canon_title: trailing season / year / work-mode tags collapse to one role
    assert canon_title("Software Engineer Intern - Summer 2027") == "software engineer intern"
    assert canon_title("Software Engineer Intern (Remote)") == "software engineer intern"
    assert canon_title("SWE Intern, Fall 2026") == "swe intern"
    assert canon_title("SWE Intern 2027") == "swe intern"
    assert canon_title("ML Intern (Remote) - Summer 2027") == "ml intern"
    # leading year / season+year peeled symmetrically
    assert canon_title("2027 Software Engineer Intern") == "software engineer intern"
    assert canon_title("Spring 2027 Internship - Software") == "intern software"
    assert canon_title("Summer 2027 Software Engineer Intern") == "software engineer intern"
    # bare leading "Spring" without year is tech (Spring Boot), not a season tag
    assert "spring" in canon_title("Spring Boot Intern")
    assert canon_title("Remote Software Engineer Intern") == "software engineer intern"
    # internship ↔ intern whole-word synonym (cross-source wording)
    assert canon_title("Software Engineering- Internship") \
        == canon_title("Software Engineering Intern") \
        == "software engineering intern"
    assert canon_title("Software Engineer, Internship") == "software engineer intern"
    # C++ keeps its pluses (protected before punctuation strip)
    assert canon_title("Software Engineer Intern - C++, Summer 2027") \
        == "software engineer intern c++"
    # emoji / non-role punctuation already flattened
    assert canon_title("Python Software Engineer Intern 🇺🇸") \
        == "python software engineer intern"
    # ...but a distinguishing word in the middle of the role is NOT a tag
    assert canon_title("Quantitative Developer Intern") != canon_title(
        "Quantitative Software Developer Intern")
    # ...and a role/region qualifier AFTER the word "Internship" must survive --
    # peeling to end-of-string collapsed distinct Palantir/Citadel roles (and
    # different countries) into one. internship→intern still leaves Defense Tech.
    assert canon_title("Software Engineer, Internship - Defense Tech") \
        != canon_title("Software Engineer, Internship - Infrastructure")
    assert "defense tech" in canon_title("Software Engineer, Internship - Defense Tech")
    assert canon_title("Software Engineer, Internship") \
        != canon_title("Software Engineer, Internship - Defense Tech")
    assert canon_title("SWE Intern - US") != canon_title("SWE Intern - Europe")
    # a level/tech qualifier is not a season tag either
    assert canon_title("Data Analyst, MS SQL Server") == "data analyst ms sql server"
    assert "phd" in canon_title("Research Intern PhD")
    assert "bs" in canon_title("SWE Intern BS") or "ms" in canon_title("SWE Intern, MS")

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
    # country-first / state-first: city is a later segment (Intel-style)
    assert location_tokens("US, Arizona, Phoenix") == frozenset({"phoenix"})
    assert location_tokens("US, Arizona, Phoenix") == location_tokens("Phoenix, AZ")
    assert location_tokens("Poland, Gdansk") == frozenset({"gdansk"})
    assert location_tokens("US, Texas, Austin") == location_tokens("Austin, TX")
    assert location_tokens("US, Oregon, Hillsboro, United States of America") \
        == frozenset({"hillsboro"})
    # DC spellings all share a city token
    assert "washington" in location_tokens("Washington D.C.")
    assert location_tokens("Washington D.C.") == location_tokens("Washington DC") \
        == location_tokens("Washington, DC") == location_tokens("D.C.")
    # multi-city without "Multi Locations:" prefix
    assert location_tokens("New York, Chicago") == frozenset({"new york", "chicago"})
    # pure noise counters / bare multi-location labels → empty
    assert location_tokens("2 Locations") == frozenset()
    assert location_tokens("Multiple Locations") == frozenset()
    assert location_tokens("Multi Locations") == frozenset()
    # trailing +N counters and facility tags still resolve to the city
    assert location_tokens("Hillsboro, OR +1") == frozenset({"hillsboro"})
    assert location_tokens("Dallas, TX - Headquarters, United States") \
        == frozenset({"dallas"})
    # bare state / country still empty (must not bridge cities)
    assert location_tokens("United States") == location_tokens("Remote") == frozenset()
    assert location_tokens("California") == location_tokens("Arizona") == frozenset()
    # full state name after city is a state, not a second city (esp. Washington)
    assert location_tokens("Seattle, Washington, United States") == frozenset({"seattle"})
    assert location_tokens("Seattle, Washington") & location_tokens("Washington, DC") == frozenset()
    assert location_tokens("Atlanta, Georgia, United States") == frozenset({"atlanta"})
    # Multi-word US state names must be full phrases in _US_STATE_NAMES (never
    # space-.split fragments). Leaked state tokens would bridge distinct cities
    # that share a multi-word state name (Buffalo↔NYC, Raleigh↔Charlotte, …).
    assert location_tokens("Buffalo, New York") == frozenset({"buffalo"})
    assert location_tokens("Buffalo, New York") & location_tokens("New York, NY") == frozenset()
    assert location_tokens("Raleigh, North Carolina") == frozenset({"raleigh"})
    assert location_tokens("Raleigh, North Carolina") & location_tokens(
        "Charlotte, North Carolina") == frozenset()
    assert location_tokens("Newark, New Jersey") == frozenset({"newark"})
    assert location_tokens("Newark, New Jersey") & location_tokens(
        "Jersey City, New Jersey") == frozenset()
    assert location_tokens("Santa Fe, New Mexico") & location_tokens(
        "US, New Mexico, Albuquerque") == frozenset()
    assert location_tokens("York, PA") == frozenset({"york"})
    assert location_tokens("Charleston, West Virginia") == frozenset({"charleston"})
    assert location_tokens("US, North Carolina, Charlotte") == frozenset({"charlotte"})
    # bare multi-word pure state is empty (must not join city clusters)
    assert location_tokens("North Carolina") == frozenset()
    # city-state allowlist: bare "New York" still names the city
    assert location_tokens("New York") == frozenset({"new york"})
    # Country-first + city-state: city-state is geo when a later city follows,
    # but the city when it is the last meaningful segment. Must not bridge
    # Redmond↔DC or Buffalo↔NYC via a shared city-state token.
    assert location_tokens("United States, Washington, Redmond") == frozenset({"redmond"})
    assert location_tokens("US, New York, Buffalo") == frozenset({"buffalo"})
    assert location_tokens("US, New York") == frozenset({"new york"})  # NY is the city
    assert location_tokens("US, Arizona, Phoenix") == frozenset({"phoenix"})
    assert location_tokens("Washington, DC") == frozenset({"washington"})
    assert location_tokens("New York, NY") == frozenset({"new york"})
    assert location_tokens("Buffalo, New York") == frozenset({"buffalo"})
    assert location_tokens("Seattle, Washington") & location_tokens("Washington, DC") == frozenset()
    assert location_tokens("United States, Washington, Redmond") & location_tokens(
        "Washington, DC") == frozenset()
    assert location_tokens("US, New York, Buffalo") & location_tokens("New York, NY") == frozenset()

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

    # Multi-word state names must not bridge distinct cities for same company+title
    multi_state_cities = [
        {"company": "Acme", "title": "Software Engineer Intern",
         "location": "Buffalo, New York", "url": "https://m/buf"},
        {"company": "Acme", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": "https://m/nyc"},
        {"company": "Acme", "title": "Software Engineer Intern",
         "location": "Raleigh, North Carolina", "url": "https://m/ral"},
        {"company": "Acme", "title": "Software Engineer Intern",
         "location": "Charlotte, North Carolina", "url": "https://m/cha"},
    ]
    msc = cluster_content(multi_state_cities)
    assert len(msc) == 4, [([r["url"] for r in g]) for g in msc]
    buf = next(r for g in msc for r in g if r["url"] == "https://m/buf")
    nyc_r = next(r for g in msc for r in g if r["url"] == "https://m/nyc")
    ral = next(r for g in msc for r in g if r["url"] == "https://m/ral")
    cha = next(r for g in msc for r in g if r["url"] == "https://m/cha")
    assert not any(buf in g and nyc_r in g for g in msc)
    assert not any(ral in g and cha in g for g in msc)

    # Country-first city-state must not merge distinct cities (Redmond↔DC, Buffalo↔NYC)
    country_first_cs = [
        {"company": "Msft", "title": "Software Engineer Intern",
         "location": "United States, Washington, Redmond", "url": "https://cf/red"},
        {"company": "Msft", "title": "Software Engineer Intern",
         "location": "Washington, DC", "url": "https://cf/dc"},
        {"company": "AcmeNY", "title": "Software Engineer Intern",
         "location": "US, New York, Buffalo", "url": "https://cf/buf"},
        {"company": "AcmeNY", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": "https://cf/nyc"},
    ]
    cfc = cluster_content(country_first_cs)
    assert len(cfc) == 4, [([r["url"] for r in g]) for g in cfc]
    red = next(r for g in cfc for r in g if r["url"] == "https://cf/red")
    dc = next(r for g in cfc for r in g if r["url"] == "https://cf/dc")
    buf2 = next(r for g in cfc for r in g if r["url"] == "https://cf/buf")
    nyc2 = next(r for g in cfc for r in g if r["url"] == "https://cf/nyc")
    assert not any(red in g and dc in g for g in cfc)
    assert not any(buf2 in g and nyc2 in g for g in cfc)

    # canon_company in cluster_content: spelling variants of the same firm merge
    # when title + location allow; distinct cities / job tokens still separate.
    palantir_merge = [
        {"company": "Palantir", "title": "Software Engineer, Internship",
         "location": "New York, NY", "url": "https://p/ats"},
        {"company": "Palantir Technologies", "title": "Software Engineer, Internship",
         "location": "New York, NY, United States", "url": "https://p/jobright"},
    ]
    assert sorted(len(g) for g in cluster_content(palantir_merge)) == [2]
    # NY vs Palo Alto stay separate even after company canon
    palantir_cities = [
        {"company": "Palantir", "title": "Software Engineer, Internship",
         "location": "New York, NY", "url": "https://p/ny"},
        {"company": "Palantir Technologies", "title": "Software Engineer, Internship",
         "location": "Palo Alto, CA", "url": "https://p/pa"},
    ]
    assert sorted(len(g) for g in cluster_content(palantir_cities)) == [1, 1]
    # distinct real job tokens still separate under a shared canon_company
    intel_reqs = [
        {"company": "Intel", "title": "SW Intern", "location": "Penang",
         "url": "https://x/job/Penang/SW-Intern_JR0285543"},
        {"company": "Intel Corporation", "title": "SW Intern", "location": "Penang",
         "url": "https://x/job/Penang/SW-Intern_JR0285538"},
    ]
    assert sorted(len(g) for g in cluster_content(intel_reqs)) == [1, 1]
    # short-form alias: IMC vs IMC Trading, same title + city → merge
    imc_merge = [
        {"company": "IMC", "title": "Software Engineer Intern",
         "location": "Chicago, IL", "url": "https://readme/imc"},
        {"company": "IMC Trading", "title": "Software Engineer Intern - Summer 2027",
         "location": "Chicago, United States", "url": "https://ats/imc"},
    ]
    assert sorted(len(g) for g in cluster_content(imc_merge)) == [2]
    # SIG: International Group (majority live spelling) merges with bare /
    # Investment Group when title + Bala Cynwyd location overlap. Titles are
    # peel-compatible so company spelling is the only variable under test.
    sig_merge = [
        {"company": "Susquehanna", "title": "Quantitative Strategy Developer Intern",
         "location": "Bala Cynwyd, PA", "url": "https://sig/bare"},
        {"company": "Susquehanna International Group",
         "title": "Quantitative Strategy Developer Intern - Summer 2027",
         "location": "Bala Cynwyd, PA, United States", "url": "https://sig/intl"},
        {"company": "Susquehanna Investment Group",
         "title": "Quantitative Strategy Developer Intern",
         "location": "Bala Cynwyd", "url": "https://sig/inv"},
    ]
    assert sorted(len(g) for g in cluster_content(sig_merge)) == [3]
    # Citadel vs Citadel Securities: same title+city must stay separate clusters
    citadel_split = [
        {"company": "Citadel", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": "https://citadel/swe"},
        {"company": "Citadel Securities", "title": "Software Engineer Intern",
         "location": "New York, NY", "url": "https://citsec/swe"},
    ]
    assert sorted(len(g) for g in cluster_content(citadel_split)) == [1, 1]

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

    # Pass 2 token identity: same gh_jid, company spelling + title seasoning
    # differ ("IMC Trading" / "IMC", season tags) -- content clustering never
    # sees these (different company bucket). Keep oldest.
    imc_rows = [
        {"company": "IMC Trading", "title": "Software Engineer Intern - Summer 2027",
         "location": "Chicago", "url": "https://job-boards.eu.greenhouse.io/imc/jobs/4823924101",
         "scraped_at": "2026-07-09T01:42:28", "id": "imc_new"},
        {"company": "IMC", "title": "Software Engineer Intern",
         "location": "Chicago, IL", "url": "https://www.imc.com/us/careers/jobs/4823924101",
         "scraped_at": "2026-07-01T00:00:00", "id": "imc_old"},
    ]
    assert real_job_token(imc_rows[0]["url"]) == real_job_token(imc_rows[1]["url"]) == "4823924101"
    # content clustering would keep them separate (different company strings)
    assert sorted(len(g) for g in cluster_content(imc_rows)) == [1, 1]
    p_imc = Path(tempfile.mkdtemp()) / "all.json"
    p_imc.write_text(json.dumps(imc_rows))
    removed, total = dedupe(p_imc)
    assert removed == 1 and total == 2
    imc_out = json.loads(p_imc.read_text())
    assert len(imc_out) == 1 and imc_out[0]["id"] == "imc_old"

    # Anduril title variants, same company, same long gh_jid -- also merges
    # (content would not: "2027 Software Engineer Intern" peels the leading
    # year differently than a trailing tag, so canon_title may differ).
    anduril_rows = [
        {"company": "Anduril", "title": "2027 Software Engineer Intern",
         "location": "Costa Mesa", "url": "https://boards.greenhouse.io/andurilindustries/jobs/5148079007",
         "scraped_at": "2026-07-09T01:42:28", "id": "and_a"},
        {"company": "Anduril", "title": "Software Engineer Intern",
         "location": "Costa Mesa, CA", "url": "https://job-boards.greenhouse.io/andurilindustries/jobs/5148079007",
         "scraped_at": "2026-07-09T01:42:28", "id": "and_b"},
    ]
    assert real_job_token(anduril_rows[0]["url"]) == "5148079007"
    p_and = Path(tempfile.mkdtemp()) / "all.json"
    p_and.write_text(json.dumps(anduril_rows))
    removed, total = dedupe(p_and)
    assert removed == 1 and total == 2
    assert len(json.loads(p_and.read_text())) == 1

    # Short token (len 5) + incompatible companies must NOT merge -- guard
    # against theoretical cross-firm reuse of a small numeric id.
    assert len("10838") < _STRONG_TOKEN_LEN
    assert not companies_compatible("Acme Corp", "Globex Inc")
    short_clash = [
        {"company": "Acme Corp", "title": "Intern", "location": "NY",
         "url": "https://careers.acme.com/jobs/10838",
         "scraped_at": "2026-01-01", "id": "acme"},
        {"company": "Globex Inc", "title": "Intern", "location": "NY",
         "url": "https://careers.globex.com/jobs/10838",
         "scraped_at": "2026-01-02", "id": "globex"},
    ]
    assert real_job_token(short_clash[0]["url"]) == real_job_token(short_clash[1]["url"]) == "10838"
    p_clash = Path(tempfile.mkdtemp()) / "all.json"
    p_clash.write_text(json.dumps(short_clash))
    removed, total = dedupe(p_clash)
    assert removed == 0 and total == 2
    assert {r["id"] for r in json.loads(p_clash.read_text())} == {"acme", "globex"}

    # Short token + compatible company spellings DO merge (Susquehanna case)
    assert companies_compatible("Susquehanna", "Susquehanna Investment Group")
    short_ok = [
        {"company": "Susquehanna", "title": "Quant Intern", "location": "Bala",
         "url": "https://careers.sig.com/jobs/10838",
         "scraped_at": "2026-01-01", "id": "sig1"},
        {"company": "Susquehanna Investment Group", "title": "Quant Intern",
         "location": "Bala Cynwyd", "url": "https://careers.sig.com/intern/jobs/10838",
         "scraped_at": "2026-01-02", "id": "sig2"},
    ]
    p_sig = Path(tempfile.mkdtemp()) / "all.json"
    p_sig.write_text(json.dumps(short_ok))
    removed, total = dedupe(p_sig)
    assert removed == 1 and total == 2
    assert json.loads(p_sig.read_text())[0]["id"] == "sig1"

    # Short-token company guard: word-sequence prefix, NOT unanchored substring
    # or shared first word. These pairs must stay separate under a short id.
    for a, b in (
        ("Meta", "Metabase"),
        ("AMD", "Ramda"),
        ("Apple", "Pineapple"),
        ("SAP", "ASAP"),
        ("Bank of America", "Bank of Montreal"),
        ("Capital One", "Capital Group"),
        ("Acme Corp", "Globex Inc"),
    ):
        assert not companies_compatible(a, b), (a, b)
    # Positive word-prefix / equal cases still hold (legal suffix peeled).
    assert companies_compatible("IMC", "IMC Trading")
    assert companies_compatible("Tower Research", "Tower Research Capital")
    assert companies_compatible("BAE Systems", "BAE Systems, Inc.")
    assert companies_compatible("Acme Corp", "Acme")
    assert not companies_compatible("", "Meta")
    assert not companies_compatible(None, "Meta")
    assert not companies_compatible("Meta", "")

    def _short_pair(co_a, co_b, id_a="a", id_b="b"):
        return [
            {"company": co_a, "title": "Intern", "location": "NY",
             "url": "https://careers.example.com/jobs/10838",
             "scraped_at": "2026-01-01", "id": id_a},
            {"company": co_b, "title": "Intern", "location": "NY",
             "url": "https://careers.other.com/jobs/10838",
             "scraped_at": "2026-01-02", "id": id_b},
        ]

    # Meta / Metabase + short token 10838: substring would false-match; keep 2
    for co_a, co_b, tag in (
        ("Meta", "Metabase", "meta"),
        ("AMD", "Ramda", "amd"),
        ("Apple", "Pineapple", "apple"),
    ):
        rows = _short_pair(co_a, co_b, f"{tag}1", f"{tag}2")
        assert real_job_token(rows[0]["url"]) == "10838"
        assert not companies_compatible(co_a, co_b)
        p = Path(tempfile.mkdtemp()) / "all.json"
        p.write_text(json.dumps(rows))
        removed, total = dedupe(p)
        assert removed == 0 and total == 2, (co_a, co_b, removed)
        assert {r["id"] for r in json.loads(p.read_text())} == {f"{tag}1", f"{tag}2"}

    # Empty company on either side + short token: fail closed, no merge
    for co_a, co_b in (("", "Acme"), ("Acme", ""), (None, "Acme")):
        rows = _short_pair(co_a if co_a is not None else "", co_b if co_b is not None else "",
                           "e1", "e2")
        if co_a is None:
            rows[0]["company"] = None
        if co_b is None:
            rows[1]["company"] = None
        p = Path(tempfile.mkdtemp()) / "all.json"
        p.write_text(json.dumps(rows))
        removed, total = dedupe(p)
        assert removed == 0 and total == 2, (co_a, co_b, removed)

    # Distinct tokens stay two rows (NXP R-1001 vs R-1002 already covered;
    # also greenhouse-style long ids that differ).
    distinct_tok = [
        {"company": "Akuna Capital", "title": "SWE Intern - Python",
         "location": "Chicago", "url": "https://www.akunacapital.com/careers/job/8018853/?gh_jid=8018853",
         "scraped_at": "2026-01-01", "id": "ak1"},
        {"company": "Akuna Capital", "title": "SWE Intern - C++",
         "location": "Chicago", "url": "https://www.akunacapital.com/careers/job/8018847/?gh_jid=8018847",
         "scraped_at": "2026-01-02", "id": "ak2"},
    ]
    assert real_job_token(distinct_tok[0]["url"]) != real_job_token(distinct_tok[1]["url"])
    p_ak = Path(tempfile.mkdtemp()) / "all.json"
    p_ak.write_text(json.dumps(distinct_tok))
    removed, total = dedupe(p_ak)
    assert removed == 0 and total == 2

    # Description fill from dropped token-sibling: kept id empty, dropped has body
    tok_desc = [
        {"company": "Tower Research Capital", "title": "Quant Dev Intern - Summer 2027",
         "location": "NY", "url": "https://www.tower-research.com/open-positions/?gh_jid=8044334",
         "scraped_at": "2026-07-09T01:42:28", "id": "tw_new"},
        {"company": "Tower Research", "title": "Quant Dev Intern",
         "location": "New York", "url": "https://tower-research.com/open-positions/?gh_jid=8044334",
         "scraped_at": "2026-07-01T00:00:00", "id": "tw_old"},
    ]
    p_td = Path(tempfile.mkdtemp()) / "all.json"
    p_td.write_text(json.dumps(tok_desc))
    desc_store.put("tw_new", "TOWER BODY FROM NEWER SOURCE", p_td)
    assert not desc_store.has("tw_old", p_td)
    removed, total = dedupe(p_td)
    assert removed == 1 and total == 2
    assert json.loads(p_td.read_text())[0]["id"] == "tw_old"
    assert desc_store.get("tw_old", p_td) == "TOWER BODY FROM NEWER SOURCE"

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
