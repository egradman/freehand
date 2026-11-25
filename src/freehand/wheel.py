"""Scroll by running your thumb around the outer ring of the clickpad, like a click wheel.

A touch that lands on the ring (farther than `ring` x the pad radius from its
centre) becomes a wheel gesture until you lift: every `step_deg` of travel
around the centre is one scroll notch. Clockwise scrolls down by default.
Touches that start on the inner pad are left alone (they're swipes).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger("freehand")


@dataclass
class WheelConfig:
    enabled: bool = True
    ring: float = 1.1  # where the ring starts, as a multiple of the touchpad radius
    step_deg: float = 20.0  # travel around the ring per scroll notch
    clockwise: str = "down"  # what clockwise does: "down" or "up"

    @classmethod
    def from_dict(cls, d: dict) -> "WheelConfig":
        unknown = set(d) - {"enabled", "ring", "step_deg", "clockwise"}
        if unknown:
            raise ValueError(f"unknown fields {sorted(unknown)}")
        cfg = cls(**d)
        if cfg.clockwise not in ("down", "up"):
            raise ValueError('clockwise must be "down" or "up"')
        if not 2 <= cfg.step_deg <= 180:
            raise ValueError("step_deg must be between 2 and 180")
        return cfg


class Wheel:
    def __init__(self, scroll, cfg: WheelConfig | None = None, pad=None):
        self.scroll = scroll  # callable(notches: int), positive = down
        self.cfg = cfg or WheelConfig()
        self.pad = pad  # pie.Touchpad: centre and radius of the inner pad
        self.reset()

    def reset(self) -> None:
        self.state = "idle"  # idle (no touch) | wheel | ignore (until lift)
        self.angle = 0.0
        self.travel = 0.0

    def hold(self) -> None:
        """Ignore the current touch until the thumb lifts (a pie menu has it)."""
        self.state = "ignore"

    def _polar(self, pos) -> tuple[float, float]:
        dx, dy = pos[0] - self.pad.center[0], pos[1] - self.pad.center[1]
        # Compass angle, 0 = up, clockwise; same convention as Touchpad.vector.
        return math.hypot(dx, dy) / self.pad.radius, math.degrees(math.atan2(dx, dy)) % 360

    def touch(self, pos) -> None:
        """Feed a touch sample (None = lifted)."""
        if pos is None:
            if self.state == "wheel":
                log.debug("wheel: lifted")
            self.reset()
            return
        if not self.cfg.enabled or self.pad is None or self.state == "ignore":
            return
        r, angle = self._polar(pos)
        if self.state == "idle":
            if r < self.cfg.ring:
                self.state = "ignore"
                return
            self.state, self.angle, self.travel = "wheel", angle, 0.0
            log.debug("wheel: thumb on the ring (%.2f x radius)", r)
            return
        if r < 0.5:
            return  # too near the centre for a steady angle; wait for the thumb to come back out
        delta = (angle - self.angle + 180) % 360 - 180
        self.angle = angle
        self.travel += delta
        notches = int(self.travel / self.cfg.step_deg)  # toward zero
        if notches:
            self.travel -= notches * self.cfg.step_deg
            self.scroll(notches if self.cfg.clockwise == "down" else -notches)
