"""Turns button down/up events into actions: press, release, tap, multi-tap, hold, repeat.

Kept free of evdev so it can be driven by tests with synthetic events.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("freehand")

Action = Any  # str | {"keys": str} | {"type": str} | list[Action]

MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "super": "logo",
    "logo": "logo",
    "win": "logo",
    "altgr": "altgr",
}


def wtype_args(chord: str) -> list[str]:
    """'ctrl+shift+t' -> wtype -M ctrl -M shift -k t -m shift -m ctrl"""
    parts = [p for p in chord.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {chord!r}")
    *mods, key = parts
    mapped = []
    for m in mods:
        if m.lower() not in MODIFIERS:
            raise ValueError(f"unknown modifier {m!r} in {chord!r}")
        mapped.append(MODIFIERS[m.lower()])
    args = ["wtype"]
    for m in mapped:
        args += ["-M", m]
    args += ["-k", key]
    for m in reversed(mapped):
        args += ["-m", m]
    return args


def to_argv(action: Action) -> list[list[str]]:
    """Normalise an action into a list of commands (argv lists)."""
    if action is None:
        return []
    if isinstance(action, str):
        return [["sh", "-c", action]]
    if isinstance(action, list):
        return [argv for a in action for argv in to_argv(a)]
    if isinstance(action, dict):
        if "keys" in action:
            return [wtype_args(action["keys"])]
        if "type" in action:
            return [["wtype", "--", action["type"]]]
        if "run" in action:
            return [["sh", "-c", action["run"]]]
    raise ValueError(f"don't know how to run action: {action!r}")


MOUSE_BUTTONS = ("left", "right", "middle")


@dataclass
class Binding:
    press: Action = None
    release: Action = None
    tap: Action = None
    double_tap: Action = None
    triple_tap: Action = None
    click_ms: int | None = None
    hold: Action = None
    repeat: bool = False
    repeat_ms: int | None = None
    hold_ms: int | None = None
    menu: str | None = None  # opens this pie menu instead of running actions
    mouse: str | None = None  # acts as this mouse button ("left", "right", "middle")

    @classmethod
    def from_dict(cls, d: dict) -> "Binding":
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        for only in ("menu", "mouse"):
            if only in d and set(d) - {only}:
                raise ValueError(f"a button with `{only}` can't also have other fields")
        if "mouse" in d and d["mouse"] not in MOUSE_BUTTONS:
            raise ValueError(f"mouse must be one of {', '.join(MOUSE_BUTTONS)}")
        b = cls(**d)
        if b.click_ms is not None and b.click_ms <= 0:
            raise ValueError("click_ms must be positive")
        if b.repeat_ms is not None and b.repeat_ms <= 0:
            raise ValueError("repeat_ms must be positive")
        if b.multi_tap and b.repeat and b.hold is None:
            raise ValueError("double_tap/triple_tap with repeat requires a hold action")
        for name in ("press", "release", "tap", "double_tap", "triple_tap", "hold"):
            to_argv(getattr(b, name))  # validate early
        return b

    @property
    def multi_tap(self) -> bool:
        return self.double_tap is not None or self.triple_tap is not None


@dataclass
class Settings:
    hold_ms: int = 400
    repeat_delay_ms: int = 350
    repeat_ms: int = 90
    command_wait_s: float = 3.0
    click_ms: int = 300

    def __post_init__(self):
        if self.click_ms <= 0:
            raise ValueError("click_ms must be positive")


Runner = Callable[[list[str]], Awaitable[None]]


async def run_action(action: Action, wait_s: float = 3.0) -> None:
    for argv in to_argv(action):
        await run_argv(argv, wait_s)


async def run_argv(argv: list[str], wait_s: float = 3.0) -> None:
    log.info("run %s", shlex.join(argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        log.error("could not run %s: %s", argv[0], e)
        return
    try:
        # Wait briefly so e.g. `superwhisper start` lands before `superwhisper stop`,
        # but don't let a long-running app block the next press.
        await asyncio.wait_for(asyncio.shield(proc.wait()), wait_s)
        if proc.returncode:
            log.warning("%s exited with %s", shlex.join(argv), proc.returncode)
    except asyncio.TimeoutError:
        asyncio.ensure_future(proc.wait())


@dataclass
class _KeyState:
    binding: Binding | None = None
    down: bool = False
    held: bool = False  # hold action fired for this press
    timer: asyncio.TimerHandle | None = None
    repeater: asyncio.Task | None = None
    chain: asyncio.Future | None = None
    clicks: int = 0
    click_timer: asyncio.TimerHandle | None = None


class Engine:
    def __init__(
        self,
        bindings: dict[str, Binding],
        settings: Settings | None = None,
        runner: Runner | None = None,
        resolver=None,
        observer=None,
    ):
        self.observer = observer or (lambda *args, **kwargs: None)
        self.bindings = bindings
        self.resolver = resolver
        self.settings = settings or Settings()
        self.runner = runner or (lambda argv: run_argv(argv, self.settings.command_wait_s))
        self.state: dict[str, _KeyState] = {}

    # -- public -------------------------------------------------------------

    def key(self, name: str, down: bool) -> None:
        """Feed one button transition. `name` is an evdev name like KEY_PLAYPAUSE."""
        st = self.state.setdefault(name, _KeyState())
        if down and not st.down and not st.clicks:
            b = self.bindings.get(name)
            st.binding = self.resolver(name, b) if self.resolver else b
        b = st.binding
        if b is None:
            if down:
                log.info("unmapped button %s (add [keys.%s] to the config)", name, name)
            return
        if down and not st.down:
            self._down(name, b, st)
        elif not down and st.down:
            self._up(name, b, st)

    def release_all(self) -> None:
        """Device vanished mid-press: finish every held button cleanly."""
        for name, st in list(self.state.items()):
            if st.down:
                self.key(name, False)
            if st.clicks:
                self._finish_clicks(name, st.binding, st)

    # -- internals ----------------------------------------------------------

    def _queue(self, name: str, action: Action, gesture: str = "action") -> None:
        cmds = to_argv(action)
        if not cmds:
            return
        self.observer("action", button=name, gesture=gesture, action=action)
        st = self.state.setdefault(name, _KeyState())
        prev = st.chain

        async def go():
            if prev is not None:
                try:
                    await prev
                except Exception:
                    pass
            for argv in cmds:
                self.observer("command", button=name, argv=argv)
                await self.runner(argv)

        st.chain = asyncio.ensure_future(go())

    def _down(self, name: str, b: Binding, st: _KeyState) -> None:
        st.down, st.held = True, False
        if st.click_timer is not None:
            st.click_timer.cancel()
            st.click_timer = None
        log.debug("%s down", name)
        self._queue(name, b.press, "press")
        if b.hold is not None:
            ms = b.hold_ms if b.hold_ms is not None else self.settings.hold_ms
            st.timer = asyncio.get_running_loop().call_later(
                ms / 1000, self._hold_fired, name, b, st
            )
        elif not b.multi_tap:
            self._queue(name, b.tap, "tap")
            if b.repeat:
                self._start_repeat(name, b.tap if b.tap is not None else b.press, st)

    def _hold_fired(self, name: str, b: Binding, st: _KeyState) -> None:
        st.timer = None
        if not st.down:
            return
        st.held = True
        self._finish_clicks(name, b, st)
        log.debug("%s hold", name)
        self._queue(name, b.hold, "hold")
        if b.repeat:
            self._start_repeat(name, b.hold, st, delay=False)

    def _start_repeat(self, name: str, action: Action, st: _KeyState, delay: bool = True) -> None:
        if action is None:
            return
        s = self.settings
        binding = st.binding
        repeat_ms = binding.repeat_ms if binding.repeat_ms is not None else s.repeat_ms

        async def rep():
            if delay:
                await asyncio.sleep(s.repeat_delay_ms / 1000)
            while st.down:
                await asyncio.sleep(repeat_ms / 1000)
                if st.down and (st.chain is None or st.chain.done()):
                    self._queue(name, action, "repeat")

        st.repeater = asyncio.ensure_future(rep())

    def _up(self, name: str, b: Binding, st: _KeyState) -> None:
        st.down = False
        log.debug("%s up", name)
        if st.timer is not None:
            st.timer.cancel()
            st.timer = None
        if st.repeater is not None:
            st.repeater.cancel()
            st.repeater = None
        if b.multi_tap and not st.held:
            st.clicks += 1
            limit = 3 if b.triple_tap is not None else 2
            if st.clicks == limit:
                self._finish_clicks(name, b, st)
            else:
                ms = b.click_ms if b.click_ms is not None else self.settings.click_ms
                st.click_timer = asyncio.get_running_loop().call_later(
                    ms / 1000, self._finish_clicks, name, b, st
                )
        elif b.hold is not None and not st.held:
            self._queue(name, b.tap, "tap")
        self._queue(name, b.release, "release")

    def _finish_clicks(self, name: str, b: Binding, st: _KeyState) -> None:
        if st.click_timer is not None:
            st.click_timer.cancel()
            st.click_timer = None
        count, st.clicks = st.clicks, 0
        if count == 3:
            self._queue(name, b.triple_tap, "triple_tap")
        elif count == 2 and b.double_tap is not None:
            self._queue(name, b.double_tap, "double_tap")
        else:
            for _ in range(count):
                self._queue(name, b.tap, "tap")
