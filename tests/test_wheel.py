import math

import pytest

from freehand.pie import Touchpad
from freehand.wheel import Wheel, WheelConfig

PAD = Touchpad(center=(100.0, -50.0), radius=400.0)


def at(deg, r=1.3):
    """Point at compass angle `deg` (0 = up, clockwise), `r` x the pad radius from its centre."""
    a = math.radians(deg)
    return 100 + r * 400 * math.sin(a), -50 + r * 400 * math.cos(a), 25


def wheel(**cfg):
    out = []
    return Wheel(out.append, WheelConfig(**cfg), PAD), out


def sweep(w, start, end, r=1.3, step=5):
    for deg in range(start, end + (step if end >= start else -step), step if end >= start else -step):
        w.touch(at(deg, r))


def test_clockwise_scrolls_down_one_notch_per_step():
    w, out = wheel(step_deg=20)
    sweep(w, 0, 90)
    assert sum(out) == 4 and all(n > 0 for n in out)


def test_counter_clockwise_scrolls_up_across_the_top():
    w, out = wheel(step_deg=20)
    sweep(w, 30, -60)  # crosses 0/360 without a jump
    assert sum(out) == -4


def test_clockwise_up_flips_direction():
    w, out = wheel(step_deg=20, clockwise="up")
    sweep(w, 0, 60)
    assert sum(out) == -3


def test_inner_pad_touch_is_ignored_even_if_it_moves_out_to_the_ring():
    w, out = wheel()
    w.touch(at(0, r=0.5))
    sweep(w, 0, 180)
    assert out == []
    w.touch(None)
    sweep(w, 0, 60)  # a fresh touch on the ring works
    assert sum(out) == 3


def test_partial_travel_carries_over_and_reversal_cancels():
    w, out = wheel(step_deg=20)
    sweep(w, 0, 15)
    assert out == []
    sweep(w, 15, 25)
    assert out == [1]
    sweep(w, 25, -20)
    assert sum(out) == -1


def test_dipping_toward_the_centre_does_not_jump():
    w, out = wheel(step_deg=20)
    sweep(w, 0, 40)
    w.touch(at(220, r=0.2))  # near the centre: angle is noise, ignored
    sweep(w, 40, 60)
    assert sum(out) == 3


def test_hold_ignores_until_lift():
    w, out = wheel()
    w.touch(at(0))
    w.hold()
    sweep(w, 0, 90)
    assert out == []
    w.touch(None)
    sweep(w, 0, 40)
    assert sum(out) == 2


def test_disabled():
    w, out = wheel(enabled=False)
    sweep(w, 0, 180)
    assert out == []


@pytest.mark.parametrize("bad", [{"clockwise": "left"}, {"step_deg": 0}, {"speed": 3}])
def test_config_errors(bad):
    with pytest.raises((ValueError, TypeError)):
        WheelConfig.from_dict(bad)
