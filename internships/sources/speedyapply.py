"""Source: speedyapply/2027-SWE-College-Jobs README table (USA internships
section of README.md -- new-grad and international roles live in separate
files and aren't fetched here). Columns: Company | Position | Location |
[Salary] | Posting | Age -- the FAANG/Quant sections include a Salary
column but the Other section doesn't, so Posting/Age are read from the
end of the row rather than assuming a fixed column count. The README has
several TABLE_..._START/_END sections (FAANG/Quant/Other) -- extract_rows
already walks all of them. Unlike vanshb03's list, company names repeat on
every row (no '↳').
"""
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.md_table import (
    README_THROTTLE, clean_text, extract_href, extract_rows, is_closed)

README_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) internships-service"


def parse(markdown: str):
    listings = []
    for cells in extract_rows(markdown):
        # The Other section's table has no Salary column (5 cells vs 6);
        # Posting/Age are always the last two cells regardless.
        if len(cells) < 5:
            continue
        company_cell, position, location = cells[:3]
        posting, age = cells[-2], cells[-1]
        company = clean_text(company_cell)
        if not company or is_closed(posting):
            continue
        listings.append(Listing(
            source="speedyapply", company=company, title=clean_text(position),
            location=clean_text(location), url=extract_href(posting),
            posted=clean_text(age)))
    return listings


class SpeedyApplySource:
    name = "speedyapply"

    def __init__(self, url=README_URL):
        self.url = url

    def fetch(self) -> list:
        with README_THROTTLE.hold(self.url):
            req = Request(self.url, headers={"User-Agent": UA})
            with urlopen(req, timeout=25) as r:
                markdown = r.read().decode("utf-8")
        return parse(markdown)


def selftest():
    doc = """
<!-- TABLE_FAANG_START -->
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://nvidia.com"><strong>NVIDIA</strong></a> | Software Engineering Intern - Fall 2026 | Santa Clara, CA | $62/hr | <a href="https://apply/1">Apply</a> | 2d |
<!-- TABLE_FAANG_END -->
<!-- TABLE_QUANT_START -->
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://jane.com"><strong>Jane Street</strong></a> | Quant Trading Intern | NYC | $70/hr | 🔒 | 5d |
<!-- TABLE_QUANT_END -->
<!-- TABLE_OTHER_START -->
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <a href="https://acme.com"><strong>Acme</strong></a> | Software Engineering Intern | Remote | <a href="https://apply/2">Apply</a> | 1d |
<!-- TABLE_OTHER_END -->
"""
    rows = parse(doc)
    assert len(rows) == 2, rows
    assert rows[0].company == "NVIDIA" and rows[0].url == "https://apply/1"
    # 5-column row (no Salary, e.g. the "Other" section) must still parse
    assert rows[1].company == "Acme" and rows[1].url == "https://apply/2"
    assert rows[1].posted == "1d"
    print("speedyapply selftest OK")


if __name__ == "__main__":
    selftest()
