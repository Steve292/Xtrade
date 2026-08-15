"""End-to-end tests for the knowledge pipeline. No network, no yt-dlp, no model.

Run directly (`python tests/test_knowledge_pipeline.py`) or under pytest.

FakeRunner writes files rather than only returning stdout, because yt-dlp's
primary side effect IS files on disk -- a fake that only fed back stdout would
leave the caption-discovery code completely unexercised.

The test that matters most is test_unconfirmed_channel_is_never_touched: it
asserts the runner was called zero times. The confirmation gate is the only
thing standing between this pipeline and ingesting an arbitrary channel, and a
gate that is merely "usually checked" is not a gate.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.knowledge import candidates as candidates_mod
from bot.knowledge import channels as channels_mod
from bot.knowledge import pipeline
from bot.knowledge.config import KnowledgeConfig
from bot.knowledge.store import KnowledgeStore
from bot.knowledge.ytdlp import RunResult

VTT = """WEBVTT

00:00:01.000 --> 00:00:06.000
never take a setup under 1:3 risk reward

00:00:06.000 --> 00:00:12.000
never take a setup under 1:3 risk reward and wait for the candle close

00:00:12.000 --> 00:00:20.000
above the order block after a liquidity sweep
"""


class FakeRunner:
    """Stands in for yt-dlp: records argv, and writes the files it would write."""

    def __init__(self, video_ids, duration=600.0):
        self.video_ids = list(video_ids)
        self.duration = duration
        self.calls = []

    def __call__(self, argv, timeout=120.0, cwd=None):
        self.calls.append(argv)
        if "--flat-playlist" in argv:
            entries = [{"id": v, "title": f"video {v}", "duration": self.duration,
                        "upload_date": "20260101", "channel_id": "UCtest",
                        "channel": "Test Channel", "view_count": 100,
                        "channel_url": "https://www.youtube.com/channel/UCtest"}
                       for v in self.video_ids]
            return RunResult(0, json.dumps({"entries": entries}), "")
        if "--write-subs" in argv:
            out = Path(argv[argv.index("-o") + 1]).parent
            out.mkdir(parents=True, exist_ok=True)
            vid = argv[-1].rsplit("=", 1)[-1]
            (out / f"{vid}.info.json").write_text(json.dumps(
                {"id": vid, "automatic_captions": {"en": [{}]}}))
            (out / f"{vid}.en.vtt").write_text(VTT)
            return RunResult(0, "", "")
        return RunResult(1, "", "unexpected call")


def _cfg(tmp: Path) -> KnowledgeConfig:
    return KnowledgeConfig.from_dict({
        "data_dir": str(tmp / "knowledge"),
        "min_video_seconds": 120,
        "min_support_videos": 1,
        "llm": {"enabled": False},
    })


def _confirmed(tmp: Path) -> Path:
    path = tmp / "channels.json"
    channels_mod.confirm("UCtest", "Test Channel",
                         "https://www.youtube.com/channel/UCtest", path=path)
    return path


def test_unconfirmed_channel_is_never_touched():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = _cfg(tmp)
        runner = FakeRunner(["v1"])
        report = pipeline.ingest(cfg, KnowledgeStore(tmp / "corpus.json"),
                                 "yt-dlp", channels_path=tmp / "none.json",
                                 runner=runner, log=lambda *_: None)
        assert runner.calls == [], "ingest reached the network with nothing confirmed"
        assert report.ingested == 0


def test_ingest_stores_documents_and_concepts():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        report = pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                                 runner=FakeRunner(["v1", "v2"]),
                                 log=lambda *_: None)
        assert report.ingested == 2, report.summary()
        docs = store.load().documents()
        assert len(docs) == 2 and all(d.ok for d in docs)
        keys = {c.key for c in docs[0].concepts}
        assert "risk_reward" in keys and "order_block" in keys
        assert "candle_close" in keys, "candle concepts must be detected"


def test_rerun_is_idempotent_and_downloads_nothing():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                        runner=FakeRunner(["v1"]), log=lambda *_: None)
        runner2 = FakeRunner(["v1"])
        report = pipeline.ingest(cfg, KnowledgeStore(tmp / "corpus.json"), "yt-dlp",
                                 channels_path=chans, runner=runner2,
                                 log=lambda *_: None)
        assert report.ingested == 0 and report.skipped_fresh == 1
        # Only the cheap channel listing; zero caption fetches.
        assert not any("--write-subs" in c for c in runner2.calls)


def test_shorts_are_skipped_before_any_download():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        runner = FakeRunner(["short1"], duration=45.0)
        report = pipeline.ingest(cfg, KnowledgeStore(tmp / "corpus.json"), "yt-dlp",
                                 channels_path=chans, runner=runner,
                                 log=lambda *_: None)
        assert report.skipped_short == 1 and report.ingested == 0
        assert not any("--write-subs" in c for c in runner.calls)


def test_dry_run_downloads_nothing():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        runner = FakeRunner(["v1"])
        report = pipeline.ingest(cfg, KnowledgeStore(tmp / "corpus.json"), "yt-dlp",
                                 channels_path=chans, runner=runner,
                                 dry_run=True, log=lambda *_: None)
        assert report.ingested == 0
        assert not any("--write-subs" in c for c in runner.calls)


def test_candidates_are_cited_and_map_to_real_parameters():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                        runner=FakeRunner(["v1", "v2"]), log=lambda *_: None)
        cands = pipeline.rebuild_candidates(store, path=tmp / "cands.json")
        assert cands, "no candidates built"
        for c in cands:
            assert c.citations, f"{c.id} has no citation"
            for cite in c.citations:
                assert cite.url.startswith("https://www.youtube.com/watch?v=")
                assert "&t=" in cite.url
            assert c.param is None or c.param in candidates_mod.PARAM_TARGETS
        # "1:3" -> min_rr 3.0 is the concrete one this fixture should yield.
        rr = [c for c in cands if c.param == "min_rr"]
        assert rr and rr[0].proposed_value == 3.0, [c.statement for c in cands]


def test_candle_concepts_surface_as_unmapped_gaps():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                        runner=FakeRunner(["v1"]), log=lambda *_: None)
        cands = pipeline.rebuild_candidates(store, path=tmp / "cands.json")
        candle = [c for c in cands if c.concept_key == "candle_close"]
        assert candle, "candle_close produced no candidate"
        assert candle[0].param is None, "nothing in this repo implements candles"


def test_human_decisions_survive_a_rebuild():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        path = tmp / "cands.json"
        pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                        runner=FakeRunner(["v1"]), log=lambda *_: None)
        first = pipeline.rebuild_candidates(store, path=path)
        target = first[0].id
        candidates_mod.set_status(target, "rejected", "not for my account", path=path)
        again = pipeline.rebuild_candidates(store, path=path)
        kept = [c for c in again if c.id == target][0]
        # A rejected candidate reappearing as "new" after every re-ingest would
        # make the review queue useless.
        assert kept.status == "rejected" and kept.reviewer_note == "not for my account"


def test_a_full_run_leaves_config_yaml_untouched():
    # The boundary is enforced structurally in tests/test_knowledge_boundary.py
    # (AST + runtime guards). This is the behavioural half: run the real thing
    # end to end and assert the file is byte-identical afterwards.
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    before = cfg_path.read_bytes()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg, chans = _cfg(tmp), _confirmed(tmp)
        store = KnowledgeStore(tmp / "corpus.json")
        pipeline.ingest(cfg, store, "yt-dlp", channels_path=chans,
                        runner=FakeRunner(["v1", "v2"]), log=lambda *_: None)
        pipeline.rebuild_candidates(store, path=tmp / "cands.json")
    assert cfg_path.read_bytes() == before, "a full ingest modified config.yaml"


def test_corrupt_corpus_fails_safe():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "corpus.json"
        path.write_text("not json{{{")
        assert KnowledgeStore(path).load().documents() == []


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
