"""Pointer motion, buttons and scroll wheel through Wayland's zwlr_virtual_pointer_v1 (Hyprland has it).

A plain Wayland client of the compositor, so no root, /dev/uinput or ydotoold.
One connection stays open and is reopened if it drops. Wire details follow
hypruse's client (hypruse/wire.py).

  message: u32 object id, u32 (size << 16 | opcode), args (little-endian)
  string:  u32 length incl. NUL, bytes, NUL, pad to 4;  fixed: signed 24.8
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import time

log = logging.getLogger("freehand")

DISPLAY = 1
SYNC, GET_REGISTRY, BIND = 0, 1, 0  # wl_display.sync, wl_display.get_registry, wl_registry.bind
EV_ERROR = 0
MANAGER = "zwlr_virtual_pointer_manager_v1"
CREATE_POINTER, MANAGER_DESTROY = 0, 1
MOTION, BUTTON, AXIS, FRAME, AXIS_SOURCE, AXIS_DISCRETE, POINTER_DESTROY = 0, 2, 3, 4, 5, 7, 8
VERTICAL, WHEEL = 0, 0
UNITS_PER_NOTCH = 15.0  # what a real mouse wheel reports per click
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}  # linux/input-event-codes.h BTN_*


def _msg(obj: int, opcode: int, body: bytes = b"") -> bytes:
    return struct.pack("<II", obj, (8 + len(body)) << 16 | opcode) + body


def _string(s: str) -> bytes:
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


class VirtualPointer:
    def __init__(self):
        self.sock: socket.socket | None = None

    def _connect(self) -> None:
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-1")
        path = display if display.startswith("/") else os.path.join(os.environ["XDG_RUNTIME_DIR"], display)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect(path)
            registry, callback, manager, pointer = 2, 3, 4, 5
            sock.sendall(_msg(DISPLAY, GET_REGISTRY, struct.pack("<I", registry))
                         + _msg(DISPLAY, SYNC, struct.pack("<I", callback)))
            found, synced, buf = None, False, b""
            while not synced:  # collect globals until the sync callback fires
                chunk = sock.recv(65536)
                if not chunk:
                    raise OSError("compositor closed the connection")
                buf += chunk
                while len(buf) >= 8 and not synced:
                    obj, sizeop = struct.unpack_from("<II", buf)
                    size = sizeop >> 16
                    if len(buf) < size:
                        break  # rest of this message is still in flight
                    body, op, buf = buf[8:size], sizeop & 0xFFFF, buf[size:]
                    if obj == registry and op == 0:  # wl_registry.global
                        name, slen = struct.unpack_from("<II", body)
                        if body[8:8 + slen - 1].decode() == MANAGER:
                            version = struct.unpack_from("<I", body, 8 + slen + (-slen % 4))[0]
                            found = name, min(version, 2)
                    elif obj == DISPLAY and op == EV_ERROR:
                        raise OSError(f"Wayland error: {body[12:].rstrip(bytes(1)).decode(errors='replace')}")
                    elif obj == callback:
                        synced = True
            if found is None:
                raise OSError(f"compositor has no {MANAGER}")
            name, self.version = found
            sock.sendall(_msg(registry, BIND, struct.pack("<I", name) + _string(MANAGER)
                              + struct.pack("<II", self.version, manager))
                         + _msg(manager, CREATE_POINTER, struct.pack("<II", 0, pointer)))
        except BaseException:
            sock.close()
            raise
        self.sock, self.pointer, self.manager = sock, pointer, manager
        log.debug("virtual pointer ready (protocol v%d)", self.version)

    def _send(self, what: str, build) -> None:
        """Send build(pointer_id, time_ms) as one batch, reconnecting once if the socket died."""
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                t = int(time.monotonic() * 1000) & 0xFFFFFFFF
                self.sock.sendall(build(self.pointer, t) + _msg(self.pointer, FRAME))
                return
            except OSError as e:
                self.close()
                if attempt == 2:
                    log.error("%s failed: %s", what, e)

    def scroll(self, notches: int) -> None:
        """Wheel clicks; positive scrolls the content down (like turning a wheel toward you)."""
        if not notches:
            return
        value = round(notches * UNITS_PER_NOTCH * 256)

        def build(p, t):
            axis = (_msg(p, AXIS_DISCRETE, struct.pack("<IIii", t, VERTICAL, value, notches))
                    if self.version >= 2 else _msg(p, AXIS, struct.pack("<IIi", t, VERTICAL, value)))
            return _msg(p, AXIS_SOURCE, struct.pack("<I", WHEEL)) + axis

        self._send("scroll", build)

    def move(self, dx: float, dy: float) -> None:
        """Relative motion in screen pixels (y grows downward)."""
        fx, fy = round(dx * 256), round(dy * 256)
        if fx or fy:
            self._send("move", lambda p, t: _msg(p, MOTION, struct.pack("<Iii", t, fx, fy)))

    def button(self, name: str, down: bool) -> None:
        code = BUTTONS[name]
        self._send("click", lambda p, t: _msg(p, BUTTON, struct.pack("<III", t, code, int(down))))

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(_msg(self.pointer, POINTER_DESTROY) + _msg(self.manager, MANAGER_DESTROY))
        except OSError:
            pass
        self.sock.close()
        self.sock = None
