"""Shared listing type. Every source emits these; nothing downstream cares
which source a listing came from beyond the `source` field."""
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from internships.filters import company_priority, year_relevance


# Query params that only track how someone arrived at a posting -- stripping
# them is what lets two sources reporting the same job (one with
# ?utm_source=github-..., one clean) share a dedup identity. Must NOT include
# identity-bearing params like gh_jid / token / for / jobCode / req: many
# Greenhouse-hosted career sites put the real job id ONLY in the query
# string (e.g. https://www.jumptrading.com/hr/job?gh_jid=7565728), and
# dropping those would collapse every role at that company into one id.
_TRACKING_QUERY_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "utm_id", "utm_reader", "utm_name", "utm_social", "utm_social-type",
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid",
    "mc_cid", "mc_eid",
    "ref",
    "jr_id",  # vansh-ouckah / similar README-list click trackers
    "iis", "iisn",  # LinkedIn apply trackers
    "no_int_redir",  # amazon.jobs
    "utm",  # nonstandard short form seen in tests / some boards
})

# Greenhouse serves the same board+job under several hostnames. Collapse to
# one so boards.greenhouse.io/... and job-boards.greenhouse.io/... (and the
# .eu variants) share a dedup identity. Path + identity query stay intact.
_GREENHOUSE_HOSTS = frozenset({
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
})
_GREENHOUSE_CANON_HOST = "boards.greenhouse.io"

# Workday career sites optional-locale prefix: /en-US/External/job/... vs
# /External/job/... are the same posting. Match en, en-US, en-GB, fr-FR, …
_WORKDAY_LOCALE_RE = re.compile(r"^[A-Za-z]{2}(-[A-Za-z]{2})?$")


def normalize_url(url: str) -> str:
    """Canonical form of a listing URL for dedup identity.

    Strips the fragment and known tracking query params (utm_*, fbclid,
    jr_id, ...), but keeps identity-bearing query params like gh_jid /
    token / for -- boards such as Jump Trading, MongoDB, and Greenhouse
    embed put the job id only in the query string, and wiping those would
    make two different roles share a Listing.id() (and cause
    --recompute dedup / cross-source known_urls to drop one of them).

    Also folds common non-identity URL variance so the same posting from two
    sources merges:
      - scheme / host lowercased; leading ``www.`` stripped
      - Greenhouse host aliases → boards.greenhouse.io
      - Workday ``/en-US/`` (and similar locale) path prefix stripped
      - path lowercased (ATS job ids are numeric/UUID) and trailing slash
        removed

    Remaining query params are sorted so param order doesn't affect the
    identity. self.url itself is never mutated -- only the id / known_urls
    basis uses this form."""
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _GREENHOUSE_HOSTS:
        host = _GREENHOUSE_CANON_HOST

    path = parts.path or ""
    if host.endswith(".myworkdayjobs.com"):
        segments = path.split("/")
        # path "/en-US/External/job/..." → ['', 'en-US', 'External', 'job', ...]
        if len(segments) > 1 and _WORKDAY_LOCALE_RE.fullmatch(segments[1] or ""):
            segments.pop(1)
            path = "/".join(segments)
    path = path.lower().rstrip("/")

    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_QUERY_PARAMS or kl.startswith("utm_"):
            continue
        kept.append((k, v))
    kept.sort()
    query = urlencode(kept, doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


@dataclass(frozen=True)
class Listing:
    source: str
    company: str
    title: str
    location: str
    url: str
    posted: str = ""
    extra_text: str = ""  # description/body text, used only for 2027 detection

    def id(self) -> str:
        """Stable identity for dedup across runs. url is the best unique key
        an ATS/board gives us; fall back to title when a source has none.
        self.url itself is untouched (the real apply link shown in the UI)
        -- only the id() basis uses the normalized form."""
        basis = f"{self.source}|{self.company}|{normalize_url(self.url) or self.title}|{self.location}"
        return hashlib.sha1(basis.encode()).hexdigest()[:16]

    @property
    def is_2027(self) -> bool:
        """True unless title/location explicitly name a non-2027 year --
        same "no year stated = maybe 2027" logic as filters.year_relevance,
        which is what decides whether this listing was kept at all."""
        return year_relevance(self.title, self.location, self.extra_text) != "no"

    @property
    def priority(self) -> int:
        """1 = big tech/top-tier-elite, 2 = mid tech, 3 = everything else
        (default) -- see filters.company_priority()."""
        return company_priority(self.company)


def selftest():
    a = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    b = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    c = Listing("x", "Acme", "SWE Intern", "SF", "http://a/2")
    assert a.id() == b.id()
    assert a.id() != c.id()

    # tracking query string / fragment must not affect dedup identity...
    d = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1?utm_source=foo&ref=bar")
    e = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1#section")
    assert a.id() == d.id() == e.id()
    # ...but the stored url field itself must stay fully intact (real apply link)
    assert d.url == "http://a/1?utm_source=foo&ref=bar"
    assert e.url == "http://a/1#section"
    assert normalize_url("http://a/1?utm_source=foo&ref=bar") == "http://a/1"
    assert normalize_url("http://a/1?utm=x") == "http://a/1"

    # identity-bearing query params (gh_jid, token, for, ...) MUST survive
    # normalize_url -- otherwise every Jump-Trading-style role that shares a
    # path and only differs by ?gh_jid= collapses to one Listing.id().
    # www. is stripped (host fold); gh_jid stays.
    j1 = Listing("ats_boards", "Jump Trading", "SWE Intern A", "Chicago",
                 "https://www.jumptrading.com/hr/job?gh_jid=111")
    j2 = Listing("ats_boards", "Jump Trading", "SWE Intern B", "Chicago",
                 "https://www.jumptrading.com/hr/job?gh_jid=222")
    assert j1.id() != j2.id(), (j1.id(), j2.id(), normalize_url(j1.url), normalize_url(j2.url))
    assert normalize_url(j1.url) == "https://jumptrading.com/hr/job?gh_jid=111"
    # tracking noise alongside gh_jid still strips cleanly, order-stable
    assert normalize_url(
        "https://www.jumptrading.com/hr/job?utm_source=gh&gh_jid=111&jr_id=abc"
    ) == "https://jumptrading.com/hr/job?gh_jid=111"
    assert normalize_url(
        "https://www.jumptrading.com/hr/job?gh_jid=111&utm_source=gh"
    ) == "https://jumptrading.com/hr/job?gh_jid=111"
    # Greenhouse embed: for + token are the job identity
    emb1 = "https://boards.greenhouse.io/embed/job_app?for=gemini&token=111"
    emb2 = "https://boards.greenhouse.io/embed/job_app?for=gemini&token=222"
    assert normalize_url(emb1) != normalize_url(emb2)
    g1 = Listing("github_readme", "Gemini", "SWE Intern Fall", "", emb1)
    g2 = Listing("github_readme", "Gemini", "SWE Intern Spring", "", emb2)
    assert g1.id() != g2.id()

    # --- live-data URL variants that must collapse (from out/all.json) ---

    # www vs bare host (+ trailing slash / tracking)
    assert normalize_url(
        "https://www.tower-research.com/open-positions/?gh_jid=8044334"
    ) == normalize_url(
        "https://tower-research.com/open-positions/?gh_jid=8044334&utm_source=github-vansh-ouckah"
    ) == "https://tower-research.com/open-positions?gh_jid=8044334"
    assert normalize_url(
        "https://www.akunacapital.com/careers/job/8018847/?gh_jid=8018847"
    ) == normalize_url(
        "https://akunacapital.com/careers/job/8018847/?gh_jid=8018847"
    ) == "https://akunacapital.com/careers/job/8018847?gh_jid=8018847"
    assert normalize_url(
        "https://www.stokespace.com/careers/current-openings/?gh_jid=5987663004"
        "&jr_id=69fae0acd21cf86d1e3cd79c&utm_source=github-vansh-ouckah"
    ) == normalize_url(
        "https://stokespace.com/careers/current-openings?gh_jid=5987663004"
    ) == "https://stokespace.com/careers/current-openings?gh_jid=5987663004"

    # trailing slash
    assert normalize_url(
        "https://www.janestreet.com/join-jane-street/position/8599644002"
    ) == normalize_url(
        "https://www.janestreet.com/join-jane-street/position/8599644002/"
    ) == "https://janestreet.com/join-jane-street/position/8599644002"
    assert normalize_url(
        "https://www.citadel.com/careers/details/software-engineer-intern-us/"
    ) == normalize_url(
        "https://www.citadel.com/careers/details/software-engineer-intern-us"
    ) == "https://citadel.com/careers/details/software-engineer-intern-us"

    # path case (Ashby board slug, DESHAW slug)
    assert normalize_url(
        "https://jobs.ashbyhq.com/Deepgram/93a6e6ee-3391-4437-8459-e28eb05eace7"
    ) == normalize_url(
        "https://jobs.ashbyhq.com/deepgram/93a6e6ee-3391-4437-8459-e28eb05eace7"
    ) == "https://jobs.ashbyhq.com/deepgram/93a6e6ee-3391-4437-8459-e28eb05eace7"
    assert normalize_url(
        "https://www.deshaw.com/careers/Software-Developer-Intern-New-York-Summer-2027-5894"
    ) == normalize_url(
        "https://www.deshaw.com/careers/software-developer-intern-new-york-summer-2027-5894"
    ) == "https://deshaw.com/careers/software-developer-intern-new-york-summer-2027-5894"

    # Greenhouse host aliases (same board + job id)
    gh_a = "https://boards.greenhouse.io/andurilindustries/jobs/5148079007?gh_jid=5148079007"
    gh_b = (
        "https://job-boards.greenhouse.io/andurilindustries/jobs/5148079007"
        "?gh_jid=5148079007&utm_source=github-vansh-ouckah"
    )
    gh_eu = (
        "https://job-boards.eu.greenhouse.io/andurilindustries/jobs/5148079007"
        "?gh_jid=5148079007"
    )
    assert normalize_url(gh_a) == normalize_url(gh_b) == normalize_url(gh_eu) == (
        "https://boards.greenhouse.io/andurilindustries/jobs/5148079007?gh_jid=5148079007"
    )
    # different job ids on greenhouse must stay distinct
    assert normalize_url(
        "https://job-boards.greenhouse.io/andurilindustries/jobs/111"
    ) != normalize_url(
        "https://boards.greenhouse.io/andurilindustries/jobs/222"
    )

    # Workday locale prefix + path case
    wd_a = (
        "https://intel.wd1.myworkdayjobs.com/External/job/US-Oregon-Hillsboro/"
        "AI-Software-Intern_JR0282639"
    )
    wd_b = (
        "https://intel.wd1.myworkdayjobs.com/en-US/external/job/US-Oregon-Hillsboro/"
        "AI-Software-Intern_JR0282639"
    )
    assert normalize_url(wd_a) == normalize_url(wd_b) == (
        "https://intel.wd1.myworkdayjobs.com/external/job/us-oregon-hillsboro/"
        "ai-software-intern_jr0282639"
    )
    # en-GB (and similar) also stripped; JR id path segment kept
    assert normalize_url(
        "https://accenture.wd103.myworkdayjobs.com/en-GB/AvanadeCareers/job/"
        "Los-Angeles/Intern_R00319370"
    ) == (
        "https://accenture.wd103.myworkdayjobs.com/avanadecareers/job/"
        "los-angeles/intern_r00319370"
    )
    # different Workday requisitions must stay distinct
    assert normalize_url(
        "https://intel.wd1.myworkdayjobs.com/External/job/X/Y_JR0282639"
    ) != normalize_url(
        "https://intel.wd1.myworkdayjobs.com/External/job/X/Y_JR0282640"
    )

    assert Listing("x", "A", "SWE Intern Summer 2027", "SF", "u").is_2027
    # no year stated at all -- "maybe 2027" counts as is_2027, same as year_relevance
    assert Listing("x", "A", "SWE Intern", "SF", "u").is_2027
    assert not Listing("x", "A", "SWE Intern Summer 2026", "SF", "u").is_2027
    # bounded match: '2027' must be a standalone year, not a substring of a
    # longer number like a req ID or address -- these count as "no year found"
    # (i.e. maybe 2027), not a false "definitely 2027" match
    assert Listing("x", "A", "SWE Intern (Req 20271)", "SF", "u").is_2027
    assert Listing("x", "A", "SWE Intern", "120275 Main St", "u").is_2027

    # priority delegates to filters.company_priority(self.company) -- the
    # classification itself is filters.py's own selftest's job
    import sys
    _self = sys.modules[__name__]
    orig = _self.company_priority
    _self.company_priority = lambda company: 1 if company == "Notable Co" else 3
    try:
        assert Listing("x", "Notable Co", "SWE Intern", "SF", "u").priority == 1
        assert Listing("x", "Nobody Inc", "SWE Intern", "SF", "u").priority == 3
    finally:
        _self.company_priority = orig
    print("models selftest OK")


if __name__ == "__main__":
    selftest()
