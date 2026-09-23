# college-wifi-autologin

Automatic login to a college captive portal — works on **Linux, macOS, and Windows**.

The portal this was written for is a Cisco ISE captive portal, but the
selectors are configurable so it can be adapted to any HTML login form.

## What it does

1. **Fast-path.** If you already have internet, exits immediately. No
   browser launches, no portal contact. Runs cheaply every 5 minutes on a
   timer.
2. **Preflight.** Verifies the venv and Chromium work before touching the
   portal — the exact failure mode that silently kills most setups.
3. **Login.** Opens the portal in headless Chromium, waits for the portal's
   RSA encryption to be ready, fills the form, submits via the portal's own
   `oAuthentication.submitActiveForm()` (with fallbacks), and verifies the
   post-login page.
4. **Retry.** Three attempts per run, five-second backoff.
5. **Diagnose.** A `--dry-run` flag and a separate doctor script for when
   something goes wrong.

## Why it exists

**Linux** has no OS-level captive-portal auto-login. NetworkManager detects
the portal but never fills in your credentials, and there's no "remember and
re-login" mechanism. Sessions on this portal expire every ~7 hours; without
this tool you have to open a browser and retype your password mid-day.

**macOS and Windows** already pop up the portal once and remember the
network, so the OS handles the first login. What they *don't* do is notice
the 7-hour session expiry and re-authenticate for you. This tool covers that
gap.

## Requirements

- Python 3.9 or newer
- Linux (systemd user session), macOS 13+, or Windows 10/11
- ~150 MB disk (mostly Chromium)

## Install

### Linux

```bash
git clone https://github.com/<you>/college-wifi-autologin.git
cd college-wifi-autologin
python3 install.py
```

If `python3-venv` isn't installed:

```bash
sudo apt install python3-venv libnotify-bin    # Debian/Ubuntu
sudo dnf install python3-virtualenv            # Fedora
```

The installer creates a venv under `~/.local/share/college-wifi-autologin`,
installs Playwright + Chromium, prompts for your credentials, and installs a
systemd user timer that fires every 5 minutes. It also offers to install a
NetworkManager dispatcher hook (needs `sudo`) that fires the moment you join
the WiFi.

### macOS

```bash
git clone https://github.com/<you>/college-wifi-autologin.git
cd college-wifi-autologin
python3 install.py
```

The installer creates a venv under `~/Library/Application Support/college-wifi-autologin`,
installs Playwright + Chromium, prompts for credentials, and installs a
launchd LaunchAgent that fires every 5 minutes.

### Windows

Open PowerShell (no admin needed):

```powershell
git clone https://github.com/<you>/college-wifi-autologin.git
cd college-wifi-autologin
python install.py
```

The installer creates a venv under `%LOCALAPPDATA%\college-wifi-autologin`,
installs Playwright + Chromium, prompts for credentials, and registers a
Task Scheduler task that fires every 5 minutes and at login.

### Unattended install (any OS)

```bash
COLLEGE_WIFI_USER='yourname' COLLEGE_WIFI_PASS='yourpass' \
    python3 install.py --unattended
```

## Verify

Run the doctor:

```bash
# Linux
~/.local/share/college-wifi-autologin/venv/bin/python \
    ~/.local/share/college-wifi-autologin/bin/college-wifi-doctor.py --launch-test

# macOS
~/Library/Application\ Support/college-wifi-autologin/venv/bin/python \
    ~/Library/Application\ Support/college-wifi-autologin/bin/college-wifi-doctor.py --launch-test

# Windows (PowerShell)
& "$env:LOCALAPPDATA\college-wifi-autologin\venv\Scripts\python.exe" `
    "$env:LOCALAPPDATA\college-wifi-autologin\bin\college-wifi-doctor.py" --launch-test
```

You should see all-green `OK` lines. If any line says `FAIL`, the doctor
prints the exact command to fix it.

Then trigger a live login:

```bash
# Linux
systemctl --user start wifi-watch.service
tail -30 ~/.local/share/college-wifi-autologin/logs/login.log

# macOS
launchctl kickstart -k gui/$(id -u)/com.college-wifi-autologin.watch
tail -30 ~/Library/Application\ Support/college-wifi-autologin/logs/login.log

# Windows
schtasks /Run /TN CollegeWiFiAutoLogin
Get-Content "$env:LOCALAPPDATA\college-wifi-autologin\logs\login.log" -Tail 30
```

Expected success line: `Portal shows 'Network Access Granted'` or
`already authenticated`.

## Uninstall

```bash
python3 uninstall.py            # keeps credentials + logs
python3 uninstall.py --purge    # removes everything
```

## Configuration

Config lives in the platform-appropriate location:

| OS      | Config file                                                       |
|---------|-------------------------------------------------------------------|
| Linux   | `~/.config/college-wifi-autologin/wifi-login.env`                 |
| macOS   | `~/Library/Application Support/college-wifi-autologin/wifi-login.env` |
| Windows | `%LOCALAPPDATA%\college-wifi-autologin\wifi-login.env`            |

Everything is optional except the two credentials. See
[`config/wifi-login.env.example`](config/wifi-login.env.example) for the full
list with comments.

Environment variables override the file:

| Variable                  | Default                          |
|---------------------------|----------------------------------|
| `COLLEGE_WIFI_USER`       | (required)                       |
| `COLLEGE_WIFI_PASS`       | (required)                       |
| `COLLEGE_WIFI_PORTAL_URL` | `https://172.22.2.6/connect/PortalMain` |
| `WIFI_LOGIN_HEADLESS`     | `1` (set to `0` for a visible browser) |
| `WIFI_LOGIN_TIMEOUT`      | `30` seconds per step            |
| `WIFI_LOGIN_LOG_FILE`     | `<data dir>/logs/login.log`      |

## Adapting for a different college's portal

The login script has three things to change, all at the top of
`bin/college-wifi-login.py`:

1. **`DEFAULT_PORTAL_URL`** — the login page URL. Open the portal in a
   browser, note the URL of the page that asks for your username, paste it
   here.
2. **`USERNAME_SELECTORS` / `PASSWORD_SELECTORS`** — the CSS selectors for
   the input fields. In your browser: right-click the username field → Inspect
   → note the `id` or `name` attribute. Put `#the-id` (or
   `input[name="the-name"]`) at the top of the appropriate tuple.
3. **`SUCCESS_MARKERS` / `FAILURE_MARKERS`** — text that appears on the
   post-login page and on a failed login. Log in manually once, then
   Ctrl-F on the page for the phrase. If the portal says "You're online"
   instead of "Network Access Granted", add that string.

You don't need to change the RSA check or the submit fallbacks — those are
generic Cisco ISE hooks that most captive portals implement.

## Troubleshooting

### "browser launch failed: Executable doesn't exist"

The Playwright Python package was upgraded but the matching browser binary
wasn't downloaded. Fix:

```bash
<python> -m playwright install chromium
```

Where `<python>` is the venv Python for your OS (see Verify section above).

### "login form did not appear"

Either the selectors are wrong (see Adapting above) or the portal is showing
a pre-login page (like an acceptable-use agreement) before the credentials
form. Open the portal manually in a browser and look at the sequence of
pages. If there's an agreement page, you'll need to add handling for it.

Screenshots of the failure are saved to the `screenshots/` subdirectory of
your data dir. Open the most recent one to see exactly what the browser
saw.

### "portal reported a failure"

Your credentials are wrong, or the portal locked your account. Run the
login script with `--no-headless` and watch:

```bash
<python> <login_script> --no-headless
```

### Timer fires but nothing happens

Check the log first. If the log ends with `already online`, the tool did its
job — the fast-path correctly skipped work.

If the log is empty, the timer isn't actually running. Check:

```bash
# Linux
systemctl --user status wifi-watch.timer
journalctl --user -u wifi-watch.service -n 50

# macOS
launchctl print gui/$(id -u)/com.college-wifi-autologin.watch

# Windows
schtasks /Query /TN CollegeWiFiAutoLogin /V /FO LIST
```

### I'm offline but the tool says "already online"

The `have_internet()` check probes two known captive-portal detection URLs.
If your college blocks *both*, the fast-path falsely reports online. You
can override with `--skip-internet-check` in the trigger command, or edit
`have_internet()` to add another probe.

## Security

- **Credentials file permissions** are enforced to `0600` on install (Unix).
  The installer warns if it can't set them.
- **Nothing runs as root.** The Linux systemd unit runs in your user
  session. The optional NetworkManager dispatcher script runs as root
  *only* to `runuser` into your account — the actual login script always
  runs as you.
- **Credentials are never sent anywhere except the portal.** The only
  external network calls are the two captive-portal probes
  (`connectivitycheck.gstatic.com` and `detectportal.firefox.com`) and the
  portal itself.
- **Do not commit `wifi-login.env`.** `.gitignore` blocks it. If you ever
  commit it by accident, change your password and use
  [`git-filter-repo`](https://github.com/newren/git-filter-repo) to scrub
  history.

## How it works

```
                ┌─────────────────────────┐
                │  systemd timer /        │
                │  launchd / Task         │
                │  Scheduler (5-min)      │
                └────────────┬────────────┘
                             │
                ┌────────────▼────────────┐
                │ college-wifi-login.py   │
                │                         │
                │  1. have_internet()?    │──── yes ──► exit 0
                │  2. Acquire lock        │
                │  3. Launch Chromium     │
                │  4. Wait for RSA ready  │
                │  5. Fill, submit        │
                │  6. Verify success page │
                │  7. Release lock        │
                └─────────────────────────┘
```

The login script is self-contained. It reads its own config file, resolves
its own paths, and doesn't rely on any shell environment beyond `PATH`.

## Contributing

See [`docs/PORTING.md`](docs/PORTING.md) for notes on adding support for
another OS or another portal type.

Pull requests welcome. Please run the doctor script after any change to
verify nothing broke.

## License

MIT — see [`LICENSE`](LICENSE).