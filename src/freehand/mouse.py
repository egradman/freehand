"""Move the mouse pointer by sliding a thumb on the clickpad, like a laptop trackpad.

Motion is relative: touching down does nothing, sliding moves the pointer by
how far the thumb travelled since the last sample, scaled by `speed` and sped
up for quick strokes (`accel`, capped at `max_gain`). Lift and touch down again
to keep going. Clicking the pad nudges the thumb, so motion pauses for
`click_settle_ms` after a mouse button goes down or up.

The daemon only feeds this touches nothing else wants: not while a pie menu is
open, and not touches that start on the outer ring (those scroll).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class MouseConfig:
    enabled: bool = True
    speed: float = 0.8  # screen pixels per touch unit for slow, careful moves
    accel: float = 0.04  # extra gain per touch unit moved in one sample (~60 samples/s)
    max_gain: float = 4.0  # acceleration never multiplies speed by more than this
    smoothing: float = 0.4  # 0 = raw samples; nearer 1 = steadier but laggier
    click_settle_ms: float = 150.0

    @classmethod
    def from_dict(cls, d: dict) -> "MouseConfig":
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        cfg = cls(**d)
        if cfg.speed <= 0:
            raise ValueError("speed must be positive")
        if cfg.accel < 0 or cfg.max_gain < 1:
            raise ValueError("accel must be >= 0 and max_gain >= 1")
        if not 0 <= cfg.smoothing < 1:
            raise ValueError("smoothing must be at least 0 and below 1")
        return cfg


class Mouse:
    def __init__(self, move, cfg: MouseConfig | None = None, clock=time.monotonic):
        self.move = move  # callable(dx, dy) in screen pixels, y down
        self.cfg = cfg or MouseConfig()
        self.clock = clock
        self.settle_until = 0.0
        self.reset()

    def reset(self) -> None:
        self.state = "idle"  # idle (no touch) | move | ignore (until lift)
        self.pos: tuple[float, float] | None = None  # smoothed thumb position

    def hold(self) -> None:
        """Ignore the current touch until the thumb lifts (a menu or the wheel has it)."""
        self.state = "ignore"

    def clicked(self) -> None:
        """A mouse button went down or up: keep the pointer still while the thumb settles."""
        self.settle_until = self.clock() + self.cfg.click_settle_ms / 1000

    def touch(self, pos) -> None:
        """Feed a touch sample (None = lifted)."""
        if pos is None:
            self.reset()
            return
        if not self.cfg.enabled or self.state == "ignore":
            return
        x, y = float(pos[0]), float(pos[1])
        if self.state == "idle":
            self.state, self.pos = "move", (x, y)
            return
        k = self.cfg.smoothing
        px, py = self.pos
        sx, sy = k * px + (1 - k) * x, k * py + (1 - k) * y
        self.pos = (sx, sy)
        if self.clock() < self.settle_until:
            return  # track the thumb, but don't move
        dx, dy = sx - px, sy - py
        gain = self.cfg.speed * min(self.cfg.max_gain, 1 + self.cfg.accel * math.hypot(dx, dy))
        # Touch y grows upward (see Touchpad.vector); screen y grows downward.
        self.move(dx * gain, -dy * gain)
