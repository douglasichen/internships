"""Source: vanshb03/Summer2027-Internships README table. No JSON feed is
published (checked: only README.md + OFFSEASON_README.md), so this parses
the generated markdown table directly. Columns: Company | Role | Location |
Application/Link | Date Posted. A company with multiple open roles repeats
as '↳' in the Company column instead of the name -- carry the last real name
forward. Closed applications render the link cell as a plain 🔒 -- skipped.
"""
from urllib.request import Request, urlopen

from internships.models import Listing
from internships.sources.md_table import (
    README_THROTTLE, clean_text, extract_href, extract_rows, is_closed)

README_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/README.md"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) internships-service"


def parse(markdown: str):
    listings = []
    last_company = ""
    for cells in extract_rows(markdown):
        if len(cells) < 5:
            continue
        company_cell, role, location, application, posted = cells[:5]
        company = clean_text(company_cell)
        if company in ("", "↳"):
            company = last_company
        else:
            last_company = company
        if not company or is_closed(application):
            continue
        listings.append(Listing(
            source="github_readme", company=company, title=clean_text(role),
            location=clean_text(location), url=extract_href(application),
            posted=clean_text(posted)))
    return listings


class GithubReadmeSource:
    name = "github_readme"

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
<!-- TABLE_START -->
| Company | Role | Location | Application/Link | Date Posted |
| --- | --- | --- | --- | --- |
| Acme | Software Engineer Intern | <details><summary>2 locations</summary>SF</br>NYC</details> | <a href="https://apply/1">Apply</a> | Jul 07 |
| ↳ | Firmware Intern | SF | <a href="https://apply/2">Apply</a> | Jul 07 |
| Beta Corp | Marketing Intern | Remote | 🔒 | Jul 06 |
<!-- TABLE_END -->
"""
    rows = parse(doc)
    assert len(rows) == 2, rows
    assert rows[0].company == "Acme" and rows[0].location == "SF; NYC"
    assert rows[1].company == "Acme" and rows[1].title == "Firmware Intern"
    assert all(r.company != "Beta Corp" for r in rows)  # closed, filtered
    print("github_readme selftest OK")


if __name__ == "__main__":
    selftest()
