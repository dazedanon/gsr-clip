"""Gamepad combo listener (guide + left trigger) via evdev.

Fires once on the rising edge of ``BTN_MODE`` held while ``ABS_Z`` (LT) crosses a
threshold. Polls for matching devices so a controller connected *after* the
daemon starts (e.g. a wireless pad powered on later) is picked up on hotplug.

NOTE: when Steam Input is enabled it grabs the controller and/or consumes
BTN_MODE, so this combo never reaches us. Disable Steam Input for the pad
(use it as a generic controller) for the guide+LT combo to work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import evdev
from evdev import InputDevice, ecodes

log = logging.getLogger("gsr-clip.gamepad")

AsyncCb = Callable[[], Awaitable[None]]


def find_gamepads(button: int, axis: int) -> list[InputDevice]:
    out: list[InputDevice] = []
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
        except (PermissionError, OSError):
            continue
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        abses = [code for code, _ in caps.get(ecodes.EV_ABS, [])]
        if button in keys and axis in abses:
            out.append(dev)
        else:
            dev.close()
    return out


class GamepadCombo:
    def __init__(
        self,
        button: int,
        axis: int,
        threshold: int,
        on_combo: AsyncCb,
        poll_seconds: float = 3.0,
    ):
        self.button = button
        self.axis = axis
        self.threshold = threshold
        self.on_combo = on_combo
        self.poll_seconds = poll_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._announced_empty = False

    async def _read_device(self, dev: InputDevice) -> None:
        log.info("listening for gamepad combo on %s (%s)", dev.path, dev.name)
        button_down = False
        axis_active = False
        fired = False
        try:
            async for ev in dev.async_read_loop():
                if ev.type == ecodes.EV_KEY and ev.code == self.button:
                    button_down = ev.value != 0
                elif ev.type == ecodes.EV_ABS and ev.code == self.axis:
                    axis_active = ev.value >= self.threshold
                else:
                    continue

                if button_down and axis_active:
                    if not fired:
                        fired = True
                        await self._fire()
                else:
                    fired = False
        except (OSError, asyncio.CancelledError) as exc:
            log.info("gamepad disconnected %s: %s", dev.path, exc)
        finally:
            try:
                dev.close()
            except OSError:
                pass

    async def _fire(self) -> None:
        try:
            await self.on_combo()
        except Exception:  # noqa: BLE001
            log.exception("gamepad combo handler failed")

    def _scan(self) -> None:
        for dev in find_gamepads(self.button, self.axis):
            if dev.path in self._tasks:
                dev.close()  # already reading this node
                continue
            log.info("gamepad attached: %s (%s)", dev.path, dev.name)
            task = asyncio.ensure_future(self._read_device(dev))
            self._tasks[dev.path] = task
            task.add_done_callback(lambda t, p=dev.path: self._tasks.pop(p, None))
        if self._tasks:
            self._announced_empty = False
        elif not self._announced_empty:
            log.info("no gamepad with button=%s axis=%s yet — waiting for hotplug",
                     self.button, self.axis)
            self._announced_empty = True

    async def run(self) -> None:
        try:
            while True:
                self._scan()
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            for task in list(self._tasks.values()):
                task.cancel()
            raise
