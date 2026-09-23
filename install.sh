#!/bin/bash
# install.sh — install college-wifi-autologin for the current user.
#
# Usage:  ./install.sh
#
# Idempotent: safe to re-run after a `git pull`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/college-wifi"
ENV_FILE="$HOME/.config/wifi-login.env"
SYSTEMD_DIR="$HOME/.config/systemd/user"
NM_DISPATCHER="/etc/NetworkManager/dispatcher.d/90-college-wifi-login"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

# --- 1. Dependency check ---------------------------------------------------
say "checking dependencies"
missing=()
command -v python3 >/dev/null || missing+=("python3")
command -v curl    >/dev/null || missing+=("curl")
dpkg -s python3-venv >/dev/null 2>&1 || missing+=("python3-venv")

if [ "${#missing[@]}" -gt 0 ]; then
    die "missing packages: ${missing[*]}
    install with:  sudo apt install ${missing[*]}"
fi

command -v notify-send >/dev/null || \
    warn "notify-send not found (optional): sudo apt install libnotify-bin"

# --- 2. Credentials --------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
    say "credentials file already exists at $ENV_FILE — leaving untouched"
else
    say "credentials file missing; creating $ENV_FILE"
    read -rp  "  College WiFi username: " WIFI_USER
    read -rsp "  College WiFi password: " WIFI_PASS; echo
    mkdir -p "$(dirname "$ENV_FILE")"
    cat > "$ENV_FILE" <<EOF
# College WiFi credentials. Do not commit.
export COLLEGE_WIFI_USER='${WIFI_USER//\'/\'\\\'\'}'
export COLLEGE_WIFI_PASS='${WIFI_PASS//\'/\'\\\'\'}'
EOF
    chmod 600 "$ENV_FILE"
    say "wrote $ENV_FILE (mode 600)"
fi

# --- 3. Install scripts + venv --------------------------------------------
say "installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 0755 "$REPO_DIR/bin/college-wifi-trigger.sh" "$INSTALL_DIR/"
install -m 0755 "$REPO_DIR/bin/college-wifi-login.py"  "$INSTALL_DIR/"
install -m 0755 "$REPO_DIR/bin/college-wifi-doctor.sh" "$INSTALL_DIR/"

if [ ! -d "$INSTALL_DIR/venv" ]; then
    say "creating virtualenv"
    python3 -m venv "$INSTALL_DIR/venv"
fi

say "installing python packages"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

say "installing playwright browser (this may take a minute)"
"$INSTALL_DIR/venv/bin/python" -m playwright install chromium

# --- 4. systemd user units -------------------------------------------------
say "installing systemd user units"
mkdir -p "$SYSTEMD_DIR"

sed "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    "$REPO_DIR/systemd/wifi-watch.service.in" > "$SYSTEMD_DIR/wifi-watch.service"
install -m 0644 "$REPO_DIR/systemd/wifi-watch.timer" "$SYSTEMD_DIR/wifi-watch.timer"

systemctl --user daemon-reload
systemctl --user enable --now wifi-watch.timer

# --- 5. NetworkManager dispatcher (needs sudo) ----------------------------
if [ -d /etc/NetworkManager/dispatcher.d ]; then
    say "installing NetworkManager dispatcher (requires sudo)"
    TMP="$(mktemp)"
    sed -e "s|@USER@|$(id -un)|g" \
        -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "$REPO_DIR/networkmanager/90-college-wifi-login.in" > "$TMP"
    sudo install -m 0700 -o root -g root "$TMP" "$NM_DISPATCHER"
    rm -f "$TMP"
    say "installed $NM_DISPATCHER"
else
    warn "NetworkManager dispatcher dir missing; timer alone will suffice"
fi

# --- 6. Smoke test --------------------------------------------------------
say "running smoke test"
if "$INSTALL_DIR/college-wifi-doctor.sh"; then
    say "doctor OK"
else
    warn "doctor reported problems — review its output above"
fi

say "done.  Log: $INSTALL_DIR/../state/college-wifi/login.log"
say "       Timer next fires: $(systemctl --user list-timers wifi-watch.timer --no-pager | sed -n 2p)"