#!/bin/bash
# Install continuous knowledge ingestion as a launchd LaunchAgent.
#
# UNLIKE scripts/install-mt5-service.sh, this starts NO trading. It only
# fetches transcripts and rebuilds the local corpus. It cannot place, modify
# or close an order: bot/knowledge/ is barred from the live execute path by
# tests/test_knowledge_boundary.py, and every writer refuses config.yaml at
# runtime. The worst it can do is use bandwidth.
#
# Pacing is the real concern, not safety. YouTube rate-limits this address,
# so knowledge_daemon.py backs off exponentially (1h -> 12h) on every
# throttled cycle and returns to a 6h base only after a cycle actually
# ingests something. Do not replace it with a tighter timer.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.smc.knowledgeingest"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${KNOWLEDGE_PYTHON:-$PROJECT_DIR/venv-knowledge/bin/python}"
UID_NUM="$(id -u)"

if [ ! -x "$PYTHON" ]; then
  echo "python not found at $PYTHON" >&2
  echo "set KNOWLEDGE_PYTHON=/path/to/python and re-run." >&2
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
        <string>$PROJECT_DIR/scripts/knowledge_daemon.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/logs/knowledge-daemon.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/logs/knowledge-daemon.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/$LABEL"

echo "installed $LABEL"
echo "  status:  launchctl print gui/$UID_NUM/$LABEL | grep -E 'state|pid'"
echo "  state:   $PYTHON $PROJECT_DIR/scripts/knowledge_daemon.py --status"
echo "  log:     tail -f $PROJECT_DIR/logs/knowledge-daemon.log"
echo "  stop:    launchctl bootout gui/$UID_NUM/$LABEL"
