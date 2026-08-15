# Trading-knowledge ingestion

Pulls transcripts from confirmed educator channels, extracts SMC / price-action
/ candlestick concepts, and produces **ranked, cited rule candidates for a human
to review**.

## Read this part first

**This does not create edge, and it cannot.**

The audit that prompted this feature (14 Aug 2026) measured the live MT5 account
over 3,547 closed trades: profit factor **1.022**, expectancy **$0.03/trade**,
max drawdown **127% of peak**, **28** consecutive losses, with the entire +$100
of profit coming from gold/silver and a single month. Hyperliquid over the same
period: 54 fills, 33% win rate, every coin negative.

A corpus of YouTube transcripts changes none of that. Ingesting video does not
alter signal logic; only a human editing a rule does. So this pipeline
**reports** and stops.

**Ranking measures how widely something is taught, not whether it is true.**
Candidates rank on distinct-video support, so a consensus view rises to the
top — and consensus SMC content is what the current 1.022-PF system was already
built from. Treat a high score as "many educators say this", never as "this
works".

## The boundary, and how it is enforced

Nothing in `bot/knowledge/` writes `config.yaml`. `accept` records your decision
and *prints* the edit; you make it. This mirrors `bot/capital_guard.py`, which
detects profit-lock and flush conditions but never executes them, and
`bot/entry_rules.py`, which stays out of the live execute path on purpose.

Enforced in three layers, not just documented:

1. **Runtime** — every writer routes through `store.assert_writable()`, which
   raises `PermissionError` on `config.yaml`, `config.yml`, `.env`. Passing one
   through any `path=` seam refuses instead of writing.
2. **Static** — `tests/test_knowledge_boundary.py` AST-walks the package: it may
   not import `yaml` at all (you cannot rewrite config without serialising it),
   may not open a forbidden filename, and may not import the live execute path.
3. **Structural** — a test asserts no module under `bot/` or the repo root
   imports `bot.knowledge`. Nothing on the path to an order can see this code,
   so no candidate can reach a trade however wrong it is.

There is deliberately **no `--apply` flag**. The moment one exists, someone runs
it, and unreviewed transcripts start moving thresholds on two armed real
accounts.

## Setup

`yt-dlp` is invoked as an external binary and never imported — the CLI is its
stable interface, and it must be updatable independently as YouTube breaks
extractors. Either:

```bash
pip install yt-dlp                       # into this repo's venv
# or point at an existing copy:
#   config.yaml -> knowledge.ytdlp_path
#   .env        -> YTDLP_PATH=/abs/path/to/yt-dlp
```

Whisper is optional and **off by default** (`knowledge.whisper.enabled`). It is
only for videos with no captions at all: captions take seconds per video,
Whisper takes minutes, and `max_videos_per_run: 5` stops a routine ingest
becoming an overnight job. Full coverage of an uncaptioned backlog is reached
across several runs, by design.

## Use

```bash
# 1. confirm a source. Nothing ingests until you do — the gate is structural,
#    ingest() reads the confirmed list and takes no channel argument at all.
python scripts/knowledge_ingest.py channels search "the composite trader"
python scripts/knowledge_ingest.py channels confirm \
    --channel-id "@LarsKooistra_" --name "The Composite Trader" \
    --url "https://www.youtube.com/@LarsKooistra_"
python scripts/knowledge_ingest.py channels list

# 2. ingest — incremental; a re-run fetches only what is new
python scripts/knowledge_ingest.py ingest --dry-run
python scripts/knowledge_ingest.py ingest --limit 3

# 3. review
python scripts/knowledge_ingest.py review
python scripts/knowledge_ingest.py review --unmapped   # feature gaps only
python scripts/knowledge_ingest.py show <id>
python scripts/knowledge_ingest.py accept <id> --note "matched my own testing"

python scripts/knowledge_ingest.py stats
python scripts/knowledge_ingest.py reextract   # rebuild candidates, no network
```

If a search is ambiguous the CLI prints `AMBIGUOUS` and refuses to guess. A
one-word name is not resolvable by better code — give it a longer phrase or an
explicit `--channel-id`.

## What it already found about this bot

`review --unmapped` is the most useful view here. The candlestick concepts in
`taxonomy.py` — engulfing, pin bar / rejection wick, doji, inside bar, marubozu,
morning-and-evening star, candle-close confirmation — **all map to nothing**. A
repo-wide search for any of them returns zero hits: `bot/indicators.py` stops at
EMA/RSI/MACD, and `bot/smc/*` reasons about swings, zones and gaps, never about
an individual candle's body-to-wick geometry.

So the seven-gate screen in `bot/screening.py` currently cannot see a rejection
wick or require a close above a level. That is a **feature gap, not a tuning
change**, and no amount of ingestion closes it — it needs code.

## How it works

1. **Channels** (`channels.py`) — search videos by name, aggregate hits by
   channel (yt-dlp has no dependable channel-search extractor), require an
   explicit confirmation persisted to `knowledge_channels.json`.
2. **Transcripts** (`transcripts.py`) — fetch captions, parse VTT, then collapse
   YouTube's **rolling auto-caption window**. Auto-captions repeat the tail of
   each cue at the head of the next; without token-level overlap removal every
   concept counts 3–4× and the ranking measures caption mechanics instead of
   content. Surviving words keep the timestamp of their *first* appearance, so
   citation links land on the moment the point was made.
3. **Extraction** (`extract.py`) — deterministic. `taxonomy.match_terms` finds
   concepts; a stdlib numeric pass reads `1:3`, `3R`, `1%`, `0.705`, `20 candles`
   while rejecting years and money. This all works with the LLM off.
4. **LLM (optional)** — a local ollama pass may propose a value for a segment
   that *already* scored a concept. It never detects concepts. Its output is
   discarded unless the parameter is on the allow-list, the value is in range,
   and the number appears verbatim in the quoted text.
5. **Candidates** (`candidates.py`) — every candidate carries ≥1 citation with a
   `&t=` deep link and the transcript source (manual captions vs ASR, because
   ASR mangles exactly these tokens). `current` values are read from the live
   dataclasses at import, so the table cannot drift from the code.

Re-runs are idempotent: videos are keyed by id, raw captions are cached so
`reextract` needs no network, the store saves after every video, and **human
decisions survive** — a rejected candidate never returns as "new".

## Legal

Transcripts of third-party videos are derived works, and bulk downloading is at
best ToS-adjacent. Everything stays local and gitignored. Keep per-channel caps
modest and personal. There is no share, publish or export-to-repo path, and none
should be added.
