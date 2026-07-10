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
   (it's a static file server *and* backs the web UI's "Recompute" button --
   the web UI does `fetch('out/all.json')`, which fails over `file://`, and
   `POST /api/recompute`, which plain `http.server` can't do). Check first,
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
- If a plain `python3 -m http.server` is already bound to 8765 from before
  this feature existed, the web UI's "Recompute" button won't work (POST
  fails with 501) even though everything else looks fine -- kill that
  process and start `internships.webserver` instead.
