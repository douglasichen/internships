# internships

A backend service that hunts for **SWE internships (esp. 2027)** across multiple
sources and reports whatever's new since the last run.

## Running it

```
python3 -m internships             # fetch all sources, write out/<timestamp>.csv
python3 -m internships --selftest  # run every module's inline self-check
```

Each run fetches every source, filters to SWE internship/co-op titles that are
2027 or "maybe 2027" (drops titles that explicitly mention a different year),
drops anything already seen on a prior run (tracked per-source in
`data/seen/*.json`), and writes the rest to `out/<timestamp>.csv` and appends
it to `out/all.json` (the full history, used by the web UI below).

## Web UI

A static page (`internships/web/index.html`) lists every listing ever found,
newest scrape first, with search + source + "2027 only" filters. It fetches
`out/all.json`, so it needs a plain static file server run from the repo root:

```
python3 -m http.server 8765
# then open http://localhost:8765/internships/web/
```

## Sources (`internships/sources/`)
- `ats_boards.py` — Ashby/Lever/Greenhouse/Workday JSON APIs listed in `companies.csv`
- `github_readme.py` — [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) README table
- `speedyapply.py` — [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) README table
- `sndsh404.py` — [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) README table

Adding a source = write a class with `.name` and `.fetch() -> list[Listing]`,
register it in `internships/__main__.py`. The three README-scraping sources
share table-parsing helpers in `internships/sources/md_table.py`.

No scheduling built in yet — run it manually (or cron it) whenever you want
fresh results.

## Legacy: boards.csv
An earlier, separate artifact — a list of Ashby/Lever job boards discovered by
probing their APIs directly. Not read by the `internships` service above.
- [`boards.csv`](boards.csv) — every valid board found, with live counts.

## CSV columns
| column | meaning |
|---|---|
| `platform` | `ashby` or `lever` |
| `company` | board slug |
| `board_url` | human job board (click to browse) |
| `api_url` | JSON posting API (used for sweeping) |
| `total_jobs` | total open roles at sweep time |
| `intern_roles` | roles with "intern"/"co-op" in title |
| `swe_internships_open` | intern roles that look software-engineering |
| `swe_intern_2027` | SWE intern roles mentioning **2027** |
| `sample_swe_intern` | up to 3 example SWE-intern titles |

## How to re-sweep
Ashby: `curl -s https://api.ashbyhq.com/posting-api/job-board/<slug>`
Lever: `curl -s "https://api.lever.co/v0/postings/<slug>?mode=json"`

## Notable as of last sweep (June 2026)
- **Skydio** — `Software Engineer Intern Fall 2026 / Winter 2027` (real 2027 start).
- Most other boards are Fall-2026 cohorts; 2027 reqs not posted yet.

_Counts are a point-in-time snapshot; re-run the sweep to refresh._
