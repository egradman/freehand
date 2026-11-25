#!/usr/bin/env bash
# Install freehand: the CLI, its systemd user service, and the bluetoothd override
# that lets it talk to the Siri Remote directly.
set -euo pipefail
cd "$(dirname "$0")"

uv tool install --force --editable .
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
mkdir -p "$config_root/systemd/user"
install -m644 contrib/freehand.service "$config_root/systemd/user/freehand.service"
if [[ ! -f "$config_root/freehand/config.toml" ]]; then
  mkdir -p "$config_root/freehand"
  cp src/freehand/default_config.toml "$config_root/freehand/config.toml"
fi

if [[ ! -f "$config_root/freehand/local.toml" ]]; then
  cp local.example.toml "$config_root/freehand/local.toml"
fi

override=/etc/systemd/system/bluetooth.service.d/freehand-no-hog.conf
if ! cmp -s contrib/bluetooth-no-hog.conf "$override"; then
  echo "Turning off BlueZ's hog plugin needs sudo (Bluetooth LE keyboards/mice stop working):"
  sudo install -D -m644 contrib/bluetooth-no-hog.conf "$override"
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
fi

systemctl --user daemon-reload
systemctl --user enable freehand.service
systemctl --user restart freehand.service

echo
echo "Done. Config: $config_root/freehand/config.toml   Logs: journalctl --user -u freehand -f"
