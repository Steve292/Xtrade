"""Optional ASR fallback for videos that have no captions at all.

Every heavy import is inside a function. This module imports cleanly on a
machine with neither faster-whisper nor openai-whisper installed, which is what
keeps `pip install -r requirements.txt` for the trading bot free of a 2GB torch
dependency nobody asked for.

Backends, in preference order:
  faster_whisper  CPU-friendly, no torch
  whisper         official, drags torch in
  cli             the `whisper` binary, driven through the same injectable
                  runner= seam as yt-dlp -- which is also what makes this path
                  testable without installing anything at all

Returns None on every failure, never raises. A missing backend is recorded on
the Document as transcript_status="whisper_unavailable" and the run continues;
losing one uncaptioned video must not abort an ingest of fifty.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

from . import ytdlp
from .store import Segment


def available(preferred: str = "auto") -> Optional[str]:
    """Name of the backend that would be used, or None."""
    import importlib.util
    import shutil

    if preferred in ("auto", "faster_whisper"):
        if importlib.util.find_spec("faster_whisper"):
            return "faster_whisper"
        if preferred == "faster_whisper":
            return None
    if preferred in ("auto", "whisper"):
        if importlib.util.find_spec("whisper"):
            return "whisper"
        if preferred == "whisper":
            return None
    if preferred in ("auto", "cli"):
        if shutil.which("whisper"):
            return "cli"
    return None


def unavailable_reason(preferred: str = "auto") -> str:
    return ("No Whisper backend found. Install one into this venv:\n"
            "    pip install faster-whisper        (recommended — no torch)\n"
            "    pip install openai-whisper        (official — pulls ~2GB of torch)\n"
            f"  or set knowledge.whisper.enabled: false to stop trying "
            f"(preferred backend was {preferred!r}).")


def transcribe(audio_path: Path, model: str = "base", backend: str = "auto",
               runner: ytdlp.Runner = ytdlp.default_runner,
               timeout: float = 3600.0) -> Optional[List[Segment]]:
    chosen = available(backend)
    if not chosen:
        return None
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return None
    try:
        if chosen == "faster_whisper":
            from faster_whisper import WhisperModel
            segs, _info = WhisperModel(model, device="cpu",
                                       compute_type="int8").transcribe(str(audio_path))
            return [Segment(float(s.start), float(s.end), s.text.strip())
                    for s in segs if s.text and s.text.strip()]
        if chosen == "whisper":
            import whisper as _w
            result = _w.load_model(model).transcribe(str(audio_path))
            return [Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip())
                    for s in result.get("segments", []) if str(s.get("text", "")).strip()]
        # cli: emit VTT and reuse our own parser, so one code path handles the
        # exact rolling/formatting quirks we already understand.
        from .transcripts import parse_vtt
        with tempfile.TemporaryDirectory() as tmp:
            res = runner(["whisper", str(audio_path), "--model", model,
                          "--output_format", "vtt", "--output_dir", tmp],
                         timeout=timeout)
            if not res.ok:
                return None
            vtt = next(iter(sorted(Path(tmp).glob("*.vtt"))), None)
            if vtt is None:
                return None
            return parse_vtt(vtt.read_text())
    except Exception:
        # Deliberately broad: every backend has its own failure taxonomy
        # (model download errors, ffmpeg errors, OOM), and none of them is
        # worth aborting a fifty-video ingest over.
        return None
