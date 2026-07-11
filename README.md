# internships

A backend service that hunts for **SWE internships (esp. 2027)** across multiple
sources and reports whatever's new since the last run, plus a local web UI
(“Internship Radar”) over the full history.

## Running it

```
python3 -m internships             # fetch all sources, write out/<timestamp>.csv
python3 -m internships --selftest  # run every module's inline self-check
python3 -m internships --recompute is_2027 descriptions dedup priority  # patch out/all.json, no scrape
```

Each run takes a `.run.lock` flock so two scrapes (or a scrape + recompute)
can't race each other's writes. It fetches every source, filters to SWE
internship/co-op titles that are 2027 or "maybe 2027" (drops titles that
explicitly mention a different year), then applies the scrape identity stack
and writes:

- **within-batch** — dedupe by `Listing.id`
- **`known_urls`** — README sources only skip URLs already found by this run's
  `ats_boards` (`custom_boards` does not)
- **seen** — drop ids already in per-source `data/seen/*.json` (read during fetch)
- **append content-key** — `append_all_json` skips company+title+location
  duplicates when job tokens agree (same merge rules as recompute `dedup`)
- **`persist_seen`** — write seen ids only after a successful `all.json` write

Outputs:

- writes new rows to `out/<timestamp>.csv` (includes description text for the run)
- **appends metadata** to `out/all.json` (the web UI dataset — **no** description bodies)
- stores full apply-page HTML in `out/descriptions.json.gz` keyed by listing `id`
  (`{id: html}`; gitignored)

Every listing gets a `priority` tier (1 = big tech / top, 2 = solid mid, 3 =
default — see `filters.company_priority`). Empty bodies are filled by a
throttled fetch of the apply page (see `service.py` / `desc_store.py`).

`--recompute FIELD [FIELD ...]` (`internships/recompute.py`) patches
`out/all.json` in place instead of scraping:

| field | effect |
|---|---|
| `is_2027` | recompute against `filters.year_relevance` (skips rows with `is_2027_override: false`) |
| `descriptions` | re-fetch page HTML into `descriptions.json.gz` for ids missing a body |
| `dedup` | merge same-job rows (`normalize_url` + content key with job-token guard), keep oldest |
| `priority` | recompute 1/2/3 tier against current company lists |

## Web UI

```
python3 -m internships.webserver 8765
# open http://localhost:8765/internships/web/
```

Use **`localhost`**, not `127.0.0.1`, if you already have browser state under
localhost (applied marks / filters are origin-scoped in `localStorage`).

`internships/web/index.html` is a single static file. It needs
`internships.webserver` (not plain `http.server`) for the API routes below.

### Actions
- **Scrape** / **Recompute** — mutually exclusive; both disable while either
  (or a terminal scrape) holds `.run.lock`. Recompute opens a popup to pick
  fields (none selected by default; **Run** disabled until you pick ≥1).
- Live status line for scrape/recompute progress.

### Filters (persisted in `localStorage`)
Search; source chips; **SWE only** / **2027 only** / **North America**
(default on); P1+P2 / P1; hide applied / applied only. Filter panel
open/closed is saved too. SWE only re-applies the software-engineering title
filter on already-scraped rows (hides marketing/AI-PM false positives).

### Per listing
- **Applied** checkbox — durable on disk as `out/applied.json` via
  `GET/POST /api/applied`, mirrored to `localStorage` (survives origin quirks).
- **Apply →** — opens the job; marks the row **Last opened** (local only).
- **2027** badge — click → confirm → clear `is_2027` (writes
  `is_2027_override: false` so recompute won’t put it back).
- **No desc** — missing body in the description store; click to paste/submit
  via `POST /api/descriptions`.
- Company **N applied** expander for other applied roles at the same company.

### API surface (`webserver.py`)
| method | path | purpose |
|---|---|---|
| `POST` | `/api/scrape` | start scrape (202) |
| `GET` | `/api/scrape/status` | scrape thread state |
| `POST` | `/api/recompute?fields=…` | start recompute (202) |
| `GET` | `/api/recompute/status` | recompute thread state |
| `GET` | `/api/status` | `{active}` if `.run.lock` held |
| `GET` | `/api/descriptions/ids` | ids that have a description body |
| `POST` | `/api/descriptions` | `{id, text}` upsert one description (409 if `.run.lock` held) |
| `GET`/`POST` | `/api/applied` | applied map `id → ISO` (`?merge=1` to union) |
| `POST` | `/api/listings/clear-2027` | `{id}` clear 2027 + set override (409 if `.run.lock` held) |

No auth — localhost personal tool only.

## Sources (`internships/sources/`)
- `ats_boards.py` — Ashby / Lever / Greenhouse / Workday / Oracle Fusion /
  Eightfold / SmartRecruiters / Pinpoint, etc. from `companies.csv`. Workday
  boards are keyword-searched ("intern"/"co-op"), paginated, and often get a
  detail-page description when the title already looks SWE-intern.
- `custom_boards.py` — ~50 CONFIG fetch endpoints for boards that don’t fit
  the generic ATS parsers, plus a Tesla careers state decoder.
- `github_readme.py` — [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
- `speedyapply.py` — [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs)
- `sndsh404.py` — [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships)

Adding a source = class with `.name` and `.fetch() -> list[Listing]`, register
in `internships/__main__.py`. README sources share `md_table.py`.

External fetches are **per-domain** throttled (`DomainThrottle.hold` in
`ats_boards.py`) so the same host doesn’t get stampeded under the thread pool.

## Data layout

| path | tracked? | contents |
|---|---|---|
| `out/all.json` | **yes** | listing metadata history for the UI |
| `out/descriptions.json.gz` | no | `{id: full page HTML}` |
| `out/applied.json` | no | `{id: ISO timestamp}` applied marks |
| `out/<timestamp>.csv` | no | per-run new listings |
| `data/seen/*.json` | no (`data/`) | per-source seen ids |
| `companies.csv` | yes | company → board URL / API / `api_status` |

`api_status`: `ok` | `dead` | `skipped` (no API wired). Dead/skipped with no
rows in `all.json` are coverage gaps (see companies list + scrape results).

## Scheduled scrape

`scripts/install_cron.sh` installs a macOS `launchd` LaunchAgent that runs
the scrape about every 2 hours with ±10min jitter. Details (logs, uninstall,
Full Disk Access for `~/Documents`): `docs/architecture.md` → **Scheduled scrape**.

## Legacy: boards.csv

Earlier Ashby/Lever board discovery artifact — **not** read by the
`internships` service. Columns / re-sweep notes are historical only.

| column | meaning |
|---|---|
| `platform` | `ashby` or `lever` |
| `company` | board slug |
| `board_url` | human job board |
| `api_url` | JSON posting API |
| `total_jobs` / `intern_roles` / … | snapshot counts from the old sweep |

More detail: [`docs/architecture.md`](docs/architecture.md).
