"""Discover PipeWire/Pulse audio sources for gpu-screen-recorder -a."""

from __future__ import annotations

import shutil
import subprocess


def list_devices() -> list[tuple[str, str]]:
    """Return ``[(source_id, label), ...]`` from ``gpu-screen-recorder --list-audio-devices``.

    *source_id* is what GSR expects (``default_output``, ``default_input``,
    ``device:name``, …). *label* is the human-readable name from the tool.
    """
    gsr = shutil.which("gpu-screen-recorder")
    if not gsr:
        return []
    try:
        proc = subprocess.run(
            [gsr, "--list-audio-devices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        source_id, label = line.split("|", 1)
        out.append((source_id.strip(), label.strip()))
    return out


def parse_audio_string(audio: str) -> list[str]:
    """Split a saved ``recording.audio`` pipe string into source ids."""
    audio = (audio or "").strip()
    if not audio:
        return []
    return [part.strip() for part in audio.split("|") if part.strip()]


def join_audio_sources(sources: list[str]) -> str:
    return "|".join(sources)


def default_selection(capture_audio: bool, capture_microphone: bool) -> list[str]:
    """Legacy fallback when ``recording.audio`` is empty."""
    out: list[str] = []
    if capture_audio:
        out.append("default_output")
    if capture_microphone:
        out.append("default_input")
    return out
