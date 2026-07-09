"""Source: sndsh404/summer-2027-internships README table. No TABLE_START/END
marker comments (unlike vanshb03/speedyapply) and several unrelated tables
in the same file ("programs open now" etc), so this locates the internship
table by its header row instead. Columns: Company | Role | Location | Apply
| Added. Links are plain markdown `[apply](url)`, not HTML <a href>. No
company-continuation marker -- each row repeats the full company name.
"""
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.md_table import clean_text, extract_md_link, extract_table_by_header, is_closed

README_URL = "https://raw.githubusercontent.com/sndsh404/summer-2027-internships/main/README.md"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) internships-service"
HEADERS = ("Company", "Role", "Location", "Apply")


def parse(markdown: str):
    listings = []
    for cells in extract_table_by_header(markdown, HEADERS):
        if len(cells) < 5:
            continue
        company, role, location, apply_cell, added = cells[:5]
        company = clean_text(company)
        if not company or is_closed(apply_cell):
            continue
        listings.append(Listing(
            source="sndsh404", company=company, title=clean_text(role),
            location=clean_text(location), url=extract_md_link(apply_cell),
            posted=clean_text(added)))
    return listings


class Sndsh404Source:
    name = "sndsh404"

    def __init__(self, url=README_URL):
        self.url = url

    def fetch(self) -> list:
        req = Request(self.url, headers={"User-Agent": UA})
        with urlopen(req, timeout=25) as r:
            markdown = r.read().decode("utf-8")
        return parse(markdown)


def selftest():
    doc = """
## programs open now

| org | opportunity |
| --- | --- |
| Foo | [bar](https://x) |

## the list

| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Acme | Software Engineer Intern | Chicago, IL | [apply](https://apply/1) | 2026-07-08 |
| Beta Corp | Marketing Intern | Remote | [apply](https://apply/2) | 2026-07-07 |
| Gamma | Backend Intern | NYC | 🔒 | 2026-07-01 |
"""
    rows = parse(doc)
    assert len(rows) == 2, rows
    assert rows[0].company == "Acme" and rows[0].url == "https://apply/1"
    assert all(r.company != "Gamma" for r in rows)  # closed, filtered
    print("sndsh404 selftest OK")


if __name__ == "__main__":
    selftest()
