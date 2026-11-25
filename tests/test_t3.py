from freehand.t3 import ago, short_machine, status_line

NOW = 1_800_000_000.0  # fixed clock


def iso(seconds_ago):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")


def test_ago():
    assert ago(iso(30), NOW) == "just now"
    assert ago(iso(13 * 60), NOW) == "13m"
    assert ago(iso(3 * 3600 + 32 * 60), NOW) == "3h 32m"
    assert ago(iso(16 * 3600), NOW) == "16h"
    assert ago(iso(3 * 86400), NOW) == "3d"
    assert ago(None, NOW) == ""


def test_status_line():
    assert status_line({"status": "running"}, NOW) == "Working"
    assert status_line({"status": "waiting"}, NOW) == "Needs your input"
    assert status_line({"status": "completed", "updatedAt": iso(600)}, NOW) == "Done · 10m"
    assert status_line({"status": "interrupted"}, NOW) == "Stopped"


def test_short_machine():
    assert short_machine("Alex’s MacBook Pro") == "MacBook Pro"
    assert short_machine("squirrel") == "squirrel"


async def test_project_menu_filters_pages_and_keeps_environment():
    from freehand.t3 import T3Client
    from freehand.pie import Item
    c = T3Client()
    calls = []
    def tool(name, args):
        calls.append((name, args))
        if 'cursor' not in args:
            return {'threads': [{'threadId': 'selected'}, {'threadId': 'child', 'parentThreadId': 'selected'},
                                {'threadId': 'settled', 'settled': True}], 'nextCursor': 3}
        return {'threads': [{'threadId': f't{i}', 'title': f'Thread {i}'} for i in range(10)], 'nextCursor': None}
    c._tool = tool
    try:
        items = await c.project_threads('remote', 'project', 'selected')
    finally:
        c.pool.shutdown()
    assert len(items) == 8 and items[0]['label'] == 'New thread'
    assert 'new-thread remote project' in items[0]['action']
    assert items[-1]['label'] == 'More threads'
    assert len(items[-1]['submenu']['items']) == 5
    assert all(args['environmentId'] == 'remote' and args['projectId'] == 'project' for _, args in calls)
    assert all('t3code://threads/remote/' in item['action'] for item in items[1:-1])
    for item in items:
        Item.from_dict(item)


def test_thread_submenu_context_and_shell_quoting():
    from freehand.t3 import thread_item
    import shlex
    item = thread_item({'environmentId': 'remote', 'threadId': 'thread', 'projectId': "project';echo oops",
                        'project': 'Project', 'machine': 'Remote'}, with_submenu=True)
    assert shlex.split(item['submenu']['source'])[-3:] == ['remote', "project';echo oops", 'thread']
    assert item['submenu']['title'] == 'Project'
    assert 'submenu' not in thread_item({'environmentId': 'local', 'threadId': 't'}, with_submenu=True)


async def test_new_thread_creates_idle_thread_and_opens_link(monkeypatch):
    import freehand.t3 as t3
    calls, opened = [], []
    def tool(self, name, args):
        calls.append((self.scope, name, args))
        return {'threadId': 'mcp:launch:new',
                'link': '[New thread](t3-thread://v2/remote/mcp:launch:new)'}
    class Proc:
        async def wait(self):
            return 0
    async def spawn(*args):
        opened.append(args)
        return Proc()
    monkeypatch.setattr(t3.T3Client, '_tool', tool)
    monkeypatch.setattr(t3.asyncio, 'create_subprocess_exec', spawn)
    await t3.new_thread('remote', 'project')
    scope, name, args = calls[0]
    assert scope == 'orchestration:operate' and name == 't3_thread_launch'
    assert args['projectId'] == 'project' and args['environmentId'] == 'remote'
    assert 'message' not in args and args['clientRequestId']
    assert args['runtimeMode'] == 'auto'
    assert opened == [('xdg-open', 't3code://threads/remote/mcp%3Alaunch%3Anew')]


async def test_creation_menu_has_direct_hosts_and_bottom_project_picker():
    from freehand.t3 import T3Client
    from freehand.pie import Item, layout
    c = T3Client()
    async def envs():
        return [{'environmentId': 'local', 'label': 'Desktop'}, {'environmentId': 'remote', 'label': 'Laptop'}]
    c.available_environments = envs
    items = await c.creation_menu()
    slots = layout([Item.from_dict(i) for i in items], 8)
    assert slots[4].label == 'New thread in project'
    assert slots[4].submenu['source'] == '@t3-projects'
    assert [i['content'] for i in items[1:]] == ['Desktop', 'Laptop']
    assert all('submenu' not in i and 'new-scratch' in i['action'] for i in items[1:])
    c.pool.shutdown()


async def test_project_picker_pagination_and_machine_actions():
    from freehand.t3 import T3Client
    from freehand.pie import Item
    c = T3Client()
    async def envs():
        return [{'environmentId': 'remote', 'label': 'Laptop'}]
    c.available_environments = envs
    calls = []
    def tool(name, args):
        calls.append(args)
        if 'cursor' not in args:
            return {'projects': [{'id': 'old', 'archivedAt': 'date'}], 'nextCursor': 1}
        return {'projects': [{'id': f'p{i}', 'title': f'Project {i}'} for i in range(10)]}
    c._tool = tool
    try:
        items = await c.available_projects()
        assert len(items) == 8 and items[-1]['label'] == 'More projects'
        assert len(items[-1]['submenu']['items']) == 3
        assert all('new-thread remote p' in i['action'] for i in items[:-1])
        assert calls[1]['cursor'] == 1
        for item in items:
            Item.from_dict(item)
    finally:
        c.pool.shutdown()


async def test_scratch_thread_uses_selected_host_without_project(monkeypatch):
    import freehand.t3 as t3
    calls = []
    def tool(self, name, args):
        calls.append(args)
        return {'threadId': 'new'}
    class Proc:
        async def wait(self):
            return 0
    async def spawn(*args):
        assert args == ('xdg-open', 't3code://threads/remote/new')
        return Proc()
    monkeypatch.setattr(t3.T3Client, '_tool', tool)
    monkeypatch.setattr(t3.asyncio, 'create_subprocess_exec', spawn)
    await t3.new_thread('remote')
    assert calls[0]['scratch'] is True
    assert calls[0]['runtimeMode'] == 'auto'
    assert calls[0]['environmentId'] == 'remote'
    assert 'projectId' not in calls[0] and 'message' not in calls[0]



def test_project_names_reads_every_page_and_refreshes_cache_misses():
    from freehand.t3 import T3Client
    c = T3Client()
    calls = []
    def tool(name, args):
        calls.append(dict(args))
        if 'cursor' not in args:
            return {'projects': [{'id': 'first', 'title': 'First'}], 'nextCursor': 20}
        return {'projects': [{'id': 'later', 'title': 'testpark'}], 'nextCursor': None}
    c._tool = tool
    try:
        assert c._project_names('mac')['later'] == 'testpark'
        assert calls[1]['cursor'] == 20 and calls[1]['environmentId'] == 'mac'
        assert c._project_names('mac')['later'] == 'testpark'
        assert len(calls) == 2
        c._project_names('mac', {'new-project'})
        assert len(calls) == 4
    finally:
        c.pool.shutdown()


def test_unknown_project_is_not_labeled_no_project():
    from freehand.t3 import thread_item
    item = thread_item({'threadId': 't', 'environmentId': 'mac', 'projectId': 'missing'}, with_submenu=True)
    assert item['content'].startswith('Unknown project')


def test_thread_tints_are_stable_per_environment():
    from freehand.t3 import thread_item, environment_tint
    from freehand.pie import Item
    a = thread_item({'threadId': 'a', 'environmentId': 'squirrel'})
    b = thread_item({'threadId': 'b', 'environmentId': 'squirrel'})
    c = thread_item({'threadId': 'c', 'environmentId': 'macbook'})
    assert a['tint'] == b['tint'] == environment_tint('squirrel')
    assert a['tint'] != c['tint']
    assert Item.from_dict(a).for_ui()['tint'] == a['tint']


async def test_local_only_menus_never_discover_or_query_remote_hosts(monkeypatch):
    from freehand.t3 import T3Client
    monkeypatch.setenv('FREEHAND_T3_LOCAL_ONLY', '1')
    c = T3Client()
    calls = []
    def tool(name, args):
        calls.append((name, args))
        assert 'environmentId' not in args or args['environmentId'] == 'local'
        if name == 't3_environment_read':
            return {'environmentId': 'local', 'label': 'Desktop'}
        if name == 't3_thread_list':
            return {'threads': [{'threadId': 't', 'title': 'Local thread'}]}
        if name == 't3_project_list':
            return {'projects': [{'id': 'p', 'title': 'Local project'}]}
        raise AssertionError(f'Unexpected call: {name}')
    c._tool = tool
    try:
        assert len(await c.recent_threads()) == 1
        assert len(await c.available_environments()) == 1
        assert len(await c.available_projects()) == 1
        assert all(name != 't3_environment_links' for name, _ in calls)
    finally:
        c.pool.shutdown()
