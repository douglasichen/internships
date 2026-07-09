# Architecture

A stdlib-only Python backend that hunts SWE internships across multiple
sources, plus a static web UI over the results. No database, no framework,
no external dependencies.

## Flow

```mermaid
flowchart TD
    subgraph Sources["internships/sources/"]
        ATS["AtsBoardsSource\n(ats_boards.py)\nAshby/Lever/Greenhouse/Workday\nJSON APIs"]
        GH["GithubReadmeSource\n(github_readme.py)"]
        SA["SpeedyApplySource\n(speedyapply.py)"]
        SN["Sndsh404Source\n(sndsh404.py)"]
    end

    CSV[("companies.csv\n481 companies,\napi_urls + api_status")] --> ATS
    R1(["vanshb03/\nSummer2027-Internships\nREADME.md"]) --> GH
    R2(["speedyapply/\n2027-SWE-College-Jobs\nREADME.md"]) --> SA
    R3(["sndsh404/\nsummer-2027-internships\nREADME.md"]) --> SN

    GH -.->|shared table parsing| MD["md_table.py"]
    SA -.->|shared table parsing| MD
    SN -.->|shared table parsing| MD

    ATS --> SVC
    GH --> SVC
    SA --> SVC
    SN --> SVC

    subgraph Orchestrator["internships/service.py — run()"]
        SVC["_run_one() per source, in parallel\n1. filters.is_swe_internship(title)\n2. filters.year_relevance(title, location, extra_text)\n3. dedupe within batch by Listing.id()\n4. drop ids already in SeenStore\n5. persist updated seen-ids"]
    end

    SEEN[("data/seen/SOURCE.json\nper-source seen-id store")]
    SVC <--> SEEN

    SVC --> MAIN["internships/__main__.py — main()"]
    MAIN --> CSVOUT[("out/TIMESTAMP.csv\nthis run's new listings")]
    MAIN --> ALLJSON[("out/all.json\nevery listing ever found,\nappended, never overwritten")]

    ALLJSON --> WEB["internships/web/index.html\nvanilla JS, fetch('/out/all.json')\nsearch + source + 2027-only filters"]
    WEB --> Browser(["served via\npython3 -m http.server"])

    style CSV fill:#F1EEE5,stroke:#8A8478
    style R1 fill:#F1EEE5,stroke:#8A8478
    style R2 fill:#F1EEE5,stroke:#8A8478
    style R3 fill:#F1EEE5,stroke:#8A8478
    style SEEN fill:#F1EEE5,stroke:#8A8478
    style CSVOUT fill:#F1EEE5,stroke:#8A8478
    style ALLJSON fill:#F1EEE5,stroke:#8A8478
```

## Components

| File | Responsibility |
|---|---|
| `internships/models.py` | `Listing` dataclass — the one shape every source produces. `id()` gives a stable identity for dedup (sha1 of source+company+url+location); `is_2027` is a display-only flag. |
| `internships/filters.py` | `is_swe_internship(title)` (regex) and `year_relevance(title, location, extra_text)` (drops titles that explicitly name a non-2027 year; title/location are authoritative, the description body can only confirm 2027, never veto). |
| `internships/seen_store.py` | `SeenStore` — a JSON file of previously-reported listing ids, one per source. This *is* the dedup mechanism across runs; there's no snapshot/diff machinery. |
| `internships/sources/ats_boards.py` | Reads `companies.csv`, fetches each company's Ashby/Lever/Greenhouse/Workday JSON endpoint (per-domain throttled, concurrent across domains), extracts postings into `Listing`s, resolves Workday's relative URLs to absolute ones, writes `api_status` back to the CSV. |
| `internships/sources/{github_readme,speedyapply,sndsh404}.py` | Fetch a tracked repo's `README.md`, parse its job table into `Listing`s. Each repo's table dialect differs slightly (marker comments or not, HTML links vs. markdown links, company-continuation rows or not) — handled per-source. |
| `internships/sources/md_table.py` | Shared table-parsing primitives used by the three README sources: `extract_rows` (marker-delimited tables), `extract_table_by_header` (unmarked tables, located by header row), `clean_text`, `extract_href`/`extract_md_link`, `is_closed`. |
| `internships/service.py` | `run(sources)` — fetches every source in parallel, applies the filters, dedupes, persists seen-ids, returns what's new. One bad source degrades to a `SourceResult(error=...)` rather than taking down the run. |
| `internships/__main__.py` | CLI entrypoint (`python3 -m internships`). Writes this run's new listings to `out/<timestamp>.csv` and appends them to the cumulative `out/all.json`. |
| `internships/web/index.html` | Single static file, no build step. Fetches `out/all.json`, renders a reverse-chronological feed grouped by scrape run, with search/source/2027-only filters. |

## Adding a source

Write a class with `.name: str` and `.fetch() -> list[Listing]`, register it
in `SOURCES` in `internships/__main__.py`. Everything downstream (filtering,
dedup, output, web UI) is source-agnostic.

## What's deliberately not here

- **No scheduling** — run `python3 -m internships` manually or cron it yourself.
- **No database** — `data/seen/*.json` + `out/all.json` are the entire persisted state.
- **No cross-source dedup** — the same job can appear once per source that lists it (e.g. found by both `ats_boards` and a README tracker). Accepted, not a bug.
