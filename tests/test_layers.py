import asyncio

import pytest

from freehand.engine import Binding, Engine
from freehand.layers import Layers, active_class


def layers(focus):
    defaults = {'mute': Binding(tap='default', double_tap='close', click_ms=10)}
    layer = Layers({'t3': {'class': 'com.t3tools.T3Code', 'buttons': {
        'mute': {'tap': 'settle'}}}}, defaults, {'mute'}, focus)
    return defaults, layer


def test_override_inherits_other_gestures_and_falls_back():
    focused = ['com.t3tools.T3Code']
    defaults, layer = layers(lambda: focused[0])
    assert layer.resolve('mute', defaults['mute']).tap == 'settle'
    assert layer.resolve('mute', defaults['mute']).double_tap == 'close'
    focused[0] = 'chrome'
    assert layer.resolve('mute', defaults['mute']) == defaults['mute']


@pytest.mark.parametrize('count, expected', [(1, 'settle'), (2, 'close')])
async def test_layer_stays_fixed_for_click_sequence(count, expected):
    focused = ['com.t3tools.T3Code']
    defaults, layer = layers(lambda: focused[0])
    ran = []
    async def runner(argv):
        ran.append(argv[-1])
    engine = Engine(defaults, runner=runner, resolver=layer.resolve)
    engine.key('mute', True)
    focused[0] = 'chrome'
    engine.key('mute', False)
    for _ in range(count - 1):
        engine.key('mute', True)
        engine.key('mute', False)
    await asyncio.sleep(0.03)
    assert ran == [expected]
    engine.key('mute', True)
    engine.key('mute', False)
    await asyncio.sleep(0.03)
    assert ran == [expected, 'default']


def test_focus_lookup_failure_uses_defaults(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('no Hyprland')
    monkeypatch.setattr('freehand.layers.subprocess.run', fail)
    assert active_class() == ''
    defaults, layer = layers(active_class)
    assert layer.resolve('mute', defaults['mute']) == defaults['mute']


def test_rejects_invalid_overrides():
    with pytest.raises(ValueError, match='unknown button'):
        Layers({'x': {'class': 'app', 'buttons': {'invalid': {'tap': 'a'}}}}, {}, {'mute'})
    with pytest.raises(ValueError, match='Siri/mouse'):
        Layers({'x': {'class': 'app', 'buttons': {'mute': {'mouse': 'left'}}}}, {}, {'mute'})


def test_menu_override_replaces_media_actions():
    default = Binding(tap='play', hold='next')
    layer = Layers({'x': {'class': 'app', 'buttons': {
        'play_pause': {'menu': 'actions'}}}}, {'play_pause': default},
        {'play_pause'}, lambda: 'app')
    assert layer.resolve('play_pause', default) == Binding(menu='actions')
    layer.focus = lambda: 'other'
    assert layer.resolve('play_pause', default) == default
