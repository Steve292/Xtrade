#!/usr/bin/env python
"""Discover trading-education channels at scale and rank them by REACH.

    python scripts/discover_channels.py --limit 100
    python scripts/discover_channels.py --limit 100 --confirm   # writes the roster

WHAT "RANK" MEANS HERE, AND WHAT IT DOES NOT
--------------------------------------------
This ranks by reach: how many distinct topic queries a channel surfaces for,
and the view counts of the videos it surfaces with.

It does NOT rank by trading success rate, and no honest tool can. YouTube
trading educators do not publish audited track records; the ones who advertise
win rates are unverifiable and usually selling something. There is no dataset
of "best traders by success rate" to sort against, so any such ordering would
be invented.

That distinction is not pedantry here -- it is the project's central finding.
docs/KNOWLEDGE.md has always said ranking measures how widely something is
TAUGHT, never whether it works. scripts/gate_split.py then measured it: the
gates drawn from the MOST widely taught concepts performed WORST (PF 0.964 vs
1.093 for the least widely taught). Reach was inversely related to edge.

So treat this ordering as "who is loud", which is a sampling strategy, never as
"who is right". Breadth of sources is still worth having -- it is what stopped
Wyckoff being a single-channel artifact -- but breadth is a defence against
sampling bias, not evidence of profitability.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Spread across method families so the roster is not one school of thought
# talking to itself -- the homogeneity problem that made the first four
# channels' agreement look like consensus when it was selection.
QUERIES = [
    "smart money concepts trading course",
    "inner circle trader ICT concepts",
    "price action trading strategy course",
    "wyckoff method accumulation distribution",
    "volume profile trading strategy",
    "order flow footprint trading",
    "supply and demand zones trading",
    "liquidity sweep stop hunt trading",
    "fair value gap imbalance trading",
    "order block trading strategy",
    "market structure break of structure trading",
    "risk management position sizing trading",
    "trading psychology discipline",
    "candlestick patterns price action",
    "fibonacci retracement trading strategy",
    "swing trading strategy education",
    "day trading strategy beginners course",
    "forex trading full course",
    "crypto trading technical analysis course",
    "backtesting trading strategy statistics",
]


def search(query: str, ytdlp: str, limit: int, timeout: float) -> list[dict]:
    from bot.knowledge import channels as ch
    try:
        return ch.search_channels(query, limit, ytdlp, timeout=timeout)
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! {query[:40]}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def main() -> int:
    import yaml
    from bot.knowledge import channels as ch
    from bot.knowledge.config import KnowledgeConfig

    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=100, help="how many to report")
    ap.add_argument("--confirm", action="store_true",
                    help="add the discovered channels to the confirmed roster")
    ap.add_argument("--queries", type=int, default=len(QUERIES),
                    help="use only the first N queries (fewer = gentler on the source)")
    a = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    cfg = KnowledgeConfig.from_dict(
        (yaml.safe_load((root / "config.yaml").read_text()) or {}).get("knowledge"))
    ytdlp = cfg.ytdlp_path or "yt-dlp"

    existing = {c.channel_id for c in ch.list_confirmed(ch.DEFAULT_PATH)}
    print(f"already confirmed: {len(existing)}\n")

    agg: dict[str, dict] = defaultdict(
        lambda: {"queries": 0, "hits": 0, "views": 0, "name": "", "url": ""})
    for i, q in enumerate(QUERIES[:a.queries], 1):
        rows = search(q, ytdlp, cfg.search_limit, cfg.request_timeout_sec)
        print(f"[{i}/{a.queries}] {q[:48]:<50} {len(rows)} channels")
        # search_channels returns ChannelMatch objects, not dicts.
        for r in rows:
            cid = r.channel_id
            if not cid:
                continue
            e = agg[cid]
            e["queries"] += 1
            e["hits"] += int(r.video_count or 0)
            e["views"] += int(r.total_views or 0)
            e["name"] = e["name"] or (r.channel_name or cid)
            e["url"] = e["url"] or (r.channel_url or "")

    # Reach score: topic breadth first (a channel surfacing for many distinct
    # queries is a general educator, not a one-video fluke), views as tiebreak.
    ranked = sorted(agg.items(),
                    key=lambda kv: (kv[1]["queries"], kv[1]["hits"], kv[1]["views"]),
                    reverse=True)
    new = [(cid, e) for cid, e in ranked if cid not in existing]

    print(f"\n{'#':>3}  {'topics':>6}{'hits':>6}{'views':>14}  channel")
    print("-" * 78)
    for i, (cid, e) in enumerate(new[:a.limit], 1):
        print(f"{i:>3}  {e['queries']:>6}{e['hits']:>6}{e['views']:>14,}  {e['name'][:40]}")

    if a.confirm:
        added = 0
        for cid, e in new[:a.limit]:
            if not e["url"]:
                continue
            ch.confirm(cid, e["name"] or cid, e["url"],
                       query="discover_channels (ranked by reach, NOT success rate)",
                       path=ch.DEFAULT_PATH)
            added += 1
        print(f"\nconfirmed {added} new channels "
              f"(total {len(ch.list_confirmed(ch.DEFAULT_PATH))})")
        print("Ingest is incremental and rate-limited -- scripts/knowledge_daemon.py "
              "will work through them with backoff.")
    else:
        print("\n(dry run — pass --confirm to add these to the roster)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
