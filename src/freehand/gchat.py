"""Google Chat recent conversations, read from the Chat web app's accessibility tree.

Runs under the system Python (needs PyGObject with Atspi); freehand calls it as a
subprocess, like the pie overlay. Chromium exposes web content over AT-SPI
because Omarchy starts it with --force-renderer-accessibility.

  gchat.py list [N] [--unread]
                        print up to N conversations from Chat's Home list and sidebar as JSON
                        (--unread: only conversations with unread messages)
  gchat.py open ID      click that conversation (in place, no reload) and focus the Chat window

Imports only the stdlib and gi: it must not import freehand.
"""

import json
import html
import re
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

try:
    from .local_config import section
except ImportError:  # launched directly by the system Python for PyGObject
    from local_config import section


def focus_command():
    command = section('chat').get('focus_command', ['chromium', '--app=https://chat.google.com/'])
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError('[chat] focus_command must be a nonempty array of strings')
    return command


MAX_DEPTH = 45


def chat_window():
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if not app or app.get_name() != "Chromium":
            continue
        for wi in range(app.get_child_count()):
            w = app.get_child_at_index(wi)
            # Titles look like "Chat", "Some Room - Chat", or "... - Chat - High memory usage - 982 MB"
            name = re.sub(r" - High memory usage.*$", "", (w and w.get_name()) or "")
            if name == "Chat" or name.endswith(" - Chat"):
                return w
    return None


def find(node, pred, depth=0):
    if node is None or depth > MAX_DEPTH:
        return None
    try:
        if pred(node):
            return node
        n = node.get_child_count()
    except Exception:
        return None
    for c in range(n):
        hit = find(node.get_child_at_index(c), pred, depth + 1)
        if hit is not None:
            return hit
    return None


def home_list(win):
    return find(win, lambda n: n.get_role_name() == "list" and n.get_name() == "Home")


def texts(node, out, depth=0):
    if depth > 12:
        return out
    try:
        role, name = node.get_role_name(), node.get_name() or ""
        if role in ("static", "image") and name:
            out.append(name)
        for c in range(node.get_child_count()):
            texts(node.get_child_at_index(c), out, depth + 1)
    except Exception:
        pass
    return out


def item_id(node) -> str:
    try:
        raw = (node.get_attributes() or {}).get("id", "")
    except Exception:
        raw = ""
    # "space/EXAMPLE/ITEM" -> "space/EXAMPLE"
    return "/".join(raw.split("/")[:2])


def conversation_nodes(node, depth=0):
    """Home rows and sidebar rows, without depending on the selected view."""
    if node is None or depth > MAX_DEPTH:
        return
    try:
        if node.get_role_name() == "list item" and re.fullmatch(r"(space|dm)/[\w-]+", item_id(node)):
            yield node
            return
        children = [node.get_child_at_index(i) for i in range(node.get_child_count())]
    except Exception:
        return
    for child in children:
        yield from conversation_nodes(child, depth + 1)


def parse_item(node) -> dict | None:
    cid = item_id(node)
    parts = [t for t in texts(node, []) if t != "Press tab for more options."]
    if not cid or not parts:
        return None
    # Sidebar rows have a link containing a name (possibly split across many
    # text nodes), a conversation kind, and an optional unread marker. They
    # have no timestamp or message preview, unlike Home rows.
    link = find(node, lambda n: n.get_role_name() == "link")
    if link is not None:
        parts = texts(link, [])
        markers = {"Unread", "Unread message", "Unread messages", "Unread followed thread"}
        unread = any(p in markers for p in parts)
        parts = [p for p in parts if p not in markers]
        parts = [p for p in parts if not p.startswith("Press tab for more options")]
        if parts and parts[-1] in ("Space", "Conversation"):
            parts.pop()
        title = "".join(parts).strip()
        if not title:
            return None
        return {"id": cid, "title": title, "when": "", "preview": "", "unread": unread}
    title = parts[0]
    when = parts[1] if len(parts) > 1 else ""
    rest = parts[2:]
    unread = "KKjvXb" in (node.get_attributes() or {}).get("class", "")
    if rest and rest[0] == "Unread":  # screen-reader-only marker
        unread, rest = True, rest[1:]
    return {"id": cid, "title": title, "when": when, "preview": "".join(rest).strip(), "unread": unread}


def cmd_list(n: int, unread_only: bool = False) -> int:
    win = chat_window()
    if win is None:
        print(json.dumps({"error": "Google Chat isn't open"}))
        return 1
    # Prefer Home's ordering and previews when available, supplementing it
    # with sidebar unread thread markers. A conversation appears only once.
    home = home_list(win)
    rows = list(conversation_nodes(home)) + list(conversation_nodes(win))
    conversations = {}
    for node in rows:
        item = parse_item(node)
        if item:
            previous = conversations.get(item["id"])
            if previous is None:
                conversations[item["id"]] = item
            else:
                previous["unread"] |= item["unread"]
    out = [item for item in conversations.values() if item["unread"] or not unread_only]
    out = out[:max(0, n)]
    print(json.dumps(out, ensure_ascii=False))
    return 0


def click(node) -> bool:
    action = node.get_action_iface()
    if action is None:
        return False
    for i in range(action.get_n_actions()):
        if action.get_action_name(i) in ("clickAncestor", "click", "press", "jump", "open", "select"):
            return action.do_action(i)
    return False


def cmd_open(cid: str) -> int:
    subprocess.Popen(focus_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    win = chat_window()
    if win is None:
        print("Chat isn't available", file=sys.stderr)
        return 1
    for node in conversation_nodes(win):
        if item_id(node) == cid:
            link = find(node, lambda n: n.get_role_name() == "link")
            return 0 if click(link if link is not None else node) else 1
    print(f"conversation {cid} isn't in Chat's visible lists anymore", file=sys.stderr)
    return 1


def focused_message(win):
    node = find(win, lambda n: n.get_state_set().contains(Atspi.StateType.FOCUSED))
    for _ in range(MAX_DEPTH):
        if node is None:
            break
        attrs = node.get_attributes() or {}
        # Chat's message group, observed in both the timeline and thread view.
        if attrs.get('xml-roles') == 'group' and 'nF6pT' in attrs.get('class', '').split():
            return node
        node = node.get_parent()
    return None


def wait_control(root, names, timeout=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = find(root, lambda n: n.get_role_name() in ('button', 'push button', 'menu item')
                    and n.get_name() in names)
        if node is not None:
            return node
        time.sleep(.05)
    return None


def message_preview(message):
    """Use the exact message node used for copying, never a fresh focus lookup."""
    label = (message.get_name() or '').strip()
    if not label:
        label = ' '.join(texts(message, []))
    label = re.sub(r'\s+', ' ', label).strip()
    if len(label) > 700:
        label = label[:699].rstrip() + '…'
    return label or 'Selected message (no text preview available)'


def confirm_copied_message(message, preview):
    # Closing Chat's action menu can move focus; restore it to the actual target.
    try:
        message.get_component_iface().grab_focus()
    except Exception:
        pass  # The conversation may have changed while copying.
    subprocess.run(['notify-send', '--app-name=freehand', '--expire-time=6000',
                    'Copied link to this message', html.escape(preview)], check=False)


def cmd_copy_message_link():
    win = chat_window()
    message = focused_message(win) if win is not None else None
    error = None
    if message is None:
        error = 'Focus a Chat message first, then choose Copy message link.'
    elif not message.get_component_iface().grab_focus():
        error = 'Could not focus the selected Chat message.'
    else:
        preview = message_preview(message)
        more = wait_control(message, ('More actions', 'More options'))
        if more is None or not click(more):
            error = 'This message has no available More actions control.'
        else:
            copy = wait_control(win, ('Copy link', 'Copy message link'))
            if copy is not None and click(copy):
                confirm_copied_message(message, preview)
                return 0
            error = 'Copy link is not available for this Chat message.'
    print(error, file=sys.stderr)
    subprocess.run(['notify-send', 'freehand', error], check=False)
    return 1


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore", category=DeprecationWarning)
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        args = sys.argv[2:]
        unread_only = "--unread" in args
        args = [a for a in args if a != "--unread"]
        sys.exit(cmd_list(int(args[0]) if args else 8, unread_only))
    if len(sys.argv) == 2 and sys.argv[1] == "copy-message-link":
        sys.exit(cmd_copy_message_link())
    if len(sys.argv) == 3 and sys.argv[1] == "open":
        sys.exit(cmd_open(sys.argv[2]))
    print(__doc__, file=sys.stderr)
    sys.exit(2)
