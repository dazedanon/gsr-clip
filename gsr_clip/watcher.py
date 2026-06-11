"""Focus watcher (auto-start) and game-PID watcher (auto-stop).

Start is focus-driven; stop is driven by the watched game PID exiting — never by
focus loss, so alt-tabbing out of the game keeps recording.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from . import focus, steam_gate

log = logging.getLogger("gsr-clip.watcher")


class FocusWatcher:
    """Polls the active-window PID and reports it to the daemon."""

    def __init__(
        self,
        on_active_pid: Callable[[int | None], Awaitable[None]],
        interval: float = 1.0,
    ):
        self.on_active_pid = on_active_pid
        self.interval = interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not focus.kdotool_available():
            log.warning("kdotool not found — auto-start disabled (clips still work)")
            return
        log.info("focus watcher started (interval=%ss)", self.interval)
        while not self._stop.is_set():
            try:
                pid = focus.get_active_window_pid()
                await self.on_active_pid(pid)
            except Exception:  # noqa: BLE001
                log.exception("focus watcher iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass


async def pid_watch_loop(
    pid: int,
    comm: str | None,
    interval: float,
    on_exit: Callable[[], Awaitable[None]],
) -> None:
    """Watch ``pid`` until it exits, then fire ``on_exit``.

    On exit, attempt a Proton-respawn re-acquire: if another live PID with the
    same comm is still a Steam game, keep watching it instead of stopping.
    """
    log.info("pid watcher started for pid=%s comm=%s", pid, comm)
    current = pid
    try:
        while True:
            await asyncio.sleep(interval)
            if steam_gate.pid_exists(current):
                continue
            # Re-acquire (Proton may fork a new PID for the same game).
            reacquired = None
            if comm:
                for cand in steam_gate.find_pids_by_comm(comm):
                    ok, _ = steam_gate.is_steam_game(cand)
                    if ok:
                        reacquired = cand
                        break
            if reacquired is not None:
                log.info("re-acquired game pid %s -> %s (comm=%s)", current, reacquired, comm)
                current = reacquired
                continue
            log.info("watched pid %s gone; stopping session", current)
            await on_exit()
            return
    except asyncio.CancelledError:
        log.info("pid watcher cancelled (manual stop)")
        raise
