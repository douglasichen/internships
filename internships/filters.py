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
    """'yes' if 2027 is mentioned anywhere, including the description body.
    'no' if the title or location explicitly name a different year and
    nothing mentions 2027 -- i.e. for-sure not a 2027 posting (e.g. an
    explicit Summer 2026 role). 'maybe' if title/location name no year.

    extra_text (the job description body) can only confirm 2027, never
    disqualify: it's full of years unrelated to the posting's own year
    (copyright footers, academic-year ranges, other programs' dates), so
    letting it veto a listing silently drops real, currently-open roles."""
    all_years = {y for t in (title, location, extra_text) if t for y in YEAR_RE.findall(t)}
    if "2027" in all_years:
        return "yes"
    core_years = {y for t in (title, location) if t for y in YEAR_RE.findall(t)}
    return "no" if core_years else "maybe"


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
    assert year_relevance("SWE Intern Summer 2026", "SF", "mentions 2027 somewhere") == "yes"
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
