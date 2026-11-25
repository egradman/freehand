import asyncio
from pathlib import Path

import pytest

from freehand import Daemon, load_config

CONFIG = """
[dictation]
mic = "computer"

[touchpad]
center = [0, 0]
radius = 400

[buttons.down]
menu = "apps"

[buttons.up]
menu = "threads"

[menus.apps]
items = [{ label = "Top", action = "echo top", position = "n" }]

[menus.threads]
items = [{ label = "Top", action = "echo t", position = "n" }]

[agent]
menus = ["apps"]
command = "printf 'did: %s' {text}"
"""


class FakeRecorder:
    def __init__(self, wav="/tmp/x.wav"):
        self.started = self.cancelled = False
        self.wav = wav

    def start(self):
        self.started = True

    async def finish(self):
        return Path(self.wav) if self.wav else None

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("freehand.agent.STATE_DIR", tmp_path / "agent")
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    d = Daemon(path)
    d.sent, d.keys, d.notes, d.recorders = [], [], [], []
    d.pie.send = d.sent.append
    d.engine.key = lambda name, down: d.keys.append((name, down))
    v = d.voice

    async def notify(summary, body="", urgency="normal"):
        d.notes.append((summary, body))

    async def transcribe(wav):
        return "open my calendar"

    def new_recorder():
        r = FakeRecorder()
        d.recorders.append(r)
        return r

    v.notify, v.transcribe, v.new_recorder = notify, transcribe, new_recorder
    return d


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_siri_in_app_switcher_goes_to_agent(daemon):
    d = daemon
    d.buttons([("down", True)])
    d.buttons([("down", False)])
    assert d.pie.open
    d.buttons([("siri", True)])
    assert not d.pie.open  # the menu gets out of the way
    assert d.sent[-1] == {"cmd": "hide"}
    assert d.voice.listening and d.recorders[0].started
    assert d.keys == []  # no superwhisper start
    d.buttons([("siri", False)])
    assert not d.voice.listening and d.keys == []
    await settle()
    reply = await d.voice.task
    assert reply == "did: open my calendar"
    assert d.notes[-1] == ("Done", "did: open my calendar")


async def test_siri_in_other_menu_dictates_as_usual(daemon):
    d = daemon
    d.buttons([("up", True)])
    d.buttons([("siri", True)])
    assert d.pie.open and not d.voice.listening
    assert d.keys == [("siri", True)]


async def test_siri_without_menu_dictates_as_usual(daemon):
    d = daemon
    d.buttons([("siri", True)])
    d.buttons([("siri", False)])
    assert d.keys == [("siri", True), ("siri", False)] and d.voice.task is None


async def test_nothing_heard(daemon):
    d = daemon
    d.voice.new_recorder = lambda: FakeRecorder(wav=None)
    d.buttons([("down", True)])
    d.buttons([("siri", True)])
    d.buttons([("siri", False)])
    await settle()
    assert d.voice.task is None and d.notes[-1][1] == "Didn't hear anything."


async def test_back_stops_running_agent(daemon):
    d = daemon
    d.voice.cfg.command = "sleep 30 # {text}"
    task = d.voice.start("wait")
    await asyncio.sleep(0.05)
    assert d.voice.running
    d.buttons([("back", True)])
    d.buttons([("back", False)])
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not d.voice.running
    assert ("back", True) not in d.keys
    d.buttons([("back", True)])  # with nothing running, Back is Back again
    assert d.keys[-1] == ("back", True)


async def test_disconnect_drops_recording(daemon):
    d = daemon
    d.buttons([("down", True)])
    d.buttons([("siri", True)])
    d.disconnected()
    assert not d.voice.listening and d.recorders[0].cancelled


def test_agent_config_errors(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(CONFIG.replace('menus = ["apps"]', 'menus = ["nope"]'))
    with pytest.raises(ValueError, match=r"no \[menus.nope\]"):
        load_config(path)
    path.write_text(CONFIG.replace("{text}", "hi"))
    with pytest.raises(ValueError, match="placeholder"):
        load_config(path)


def test_custom_command_quotes_text():
    import shlex
    from freehand.agent import AgentConfig

    text = "don't; rm -rf ~"
    cfg = AgentConfig(command="echo {text} {prompt}", prompt=Path("/c/my notes.md"))
    assert shlex.split(cfg.argv_for(text)) == ["echo", text, "/c/my notes.md"]
    assert AgentConfig().command is None


async def test_prompt_file_seeded_next_to_config(daemon, tmp_path):
    d = daemon
    assert d.voice.cfg.prompt == tmp_path / "agent.md"
    assert not d.voice.cfg.prompt.exists()
    await d.voice.start("hi")
    assert "## The user's notes" in d.voice.cfg.prompt.read_text()
    d.voice.cfg.prompt.write_text("mine")
    await d.voice.start("hi")
    assert d.voice.cfg.prompt.read_text() == "mine"  # never overwritten


async def test_voice_pick_uses_codex_and_opens_only_selected_action(daemon, monkeypatch):
    import json
    from freehand.pie import Item
    d = daemon
    menu = d.cfg.menus["threads"]
    menu.voice = "pick"
    target = Item("Rebind Chat and App Switchers", action="open-selected")
    counts, actions = [], []

    async def load(count=None):
        counts.append(count)
        return [Item("Other")] * 8 + [target]

    async def codex(text, model, **kwargs):
        payload = json.loads(text)
        assert payload["request"] == "rebind chat buttons"
        assert payload["items"][8]["title"] == target.label
        assert "action" not in payload["items"][8]
        assert not kwargs.get("desktop")
        return '{"index": 8}'

    async def transcribe(wav):
        return "rebind chat buttons"

    async def action(value):
        actions.append(value)

    monkeypatch.setattr(menu, "load", load)
    monkeypatch.setattr("freehand.agent.codex_exec", codex)
    d.voice.transcribe = transcribe
    d.pie.run_action = action
    d.buttons([("up", True)])
    d.buttons([("siri", True)])
    d.buttons([("siri", False)])
    await d.voice.processing
    assert actions == ["open-selected"] and 1000 in counts
    assert not d.keys and d.voice.task is None


@pytest.mark.parametrize("reply", ['{"index": null}', '{"index": 999}', '{"index": true}', 'bad'])
async def test_codex_match_rejects_invalid_or_missing_selection(monkeypatch, reply):
    from freehand.agent import codex_match
    from freehand.pie import Item

    async def codex(*args, **kwargs):
        return reply

    monkeypatch.setattr("freehand.agent.codex_exec", codex)
    if reply == '{"index": null}':
        assert await codex_match("nonsense", [Item("Title")], "model") is None
    else:
        with pytest.raises(ValueError):
            await codex_match("nonsense", [Item("Title")], "model")


async def test_back_cancels_pending_match(daemon):
    d = daemon
    entered = asyncio.Event()
    actions = []

    async def pending(text):
        entered.set()
        await asyncio.Event().wait()
        actions.append(text)

    d.voice.listen(pending)
    d.buttons([("siri", False)])
    await entered.wait()
    d.buttons([("back", True)])
    d.buttons([("back", False)])
    with pytest.raises(asyncio.CancelledError):
        await d.voice.processing
    assert not actions and not d.keys


async def test_default_agent_uses_codex_desktop(daemon, monkeypatch):
    calls = []

    async def codex(text, model, **kwargs):
        calls.append((text, model, kwargs))
        return "Opened calendar"

    monkeypatch.setattr("freehand.agent.codex_exec", codex)
    daemon.voice.cfg.command = None
    assert await daemon.voice.start("calendar") == "Opened calendar"
    assert calls[0][2]["desktop"] is True
    assert "Hyprland" in calls[0][2]["instructions"]


async def test_back_during_recording_swallows_siri_release(daemon):
    d = daemon
    d.buttons([("down", True)])
    d.buttons([("siri", True)])
    d.buttons([("back", True)])
    d.buttons([("back", False)])
    d.buttons([("siri", False)])
    assert d.recorders[0].cancelled
    assert not d.keys and d.voice.processing is None
    await settle()


@pytest.mark.parametrize("desktop", [False, True])
async def test_codex_approvals_are_scoped_to_desktop_mcp(tmp_path, monkeypatch, desktop):
    from freehand.agent import codex_exec

    monkeypatch.setattr("freehand.agent.STATE_DIR", tmp_path)
    calls = []

    async def spawn(*argv, **kwargs):
        calls.append(argv)
        Path(argv[argv.index("--output-last-message") + 1]).write_text("ok")

        class Process:
            returncode = 0

            async def communicate(self, data):
                assert data == b"request"
                return None, b""

        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    assert await codex_exec("request", "model", desktop=desktop) == "ok"
    argv = calls[0]
    assert ('mcp_servers.hypruse.default_tools_approval_mode="approve"' in argv) is desktop
    assert ('mcp_servers.hypruse.env.HYPRUSE_BACKGROUND="0"' in argv) is desktop
    journal_settings = [arg for arg in argv if arg.startswith("mcp_servers.hypruse.env.HYPRUSE_JOURNAL=")]
    assert len(journal_settings) == int(desktop)
    if desktop:
        import json
        assert json.loads(journal_settings[0].split("=", 1)[1]) == str(tmp_path / "hypruse.ndjson")
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=false" in argv


async def test_siri_click_does_not_record_in_voice_menu(daemon):
    d = daemon
    d.cfg.siri_tap = {'keys': 'Return'}
    d.cfg.siri_hold_ms = 20
    ran = []
    async def runner(argv):
        ran.append(argv)
    d.engine.runner = runner
    d.buttons([('down', True), ('down', False)])
    d.buttons([('siri', True), ('siri', False)])
    await settle()
    assert ran == [['wtype', '-k', 'Return']]
    assert not d.recorders and not d.pie.open


async def test_siri_hold_still_records_in_voice_menu(daemon):
    d = daemon
    d.cfg.siri_tap = {'keys': 'Return'}
    d.cfg.siri_hold_ms = 20
    d.buttons([('down', True), ('down', False)])
    d.buttons([('siri', True)])
    assert d.pie.open and not d.recorders
    await asyncio.sleep(0.04)
    assert d.voice.listening and d.recorders[0].started
    d.buttons([('siri', False)])
    await settle()
    await d.voice.task
    assert not d.keys and 'siri' not in d.engine.state
