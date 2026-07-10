"""Shared parsing for the "awesome-list"-style READMEs (vanshb03, speedyapply,
and friends all use the same generated-table format): a pipe-delimited
markdown table between `<!-- TABLE_..._START -->` / `_END` marker comments,
cells containing raw HTML (`<a href>`, `<details>`, `</br>`, emoji flags).

One source repo can have several such tables (e.g. FAANG/Quant/Other
sections) -- extract_rows yields every row from every marked section.
"""
import html
import re

# Marker text varies per repo (e.g. "TABLE_START" vs "TABLE_FAANG_START",
# sometimes prefixed with unrelated comment prose) -- match the token itself
# rather than the surrounding comment, and pair START/END by document order.
MARK_RE = re.compile(r"TABLE_\w*(?:START|END)\b")
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"</?br\s*/?>", re.I)
HREF_RE = re.compile(r'href="([^"]+)"')
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _rows_in(section: str):
    lines = [l for l in section.splitlines() if l.strip().startswith("|")]
    for line in lines[2:]:  # skip header row + '---' separator row
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if any(cells):
            yield cells


def extract_rows(markdown: str):
    """Yield each data row (list of raw cell strings, tags/entities intact)
    from every TABLE_..._START/_END marked section in the document."""
    start = None
    for m in MARK_RE.finditer(markdown):
        if m.group().endswith("START"):
            start = m.end()
        elif m.group().endswith("END") and start is not None:
            yield from _rows_in(markdown[start:m.start()])
            start = None


def clean_text(cell: str) -> str:
    """Strip a cell down to plain text: drop <details>/<summary> wrappers,
    turn <br>/</br> into '; ', strip remaining tags, unescape entities.

    Unescape runs first -- some sources (e.g. Greenhouse's "content" field)
    hand back HTML whose tags are themselves entity-escaped (&lt;div&gt;);
    unescaping after stripping would turn those into literal tags too late
    and leave them in the output."""
    cell = html.unescape(cell)
    cell = re.sub(r"<details>.*?</summary>", "", cell, flags=re.S)
    cell = cell.replace("</details>", "")
    cell = BR_RE.sub("; ", cell)
    cell = TAG_RE.sub("", cell)
    cell = re.sub(r"\s*;\s*", "; ", cell).strip("; ")
    return re.sub(r"\s+", " ", cell).strip()


def extract_href(cell: str) -> str:
    m = HREF_RE.search(cell)
    return m.group(1) if m else ""


def extract_md_link(cell: str) -> str:
    m = MD_LINK_RE.search(cell)
    return m.group(1) if m else ""


def is_closed(cell: str) -> bool:
    return "🔒" in cell


def _blocks(markdown: str):
    """Yield every contiguous run of '|'-prefixed lines, unmarked or not --
    for READMEs with no TABLE_START/END comments at all."""
    block = []
    for line in markdown.splitlines():
        if line.strip().startswith("|"):
            block.append(line)
        elif block:
            yield block
            block = []
    if block:
        yield block


def extract_table_by_header(markdown: str, required_headers):
    """For READMEs with no marker comments and multiple unrelated tables:
    find the one contiguous pipe-table whose header row contains every name
    in required_headers (case-insensitive), and yield its data rows."""
    required = {h.lower() for h in required_headers}
    for block in _blocks(markdown):
        if len(block) < 2:
            continue
        header = {c.strip().lower() for c in block[0].strip().strip("|").split("|")}
        if required <= header:
            yield from _rows_in("\n".join(block))
            return


def selftest():
    doc = """
intro text
<!-- TABLE_START -->
| Company | Role |
| --- | --- |
| Acme | SWE Intern |
| ↳ | Backend Intern |
<!-- TABLE_END -->
outro
<!-- TABLE_QUANT_START -->
| Company | Role |
| --- | --- |
| Quant Co | Quant Dev Intern |
<!-- TABLE_QUANT_END -->
"""
    rows = list(extract_rows(doc))
    assert rows == [["Acme", "SWE Intern"], ["↳", "Backend Intern"],
                     ["Quant Co", "Quant Dev Intern"]], rows
    assert clean_text("<details><summary>2 locations</summary>SF</br>NYC</details>") == "SF; NYC"
    assert clean_text('<a href="https://x"><strong>Acme</strong></a>') == "Acme"
    assert clean_text("San Jose, CA &amp; Remote") == "San Jose, CA & Remote"
    # Greenhouse's "content" field: tags are themselves entity-escaped
    assert clean_text("&lt;div&gt;&lt;p&gt;About Us&lt;/p&gt;&lt;/div&gt;") == "About Us"
    assert extract_href('<a href="https://apply/1">Apply</a>') == "https://apply/1"
    assert extract_md_link("[apply](https://apply/3)") == "https://apply/3"
    assert is_closed("🔒")
    assert not is_closed('<a href="https://apply/1">Apply</a>')

    unmarked_doc = """
## programs open now

| org | opportunity |
| --- | --- |
| Foo | [bar](https://x) |

## the list

| Company | Role | Apply |
| --- | --- | --- |
| Acme | SWE Intern | [apply](https://apply/4) |
| Beta | PM Intern | 🔒 |
"""
    rows = list(extract_table_by_header(unmarked_doc, ["Company", "Role", "Apply"]))
    assert rows == [["Acme", "SWE Intern", "[apply](https://apply/4)"],
                     ["Beta", "PM Intern", "🔒"]], rows
    print("md_table selftest OK")


if __name__ == "__main__":
    selftest()
