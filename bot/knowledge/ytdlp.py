"""The only module in this package that shells out to yt-dlp.

Strictly transport -- it returns raw dicts and file paths and never decides
anything. Same "talk to the venue" / "decide what to do" split bot/marketdata.py
draws in its own docstring.

WHY A SUBPROCESS AND NOT A DEPENDENCY, in order of weight:

1. It is not importable from here. The only yt-dlp on this machine is a console
   shim whose shebang points at a Python 3.13 interpreter in another project's
   venv; this repo runs 3.9. `import yt_dlp` cannot reach it. A subprocess does
   not care -- the shim brings its own interpreter.
2. yt-dlp documents its Python API as unstable and its CLI as the stable
   interface.
3. YouTube breaks extractors constantly, so the binary must be updatable
   independently of this repo. A pin in requirements.txt rots in weeks.
4. Nobody installing a *trading bot* should have a YouTube downloader pulled in
   by `pip install -r requirements.txt`.

Every subprocess call goes through an injectable `runner=`, which is how the
tests drive all of this without the binary or the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

WATCH_URL = "https://www.youtube.com/watch?v={vid}"


class YtDlpMissing(RuntimeError):
    """No yt-dlp binary could be found. A setup error, not a transient one."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class CaptionResult:
    video_id: str
    info: Dict = field(default_factory=dict)
    vtt_path: Optional[Path] = None
    source: str = "none"       # captions_manual | captions_auto | none
    error: str = ""


Runner = Callable[..., RunResult]


def default_runner(argv: List[str], timeout: float = 120.0,
                   cwd: Optional[Path] = None) -> RunResult:
    """argv list only, never shell=True -- a channel name is untrusted input."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timed out after {timeout}s")
    except (OSError, ValueError) as exc:
        return RunResult(127, "", f"{type(exc).__name__}: {exc}")


def resolve_ytdlp_path(explicit: Optional[str] = None,
                       config_path: Optional[str] = None,
                       env: Optional[Dict[str, str]] = None,
                       which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """--ytdlp  >  config.yaml  >  $YTDLP_PATH  >  PATH."""
    env = os.environ if env is None else env
    for candidate in (explicit, config_path, env.get("YTDLP_PATH")):
        if candidate:
            return candidate
    return which("yt-dlp")


def require_ytdlp_path(explicit: Optional[str] = None,
                       config_path: Optional[str] = None,
                       env: Optional[Dict[str, str]] = None,
                       which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """Like resolve_ytdlp_path but raises, with the three ways to fix it.

    This deliberately breaks bot/marketdata.py's "return None on failure"
    contract. A missing binary is a setup error, not an outage, and returning
    "0 videos" would be indistinguishable from "the channel posted nothing new"
    -- which is exactly the kind of silent nothing this whole pipeline is
    supposed to avoid. Fail once, loudly, before anything is downloaded.
    """
    path = resolve_ytdlp_path(explicit, config_path, env, which)
    if not path:
        raise YtDlpMissing(
            "yt-dlp not found. Fix any one of these:\n"
            "  1. pip install yt-dlp            (into this repo's venv)\n"
            "  2. knowledge.ytdlp_path: /abs/path/to/yt-dlp   (in config.yaml)\n"
            "  3. YTDLP_PATH=/abs/path/to/yt-dlp              (in .env)"
        )
    return path


def _base_argv(ytdlp_path: str) -> List[str]:
    # No --no-warnings: stderr is how a failure explains itself, and its last
    # line is what gets recorded as the reason.
    return [ytdlp_path, "--ignore-config", "--no-progress",
            "--retries", "3", "--socket-timeout", "20", "--sleep-requests", "1"]


def _dump_json(argv: List[str], runner: Runner, timeout: float) -> Optional[dict]:
    res = runner(argv, timeout=timeout)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _entries(payload: Optional[dict]) -> List[dict]:
    if not payload:
        return []
    entries = payload.get("entries")
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    return [payload] if payload.get("id") else []


def search_videos(query: str, limit: int, ytdlp_path: str,
                  runner: Runner = default_runner,
                  timeout: float = 120.0) -> List[dict]:
    """Flat video search. Channel identity is inferred from these by channels.py."""
    argv = _base_argv(ytdlp_path) + [
        "--flat-playlist", "--dump-single-json",
        "--playlist-end", str(int(limit)),
        f"ytsearch{int(limit)}:{query}",
    ]
    return _entries(_dump_json(argv, runner, timeout))


def list_channel_videos(channel_url: str, limit: int, ytdlp_path: str,
                        runner: Runner = default_runner,
                        timeout: float = 180.0) -> List[dict]:
    url = channel_url.rstrip("/")
    if not url.endswith("/videos"):
        url += "/videos"
    argv = _base_argv(ytdlp_path) + [
        "--flat-playlist", "--dump-single-json",
        "--playlist-end", str(int(limit)), url,
    ]
    return _entries(_dump_json(argv, runner, timeout))


def fetch_captions(video_id: str, dest_dir: Path, ytdlp_path: str,
                   langs: str = "en.*,-live_chat",
                   runner: Runner = default_runner,
                   timeout: float = 180.0) -> CaptionResult:
    """One network pass that leaves both <id>.info.json and <id>.en.vtt on disk.

    Metadata is read off the written .info.json rather than stdout because
    --dump-single-json implies --simulate, and simulating suppresses subtitle
    writes. Asking for JSON on stdout would silently cost us the captions.

    Manual vs automatic captions land at the same filename, so the filename
    cannot tell them apart. The honest signal is in the info json: an `en*` key
    under "subtitles" means a human wrote them, under "automatic_captions"
    means ASR did. That distinction is recorded on every Document because ASR
    mangles precisely the tokens this pipeline reads -- "one to three" for
    "1:3", "point six one eight" for 0.618.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    argv = _base_argv(ytdlp_path) + [
        "--skip-download", "--no-playlist",
        "--write-info-json", "--write-subs", "--write-auto-subs",
        "--sub-langs", langs, "--sub-format", "vtt/best", "--convert-subs", "vtt",
        "-o", str(dest_dir / "%(id)s.%(ext)s"),
        WATCH_URL.format(vid=video_id),
    ]
    res = runner(argv, timeout=timeout)

    info: Dict = {}
    info_path = dest_dir / f"{video_id}.info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            info = {}

    vtt = next(iter(sorted(dest_dir.glob(f"{video_id}*.vtt"))), None)
    if vtt is None:
        reason = (res.stderr.strip().splitlines() or ["no subtitles written"])[-1]
        return CaptionResult(video_id, info, None, "none",
                             "" if res.ok else reason[:200])

    def _has_en(block) -> bool:
        return isinstance(block, dict) and any(
            k.lower().startswith("en") for k in block
        )

    source = ("captions_manual" if _has_en(info.get("subtitles"))
              else "captions_auto" if _has_en(info.get("automatic_captions"))
              else "captions_auto")
    return CaptionResult(video_id, info, vtt, source, "")


def download_audio(video_id: str, dest_dir: Path, ytdlp_path: str,
                   runner: Runner = default_runner,
                   timeout: float = 1800.0) -> Optional[Path]:
    """16 kHz mono WAV — what every Whisper backend wants, via ffmpeg."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    argv = _base_argv(ytdlp_path) + [
        "-x", "--audio-format", "wav", "--no-playlist",
        "--postprocessor-args", "-ar 16000 -ac 1",
        "-o", str(dest_dir / "%(id)s.%(ext)s"),
        WATCH_URL.format(vid=video_id),
    ]
    res = runner(argv, timeout=timeout)
    if not res.ok:
        return None
    return next(iter(sorted(dest_dir.glob(f"{video_id}*.wav"))), None)
