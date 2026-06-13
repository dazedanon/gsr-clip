"""Discover PipeWire/Pulse audio sources for gpu-screen-recorder -a."""

from __future__ import annotations

import shutil
import subprocess

DeviceKind = str  # "Output" | "Input"


def device_kind(source_id: str, label: str = "") -> DeviceKind:
    """Classify a GSR audio source as Input or Output."""
    if source_id in ("default_output",):
        return "Output"
    if source_id in ("default_input",):
        return "Input"
    name = source_id.removeprefix("device:")
    if name.startswith("alsa_output.") or name.endswith(".monitor"):
        return "Output"
    if name.startswith("alsa_input."):
        return "Input"
    if label.startswith("Monitor of "):
        return "Output"
    # app:firefox etc. — treated as output-side capture
    if source_id.startswith("app:") or source_id.startswith("app-inverse:"):
        return "Output"
    return "Output" if "input" not in source_id.lower() else "Input"


def format_device_label(source_id: str, label: str) -> str:
    kind = device_kind(source_id, label)
    return f"[{kind}] {label}"


def list_devices() -> list[tuple[str, str, DeviceKind]]:
    """Return ``[(source_id, label, kind), ...]`` from ``--list-audio-devices``."""
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
    out: list[tuple[str, str, DeviceKind]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        source_id, label = line.split("|", 1)
        source_id = source_id.strip()
        label = label.strip()
        out.append((source_id, label, device_kind(source_id, label)))
    # Outputs first, then inputs; defaults at the top of each group.
    def sort_key(item: tuple[str, str, DeviceKind]) -> tuple[int, str]:
        sid, _, kind = item
        kind_order = 0 if kind == "Output" else 1
        default_first = 0 if sid.startswith("default_") else 1
        return (kind_order, default_first, sid)

    out.sort(key=sort_key)
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
