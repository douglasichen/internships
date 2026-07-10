"""Shared listing type. Every source emits these; nothing downstream cares
which source a listing came from beyond the `source` field."""
import hashlib
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


def normalize_url(url: str) -> str:
    """Canonical form of a listing URL for dedup identity.

    Strips the fragment and known tracking query params (utm_*, fbclid,
    jr_id, ...), but KEeps identity-bearing query params like gh_jid /
    token / for -- boards such as Jump Trading, MongoDB, and Greenhouse
    embed put the job id only in the query string, and wiping those would
    make two different roles share a Listing.id() (and cause
    --recompute dedup / cross-source known_urls to drop one of them).

    Remaining query params are sorted so param order doesn't affect the
    identity. self.url itself is never mutated -- only the id / known_urls
    basis uses this form."""
    if not url:
        return ""
    parts = urlsplit(url)
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _TRACKING_QUERY_PARAMS or kl.startswith("utm_"):
            continue
        kept.append((k, v))
    kept.sort()
    query = urlencode(kept, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


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
    j1 = Listing("ats_boards", "Jump Trading", "SWE Intern A", "Chicago",
                 "https://www.jumptrading.com/hr/job?gh_jid=111")
    j2 = Listing("ats_boards", "Jump Trading", "SWE Intern B", "Chicago",
                 "https://www.jumptrading.com/hr/job?gh_jid=222")
    assert j1.id() != j2.id(), (j1.id(), j2.id(), normalize_url(j1.url), normalize_url(j2.url))
    assert normalize_url(j1.url) == "https://www.jumptrading.com/hr/job?gh_jid=111"
    # tracking noise alongside gh_jid still strips cleanly, order-stable
    assert normalize_url(
        "https://www.jumptrading.com/hr/job?utm_source=gh&gh_jid=111&jr_id=abc"
    ) == "https://www.jumptrading.com/hr/job?gh_jid=111"
    assert normalize_url(
        "https://www.jumptrading.com/hr/job?gh_jid=111&utm_source=gh"
    ) == "https://www.jumptrading.com/hr/job?gh_jid=111"
    # Greenhouse embed: for + token are the job identity
    emb1 = "https://boards.greenhouse.io/embed/job_app?for=gemini&token=111"
    emb2 = "https://boards.greenhouse.io/embed/job_app?for=gemini&token=222"
    assert normalize_url(emb1) != normalize_url(emb2)
    g1 = Listing("github_readme", "Gemini", "SWE Intern Fall", "", emb1)
    g2 = Listing("github_readme", "Gemini", "SWE Intern Spring", "", emb2)
    assert g1.id() != g2.id()

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
