# Architecture

A stdlib-only Python backend that hunts SWE internships across multiple
sources, plus a static web UI over the results. No database, no framework,
no external dependencies.

## Flow

```mermaid
flowchart TD
    subgraph Sources["internships/sources/"]
        ATS["AtsBoardsSource\n(ats_boards.py)\nAshby/Lever/GH/Workday/Oracle/Eightfold/…"]
        CB["CustomBoardsSource\n(custom_boards.py)\n~50 CONFIG + Tesla decoder"]
        GH["GithubReadmeSource"]
        SA["SpeedyApplySource"]
        SN["Sndsh404Source"]
        JR["JobrightSource\n(jobright.py)"]
    end

    CSV[("companies.csv\napi_urls + api_status")] --> ATS
    R1(["vanshb03 README"]) --> GH
    R2(["speedyapply README"]) --> SA
    R3(["sndsh404 README"]) --> SN
    JRAPI(["jobright.ai\n/swan/mini-sites/list"]) --> JR

    GH -.->|md_table + README_THROTTLE| MD["md_table.py"]
    SA -.-> MD
    SN -.-> MD

    subgraph Orchestrator["service.py — run() / _run_one per source"]
        STEP1["1. ats_boards alone first"]
        STEP2["2. build known_urls from its listings"]
        STEP3["3. custom_boards + README in parallel\nper source _run_one order:\nfilter SWE+year → batch Listing.id dedupe\n→ README-only known_urls skip\n→ page-fetch empty extra_text\n→ drop SeenStore ids (read only)"]
        STEP4["4. return new listings + pending seen\n(caller writes CSV/all.json/descriptions)"]
        STEP1 --> STEP2 --> STEP3 --> STEP4
    end

    ATS --> STEP1
    CB --> STEP3
    GH --> STEP3
    SA --> STEP3
    SN --> STEP3
    JR --> STEP3

    STEP4 --> MAIN["__main__.py\n.run.lock"]
    MAIN --> CSVOUT[("out/TIMESTAMP.csv\n+ description col")]
    MAIN --> ALLJSON[("out/all.json\nmetadata only\n+ content-key merge")]
    MAIN --> DESCS[("out/descriptions.json.gz\n{id: html}")]
    ALLJSON -->|persist_seen after write| SEEN[("data/seen/SOURCE.json")]

    RECOMPUTE["recompute.py"] -.->|patches| ALLJSON
    RECOMPUTE -.->|backfill| DESCS
    WEB -.->|POST /api/recompute| RECOMPUTE
    WEB -.->|POST /api/scrape| STEP1
    WEB -.->|descriptions / applied / clear-2027| ALLJSON
    WEB -.-> DESCS
    WEB -.-> APPLIED[("out/applied.json")]

    ALLJSON --> WEB["web/index.html"]
    WEB --> Browser(["internships.webserver"])

    style CSV fill:#F1EEE5,stroke:#8A8478
    style SEEN fill:#F1EEE5,stroke:#8A8478
    style CSVOUT fill:#F1EEE5,stroke:#8A8478
    style ALLJSON fill:#F1EEE5,stroke:#8A8478
    style DESCS fill:#F1EEE5,stroke:#8A8478
    style APPLIED fill:#F1EEE5,stroke:#8A8478
```

## Components

| File | Responsibility |
|---|---|
| `internships/models.py` | `Listing` dataclass. `id()` = sha1 of `source\|company\|normalize_url(url) or title\|location` (source is in the hash → same apply URL from two sources = two ids). `normalize_url` strips tracking params, keeps identity query (`gh_jid`, `token`, …). `is_2027` / `priority` are properties from filters. |
| `internships/filters.py` | `is_swe_internship`, `year_relevance` (2027 / maybe / no), `company_priority` tiers 1–3. |
| `internships/seen_store.py` | Per-source JSON of already-reported listing ids. Cross-run dedup. Persist deferred until after `all.json` write succeeds. |
| `internships/sources/ats_boards.py` | `companies.csv` ATS sweep + `DomainThrottle` (per-netloc lock + min interval via `hold()`). |
| `internships/sources/custom_boards.py` | ~50 CONFIG fetch endpoints + Tesla careers state decoder (`DECODERS`). |
| `internships/sources/{github_readme,speedyapply,sndsh404}.py` | Tracked README job tables. |
| `internships/sources/jobright.py` | Jobright US SWE intern minisite (`POST /swan/mini-sites/list`, paginated). |
| `internships/sources/md_table.py` | Shared README table parsing + shared `README_THROTTLE`. |
| `internships/service.py` | `run(sources)`: ats_boards first, then others in parallel. Per source: filter → batch `Listing.id` dedupe → README-only `known_urls` skip → page-fetch empty `extra_text` → drop already-seen ids (pending write only). |
| `internships/__main__.py` | CLI + lock. CSV + `append_all_json` (content-key + job-token merge) + `desc_store`; `persist_seen()` only after a successful `all.json` write. Registers all sources. |
| `internships/desc_store.py` | `out/descriptions.json.gz` — `{listing_id: html}`. Legacy migrate from plain JSON / per-id `.html.gz`. |
| `internships/applied_store.py` | `out/applied.json` — `{listing_id: ISO timestamp}` for the UI applied checkbox. |
| `internships/recompute.py` | In-place `all.json` patches: `is_2027` (honors `is_2027_override: false`), `priority`, `dedup` (`normalize_url` + content key with job-token guard; same content-key rules as scrape append), `descriptions` backfill, `clear_is_2027(id)`. |
| `internships/webserver.py` | Static files + scrape/recompute/status + descriptions + applied + clear-2027 APIs. Background threads; shared `.run.lock`. |
| `internships/web/index.html` | Vanilla FE: filters (persisted), Scrape/Recompute (mutex + recompute field popup), applied (server∪localStorage), last-opened Apply highlight, clear-2027 confirm, missing-desc submit, company applied siblings. |

## Web API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/scrape` | 202 or 409 if scrape already running |
| `GET` | `/api/scrape/status` | `{running, result, error}` |
| `POST` | `/api/recompute?fields=dedup,is_2027,…` | 202 / 400 / 409 |
| `GET` | `/api/recompute/status` | `{running, result, error}` |
| `GET` | `/api/status` | `{active}` — any holder of `.run.lock` |
| `GET` | `/api/descriptions/ids` | `{ids: [...]}` |
| `POST` | `/api/descriptions` | body `{id, text}` — 409 if `.run.lock` held |
| `GET` | `/api/applied` | full map |
| `POST` | `/api/applied` | replace map; `?merge=1` unions (later ISO wins) |
| `POST` | `/api/listings/clear-2027` | body `{id}` → `is_2027=false`, `is_2027_override=false` — 409 if `.run.lock` held |

## Adding a source

Class with `.name: str` and `.fetch() -> list[Listing]`, append to `SOURCES`
in `__main__.py`. Filtering, dedup, storage, and the UI are source-agnostic
(aside from source chip labels/colors in `index.html`).

## Scheduled scrape

`scripts/` holds a `launchd` LaunchAgent (not cron — better sleep catch-up on
macOS) that runs `python3 -m internships` about every 2 hours:

- `scripts/com.internships.scrape.plist` — `StartInterval` 7200s + `RunAtLoad`
- `scripts/scrape_cron.sh` — 0–20min random sleep (±10min jitter), absolute paths
- `scripts/install_cron.sh` — (re)install LaunchAgent

Concurrency relies on `.run.lock` (UI scrape / CLI / agent all share it).

Logs: `~/Library/Logs/internships-scrape.log`.  
Status: `launchctl list com.internships.scrape`.  
Uninstall: `launchctl bootout gui/$(id -u)/com.internships.scrape` and remove
`~/Library/LaunchAgents/com.internships.scrape.plist`.

If the repo is under `~/Documents`, grant Full Disk Access to `/bin/bash` and
the `python3` binary used by the agent (TCC does not inherit shell grants).

## What's deliberately not here

- **No database** — `data/seen/*.json`, `out/all.json`, `descriptions.json.gz`,
  and `applied.json` are the persisted state.
- **No fuzzy cross-source dedup of all URL variants** — `ats_boards` runs first
  and README sources skip exact normalized URLs it found (`custom_boards` does
  not). Scrape `append_all_json` and recompute `dedup` both merge same
  company+title+location when embedded job tokens agree (recompute also
  collapses by `normalize_url`). Distinct URLs for the same human job can
  still appear; accepted, not always a bug.
- **No auth on webserver** — localhost personal tool only.
