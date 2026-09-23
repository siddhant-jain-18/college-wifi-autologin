# college-wifi-autologin

[![CI](https://github.com/siddhant-jain-18/college-wifi-autologin/actions/workflows/ci.yml/badge.svg)](https://github.com/siddhant-jain-18/college-wifi-autologin/actions/workflows/ci.yml)

Automatic login to a college captive portal — works on **Linux, macOS and
Windows**.

The portal this was written for is a Cisco ISE captive portal, but the
selectors and the success/failure markers are configurable, so it can be
adapted to almost any HTML login form.

## What it does

1. **Fast path.** If you already have internet, it exits immediately. No
   browser, no portal contact. Cheap enough to run every 5 minutes.
2. **Reachability check.** If there is no internet *and* the portal does not
   answer, it exits without launching a browser (typical when you are simply
   off campus).
3. **Login.** Opens the portal in headless Chromium, waits for the portal's
   RSA encryption to be ready, fills the form, submits via the portal's own
   `oAuthentication.submitActiveForm()` (with CSS-click and Enter-key
   fallbacks), then verifies the post-login page.
4. **Retry.** Three attempts per run, five-second backoff. If the browser
   dies mid-run it is relaunched cleanly on the next attempt.
5. **Diagnose.** `--dry-run`, screenshots on every failure, a rotating log,
   and a separate doctor script that returns a non-zero exit code when
   something is broken.

## Why it exists

**Linux** has no OS-level captive-portal auto-login. NetworkManager detects
the portal but never fills in your credentials, and there is no "remember and
re-login" mechanism. Sessions on this portal expire every ~7 hours; without
this tool you have to open a browser and retype your password mid-day.

**macOS and Windows** already pop up the portal once and remember the
network, so the OS handles the first login. What they *don't* do is notice
the session expiry and re-authenticate for you. This tool covers that gap.

## Requirements

- Python 3.9 or newer
- Linux (systemd user session), macOS 13+, or Windows 10/11
- ~150 MB disk (mostly the Chromium download)

`platformdirs` is used when present but is not required: without it the
scripts fall back to the same per-OS directories computed by hand, so the
installer can bootstrap itself on a bare Python.

## Install

Clone once:

```bash
git clone https://github.com/siddhant-jain-18/college-wifi-autologin.git
cd college-wifi-autologin
```

Then run the installer for your OS.

### Linux

```bash
python3 install.py
```

If `python3-venv` is missing:

```bash
sudo apt install python3-venv libnotify-bin    # Debian/Ubuntu
sudo dnf install python3-virtualenv            # Fedora
```

The installer creates a venv under `~/.local/share/college-wifi-autologin`,
installs Playwright + Chromium, prompts for credentials, installs a systemd
user timer that fires every 5 minutes, and offers to install a
NetworkManager dispatcher hook (`sudo`, optional) that fires the moment you
join the WiFi.

### macOS

```bash
python3 install.py
```

Venv under `~/Library/Application Support/college-wifi-autologin`, plus a
launchd LaunchAgent that fires every 5 minutes and at login.

### Windows

Open PowerShell (no admin needed):

```powershell
python install.py
```

Venv under `%LOCALAPPDATA%\college-wifi-autologin`, plus a Task Scheduler
task that fires every 5 minutes, at logon and at boot.

### Unattended install (any OS)

```bash
COLLEGE_WIFI_USER='yourname' COLLEGE_WIFI_PASS='yourpass' \
    python3 install.py --unattended
```

Unattended mode never blocks on a password prompt: `sudo -n` is used for the
optional NetworkManager hook and a failure there is only a warning.

### Installer flags

| Flag                  | Effect                                                        |
|-----------------------|---------------------------------------------------------------|
| `--unattended`        | Read credentials from env vars; never prompt                   |
| `--reconfigure`       | Re-prompt even if a credentials file already exists            |
| `--install-dir DIR`   | Use a custom data directory                                    |
| `--skip-playwright`   | Don't download Chromium                                        |
| `--skip-deps`         | Don't pip-install anything (assume the venv is ready)          |
| `--no-trigger`        | Install files only; register no scheduler                      |
| `--no-dispatcher`     | Linux: skip the NetworkManager hook                            |
| `--no-smoke-test`     | Skip the final `--dry-run`                                     |
| `--verbose`           | Show full pip / playwright output                              |
| `--version`           | Print the version                                              |

The installer is safe to re-run; an existing venv is reused and an existing
credentials file is left untouched.

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

Every line is `OK`, `WARN` or `FAIL`, and the doctor exits non-zero if
anything `FAIL`ed, so it is safe to call from your own scripts. Add `--json`
for machine-readable output.

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

Expected success line: `Portal shows 'Network Access Granted'`, `already
authenticated`, or `already online; nothing to do`.

## Uninstall

```bash
python3 uninstall.py            # keeps credentials + logs/screenshots
python3 uninstall.py --purge    # removes absolutely everything
python3 uninstall.py --dry-run  # show what would be removed
```

The default removes the trigger, the venv and the installed scripts while
keeping your credentials and logs. `--purge` also removes the config
directory.

## Configuration

Config lives in the platform-appropriate location:

| OS      | Config file                                                             |
|---------|-------------------------------------------------------------------------|
| Linux   | `~/.config/college-wifi-autologin/wifi-login.env`                       |
| macOS   | `~/Library/Application Support/college-wifi-autologin/wifi-login.env`   |
| Windows | `%LOCALAPPDATA%\college-wifi-autologin\wifi-login.env`                  |

Logs, screenshots and the lock file live under the data dir
(`~/.local/share/...`, `~/Library/Application Support/...`,
`%LOCALAPPDATA%\...`).

Only the two credentials are required. Environment variables override the
file, and command-line flags override both.

| Variable                        | Default                        | Meaning                                              |
|---------------------------------|--------------------------------|------------------------------------------------------|
| `COLLEGE_WIFI_USER`             | *(required)*                   | Portal username                                      |
| `COLLEGE_WIFI_PASS`             | *(required)*                   | Portal password                                      |
| `COLLEGE_WIFI_PORTAL_URL`       | `https://172.22.2.6/connect/PortalMain` | Login page URL                              |
| `COLLEGE_WIFI_CONFIG_FILE`      | platform config dir            | Use a different env file                             |
| `WIFI_LOGIN_HEADLESS`           | `1`                            | `0` opens a visible browser                          |
| `WIFI_LOGIN_TIMEOUT`            | `30`                           | Per-step timeout, seconds                            |
| `WIFI_LOGIN_ATTEMPTS`           | `3`                            | Attempts per run                                     |
| `WIFI_LOGIN_BACKOFF`            | `5`                            | Seconds between attempts                             |
| `WIFI_LOGIN_POST_SUBMIT`        | `25`                           | Seconds to wait for the success page                 |
| `WIFI_LOGIN_RSA_TIMEOUT`        | `15`                           | Seconds to wait for `cpRSAobj` (`0` disables)        |
| `WIFI_LOGIN_CONNECTIVITY_TIMEOUT` | `5`                          | Seconds per internet probe                           |
| `WIFI_LOGIN_BLOCK_RESOURCES`    | `1`                            | Block images/fonts/media for faster loads            |
| `WIFI_LOGIN_LOG_FILE`           | `<data>/logs/login.log`        | Log destination                                      |
| `WIFI_LOGIN_SCREENSHOT_DIR`     | `<data>/screenshots`           | Failure screenshots                                  |
| `WIFI_LOGIN_LOCK_FILE`          | `<data>/login.lock`            | Concurrency lock                                     |
| `WIFI_LOGIN_USER_SELECTOR`      | *(extra)*                      | Extra CSS selectors for the username field           |
| `WIFI_LOGIN_PASSWORD_SELECTOR`  | *(extra)*                      | Extra CSS selectors for the password field           |
| `WIFI_LOGIN_SUBMIT_SELECTOR`    | *(extra)*                      | Extra CSS selectors for the submit control           |
| `WIFI_LOGIN_NO_SANDBOX`         | unset                          | Force `--no-sandbox` from the first launch           |

Selector variables accept several selectors separated by commas or newlines;
yours are tried *before* the built-in ones. See
[`config/wifi-login.env.example`](config/wifi-login.env.example) for a
commented template.

## Exit codes

`college-wifi-login.py` is designed to be called from a scheduler:

| Code | Meaning                                                             |
|------|---------------------------------------------------------------------|
| `0`  | Logged in, already online, or another instance is already running   |
| `1`  | Login was attempted and failed                                      |
| `2`  | Configuration problem (missing credentials, unreadable config)      |
| `3`  | Environment problem (Playwright or the browser is broken)           |
| `4`  | No internet and the portal is not reachable                         |

`college-wifi-doctor.py` exits `1` if any check failed, `0` otherwise.

## Adapting for a different college's portal

Everything you need to change is at the top of `bin/college_wifi_common.py`,
or — better — in your env file, so you never have to patch the code:

1. **`COLLEGE_WIFI_PORTAL_URL`** — the URL of the page that asks for your
   username. Open the portal in a browser and copy the address.
2. **`WIFI_LOGIN_USER_SELECTOR` / `WIFI_LOGIN_PASSWORD_SELECTOR`** — the CSS
   selectors for the input fields. Right-click the field → Inspect → use the
   `id` (`#the-id`) or the `name` (`input[name="the-name"]`).
3. **`WIFI_LOGIN_SUBMIT_SELECTOR`** — if the submit button is not a plain
   `<button>`/`<input type=submit>`, add its selector.

If your portal's success page uses different wording, add the phrase to
`SUCCESS_MARKERS` in `bin/college_wifi_common.py` — that is the only place
the text lives.

## How it works

```
                ┌─────────────────────────┐
                │  systemd timer /        │
                │  launchd / Task         │
                │  Scheduler / NM hook    │
                └────────────┬────────────┘
                             │
                ┌────────────▼────────────┐
                │ college-wifi-login.py   │
                │                         │
                │  1. already online? ────┼──► exit 0
                │  2. portal reachable? ──┼──► exit 4
                │  3. Acquire lock        │
                │  4. Launch Chromium     │
                │  5. Wait for RSA ready  │
                │  6. Fill, submit        │
                │  7. Verify success page │
                │  8. Release lock        │
                └─────────────────────────┘
```

The three scripts are standalone: each resolves its own paths, reads its own
config file and does not depend on any shell environment beyond `PATH`. They
share `bin/college_wifi_common.py`, which is copied next to them on install.

| File                            | Role                                            |
|---------------------------------|-------------------------------------------------|
| `bin/college-wifi-login.py`     | The login run                                    |
| `bin/college-wifi-doctor.py`    | Diagnostics                                      |
| `bin/college_wifi_common.py`    | Paths, config, logging, probes, lock (shared)    |
| `install.py` / `uninstall.py`   | Setup and teardown for all three platforms       |
| `platform/<os>/…`               | The scheduled-trigger templates                  |
| `tests/`                        | The unit/integration test suite                  |

## Development

```bash
# Syntax check
python3 -m py_compile install.py uninstall.py bin/*.py tests/*.py

# Test suite (stdlib only, no network, no browser)
python3 -m unittest discover -s tests -t . -v

# See what the login script thinks before it touches anything
python3 bin/college-wifi-login.py --dry-run

# Explain the installation state
python3 bin/college-wifi-doctor.py
```

CI runs the test suite on Linux, macOS and Windows for Python 3.9 and 3.12,
plus an installer smoke test on each OS.

## Security

- **Credentials file permissions** are enforced to `0600` on install (Unix).
  The doctor warns if they drift.
- **Nothing runs as root.** The Linux systemd unit runs in your user
  session. The optional NetworkManager dispatcher script runs as root *only*
  to `runuser` into your account — the login script always runs as you.
- **The browser sandbox stays on** unless a normal launch fails, or you set
  `WIFI_LOGIN_NO_SANDBOX=1`. The old `--disable-web-security` flag is gone.
- **Certificate errors are ignored only inside the portal's browser
  context** — captive portals routinely use self-signed certificates.
- **Credentials are never sent anywhere except the portal.** The only other
  network calls are the captive-portal probes
  (`connectivitycheck.gstatic.com`, `detectportal.firefox.com`,
  `cp.cloudflare.com`) and the portal itself.
- **Do not commit `wifi-login.env`.** `.gitignore` blocks it. If you ever
  commit it by accident, change your password and use
  [`git-filter-repo`](https://github.com/newren/git-filter-repo) to scrub
  history.

## Troubleshooting

### "browser launch failed: Executable doesn't exist"

The Playwright Python package was upgraded but the matching browser binary
wasn't downloaded. Fix:

```bash
<python> -m playwright install chromium
```

`<python>` is the venv Python for your OS (see Verify above). The installer's
"verifying that Chromium actually launches" step and the doctor's
`--launch-test` both catch this before it bites you at 3 AM.

### "login form did not appear"

Either the selectors are wrong (see Adapting above) or the portal shows a
pre-login page (acceptable-use agreement, "click to continue") before the
credentials form. Open the portal manually in a browser and look at the
sequence of pages.

Screenshots of every failure are written to the `screenshots/` subdirectory
of your data dir, named with a millisecond timestamp.

### "portal reported a failure"

Your credentials are wrong, or the portal locked your account. Run the login
script with `--no-headless` and watch:

```bash
<python> <login_script> --no-headless --verbose
```

### "no internet and the portal is not reachable" (exit 4)

Expected when you are off campus. If you *are* on campus and the portal
blocks `HEAD` requests, pass `--skip-internet-check` to the login script (or
set `WIFI_LOGIN_*` accordingly) to force an attempt.

### Timer fires but nothing happens

Check the log first. If it ends with `already online; nothing to do`, the
tool did its job.

If the log is empty, the timer isn't running:

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

The fast path probes three well-known captive-portal detection URLs. If your
college blocks all three, the fast path falsely reports online. Start the
single run with `--skip-internet-check` to bypass it, or add another probe
URL to `CONNECTIVITY_PROBES` in `bin/college_wifi_common.py`.

### Two instances at once

The run takes a lock in the data dir. A second instance exits `0`
immediately. Locks older than 10 minutes are treated as stale and reclaimed,
so a crashed run can never wedge the tool.

## Contributing

See [`docs/PORTING.md`](docs/PORTING.md) for the layer-by-layer design and a
checklist for adding another OS or portal type.

Pull requests welcome. Please run the test suite and the doctor script after
any change.

## License

MIT — see [`LICENSE`](LICENSE).
