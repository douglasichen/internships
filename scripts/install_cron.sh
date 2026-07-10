#!/bin/bash
# (Re)installs the com.internships.scrape LaunchAgent. Safe to re-run after
# editing scripts/com.internships.scrape.plist or scripts/scrape_cron.sh.
set -euo pipefail

LABEL="com.internships.scrape"
SRC_PLIST="/Users/eggs/Documents/internships/scripts/com.internships.scrape.plist"
DEST_PLIST="/Users/eggs/Library/LaunchAgents/com.internships.scrape.plist"
UID_GUI="gui/$(id -u)"

plutil -lint "$SRC_PLIST"

mkdir -p "$(dirname "$DEST_PLIST")"
cp "$SRC_PLIST" "$DEST_PLIST"

# bootout is a no-op (with error, ignored) if it wasn't loaded yet
launchctl bootout "$UID_GUI/$LABEL" 2>/dev/null || true
launchctl bootstrap "$UID_GUI" "$DEST_PLIST"
launchctl enable "$UID_GUI/$LABEL"

echo "installed -- launchctl print $UID_GUI/$LABEL"
launchctl print "$UID_GUI/$LABEL" | head -20
