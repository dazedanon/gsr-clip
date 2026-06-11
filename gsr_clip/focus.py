"""Active-window detection (KDE Wayland) via kdotool, plus game-name resolution.

kdotool loads a transient KWin script over D-Bus and is non-interactive, unlike
``org.kde.KWin queryWindowInfo`` (which forces the user to click a window and is
therefore unusable for automation).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import steam_gate

log = logging.getLogger("gsr-clip.focus")

KDOTOOL = "kdotool"
_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9]+")


def kdotool_available() -> bool:
    return shutil.which(KDOTOOL) is not None


def _run(args: list[str], timeout: float = 1.5) -> str | None:
    try:
        out = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("command %s failed: %s", args, exc)
        return None
    if out.returncode != 0:
        log.debug("command %s rc=%s stderr=%s", args, out.returncode, out.stderr.strip())
        return None
    return out.stdout.strip()


def get_active_window_pid() -> int | None:
    out = _run([KDOTOOL, "getactivewindow", "getwindowpid"])
    if not out:
        return None
    line = out.splitlines()[-1].strip()
    try:
        return int(line)
    except ValueError:
        log.debug("unexpected getwindowpid output: %r", out)
        return None


def get_active_window_title() -> str | None:
    out = _run([KDOTOOL, "getactivewindow", "getwindowname"])
    return out.splitlines()[-1].strip() if out else None


def get_active_window_class() -> str | None:
    out = _run([KDOTOOL, "getactivewindow", "getwindowclassname"])
    return out.splitlines()[-1].strip() if out else None


def sanitize_name(name: str, max_len: int = 48) -> str:
    cleaned = _NAME_SANITIZE.sub("-", name).strip("-")
    cleaned = cleaned[:max_len].strip("-")
    return cleaned or "Game"


def _steam_library_paths() -> list[Path]:
    libs: list[Path] = []
    for base in steam_gate.STEAM_DIRS:
        steamapps = base / "steamapps"
        if steamapps.is_dir():
            libs.append(steamapps)
        vdf = steamapps / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(errors="replace")
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = Path(m.group(1)) / "steamapps"
                if p.is_dir():
                    libs.append(p)
    # de-dup preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in libs:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def steam_name_for_appid(appid: str) -> str | None:
    """Read the game's display name from its appmanifest_<appid>.acf."""
    for steamapps in _steam_library_paths():
        manifest = steamapps / f"appmanifest_{appid}.acf"
        if manifest.exists():
            try:
                text = manifest.read_text(errors="replace")
            except OSError:
                continue
            m = re.search(r'"name"\s+"([^"]+)"', text)
            if m:
                return m.group(1)
    return None


def resolve_game_name(pid: int | None, appid: str | None) -> str:
    """Best-effort human game name: Steam manifest > window title > class > comm."""
    if appid:
        name = steam_name_for_appid(appid)
        if name:
            return sanitize_name(name)
    title = get_active_window_title()
    if title:
        return sanitize_name(title)
    cls = get_active_window_class()
    if cls:
        return sanitize_name(cls)
    if pid is not None:
        comm = steam_gate.read_comm(pid)
        if comm:
            return sanitize_name(comm)
    return "Game"
