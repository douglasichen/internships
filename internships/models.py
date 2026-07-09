"""Shared listing type. Every source emits these; nothing downstream cares
which source a listing came from beyond the `source` field."""
import hashlib
import re
from dataclasses import dataclass

Y2027_RE = re.compile(r"\b20\s?27\b")


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
        return any(Y2027_RE.search(t) for t in (self.title, self.location, self.extra_text) if t)


def selftest():
    a = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    b = Listing("x", "Acme", "SWE Intern", "SF", "http://a/1")
    c = Listing("x", "Acme", "SWE Intern", "SF", "http://a/2")
    assert a.id() == b.id()
    assert a.id() != c.id()
    assert Listing("x", "A", "SWE Intern Summer 2027", "SF", "u").is_2027
    assert not Listing("x", "A", "SWE Intern", "SF", "u").is_2027
    # bounded match: '2027' must be a standalone year, not a substring of a
    # longer number like a req ID or address
    assert not Listing("x", "A", "SWE Intern (Req 20271)", "SF", "u").is_2027
    assert not Listing("x", "A", "SWE Intern", "120275 Main St", "u").is_2027
    print("models selftest OK")


if __name__ == "__main__":
    selftest()
