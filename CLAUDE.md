# CLAUDE.md

## Working style

- When the user tells you to do something, just do it. Don't stop to ask for approval or confirmation first.
- Exception: pause and check only if the action is genuinely likely to break something badly or is irreversible (e.g. destructive git ops, deleting data, force-push).

## out/ data

- `out/all.json` (listing **metadata** the web UI reads — no description bodies) is tracked in git deliberately — see `.gitignore` (`out/*` + `!out/all.json`; per-run CSVs stay untracked).
- Full apply-page HTML: `out/descriptions.json.gz` (`{listing_id: html}`). **Gitignored.** Don't commit it.
- Applied checkbox marks: `out/applied.json` (`{listing_id: ISO}`). **Gitignored.** Server is source of truth; FE also mirrors `localStorage` (prefer serving as `http://localhost:…` not `127.0.0.1` so browser state matches).
- Whenever a scrape, `--recompute`, or UI clear-2027 changes `out/all.json`, commit that change **on its own** — don't bundle it into a code commit. Message what happened to the data (e.g. "Scrape: 12 new listings", "Recompute: deduped 15 rows", "Clear is_2027 on N listings"), not a generic "update data".
- **Never `git checkout` / `git restore` `out/all.json` to discard uncommitted scrape results** — that is how scheduled-scrape appends get wiped. The launchd wrapper (`scripts/scrape_cron.sh`) auto-commits + pushes `out/all.json` after a successful scrape so history stays durable; agents should still commit data changes themselves when they scrape outside launchd.

## Fetching external domains

- Any source that hits an external domain must rate-limit itself **per domain**, not globally — different domains should still fetch concurrently. Reuse `DomainThrottle` in `internships/sources/ats_boards.py` rather than writing a new limiter.
- Wrap the actual HTTP call in `with throttle.hold(url):` — that holds a per-netloc lock for the whole request and enforces a minimum gap after it ends before the next request to the same host. Do **not** call `wait()` then fetch unlocked (that allowed same-domain stampedes under a thread pool).
- Default interval is ~1s between requests to the same domain (ATS / custom_boards / page-fetch / README). The goal is not getting IP-blocked by a job board or GitHub, not maximizing throughput.

## Web UI / API notes

- Serve with `python3 -m internships.webserver` (not plain `http.server`) — static files plus scrape/recompute/descriptions/applied/clear-2027.
- Scrape and recompute are mutually exclusive (shared `.run.lock`); FE disables both buttons while either is live.
- Manual clear of 2027 sets `is_2027_override: false` on the row; `recompute is_2027` must not flip those rows back on.

## PR workflow

Whenever the user says a task requires a PR, run this loop rather than doing the work solo and pushing straight to main:

1. Spawn multiple agents (5 by default, adjust to the task's size) in parallel to do the actual work — e.g. find and fix bugs, implement a feature. Keep each agent's changes solvable and scoped; no drastic redesigns unless the task explicitly calls for one.
2. Each agent pushes its own branch and opens its own PR (`gh pr create`).
3. Spawn fresh review agents — no memory of the implementation — to review each PR and leave comments.
4. Spawn new agents to address the review comments. Keep looping review → fix → re-review until each PR is resolved cleanly.
5. Once resolved, approve and merge each PR into main autonomously (`gh pr merge`). This whole loop is pre-authorized once the user has flagged a task as needing a PR — no need to check in again before merging.

Note: `git push origin main` directly is blocked by the harness's own auto-mode classifier regardless of what's written here (it requires genuine per-action user authorization, not standing instructions) — always go through a branch + PR + `gh pr merge` instead. This is also just good practice for anything going through the PR loop above.

Agents in this loop (reviewers especially) must never run destructive/irreversible git commands — `reset --hard`, `push --force`, `branch -D`, `clean -f` — on a shared branch (`main` or otherwise) as a side effect of "syncing" or "cleaning up." If a local branch needs to catch up after a squash-merge, that's a call for the orchestrating agent to make deliberately (and check reflog/stash first), not something a review/merge subagent should do on its own.
