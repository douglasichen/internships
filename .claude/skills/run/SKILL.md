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
2. Make sure `internships.webserver` is serving the repo root on port 8765
   (static files + scrape/recompute/descriptions/applied APIs — plain
   `http.server` is not enough). Prefer **localhost** over 127.0.0.1 so
   browser `localStorage` / applied state match prior sessions. Check first,
   only start if it's not already up:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/internships/web/ || true
   ```
   If that's not `200`, start it in the background:
   ```bash
   (python3 -m internships.webserver 8765 > /tmp/internships-http.log 2>&1 &)
   ```
3. Open the jobs list in Chrome with the `open` CLI (macOS) — no browser tool needed:
   ```bash
   open -a "Google Chrome" http://localhost:8765/internships/web/
   ```

## Notes

- `python3 -m internships` only prints "no new SWE internships this run" when
  there's nothing new — that's not an error, the web UI still has the full
  history in `out/all.json`.
- Don't kill or restart the server if it's already serving — just reuse it.
- If a plain `python3 -m http.server` is already bound to 8765, kill it and
  start `internships.webserver` instead (API POSTs will 404/501 otherwise).
- Descriptions live in `out/descriptions.json.gz`; applied marks in
  `out/applied.json` — both gitignored.
