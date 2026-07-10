# internships

A backend service that hunts for **SWE internships (esp. 2027)** across multiple
sources and reports whatever's new since the last run.

## Running it

```
python3 -m internships             # fetch all sources, write out/<timestamp>.csv
python3 -m internships --selftest  # run every module's inline self-check
python3 -m internships --recompute is_2027 descriptions dedup priority  # patch out/all.json, no scrape
```

Each run takes a `.run.lock` flock so two scrapes can't race each other's
writes (a second concurrent run just prints an error and exits). It fetches
every source, filters to SWE internship/co-op titles that are 2027 or "maybe
2027" (drops titles that explicitly mention a different year), drops anything
already seen on a prior run (tracked per-source in `data/seen/*.json`), and
writes the rest to `out/<timestamp>.csv` and appends it to `out/all.json`
(the full history, used by the web UI below) — every row carries a
`description` (the posting's own body, or a raw-page-fetch fallback for
sources that don't have one) and a `priority` tier (1 = big tech/absolute
top tier, 2 = solid mid tech, 3 = everything else/default, from a
hand-curated classification of company names — see
`filters.company_priority`).

`--recompute FIELD [FIELD ...]` (`internships/recompute.py`) patches
`out/all.json` in place instead of scraping — handy after changing filter
logic or for backfilling rows scraped before a field existed:
- `is_2027` — recompute the flag against the current `filters.year_relevance`
- `descriptions` — re-fetch a description for any row missing one
- `dedup` — merge rows that are the same job link under today's rules,
  always keeping the oldest record
- `priority` — recompute the 1/2/3 tier against the current
  `filters.company_priority` classification

## Web UI

A static page (`internships/web/index.html`) lists every listing ever found,
newest scrape first, with search + source + "2027 only"/"P1+P2 only"/"P1
only"/"Hide applied"/"Applied only" filters. Priority-1 and priority-2
listings get a "P1"/"P2" badge next to the title (priority 3 is the silent
default, no badge). A checkbox on each row marks it applied — that state
lives only in the browser's `localStorage`, not the backend, so it survives
`out/all.json` being regenerated. A "Recompute" control in the filter panel
lets you trigger `--recompute dedup`/`is_2027`/`priority`/`descriptions`
from the page itself instead of the terminal. A "Run scrape" button in the
header does the same for a full scrape, with a live "scrape running…"
indicator that polls regardless of whether the scrape was started from this
page, another tab, or a bare terminal `python3 -m internships`/`--recompute`
run (they all take the same `.run.lock`).

It fetches `out/all.json` and POSTs to `/api/recompute`/`/api/scrape`, so it
needs `internships/webserver.py` (not plain `python3 -m http.server`) run
from the repo root:

```
python3 -m internships.webserver 8765
# then open http://localhost:8765/internships/web/
```

## Sources (`internships/sources/`)
- `ats_boards.py` — Ashby/Lever/Greenhouse/Workday JSON APIs listed in
  `companies.csv`. Workday boards are keyword-searched ("intern"/"co-op",
  merged + deduped) and paginated instead of grabbing the first unfiltered
  page, and get a per-job description backfilled from Workday's detail
  endpoint for postings that already look like an SWE internship by title.
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
