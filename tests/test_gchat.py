"""Chat accessibility fixtures: Home and the sidebar shown inside a conversation."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


class Node:
    def __init__(self, role, name='', children=(), **attrs):
        self.role, self.name, self.children, self.attrs = role, name, children, attrs

    def get_role_name(self): return self.role
    def get_name(self): return self.name
    def get_attributes(self): return self.attrs
    def get_child_count(self): return len(self.children)
    def get_child_at_index(self, i): return self.children[i]


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args: None
    repository = types.ModuleType('gi.repository')
    repository.Atspi = None
    monkeypatch.setitem(sys.modules, 'gi', gi)
    monkeypatch.setitem(sys.modules, 'gi.repository', repository)
    spec = importlib.util.spec_from_file_location('freehand.gchat_test', Path(__file__).parents[1] / 'src/freehand/gchat.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sidebar(cid, *parts):
    return Node('list item', children=[Node('link', children=[Node('static', p) for p in parts])], id=cid + '/SCcFR')


def test_unread_without_home(chat, monkeypatch, capsys):
    win = Node('frame', children=[sidebar('space/group', 'Unread followed thread', 'Alex', ', ', 'Sam', 'Conversation'),
                                  sidebar('space/read', 'Read room', 'Space')])
    monkeypatch.setattr(chat, 'chat_window', lambda: win)
    assert chat.cmd_list(8, True) == 0
    assert json.loads(capsys.readouterr().out) == [dict(id='space/group', title='Alex, Sam', when='', preview='', unread=True)]


def test_home_preview_and_sidebar_unread_merge(chat, monkeypatch, capsys):
    row = Node('list item', children=[Node('static', s) for s in ['Team', 'Now', 'hello']], id='space/team/row')
    home = Node('list', 'Home', [row])
    win = Node('frame', children=[sidebar('space/team', 'Unread followed thread', 'Team', 'Space'), home])
    monkeypatch.setattr(chat, 'chat_window', lambda: win)
    chat.cmd_list(8, True)
    assert json.loads(capsys.readouterr().out) == [dict(id='space/team', title='Team', when='Now', preview='hello', unread=True)]


def test_home_unread_marker(chat):
    row = Node('list item', children=[Node('static', s) for s in ['Team', 'Now', 'Unread', 'hello']], id='space/team/row')
    assert chat.parse_item(row)['unread'] is True
    assert chat.parse_item(row)['preview'] == 'hello'


def test_unread_after_space_icon(chat):
    row = sidebar('space/team', '🎡', 'Unread', 'Example-team', 'Space')
    row.children[0].children[0].role = 'image'
    item = chat.parse_item(row)
    assert item['unread'] is True
    assert item['title'] == '🎡Example-team'


def test_open_sidebar_link(chat, monkeypatch):
    row = sidebar('dm/person', 'Unread', 'Person')
    monkeypatch.setattr(chat, 'chat_window', lambda: Node('frame', children=[row]))
    monkeypatch.setattr(chat.subprocess, 'Popen', lambda *args, **kwargs: None)
    confirmations = []
    monkeypatch.setattr(chat, 'confirm_copied_message', lambda node, preview: confirmations.append((node, preview)))
    clicked = []
    monkeypatch.setattr(chat, 'click', lambda n: clicked.append(n) or True)
    assert chat.cmd_open('dm/person') == 0
    assert clicked == [row.children[0]]



def test_copy_message_link_uses_selected_message(chat, monkeypatch):
    win, more, copy = object(), object(), object()
    message = types.SimpleNamespace(get_component_iface=lambda: types.SimpleNamespace(grab_focus=lambda: True),
                                    get_name=lambda: 'Sam: The selected message')
    monkeypatch.setattr(chat, 'chat_window', lambda: win)
    monkeypatch.setattr(chat, 'focused_message', lambda w: message)
    monkeypatch.setattr(chat, 'wait_control', lambda root, names: more if root is message else copy)
    confirmations = []
    monkeypatch.setattr(chat, 'confirm_copied_message', lambda node, preview: confirmations.append((node, preview)))
    clicked = []
    monkeypatch.setattr(chat, 'click', lambda n: clicked.append(n) or True)
    assert chat.cmd_copy_message_link() == 0
    assert clicked == [more, copy]
    assert confirmations == [(message, 'Sam: The selected message')]


def test_copy_message_link_refuses_ambiguous_selection(chat, monkeypatch):
    monkeypatch.setattr(chat, 'chat_window', lambda: object())
    monkeypatch.setattr(chat, 'focused_message', lambda w: None)
    notices = []
    monkeypatch.setattr(chat.subprocess, 'run', lambda args, **kwargs: notices.append(args))
    monkeypatch.setattr(chat, 'click', lambda n: pytest.fail('must not choose an arbitrary message'))
    assert chat.cmd_copy_message_link() == 1
    assert 'Focus a Chat message' in notices[0][-1]


def test_copy_confirmation_restores_target_and_escapes_markup(chat, monkeypatch):
    focused, notices = [], []
    message = types.SimpleNamespace(get_component_iface=lambda: types.SimpleNamespace(grab_focus=lambda: focused.append(True)))
    monkeypatch.setattr(chat.subprocess, 'run', lambda args, **kwargs: notices.append(args))
    chat.confirm_copied_message(message, 'Sam: <b>literal</b> & text')
    assert focused == [True]
    assert notices[0][-1] == 'Sam: &lt;b&gt;literal&lt;/b&gt; &amp; text'
    assert '--expire-time=6000' in notices[0]


def test_message_preview_is_bounded_and_has_fallback(chat):
    assert chat.message_preview(Node('panel', 'Sam:  hello\nthere')) == 'Sam: hello there'
    assert len(chat.message_preview(Node('panel', 'x' * 800))) == 700
    assert chat.message_preview(Node('panel', children=[Node('static', 'Body')])) == 'Body'


def test_focus_uses_local_argv(chat, tmp_path):
    config = tmp_path / 'freehand/local.toml'
    config.parent.mkdir()
    config.write_text('[chat]\nfocus_command = ["my-focus-helper", "chat"]\n')
    assert chat.focus_command() == ['my-focus-helper', 'chat']


def test_focus_defaults_without_local_settings(chat):
    assert chat.focus_command() == ['chromium', '--app=https://chat.google.com/']
