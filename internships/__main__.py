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
from internships.sources.custom_boards import CustomBoardsSource
from internships.sources.github_readme import GithubReadmeSource
from internships.sources.sndsh404 import Sndsh404Source
from internships.sources.speedyapply import SpeedyApplySource

OUT_DIR = ROOT / "out"
ALL_JSON_PATH = OUT_DIR / "all.json"
LOCK_PATH = ROOT / ".run.lock"

SOURCES = [AtsBoardsSource(), CustomBoardsSource(), GithubReadmeSource(), SpeedyApplySource(), Sndsh404Source()]

FIELDS = ["company", "title", "location", "is_2027", "priority", "source", "posted",
          "scraped_at", "url", "description"]


def _row(l, scraped_at, *, include_description=False):
    """Listing as a plain dict. all.json omits description (see desc_store);
    per-run CSVs still include it when include_description=True."""
    row = {"id": l.id(), "company": l.company, "title": l.title, "location": l.location,
           "is_2027": l.is_2027, "priority": l.priority, "source": l.source,
           "posted": l.posted, "scraped_at": scraped_at, "url": l.url}
    if include_description:
        row["description"] = l.extra_text
    return row


def write_csv(listings, scraped_at, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        # extrasaction="ignore": _row() includes "id" for the JSON dataset,
        # but the CSV schema (FIELDS) deliberately omits it.
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for l in listings:
            row = _row(l, scraped_at, include_description=True)
            row["is_2027"] = "yes" if row["is_2027"] else ""
            # priority is an int (1/2/3), not a boolean flag -- write it
            # straight through, csv.DictWriter handles ints fine.
            w.writerow(row)


def append_all_json(listings, scraped_at, path=ALL_JSON_PATH):
    """The web UI's whole dataset: every listing ever found, across every run,
    each stamped with when it was scraped. Grows by appending.

    Descriptions are NOT stored here -- they go to out/descriptions.json keyed
    by the same listing id (see desc_store). Skips a row when:
    - its listing id is already present (retry after crash before persist_seen), or
    - the same company+title+location already exists (cross-source / re-scrape
      of the same human-visible job with a different URL).
    """
    from internships import desc_store

    existing = json.loads(path.read_text()) if path.exists() else []
    # one-shot peel if this all.json still has inline description fields
    descs = desc_store.load(path)
    peeled = desc_store.peel_from_rows(existing)
    if peeled:
        for k, v in peeled.items():
            descs.setdefault(k, v)

    have = {r.get("id") for r in existing}
    have_content = {recompute.content_key(r) for r in existing}
    for l in listings:
        row = _row(l, scraped_at)  # no description field
        lid = row["id"]
        ck = recompute.content_key(row)
        if lid in have:
            # retry after crash: row may already be in all.json but desc never
            # saved -- still fill the store when we have body text
            if l.extra_text and not descs.get(lid):
                descs[lid] = l.extra_text
            continue
        if ck in have_content:
            # same company+title+location already recorded under another id
            continue
        existing.append(row)
        have.add(lid)
        have_content.add(ck)
        if l.extra_text:
            descs[lid] = l.extra_text
    path.parent.mkdir(parents=True, exist_ok=True)
    # descriptions.json FIRST, then all.json: a crash between the two leaves
    # bodies intact (and peelable if all.json still had inline fields). Orphan
    # desc keys for ids not yet in all.json are harmless.
    desc_store.save(descs, path)
    # Atomic (tmp + replace): full rewrite of the cumulative dataset.
    recompute._atomic_write(existing, path)


def selftest():
    import subprocess
    import tempfile
    from internships.models import Listing

    modules = ["internships.models", "internships.filters", "internships.seen_store",
               "internships.desc_store",
               "internships.sources.md_table", "internships.sources.ats_boards",
               "internships.sources.custom_boards",
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

    # append_all_json rewrites the ENTIRE cumulative all.json each run; a crash
    # partway through the write must not corrupt the existing dataset. Simulate
    # a write that truncates then fails, and assert the prior file survives
    # (atomic tmp+replace guarantees this; a direct write_text would not).
    with tempfile.TemporaryDirectory() as td:
        all_json = Path(td) / "all.json"
        all_json.write_text(json.dumps([_row(Listing("s", "Old", "SWE", "SF", "http://x/0"),
                                              "2026-01-01T00:00:00")]))
        orig_write_text = Path.write_text

        def _partial_write(self, data, *args, **kwargs):
            orig_write_text(self, data[:len(data) // 2], *args, **kwargs)  # truncated
            raise OSError("simulated crash mid-write")

        Path.write_text = _partial_write
        try:
            append_all_json([Listing("s", "New", "SWE", "NY", "http://x/1")],
                            "2026-07-09T00:00:00", all_json)
        except OSError:
            pass
        finally:
            Path.write_text = orig_write_text
        survivors = json.loads(all_json.read_text())  # must still be valid JSON
        assert [r["company"] for r in survivors] == ["Old"]  # prior data intact

    # append_all_json must not double-insert a listing whose id is already
    # present (retry after a crash between append and persist_seen).
    with tempfile.TemporaryDirectory() as td:
        all_json = Path(td) / "all.json"
        listing = Listing("s", "Acme", "SWE", "SF", "http://x/dup")
        append_all_json([listing], "2026-07-09T00:00:00", all_json)
        append_all_json([listing], "2026-07-09T01:00:00", all_json)  # same id
        rows = json.loads(all_json.read_text())
        assert len(rows) == 1 and rows[0]["company"] == "Acme"
        assert rows[0]["scraped_at"] == "2026-07-09T00:00:00"  # first write kept
        assert "description" not in rows[0]

    # descriptions land in descriptions.json keyed by the same listing id
    with tempfile.TemporaryDirectory() as td:
        from internships import desc_store
        all_json = Path(td) / "all.json"
        listing = Listing("s", "Acme", "SWE", "SF", "http://x/1",
                          extra_text="<p>full body</p>")
        append_all_json([listing], "2026-07-09T00:00:00", all_json)
        rows = json.loads(all_json.read_text())
        assert "description" not in rows[0]
        descs = desc_store.load(all_json)
        assert descs[listing.id()] == "<p>full body</p>"

    # same company+title+location, different URL/source -- do not double-insert
    with tempfile.TemporaryDirectory() as td:
        all_json = Path(td) / "all.json"
        a = Listing("github_readme", "Point72", "Quantitative Developer Intern",
                    "New York, NY", "http://readme/p72")
        b = Listing("ats_boards", "Point72", "Quantitative Developer Intern",
                    "New York, NY", "http://boards/p72-other")
        append_all_json([a], "2026-07-09T00:00:00", all_json)
        append_all_json([b], "2026-07-10T00:00:00", all_json)
        rows = json.loads(all_json.read_text())
        assert len(rows) == 1 and rows[0]["id"] == a.id()

    # retry: id already in all.json, desc missing -- second append fills store
    with tempfile.TemporaryDirectory() as td:
        from internships import desc_store
        all_json = Path(td) / "all.json"
        listing = Listing("s", "Acme", "SWE", "SF", "http://x/retry",
                          extra_text="body on retry")
        # simulate crash: row written, desc never saved
        all_json.write_text(json.dumps([_row(listing, "2026-07-09T00:00:00")]))
        assert listing.id() not in desc_store.load(all_json)
        append_all_json([listing], "2026-07-09T01:00:00", all_json)
        assert len(json.loads(all_json.read_text())) == 1  # no double insert
        assert desc_store.load(all_json)[listing.id()] == "body on retry"

    # peel legacy inline descriptions: store written, all.json slimmed
    with tempfile.TemporaryDirectory() as td:
        from internships import desc_store
        all_json = Path(td) / "all.json"
        all_json.write_text(json.dumps([
            {"id": "leg1", "company": "X", "description": "legacy body"},
        ]))
        append_all_json([], "2026-07-09T00:00:00", all_json)
        assert "description" not in json.loads(all_json.read_text())[0]
        assert desc_store.load(all_json)["leg1"] == "legacy body"
    print("__main__ selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run every module's self-check")
    ap.add_argument("--recompute", nargs="+",
                     choices=["is_2027", "descriptions", "dedup", "priority"], metavar="FIELD",
                     help="recompute stale out/all.json field(s) in place instead of scraping: "
                          "is_2027 (against current filters), descriptions (re-fetch any row "
                          "missing one), dedup (merge rows that are the same job link under "
                          "today's rules, always keeping the oldest record), and/or priority "
                          "(1/2/3 tier against current company classification)")
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
        if "priority" in a.recompute:
            changed, total = recompute.recompute_priority()
            print(f"recomputed priority for {total} listings, {changed} changed -> {recompute.ALL_JSON_PATH}")
        if "descriptions" in a.recompute:
            changed, stale = recompute.backfill_descriptions()
            print(f"backfilled {changed}/{stale} stale descriptions -> "
                  f"{recompute.DESCRIPTIONS_PATH}")
        return

    # Defer seen-id writes until after a successful out/all.json append so a
    # crash/exception between fetch and the dataset write cannot permanently
    # drop new listings (they'd be marked seen without ever being recorded).
    result = run(SOURCES, persist_seen=False)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    for name, stats in result.per_source.items():
        if stats.error:
            print(f"  {name}: ERROR {stats.error}", file=sys.stderr)
        else:
            print(f"  {name}: fetched={stats.fetched} swe={stats.swe} new={stats.new}")

    if not result.new_listings:
        result.persist_seen()
        print("no new SWE internships this run")
        return

    scraped_at = datetime.now().isoformat(timespec="seconds")
    out_path = OUT_DIR / f"{ts}.csv"
    write_csv(result.new_listings, scraped_at, out_path)
    append_all_json(result.new_listings, scraped_at)
    result.persist_seen()
    n27 = sum(1 for l in result.new_listings if l.is_2027)
    print(f"{len(result.new_listings)} new SWE internships ({n27} mention 2027) -> {out_path}")
    print(f"web dataset updated -> {ALL_JSON_PATH}")


if __name__ == "__main__":
    main()
