"""Recompute the stored `is_2027` flag on out/all.json against the current
filters.year_relevance logic -- run after changing that logic, since is_2027
is baked into each row at scrape time and otherwise never revisited.

Usage:
    python3 -m internships --recompute
"""
import json

from internships.filters import year_relevance
from internships.service import ROOT

ALL_JSON_PATH = ROOT / "out" / "all.json"


def recompute(path=ALL_JSON_PATH):
    rows = json.loads(path.read_text())
    changed = 0
    for row in rows:
        # extra_text (description body) isn't persisted in all.json, only
        # title/location -- fine, since those alone are authoritative and
        # extra_text can only ever turn a "maybe" into a "yes".
        is_2027 = year_relevance(row["title"], row["location"]) != "no"
        if row["is_2027"] != is_2027:
            row["is_2027"] = is_2027
            changed += 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    tmp.replace(path)
    return changed, len(rows)


def selftest():
    import tempfile
    from pathlib import Path

    rows = [
        {"title": "SWE Intern", "location": "SF", "is_2027": False},  # maybe -> now True
        {"title": "SWE Intern Summer 2026", "location": "SF", "is_2027": False},  # stays False
        {"title": "SWE Intern Summer 2027", "location": "SF", "is_2027": True},  # stays True
    ]
    p = Path(tempfile.mkdtemp()) / "all.json"
    p.write_text(json.dumps(rows))
    changed, total = recompute(p)
    assert changed == 1 and total == 3
    result = json.loads(p.read_text())
    assert result[0]["is_2027"] is True
    assert result[1]["is_2027"] is False
    assert result[2]["is_2027"] is True
    print("recompute selftest OK")


if __name__ == "__main__":
    selftest()
