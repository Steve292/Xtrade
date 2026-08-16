"""The `knowledge:` section of config.yaml.

Follows bot/screening.py::ScreenConfig.from_dict exactly -- filter the incoming
dict down to known dataclass fields, let dataclass defaults cover the rest.
This is the repo's only structured-config idiom and there is no schema
validation anywhere, so unknown keys are silently ignored here just as they are
there. tests/test_knowledge_config.py parses the real config.yaml block to turn
that hazard into something at least one test notices.

Extended once beyond ScreenConfig: two nested subsections. Whisper and ollama
are genuinely independent optional subsystems -- either can be absent without
the other caring -- and flattening them into `whisper_enabled`,
`whisper_model`, `llm_enabled`... would read worse than it reads in YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WhisperConfig:
    """Fallback ASR for videos with no captions at all.

    Disabled by default. A caption fetch is seconds per video; Whisper is
    minutes. max_videos_per_run exists so that turning this on cannot silently
    convert a routine ingest into an overnight job -- full coverage of an
    uncaptioned backlog is reached across several runs, by design.
    """

    enabled: bool = False
    model: str = "base"
    backend: str = "auto"          # auto | faster_whisper | whisper | cli
    keep_audio: bool = False
    max_videos_per_run: int = 5

    @classmethod
    def from_dict(cls, d: dict) -> "WhisperConfig":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class LLMConfig:
    """Optional local-model pass over segments that already scored a concept.

    Talks HTTP to ollama rather than shelling out to the `ollama` binary -- the
    opposite call from yt-dlp, deliberately. `requests` is already a dependency
    here, the HTTP API returns structured JSON with a real timeout and a
    json-mode flag, and it reuses the injectable-callable test seam this repo
    already uses in bot/marketdata.py. For yt-dlp the CLI *is* the stable
    interface and the library is not importable from this venv; different
    constraints, different answer.
    """

    enabled: bool = True
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:14b"
    timeout_sec: float = 120.0
    max_segments_per_video: int = 40

    @classmethod
    def from_dict(cls, d: dict) -> "LLMConfig":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    # Absolute path to a yt-dlp binary. Blank means "search PATH". It is a
    # config value rather than a dependency because the binary is updated far
    # more often than this repo is, and on this machine the only copy lives in
    # an unrelated project's venv under a different Python.
    ytdlp_path: str = ""
    data_dir: str = "knowledge"
    search_limit: int = 25
    max_videos_per_channel: int = 50
    min_video_seconds: int = 120       # skip Shorts; nobody teaches a rule in 45s
    max_video_seconds: int = 10800
    segment_seconds: float = 30.0
    segment_max_chars: int = 600
    min_support_videos: int = 2        # one throwaway sentence is not a finding
    # Stop after this many consecutive video failures. A long streak is one
    # systemic cause, not bad luck, and continuing worsens a rate-limit.
    max_consecutive_failures: int = 10
    request_timeout_sec: float = 120.0
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "KnowledgeConfig":
        d = d or {}
        nested = ("whisper", "llm")
        scalars = {k: d[k] for k in cls.__dataclass_fields__
                   if k in d and k not in nested}
        return cls(
            whisper=WhisperConfig.from_dict(d.get("whisper") or {}),
            llm=LLMConfig.from_dict(d.get("llm") or {}),
            **scalars,
        )
