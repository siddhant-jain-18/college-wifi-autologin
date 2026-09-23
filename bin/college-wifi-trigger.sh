#!/bin/bash
# college-wifi-trigger.sh — robust wrapper for the college WiFi auto-login.
#
# Invoked by:
#   ~/.config/systemd/user/wifi-watch.timer          (every 5 min)
#   /etc/NetworkManager/dispatcher.d/90-college-wifi-login
#
# Flow:
#   1. Load credentials from ~/.config/wifi-login.env
#   2. Fast-path: if we already have internet, exit 0 immediately
#   3. Preflight: verify the venv + Playwright browser actually work
#   4. Serialize with flock so parallel triggers don't fight
#   5. Wait for the network to settle; confirm the portal is reachable
#   6. Run the login script with a hard timeout
#   7. Log everything; desktop-notify on failure

set -u
set -o pipefail

# --- Configuration ---------------------------------------------------------
PORTAL_URL="https://172.22.2.6/connect/PortalMain"
INSTALL_DIR="$HOME/.local/share/college-wifi"
LOGIN_SCRIPT="$INSTALL_DIR/college-wifi-login.py"
PYTHON="$INSTALL_DIR/venv/bin/python"
ENV_FILE="$HOME/.config/wifi-login.env"

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/college-wifi"
LOG="$STATE_DIR/login.log"
LOG_MAX_BYTES=$((1024 * 1024))
LOG_KEEP=5

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ ! -d "$RUNTIME_DIR" ] || [ ! -w "$RUNTIME_DIR" ]; then
    RUNTIME_DIR="/tmp"
fi
LOCK_FILE="$RUNTIME_DIR/college-wifi-login.lock"

SCRIPT_TIMEOUT=120      # hard limit on the python login script (seconds)
NET_SETTLE_DELAY=5      # wait after a network event before touching the portal

# --- Logging ---------------------------------------------------------------
mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true

log()       { printf '%s [%-5s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2" >> "$LOG"; }
log_info()  { log INFO  "$1"; }
log_warn()  { log WARN  "$1"; }
log_error() { log ERROR "$1"; }

rotate_log() {
    [ -f "$LOG" ] || return 0
    local size
    size=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    [ "$size" -gt "$LOG_MAX_BYTES" ] || return 0
    local i
    for i in $(seq $((LOG_KEEP - 1)) -1 1); do
        [ -f "$LOG.$i" ] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
    done
    mv -f "$LOG" "$LOG.1"
}

# --- Notification (best-effort; no-op without a session bus) --------------
notify() {
    local urgency="$1" summary="$2" body="$3"
    if command -v notify-send >/dev/null 2>&1 \
       && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        notify-send -u "$urgency" -a "College WiFi" "$summary" "$body" \
            2>/dev/null || true
    fi
}

# --- Probes ----------------------------------------------------------------
have_internet() {
    # Probe 1: gstatic's 204 endpoint. A clean 204 with an EMPTY body means
    # real internet — not a captive-portal MITM returning 200/302.
    local out body code
    out=$(curl -s -w '\n%{http_code}' --connect-timeout 3 --max-time 5 \
        "http://connectivitycheck.gstatic.com/generate_204" 2>/dev/null || echo "")
    body="${out%$'\n'*}"
    code="${out##*$'\n'}"
    if [ "$code" = "204" ] && [ -z "$body" ]; then
        return 0
    fi

    # Probe 2: firefox's success endpoint, in case gstatic is blocked.
    out=$(curl -s -w '\n%{http_code}' --connect-timeout 3 --max-time 5 \
        "http://detectportal.firefox.com/success.txt" 2>/dev/null || echo "")
    body="${out%$'\n'*}"
    code="${out##*$'\n'}"
    if [ "$code" = "200" ] && [ "$body" = "success" ]; then
        return 0
    fi

    return 1
}

portal_reachable() {
    # Any HTTP response (even an error/redirect) means the host is up.
    curl -s -k -o /dev/null --connect-timeout 5 --max-time 10 \
        "$PORTAL_URL" 2>/dev/null
}

# --- Preflight -------------------------------------------------------------
preflight() {
    if [ ! -x "$PYTHON" ]; then
        log_error "preflight: python not found at $PYTHON"; return 1
    fi
    if [ ! -f "$LOGIN_SCRIPT" ]; then
        log_error "preflight: login script not found at $LOGIN_SCRIPT"; return 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        log_error "preflight: credentials file missing at $ENV_FILE"; return 1
    fi

    # The check that would have caught the "Playwright upgraded, browsers
    # not reinstalled" failure before it silently killed every run.
    local out
    if ! out=$("$PYTHON" - <<'PY' 2>&1
import sys
try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print(f"playwright import failed: {e}")
    sys.exit(2)
try:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        b.close()
except Exception as e:
    print(f"chromium launch failed: {e}")
    sys.exit(3)
PY
    ); then
        log_error "preflight: $out"
        notify critical "College WiFi" "Playwright broken: $out"
        return 1
    fi
    return 0
}

# --- Main ------------------------------------------------------------------
main() {
    rotate_log

    if [ ! -f "$ENV_FILE" ]; then
        log_error "credentials file missing: $ENV_FILE"
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a

    # Fast-path: nothing to do if we already have internet.
    if have_internet; then
        log_info "already online; nothing to do"
        exit 0
    fi

    log_info "offline; will check portal"

    preflight || exit 1

    # Serialize concurrent runs (timer + dispatcher can race).
    exec 200>"$LOCK_FILE" 2>/dev/null || {
        log_error "cannot create lock file at $LOCK_FILE"; exit 1
    }
    if ! flock -n 200; then
        log_info "another instance is already running; exiting"
        exit 0
    fi

    sleep "$NET_SETTLE_DELAY"

    if ! portal_reachable; then
        log_warn "portal not reachable; will retry on the next timer tick"
        exit 1
    fi

    log_info "portal reachable; running login script"
    if timeout --kill-after=10 "$SCRIPT_TIMEOUT" \
            "$PYTHON" "$LOGIN_SCRIPT" >> "$LOG" 2>&1; then
        log_info "login succeeded"
        notify normal "College WiFi" "Logged in successfully"
        exit 0
    else
        local rc=$?
        log_error "login script failed (rc=$rc)"
        notify critical "College WiFi" "Login failed (rc=$rc). See $LOG"
        exit "$rc"
    fi
}

main "$@"