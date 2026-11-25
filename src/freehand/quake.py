"""Move a window into Quake and remember its workspace on the window itself."""

import json
import subprocess

CONSOLE = "special:qconsole"
ORIGIN = "freehand-quake-from-"


def hypr(*args):
    return subprocess.check_output(["hyprctl", *args], text=True).strip()


def dispatch(call, **fields):
    # JSON strings are also Lua strings for workspace names and window addresses.
    values = ", ".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    result = hypr("dispatch", f"hl.dsp.{call}({{ {values} }})")
    if result != "ok":
        raise RuntimeError(f"Hyprland: {result}")


def toggle_window():
    active = json.loads(hypr("-j", "activewindow"))
    if not active.get("address"):
        return
    window = "address:" + active["address"]
    workspace = active["workspace"]["name"]
    tags = [tag for tag in active.get("tags", []) if tag.startswith(ORIGIN)]
    if workspace != CONSOLE:
        for tag in tags:
            dispatch("window.tag", window=window, tag="-" + tag)
        # A window tag survives freehand restarts and disappears when the window closes.
        dispatch("window.tag", window=window, tag="+" + ORIGIN + workspace.encode().hex())
        dispatch("window.move", window=window, workspace=CONSOLE, follow=False)
        return

    destination = None
    for tag in tags:
        try:
            destination = bytes.fromhex(tag[len(ORIGIN):]).decode()
        except (ValueError, UnicodeDecodeError):
            continue
        if destination and destination != CONSOLE:
            break
        destination = None
    if destination is None:
        # Windows placed here before this feature have no recorded origin.
        monitors = json.loads(hypr("-j", "monitors"))
        destination = next(m["activeWorkspace"]["name"] for m in monitors if m["focused"])
    dispatch("window.move", window=window, workspace=destination, follow=False)
    for tag in tags:
        dispatch("window.tag", window=window, tag="-" + tag)
    # Moving the last window can already close the console. Only toggle it if
    # it remains visible, so returning a window never reopens an empty console.
    monitors = json.loads(hypr("-j", "monitors"))
    if any(m["focused"] and m.get("specialWorkspace", {}).get("name") == CONSOLE
           for m in monitors):
        result = hypr("dispatch", 'hl.dsp.workspace.toggle_special("qconsole")')
        if result != "ok":
            raise RuntimeError(f"Hyprland: {result}")
