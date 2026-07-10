"""Shared relevance filters, applied the same way regardless of source."""
import csv
import re
from pathlib import Path

INTERN_RE = re.compile(r"\b(intern(ship)?|co[\- ]?op)\b", re.I)
SWE_RE = re.compile(
    r"\b(software|swe|develop(er|ment)|programmer|full[\- ]?stack|back[\- ]?end|"
    r"front[\- ]?end|infrastructure|platform|systems?|embedded|"
    r"machine learning|\bml\b|\bai\b|data engineer|security engineer)\b", re.I)
YEAR_RE = re.compile(r"\b20\d{2}\b")

# Not imported from ats_boards.py's own CSV_PATH -- that would be a circular
# import (ats_boards.py imports this module). companies.csv lives at the
# same repo-root-relative spot either way.
_COMPANIES_CSV = Path(__file__).resolve().parent.parent / "companies.csv"
_important_companies_cache = None


def is_swe_internship(title: str) -> bool:
    return bool(title) and bool(INTERN_RE.search(title)) and bool(SWE_RE.search(title))


def _important_companies():
    global _important_companies_cache
    if _important_companies_cache is None:
        try:
            with open(_COMPANIES_CSV, newline="") as f:
                _important_companies_cache = frozenset(
                    row["company"].strip().lower() for row in csv.DictReader(f) if row.get("company"))
        except (FileNotFoundError, KeyError):
            _important_companies_cache = frozenset()
    return _important_companies_cache


def is_important_company(company: str) -> bool:
    """"Important" = mid/big tech or otherwise prestigious. Approximated as
    "is this company on our own companies.csv sweep list" rather than a
    second hand-curated list to keep in sync -- companies.csv already is
    that curation (Google, Anthropic, Jane Street, Figma, ...), built for
    exactly this purpose.

    ponytail: exact case-insensitive name match, so naming variants across
    sources (e.g. "Amazon Web Services" vs "AWS", "Google DeepMind" vs
    "DeepMind") won't match. Upgrade to alias/fuzzy matching if that turns
    out to miss too much in practice."""
    return bool(company) and company.strip().lower() in _important_companies()


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

    # is_important_company against a fake companies.csv (real one shouldn't
    # drive test assertions -- it can gain/lose rows over time)
    import tempfile
    global _important_companies_cache, _COMPANIES_CSV
    orig_csv, orig_cache = _COMPANIES_CSV, _important_companies_cache
    try:
        fake = Path(tempfile.mkdtemp()) / "companies.csv"
        fake.write_text("company,board_url\nGoogle,https://x\nJane Street,https://y\n")
        _COMPANIES_CSV = fake
        _important_companies_cache = None
        assert is_important_company("Google")
        assert is_important_company("  google  ")  # trimmed + case-insensitive
        assert is_important_company("Jane Street")
        assert not is_important_company("Totally Unknown LLC")
        assert not is_important_company("")
    finally:
        _COMPANIES_CSV, _important_companies_cache = orig_csv, orig_cache
    print("filters selftest OK")


if __name__ == "__main__":
    selftest()
