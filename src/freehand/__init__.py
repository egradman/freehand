"""freehand: map an Apple TV (Siri) Remote's buttons to anything on Linux."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .engine import Binding, Engine, Settings

log = logging.getLogger("freehand")

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "freehand" / "config.toml"
MIC_MODES = ("remote", "computer", "off")


# --- config ------------------------------------------------------------------


@dataclass
class Config:
    bindings: dict[str, Binding]
    settings: Settings
    address: str | None = None
    mic: str = "remote"
    transcribe: str = "superwhisper transcribe {wav} --insert"
    menus: dict = field(default_factory=dict)
    touchpad: object = None
    wheel: object = None
    mouse: object = None
    agent: object = None
    siri_tap: object = None
    siri_hold_ms: int = 300
    layers: object = None
    event_log: Path | None = None


def ensure_config(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resources.files("freehand").joinpath("default_config.toml").read_text())
        log.info("wrote default config to %s", path)


def _wheel_config(raw: dict):
    from .wheel import WheelConfig

    try:
        return WheelConfig.from_dict(raw.get("wheel", {}))
    except (TypeError, ValueError) as e:
        raise ValueError(f"[wheel]: {e}") from e


def _mouse_config(raw: dict):
    from .mouse import MouseConfig

    try:
        return MouseConfig.from_dict(raw.get("mouse", {}))
    except (TypeError, ValueError) as e:
        raise ValueError(f"[mouse]: {e}") from e


def load_config(path: Path) -> Config:
    from .siri import BUTTON_NAMES

    with path.open("rb") as f:
        raw = tomllib.load(f)
    t = raw.get("timing", {})
    settings = Settings(**{k: t[k] for k in ("hold_ms", "repeat_delay_ms", "repeat_ms", "command_wait_s", "click_ms") if k in t})
    bindings = {}
    for name, spec in raw.get("buttons", {}).items():
        if name not in BUTTON_NAMES:
            raise ValueError(f"[buttons.{name}]: unknown button; use one of {', '.join(sorted(BUTTON_NAMES))}")
        try:
            bindings[name] = Binding.from_dict(spec)
        except (TypeError, ValueError) as e:
            raise ValueError(f"[buttons.{name}]: {e}") from e
    d = raw.get("dictation", {})
    from .engine import to_argv
    to_argv(d.get("tap"))
    if d.get("hold_ms", 300) <= 0:
        raise ValueError("[dictation] hold_ms must be positive")
    mic = d.get("mic", "remote")
    if mic not in MIC_MODES:
        raise ValueError(f"[dictation] mic must be one of {MIC_MODES}")
    if mic == "computer" and "siri" not in bindings:
        bindings["siri"] = Binding(press="superwhisper start", release="superwhisper stop")

    from .pie import Menu, Touchpad

    menus = {}
    for name, spec in raw.get("menus", {}).items():
        try:
            menus[name] = Menu.from_dict(name, spec)
        except (TypeError, ValueError) as e:
            raise ValueError(f"[menus.{name}]: {e}") from e
    for name, b in bindings.items():
        if b.menu is not None and b.menu not in menus:
            raise ValueError(f"[buttons.{name}]: no [menus.{b.menu}] defined")
    tp = raw.get("touchpad", {})
    unknown = set(tp) - {"center", "radius", "deadzone", "hysteresis_deg"}
    if unknown:
        raise ValueError(f"[touchpad]: unknown fields {sorted(unknown)}")
    if "center" in tp:
        tp["center"] = tuple(float(v) for v in tp["center"])
    from .agent import AgentConfig

    try:
        agent = AgentConfig.from_dict(raw.get("agent", {}))
        agent.prompt = path.parent / "agent.md"
    except (TypeError, ValueError) as e:
        raise ValueError(f"[agent]: {e}") from e
    for name in agent.menus:
        if name not in menus:
            raise ValueError(f"[agent] menus: no [menus.{name}] defined")
    from .layers import Layers
    layers = Layers(raw.get("applications", {}), bindings, BUTTON_NAMES)
    for _, overrides in layers.profiles:
        for name, binding in overrides.items():
            if binding.menu is not None and binding.menu not in menus:
                raise ValueError(f"application button {name}: no [menus.{binding.menu}] defined")
    recording = raw.get("recording", {})
    if set(recording) - {"event_log"}:
        raise ValueError("[recording]: only event_log is supported")
    event_path = recording.get("event_log", "")
    if not isinstance(event_path, str):
        raise ValueError("[recording] event_log must be a path string")
    event_path = Path(event_path).expanduser() if event_path else None
    if event_path is not None and not event_path.is_absolute():
        event_path = path.parent / event_path
    return Config(
        event_log=event_path,
        bindings=bindings,
        settings=settings,
        address=raw.get("remote", {}).get("address"),
        mic=mic,
        transcribe=d.get("command", Config.transcribe),
        menus=menus,
        touchpad=Touchpad(**tp),
        wheel=_wheel_config(raw),
        mouse=_mouse_config(raw),
        agent=agent,
        siri_tap=d.get("tap"),
        siri_hold_ms=d.get("hold_ms", 300),
        layers=layers,
    )


# --- daemon ------------------------------------------------------------------


class Daemon:
    def __init__(self, config_path: Path):
        from .agent import VoiceAgent
        from .mic import RemoteMic

        from .events import EventLog

        self.events = EventLog()
        self.config_path = config_path
        self.mtime = 0.0
        self.engine = Engine({})
        self.mic = RemoteMic()
        self.voice = VoiceAgent(self.mic)
        self.pie = None
        self.wheel = None
        self.mouse = None
        self.pointer = None
        self.mouse_down: dict[str, str] = {}  # remote button -> mouse button it's holding down
        self.swallowed: set[str] = set()
        self.siri_timer = None
        self.siri_down = False
        self.siri_started = False
        self.reload(initial=True)

    def reload(self, initial: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        try:
            cfg = load_config(self.config_path)
        except Exception as e:
            if initial:
                raise
            log.error("config not reloaded, keeping the old one: %s", e)
            return
        from .engine import run_action
        from .pie import PieController

        self.engine.release_all()
        self.cfg = cfg
        self.events.configure(cfg.event_log)
        self.events.record("config", touchpad={"center": cfg.touchpad.center, "radius": cfg.touchpad.radius})
        self.engine = Engine(cfg.bindings, cfg.settings, resolver=cfg.layers.resolve, observer=self.events.record)
        self.mic.command = cfg.transcribe
        self.voice.configure(cfg.agent, cfg.mic)
        if self.pie is None:
            self.pie = PieController(lambda a: run_action(a, cfg.settings.command_wait_s))
        self.pie.observer = self.events.record
        self.pie.menus, self.pie.pad = cfg.menus, cfg.touchpad
        if self.pointer is None:
            from .mouse import Mouse
            from .vpointer import VirtualPointer
            from .wheel import Wheel

            self.pointer = VirtualPointer()
            def scroll(direction):
                self.events.record("scroll", direction=direction)
                self.pointer.scroll(direction)
            def move(dx, dy):
                self.events.record("pointer", dx=dx, dy=dy)
                self.pointer.move(dx, dy)
            self.wheel = Wheel(scroll)
            self.mouse = Mouse(move)
        self.wheel.cfg, self.wheel.pad = cfg.wheel, cfg.touchpad
        self.mouse.cfg = cfg.mouse
        log.info("%s %s (%d buttons mapped, mic: %s)", "loaded" if initial else "reloaded",
                 self.config_path, len(cfg.bindings), cfg.mic)

    def buttons(self, changes: list[tuple[str, bool]], *, siri_ready=False) -> None:
        """All button changes from one remote report (presses that happened together)."""
        import time

        from .pie import CLICKPAD

        if not siri_ready:
            if any(d for _, d in changes):
                self.reload()
            for name, down in changes:
                self.events.record("button", button=name, down=down)
            if self.cfg.siri_tap is not None and self.cfg.mic != "off":
                filtered = []
                for name, down in changes:
                    if name == "back" and down and self.siri_timer is not None:
                        self._cancel_siri_click()
                    if name != "siri":
                        filtered.append((name, down))
                    elif down and not self.siri_down:
                        self.siri_down = True
                        self.siri_started = False
                        self.siri_timer = asyncio.get_running_loop().call_later(
                            self.cfg.siri_hold_ms / 1000, self._siri_hold
                        )
                    elif not down and self.siri_down:
                        self.siri_down = False
                        if self.siri_timer is not None:
                            self.siri_timer.cancel()
                            self.siri_timer = None
                            self.pie.cancel()
                            self.engine._queue("siri", self.cfg.siri_tap, "tap")
                        elif self.siri_started:
                            filtered.append((name, down))
                        self.siri_started = False
                changes = filtered

        now = time.monotonic()
        # Releases of presses we swallowed (they opened or chose a menu) go nowhere;
        # a held mouse button always comes back up, whatever opened since.
        live = []
        for name, down in changes:
            if not down and name in self.mouse_down:
                self._mouse_button(name, False)
            elif not down and name in self.swallowed:
                self.swallowed.discard(name)
            else:
                live.append((name, down))
        clicks = {n for n, d in live if d and n in CLICKPAD}

        menu_bindings = {
            name: self.cfg.layers.resolve(name, self.cfg.bindings.get(name, Binding()))
            for name, down in live if down
        }

        if self.pie.open:
            # Menus are driven by swipes: Back or the opening button cancels;
            # other clicks do nothing unless they switch menus.
            trigger = self.pie.session.trigger
            menu = self.pie.session.menu
            switch = next((n for n, d in live if d and n != trigger
                           and getattr(menu_bindings.get(n), "menu", None)), None)
            if switch is not None:
                # Another menu's button: jump straight to that menu.
                self.pie.show(menu_bindings[switch].menu, switch, now)
                self.swallowed |= clicks | {switch}
                return
            for name, down in live:
                if name in CLICKPAD and name != trigger:
                    continue
                if name == "siri" and down and self.voice.wants(menu):
                    # Holding Siri in this menu talks to the computer-use agent
                    # instead of dictating; the menu goes away so it sees the desktop.
                    self.pie.cancel()
                    on_text = None
                    if menu.voice == "pick":
                        on_text = lambda text: self.voice.pick(text, menu, self.pie.run_action)
                    elif menu.voice == "chat-search":
                        from .chat_search import search_and_open
                        on_text = lambda text: search_and_open(text, self.voice.notify)
                    self.voice.listen(on_text)
                    continue
                if name in ("back", trigger):
                    if down:
                        self.pie.cancel()
                        self.swallowed.add(name)
                        break
                else:
                    self.button(name, down)
            self.swallowed |= clicks
            return

        if any(d for _, d in live):
            self.reload()
        trigger = next((n for n, d in live if d and getattr(menu_bindings.get(n), "menu", None)), None)
        if trigger is not None:
            self.engine.release_all()  # nothing keeps repeating behind the menu
            self.pie.show(menu_bindings[trigger].menu, trigger, now)
            self.swallowed |= clicks | {trigger}
            live = [(n, d) for n, d in live if n not in clicks and n != trigger]
        for name, down in live:
            self.button(name, down)

    def _siri_hold(self) -> None:
        self.siri_timer = None
        if self.siri_down:
            self.siri_started = True
            self.buttons([("siri", True)], siri_ready=True)

    def _cancel_siri_click(self) -> None:
        if self.siri_timer is not None:
            self.siri_timer.cancel()
            self.siri_timer = None
        self.siri_down = self.siri_started = False

    def button(self, name: str, down: bool) -> None:
        if name == "siri" and self.voice.listening:
            if not down:
                self.voice.finish()
            return
        if name == "back" and down:
            was_listening = self.voice.listening
            if self.voice.stop():
                self.swallowed.add(name)
                if was_listening:
                    self.swallowed.add("siri")
                return
        if name == "siri" and self.cfg.mic == "remote":
            if down:
                self.mic.start()
            else:
                asyncio.ensure_future(self.mic.stop())
            return
        if down and getattr(self.cfg.bindings.get(name), "mouse", None):
            self._mouse_button(name, True)
            return
        self.engine.key(name, down)

    def _mouse_button(self, name: str, down: bool) -> None:
        if down:
            button = self.mouse_down[name] = self.cfg.bindings[name].mouse
        else:
            button = self.mouse_down.pop(name)
        self.events.record("mouse_button", button=button, down=down)
        self.mouse.clicked()
        self.pointer.button(button, down)

    async def _warm(self, menu) -> None:
        """Fetch a built-in source once at startup so its first real open is quick."""
        try:
            items = await menu.load()
            log.info("menu %s ready (%d items)", menu.name, len(items))
        except Exception as e:
            log.warning("menu %s: warm-up failed: %s", menu.name, e)

    def touch(self, pos) -> None:
        """Touch surface samples: swipes drive an open pie menu, the outer ring
        scrolls, and anything else moves the mouse pointer."""
        self.events.record("touch", position=pos)
        if pos is not None and self.pie.open:
            self.wheel.hold()
            self.mouse.hold()
        else:
            self.wheel.touch(pos)
            if pos is not None and self.wheel.state == "wheel":
                self.mouse.hold()
            else:
                self.mouse.touch(pos)
        self.pie.touch(pos)

    def disconnected(self) -> None:
        self.events.record("disconnect")
        self._cancel_siri_click()
        self.wheel.reset()
        self.mouse.reset()
        for name in list(self.mouse_down):
            self._mouse_button(name, False)
        self.swallowed.clear()
        self.engine.release_all()
        self.mic.cancel()
        self.voice.cancel_listen()
        self.voice.stop()
        self.pie.cancel()

    async def run(self) -> None:
        from .siri import SiriRemote

        if self.cfg.menus:
            try:
                await self.pie.start_ui()
            except Exception as e:
                log.error("pie overlay failed to start: %s", e)
        for menu in self.cfg.menus.values():
            if menu.source and menu.source.startswith("@"):
                asyncio.ensure_future(self._warm(menu))
        remote = SiriRemote(self.button, self.mic.audio, self.disconnected, self.cfg.address,
                            on_touch=self.touch, on_buttons=self.buttons)
        try:
            await remote.run()
        finally:
            self.disconnected()
            self.events.record("end")
            self.events.close()


# --- commands ------------------------------------------------------------------


def _run_loop(coro_factory) -> None:
    """Run until the coroutine ends or SIGINT/SIGTERM, then cancel it cleanly."""

    async def main():
        task = asyncio.ensure_future(coro_factory())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def cmd_run(args) -> int:
    ensure_config(args.config)
    try:
        daemon = Daemon(args.config)
    except Exception as e:
        log.error("bad config %s: %s", args.config, e)
        return 1
    _run_loop(daemon.run)
    daemon.engine.release_all()
    return 0


def cmd_learn(args) -> int:
    from .siri import SiriRemote

    print("Press buttons on the remote (wake it with any press first); Ctrl+C to quit.")

    def show(name, down):
        print(f"{name:<12} {'down' if down else 'up'}", flush=True)

    _run_loop(lambda: SiriRemote(show).run())
    return 0


def cmd_mic_test(args) -> int:
    from .mic import RemoteMic
    from .siri import SiriRemote

    mic = RemoteMic(command="superwhisper transcribe {wav}")
    done = asyncio.Event()

    def button(name, down):
        if name != "siri":
            return
        if down:
            mic.start()
        else:
            async def finish():
                if await mic.stop():
                    import json, subprocess
                    print(f"saved {mic.dir / 'dictation.wav'}")
                    out = subprocess.run(["superwhisper", "read"], capture_output=True, text=True).stdout
                    try:
                        print("heard:", json.loads(out).get("text"), flush=True)
                    except ValueError:
                        print(out, flush=True)
                    done.set()
            asyncio.ensure_future(finish())

    async def main():
        print("Hold the Siri button on the side of the remote and talk; let go to transcribe.")
        t = asyncio.ensure_future(SiriRemote(button, mic.audio).run())
        await done.wait()
        t.cancel()

    logging.getLogger("freehand").setLevel(logging.DEBUG)
    _run_loop(main)
    return 0


def cmd_t3_threads(args) -> int:
    import json

    from .t3 import client

    items = asyncio.run(client().recent_threads(8))
    print(json.dumps(items, indent=2, ensure_ascii=False))
    return 0


def cmd_agent(args) -> int:
    """Send a typed request to the agent, exactly as a spoken one would go."""
    from .agent import VoiceAgent

    ensure_config(args.config)
    cfg = load_config(args.config)
    voice = VoiceAgent(None)
    voice.configure(cfg.agent, cfg.mic)
    reply = None

    async def go():
        nonlocal reply
        reply = await voice.start(" ".join(args.request))

    _run_loop(go)
    if reply:
        print(reply)
    return 0


def cmd_quake_window(args) -> int:
    from .quake import toggle_window

    toggle_window()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="freehand", description="Map Apple TV remote buttons to anything.")
    p.add_argument("--config", type=Path, default=CONFIG_PATH, help=f"config file (default {CONFIG_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the remapper (what the systemd service does)")
    sub.add_parser("learn", help="print each button you press")
    sub.add_parser("mic-test", help="record one Siri-button hold from the remote's mic and transcribe it")
    a = sub.add_parser("agent", help="send a typed request to the computer-use agent (what holding Siri in the app switcher does)")
    a.add_argument("request", nargs="+")
    sub.add_parser("t3-threads", help="print the recent T3 threads the @t3-threads menu would show")
    sub.add_parser("quake-window", help="move the focused window into Quake or back to its original workspace")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    handler = {"run": cmd_run, "learn": cmd_learn, "mic-test": cmd_mic_test,
               "t3-threads": cmd_t3_threads, "agent": cmd_agent,
               "quake-window": cmd_quake_window}.get(args.cmd or "run")
    sys.exit(handler(args))
