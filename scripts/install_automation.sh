#!/bin/zsh
# Install (or reinstall) the launchd agents for the current user.
# Undo with scripts/uninstall_automation.sh.
set -eu
ROOT="/Applications/Projects/Intraday/nse-intraday-agent"
AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS" "$ROOT/data/logs"
chmod +x "$ROOT/scripts/daily_evaluation.sh"
for name in com.nseintraday.evaluation com.nseintraday.dashboard; do
  plutil -lint "$ROOT/scripts/launchd/$name.plist" >/dev/null
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  cp "$ROOT/scripts/launchd/$name.plist" "$AGENTS/$name.plist"
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$name.plist"
  echo "installed $name"
done
echo
echo "To wake the Mac from sleep before each session, run once (needs your password):"
echo "  sudo pmset repeat wakeorpoweron MTWRF 08:45:00"
