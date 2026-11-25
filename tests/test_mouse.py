import pytest

from freehand.mouse import Mouse, MouseConfig


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def mouse(**cfg):
    out, clock = [], Clock()
    cfg.setdefault("smoothing", 0.0)
    m = Mouse(lambda dx, dy: out.append((dx, dy)), MouseConfig(**cfg), clock)
    return m, out, clock


def total(out):
    return sum(dx for dx, _ in out), sum(dy for _, dy in out)


def test_touch_down_does_not_move_then_motion_is_relative():
    m, out, _ = mouse(speed=1.0, accel=0.0)
    m.touch((500, 500, 25))  # wherever the thumb lands, the pointer stays put
    assert out == []
    m.touch((510, 500, 25))
    m.touch((520, 495, 25))
    assert total(out) == (20, 5)  # touch y is up, screen y is down


def test_lifting_and_landing_elsewhere_does_not_jump():
    m, out, _ = mouse(speed=1.0, accel=0.0)
    m.touch((0, 0, 25))
    m.touch((10, 0, 25))
    m.touch(None)
    m.touch((-300, 200, 25))
    m.touch((-290, 200, 25))
    assert total(out) == (20, 0)


def test_fast_strokes_go_further_but_capped():
    m, out, _ = mouse(speed=1.0, accel=0.1, max_gain=3.0)
    m.touch((0, 0, 25))
    m.touch((5, 0, 25))  # slow: gain 1.5
    assert out[-1][0] == pytest.approx(7.5)
    m.touch((105, 0, 25))  # fast: gain would be 11, capped at 3
    assert out[-1][0] == pytest.approx(300)


def test_smoothing_spreads_motion_over_samples():
    m, out, _ = mouse(speed=1.0, accel=0.0, smoothing=0.5)
    m.touch((0, 0, 25))
    m.touch((100, 0, 25))
    assert out[-1][0] == pytest.approx(50)
    m.touch((100, 0, 25))
    assert total(out)[0] == pytest.approx(75)


def test_pointer_holds_still_while_a_click_settles():
    m, out, clock = mouse(speed=1.0, accel=0.0, click_settle_ms=150)
    m.touch((0, 0, 25))
    m.clicked()
    m.touch((8, 0, 25))  # the thumb rolls as it presses
    assert out == []
    clock.t += 0.2
    m.touch((18, 0, 25))  # dragging afterwards moves from where it settled
    assert total(out) == (10, 0)


def test_hold_ignores_until_lift():
    m, out, _ = mouse()
    m.touch((0, 0, 25))
    m.hold()
    m.touch((100, 0, 25))
    assert out == []
    m.touch(None)
    m.touch((0, 0, 25))
    m.touch((10, 0, 25))
    assert out


def test_disabled():
    m, out, _ = mouse(enabled=False)
    m.touch((0, 0, 25))
    m.touch((100, 0, 25))
    assert out == []


@pytest.mark.parametrize("bad", [{"speed": 0}, {"smoothing": 1}, {"max_gain": 0.5}, {"ring": 1}])
def test_config_errors(bad):
    with pytest.raises((ValueError, TypeError)):
        MouseConfig.from_dict(bad)
