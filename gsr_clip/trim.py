"""Lossless trimming via ffmpeg stream-copy (fast, keyframe-aligned)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def trim_clip(src: Path, start: float, end: float, out: Path) -> tuple[bool, str]:
    """Cut ``src`` from ``start`` to ``end`` seconds into ``out`` without re-encoding.

    Uses input seeking + ``-c copy`` so quality is preserved exactly. The cut
    snaps to the nearest keyframe at/just before ``start`` (≤ keyframe interval).
    """
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg not found"
    start = max(0.0, start)
    duration = max(0.1, end - start)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False, proc.stderr[-800:].strip() or "ffmpeg failed"
    return True, str(out)


def transcode_clip(
    src: Path,
    start: float,
    end: float,
    out: Path,
    target_mb: float,
    audio_kbps: int = 128,
) -> tuple[bool, str]:
    """Re-encode ``src[start:end]`` to roughly ``target_mb`` megabytes (h264/aac).

    Used for share presets (e.g. Discord size caps). Single-pass with a capped
    max bitrate; drops to 720p when the bitrate budget gets tight so quality
    stays reasonable.
    """
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg not found"
    start = max(0.0, start)
    duration = max(0.1, end - start)
    # 4% container/overhead headroom so we stay under the cap.
    target_bits = target_mb * 8 * 1024 * 1024 * 0.96
    total_kbps = target_bits / duration / 1000.0
    video_kbps = int(max(200, total_kbps - audio_kbps))
    height = 720 if video_kbps < 2500 else 1080
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.3)}k",
        "-bufsize", f"{int(video_kbps * 2)}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return False, proc.stderr[-800:].strip() or "ffmpeg failed"
    return True, str(out)
