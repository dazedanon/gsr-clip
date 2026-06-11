"""gsr-clip daemon: one GSR process + hotkeys + auto-started Steam sessions.

State machine
-------------
* single tap / gamepad combo -> highlight (in session) else save clip
* double tap                  -> manual override: stop (in session) else start
* focus watcher               -> auto-start a session when a Steam game is focused
* game-PID watcher            -> auto-stop when the game exits (not on focus loss)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import focus, steam_gate, storage
from .config import Config, load_config
from .gsr_process import EncoderConflict, GSRProcess
from .highlights import SessionRecord, sidecar_path_for
from .watcher import FocusWatcher, pid_watch_loop

log = logging.getLogger("gsr-clip.daemon")

SAVE_TYPES = {"regular", "replay", "screenshot"}
FINALIZE_FALLBACK_S = 6.0

# Friendly modifier names -> the evdev key names that satisfy them.
_MODIFIER_ALIASES = {
    "alt": ("KEY_LEFTALT", "KEY_RIGHTALT"),
    "ctrl": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "control": ("KEY_LEFTCTRL", "KEY_RIGHTCTRL"),
    "shift": ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"),
    "meta": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
    "super": ("KEY_LEFTMETA", "KEY_RIGHTMETA"),
}


def _resolve_modifiers(name: str, ecodes) -> set[int]:
    """Turn a modifier config string into the set of evdev keycodes that satisfy it."""
    name = (name or "").strip()
    if not name:
        return set()
    key_names = _MODIFIER_ALIASES.get(name.lower(), (name,))
    codes: set[int] = set()
    for kn in key_names:
        code = ecodes.ecodes.get(kn)
        if code is not None:
            codes.add(code)
        else:
            log.warning("unknown hotkey modifier %r — ignoring", kn)
    return codes


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.gsr = GSRProcess(cfg)
        self.session: SessionRecord | None = None
        self.session_active = False
        self._pid_task: asyncio.Task | None = None
        self._finalizing: tuple[SessionRecord, str] | None = None
        self._finalize_timer: asyncio.TimerHandle | None = None
        self._last_stop_monotonic = 0.0
        self._tasks: list[asyncio.Task] = []
        self._server: asyncio.AbstractServer | None = None
        self._stopping = False
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

    # ------------------------------------------------------------------ setup
    def _write_hook(self) -> None:
        hook = self.cfg.hook_path
        hook.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -m gsr_clip.cli on-save "$1" "$2"\n'
        )
        hook.chmod(0o755)
        log.info("wrote save hook %s", hook)

    async def start(self) -> None:
        self.cfg.ensure_dirs()
        self._write_hook()
        if self.cfg.recording.always_on:
            try:
                self.gsr.start()
            except EncoderConflict as exc:
                log.error("%s", exc)
                self.notify("gsr-clip: cannot start", str(exc))
                raise SystemExit(1)
            log.info("GSR running (pid=%s)", self.gsr.pid)
        else:
            log.info("sessions-only mode: GSR starts when a game session begins")

        await self._start_ipc()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.ensure_future(self.shutdown(s)))

        # Background tasks.
        self._tasks.append(asyncio.ensure_future(self._health_loop()))
        asyncio.ensure_future(self._enforce_storage())  # prune any backlog at boot
        if self.cfg.session.auto_start:
            fw = FocusWatcher(self._on_active_pid, self.cfg.session.focus_poll_seconds)
            self._tasks.append(asyncio.ensure_future(fw.run()))
        self._spawn_input_listeners()

    def _spawn_input_listeners(self) -> None:
        if self.cfg.hotkeys.enabled:
            try:
                from evdev import ecodes

                from .hotkeys import KeyboardHotkeys

                keycode = ecodes.ecodes[self.cfg.hotkeys.clip]
                modifiers = _resolve_modifiers(self.cfg.hotkeys.modifier, ecodes)
                kb = KeyboardHotkeys(
                    keycode=keycode,
                    on_single=self.on_single_tap,
                    on_double=self.on_double_tap,
                    double_tap_ms=self.cfg.hotkeys.double_tap_ms,
                    modifiers=modifiers,
                )
                self._tasks.append(asyncio.ensure_future(kb.run()))
            except Exception:  # noqa: BLE001
                log.exception("failed to start keyboard hotkeys")
        if self.cfg.gamepad.enabled:
            try:
                from evdev import ecodes

                from .gamepad import GamepadCombo

                gp = GamepadCombo(
                    button=ecodes.ecodes[self.cfg.gamepad.button],
                    axis=ecodes.ecodes[self.cfg.gamepad.axis],
                    threshold=self.cfg.gamepad.threshold,
                    on_combo=self.on_single_tap,
                )
                self._tasks.append(asyncio.ensure_future(gp.run()))
            except Exception:  # noqa: BLE001
                log.exception("failed to start gamepad listener")

    # ------------------------------------------------------------------- IPC
    async def _start_ipc(self) -> None:
        sock = self.cfg.socket_path
        try:
            sock.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(sock))
        sock.chmod(0o600)
        log.info("IPC socket at %s", sock)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                writer.write(b'{"ok": false, "error": "bad json"}\n')
                await writer.drain()
                return
            resp = await self._dispatch(msg)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        try:
            if cmd == "clip":
                await self.save_clip()
                return {"ok": True}
            if cmd == "highlight":
                await self.on_single_tap()
                return {"ok": True}
            if cmd == "session":
                await self.on_double_tap()
                return {"ok": True}
            if cmd == "status":
                return {"ok": True, "status": self.status()}
            if cmd == "on_save":
                await self._on_save(msg.get("a", ""), msg.get("b", ""))
                return {"ok": True}
            if cmd == "stop_daemon":
                asyncio.ensure_future(self.shutdown(signal.SIGTERM))
                return {"ok": True}
            return {"ok": False, "error": f"unknown cmd {cmd!r}"}
        except Exception as exc:  # noqa: BLE001
            log.exception("dispatch error for %s", cmd)
            return {"ok": False, "error": str(exc)}

    # --------------------------------------------------------------- actions
    async def save_clip(self) -> None:
        if not self.gsr.is_alive():
            log.warning("clip requested but GSR not alive")
            if not self.cfg.recording.always_on:
                self.notify("gsr-clip", "Not recording — clips work during a game session")
            return
        self.gsr.save_clip()
        log.info("clip saved (replay buffer)")
        if self.cfg.notifications.clips:
            self.notify("Clip saved", str(self.cfg.paths.replays_path))

    async def on_single_tap(self) -> None:
        if self.session_active and self.session is not None:
            h = self.session.add_highlight()
            self._write_sidecar_incremental()
            if self.cfg.notifications.highlights:
                self.notify("Highlight", h.label)
            self.play_sound(self.cfg.notifications.sound_highlight)
        else:
            await self.save_clip()

    async def on_double_tap(self) -> None:
        async with self._lock:
            if self.session_active:
                await self._stop_session(reason="manual")
            else:
                await self._manual_start()

    async def _on_active_pid(self, pid: int | None) -> None:
        if self.session_active or self._stopping:
            return
        if pid is None or pid <= 1:
            return
        if steam_gate.is_rejected(pid):
            return
        # debounce: ignore for a moment after a stop / flicker
        if time.monotonic() - self._last_stop_monotonic < self.cfg.session.debounce_seconds:
            return
        ok, appid = steam_gate.is_steam_game(pid)
        if not ok:
            return
        async with self._lock:
            if self.session_active:  # re-check under lock
                return
            await self._start_session(pid, appid, manual=False)

    async def _manual_start(self) -> None:
        pid = focus.get_active_window_pid()
        if pid is None:
            self.notify("gsr-clip", "Could not detect focused window")
            return
        if self.cfg.session.require_steam_game:
            if steam_gate.is_rejected(pid):
                self.notify("gsr-clip", "Focused app is Steam UI, not a game")
                return
            ok, appid = steam_gate.is_steam_game(pid)
            if not ok:
                self.notify("gsr-clip", "Focused app is not a Steam game")
                return
        else:
            _, appid = steam_gate.is_steam_game(pid)
        await self._start_session(pid, appid, manual=True)

    async def _start_session(self, pid: int, appid: str | None, manual: bool) -> None:
        if not self.gsr.is_alive():
            if self.cfg.recording.always_on:
                log.warning("cannot start session, GSR not alive")
                return
            # sessions-only: spin up the encoder now (off-loop; start() blocks ~1s).
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.gsr.start)
            except EncoderConflict as exc:
                log.error("%s", exc)
                self.notify("gsr-clip: cannot record", str(exc))
                return
            except Exception:  # noqa: BLE001
                log.exception("failed to launch GSR for session")
                self.notify("gsr-clip", "Failed to start the recorder")
                return
            log.info("GSR launched for session (pid=%s)", self.gsr.pid)
        game = focus.resolve_game_name(pid, appid)
        now = datetime.now()
        record = SessionRecord(
            game=game,
            appid=appid,
            watched_pid=pid,
            started_at=now.isoformat(timespec="seconds"),
            started_monotonic=time.monotonic(),
            planned_name=f"{game}_{now.strftime('%Y-%m-%d_%H-%M-%S')}.mp4",
        )
        self.gsr.toggle_session()  # SIGRTMIN -> start regular recording
        self.session = record
        self.session_active = True
        log.info("session started: game=%s appid=%s pid=%s manual=%s", game, appid, pid, manual)
        if self.cfg.notifications.session_start:
            self.notify("Recording started", game)
        self.play_sound(self.cfg.notifications.sound_session_start)
        if self.cfg.session.auto_stop_on_exit:
            comm = steam_gate.read_comm(pid)
            self._pid_task = asyncio.ensure_future(
                pid_watch_loop(pid, comm, self.cfg.session.pid_poll_seconds, self._on_game_exit)
            )

    async def _on_game_exit(self) -> None:
        async with self._lock:
            if self.session_active:
                await self._stop_session(reason="game-exit")

    async def _stop_session(self, reason: str) -> None:
        if not self.session_active or self.session is None:
            return
        record = self.session
        planned = record.planned_name
        log.info("stopping session (%s); planned file=%s", reason, planned)

        # Arrange finalization BEFORE signalling stop, so we don't miss a fast
        # save-hook callback.
        try:
            self.cfg.session_name_path.write_text(planned)
        except OSError:
            log.exception("could not write session-name file")
        self._finalizing = (record, planned)
        loop = asyncio.get_running_loop()
        self._finalize_timer = loop.call_later(
            FINALIZE_FALLBACK_S, lambda: asyncio.ensure_future(self._finalize_fallback())
        )

        self.gsr.toggle_session()  # SIGRTMIN -> stop

        if self._pid_task and not self._pid_task.done():
            self._pid_task.cancel()
        self._pid_task = None

        self.session_active = False
        self.session = None
        self._last_stop_monotonic = time.monotonic()

    async def _on_save(self, a: str, b: str) -> None:
        # Be robust to argument ordering: type is whichever is a known type.
        if a in SAVE_TYPES:
            save_type, path = a, b
        elif b in SAVE_TYPES:
            save_type, path = b, a
        else:
            save_type, path = "regular", a or b
        log.info("save hook: type=%s path=%s", save_type, path)
        if save_type == "regular":
            await self._finalize_session(Path(path))
        elif save_type == "replay" and self.cfg.notifications.clips:
            self.notify("Clip saved", path)
        await self._enforce_storage()

    async def _finalize_session(self, gsr_path: Path) -> None:
        if not self._finalizing:
            log.info("regular save with no pending session; leaving as-is: %s", gsr_path)
            return
        record, planned = self._finalizing
        self._finalizing = None
        if self._finalize_timer:
            self._finalize_timer.cancel()
            self._finalize_timer = None

        target = self.cfg.paths.sessions_path / planned
        try:
            if gsr_path.exists() and gsr_path != target:
                gsr_path.rename(target)
        except OSError:
            log.exception("rename %s -> %s failed; keeping original", gsr_path, target)
            target = gsr_path
        record.write_sidecar(target)
        log.info("session finalized: %s", target)
        if self.cfg.notifications.session_stop:
            self.notify("Recording stopped", str(target))
        self.play_sound(self.cfg.notifications.sound_session_stop)
        await self._maybe_stop_encoder()

    async def _finalize_fallback(self) -> None:
        if not self._finalizing:
            return
        record, planned = self._finalizing
        self._finalizing = None
        log.warning("save hook did not fire; finalizing sidecar with planned name")
        target = self.cfg.paths.sessions_path / planned
        record.write_sidecar(target)
        if self.cfg.notifications.session_stop:
            self.notify("Recording stopped", str(target))
        self.play_sound(self.cfg.notifications.sound_session_stop)
        await self._maybe_stop_encoder()
        await self._enforce_storage()

    async def _maybe_stop_encoder(self) -> None:
        """In sessions-only mode, shut GSR down between sessions."""
        if self.cfg.recording.always_on:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.gsr.stop)
            log.info("sessions-only: encoder stopped (recording icon clears)")
        except Exception:  # noqa: BLE001
            log.exception("failed to stop encoder after session")

    async def _enforce_storage(self) -> None:
        """Delete oldest recordings if we're over the configured size cap."""
        if self.cfg.storage.max_size_bytes <= 0:
            return
        protect: set[Path] = set()
        if self.session_active and self.session is not None:
            protect.add(self.cfg.paths.sessions_path / self.session.planned_name)
        if self._finalizing is not None:
            protect.add(self.cfg.paths.sessions_path / self._finalizing[1])
        try:
            freed, deleted = await asyncio.get_running_loop().run_in_executor(
                None, lambda: storage.enforce_limit(self.cfg, protect)
            )
        except Exception:  # noqa: BLE001
            log.exception("storage enforcement failed")
            return
        if deleted:
            log.info(
                "storage cap: freed %.1f MB by removing %d old recording(s)",
                freed / (1024 * 1024),
                len(deleted),
            )

    def _write_sidecar_incremental(self) -> None:
        if self.session is None:
            return
        # Write to the stable planned-name sidecar so highlights survive a crash
        # mid-session and match the final renamed file.
        target = self.cfg.paths.sessions_path / self.session.planned_name
        try:
            self.session.write_sidecar(target)
        except OSError:
            log.exception("incremental sidecar write failed")

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "gsr_alive": self.gsr.is_alive(),
            "gsr_pid": self.gsr.pid,
            "session_active": self.session_active,
            "game": self.session.game if self.session else None,
            "appid": self.session.appid if self.session else None,
            "watched_pid": self.session.watched_pid if self.session else None,
            "highlights": len(self.session.highlights) if self.session else 0,
            "started_at": self.session.started_at if self.session else None,
            "kdotool": focus.kdotool_available(),
            "auto_start": self.cfg.session.auto_start,
            "mode": "always-on" if self.cfg.recording.always_on else "sessions-only",
        }

    # ---------------------------------------------------------- notifications
    def notify(self, title: str, body: str = "") -> None:
        try:
            subprocess.Popen(
                ["notify-send", "-a", "gsr-clip", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.debug("notify-send unavailable")

    def play_sound(self, path: str) -> None:
        """Play a short cue. Audible even in fullscreen games (unlike popups)."""
        if not self.cfg.notifications.sound or not path or not Path(path).exists():
            return
        for player in ("pw-play", "paplay", "canberra-gtk-play"):
            if shutil.which(player):
                args = [player, "-f", path] if player == "canberra-gtk-play" else [player, path]
                try:
                    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
                return

    # --------------------------------------------------------------- health
    async def _health_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(5.0)
            if self._stopping:
                break
            # In sessions-only mode GSR is intentionally absent between games.
            if not self.cfg.recording.always_on:
                continue
            if not self.gsr.is_alive():
                log.error("GSR process died; restarting (session state lost)")
                if self.session_active:
                    self.session_active = False
                    self.session = None
                    if self._pid_task and not self._pid_task.done():
                        self._pid_task.cancel()
                try:
                    self.gsr.start()
                    log.info("GSR restarted (pid=%s)", self.gsr.pid)
                except Exception:  # noqa: BLE001
                    log.exception("GSR restart failed; retrying in 5s")

    # -------------------------------------------------------------- shutdown
    async def shutdown(self, sig: signal.Signals) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down (signal %s)", sig)
        if self._pid_task and not self._pid_task.done():
            self._pid_task.cancel()
        for t in self._tasks:
            t.cancel()
        if self._server:
            self._server.close()
        try:
            self.gsr.stop()
        except Exception:  # noqa: BLE001
            log.exception("error stopping GSR")
        try:
            self.cfg.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._shutdown_event.set()

    async def wait_closed(self) -> None:
        await self._shutdown_event.wait()


async def _run() -> None:
    cfg = load_config()
    daemon = Daemon(cfg)
    await daemon.start()
    await daemon.wait_closed()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GSR_CLIP_LOG", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
