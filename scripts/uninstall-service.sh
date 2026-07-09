#!/bin/bash
# Stop and remove the SMC auto-trader 24/7 launchd service.
LABEL="com.smc.autotrader"
UID_NUM="$(id -u)"

if launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null; then
  echo "Stopped and unloaded $LABEL."
else
  echo "$LABEL was not loaded."
fi
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed the LaunchAgent plist."
