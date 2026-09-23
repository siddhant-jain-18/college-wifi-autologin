#!/bin/bash
# college-wifi-doctor.sh — diagnose the auto-login setup.
set -u
PYTHON="$HOME/college-wifi-venv/bin/python"
SCRIPT="$HOME/college-wifi-login.py"
ENV_FILE="$HOME/.config/wifi-login.env"
PORTAL_URL="https://172.22.2.6/connect/PortalMain"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/college-wifi/login.log"

echo "== files =="
for f in "$PYTHON" "$SCRIPT" "$ENV_FILE"; do
    if [ -f "$f" ]; then echo "  OK   $f"; else echo "  MISS $f"; fi
done

echo; echo "== credentials =="
if [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE"; set +a
    echo "  user: ${COLLEGE_WIFI_USER:-<unset>}"
    echo "  pass: ${COLLEGE_WIFI_PASS:+<set, ${#COLLEGE_WIFI_PASS} chars>}"
fi

echo; echo "== internet =="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://connectivitycheck.gstatic.com/generate_204" || echo 000)
[ "$code" = "204" ] && echo "  online" || echo "  offline (captive portal or no connectivity)"

echo; echo "== portal =="
if curl -s -k -o /dev/null --connect-timeout 5 --max-time 10 "$PORTAL_URL"; then
    echo "  reachable: $PORTAL_URL"
else
    echo "  NOT reachable: $PORTAL_URL"
fi

echo; echo "== playwright =="
"$PYTHON" - <<'PY' 2>&1 | sed 's/^/  /'
import sys
try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print(f"import failed: {e}"); sys.exit(2)
try:
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        b.close()
    print("OK (chromium launches)")
except Exception as e:
    print(f"launch failed: {e}"); sys.exit(3)
PY

echo; echo "== systemd timer =="
systemctl --user list-timers --no-pager 2>/dev/null | grep -E 'NEXT|wifi-watch' || true

echo; echo "== last 20 log lines =="
if [ -f "$LOG" ]; then tail -20 "$LOG"; else echo "  (no log yet)"; fi	w