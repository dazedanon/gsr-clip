"""Decide whether a focused PID is a real Steam-launched game.

Primary signal: ``SteamAppId`` in ``/proc/<pid>/environ`` (robust even under
systemd app scopes + Proton's pressure-vessel/bwrap). Fallbacks: ``STEAM_COMPAT_*``
env markers, then a PPID ancestry walk toward the Steam client.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("gsr-clip.steam")

# Processes that are Steam infrastructure, never "the game".
REJECT_COMMS = {
    "steam",
    "steamwebhelper",
    "steam-runtime-launcher-service",
    "srt-bwrap",
    "pv-adverb",
    "steam.sh",
    "reaper",
}

STEAM_DIRS = [
    Path(os.path.expanduser("~/.local/share/Steam")),
    Path(os.path.expanduser("~/.steam/steam")),
]

MAX_ANCESTRY_DEPTH = 24


def pid_exists(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def read_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def read_environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        key, sep, val = chunk.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = val.decode("utf-8", "replace")
    return env


def read_ppid(pid: int) -> int | None:
    """Parse PPID from /proc/<pid>/stat (field 4, after the comm in parens)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm may contain spaces/parens; everything after the last ')' is stable.
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    fields = data[rparen + 2 :].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _exe_under_steam(pid: int) -> bool:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    return any(str(d) in exe for d in STEAM_DIRS)


def is_rejected(pid: int) -> bool:
    comm = read_comm(pid)
    return comm in REJECT_COMMS if comm else False


def has_steam_ancestor(pid: int) -> bool:
    seen: set[int] = set()
    cur: int | None = pid
    depth = 0
    while cur and cur > 1 and depth < MAX_ANCESTRY_DEPTH:
        if cur in seen:
            break
        seen.add(cur)
        comm = read_comm(cur)
        if comm == "steam" or _exe_under_steam(cur):
            return True
        cur = read_ppid(cur)
        depth += 1
    return False


def is_steam_game(pid: int) -> tuple[bool, str | None]:
    """Return ``(is_game, appid)``.

    ``appid`` is the Steam AppID string when known, else ``None``.
    """
    env = read_environ(pid)
    appid = env.get("SteamAppId") or env.get("STEAM_APPID")
    if appid and appid not in ("0", ""):
        return True, appid
    if any(k.startswith("STEAM_COMPAT") for k in env):
        return True, None
    # environ unreadable/empty -> best-effort ancestry walk.
    if not env and has_steam_ancestor(pid):
        return True, None
    return False, None


def find_pids_by_comm(comm: str) -> list[int]:
    """Find live PIDs whose comm matches (used for Proton-respawn re-acquire)."""
    out: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        if read_comm(int(entry.name)) == comm:
            out.append(int(entry.name))
    return out
