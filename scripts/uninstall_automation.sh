#!/bin/zsh
# Remove the launchd agents installed by install_automation.sh.
set -u
for name in com.nseintraday.evaluation com.nseintraday.dashboard; do
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null && echo "stopped $name"
  rm -f "$HOME/Library/LaunchAgents/$name.plist" && echo "removed $name"
done
echo "To remove the wake schedule (if you set it): sudo pmset repeat cancel"
