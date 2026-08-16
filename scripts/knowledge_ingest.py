#!/usr/bin/env python3
"""Trading-knowledge ingestion — transcripts in, reviewable rule candidates out.

    # 1. find and confirm a source (nothing ingests until you do)
    python scripts/knowledge_ingest.py channels search "the composite trader"
    python scripts/knowledge_ingest.py channels confirm --channel-id UC... \\
        --name "The Composite Trader" --url https://www.youtube.com/@LarsKooistra_
    python scripts/knowledge_ingest.py channels list

    # 2. ingest (incremental — re-running fetches only what is new)
    python scripts/knowledge_ingest.py ingest --dry-run
    python scripts/knowledge_ingest.py ingest --limit 3

    # 3. review. THIS NEVER WRITES config.yaml.
    python scripts/knowledge_ingest.py review
    python scripts/knowledge_ingest.py show <id>
    python scripts/knowledge_ingest.py accept <id> --note "matches my own testing"

    # rebuild candidates from the stored corpus, no network
    python scripts/knowledge_ingest.py reextract

This tool reports. It does not tune. `accept` records your decision and prints
the config.yaml edit for you to make by hand — the same boundary
bot/capital_guard.py draws for profit-lock and bot/entry_rules.py draws by
staying out of the live execute path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from dotenv import load_dotenv

from bot.knowledge import candidates as candidates_mod
from bot.knowledge import channels as channels_mod
from bot.knowledge import pipeline, ytdlp
from bot.knowledge.config import KnowledgeConfig
from bot.knowledge.store import KnowledgeStore


def _load_cfg(path: str) -> KnowledgeConfig:
    try:
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
    except OSError:
        raw = {}
    return KnowledgeConfig.from_dict(raw.get("knowledge"))


def _store(cfg: KnowledgeConfig) -> KnowledgeStore:
    return KnowledgeStore(Path(cfg.data_dir) / "corpus.json")


def _ytdlp(args, cfg: KnowledgeConfig) -> str:
    return ytdlp.require_ytdlp_path(getattr(args, "ytdlp", None) or None,
                                    cfg.ytdlp_path or None)


def cmd_channels(args, cfg: KnowledgeConfig) -> int:
    path = Path(args.channels_file)
    if args.action == "list":
        rows = channels_mod.list_confirmed(path)
        if not rows:
            print("No channels confirmed. Nothing can be ingested yet.")
            return 0
        for c in rows:
            flag = "" if c.enabled else "  (disabled)"
            print(f"  {c.channel_id}  {c.channel_name}{flag}\n      {c.channel_url}")
        return 0

    if args.action == "playlists":
        url = args.url or (channels_mod.list_confirmed(path)[0].channel_url
                           if channels_mod.list_confirmed(path) else "")
        if not url:
            print("Pass --url <channel> (or confirm a channel first).")
            return 2
        pls = ytdlp.list_playlists(url, 100, _ytdlp(args, cfg),
                                   timeout=cfg.request_timeout_sec)
        if not pls:
            print(f"No playlists found on {url}")
            return 0
        print(f"Playlists on {url}:\n")
        for pl in pls:
            n = pl.get("playlist_count") or pl.get("video_count") or "?"
            print(f"  {str(n):>4} videos  {pl.get('title','')[:60]}")
            print(f"              {pl.get('url') or pl.get('webpage_url','')}")
        print("\n  A playlist can be confirmed as its own source:")
        print("    knowledge_ingest.py channels confirm --channel-id <id> "
              "--name <name> --url <playlist-url>")
        return 0

    if args.action == "search":
        matches = channels_mod.search_channels(
            args.query, cfg.search_limit, _ytdlp(args, cfg),
            timeout=cfg.request_timeout_sec)
        print(channels_mod.format_matches(args.query, matches))
        print("\n  To confirm one:\n    python scripts/knowledge_ingest.py channels "
              "confirm --channel-id <id> --name <name> --url <url>")
        return 0

    if args.action == "confirm":
        if not args.channel_id or not args.url:
            print("confirm needs --channel-id and --url (a name is optional but kind).")
            return 2
        rec = channels_mod.confirm(args.channel_id, args.name or args.channel_id,
                                   args.url, query=args.query or "", path=path)
        print(f"Confirmed {rec.channel_name} ({rec.channel_id}).")
        print(f"Recorded in {path}. Ingestion will now include it.")
        return 0

    if args.action in ("disable", "enable"):
        ok = channels_mod.set_enabled(args.channel_id, args.action == "enable", path)
        print("updated" if ok else "no such channel")
        return 0 if ok else 1

    if args.action == "remove":
        ok = channels_mod.revoke(args.channel_id, path)
        print("removed" if ok else "no such channel")
        return 0 if ok else 1
    return 2


def cmd_ingest(args, cfg: KnowledgeConfig) -> int:
    store = _store(cfg)
    report = pipeline.ingest(
        cfg, store, _ytdlp(args, cfg), channels_path=Path(args.channels_file),
        limit=args.limit, only_channel=args.channel, dry_run=args.dry_run,
        force=args.force)
    print("\n" + report.summary())
    if report.failed:
        print("  failed: " + ", ".join(report.failed[:10]))
    if not args.dry_run:
        merged = pipeline.rebuild_candidates(store)
        listed = [c for c in merged
                  if not c.stale and c.support_videos >= cfg.min_support_videos]
        print(f"  candidates: {len(listed)} above the {cfg.min_support_videos}-video "
              f"evidence floor ({len(merged)} total)")
    return 0


def cmd_reextract(args, cfg: KnowledgeConfig) -> int:
    store = _store(cfg)
    # Concepts first, then candidates. Candidates are derived FROM concepts, so
    # rebuilding them against a stale concept snapshot would silently produce
    # the old answer and look like it worked.
    pipeline.refresh_concepts(store)
    merged = pipeline.rebuild_candidates(store)
    print(f"Rebuilt {len(merged)} candidates from the stored corpus (no network).")
    return 0


def cmd_verify(args, cfg: KnowledgeConfig) -> int:
    """Backtest the optional gates against real history.

    The answer to "is this actually true", as opposed to "is this widely
    taught", which is all the rest of this tool can tell you.
    """
    import pandas as pd
    from bot.backtest.engine import resample_htf
    from bot.knowledge import verify as verify_mod

    try:
        df = pd.read_pickle(args.history)
    except Exception as exc:
        print(f"Could not read history from {args.history}: {exc}", file=sys.stderr)
        print("Expected a pickled OHLCV DataFrame "
              "(timestamp/open/high/low/close/volume).", file=sys.stderr)
        return 2
    htf = resample_htf(df, args.htf)
    print(f"{len(df)} bars  {df.timestamp.min()} -> {df.timestamp.max()}\n")

    gates = ([args.gate] if args.gate else
             ["require_mitigation", "require_breaker",
              "require_candle_confirmation", "require_wyckoff",
              "require_value_area_edge"])
    strat = {"stop_loss_pct": args.stop_loss_pct} if args.stop_loss_pct else {}
    out = []
    for g in gates:
        try:
            c = verify_mod.compare_screen_gate(df, htf, g, strategy_kwargs=strat)
            out.append(c)
            print(c.report() + "\n")
        except Exception as exc:
            print(f"  gate {g}: FAILED {type(exc).__name__}: {exc}\n")
    print("=" * 66)
    print(verify_mod.format_summary(out))
    return 0


def cmd_review(args, cfg: KnowledgeConfig) -> int:
    cands = candidates_mod.load()
    if not cands:
        print("No candidates yet — run `ingest` first.")
        return 0
    rows = [c for c in cands if args.all or not c.stale]
    if not args.all:
        rows = [c for c in rows if c.support_videos >= cfg.min_support_videos]
    if args.status:
        rows = [c for c in rows if c.status == args.status]
    if args.concept:
        rows = [c for c in rows if c.concept_key == args.concept]
    if args.unmapped:
        # "No code implements this" (taxonomy maps_to is None), NOT "no tunable
        # knob" (candidate.param is None). Filtering on param listed
        # order_block, liquidity_pool, fvg and bos as gaps -- all of which
        # bot/smc/ implements fully; they simply have no threshold worth
        # proposing from a transcript. Conflating the two buried the handful of
        # concepts that genuinely have nothing behind them under twenty that do.
        from bot.knowledge import taxonomy
        rows = [c for c in rows
                if (taxonomy.BY_KEY.get(c.concept_key) is None
                    or taxonomy.BY_KEY[c.concept_key].maps_to is None)]
    print(candidates_mod.format_table(rows, limit=args.limit))
    print(f"\n  {len(rows)} shown. Nothing here has changed config.yaml.")
    return 0


def cmd_show(args, cfg: KnowledgeConfig) -> int:
    for c in candidates_mod.load():
        if c.id == args.id:
            print(candidates_mod.format_detail(c))
            print("\n" + candidates_mod.format_edit(c))
            return 0
    print(f"no candidate {args.id}")
    return 1


def cmd_decide(args, cfg: KnowledgeConfig) -> int:
    status = {"accept": "accepted", "reject": "rejected", "defer": "deferred"}[args.cmd]
    hit = candidates_mod.set_status(args.id, status, args.note or "")
    if hit is None:
        print(f"no candidate {args.id}")
        return 1
    print(f"{status.upper()} (recorded in {candidates_mod.DEFAULT_PATH} — "
          f"nothing else changed).")
    if status == "accepted":
        print("\n" + candidates_mod.format_edit(hit))
    return 0


def cmd_stats(args, cfg: KnowledgeConfig) -> int:
    store = _store(cfg)
    store.load()
    print("  corpus:", store.stats())
    counts = store.concept_counts()
    if counts:
        print("\n  concepts by mentions:")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {k:20s} {v}")
    from bot.knowledge import taxonomy
    gaps = [k for k in taxonomy.unmapped_keys() if k in counts]
    if gaps:
        print("\n  MENTIONED BUT NOT IMPLEMENTED anywhere in this repo:")
        for k in gaps:
            print(f"    {k:20s} {counts[k]} mentions")
        print("  These are feature gaps, not tuning changes.")
    return 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--channels-file", default=str(channels_mod.DEFAULT_PATH))
    p.add_argument("--ytdlp", default="", help="path to the yt-dlp binary")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("channels")
    c.add_argument("action", choices=["search", "confirm", "list", "disable",
                                      "enable", "remove", "playlists"])
    c.add_argument("query", nargs="?", default="")
    c.add_argument("--channel-id", default="")
    c.add_argument("--name", default="")
    c.add_argument("--url", default="")

    i = sub.add_parser("ingest")
    i.add_argument("--limit", type=int, default=None)
    i.add_argument("--channel", default=None)
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--force", action="store_true")

    sub.add_parser("reextract")

    r = sub.add_parser("review")
    r.add_argument("--status", default="")
    r.add_argument("--concept", default="")
    r.add_argument("--limit", type=int, default=30)
    r.add_argument("--all", action="store_true")
    r.add_argument("--unmapped", action="store_true",
                   help="only candidates with no code behind them (feature gaps)")

    s = sub.add_parser("show")
    s.add_argument("id")

    for name in ("accept", "reject", "defer"):
        d = sub.add_parser(name)
        d.add_argument("id")
        d.add_argument("--note", default="")

    v = sub.add_parser("verify")
    v.add_argument("--history", required=True,
                   help="pickled OHLCV DataFrame to backtest against")
    v.add_argument("--htf", default="1h")
    v.add_argument("--gate", default="", help="one ScreenConfig flag, or all")
    v.add_argument("--stop-loss-pct", type=float, default=None)

    sub.add_parser("stats")

    args = p.parse_args()
    cfg = _load_cfg(args.config)

    try:
        if args.cmd == "channels":
            return cmd_channels(args, cfg)
        if args.cmd == "ingest":
            return cmd_ingest(args, cfg)
        if args.cmd == "reextract":
            return cmd_reextract(args, cfg)
        if args.cmd == "review":
            return cmd_review(args, cfg)
        if args.cmd == "show":
            return cmd_show(args, cfg)
        if args.cmd in ("accept", "reject", "defer"):
            return cmd_decide(args, cfg)
        if args.cmd == "verify":
            return cmd_verify(args, cfg)
        if args.cmd == "stats":
            return cmd_stats(args, cfg)
    except ytdlp.YtDlpMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
