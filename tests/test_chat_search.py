import asyncio
import json

import pytest

from freehand.chat_search import parse_result, search_archive

ENVIRONMENT = "example-environment"
MODEL = {"instanceId": "example-provider", "model": "example-model"}

@pytest.fixture(autouse=True)
def local_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "freehand" / "local.toml"
    config.parent.mkdir()
    config.write_text('''[chat_search]
environment_id = "example-environment"
archive_path = "~/archive/chat.db"
skill_path = "~/skills/archive/SKILL.md"
provider = "example-provider"
model = "example-model"
''')
    return config
from freehand.t3 import T3Client


def test_result_only_opens_valid_chat_message():
    assert parse_result('{"resource":"spaces/ABC/messages/T.M","title":"Match"}') == ('https://chat.google.com/room/ABC/T/M', 'Match')
    assert parse_result('{"resource":null,"title":"Ambiguous"}') == (None, 'Ambiguous')
    for resource in ['https://evil.example', 'spaces/../messages/T.M', 'spaces/ABC/messages/T.M?x=y']:
        with pytest.raises(ValueError):
            parse_result(json.dumps({'resource': resource, 'title': 'Bad'}))


async def test_launches_configured_host_and_model_and_reads_final_result():
    c = T3Client()
    calls = []
    def tool(name, args):
        calls.append((name, args))
        if name == 't3_environment_links':
            return {'links': [{'environmentId': ENVIRONMENT, 'status': 'reachable'}]}
        if name == 't3_thread_launch':
            return {'threadId': 'search', 'runId': 'run'}
        return {'thread': {'status': 'completed'}, 'items': [
            {'type': 'assistant_message', 'runId': 'run', 'position': 2, 'text': 'Searching…'},
            {'type': 'assistant_message', 'runId': 'run', 'position': 4,
             'text': '{"resource":"spaces/A/messages/T.M","title":"Found"}'}]}
    c._tool = tool
    async def notify(*args): pass
    try:
        assert await search_archive('find the robot discussion', notify, c) == ('https://chat.google.com/room/A/T/M', 'Found')
        launch = calls[1][1]
        assert launch['environmentId'] == ENVIRONMENT and launch['modelSelection'] == MODEL
        assert launch['runtimeMode'] == 'auto'
        assert 'find the robot discussion' in launch['message']
        assert '~/archive/chat.db' in launch['message']
        assert '~/skills/archive/SKILL.md' in launch['message']
    finally:
        c.pool.shutdown()


async def test_timeout_interrupts_remote_search():
    c = T3Client()
    calls = []
    def tool(name, args):
        calls.append(name)
        if name == 't3_environment_links': return {'links': [{'environmentId': ENVIRONMENT, 'status': 'reachable'}]}
        if name == 't3_thread_launch': return {'threadId': 'search', 'runId': 'run'}
        return {}
    c._tool = tool
    async def notify(*args): pass
    try:
        with pytest.raises(asyncio.TimeoutError):
            await search_archive('query', notify, c, timeout_s=0)
        assert calls[-1] == 't3_thread_interrupt'
    finally:
        c.pool.shutdown()


async def test_local_only_blocks_archive_search_before_any_remote_call(monkeypatch):
    monkeypatch.setenv('FREEHAND_T3_LOCAL_ONLY', '1')
    c = T3Client()
    def tool(*args):
        pytest.fail('Local-only mode must not contact the archive host')
    c._tool = tool
    async def notify(*args): pass
    try:
        with pytest.raises(RuntimeError, match='local-only'):
            await search_archive('query', notify, c)
    finally:
        c.pool.shutdown()


async def test_unconfigured_search_makes_no_remote_calls(local_settings):
    local_settings.unlink()
    c = T3Client()
    c._tool = lambda *args: pytest.fail('Must not contact a remote without local settings')
    async def notify(*args): pass
    try:
        with pytest.raises(RuntimeError, match='Configure'):
            await search_archive('query', notify, c)
    finally:
        c.pool.shutdown()
