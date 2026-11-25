import math

import pytest

from freehand.pie import Item, Menu, PieSession, Touchpad, layout, slice_at

PAD = Touchpad(center=(0.0, 0.0), radius=400.0, swipe=0.4, preview=0.15, hysteresis_deg=6.0)


def pt(deg, length, origin=(0, 0)):
    """Point `length` (fraction of radius) from origin at compass angle `deg`."""
    a = math.radians(deg)
    return int(origin[0] + length * 400 * math.sin(a)), int(origin[1] + length * 400 * math.cos(a)), 25


def swipe(s, deg, length=0.6, origin=(50, -30)):
    """Touch down at origin, move out in steps, lift. Returns the lift result."""
    for f in (0.0, 0.3, 0.6, 1.0):
        assert s.touch(pt(deg, length * f, origin)) is None
    return s.touch(None)


def menu8(**positions):
    items = [Item(label, action=f"echo {label}", position=pos) for label, pos in positions.items()]
    m = Menu("apps", items=items)
    return m, layout(m.items, 8)


def session(touching=False):
    m, slots = menu8(top="n", right="e", bottom="s", left="w", corner="ne")
    return PieSession(m, slots, "down", PAD, touching=touching)


def test_slice_at():
    assert slice_at(0, 8) == 0
    assert slice_at(22, 8) == 0
    assert slice_at(23, 8) == 1
    assert slice_at(180, 8) == 4
    assert slice_at(340, 8) == 0


def test_layout_positions_and_fill():
    a, b, c = Item("a", position="se"), Item("b"), Item("c", position=0)
    slots = layout([a, b, c], 8)
    assert slots[3] is a and slots[0] is c and slots[1] is b
    with pytest.raises(ValueError):
        layout([Item("x", position="n"), Item("y", position="n")], 8)
    with pytest.raises(ValueError):
        layout([Item("x", position="up")], 8)
    with pytest.raises(ValueError):
        layout([Item(str(i)) for i in range(9)], 8)


@pytest.mark.parametrize("deg,label", [(0, "top"), (45, "corner"), (90, "right"), (180, "bottom"), (270, "left")])
def test_swipe_chooses_direction(deg, label):
    s = session()
    assert swipe(s, deg) == "choose"
    assert s.chosen().label == label


def test_swipe_from_anywhere_is_relative():
    s = session()
    assert swipe(s, 90, origin=(-300, 200)) == "choose"
    assert s.chosen().label == "right"


def test_short_swipe_does_nothing():
    s = session()
    assert swipe(s, 90, length=0.25) is None
    assert s.highlight is None


def test_swipe_to_empty_slot_does_nothing():
    s = session()
    assert swipe(s, 135) is None  # SE is empty


def test_preview_lights_up_before_lift():
    s = session()
    s.touch(pt(0, 0, (0, 0)))
    s.touch(pt(90, 0.1))
    assert s.highlight is None  # below preview distance
    s.touch(pt(90, 0.3))
    assert s.highlight == 2  # E
    s.touch(pt(90, 0.05))  # slid back near the start: changed my mind
    assert s.highlight is None
    assert s.touch(None) is None


def test_touch_already_down_when_opened_is_ignored():
    s = session(touching=True)
    s.touch(pt(180, 0.0, (0, 300)))
    s.touch(pt(180, 0.8, (0, 300)))
    assert s.highlight is None
    assert s.touch(None) is None  # lifting the opening thumb doesn't choose
    assert swipe(s, 0) == "choose"  # the next touch works
    assert s.chosen().label == "top"


def test_menu_validation():
    with pytest.raises(ValueError):
        Menu.from_dict("x", {})
    with pytest.raises(ValueError):
        Menu.from_dict("x", {"items": [{"content": "no label"}]})
    with pytest.raises(ValueError):
        Menu.from_dict("x", {"items": [{"label": "a", "position": "nnw"}]})
    m = Menu.from_dict("x", {"items": [{"label": "a", "content": "big\ntext", "action": {"type": "hi"}}]})
    assert m.items[0].for_ui() == {"label": "a", "content": "big\ntext", "markup": False, "submenu": False}


async def test_source_menu():
    m = Menu.from_dict("x", {"source": """echo '[{"label": "dyn", "content": "c", "position": "s"}]'"""})
    items = await m.load()
    assert layout(items, 8)[4].label == "dyn"


async def test_gchat_source(tmp_path, monkeypatch):
    import freehand.pie as pie

    fake = tmp_path / "gchat.py"
    fake.write_text('import json, sys\nassert "--unread" in sys.argv and "--include-drafts" not in sys.argv\nprint(json.dumps([\n'
                    '  {"id": "space/EXAMPLE", "title": "Team", "when": "Now", "preview": "x" * 300, "unread": True},\n'
                    '  {"id": "dm/60zB9q", "title": "Sam", "when": "41 min", "preview": "hi", "unread": True},\n'
                    '  {"id": "dm/Read1", "title": "Old", "when": "2 h", "preview": "seen", "unread": False},\n'
                    '  {"id": "space/bad id; rm -rf ~", "title": "Evil", "when": "", "preview": "", "unread": True},\n'
                    '  {"id": "draft/abc123", "title": "Unfinished", "when": "Thu", "preview": "hello", "unread": False, "draft": True},\n'
                    ']))\n')
    monkeypatch.setattr(pie, "GCHAT_HELPER", fake)
    items = await pie._gchat(8)
    assert [i["label"] for i in items] == ["Team", "Sam"]  # read chats and the malformed id are dropped
    assert items[0]["content"].startswith("Now\n") and items[0]["content"].endswith("…")
    assert items[1]["action"].endswith("open dm/60zB9q")


async def test_dwell_opens_submenu_without_choosing_on_lift():
    import asyncio
    from freehand.pie import PieController
    ran, sent = [], []
    async def run(action):
        ran.append(action)
    child = Menu('child', items=[Item('Child', action='child')])
    parent = Menu('parent', items=[Item('Parent', action='parent', submenu='child', dwell_ms=35)])
    c = PieController(run, {'parent': parent, 'child': child}, Touchpad(center=(0, 0), radius=400))
    c.send = sent.append
    c.show('parent', 'left', 0)
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    await asyncio.sleep(.09)
    assert c.session.menu is child
    assert any(m['cmd'] == 'dwell' and 0 < m['progress'] < 1 for m in sent)
    c.touch(None)
    assert c.open and not ran
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    c.touch(None)
    await asyncio.sleep(0)
    assert ran == ['child']
    assert not c.open and c._dwell is None


async def test_dwell_resets_on_move_and_stops_on_cancel_or_selection():
    import asyncio
    from freehand.pie import PieController
    ran = []
    async def run(action):
        ran.append(action)
    child = {'items': [{'label': 'Child'}]}
    parent = Menu('parent', items=[Item('Parent', action='parent', submenu=child, dwell_ms=80)])
    c = PieController(run, {'parent': parent}, Touchpad(center=(0, 0), radius=400))
    c.send = lambda msg: None
    c.show('parent', 'left', 0)
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    await asyncio.sleep(.04)
    c.touch((250, 0, 20))  # empty slice cancels dwell
    assert c._dwell is None
    c.touch((0, 250, 20))
    await asyncio.sleep(.05)
    assert c.session.menu is parent
    c.touch(None)  # lifting before the timeout still runs the normal action
    await asyncio.sleep(.1)
    assert ran == ['parent'] and not c.open
    c.show('parent', 'left', 0)
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    c.cancel()
    await asyncio.sleep(.1)
    assert not c.open and c._dwell is None


@pytest.mark.parametrize('patch', [{'dwell_ms': 0}, {'submenu': 42}, {'submenu': {'items': []}}])
def test_invalid_dwell_item(patch):
    with pytest.raises(ValueError):
        Item.from_dict({'label': 'x', **patch})


async def test_dwell_action_opens_external_picker_without_pasting():
    import asyncio
    from freehand.pie import PieController
    ran = []
    async def run(action):
        ran.append(action)
    item = Item.from_dict({'label': 'Paste', 'action': 'paste', 'dwell_action': 'history', 'dwell_ms': 35})
    c = PieController(run, {'keys': Menu('keys', items=[item])}, Touchpad(radius=400))
    c.send = lambda msg: None
    c.show('keys', 'down', 0)
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    await asyncio.sleep(.09)
    assert not c.open and ran == ['history']
    c.touch(None)
    await asyncio.sleep(0)
    assert ran == ['history']
    c.show('keys', 'down', 0)
    c.touch((0, 0, 20))
    c.touch((0, 250, 20))
    c.touch(None)
    await asyncio.sleep(.09)
    assert ran == ['history', 'paste']
