# Freehand reference snapshot

This is a one-time publication of a working desktop experiment, intended as
inspiration for the reader's own agent. No maintenance or future updates are
promised. Adapt it to your user's machine instead of assuming this exact stack.

- Ask your user which features they want before installing system integration.
- Inspect `install.sh`: it disables BlueZ's hog plugin, affecting Bluetooth LE
  keyboards and mice, and enables a systemd user service. Do not run it just to
  explore this repository.
- The package/CLI is `freehand`. Button/menu configuration is
  `$XDG_CONFIG_HOME/freehand/config.toml` (default `~/.config/freehand/config.toml`).
  Start from `src/freehand/default_config.toml` and adapt application commands.
- Optional machine-specific integrations use `freehand/local.toml` under the same
  configuration root. Start from `local.example.toml`; archive search is disabled
  until its settings are filled in. Supply your own archive and search skill.
- Agent instructions live in `freehand/agent.md` under the configuration root.
  Review its desktop assumptions and supply your own model/provider and login.
- OAuth credentials belong in the user's state directory, never the repository.
  Do not copy private chats, real machine IDs, personal paths or recordings into
  fixtures, examples, documentation or commits.
- `src/freehand/siri.py` handles the remote protocol; `engine.py` handles button
  gestures; `pie.py`/`pie_ui.py` handle menus; `mouse.py`/`wheel.py` handle movement;
  `agent.py`, `t3.py`, `gchat.py` and `chat_search.py` are optional integrations.
- `events.py` is an optional runtime event logger, disabled by default. Its output
  can contain spoken text and window/menu information. Keep logs private.
- Run `uv run pytest` after adapting behavior. Hardware, desktop accessibility,
  Bluetooth and remote-agent behavior need validation on your user's machine.
