---
name: run
description: Run the internship source aggregation and open the resulting jobs list in Chrome. Use when the user says "/run", "run the scraper", "aggregate internships", or "refresh the jobs list".
---

# run

Refresh internship listings and view them in the browser.

## Steps

1. From the repo root, run the scrape:
   ```bash
   python3 -m internships
   ```
2. Make sure a static file server is serving the repo root on port 8765 (needed
   because the web UI does `fetch('out/all.json')`, which fails over `file://`).
   Check first, only start if it's not already up:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/internships/web/ || true
   ```
   If that's not `200`, start it in the background:
   ```bash
   (python3 -m http.server 8765 > /tmp/internships-http.log 2>&1 &)
   ```
3. Open the jobs list in Chrome with the `open` CLI (macOS) — no browser tool needed:
   ```bash
   open -a "Google Chrome" http://localhost:8765/internships/web/
   ```

## Notes

- `python3 -m internships` only prints "no new SWE internships this run" when
  there's nothing new — that's not an error, the web UI still has the full
  history in `out/all.json`.
- Don't kill or restart the http.server if it's already serving — just reuse it.
