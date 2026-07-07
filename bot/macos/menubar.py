"""
macOS menu bar app for SMC Trading Bot.
Requires: pip install rumps pyobjc-framework-Cocoa
Run: python -m bot.macos.menubar
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

try:
    import rumps
except ImportError:
    rumps = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SMCBotApp(rumps.App):
    def __init__(self):
        super().__init__("SMC Bot", quit_button=None)
        self.bot_process: subprocess.Popen | None = None
        self.menu = [
            rumps.MenuItem("Start Bot", callback=self.start_bot),
            rumps.MenuItem("Stop Bot", callback=self.stop_bot),
            None,
            rumps.MenuItem("Run Backtest", callback=self.run_backtest),
            rumps.MenuItem("Scan Market", callback=self.scan_market),
            None,
            rumps.MenuItem("Open Project", callback=self.open_project),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

    @rumps.clicked("Start Bot")
    def start_bot(self, _):
        if self.bot_process and self.bot_process.poll() is None:
            rumps.alert("Bot is already running")
            return
        venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        self.bot_process = subprocess.Popen(
            [python, str(PROJECT_ROOT / "main.py")],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.title = "SMC ●"
        rumps.notification("SMC Bot", "Started", "Paper trading bot is running")

    @rumps.clicked("Stop Bot")
    def stop_bot(self, _):
        if self.bot_process and self.bot_process.poll() is None:
            self.bot_process.terminate()
            self.bot_process.wait(timeout=5)
        self.bot_process = None
        self.title = "SMC Bot"
        rumps.notification("SMC Bot", "Stopped", "Bot has been stopped")

    @rumps.clicked("Run Backtest")
    def run_backtest(self, _):
        def _run():
            venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
            python = str(venv_python) if venv_python.exists() else sys.executable
            result = subprocess.run(
                [python, str(PROJECT_ROOT / "backtest.py"), "--bars", "1500"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = result.stdout[-500:] if result.stdout else result.stderr[-500:]
            rumps.alert("Backtest Results", output or "No output")

        threading.Thread(target=_run, daemon=True).start()
        rumps.notification("SMC Bot", "Backtest", "Running backtest...")

    @rumps.clicked("Scan Market")
    def scan_market(self, _):
        def _run():
            venv_python = PROJECT_ROOT / "venv" / "bin" / "python"
            python = str(venv_python) if venv_python.exists() else sys.executable
            result = subprocess.run(
                [python, str(PROJECT_ROOT / "scan.py"), "--live"],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout or result.stderr
            rumps.alert("Market Scan", output[-600:] if output else "Scan failed")

        threading.Thread(target=_run, daemon=True).start()

    @rumps.clicked("Open Project")
    def open_project(self, _):
        subprocess.run(["open", str(PROJECT_ROOT)])

    @rumps.clicked("Quit")
    def quit_app(self, _):
        self.stop_bot(None)
        rumps.quit_application()


def run_menubar():
    if rumps is None:
        print("Install macOS dependencies: pip install rumps pyobjc-framework-Cocoa")
        sys.exit(1)
    SMCBotApp().run()


if __name__ == "__main__":
    run_menubar()
