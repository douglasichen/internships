#!/usr/bin/env python3
"""Run every source once and print/write whatever's new since the last run.

Usage:
    python3 -m internships              # run all sources, write out/<ts>.csv + out/all.json
    python3 -m internships --selftest   # run every module's inline self-check
    python3 -m internships --recompute is_2027 descriptions dedup  # fix stale out/all.json fields

Serving the web UI (internships/web/index.html) needs internships/webserver.py
(not plain http.server -- it also backs the UI's "Recompute" button) -- from
the repo root: `python3 -m internships.webserver 8765`, then open
http://localhost:8765/internships/web/
"""
import argparse
import csv
import fcntl
import json
import sys
from datetime import datetime
from pathlib import Path

from internships import recompute
from internships.service import ROOT, run
from internships.sources.ats_boards import AtsBoardsSource
from internships.sources.github_readme import GithubReadmeSource
from internships.sources.sndsh404 import Sndsh404Source
from internships.sources.speedyapply import SpeedyApplySource

OUT_DIR = ROOT / "out"
ALL_JSON_PATH = OUT_DIR / "all.json"
LOCK_PATH = ROOT / ".run.lock"

SOURCES = [AtsBoardsSource(), GithubReadmeSource(), SpeedyApplySource(), Sndsh404Source()]

FIELDS = ["company", "title", "location", "is_2027", "source", "posted", "scraped_at", "url",
          "description"]


def _row(l, scraped_at):
    return {"id": l.id(), "company": l.company, "title": l.title, "location": l.location,
            "is_2027": l.is_2027, "source": l.source, "posted": l.posted,
            "scraped_at": scraped_at, "url": l.url, "description": l.extra_text}


def write_csv(listings, scraped_at, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        # extrasaction="ignore": _row() includes "id" for the JSON dataset,
        # but the CSV schema (FIELDS) deliberately omits it.
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
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
    import tempfile
    from internships.models import Listing

    modules = ["internships.models", "internships.filters", "internships.seen_store",
               "internships.sources.md_table", "internships.sources.ats_boards",
               "internships.sources.github_readme", "internships.sources.speedyapply",
               "internships.sources.sndsh404", "internships.service", "internships.recompute"]
    for m in modules:
        subprocess.run([sys.executable, "-m", m], cwd=ROOT, check=True)
    # webserver.py's bare `-m` invocation starts the (blocking) real server,
    # unlike every other module here -- its selftest needs the explicit flag.
    subprocess.run([sys.executable, "-m", "internships.webserver", "--selftest"],
                    cwd=ROOT, check=True)

    # write_csv's _row() includes "id" (needed for all.json) but FIELDS doesn't --
    # make sure that mismatch doesn't blow up csv.DictWriter.
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.csv"
        write_csv([Listing("ats_boards", "Acme", "SWE Intern", "SF", "http://x/1")],
                   "2026-07-09T00:00:00", out)
        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1 and rows[0]["company"] == "Acme" and "id" not in rows[0]

    # a second flock on the same lock file must fail while the first is held
    with tempfile.TemporaryDirectory() as td:
        lock_path = Path(td) / ".run.lock"
        f1 = open(lock_path, "w")
        fcntl.flock(f1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f2 = open(lock_path, "w")
        try:
            fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise AssertionError("second flock should have failed")
        except BlockingIOError:
            pass
        f1.close()  # releases the lock
        fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB)  # now succeeds
        f2.close()
    print("__main__ selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run every module's self-check")
    ap.add_argument("--recompute", nargs="+", choices=["is_2027", "descriptions", "dedup"],
                     metavar="FIELD",
                     help="recompute stale out/all.json field(s) in place instead of scraping: "
                          "is_2027 (against current filters), descriptions (re-fetch any row "
                          "missing one), and/or dedup (merge rows that are the same job link "
                          "under today's rules, always keeping the oldest record)")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return

    # ponytail: flock held for the process lifetime (released on exit), not
    # released explicitly -- fine for a one-shot CLI run, not for a long-lived
    # server holding this same lock. Held for --recompute too since both
    # recompute modes rewrite out/all.json, same file a real scrape appends to.
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another scrape is already running, exiting", file=sys.stderr)
        sys.exit(1)

    if a.recompute:
        # dedup first when combined with descriptions: no point fetching a
        # description for a row that's about to be dropped as a duplicate
        if "dedup" in a.recompute:
            removed, total = recompute.dedupe()
            print(f"deduped {removed}/{total} rows (oldest record kept) -> {recompute.ALL_JSON_PATH}")
        if "is_2027" in a.recompute:
            changed, total = recompute.recompute()
            print(f"recomputed is_2027 for {total} listings, {changed} changed -> {recompute.ALL_JSON_PATH}")
        if "descriptions" in a.recompute:
            changed, stale = recompute.backfill_descriptions()
            print(f"backfilled {changed}/{stale} stale descriptions -> {recompute.ALL_JSON_PATH}")
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
