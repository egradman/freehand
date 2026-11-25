<img src="assets/icon.svg" width="64" height="64" alt="">

# Freehand

A one-time reference snapshot for other people and their coding agents to adapt.
This repository is not maintained; there are no planned updates or support.
Read the code, take the useful ideas, and tailor them to your own desktop.

The original working project was called moter. This copy uses its own `freehand`
command, configuration and state directories so it can be adapted independently.
Demo recordings, editing tools, exports and original Git history are excluded.

## Start here with your agent

Read `AGENTS.md` and the implementation before installing anything. The installer
changes Bluetooth configuration and enables a desktop service. It is a reference
for an Omarchy/Hyprland setup, not a universal installer.

Copy `local.example.toml` to `~/.config/freehand/local.toml` for optional integrations.
Button bindings live in `~/.config/freehand/config.toml`; personal agent notes live
in `~/.config/freehand/agent.md`. XDG configuration/state overrides are respected.
Keep local settings and credentials out of this repository.

Use an Apple Siri Remote (3rd gen, USB-C) on Omarchy. Hold the Siri button to
dictate with Superwhisper, through your computer's mic or the remote's own, and
map every other button to whatever you want.

## How it works

The remote isn't a normal Bluetooth keyboard. Its buttons, touch surface and
mic are Apple-specific HID reports, and it stays silent until the host writes
`0xAF` to its feature report `0xF0`. BlueZ's `hog` plugin claims the HID service
and refuses that write, so bluetoothd runs without it (Bluetooth LE
keyboards/mice won't work while that override is installed). freehand then talks
GATT over D-Bus itself:

| report | what                                                       |
| ------ | ---------------------------------------------------------- |
| `0xFB` | buttons, 2-byte little-endian bitmask                      |
| `0xFC` | touch surface positions (mouse pointer, scroll wheel, pie menus) |
| `0xFA` | mic, one Opus frame (20 ms, 48 kHz mono) per packet, only while Siri is held |

Protocol facts from [azais-corentin/siri-remote](https://github.com/azais-corentin/siri-remote),
confirmed against captures from a real remote.

## Setup

1. `./install.sh` installs the `freehand` command and its systemd user service,
   and uses sudo once for the bluetoothd override (`contrib/bluetooth-no-hog.conf`).
2. Pair the remote: hold **Back + Volume Up** for about 5 seconds next to the
   computer, then pair it with `bluetoothctl` (`scan on`, `pair <addr>`,
   `trust <addr>`). It shows up with **no name**, only an address. Its Modalias
   is `bluetooth:v004Cp0315`, and after pairing it names itself by serial number.
3. `freehand learn` prints each button as you press it.

The remote sleeps when idle. Press any button to wake it; freehand reconnects by itself.

## Configuring

Edit `~/.config/freehand/config.toml`. Changes apply on the next button press.

```toml
[dictation]
mic = "computer"   # or "remote" (the remote's own mic), or "off"
# Optional: click Siri for Enter; hold 300 ms to start recording.
tap = { keys = "Return" }
hold_ms = 300

[buttons.play_pause]
tap = "omarchy-shell media playPause"
hold = { keys = "super+Tab" }    # key chords are sent with wtype

[buttons.volume_up]
tap = "omarchy-audio-output-volume raise"
repeat = true
```

Buttons: `siri tv back select up down left right play_pause mute volume_up volume_down power`.
With `[dictation] tap` set, short Siri clicks run that action without recording;
holds keep the usual dictation or voice-menu behavior. This applies to both mic
modes; `mic = "off"` uses the ordinary Siri button binding instead.
Each can have `press`, `release`, `tap`, `hold` (after `hold_ms`) and `repeat`,
or instead `menu = "name"` (a pie menu) or `mouse = "left"` (a mouse button:
`left`, `right` or `middle`, held for as long as you hold it, so it drags).
An action is a shell command, `{ keys = "ctrl+c" }`, `{ type = "text" }`, or a list of these.

Use `double_tap` and `triple_tap` for separate actions on two or three clicks:

```toml
[buttons.volume_up]
# Call Hyprland directly for compositor actions.
tap = """hyprctl dispatch 'hl.dsp.workspace.toggle_special("qconsole")'"""
double_tap = "freehand quake-window"  # move into Quake, or back to the original workspace
click_ms = 300
```

`click_ms` is the maximum gap from release to the next press, configurable per
button or under `[timing]` (default 300 ms). Only buttons with multi-tap actions
wait for more clicks; the highest configured count fires on release immediately.
With `triple_tap`, a double tap also waits for a possible third click. If no
`double_tap` is bound, two clicks fall back to two single-tap actions. Holds
remain separate; `press` and `release` still run on every transition. Multi-tap
bindings cannot use `menu` or `mouse`. To combine multi-tap with `repeat`, set
an explicit `hold` action; only the hold action repeats. `repeat_ms` overrides
the repeat interval per button.

`freehand quake-window` remembers each window's original workspace in a Hyprland
window tag. Open Quake, focus that window, and double-click again to return it
and close the Quake console.
Windows already in Quake without a saved origin return to the current workspace.

## Application overrides

Application-specific overrides can change ordinary action buttons while keeping
the other gestures from the default binding:

```toml
[applications.t3]
class = "com.t3tools.T3Code"

[applications.t3.buttons.mute]
tap = { keys = "ctrl+shift+s" }  # settle thread; T3 disables this in its terminal
```

The class must match Hyprland's focused window class exactly. The last matching
override for a button wins. Each button gesture keeps the binding chosen on its
first press, including subsequent clicks in a double/triple click. If focus
cannot be read, defaults apply. Siri and mouse buttons keep their global bindings.

Play can open a different pie menu for each application. Leave its global binding
empty so applications without a menu do nothing:

```toml
[buttons.play_pause]

[applications.t3.buttons.play_pause]
menu = "t3_actions"

[menus.t3_actions]
title = "T3 actions"
items = [
  { label = "Settle thread", action = { keys = "ctrl+shift+s" }, position = "n" },
]
```

A menu override replaces the entire default button binding, including tap and
hold actions. Menu names must refer to a defined menu. Press Play again or Back
to close it; other menu buttons switch menus as usual.

## Dwell submenus

Any pie item can optionally set `submenu = "menu_name"` and `dwell_ms = 1500`.
Point toward that slice and keep your thumb down: it fills over 1.5 seconds,
then opens the submenu. Moving to another slice or back to the centre resets
the fill. Lift, then swipe again to choose in the new menu. Back or the original
menu button closes the menu.

An item can use `dwell_action = "omarchy-shell shell toggle omarchy.clipboard"`
instead of `submenu` to close the pie and launch an external picker on linger.
The normal `action` still runs on a quick swipe.

Lifting before the dwell completes runs the item's normal action. An item with
only a submenu opens it on a normal swipe too. Sources may also return an inline
menu definition in `submenu`, with the same fields as a `[menus.NAME]` table.

```toml
[menus.example]
items = [{ label = "App", action = "launch-app", submenu = "app_options", dwell_ms = 1500 }]

[menus.app_options]
items = [{ label = "Special action", action = "app-special-action" }]
```

The T3 thread switcher supplies project submenus automatically. Linger on a
thread to see **New thread** and the other unsettled, unsnoozed top-level threads
in that project on that machine, newest first. **More threads** opens additional
pages. New thread creates an idle thread in that project and opens it for you to
enter a prompt. Listing uses read-only access; creating threads uses a user-approved OAuth
credential with Auto access.

## Pie menus

A button with `menu = "name"` opens a big pie overlay (GTK4 layer-shell; it
never takes focus). Lift the thumb that clicked, then swipe in one of 8
directions and lift: the vector from touch-down to lift picks the slot. A short
swipe does nothing; Back or pressing the opening button again closes the menu.

```toml
[buttons.down]
menu = "apps"

[menus.apps]
items = [
  { position = "w", label = "T3 Code", content = "F1", action = "..." },
  { position = "n", label = "Chrome",  content = "F2", action = "..." },
]

[menus.threads]
voice = "pick"         # hold Siri and say a thread title
source = "@t3-threads"   # 8 most recently active T3 threads, all linked machines
```

`source` can also be any shell command that prints a JSON list of items.
`@t3-threads` asks the local T3 server (with a read-only token from its own CLI)
for threads on this machine and every linked one; `freehand t3-threads` prints
what it would show. Swipe feel is tuned under `[touchpad]` (`swipe`, `preview`,
`hysteresis_deg`).

`source = "@gchat"` shows unread Google Chat conversations from the open app's
Home list and sidebar, including unread thread replies. Loading this menu
reads the current view without navigating Chat.

## Mouse pointer

Slide your thumb on the clickpad to move the pointer, like a laptop trackpad:
motion is relative, so lift and slide again to keep going. Quick strokes go
further than slow ones. Click the centre of the pad to click (the default
config binds `select` to `mouse = "left"`); hold the click and slide to drag.
The pointer holds still for a moment around each click so it lands where you
aimed. It's off while a pie menu is open, and touches that start on the outer
ring scroll instead. Tuned under `[mouse]` (`speed`, `accel`, `max_gain`,
`smoothing`, `click_settle_ms`).

## Scroll wheel

Run your thumb around the outer ring of the clickpad to scroll whatever is
under the mouse pointer: clockwise scrolls down. It works like a click wheel,
one notch per `step_deg` degrees, and is tuned under `[wheel]` (`ring`,
`step_deg`, `clockwise`). Scrolling goes through Wayland's virtual-pointer
protocol (as does the pointer), so it needs no root and no ydotool.

## Telling the computer what to do

Open the app switcher, then hold Siri and say what you want ("open my calendar
and go to next week"). The menu closes, and instead of being typed out your
words go to a computer-use agent: Codex (`codex exec`, your own
login) driving the desktop through [hypruse](https://github.com/IlyasKhallouki/hypruse)
(screenshots, Wayland virtual pointer, wtype, hyprctl; no root). Progress and
the agent's one-line answer show as notifications. Press Back while it's working
to stop it.

The agent's instructions live in `~/.config/freehand/agent.md` (written on first
use). Add your own notes under "The user's notes": apps you use, keyboard
shortcuts, how you like things done. It's read fresh for every request.

```toml
[agent]
model = "gpt-6-astra"  # used for computer control and thread matching

[menus.apps]
voice = "agent"   # add to your existing menu definition
# command = "..."  # any shell command; {text} = the request, {prompt} = agent.md
```

For the thread switcher, set `voice = "pick"` under `[menus.threads]`.
Hold Siri, say something like "rebind chat buttons", then release. Codex matches
against up to 1,000 recent unsettled top-level threads across reachable machines,
including threads beyond the eight visible slots. It returns a candidate index;
freehand opens that candidate's existing action. A missing match opens nothing.
Back cancels recording or a pending match. Outside a voice-enabled menu, dictation
works as usual. Legacy `[agent] menus = ["apps"]` is still supported.

Both paths require a logged-in Codex CLI. Matching runs without the desktop MCP;
computer control connects hypruse. Codex shell tools are disabled and commands run
with the read-only sandbox. Desktop mode explicitly approves hypruse tools so
spoken requests can click and type without an interactive approval prompt.
This approval is scoped to hypruse; thread matching has no desktop tools.
The integration uses the documented
[Codex configuration options](https://developers.openai.com/codex/config-reference).

It records from the same mic as dictation (`computer` uses `pw-record`,
`remote` the remote's own) and transcribes with `superwhisper transcribe`
without pasting. Needs `uv tool install hypruse` (`hypruse doctor` checks the
session). `freehand agent "..."` sends a typed request the same way.

`freehand mic-test` records one Siri-button hold from the remote's mic and transcribes it.
Logs: `journalctl --user -u freehand -f`.

## Undo

```sh
systemctl --user disable --now freehand
sudo rm /etc/systemd/system/bluetooth.service.d/freehand-no-hog.conf
sudo systemctl daemon-reload && sudo systemctl restart bluetooth
```

## Development

`uv run pytest`

### T3 creation menu

Use `source = "@t3-create"` in a menu bound to T3's Play button. The bottom
slice opens a project picker on linger (1.5 seconds), listing available projects
across reachable, writable environments with host labels. Each host also has a
direct **New no-project thread** slice, which creates and opens an idle Scratch
thread there. Project selection creates an idle thread in that project. Large
project lists have **More projects** pages.

### Authorize T3 thread creation

Run `python -m freehand.t3_auth` using freehand's installed Python environment in your
desktop session. The browser opens T3's OAuth consent page: approve **freehand remote**
with **Auto** access. Setup uses PKCE and a local callback, then stores the token
with owner-only permissions at `~/.local/state/freehand/t3-oauth.json` (or under
`XDG_STATE_HOME`). No restart is needed for creation actions.

This T3 version does not expose a refresh-token grant. Reconnect when the token
expires or is revoked. Generic CLI-issued tokens remain limited to read-only
menu discovery; they are never used as a fallback for Auto thread creation.

### Voice search in the Chat switcher

With `voice = "chat-search"` on the Chat menu, a spoken query searches an archive
on a linked T3 environment. Configure your environment ID, archive location,
archive skill, provider and model in `~/.config/freehand/local.toml` first.
No target machine is built in. The remote search starts an Auto session, reads
the archive and returns a message link; Back cancels it. It requires your own
archive, skill and approved T3 connection. See `local.example.toml`.

T3 thread slices use a stable background tint derived from their environment ID.
The same host keeps its tint across refreshes, ordering changes, and project
submenus; selection and dwell highlights remain visible over the tint.

