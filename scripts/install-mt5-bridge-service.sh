#!/bin/bash
# Install the MT5 rpyc bridge as a LaunchAgent.
#
# The bridge is what mt5linux connects to: a Python process running INSIDE the
# MetaTrader 5 Wine prefix, exposing the MetaTrader5 module over TCP on
# 127.0.0.1:18812. Without it, bot/mt5/client.py gets ECONNREFUSED and
# com.smc.mt5autotrader dies on every start -- which is exactly what was
# happening (exit code 1, "Sign in"/connection-refused loops) while the
# terminal itself was running fine. The terminal being up is not enough; the
# bridge is a separate process and nothing was supervising it.
#
# Bound to 127.0.0.1 deliberately. docs/MT5_SETUP.md warns against exposing
# this port -- it is an unauthenticated rpyc SlaveService, i.e. arbitrary
# remote code execution on this machine if it ever listens on 0.0.0.0.
set -e

# Overridable on purpose. Derived from the script's own location this would
# pin a permanent LaunchAgent to whatever checkout it happened to be run from
# -- including a .claude/worktrees/ copy, which gets deleted. The plist must
# point at the durable project directory.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
LABEL="com.smc.mt5bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
WINEPREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
WIN_PYTHON='C:\Python311\python.exe'
PORT="${MT5_PORT:-18812}"

if [ ! -x "$WINE" ]; then
  echo "MetaTrader 5.app not found at the expected path -- is it installed?" >&2
  exit 1
fi
if [ ! -f "$WINEPREFIX/drive_c/Python311/python.exe" ]; then
  echo "No Windows Python in the Wine prefix. Install Python 3.11 into MT5's" >&2
  echo "prefix, then: pip install MetaTrader5 mt5linux  (see docs/MT5_SETUP.md)" >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WINE</string>
        <string>$WIN_PYTHON</string>
        <string>-m</string>
        <string>mt5linux</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>-p</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WINEPREFIX</key><string>$WINEPREFIX</string>
        <key>WINEDEBUG</key><string>-all</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardOutPath</key><string>$PROJECT_DIR/logs/mt5-bridge.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/mt5-bridge.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"

echo "Installed $LABEL -> $PLIST"
echo "Logs: $PROJECT_DIR/logs/mt5-bridge.log"
echo
echo "Verify with:  lsof -nP -iTCP:$PORT -sTCP:LISTEN"
