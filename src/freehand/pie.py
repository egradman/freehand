"""Pie menus driven by swipes on the Siri Remote's touch surface.

A button opens a menu laid out in fixed slots (8 by default: N, NE, E, ... NW).
Then you swipe: put your thumb down, move it in a direction, lift. The vector
from touch-down to lift picks the slot; a swipe shorter than `swipe` (a fraction
of the pad radius) does nothing. The slot you're heading for lights up while
you move. Back cancels. The touch that's already on the pad when the menu
opens (the thumb that clicked to open it) is ignored.

This module is pure logic; pie_ui.py draws whatever it's told.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import to_argv

log = logging.getLogger("freehand")


@dataclass
class Touchpad:
    center: tuple[float, float] = (-388.0, -395.0)
    radius: float = 420.0  # the inner pad; the touch-sensitive ring reaches ~1.5x this
    swipe: float = 0.4  # minimum swipe length to choose, as a fraction of radius
    preview: float = 0.15  # swipe length at which the target slot lights up
    hysteresis_deg: float = 6.0

    def vector(self, a: tuple[int, int], b: tuple[int, int]) -> tuple[float, float]:
        """-> (length as a fraction of the radius, compass angle: 0 = up, clockwise)."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        return math.hypot(dx, dy) / self.radius, math.degrees(math.atan2(dx, dy)) % 360


@dataclass
class Item:
    label: str
    content: str = ""
    tint: str | None = None  # optional environment color (#RRGGBB)
    markup: bool = False  # content is Pango markup
    action: Any = None
    submenu: Any = None  # menu name or inline menu definition, opened by dwelling
    dwell_action: Any = None  # optional action instead of a dwell submenu
    dwell_ms: int = 1500
    position: Any = None  # compass name ("n", "ne", ...) or slot index; default: next free


    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        if not isinstance(d, dict) or "label" not in d:
            raise ValueError(f"menu item needs a label: {d!r}")
        unknown = set(d) - {"label", "content", "markup", "action", "position", "submenu", "dwell_ms", "dwell_action", "tint"}
        if unknown:
            raise ValueError(f"unknown menu item fields {sorted(unknown)}")
        item = cls(**d)
        if item.tint is not None and (not isinstance(item.tint, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", item.tint)):
            raise ValueError("tint must be #RRGGBB")
        if not isinstance(item.dwell_ms, int) or item.dwell_ms <= 0:
            raise ValueError("dwell_ms must be a positive integer")
        if item.submenu is not None:
            if isinstance(item.submenu, dict):
                Menu.from_dict(item.label, item.submenu)
            elif not isinstance(item.submenu, str) or not item.submenu:
                raise ValueError("submenu must be a menu name or inline menu definition")
        if item.submenu is not None and item.dwell_action is not None:
            raise ValueError("use submenu or dwell_action, not both")
        to_argv(item.dwell_action)
        to_argv(item.action)  # validate
        return item

    def for_ui(self) -> dict:
        data = {"label": self.label, "content": self.content, "markup": self.markup, "submenu": self.submenu is not None or self.dwell_action is not None}
        if self.tint:
            data["tint"] = self.tint
        return data


@dataclass
class Menu:
    name: str
    title: str = ""
    items: list[Item] = field(default_factory=list)
    source: str | None = None  # shell command printing a JSON list of items, or "@builtin"
    source_timeout_s: float = 5.0
    slots: int = 8
    voice: str | None = None  # holding Siri in this menu: "agent" (computer-use) or "pick" (say an item)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Menu":
        unknown = set(d) - {"title", "items", "source", "source_timeout_s", "slots", "voice"}
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        if not d.get("items") and not d.get("source"):
            raise ValueError("needs `items` or a `source` command")
        menu = cls(
            name=name,
            title=d.get("title", name),
            items=[Item.from_dict(i) for i in d.get("items", [])],
            source=d.get("source"),
            source_timeout_s=float(d.get("source_timeout_s", 5.0)),
            slots=int(d.get("slots", 8)),
            voice=d.get("voice"),
        )
        if menu.voice not in (None, "agent", "pick", "chat-search"):
            raise ValueError('voice must be "agent", "pick", or "chat-search"')
        if menu.source and menu.source.startswith("@") and menu.source[1:] not in BUILTIN_SOURCES:
            raise ValueError(f"unknown built-in source {menu.source!r}; "
                             f"have {', '.join('@' + k for k in BUILTIN_SOURCES)}")
        if not 2 <= menu.slots <= 16:
            raise ValueError("slots must be between 2 and 16")
        layout(menu.items, menu.slots)  # validate positions
        return menu

    async def load(self, count: int | None = None) -> list[Item]:
        """Static items, or whatever the source command prints right now.
        A built-in source returns `count` items (default: one per slot)."""
        if not self.source:
            return list(self.items)
        if self.source.startswith("@"):
            fetch = BUILTIN_SOURCES[self.source[1:]]
            try:
                data = await asyncio.wait_for(fetch(count or self.slots), self.source_timeout_s)
            except asyncio.TimeoutError:
                raise RuntimeError(f"menu {self.name}: {self.source} took longer than {self.source_timeout_s}s")
            return [Item.from_dict(i) for i in data]
        proc = await asyncio.create_subprocess_shell(
            self.source, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.source_timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            proc.kill()
            await proc.communicate()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise RuntimeError(f"menu {self.name}: source took longer than {self.source_timeout_s}s")
        if proc.returncode:
            raise RuntimeError(f"menu {self.name}: source exited with {proc.returncode}")
        data = json.loads(out)
        if isinstance(data, dict):
            data = data.get("items", [])
        return [Item.from_dict(i) for i in data]


async def _t3_threads(count: int) -> list[dict]:
    from .t3 import client

    return await client().recent_threads(count)


GCHAT_HELPER = Path(__file__).with_name("gchat.py")
CHAT_ID = re.compile(r"^(space|dm)/[A-Za-z0-9_-]+$")


async def _gchat(count: int) -> list[dict]:
    """Unread Google Chat conversations from Home and the sidebar."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/python3", "-I", str(GCHAT_HELPER), "list", str(count), "--unread",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    data = json.loads(out or b"null")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Google Chat: {data['error']}")
    if not isinstance(data, list):
        raise RuntimeError(f"Google Chat helper failed: {err.decode(errors='replace').strip()[:200]}")
    items = []
    for c in data:
        if not CHAT_ID.match(c.get("id", "")) or not c.get("unread"):
            continue
        preview = c.get("preview", "")
        if len(preview) > 160:
            preview = preview[:157].rstrip() + "…"
        items.append({
            "label": c.get("title", "?"),
            "content": f"{c.get('when', '')}\n{preview}".strip(),
            "action": f"/usr/bin/python3 -I {GCHAT_HELPER} open {c['id']}",
        })
    return items


async def _t3_create(count: int) -> list[dict]:
    from .t3 import client
    return await client().creation_menu(count)


async def _t3_projects(count: int) -> list[dict]:
    from .t3 import client
    return await client().available_projects(count)


BUILTIN_SOURCES = {"t3-threads": _t3_threads, "gchat": _gchat,
                   "t3-create": _t3_create, "t3-projects": _t3_projects}

COMPASS = ["n", "ne", "e", "se", "s", "sw", "w", "nw"]


def layout(items: list[Item], slots: int) -> list[Item | None]:
    """Place items into `slots` slots (0 = top, clockwise). Explicit positions
    first, then the rest fill the free slots in order."""
    out: list[Item | None] = [None] * slots
    loose = []
    for item in items:
        pos = item.position
        if pos is None:
            loose.append(item)
            continue
        if isinstance(pos, str):
            key = pos.strip().lower()
            if slots != 8 or key not in COMPASS:
                raise ValueError(f"{item.label}: position {pos!r} needs to be one of {COMPASS} "
                                 f"(8 slots) or a slot number")
            idx = COMPASS.index(key)
        else:
            idx = int(pos)
        if not 0 <= idx < slots:
            raise ValueError(f"{item.label}: position {pos!r} is outside 0..{slots - 1}")
        if out[idx] is not None:
            raise ValueError(f"{item.label}: slot {pos!r} is already taken by {out[idx].label}")
        out[idx] = item
    free = [i for i, it in enumerate(out) if it is None]
    if len(loose) > len(free):
        raise ValueError(f"{len(items)} items don't fit in {slots} slots")
    for idx, item in zip(free, loose):
        out[idx] = item
    return out


def slice_at(angle: float, n: int) -> int:
    width = 360 / n
    return int(((angle + width / 2) % 360) // width)


def angular_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


CLICKPAD = frozenset({"select", "up", "down", "left", "right"})


class PieSession:
    """One open menu: follows the swipe and decides when a slot is chosen."""

    def __init__(self, menu: Menu, slots: list[Item | None], trigger: str, pad: Touchpad,
                 touching: bool = False):
        self.menu, self.trigger, self.pad = menu, trigger, pad
        self.slots = slots
        self.highlight: int | None = None
        self.thumb: tuple[float, float] | None = None  # swipe vector on the unit disc, for the UI
        self.origin: tuple[int, int] | None = None
        self.armed = not touching  # ignore the touch that was down when we opened

    def set_slots(self, slots: list[Item | None]) -> None:
        self.slots = slots
        if self.highlight is not None and self.slots[self.highlight] is None:
            self.highlight = None

    def touch(self, pos: tuple[int, int, int] | None) -> str | None:
        """Feed a touch sample (None = lifted). Returns "choose" when a swipe completes."""
        if pos is None:
            fire = self.armed and self.origin is not None and self.highlight is not None and self._long
            self.armed, self.origin, self.thumb, self._long = True, None, None, False
            if not fire:
                self.highlight = None
            return "choose" if fire else None
        if not self.armed:
            return None
        if self.origin is None:
            self.origin, self._long = (pos[0], pos[1]), False
        length, angle = self.pad.vector(self.origin, (pos[0], pos[1]))
        a = math.radians(angle)
        m = min(length / max(self.pad.swipe, 1e-6), 1.0)
        self.thumb = (m * math.sin(a), -m * math.cos(a))
        self._long = length >= self.pad.swipe
        n = len(self.slots)
        if length < self.pad.preview:
            self.highlight = None
            return None
        idx = slice_at(angle, n)
        if self.highlight is not None and self.slots[self.highlight] is not None:
            width = 360 / n
            if angular_distance(angle, self.highlight * width) <= width / 2 + self.pad.hysteresis_deg:
                idx = self.highlight
        self.highlight = idx if self.slots[idx] is not None else None
        return None

    _long = False

    def chosen(self) -> Item | None:
        return self.slots[self.highlight] if self.highlight is not None else None


UI_SCRIPT = __import__("pathlib").Path(__file__).with_name("pie_ui.py")
LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"
SYSTEM_PYTHON = "/usr/bin/python3"


class PieController:
    """Owns the overlay process and the open menu (at most one at a time)."""

    def __init__(self, run_action, menus: dict[str, Menu] | None = None,
                 pad: Touchpad | None = None, timeout_s: float = 30.0):
        self.observer = lambda *args, **kwargs: None
        self.run_action = run_action  # async callable(action)
        self.menus = menus or {}
        self.pad = pad or Touchpad()
        self.timeout_s = timeout_s
        self.session: PieSession | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._timeout: asyncio.TimerHandle | None = None
        self._loading: asyncio.Task | None = None
        self._pending: list[bytes] | None = None
        self._dwell: asyncio.TimerHandle | None = None
        self._dwell_target = None
        self.touching = False  # is a thumb on the pad right now (menu open or not)

    @property
    def open(self) -> bool:
        return self.session is not None

    # -- overlay process ----------------------------------------------------------

    async def start_ui(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        import os

        env = dict(os.environ)
        env["LD_PRELOAD"] = ":".join(p for p in (LAYER_SHELL_LIB, env.get("LD_PRELOAD")) if p)
        self.proc = await asyncio.create_subprocess_exec(
            SYSTEM_PYTHON, "-I", str(UI_SCRIPT), stdin=asyncio.subprocess.PIPE, env=env
        )
        log.debug("pie overlay started (pid %s)", self.proc.pid)

    def send(self, msg: dict) -> None:
        self.observer("menu", **msg)
        line = (json.dumps(msg) + "\n").encode()
        if self._pending is not None:  # overlay (re)starting: queue in order
            self._pending.append(line)
            return
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            try:
                self.proc.stdin.write(line)
                return
            except (BrokenPipeError, ConnectionResetError):
                pass
        self._pending = [line]
        asyncio.ensure_future(self._restart())

    async def _restart(self) -> None:
        try:
            await self.start_ui()
            for line in self._pending:
                self.proc.stdin.write(line)
        except Exception as e:
            log.error("pie overlay failed to start: %s", e)
        finally:
            self._pending = None

    # -- menu lifecycle ---------------------------------------------------------

    def show(self, name: str, trigger: str, now: float) -> None:
        menu = self.menus.get(name)
        if menu is None:
            log.error("no menu named %r", name)
            return
        self._show_menu(menu, trigger)

    def _show_menu(self, menu: Menu, trigger: str) -> None:
        if self.session:
            self.cancel()
        slots = [None] * menu.slots if menu.source else layout(menu.items, menu.slots)
        self.session = PieSession(menu, slots, trigger, self.pad, touching=self.touching)
        self.send({"cmd": "show", "title": menu.title, "items": _ui(slots), "loading": bool(menu.source),
                   "empty_text": "No unread chats" if menu.source == "@gchat" else "Empty"})
        log.info("menu %s opened%s", menu.name, " (loading)" if menu.source else "")
        if menu.source:
            self._loading = asyncio.ensure_future(self._load(self.session))
        loop = asyncio.get_running_loop()
        self._timeout = loop.call_later(self.timeout_s, self.cancel)

    async def _load(self, session: PieSession) -> None:
        try:
            items = await session.menu.load()
        except Exception as e:
            log.error("%s", e)
            if self.session is session:
                self.cancel()
            return
        if self.session is session:
            try:
                slots = layout(items, session.menu.slots)
            except ValueError as e:
                log.error("menu %s: %s", session.menu.name, e)
                self.cancel()
                return
            session.set_slots(slots)
            self.send({"cmd": "items", "items": _ui(slots)})

    def touch(self, pos) -> None:
        self.touching = pos is not None
        s = self.session
        if s is None:
            return
        before = (s.highlight, s.thumb)
        result = s.touch(pos)
        if result == "choose":
            self.choose()
        else:
            if (s.highlight, s.thumb) != before:
                self.send({"cmd": "hover", "index": s.highlight, "thumb": list(s.thumb) if s.thumb else None})
            self._update_dwell(s)

    def _stop_dwell(self) -> None:
        if self._dwell:
            self._dwell.cancel()
            self._dwell = None
        if self._dwell_target is not None:
            self.send({"cmd": "dwell", "index": None, "progress": 0})
        self._dwell_target = None

    def _update_dwell(self, session: PieSession) -> None:
        item = session.chosen()
        target = (session, session.highlight) if (self.touching and session.armed and
                 item is not None and (item.submenu is not None or item.dwell_action is not None)) else None
        if target == self._dwell_target:
            return
        self._stop_dwell()
        if target is None:
            return
        self._dwell_target = target
        loop = asyncio.get_running_loop()
        started = loop.time()

        def tick():
            if self._dwell_target != target or self.session is not session:
                return
            progress = min(1.0, (loop.time() - started) / (item.dwell_ms / 1000))
            self.send({"cmd": "dwell", "index": target[1], "progress": progress})
            if progress < 1:
                self._dwell = loop.call_later(1 / 30, tick)
                return
            if item.dwell_action is not None:
                self.cancel()
                asyncio.ensure_future(self.run_action(item.dwell_action))
                return
            submenu = (Menu.from_dict(item.label, item.submenu) if isinstance(item.submenu, dict)
                       else self.menus.get(item.submenu))
            self._stop_dwell()
            if submenu is None:
                log.error("no submenu named %r", item.submenu)
                return
            # The lingering touch must lift before a new swipe can select a child.
            self._show_menu(submenu, session.trigger)

        tick()

    def choose(self) -> None:
        s = self.session
        item = s.chosen() if s else None
        if item is None:
            self.cancel()
            return
        if item.action is None and item.submenu is not None:
            submenu = (Menu.from_dict(item.label, item.submenu) if isinstance(item.submenu, dict)
                       else self.menus.get(item.submenu))
            if submenu is not None:
                self._show_menu(submenu, s.trigger)
            else:
                self.cancel()
            return
        self.send({"cmd": "choose", "index": s.highlight})
        log.info("menu %s: chose %r", s.menu.name, item.label)
        self._close()
        if item.action is not None:
            asyncio.ensure_future(self.run_action(item.action))

    def cancel(self) -> None:
        if self.session is None:
            return
        log.info("menu %s cancelled", self.session.menu.name)
        self.send({"cmd": "hide"})
        self._close()

    def _close(self) -> None:
        self._stop_dwell()
        self.session = None
        if self._timeout:
            self._timeout.cancel()
            self._timeout = None
        if self._loading and not self._loading.done():
            self._loading.cancel()
        self._loading = None


def _ui(slots: list[Item | None]) -> list[dict | None]:
    return [i.for_ui() if i else None for i in slots]
