#!/usr/bin/env bash
# SMC Trading Bot — macOS installer (Apple Silicon + Intel)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

ARCH=$(uname -m)
echo "╔══════════════════════════════════════════╗"
echo "║   SMC Trading Bot — macOS Setup          ║"
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
APP_DIR="$PROJECT_DIR/macos/SMCBot.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cat > "$APP_DIR/Contents/MacOS/launcher" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$DIR"
source venv/bin/activate
exec python -m bot.macos.menubar
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>SMC Bot</string>
    <key>CFBundleDisplayName</key>
    <string>SMC Trading Bot</string>
    <key>CFBundleIdentifier</key>
    <string>com.smc.tradingbot</string>
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

echo ""
echo "✓ Installation complete!"
echo ""
echo "  Run bot (CLI):       python main.py"
echo "  Run backtest:        python backtest.py --bars 2000"
echo "  Menu bar app:        open macos/SMCBot.app"
echo "  Or:                  python -m bot.macos.menubar"
echo ""
