#!/usr/bin/env python
"""Re-derive bot/smc/consensus.py WEIGHTS from the ingested corpus.

    python scripts/derive_weights.py            # print the table
    python scripts/derive_weights.py --diff     # compare against the live values

The formula is the one documented in consensus.py and is NOT a free parameter:

    weight = (channels discussing the concept / total channels)
             x (share of documents that mention it)

normalised so the top concept is 1.0. Channel breadth is half the formula on
purpose -- a concept every video of one channel repeats is measuring that
channel, not the field. Wyckoff was the case that proved it: 61 of 62 videos
from a single source, which looked like consensus and was not.

This prints; it does not write. Editing consensus.py stays a human act, the
same boundary bot/knowledge holds -- a corpus of transcripts measures how
widely something is TAUGHT, never whether it is true, so no number derived
here should reach live code without someone deciding it should.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Concept -> the taxonomy keys that evidence it. Mostly one-to-one, but note
# premium_discount is scored separately even though taxonomy files it under
# bot.smc.structure, and volume_profile absorbs vwap.
GROUPS: dict[str, set[str]] = {
    "structure": {"bos", "choch", "swing"},
    "take_profit": {"take_profit"},
    "liquidity_sweep": {"liquidity_pool", "sweep"},
    "mitigation": {"mitigation"},
    "candle": {"candle_close", "doji", "engulfing", "inside_bar",
               "marubozu", "outside_bar", "pin_bar", "wick"},
    "premium_discount": {"premium_discount"},
    "volume_profile": {"volume_profile", "vwap"},
    "supply_demand": {"supply_demand"},
    "fibonacci": {"fib"},
    "breaker": {"breaker"},
    "wyckoff": {"wyckoff"},
}


def derive(corpus_path: Path) -> tuple[dict, int, int]:
    corpus = json.loads(corpus_path.read_text())
    docs = corpus["documents"]
    n_docs = len(docs)
    n_chan = len({d.get("channel_name") for d in docs if d.get("channel_name")})

    raw = {}
    for concept, keys in GROUPS.items():
        hits = [d for d in docs
                if {c["key"] for c in (d.get("concepts") or [])} & keys]
        chans = {d.get("channel_name") for d in hits if d.get("channel_name")}
        share = len(hits) / n_docs if n_docs else 0.0
        raw[concept] = {
            "channels": len(chans),
            "docs": len(hits),
            "share": share,
            "score": (len(chans) / n_chan if n_chan else 0.0) * share,
        }
    top = max((v["score"] for v in raw.values()), default=0.0) or 1.0
    for v in raw.values():
        v["weight"] = round(v["score"] / top, 2)
    return raw, n_docs, n_chan


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--corpus", default="knowledge/corpus.json")
    p.add_argument("--diff", action="store_true",
                   help="compare against the values currently in consensus.py")
    a = p.parse_args()

    path = Path(a.corpus)
    if not path.exists():
        print(f"no corpus at {path} -- run knowledge_ingest.py ingest first",
              file=sys.stderr)
        return 2

    raw, n_docs, n_chan = derive(path)
    print(f"corpus: {n_docs} documents, {n_chan} channels\n")

    live = {}
    if a.diff:
        from bot.smc import consensus
        live = consensus.WEIGHTS

    hdr = f"{'concept':<18}{'ch':>4}{'docs':>6}{'share':>8}{'weight':>8}"
    if a.diff:
        hdr += f"{'live':>8}{'delta':>8}"
    print(hdr)
    print("-" * len(hdr))
    for concept, v in sorted(raw.items(), key=lambda kv: -kv[1]["score"]):
        line = (f"{concept:<18}{v['channels']:>4}{v['docs']:>6}"
                f"{v['share']:>7.0%}{v['weight']:>8.2f}")
        if a.diff:
            old = live.get(concept)
            line += (f"{old:>8.2f}{v['weight'] - old:>+8.2f}"
                     if old is not None else f"{'--':>8}{'--':>8}")
        print(line)

    print("\nPaste-ready:")
    for concept, v in sorted(raw.items(), key=lambda kv: -kv[1]["score"]):
        print(f'    "{concept}": {v["weight"]:.2f},'.ljust(34)
              + f"# {v['channels']} ch, {v['docs']} docs, {v['share']:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
