"""Talk to an Apple Siri Remote (3rd gen, 004C:0315) over BlueZ GATT.

The remote's HID service is vendor-defined and silent until the host writes
0xAF to its feature report 0xF0, which BlueZ's `hog` plugin won't let anyone
else do. So bluetoothd runs with --noplugin=hog and we read the reports here:

  0xFB  buttons, 2-byte little-endian bitmask
  0xFC  touch surface positions
  0xFA  microphone, Opus frames, only while Siri is held

Protocol facts from github.com/azais-corentin/siri-remote and our own captures.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

log = logging.getLogger("freehand")

BLUEZ = "org.bluez"
MODALIAS_PREFIXES = ("bluetooth:v004Cp0315",)
HID_REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_REF_UUID = "00002908-0000-1000-8000-00805f9b34fb"

REPORT_MIC, REPORT_BUTTONS, REPORT_TOUCH = 0xFA, 0xFB, 0xFC
ENABLE_REPORT, ENABLE_BYTE = 0xF0, 0xAF
INPUT, FEATURE = 1, 3

BUTTONS = {
    0x0001: "tv",
    0x0002: "volume_up",
    0x0004: "volume_down",
    0x0008: "select",
    0x0010: "power",
    0x0020: "siri",
    0x0040: "back",
    0x0080: "mute",
    0x0100: "play_pause",
    0x0200: "up",
    0x0400: "right",
    0x0800: "down",
    0x1000: "left",
}
BUTTON_NAMES = set(BUTTONS.values())


def _value_callback(handler):
    def on_value(iface, changed, invalidated):
        if "Value" in changed:
            handler(bytes(changed["Value"].value))

    return on_value


TOUCH_MARKER = 0x32


def _sext12(v: int) -> int:
    return v - 0x1000 if v & 0x800 else v


def parse_touch(data: bytes) -> tuple[int, int, int] | None:
    """Touch report -> (x, y, pressure) of the first finger, or None if lifted.

    11 bytes (one finger) or 18 (two): [0]=0x32, [1:3] sequence, [3] header,
    then 7-byte slots: 24-bit packed x/y (12 bits each, signed), major, minor,
    pressure, flags. A slot is in contact if major/minor/pressure is non-zero.
    """
    if len(data) not in (11, 18) or data[0] != TOUCH_MARKER:
        return None
    slot = data[4:11]
    if not (slot[3] or slot[4] or slot[5]):
        return None
    packed = slot[0] | slot[1] << 8 | slot[2] << 16
    return _sext12(packed & 0xFFF), _sext12(packed >> 12 & 0xFFF), slot[5]


def button_changes(old: int, new: int) -> list[tuple[str, bool]]:
    """Diff two button masks into (name, is_down) transitions, releases first."""
    out = []
    for bit, name in BUTTONS.items():
        if old & bit and not new & bit:
            out.append((name, False))
    for bit, name in BUTTONS.items():
        if new & bit and not old & bit:
            out.append((name, True))
    return out


class SiriRemote:
    """Finds, connects to and streams from the remote; reconnects forever."""

    def __init__(
        self,
        on_button: Callable[[str, bool], None],
        on_audio: Callable[[bytes], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        address: str | None = None,
        on_touch: Callable[[tuple[int, int, int] | None], None] | None = None,
        on_buttons: Callable[[list[tuple[str, bool]]], None] | None = None,
    ):
        # on_buttons gets every change from one report at once (buttons pressed
        # together); without it, on_button is called per change.
        self.on_buttons = on_buttons or (lambda changes: [on_button(n, d) for n, d in changes])
        self.on_button = on_button
        self.on_audio = on_audio
        self.on_touch = on_touch
        self._touching = False
        self._lift_timer: asyncio.TimerHandle | None = None
        self.on_disconnect = on_disconnect
        self.address = address.upper() if address else None
        self.mask = 0
        self.bus: MessageBus | None = None
        self._subs: list = []

    async def run(self) -> None:
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        announced = False
        while True:
            path = await self._find_device()
            if path is None:
                if not announced:
                    log.info("no paired Siri Remote found; pair it with bluetoothctl (see README)")
                    announced = True
                await asyncio.sleep(5)
                continue
            announced = False
            try:
                await self._session(path)
            except DBusError as e:
                log.debug("session ended: %s", e)
            except Exception:
                log.exception("remote session crashed")
            self._release()
            await asyncio.sleep(1)

    # -- discovery --------------------------------------------------------------

    async def _objects(self) -> dict:
        intro = await self.bus.introspect(BLUEZ, "/")
        om = self.bus.get_proxy_object(BLUEZ, "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
        return await om.call_get_managed_objects()

    async def _find_device(self) -> str | None:
        for path, ifaces in (await self._objects()).items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not dev.get("Paired", Variant("b", False)).value:
                continue
            addr = dev["Address"].value.upper()
            alias = dev.get("Modalias", Variant("s", "")).value
            if (self.address and addr == self.address) or (
                not self.address and alias.startswith(MODALIAS_PREFIXES)
            ):
                return path
        return None

    async def _iface(self, path: str, name: str):
        intro = await self.bus.introspect(BLUEZ, path)
        obj = self.bus.get_proxy_object(BLUEZ, path, intro)
        return obj, obj.get_interface(name)

    # -- one connection -----------------------------------------------------------

    async def _session(self, path: str) -> None:
        obj, dev = await self._iface(path, "org.bluez.Device1")
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        gone = asyncio.Event()
        resolved = asyncio.Event()

        def changed(iface, changed, invalidated):
            if iface != "org.bluez.Device1":
                return
            if "Connected" in changed and not changed["Connected"].value:
                gone.set()
            if "ServicesResolved" in changed and changed["ServicesResolved"].value:
                resolved.set()

        props.on_properties_changed(changed)
        try:
            if not await dev.get_connected():
                # The remote sleeps when idle and only advertises again after a
                # button press, so keep asking; BlueZ fails fast if it's absent.
                try:
                    await dev.call_connect()
                except DBusError as e:
                    log.debug("connect: %s", e.text)
                    await asyncio.sleep(1)
                    return
            if not await dev.get_services_resolved():
                await asyncio.wait_for(resolved.wait(), 20)
            name = await dev.get_alias()
            await self._subscribe(path)
            log.info("connected: Siri Remote %s (%s)", name, await dev.get_address())
            await gone.wait()
            log.info("remote disconnected (it sleeps when idle; press a button to wake it)")
        finally:
            props.off_properties_changed(changed)
            for p, cb in self._subs:
                p.off_properties_changed(cb)
            self._subs = []

    async def _subscribe(self, dev_path: str) -> None:
        reports: dict[tuple[int, int], str] = {}
        for path, ifaces in (await self._objects()).items():
            d = ifaces.get("org.bluez.GattDescriptor1")
            if path.startswith(dev_path + "/") and d and d["UUID"].value == REPORT_REF_UUID:
                _, desc = await self._iface(path, "org.bluez.GattDescriptor1")
                ref = await desc.call_read_value({})
                reports[(ref[0], ref[1])] = d["Characteristic"].value

        handlers = {REPORT_BUTTONS: self._buttons}
        if self.on_audio:
            handlers[REPORT_MIC] = self.on_audio
        if self.on_touch:
            handlers[REPORT_TOUCH] = self._touch
        for rid, handler in handlers.items():
            cpath = reports.get((rid, INPUT))
            if not cpath:
                raise RuntimeError(f"remote has no input report {rid:#x}")
            obj, char = await self._iface(cpath, "org.bluez.GattCharacteristic1")
            props = obj.get_interface("org.freedesktop.DBus.Properties")
            cb = _value_callback(handler)
            props.on_properties_changed(cb)
            self._subs.append((props, cb))
            await char.call_start_notify()

        cpath = reports.get((ENABLE_REPORT, FEATURE))
        if not cpath:
            raise RuntimeError("remote has no enable report 0xf0")
        _, char = await self._iface(cpath, "org.bluez.GattCharacteristic1")
        await char.call_write_value(bytes([ENABLE_BYTE]), {"type": Variant("s", "request")})

    # -- reports ----------------------------------------------------------------

    def _buttons(self, data: bytes) -> None:
        if len(data) != 2:
            return
        new = int.from_bytes(data, "little")
        changes = button_changes(self.mask, new)
        self.mask = new
        if changes:
            self.on_buttons(changes)

    LIFT_TIMEOUT_S = 0.25  # the remote streams ~60 reports/s while touched

    def _touch(self, data: bytes) -> None:
        t = parse_touch(data)
        if self._lift_timer:
            self._lift_timer.cancel()
            self._lift_timer = None
        if t is None and not self._touching:
            return
        self._touching = t is not None
        if self._touching:
            loop = asyncio.get_running_loop()
            self._lift_timer = loop.call_later(self.LIFT_TIMEOUT_S, self._lifted)
        self.on_touch(t)

    def _lifted(self) -> None:
        """No touch reports for a while: treat it as a lift even if we missed the report."""
        self._lift_timer = None
        if self._touching:
            self._touching = False
            self.on_touch(None)

    def _release(self) -> None:
        changes = button_changes(self.mask, 0)
        self.mask = 0
        if changes:
            self.on_buttons(changes)
        if self._touching and self.on_touch:
            self._touching = False
            self.on_touch(None)
        if self.on_disconnect:
            self.on_disconnect()
