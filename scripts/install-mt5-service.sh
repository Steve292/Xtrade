#!/bin/bash
# Install the MT5 auto-trader as a 24/7 launchd service (macOS LaunchAgent).
# Mirrors scripts/install-service.sh (the Hyperliquid service) but for
# main.py / bot/runner.py, which reads live-vs-paper mode from the MODE
# environment variable rather than a CLI flag.
#
# Starts at login, restarts automatically if it crashes, runs continuously.
# config.yaml's venue is already "mt5" and mt5_watchlist already lists the
# 7 symbols — this script does not change either.
#
# NOT run automatically by anything in this project — running it starts a
# real MODE=live trading loop against your live Exness account immediately,
# and (same shared armed flag as Hyperliquid) it can fire a real order on
# the very first qualifying signal, no separate confirmation step. Run it
# yourself only when you're ready for that.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.smc.mt5autotrader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$PROJECT_DIR/venv/bin/python"
UID_NUM="$(id -u)"

if [ ! -x "$PYTHON" ]; then
  echo "venv python not found at $PYTHON — run the installer / create the venv first." >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-u</string>
        <string>$PROJECT_DIR/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>MODE</key>
        <string>live</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/mt5-autotrader.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/mt5-autotrader.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"

echo "Installed and started $LABEL (24/7)."
echo "  status:  launchctl print gui/$UID_NUM/$LABEL | grep -E 'state|pid'"
echo "  logs:    tail -f $PROJECT_DIR/logs/mt5-autotrader.log"
echo "  stop:    scripts/uninstall-mt5-service.sh"
