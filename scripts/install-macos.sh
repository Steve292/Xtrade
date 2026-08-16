#!/usr/bin/env bash
# TraderX — macOS installer (Apple Silicon + Intel)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ARCH=$(uname -m)
echo "╔══════════════════════════════════════════╗"
echo "║   TraderX — macOS Setup                  ║"
echo "║   Architecture: $ARCH                    ║"
echo "╚══════════════════════════════════════════╝"

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install via: brew install python@3.12"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "→ Python $PY_VERSION detected"

# Create venv
if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip -q

echo "→ Installing dependencies..."
pip install -r requirements.txt -q

# macOS-specific deps
echo "→ Installing macOS menu bar dependencies..."
pip install rumps pyobjc-framework-Cocoa -q 2>/dev/null || echo "  (rumps skipped — run manually if needed)"

# Copy env if missing
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "→ Created .env from template"
fi

# Build .app bundle
APP_DIR="$PROJECT_DIR/macos/TraderX.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cat > "$APP_DIR/Contents/MacOS/launcher" << LAUNCHER
#!/bin/bash
# TraderX desktop launcher. Launched by macOS LaunchServices (double-click),
# which gives a minimal PATH and arbitrary CWD — so absolute paths throughout,
# and the venv interpreter is invoked directly (no reliance on \\\$0 or activate).
DIR="$PROJECT_DIR"
PY="\$DIR/venv/bin/python"
LSOF="/usr/sbin/lsof"
PORT=8420
URL="http://127.0.0.1:\$PORT"

# The venv python is universal but numpy/pandas are compiled arm64-only.
# LaunchServices starts the app under x86_64 (Rosetta), where those .so files
# fail to load. Force native arm64 so extensions match the interpreter arch.
ARCH="/usr/bin/arch -arm64"

cd "\$DIR" || exit 1

# Start the local dashboard server only if it isn't already listening.
if ! "\$LSOF" -iTCP:\$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    mkdir -p logs
    nohup \$ARCH "\$PY" webapp/server.py >> logs/dashboard.log 2>&1 &
    for i in \$(seq 1 20); do
        "\$LSOF" -iTCP:\$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
        sleep 0.5
    done
fi

# The visible result: the TraderX control panel opens in the default browser.
/usr/bin/open "\$URL"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>TraderX</string>
    <key>CFBundleDisplayName</key>
    <string>TraderX</string>
    <key>CFBundleIdentifier</key>
    <string>com.traderx.tradingbot</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cp "$PROJECT_DIR/macos/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
codesign --force --deep --sign - "$APP_DIR" 2>/dev/null || true

echo ""
echo "✓ Installation complete!"
echo ""
echo "  Run bot (CLI):       python main.py"
echo "  Run backtest:        python backtest.py --bars 2000"
echo "  Menu bar app:        open macos/TraderX.app"
echo "  Or:                  python -m bot.macos.menubar"
echo ""
