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

set -u

REPO="/Users/eggs/Documents/internships"
PYTHON="/Users/eggs/.pyenv/versions/3.10.17/bin/python3"

jitter=$(( RANDOM % 1201 ))
echo "$(date '+%Y-%m-%d %H:%M:%S') jitter=${jitter}s, sleeping before scrape"
sleep "$jitter"

cd "$REPO" || exit 1
echo "$(date '+%Y-%m-%d %H:%M:%S') starting scrape"
"$PYTHON" -m internships
status=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') scrape finished (exit=$status)"
exit "$status"
