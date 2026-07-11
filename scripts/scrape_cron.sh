#!/bin/bash
# Wrapper invoked by launchd (~/Library/LaunchAgents/com.internships.scrape.plist)
# every ~2h. launchd doesn't source .zshrc/.bash_profile, so both paths below
# are absolute -- no reliance on PATH or pyenv shims.
#
# Jitter: launchd's StartInterval fires on a fixed 7200s (2h) cadence -- it
# can't itself express a random/negative offset, so we jitter here instead.
# Sleep a uniform random amount in [0, 1200]s (0-20min) before each run, i.e.
# symmetric +/-10min around a nominal "10 minutes after the trigger" point --
# not a one-sided 0-to-+10min delay, which would bias every run late.
#
# Concurrency: relies entirely on internships/__main__.py's own .run.lock
# flock -- if a previous run is still going (or the web UI kicked one off),
# this just no-ops. No locking here.
#
# Data safety: out/all.json is tracked in git. Agent/checkout can wipe uncommitted
# appends. After a successful scrape that changed all.json, commit + push so
# the UI history and remote stay durable.

set -u

REPO="/Users/eggs/Documents/internships"
PYTHON="/Users/eggs/.pyenv/versions/3.10.17/bin/python3"
GIT="/usr/bin/git"

jitter=$(( RANDOM % 1201 ))
echo "$(date '+%Y-%m-%d %H:%M:%S') jitter=${jitter}s, sleeping before scrape"
sleep "$jitter"

cd "$REPO" || exit 1
echo "$(date '+%Y-%m-%d %H:%M:%S') starting scrape"
"$PYTHON" -m internships
status=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') scrape finished (exit=$status)"

if [ "$status" -eq 0 ]; then
  # Commit all.json if the scrape changed it (and optionally companies.csv
  # api_status). Never stage anything else. Failures here must not fail the
  # scrape exit code for launchd accounting.
  if ! "$GIT" -C "$REPO" diff --quiet -- out/all.json 2>/dev/null \
     || ! "$GIT" -C "$REPO" diff --quiet -- companies.csv 2>/dev/null; then
    n_new=$("$PYTHON" -c "
import json
from pathlib import Path
p = Path('$REPO') / 'out' / 'all.json'
rows = json.loads(p.read_text())
if not rows:
    print(0)
else:
    latest = max((r.get('scraped_at') or '') for r in rows)
    print(sum(1 for r in rows if (r.get('scraped_at') or '') == latest))
" 2>/dev/null || echo 0)
    msg="Scrape: ${n_new} new listings"
    "$GIT" -C "$REPO" add -- out/all.json companies.csv 2>/dev/null || true
    if "$GIT" -C "$REPO" commit -m "$msg" -- out/all.json companies.csv 2>/dev/null; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') committed: $msg"
      if "$GIT" -C "$REPO" push origin HEAD 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') pushed to origin"
      else
        echo "$(date '+%Y-%m-%d %H:%M:%S') warn: git push failed (data still committed locally)"
      fi
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') no git commit (nothing staged or commit failed)"
    fi
  fi
fi

exit "$status"
