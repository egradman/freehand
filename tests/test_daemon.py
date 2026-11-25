import math

import pytest

from freehand import Daemon

CONFIG = """
[dictation]
mic = "off"

[touchpad]
center = [0, 0]
radius = 400

[buttons.down]
menu = "apps"

[buttons.select]
mouse = "left"

[buttons.play_pause]
tap = "echo enter"

[buttons.volume_up]
tap = "echo vol"

[menus.apps]
items = [
  { label = "Top", action = "echo top", position = "n" },
  { label = "Right", action = "echo right", position = "e" },
  { label = "Bottom", action = "echo bottom", position = "s" },
  { label = "Left", action = "echo left", position = "w" },
]
"""


@pytest.fixture
def daemon(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    d = Daemon(path)
    d.sent, d.ran, d.keys = [], [], []
    d.pie.send = d.sent.append

    async def run_action(action):
        d.ran.append(action)

    d.pie.run_action = run_action
    d.engine.key = lambda name, down: d.keys.append((name, down))
    d.moved, d.clicks = [], []
    d.mouse.move = lambda dx, dy: d.moved.append((dx, dy))
    d.pointer.button = lambda name, down: d.clicks.append((name, down))
    return d


async def settle():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


def swipe(d, dx, dy, origin=(20, -10)):
    d.pie.touch((origin[0], origin[1], 25))
    d.pie.touch((origin[0] + dx // 2, origin[1] + dy // 2, 25))
    d.pie.touch((origin[0] + dx, origin[1] + dy, 25))
    d.pie.touch(None)


async def test_click_open_then_swipe(daemon):
    d = daemon
    d.pie.touch((0, -350, 25))  # thumb on the ring at the bottom, clicking Down
    d.buttons([("select", True), ("down", True)])
    assert d.pie.open and d.keys == [] and d.clicks == []  # the select in the same report did NOT click
    d.buttons([("select", False), ("down", False)])
    d.pie.touch(None)  # lifting the opening thumb doesn't choose
    assert d.pie.open and d.keys == []
    swipe(d, 0, 250)  # up
    await settle()
    assert not d.pie.open and d.ran == ["echo top"]


async def test_clicks_do_nothing_while_open(daemon):
    d = daemon
    d.buttons([("down", True)])
    d.buttons([("down", False)])
    d.buttons([("select", True)])
    d.buttons([("up", True)])
    d.buttons([("select", False), ("up", False)])
    assert d.pie.open and d.keys == [] and d.clicks == [] and d.ran == []
    swipe(d, 250, 0)  # right
    await settle()
    assert d.ran == ["echo right"]


@pytest.mark.parametrize("trigger", ["up", "down", "left", "right", "play_pause"])
async def test_opening_button_closes_menu(tmp_path, trigger):
    path = tmp_path / "config.toml"
    config = CONFIG.replace('[buttons.play_pause]\ntap = "echo enter"\n', "")
    path.write_text(config.replace("[buttons.down]", f"[buttons.{trigger}]"))
    d = Daemon(path)
    sent, keys, clicks = [], [], []
    d.pie.send = sent.append
    d.engine.key = lambda name, down: keys.append((name, down))
    d.pointer.button = lambda name, down: clicks.append((name, down))

    d.buttons([("select", True), (trigger, True)])
    d.buttons([("select", False), (trigger, False)])
    assert d.pie.open
    d.buttons([("select", True), (trigger, True)])
    assert not d.pie.open and {"cmd": "hide"} in sent
    d.buttons([("select", False), (trigger, False)])
    assert keys == [] and clicks == [] and not d.swallowed


async def test_back_cancels_and_other_buttons_pass(daemon):
    d = daemon
    d.buttons([("down", True)])
    d.buttons([("volume_up", True)])
    assert d.keys == [("volume_up", True)]
    d.buttons([("back", True)])
    assert not d.pie.open and {"cmd": "hide"} in d.sent
    d.buttons([("back", False)])  # the cancelling press doesn't also send Backspace
    assert ("back", False) not in d.keys
    await settle()
    assert d.ran == []


SWITCH_CONFIG = CONFIG + """
[buttons.left]
menu = "more"

[menus.more]
items = [{ label = "Other", action = "echo other", position = "n" }]
"""


async def test_other_menus_button_switches_menus(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SWITCH_CONFIG)
    d = Daemon(path)
    d.sent, d.ran = [], []
    d.pie.send = d.sent.append

    async def run_action(action):
        d.ran.append(action)

    d.pie.run_action = run_action
    d.buttons([("down", True)])
    d.buttons([("down", False)])
    assert d.pie.session.menu.name == "apps"
    d.buttons([("left", True)])  # a clickpad button that opens another menu
    assert d.pie.open and d.pie.session.menu.name == "more"
    d.buttons([("left", False)])
    d.buttons([("down", True)])  # and back again
    assert d.pie.session.menu.name == "apps"
    d.buttons([("down", False)])
    swipe(d, 0, 250)
    await settle()
    assert d.ran == ["echo top"]


async def test_wheel_scrolls_when_no_menu_is_open(daemon):
    d = daemon
    scrolled = []
    d.wheel.scroll = scrolled.append
    for deg in range(0, 95, 5):  # clockwise around the ring
        a = math.radians(deg)
        d.touch((int(500 * math.sin(a)), int(500 * math.cos(a)), 25))
    d.touch(None)
    assert sum(scrolled) == 4
    d.buttons([("down", True)])  # with a menu open, the ring belongs to the menu
    scrolled.clear()
    for deg in range(0, 95, 5):
        a = math.radians(deg)
        d.touch((int(500 * math.sin(a)), int(500 * math.cos(a)), 25))
    assert scrolled == []


async def test_inner_pad_moves_the_mouse_but_not_the_ring_or_an_open_menu(daemon):
    d = daemon
    for x in range(0, 100, 10):  # inner pad
        d.touch((x, 0, 25))
    d.touch(None)
    assert d.moved and sum(dx for dx, _ in d.moved) > 0
    d.moved.clear()
    for deg in range(0, 95, 5):  # outer ring: scrolls instead
        a = math.radians(deg)
        d.touch((int(500 * math.sin(a)), int(500 * math.cos(a)), 25))
    d.touch(None)
    assert d.moved == []
    d.touch((0, -100, 25))
    d.buttons([("down", True)])  # the clicking thumb opens a menu...
    for x in range(0, 100, 10):
        d.touch((x, -100, 25))  # ...and then belongs to it
    assert d.moved == []


async def test_select_is_a_mouse_button_that_can_drag(daemon):
    d = daemon
    d.buttons([("select", True)])
    assert d.clicks == [("left", True)]
    d.mouse.settle_until = 0  # past the click settle time
    d.touch((0, 0, 25))
    d.touch((50, 0, 25))
    assert d.moved  # dragging
    d.buttons([("select", False)])
    assert d.clicks == [("left", True), ("left", False)]
    assert ("select", True) not in d.keys


async def test_mouse_button_comes_up_even_if_a_menu_opened_meanwhile(daemon):
    d = daemon
    d.buttons([("select", True)])
    d.buttons([("down", True)])
    assert d.pie.open
    d.buttons([("select", False)])
    assert d.clicks == [("left", True), ("left", False)]


async def test_disconnect_releases_a_held_mouse_button(daemon):
    d = daemon
    d.buttons([("select", True)])
    d.disconnected()
    assert d.clicks == [("left", True), ("left", False)]


@pytest.mark.parametrize('mic', ['computer', 'remote'])
async def test_siri_click_enter_and_hold_dictates(tmp_path, mic):
    import asyncio
    path = tmp_path / 'config.toml'
    path.write_text(f'''[dictation]
mic = "{mic}"
tap = {{ keys = "Return" }}
hold_ms = 20
[buttons.tv]
tap = {{ keys = "Return" }}
''')
    d = Daemon(path)
    ran = []
    async def runner(argv):
        ran.append(argv)
    d.engine.runner = runner
    d.mic.start = lambda: ran.append('start remote')
    async def stop():
        ran.append('stop remote')
    d.mic.stop = stop
    d.buttons([('siri', True)])
    d.buttons([('siri', False)])
    await settle()
    await asyncio.sleep(0.04)
    assert ran == [['wtype', '-k', 'Return']]
    ran.clear()
    d.buttons([('siri', True)])
    await asyncio.sleep(0.04)
    d.buttons([('siri', False)])
    await settle()
    assert ran == (['start remote', 'stop remote'] if mic == 'remote' else
                   [['sh', '-c', 'superwhisper start'], ['sh', '-c', 'superwhisper stop']])
    ran.clear()
    d.buttons([('tv', True), ('tv', False)])
    await settle()
    assert ran == [['wtype', '-k', 'Return']]


@pytest.mark.parametrize('cancel', ['disconnect', 'back'])
async def test_pending_siri_click_can_be_cancelled(daemon, cancel):
    import asyncio
    d = daemon
    d.cfg.mic = 'computer'
    d.cfg.siri_tap = {'keys': 'Return'}
    d.cfg.siri_hold_ms = 20
    d.buttons([('siri', True)])
    if cancel == 'disconnect':
        d.disconnected()
    else:
        d.buttons([('back', True)])
    d.buttons([('siri', False)])
    await asyncio.sleep(0.04)
    assert not any(name == 'siri' for name, _ in d.keys)
    assert 'siri' not in d.engine.state


async def test_application_menu_open_switch_and_cancel(daemon):
    from freehand.layers import Layers
    d = daemon
    focused = ['special']
    d.cfg.layers = Layers({'special': {'class': 'special', 'buttons': {
        'play_pause': {'menu': 'apps'}}}}, d.cfg.bindings,
        set(d.cfg.bindings), lambda: focused[0])
    d.buttons([('play_pause', True)])
    d.buttons([('play_pause', False)])
    assert d.pie.open and d.keys == []
    assert d.pie.session.trigger == 'play_pause'
    focused[0] = 'other'
    d.buttons([('play_pause', True)])
    d.buttons([('play_pause', False)])
    assert not d.pie.open and d.keys == []
    d.buttons([('play_pause', True)])
    d.buttons([('play_pause', False)])
    assert not d.pie.open
    assert d.keys == [('play_pause', True), ('play_pause', False)]
    focused[0] = 'special'
    d.buttons([('down', True)])
    d.buttons([('down', False)])
    d.buttons([('play_pause', True)])
    assert d.pie.session.trigger == 'play_pause'
    d.buttons([('play_pause', False), ('back', True)])
    assert not d.pie.open
    await settle()


def test_missing_application_menu_rejected(tmp_path):
    from freehand import load_config
    path = tmp_path / 'config.toml'
    path.write_text(CONFIG + '\n[applications.app]\nclass = "app"\n'
                    '[applications.app.buttons.play_pause]\nmenu = "missing"\n')
    with pytest.raises(ValueError, match='no .*menus.missing'):
        load_config(path)
