# internships

A tracked list of company job boards (Ashby + Lever) discovered by sweeping their
public posting APIs, hunting for **SWE internships (esp. 2027)**.

Even boards with **no internships posted yet** are kept here — many open their
Summer/Fall 2027 internship reqs in **Aug–Oct 2026**, so this is a watchlist to
re-check.

## Files
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
