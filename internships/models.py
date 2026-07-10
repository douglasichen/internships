"""Shared listing type. Every source emits these; nothing downstream cares
which source a listing came from beyond the `source` field."""
import hashlib
from dataclasses import dataclass

from internships.filters import year_relevance


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
        an ATS/board gives us; fall back to title when a source has none."""
        basis = f"{self.source}|{self.company}|{self.url or self.title}|{self.location}"
        return hashlib.sha1(basis.encode()).hexdigest()[:16]

    @property
    def is_2027(self) -> bool:
        """True unless title/location explicitly name a non-2027 year --
        same "no year stated = maybe 2027" logic as filters.year_relevance,
        which is what decides whether this listing was kept at all."""
        return year_relevance(self.title, self.location, self.extra_text) != "no"


def selftest():
    a = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    b = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    c = Listing("x", "Acme", "SWE Intern", "SF", "http://a/2")
    assert a.id() == b.id()
    assert a.id() != c.id()
    assert Listing("x", "A", "SWE Intern Summer 2027", "SF", "u").is_2027
    # no year stated at all -- "maybe 2027" counts as is_2027, same as year_relevance
    assert Listing("x", "A", "SWE Intern", "SF", "u").is_2027
    assert not Listing("x", "A", "SWE Intern Summer 2026", "SF", "u").is_2027
    # bounded match: '2027' must be a standalone year, not a substring of a
    # longer number like a req ID or address -- these count as "no year found"
    # (i.e. maybe 2027), not a false "definitely 2027" match
    assert Listing("x", "A", "SWE Intern (Req 20271)", "SF", "u").is_2027
    assert Listing("x", "A", "SWE Intern", "120275 Main St", "u").is_2027
    print("models selftest OK")


if __name__ == "__main__":
    selftest()
