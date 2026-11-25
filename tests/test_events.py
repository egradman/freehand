import asyncio
import json

import pytest

from freehand import Daemon, load_config
from freehand.engine import Binding, Engine, Settings
from freehand.events import EventLog


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_recording_sessions_append_and_reload(tmp_path):
    path = tmp_path / 'events.jsonl'
    recorder = EventLog()
    recorder.record('ignored')
    assert not path.exists()
    recorder.configure(path)
    recorder.record('button', button='tv', down=True)
    recorder.configure(path)
    recorder.record('button', button='tv', down=False)
    recorder.close()
    recorder.configure(path)
    recorder.close()
    data = rows(path)
    assert [r['kind'] for r in data] == ['session', 'button', 'button', 'session']
    assert data[0]['session'] == data[2]['session'] != data[3]['session']
    assert data[0]['t_ms'] <= data[1]['t_ms'] <= data[2]['t_ms']
    assert data[0]['unix_ms'] > 0


async def test_recognized_double_and_hold(tmp_path):
    recorder = EventLog()
    path = tmp_path / 'events.jsonl'
    recorder.configure(path)
    async def runner(argv):
        pass
    engine = Engine({'tv': Binding(tap='one', double_tap='two', hold='held')},
                    Settings(hold_ms=10), runner=runner, observer=recorder.record)
    for _ in range(2):
        engine.key('tv', True)
        engine.key('tv', False)
    await asyncio.sleep(0)
    engine.key('tv', True)
    await asyncio.sleep(.03)
    engine.key('tv', False)
    await asyncio.sleep(0)
    recorder.close()
    assert [e['gesture'] for e in rows(path) if e['kind'] == 'action'] == ['double_tap', 'hold']


async def test_raw_events_before_menu_and_siri_filter(tmp_path, monkeypatch):
    from freehand.pie import PieController
    monkeypatch.setattr(PieController, 'send', lambda *args: None)
    path = tmp_path / 'config.toml'
    path.write_text('''[recording]
event_log = "events.jsonl"
[dictation]
mic = "computer"
tap = { keys = "Return" }
hold_ms = 10
[buttons.tv]
menu = "apps"
[menus.apps]
items = [{ label = "Example", action = "true" }]
''')
    d = Daemon(path)
    async def runner(argv):
        pass
    d.engine.runner = runner
    d.buttons([('siri', True)])
    await asyncio.sleep(.03)
    d.buttons([('siri', False)])
    d.buttons([('tv', True), ('tv', False)])
    d.disconnected()
    await asyncio.sleep(0)
    d.events.close()
    data = rows(tmp_path / 'events.jsonl')
    assert [(e['button'], e['down']) for e in data if e['kind'] == 'button'] == [
        ('siri', True), ('siri', False), ('tv', True), ('tv', False)]
    assert any(e['kind'] == 'disconnect' for e in data)


def test_bad_recording_config(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[recording]\nevent_log = true\n')
    with pytest.raises(ValueError, match='path string'):
        load_config(path)


def test_unwritable_recording_does_not_break_input(tmp_path):
    recorder = EventLog()
    recorder.configure(tmp_path)  # directory, not a file
    recorder.record('button', button='tv', down=True)
    assert recorder.stream is None
