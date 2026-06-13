"""Manage the single long-running gpu-screen-recorder process.

One GSR process handles BOTH the rolling replay buffer (saved via SIGUSR1) and
full session recordings (toggled via SIGRTMIN to the ``-ro`` directory). We never
spawn a second encoder.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import CLIP_BUCKET_SIGNALS, Config

log = logging.getLogger("gsr-clip.gsr")

GSR_BIN = "gpu-screen-recorder"
# Kernel truncates comm to 15 chars.
GSR_COMM = "gpu-screen-reco"


def signal_from_name(name: str) -> int:
    """Resolve names like 'SIGUSR1', 'SIGRTMIN', 'SIGRTMIN+2' to numbers."""
    name = name.strip().upper()
    if "+" in name:
        base, _, off = name.partition("+")
        return _base_signal(base) + int(off)
    return _base_signal(name)


def _base_signal(name: str) -> int:
    if name == "SIGRTMIN":
        return int(signal.SIGRTMIN)  # type: ignore[attr-defined]
    return int(getattr(signal, name))


def find_running_encoders(exclude_pid: int | None = None) -> list[int]:
    """Return PIDs of any running gpu-screen-recorder processes."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if exclude_pid is not None and pid == exclude_pid:
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm == GSR_COMM:
            pids.append(pid)
    return pids


class EncoderConflict(RuntimeError):
    """Raised when another gpu-screen-recorder is already running."""


class GSRProcess:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._proc: subprocess.Popen | None = None

    # ---- lifecycle ----
    def build_argv(self) -> list[str]:
        rec = self.cfg.recording
        argv = [
            GSR_BIN,
            "-w", "portal",
            "-restore-portal-session", "yes",
            "-portal-session-token-filepath", str(rec.portal_token_path),
            "-f", str(rec.fps),
            "-fm", rec.frame_rate_mode,
            "-s", rec.resolution,
            "-r", str(rec.buffer_seconds),
            "-c", "mp4",
            "-ac", rec.audio_codec,
        ]
        if rec.capture_audio:
            src = rec.audio.strip()
            if src:
                argv += ["-a", src]
            elif rec.capture_microphone:
                argv += ["-a", "default_output|default_input"]
            else:
                argv += ["-a", "default_output"]
        argv += [
            "-o", str(self.cfg.paths.replays_path),
            "-ro", str(self.cfg.paths.sessions_path),
            "-sc", str(self.cfg.hook_path),
        ]
        return argv

    def preflight(self) -> None:
        if shutil.which(GSR_BIN) is None:
            raise FileNotFoundError(
                f"{GSR_BIN} not found on PATH — install gpu-screen-recorder"
            )
        others = find_running_encoders()
        if others:
            raise EncoderConflict(
                "Another gpu-screen-recorder is already running "
                f"(pids: {others}). Disable Vice / other recorders first: "
                "systemctl --user disable --now vice.service"
            )

    def start(self) -> None:
        self.cfg.ensure_dirs()
        self.preflight()
        argv = self.build_argv()
        log.info("starting GSR: %s", " ".join(argv))
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Give it a moment to fail fast (bad portal, etc.).
        time.sleep(1.0)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"GSR exited immediately with code {self._proc.returncode}"
            )

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, sig: int) -> None:
        if not self.is_alive():
            raise RuntimeError("GSR is not running")
        assert self._proc is not None
        log.debug("sending signal %s to GSR pid %s", sig, self._proc.pid)
        self._proc.send_signal(sig)

    # ---- commands ----
    def save_clip(self) -> None:
        """Save from the replay buffer. 'full' => whole buffer (SIGUSR1)."""
        length = str(self.cfg.recording.clip_length).strip().lower()
        if length in ("full", "all", ""):
            self._send(signal.SIGUSR1)
            return
        try:
            seconds = int(length)
        except ValueError:
            log.warning("invalid clip_length %r, saving full buffer", length)
            self._send(signal.SIGUSR1)
            return
        sig_name = CLIP_BUCKET_SIGNALS.get(seconds)
        if sig_name is None:
            # Snap to the nearest available bucket.
            nearest = min(CLIP_BUCKET_SIGNALS, key=lambda s: abs(s - seconds))
            sig_name = CLIP_BUCKET_SIGNALS[nearest]
            log.info("clip_length %ss not a GSR bucket; using nearest (%ss)", seconds, nearest)
        self._send(signal_from_name(sig_name))

    def toggle_session(self) -> None:
        """Start or stop a regular recording to the -ro directory."""
        self._send(int(signal.SIGRTMIN))  # type: ignore[attr-defined]

    def stop(self, timeout: float = 5.0) -> None:
        if not self._proc:
            return
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("GSR did not exit on SIGINT; killing")
                self._proc.kill()
        self._proc = None
