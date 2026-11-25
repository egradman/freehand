"""Recent T3 Code threads across this machine and every linked one.

Talks to the local T3 server's MCP endpoint with a short-lived, read-only
token minted by the server's own CLI (`t3 auth session issue`). The server
reaches linked machines itself, so one token covers them all.
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import uuid
from urllib.parse import quote
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("freehand")

T3_HOME = Path.home() / ".t3"
DEFAULT_URL = "http://127.0.0.1:3773/mcp"
TOKEN_TTL_S = 3600
PROJECT_CACHE_S = 600
WORKING = {"running", "starting", "preparing", "queued"}


def t3_cli() -> Path:
    state = json.loads((T3_HOME / "runtime/service-state.json").read_text())
    return T3_HOME / "runtime/versions" / state["activeVersion"] / "t3"


def ago(iso: str | None, now: float | None = None) -> str:
    if not iso:
        return ""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    s = max(0, int((now or time.time()) - t))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m" if s < 36000 else f"{s // 3600}h"
    return f"{s // 86400}d"


def status_line(t: dict, now: float | None = None) -> str:
    st = t.get("status")
    if st in WORKING:
        return "Working"
    if st == "waiting" or t.get("pendingRequestCount"):
        return "Needs your input"
    if st == "failed":
        return "Failed"
    if st in ("interrupted", "cancelled"):
        return "Stopped"
    when = ago(t.get("updatedAt"), now)
    return f"Done · {when}" if st == "completed" else (f"Idle · {when}" if when else "Idle")


def short_machine(label: str) -> str:
    """"Alex’s MacBook Pro" -> "MacBook Pro"."""
    return re.sub(r"^\S+['’]s\s+", "", label or "").strip() or label


class T3Client:
    def __init__(self, url: str = DEFAULT_URL, scope: str = "orchestration:read"):
        self.scope = scope
        self.local_only = os.environ.get("FREEHAND_T3_LOCAL_ONLY", "0") == "1"
        self.url = url
        self.token: str | None = None
        self.token_expires = 0.0
        self.session: str | None = None
        self.projects: dict[str, tuple[float, dict[str, str]]] = {}  # env -> (fetched, {id: title})
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="t3")

    # -- plumbing (blocking; run in the pool) ------------------------------------

    def _mint(self) -> None:
        if self.scope == "orchestration:operate":
            from .t3_auth import load_credential
            self.token, self.token_expires = load_credential(self.url)
            self.session = None
            return
        import subprocess

        out = subprocess.run(
            [str(t3_cli()), "auth", "session", "issue", "--subject", "mcp-client",
             "--scope", self.scope, "--ttl", f"{TOKEN_TTL_S}s",
             "--label", "freehand", "--token-only"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode or not out.stdout.strip():
            raise RuntimeError(f"couldn't get a T3 token: {out.stderr.strip()[:200]}")
        self.token = out.stdout.strip()
        self.token_expires = time.time() + TOKEN_TTL_S
        self.session = None

    def _post(self, body: dict, with_session: bool = True) -> tuple[dict | None, dict]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if with_session and self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = "2025-06-18"
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            hdrs = {k.lower(): v for k, v in r.headers.items()}
        if not raw.strip():
            return None, hdrs
        if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):  # SSE framing
            raw = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
        return json.loads(raw), hdrs

    def _connect(self) -> None:
        if not self.token or time.time() > self.token_expires - 300:
            self._mint()
        if self.session:
            return
        _, hdrs = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "freehand", "version": "0.1"}}}, with_session=False)
        self.session = hdrs.get("mcp-session-id")
        if not self.session:
            raise RuntimeError("T3 server didn't start an MCP session")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _tool(self, name: str, args: dict) -> dict:
        for attempt in (1, 2):
            self._connect()
            try:
                resp, _ = self._post({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**9,
                                      "method": "tools/call", "params": {"name": name, "arguments": args}})
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404) and attempt == 1:  # token or session expired
                    self.token = self.session = None
                    continue
                raise
            if "error" in resp:
                if attempt == 1 and "session" in json.dumps(resp["error"]).lower():
                    self.session = None
                    continue
                raise RuntimeError(f"{name}: {resp['error'].get('message')}")
            result = resp["result"]
            if result.get("isError"):
                raise RuntimeError(f"{name}: {result['content'][0].get('text', '')[:200]}")
            return result.get("structuredContent") or json.loads(result["content"][0]["text"])
        raise RuntimeError(f"{name}: couldn't reach the T3 server")

    def _project_names(self, env: str | None, required=()) -> dict[str, str]:
        key = env or "local"
        cached = self.projects.get(key)
        if cached and time.time() - cached[0] < PROJECT_CACHE_S and set(required) <= cached[1].keys():
            return cached[1]
        args = {"environmentId": env} if env else {}
        args["limit"] = 100
        names, seen = {}, set()
        while True:
            page = self._tool("t3_project_list", args)
            names.update({p["id"]: p.get("title", "") for p in page.get("projects", [])})
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if cursor in seen:
                raise RuntimeError("T3 project pagination repeated a cursor")
            seen.add(cursor)
            args["cursor"] = cursor
        self.projects[key] = (time.time(), names)
        return names

    def _env_threads(self, env: str | None, limit: int) -> list[dict]:
        args = {"allProjects": True, "settled": False, "limit": limit}
        if env:
            args["environmentId"] = env
        return self._tool("t3_thread_list", args).get("threads", [])

    # -- public ---------------------------------------------------------------

    async def recent_threads(self, count: int = 8) -> list[dict]:
        """The `count` most recently active unsettled top-level threads, all machines."""
        loop = asyncio.get_running_loop()
        run = lambda f, *a: loop.run_in_executor(self.pool, f, *a)  # noqa: E731
        local = await run(self._tool, "t3_environment_read", {})
        links = [] if self.local_only else (await run(self._tool, "t3_environment_links", {})).get("links", [])
        envs = [(None, local["environmentId"], local.get("label", "this machine"))]
        envs += [(l["environmentId"], l["environmentId"], l.get("label", "?"))
                 for l in links if l.get("status") == "reachable"]

        async def one(env, env_id, label):
            try:
                threads, names = await asyncio.gather(run(self._env_threads, env, max(40, count)),
                                                      run(self._project_names, env))
            except Exception as e:
                log.warning("couldn't list threads on %s: %s", label, e)
                return []
            missing = {t["projectId"] for t in threads if t.get("projectId") and t["projectId"] not in names}
            if missing:
                try:
                    names = await run(self._project_names, env, missing)
                except Exception as e:
                    log.warning("couldn't refresh project names on %s: %s", label, e)
            return [dict(t, environmentId=env_id, machine=short_machine(label),
                         project=names.get(t.get("projectId"), "")) for t in threads]

        everything = [t for ts in await asyncio.gather(*(one(*e) for e in envs)) for t in ts]
        now = time.time()
        live = [t for t in everything
                if not t.get("parentThreadId") and not t.get("settled") and not t.get("snoozed")]
        live.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
        return [thread_item(t, with_submenu=True) for t in live[:count]]

    async def available_environments(self) -> list[dict]:
        loop = asyncio.get_running_loop()
        local = await loop.run_in_executor(self.pool, self._tool, "t3_environment_read", {})
        if self.local_only:
            return [local]
        links = await loop.run_in_executor(self.pool, self._tool, "t3_environment_links", {})
        return [local, *(link for link in links.get("links", [])
                         if link.get("status") == "reachable" and link.get("access") != "read-only"
                         and link["environmentId"] != local["environmentId"])]

    async def creation_menu(self, count: int = 8) -> list[dict]:
        envs = await self.available_environments()
        hosts = [{"label": "New no-project thread", "content": short_machine(e.get("label", "This machine")),
                  "action": helper_command("new-scratch", e["environmentId"])} for e in envs]
        projects = {"label": "New thread in project", "position": "s",
                    "submenu": {"title": "Choose project", "source": "@t3-projects",
                                "source_timeout_s": 20}}
        # Keep the project picker at the bottom; extra hosts remain reachable on another page.
        visible = hosts if len(hosts) <= 7 else hosts[:6] + [{
            "label": "More hosts", "submenu": {"title": "Choose host", "items": picker_pages(hosts[6:], "hosts")}}]
        return [projects, *visible]

    async def available_projects(self, count: int = 8) -> list[dict]:
        loop = asyncio.get_running_loop()
        async def one(env):
            items, cursor = [], None
            while True:
                args = {"environmentId": env["environmentId"], "limit": 100}
                if cursor is not None:
                    args["cursor"] = cursor
                page = await loop.run_in_executor(self.pool, self._tool, "t3_project_list", args)
                for project in page.get("projects", []):
                    if project.get("archivedAt") or project.get("deletedAt"):
                        continue
                    items.append({"label": project.get("title") or "Untitled project",
                                  "content": short_machine(env.get("label", "This machine")),
                                  "action": helper_command("new-thread", env["environmentId"], project["id"])})
                next_cursor = page.get("nextCursor")
                if next_cursor is None or next_cursor == cursor:
                    break
                cursor = next_cursor
            return items
        groups = await asyncio.gather(*(one(e) for e in await self.available_environments()))
        items = sorted([item for group in groups for item in group], key=lambda i: (i["label"].casefold(), i["content"]))
        return picker_pages(items, "projects")

    async def project_threads(self, env: str, project: str, exclude: str) -> list[dict]:
        loop = asyncio.get_running_loop()
        threads, cursor = [], None
        while True:
            args = {"environmentId": env, "projectId": project, "settled": False,
                    "snoozed": False, "includeSubagents": False, "limit": 100}
            if cursor is not None:
                args["cursor"] = cursor
            page = await loop.run_in_executor(self.pool, self._tool, "t3_thread_list", args)
            threads.extend(t for t in page.get("threads", []) if t.get("threadId") != exclude
                           and not t.get("parentThreadId") and not t.get("settled") and not t.get("snoozed"))
            next_cursor = page.get("nextCursor")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        threads.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
        items = [thread_item(dict(t, environmentId=env)) for t in threads]
        new = {"label": "New thread", "content": "Start in this project",
               "action": helper_command("new-thread", env, project)}
        return project_pages(new, items)


def picker_pages(items: list[dict], kind: str) -> list[dict]:
    if len(items) <= 8:
        return items
    return [*items[:7], {"label": f"More {kind}", "submenu": {
        "title": f"Choose {kind}", "items": picker_pages(items[7:], kind)}}]


def helper_command(*args: str) -> str:
    return shlex.join([sys.executable, "-m", "freehand.t3", *args])


def environment_tint(environment: str) -> str:
    hue = int.from_bytes(hashlib.sha256(environment.encode()).digest()[:2]) / 65536
    rgb = colorsys.hls_to_rgb(hue, .65, .65)
    return '#' + ''.join(f'{round(c * 255):02x}' for c in rgb)


def thread_item(t: dict, with_submenu: bool = False) -> dict:
    env, thread = t["environmentId"], t["threadId"]
    uri = f"t3code://threads/{quote(env, safe='')}/{quote(thread, safe='')}"
    item = {"label": t.get("title") or "Untitled", "tint": environment_tint(env),
            "content": status_line(t), "action": shlex.join(["xdg-open", uri])}
    if with_submenu:
        item["content"] = f"{t.get('project') or ('Unknown project' if t.get('projectId') else 'No project')} · {t.get('machine', '')}\n{status_line(t)}"
        if t.get("projectId"):
            item["submenu"] = {"title": t.get("project") or "Project threads",
                               "source": helper_command("project-menu", env, t["projectId"], thread),
                               "source_timeout_s": 20, "voice": "pick"}
    return item


def project_pages(new: dict, items: list[dict]) -> list[dict]:
    # Leave room for a More slice when a project has more than seven other threads.
    if len(items) <= 7:
        return [new, *items]
    return [new, *items[:6], {"label": "More threads", "content": "Hold to browse more",
            "submenu": {"title": "More project threads", "voice": "pick",
                        "items": project_pages(new, items[6:])}}]


async def new_thread(env: str, project: str | None = None) -> None:
    # Only the explicit New thread selection obtains an operate token.
    c = T3Client(scope="orchestration:operate")
    try:
        args = {"environmentId": env, "title": "New thread", "runtimeMode": "auto",
                "clientRequestId": str(uuid.uuid4())}
        args.update({"projectId": project} if project is not None else {"scratch": True})
        result = await asyncio.get_running_loop().run_in_executor(c.pool, c._tool, "t3_thread_launch", args)
        # MCP links are Markdown for chat; the desktop handler needs a t3code URI.
        uri = f"t3code://threads/{quote(env, safe='')}/{quote(result['threadId'], safe='')}"
        proc = await asyncio.create_subprocess_exec("xdg-open", uri)
        if await proc.wait():
            raise RuntimeError("couldn't open the new T3 thread")
    finally:
        c.pool.shutdown(wait=False)



_client: T3Client | None = None


def client() -> T3Client:
    global _client
    if _client is None:
        _client = T3Client()
    return _client


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="T3 project pie menu actions")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("project-menu")
    listing.add_argument("environment")
    listing.add_argument("project")
    listing.add_argument("exclude")
    creating = sub.add_parser("new-thread")
    creating.add_argument("environment")
    creating.add_argument("project")
    scratch = sub.add_parser("new-scratch")
    scratch.add_argument("environment")
    args = parser.parse_args()
    if args.command == "project-menu":
        print(json.dumps(asyncio.run(client().project_threads(args.environment, args.project, args.exclude))))
    elif args.command == "new-scratch":
        asyncio.run(new_thread(args.environment))
    else:
        asyncio.run(new_thread(args.environment, args.project))
