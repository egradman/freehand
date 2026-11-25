import json

import pytest

from freehand import quake


def setup(monkeypatch, workspace="2", tags=(), console_open=False):
    active = {"address": "0x123", "workspace": {"name": workspace}, "tags": list(tags)}
    calls = []

    def hypr(*args):
        if args == ("-j", "activewindow"):
            return json.dumps(active)
        if args == ("-j", "monitors"):
            return json.dumps([{"focused": True, "activeWorkspace": {"name": "4"},
                                "specialWorkspace": {"name": quake.CONSOLE if console_open else ""}}])
        calls.append(args[1])
        return "ok"

    monkeypatch.setattr(quake, "hypr", hypr)
    return active, calls


def test_remembers_origin_before_moving(monkeypatch):
    _, calls = setup(monkeypatch, "named workspace")
    quake.toggle_window()
    assert quake.ORIGIN + "named workspace".encode().hex() in calls[0]
    assert 'workspace = "special:qconsole"' in calls[1]
    assert 'window = "address:0x123"' in calls[1]
    assert 'follow = false' in calls[1]


def test_returns_to_saved_origin_and_clears_tag(monkeypatch):
    tag = quake.ORIGIN + b"2".hex()
    _, calls = setup(monkeypatch, quake.CONSOLE, [tag])
    quake.toggle_window()
    assert 'workspace = "2"' in calls[0]
    assert 'tag = "-' + tag + '"' in calls[1]
    assert len(calls) == 2  # already closed: do not toggle it open again


def test_return_closes_console_when_other_windows_remain(monkeypatch):
    tag = quake.ORIGIN + b"2".hex()
    _, calls = setup(monkeypatch, quake.CONSOLE, [tag], console_open=True)
    quake.toggle_window()
    assert 'workspace = "2"' in calls[0]
    assert calls[-1] == 'hl.dsp.workspace.toggle_special("qconsole")'


@pytest.mark.parametrize("tags", [[], [quake.ORIGIN + "invalid"]])
def test_legacy_window_returns_to_current_workspace(monkeypatch, tags):
    _, calls = setup(monkeypatch, quake.CONSOLE, tags)
    quake.toggle_window()
    assert 'workspace = "4"' in calls[0]


def test_no_active_window(monkeypatch):
    active, calls = setup(monkeypatch)
    active.clear()
    quake.toggle_window()
    assert calls == []


def test_failed_return_preserves_origin(monkeypatch):
    tag = quake.ORIGIN + b"2".hex()
    _, calls = setup(monkeypatch, quake.CONSOLE, [tag])
    original = quake.hypr
    monkeypatch.setattr(quake, "hypr", lambda *args: "failed" if args[0] == "dispatch" else original(*args))
    with pytest.raises(RuntimeError, match="failed"):
        quake.toggle_window()
    assert calls == []
