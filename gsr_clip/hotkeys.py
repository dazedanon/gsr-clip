"""Global keyboard hotkeys via evdev (single tap vs double tap).

Reads raw key events from every keyboard device that exposes the configured key,
so it works regardless of which window is focused. Requires read access to
``/dev/input/event*`` (membership in the ``input`` group).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import evdev
from evdev import InputDevice, ecodes

log = logging.getLogger("gsr-clip.hotkeys")

AsyncCb = Callable[[], Awaitable[None]]


def list_input_devices() -> list[InputDevice]:
    devices: list[InputDevice] = []
    for path in evdev.list_devices():
        try:
            devices.append(InputDevice(path))
        except (PermissionError, OSError) as exc:
            log.warning("cannot open %s: %s", path, exc)
    return devices


def keyboards_with_key(devices: list[InputDevice], keycode: int) -> list[InputDevice]:
    out = []
    for dev in devices:
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        if keycode in keys:
            out.append(dev)
    return out


class KeyboardHotkeys:
    """Detect single vs double presses of one key across all keyboards."""

    def __init__(
        self,
        keycode: int,
        on_single: AsyncCb,
        on_double: AsyncCb,
        double_tap_ms: int = 350,
    ):
        self.keycode = keycode
        self.on_single = on_single
        self.on_double = on_double
        self.double_tap_s = double_tap_ms / 1000.0
        self._pending_single: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._devices: list[InputDevice] = []

    def _handle_press(self) -> None:
        # Called on key-down. If a single is already pending within the window,
        # this is a double tap.
        if self._pending_single is not None:
            self._pending_single.cancel()
            self._pending_single = None
            asyncio.ensure_future(self._fire(self.on_double, "double"))
            return
        assert self._loop is not None
        self._pending_single = self._loop.call_later(self.double_tap_s, self._fire_single)

    def _fire_single(self) -> None:
        self._pending_single = None
        asyncio.ensure_future(self._fire(self.on_single, "single"))

    async def _fire(self, cb: AsyncCb, kind: str) -> None:
        try:
            await cb()
        except Exception:  # noqa: BLE001 - never let a hotkey kill the loop
            log.exception("hotkey %s handler failed", kind)

    async def _read_device(self, dev: InputDevice) -> None:
        log.info("listening for key %s on %s (%s)", self.keycode, dev.path, dev.name)
        try:
            async for ev in dev.async_read_loop():
                if ev.type == ecodes.EV_KEY and ev.code == self.keycode and ev.value == 1:
                    self._handle_press()
        except (OSError, asyncio.CancelledError) as exc:
            log.info("stopped reading %s: %s", dev.path, exc)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        devices = list_input_devices()
        self._devices = keyboards_with_key(devices, self.keycode)
        # Close the devices we won't use.
        used = {d.path for d in self._devices}
        for d in devices:
            if d.path not in used:
                d.close()
        if not self._devices:
            log.warning(
                "no keyboard exposes keycode %s — keyboard hotkeys disabled "
                "(check 'input' group membership)",
                self.keycode,
            )
            return
        await asyncio.gather(*(self._read_device(d) for d in self._devices))
