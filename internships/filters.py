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


def year_relevance(*texts: str) -> str:
    """'yes' if 2027 is mentioned, 'maybe' if no year is mentioned at all,
    'no' if some other year is mentioned and 2027 isn't -- i.e. for-sure
    not a 2027 posting (e.g. an explicit Summer 2026 or 2025 role)."""
    years = {y for t in texts if t for y in YEAR_RE.findall(t)}
    if "2027" in years:
        return "yes"
    return "no" if years else "maybe"


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
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
