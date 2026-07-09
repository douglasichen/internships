"""Shared relevance filters, applied the same way regardless of source."""
import re

INTERN_RE = re.compile(r"\b(intern(ship)?|co[\- ]?op)\b", re.I)
SWE_RE = re.compile(
    r"\b(software|swe|develop(er|ment)|programmer|full[\- ]?stack|back[\- ]?end|"
    r"front[\- ]?end|infrastructure|platform|systems?|embedded|"
    r"machine learning|\bml\b|\bai\b|data engineer|security engineer)\b", re.I)
YEAR_RE = re.compile(r"\b20\d{2}\b")


def is_swe_internship(title: str) -> bool:
    return bool(title) and bool(INTERN_RE.search(title)) and bool(SWE_RE.search(title))


def year_relevance(title: str, location: str = "", extra_text: str = "") -> str:
    """'yes' if title/location say 2027, or say no year at all and the
    description body mentions 2027. 'no' if title/location explicitly name
    a different year -- that's authoritative and extra_text can't override
    it. 'maybe' if title/location name no year and the body doesn't
    mention 2027 either.

    extra_text (the job description body) is untrustworthy either way: full
    of years unrelated to the posting's own year (copyright footers,
    academic-year ranges, other programs' dates). It can only settle things
    when title/location are silent -- it must never veto an explicit
    title/location year, and must never override one either."""
    core_years = {y for t in (title, location) if t for y in YEAR_RE.findall(t)}
    if "2027" in core_years:
        return "yes"
    if core_years:
        return "no"
    return "yes" if extra_text and "2027" in YEAR_RE.findall(extra_text) else "maybe"


def selftest():
    assert is_swe_internship("Software Engineer Intern, Summer 2027")
    assert is_swe_internship("Backend Engineering Co-op")
    assert not is_swe_internship("Marketing Intern")
    assert not is_swe_internship("Staff Software Engineer")  # not an internship
    assert not is_swe_internship("")

    assert year_relevance("SWE Intern Summer 2027") == "yes"
    assert year_relevance("SWE Intern", "", "mentions 2027 in body") == "yes"
    assert year_relevance("SWE Intern Summer 2026") == "no"
    assert year_relevance("SWE Intern") == "maybe"
    assert year_relevance("SWE Intern 2026 and 2027 rotation") == "yes"  # 2027 wins if present
    # a year in the description body alone must never disqualify -- only
    # title/location can produce "no" (copyright footers, academic-year
    # ranges, and other programs' dates live in the body, not the title)
    assert year_relevance("SWE Intern", "SF", "copyright 2019 Acme Corp") == "maybe"
    # ...and a stray "2027" in the noisy body must never override an
    # explicit non-2027 year in the title either -- title/location are
    # authoritative in both directions
    assert year_relevance("SWE Intern Summer 2026", "SF", "mentions 2027 somewhere") == "no"
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
