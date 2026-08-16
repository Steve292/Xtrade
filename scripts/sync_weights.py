#!/usr/bin/env python
"""Propagate a grown corpus into bot/smc/consensus.py WEIGHTS, gated on tests.

    python scripts/sync_weights.py --check     # report drift, change nothing
    python scripts/sync_weights.py --apply     # rewrite WEIGHTS if drifted
    python scripts/sync_weights.py --apply --commit

This is the path by which new knowledge reaches the bot. Ingesting transcripts
alone changes nothing that runs -- the corpus is gitignored and nothing on the
execute path reads it. The only thing that crosses over is the derived weights,
so that crossing is what gets automated, and it gets automated with a gate.

THE GATE IS THE POINT
---------------------
A weight change alters bot/screening.py's consensus vote, which gates orders on
a live account. So --apply will not commit unless:

  1. the recomputed weights actually differ by more than --threshold (0.02 by
     default -- below that it is corpus noise, not a finding), and
  2. the test suite still passes afterwards.

If the tests fail the edit is REVERTED and the failure is reported. A corpus
that grows into a state the tests reject is a signal to look, not to ship.

It never pushes and never merges. It commits to the current branch and stops --
promoting that to main stays a human decision, which is the same boundary
docs/KNOWLEDGE.md draws around every other number this pipeline produces.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSENSUS = ROOT / "bot/smc/consensus.py"
TESTS = ("tests/test_smc_consensus.py", "tests/test_trade_review.py",
         "tests/test_screening_optional_gates.py", "tests/test_knowledge_boundary.py")


def current_weights() -> dict:
    sys.path.insert(0, str(ROOT))
    from bot.smc import consensus
    return dict(consensus.WEIGHTS)


def rewrite(weights: dict) -> bool:
    """Replace the numeric values inside the WEIGHTS dict, in place.

    Only the `"key": 0.00,` value is touched; the surrounding commentary --
    which carries the history of how each weight moved and why -- is preserved,
    because that record is the most useful part of the block.
    """
    src = CONSENSUS.read_text()
    start = src.index("WEIGHTS: dict = {")
    end = src.index("}", start)
    block = src[start:end]

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in weights:
            return m.group(0)
        return f'"{key}": {weights[key]:.2f},'

    new_block = re.sub(r'"([a-z_]+)":\s*[0-9.]+,', sub, block)
    if new_block == block:
        return False
    CONSENSUS.write_text(src[:start] + new_block + src[end:])
    return True


def run_tests(python: str) -> tuple[bool, str]:
    out = []
    ok = True
    for t in TESTS:
        p = ROOT / t
        if not p.exists():
            continue
        r = subprocess.run([python, str(p)], cwd=ROOT,
                           capture_output=True, text=True, timeout=900)
        passed = r.returncode == 0
        ok = ok and passed
        out.append(f"  {'ok  ' if passed else 'FAIL'} {t}")
        if not passed:
            out.append((r.stdout or r.stderr or "")[-400:])
    return ok, "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default="knowledge/corpus.json")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.02,
                    help="ignore drift smaller than this (default 0.02)")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    from derive_weights import derive           # same formula, one definition

    corpus = Path(a.corpus)
    if not corpus.is_absolute():
        corpus = ROOT / corpus
    if not corpus.exists():
        print(f"no corpus at {corpus}", file=sys.stderr)
        return 2

    raw, n_docs, n_chan = derive(corpus)
    new = {k: v["weight"] for k, v in raw.items()}
    old = current_weights()

    drift = {k: (old.get(k), v) for k, v in new.items()
             if old.get(k) is None or abs(v - old[k]) >= a.threshold}

    print(f"corpus: {n_docs} documents, {n_chan} channels")
    if not drift:
        print(f"no weight moved by >= {a.threshold}. Nothing to do.")
        return 0

    print(f"\n{len(drift)} weight(s) drifted >= {a.threshold}:")
    for k, (o, v) in sorted(drift.items()):
        print(f"  {k:<20} {o if o is not None else '--':>6} -> {v:.2f}")

    if a.check or not a.apply:
        print("\n(check only — pass --apply to write)")
        return 0

    if not rewrite(new):
        print("nothing rewritten (values already match)")
        return 0
    print(f"\nrewrote WEIGHTS in {CONSENSUS.relative_to(ROOT)}")

    ok, report = run_tests(a.python)
    print("\ntests:")
    print(report)
    if not ok:
        subprocess.run(["git", "checkout", "--", str(CONSENSUS)], cwd=ROOT)
        print("\nTESTS FAILED — WEIGHTS reverted, nothing committed.")
        print("A corpus that grows into a state the tests reject is a signal "
              "to look at, not to ship.")
        return 1

    if not a.commit:
        print("\ntests pass. Not committing (pass --commit).")
        return 0

    lines = "\n".join(f"    {k}: {o} -> {v:.2f}" for k, (o, v) in sorted(drift.items()))
    msg = (f"Re-derive consensus weights from the grown corpus\n\n"
           f"Corpus is now {n_docs} documents across {n_chan} channels.\n"
           f"Weights that moved by >= {a.threshold}:\n\n{lines}\n\n"
           f"Derived by scripts/derive_weights.py using the formula documented\n"
           f"in consensus.py -- channel breadth x document share, normalised.\n"
           f"Applied by scripts/sync_weights.py, which reverts on any test\n"
           f"failure and never pushes or merges.\n\n"
           f"Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n")
    subprocess.run(["git", "add", str(CONSENSUS)], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True)
    print("\ncommitted to the current branch (not pushed, not merged).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
