# CLAUDE.md

## Working style

- When the user tells you to do something, just do it. Don't stop to ask for approval or confirmation first.
- Exception: pause and check only if the action is genuinely likely to break something badly or is irreversible (e.g. destructive git ops, deleting data, force-push).

## Fetching external domains

- Any source that hits an external domain must rate-limit itself **per domain**, not globally — different domains should still fetch concurrently. Reuse `DomainThrottle` in `internships/sources/ats_boards.py` (per-netloc lock + minimum interval between requests) rather than writing a new limiter.
- No need for a strict/hard cap — just something reasonable (the ATS sweep defaults to 1s between requests to the same domain). The goal is not getting IP-blocked by a job board or GitHub, not maximizing throughput.
