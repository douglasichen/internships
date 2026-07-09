#!/usr/bin/env python3
"""Run every source once and print/write whatever's new since the last run.

Usage:
    python3 -m internships              # run all sources, write out/<ts>.csv + out/all.json
    python3 -m internships --selftest   # run every module's inline self-check

Serving the web UI (internships/web/index.html) needs a static file server for
CORS reasons -- from the repo root: `python3 -m http.server 8765`, then open
http://localhost:8765/internships/web/
"""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from internships.service import ROOT, run
from internships.sources.ats_boards import AtsBoardsSource
from internships.sources.github_readme import GithubReadmeSource
from internships.sources.sndsh404 import Sndsh404Source
from internships.sources.speedyapply import SpeedyApplySource

OUT_DIR = ROOT / "out"
ALL_JSON_PATH = OUT_DIR / "all.json"

SOURCES = [AtsBoardsSource(), GithubReadmeSource(), SpeedyApplySource(), Sndsh404Source()]

FIELDS = ["company", "title", "location", "is_2027", "source", "posted", "scraped_at", "url"]


def _row(l, scraped_at):
    return {"id": l.id(), "company": l.company, "title": l.title, "location": l.location,
            "is_2027": l.is_2027, "source": l.source, "posted": l.posted,
            "scraped_at": scraped_at, "url": l.url}


def write_csv(listings, scraped_at, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for l in listings:
            row = _row(l, scraped_at)
            row["is_2027"] = "yes" if row["is_2027"] else ""
            w.writerow(row)


def append_all_json(listings, scraped_at, path=ALL_JSON_PATH):
    """The web UI's whole dataset: every listing ever found, across every run,
    each stamped with when it was scraped. Grows by appending -- seen_store
    already guarantees a given listing only reaches here once."""
    existing = json.loads(path.read_text()) if path.exists() else []
    existing.extend(_row(l, scraped_at) for l in listings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2))


def selftest():
    import subprocess
    modules = ["internships.models", "internships.filters", "internships.seen_store",
               "internships.sources.md_table", "internships.sources.ats_boards",
               "internships.sources.github_readme", "internships.sources.speedyapply",
               "internships.sources.sndsh404", "internships.service"]
    for m in modules:
        subprocess.run([sys.executable, "-m", m], cwd=ROOT, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run every module's self-check")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return

    result = run(SOURCES)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    for name, stats in result.per_source.items():
        if stats.error:
            print(f"  {name}: ERROR {stats.error}", file=sys.stderr)
        else:
            print(f"  {name}: fetched={stats.fetched} swe={stats.swe} new={stats.new}")

    if not result.new_listings:
        print("no new SWE internships this run")
        return

    scraped_at = datetime.now().isoformat(timespec="seconds")
    out_path = OUT_DIR / f"{ts}.csv"
    write_csv(result.new_listings, scraped_at, out_path)
    append_all_json(result.new_listings, scraped_at)
    n27 = sum(1 for l in result.new_listings if l.is_2027)
    print(f"{len(result.new_listings)} new SWE internships ({n27} mention 2027) -> {out_path}")
    print(f"web dataset updated -> {ALL_JSON_PATH}")


if __name__ == "__main__":
    main()
