"""Gamepad combo listener (guide + left trigger) via evdev.

Fires once on the rising edge of ``BTN_MODE`` held while ``ABS_Z`` (LT) crosses a
threshold. Listens on the physical gamepad device.

NOTE: when Steam Input is enabled it often grabs the controller and/or consumes
BTN_MODE, so this combo may never reach us while in-game. The keyboard hotkey is
the reliable fallback.
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
    ):
        self.button = button
        self.axis = axis
        self.threshold = threshold
        self.on_combo = on_combo

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
            log.info("stopped reading gamepad %s: %s", dev.path, exc)

    async def _fire(self) -> None:
        try:
            await self.on_combo()
        except Exception:  # noqa: BLE001
            log.exception("gamepad combo handler failed")

    async def run(self) -> None:
        devices = find_gamepads(self.button, self.axis)
        if not devices:
            log.info("no gamepad with button=%s axis=%s found", self.button, self.axis)
            return
        await asyncio.gather(*(self._read_device(d) for d in devices))
