#!/bin/bash
set -euo pipefail

INSTALL_DIR="$HOME/.local/share/college-wifi"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/college-wifi"
NM_DISPATCHER="/etc/NetworkManager/dispatcher.d/90-college-wifi-login"

systemctl --user disable --now wifi-watch.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/wifi-watch.service" \
      "$HOME/.config/systemd/user/wifi-watch.timer"
systemctl --user daemon-reload

if [ -f "$NM_DISPATCHER" ]; then
    sudo rm -f "$NM_DISPATCHER"
fi

rm -rf "$INSTALL_DIR"
# Keep state and credentials unless --purge is passed.
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$STATE_DIR"
    rm -f  "$HOME/.config/wifi-login.env"
    echo "purged state and credentials"
fi

echo "uninstalled"