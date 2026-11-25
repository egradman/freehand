import asyncio
from pathlib import Path

import pytest

from freehand import load_config
from freehand.engine import Binding, Engine, Settings, to_argv, wtype_args

FAST = Settings(hold_ms=50, repeat_delay_ms=30, repeat_ms=10)


def make(bindings):
    ran = []

    async def runner(argv):
        ran.append(argv[-1] if argv[0] == "sh" else " ".join(argv))
        await asyncio.sleep(0)

    return Engine({k: Binding.from_dict(v) for k, v in bindings.items()}, FAST, runner), ran


async def settle(t=0.0):
    await asyncio.sleep(t)
    for _ in range(10):
        await asyncio.sleep(0)


def test_wtype_chord():
    assert wtype_args("super+Tab") == ["wtype", "-M", "logo", "-k", "Tab", "-m", "logo"]
    assert wtype_args("Return") == ["wtype", "-k", "Return"]
    with pytest.raises(ValueError):
        wtype_args("hyper+x")
    assert to_argv(["a", {"type": "hi"}]) == [["sh", "-c", "a"], ["wtype", "--", "hi"]]


async def test_push_to_talk_order():
    e, ran = make({"KEY_VOICECOMMAND": {"press": "start", "release": "stop"}})
    e.key("KEY_VOICECOMMAND", True)
    e.key("KEY_VOICECOMMAND", False)  # instant release: stop must still follow start
    await settle()
    assert ran == ["start", "stop"]


async def test_tap_vs_hold():
    e, ran = make({"KEY_BACK": {"tap": "tap", "hold": "hold"}})
    e.key("KEY_BACK", True)
    await settle(0.01)
    e.key("KEY_BACK", False)
    await settle()
    assert ran == ["tap"]
    ran.clear()
    e.key("KEY_BACK", True)
    await settle(0.08)
    e.key("KEY_BACK", False)
    await settle()
    assert ran == ["hold"]


async def test_tap_without_hold_fires_on_press():
    e, ran = make({"KEY_SELECT": {"tap": "go"}})
    e.key("KEY_SELECT", True)
    await settle()
    assert ran == ["go"]


async def test_repeat():
    e, ran = make({"KEY_VOLUMEUP": {"tap": "up", "repeat": True}})
    e.key("KEY_VOLUMEUP", True)
    await settle(0.1)
    e.key("KEY_VOLUMEUP", False)
    await settle(0.05)
    n = len(ran)
    assert n >= 3 and set(ran) == {"up"}
    await settle(0.05)
    assert len(ran) == n  # stopped after release


async def test_release_all_finishes_held_button():
    e, ran = make({"KEY_VOICECOMMAND": {"press": "start", "release": "stop"}})
    e.key("KEY_VOICECOMMAND", True)
    e.release_all()
    await settle()
    assert ran == ["start", "stop"]


def test_default_config_loads():
    path = Path(__file__).parent.parent / "src/freehand/default_config.toml"
    cfg = load_config(path)
    assert cfg.mic == "computer"
    assert cfg.bindings["siri"].press == "superwhisper start"
    assert cfg.bindings["siri"].release == "superwhisper stop"
    assert "volume_up" in cfg.bindings


def click(e, name="mute"):
    e.key(name, True)
    e.key(name, False)


@pytest.mark.parametrize("count, expected", [(1, "one"), (2, "two"), (3, "three")])
async def test_multi_tap(count, expected):
    e, ran = make({"mute": {"tap": "one", "double_tap": "two", "triple_tap": "three", "click_ms": 30}})
    for _ in range(count):
        click(e)
    await settle(0.06)
    assert ran == [expected]


async def test_double_tap_fires_without_waiting_and_resets():
    e, ran = make({"mute": {"tap": "one", "double_tap": "two", "click_ms": 30}})
    click(e)
    await settle()
    assert ran == []
    click(e)
    await settle()
    assert ran == ["two"]
    click(e)
    await settle(0.06)
    assert ran == ["two", "one"]


async def test_click_then_hold_preserves_first_click():
    e, ran = make({"mute": {"tap": "one", "double_tap": "two", "hold": "hold", "click_ms": 30}})
    click(e)
    e.key("mute", True)
    await settle(0.08)
    e.key("mute", False)
    await settle(0.06)
    assert ran == ["one", "hold"]


async def test_separate_clicks_and_buttons():
    binding = {"tap": "one", "double_tap": "two", "click_ms": 30}
    e, ran = make({"mute": binding, "tv": binding})
    click(e)
    click(e, "tv")
    await settle(0.06)
    click(e)
    await settle(0.06)
    assert ran == ["one", "one", "one"]


async def test_release_all_flushes_click_timer():
    e, ran = make({"mute": {"tap": "one", "double_tap": "two", "click_ms": 30}})
    click(e)
    e.release_all()
    await settle()
    assert ran == ["one"]
    await settle(0.06)
    assert ran == ["one"]


async def test_triple_only_falls_back_to_two_single_taps():
    e, ran = make({"mute": {"tap": "one", "triple_tap": "three", "click_ms": 30}})
    click(e)
    click(e)
    await settle(0.06)
    assert ran == ["one", "one"]


def test_multi_tap_validation():
    with pytest.raises(ValueError, match="repeat"):
        Binding.from_dict({"double_tap": "two", "repeat": True})
    with pytest.raises(ValueError, match="click_ms"):
        Binding.from_dict({"click_ms": 0})
    with pytest.raises(ValueError, match="click_ms"):
        Settings(click_ms=-1)
    with pytest.raises(ValueError):
        Binding.from_dict({"triple_tap": {"unknown": "action"}})


async def test_back_double_click_and_fast_hold_repeat():
    e, ran = make({'back': {'tap': 'backspace', 'double_tap': 'escape',
                           'hold': 'backspace', 'hold_ms': 30,
                           'repeat': True, 'repeat_ms': 5, 'click_ms': 20}})
    e.settings = Settings(repeat_ms=1000)
    click(e, 'back')
    click(e, 'back')
    await settle(0.04)
    assert ran == ['escape']
    ran.clear()
    e.key('back', True)
    await settle(0.09)
    e.key('back', False)
    await settle()
    assert len(ran) >= 4 and set(ran) == {'backspace'}
    n = len(ran)
    await settle(0.04)
    assert len(ran) == n  # release neither repeats nor triggers a tap
    ran.clear()
    click(e, 'back')
    await settle(0.04)
    assert ran == ['backspace']


def test_repeat_interval_must_be_positive():
    with pytest.raises(ValueError, match='repeat_ms'):
        Binding.from_dict({'repeat_ms': 0})
