"""Enforces the report-only boundary. No network.

Run directly (`python tests/test_knowledge_boundary.py`) or under pytest.

Three docstrings in bot/knowledge/ promise this package never edits trading
configuration and never runs inside the live execute path. Docstrings are
intent. These tests are enforcement, and they are deliberately structural
rather than behavioural: they parse the source, so they catch a violation the
moment it is written, without needing a test that happens to exercise the bad
path.

Why it matters concretely: both accounts this repo trades are armed and real
(MT5 trade_mode=2, Hyperliquid mainnet). A pipeline that ingests unreviewed
YouTube transcripts must not be one refactor away from moving min_rr or
max_stop_pct on either of them.
"""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import candidates as candidates_mod
from bot.knowledge import channels as channels_mod
from bot.knowledge.store import FORBIDDEN_WRITE_NAMES, KnowledgeStore, assert_writable

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "bot" / "knowledge"

# Modules that place orders or drive a live session. Nothing in bot/knowledge/
# may import these, and they may not import bot/knowledge/.
LIVE_PATH = ("bot.runner", "bot.hyperliquid", "bot.mt5", "bot.exchange",
             "hypertrade", "hyperwallet", "main")


def _modules():
    for py in sorted(PKG.glob("*.py")):
        yield py, ast.parse(py.read_text())


def test_package_never_serialises_yaml():
    # The only way to rewrite config.yaml is to dump YAML. Nothing here should
    # be able to, so the capability itself is banned rather than its misuse.
    offenders = []
    for py, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("dump", "safe_dump", "dump_all"):
                    offenders.append(f"{py.name}:{node.lineno}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names]
                         + ([node.module] if isinstance(node, ast.ImportFrom) else []))
                if any(n and n.split(".")[0] == "yaml" for n in names):
                    offenders.append(f"{py.name}:{node.lineno} imports yaml")
    assert not offenders, f"bot/knowledge/ can serialise YAML: {offenders}"


def test_package_never_opens_a_forbidden_file():
    offenders = []
    for py, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in ("open", "write_text", "write_bytes"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.lower() in FORBIDDEN_WRITE_NAMES:
                        offenders.append(f"{py.name}:{node.lineno}")
    assert not offenders, f"forbidden file opened in: {offenders}"


def test_package_does_not_import_the_live_execute_path():
    offenders = []
    for py, tree in _modules():
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module]
            for m in mods:
                if any(m == lp or m.startswith(lp + ".") for lp in LIVE_PATH):
                    offenders.append(f"{py.name}:{node.lineno} -> {m}")
    assert not offenders, f"knowledge imports the live trading path: {offenders}"


def test_the_live_path_does_not_import_knowledge():
    # The other direction, and the one that actually guarantees this feature
    # cannot influence a trade: if nothing on the execute path imports
    # bot.knowledge, no candidate can reach an order however wrong it is.
    offenders = []
    for py in list((ROOT / "bot").rglob("*.py")) + list(ROOT.glob("*.py")):
        if "knowledge" in py.parts or py.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(py.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m and "knowledge" in m:
                    offenders.append(f"{py.relative_to(ROOT)}:{node.lineno} -> {m}")
    assert not offenders, f"live code imports bot.knowledge: {offenders}"


def test_assert_writable_refuses_trading_config():
    for name in ("config.yaml", "config.yml", ".env"):
        try:
            assert_writable(Path("/tmp") / name)
        except PermissionError:
            continue
        raise AssertionError(f"assert_writable allowed {name}")


def test_assert_writable_refuses_a_symlink_to_trading_config():
    # An allowed FILENAME that is a symlink to config.yaml passes a name-only
    # check. Before this, the only thing preventing a clobber was that every
    # writer happens to use tmp-file + os.replace, which replaces the link
    # instead of following it -- an accident of the atomic-write pattern, not a
    # guarantee. One writer using a plain write_text() would go right through.
    import os
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = tmp / "config.yaml"
        cfg.write_text("screening:\n  min_rr: 2.0\n")
        link = tmp / "corpus.json"
        os.symlink(cfg, link)
        try:
            assert_writable(link)
        except PermissionError:
            assert cfg.read_text().startswith("screening:")
            return
        raise AssertionError("assert_writable allowed a symlink to config.yaml")


def test_assert_writable_allows_knowledge_files():
    for name in ("corpus.json", "knowledge_candidates.json", "knowledge_channels.json"):
        assert assert_writable(Path("/tmp") / name).name == name


def test_every_writer_refuses_at_runtime():
    # Each of these exposes a `path=` seam for testing. Passing config.yaml
    # through any of them must raise, not write.
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "config.yaml"
        for label, call in (
            ("store", lambda: KnowledgeStore(target).save()),
            ("candidates", lambda: candidates_mod.save([], target)),
            ("channels", lambda: channels_mod.confirm("x", "y", "z", path=target)),
        ):
            try:
                call()
            except PermissionError:
                continue
            raise AssertionError(f"{label} wrote config.yaml instead of refusing")
        assert not target.exists(), "config.yaml was created despite the guard"


def test_candidates_never_claim_an_unknown_parameter():
    # A hallucinated parameter name reaching the review file would send a human
    # to edit a key that does not exist.
    for name, target in candidates_mod.PARAM_TARGETS.items():
        assert target.name == name
        assert target.lo < target.hi


def test_param_table_matches_live_dataclass_defaults():
    # Drift guard: change ScreenConfig without updating PARAM_TARGETS and this
    # fails, rather than the review file quietly citing a stale "current" value.
    from bot.screening import ScreenConfig
    cfg = ScreenConfig()
    for name, target in candidates_mod.PARAM_TARGETS.items():
        if target.owner.endswith("ScreenConfig"):
            assert target.current == getattr(cfg, name), (
                f"{name}: table says {target.current}, ScreenConfig says "
                f"{getattr(cfg, name)}")


def _run_all() -> bool:
    ok = True
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                ok = False
                print(f"  FAIL {name}: {exc}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
