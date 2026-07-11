#!/bin/bash
# Install the SMC auto-trader as a 24/7 launchd service (macOS LaunchAgent).
# It starts at login, restarts automatically if it crashes, and runs the
# screened live watchlist loop continuously.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.smc.autotrader"
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
        <string>$PROJECT_DIR/hypertrade.py</string>
        <string>--watchlist</string>
        <string>--live</string>
        <string>--loop</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/autotrader.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/autotrader.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"

echo "Installed and started $LABEL (24/7)."
echo "  status:  launchctl print gui/$UID_NUM/$LABEL | grep -E 'state|pid'"
echo "  logs:    tail -f $PROJECT_DIR/logs/autotrader.log"
echo "  stop:    scripts/uninstall-service.sh"
