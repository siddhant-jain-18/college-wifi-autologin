# Porting notes

The project has four layers. Only one and a half are OS-specific.

## Layer 1 — Shared support code (portable)

`bin/college_wifi_common.py` holds everything the entry points would
otherwise duplicate:

- per-OS paths (via `platformdirs`, with a hand-written fallback so the
  installer works on a bare Python),
- parsing **and** generating the `wifi-login.env` file (they are a matched
  pair — see `quote_value` / `unquote_value`),
- the `Settings` dataclass and `load_settings()`, which is where the
  CLI > environment > file > default precedence lives,
- rotating logging, connectivity probes and the cross-platform lock.

It imports nothing outside the standard library, and importing it must never
touch the network, the filesystem or Playwright. That constraint is what
makes the test suite fast and hermetic — please keep it.

## Layer 2 — Login logic (portable)

`bin/college-wifi-login.py` imports Playwright and drives the portal. If you
are adapting this to a non-Cisco-ISE portal, prefer changing configuration
over changing code:

| Want to change          | Do this instead of patching                       |
|-------------------------|---------------------------------------------------|
| Portal URL              | `COLLEGE_WIFI_PORTAL_URL`                          |
| Username/password field | `WIFI_LOGIN_USER_SELECTOR` / `..._PASSWORD_SELECTOR` |
| Submit control          | `WIFI_LOGIN_SUBMIT_SELECTOR`                       |

Genuinely code-shaped changes live in `bin/college_wifi_common.py`:

- `DEFAULT_PORTAL_URL`
- `USERNAME_SELECTORS`, `PASSWORD_SELECTORS`, `SUBMIT_SELECTORS`
- `SUCCESS_MARKERS`, `FAILURE_MARKERS`
- `CONNECTIVITY_PROBES` (add a probe if your campus blocks the defaults)
- `BASE_CHROMIUM_ARGS` / `FALLBACK_CHROMIUM_ARGS`

## Layer 3 — Trigger (OS-specific)

Each OS has a small template that runs the login script on a schedule:

| OS      | Trigger                             | File                                             |
|---------|-------------------------------------|--------------------------------------------------|
| Linux   | systemd user timer (5 min)          | `platform/linux/wifi-watch.timer`                |
| Linux   | systemd user service                | `platform/linux/wifi-watch.service.in`           |
| Linux   | NetworkManager dispatcher (on join) | `platform/linux/90-college-wifi-login.in`        |
| macOS   | launchd LaunchAgent (5 min + login) | `platform/macos/com.college-wifi-autologin.watch.plist.in` |
| Windows | Task Scheduler (5 min + logon + boot) | `platform/windows/task.xml.in`                 |

Every template uses `@PLACEHOLDER@` tokens that `install.py` substitutes:
`@PYTHON@`, `@LOGIN_SCRIPT@`, `@USER@` (Linux dispatcher), `@HOME@` (Linux
dispatcher), `@LOG_FILE@` (macOS launchd log) and `@USERID@` (Windows).
`tests/test_integration.py::TestRepoTemplates` asserts these placeholders are
present, so renaming one on only one side of the fence fails CI.

Two rules for trigger templates:

1. **Never run the login script as root.** The NetworkManager dispatcher runs
   as root and must `runuser`/`su` into the target user first.
2. **Never make the trigger block.** Detach long work (`&`, `IgnoreNew`,
   `AbandonProcessGroup`) so the network manager or login session is not
   held up by a browser launch.

## Layer 4 — Installer (portable)

`install.py` detects the OS and:

1. Creates the venv under `platformdirs.user_data_dir(APP_NAME)`.
2. Installs `requirements.txt` into it (skip with `--skip-deps`).
3. Runs `python -m playwright install chromium` and then *launches* Chromium
   once to prove it works.
4. Prompts for and writes credentials to the config dir (`--unattended` reads
   `COLLEGE_WIFI_USER` / `COLLEGE_WIFI_PASS` instead).
5. Copies every file in `PYTHON_SOURCES` into `<data>/bin` and deletes
   anything in `STALE_FILES`.
6. Substitutes the placeholders into the OS-specific trigger and installs it.
7. Runs `college-wifi-login.py --dry-run` as a smoke test.

`uninstall.py` mirrors it. Note the contract: the **default** uninstall keeps
credentials and logs; only `--purge` removes them.

## Adding an OS: checklist

1. Add the template under `platform/<os>/`.
2. In `install.py`: add a `check_repo_layout` branch, an `install_<os>()`, and
   a branch in `main()`; add the new `@PLACEHOLDER@`s to the substitution.
3. In `uninstall.py`: add a matching `remove_<os>()`.
4. In `bin/college-wifi-doctor.py`: add a branch under `check_triggers()`.
5. In `tests/test_integration.py`: add the template to
   `TestRepoTemplates.EXPECTED` (placeholders) and to `known` if you invented
   a new token.
6. Add the OS to `.github/workflows/ci.yml` if GitHub Actions supports it.
7. Add Requirements / Install / Verify sections to `README.md`.
8. Test the full cycle: install → doctor → manual trigger → uninstall.

## Notes for a new portal type

- **No `cpRSAobj`?** Set `WIFI_LOGIN_RSA_TIMEOUT='0'` to skip the RSA wait.
  The form fill works unchanged for plain-text submissions.
- **No `oAuthentication.submitActiveForm`?** The submit ladder already handles
  `<input type=submit>`, `<button>`, `<a class=button>` and the Enter key. Add
  a selector via `WIFI_LOGIN_SUBMIT_SELECTOR` if yours is different.
- **Multi-page login (username → next → password)?** Not supported yet. Split
  `login_once()` into stages. Open an issue if you build this — it is a common
  portal pattern.
- **Captcha?** Out of scope. The tool cannot solve captchas and should not.
- **Acceptable-use page first?** Also not supported. You would need to click
  "Agree" and then continue; `login_once()` is the place to add that.
- **Portal blocks image loads and renders anyway?** Set
  `WIFI_LOGIN_BLOCK_RESOURCES='0'`.

## Testing changes

Before opening a PR:

```bash
# 1. Syntax check
python3 -m py_compile install.py uninstall.py bin/*.py tests/*.py

# 2. Unit + integration tests (no network, no browser)
python3 -m unittest discover -s tests -t . -v

# 3. Dry run against your real config
python3 bin/college-wifi-login.py --dry-run

# 4. Full cycle on a clean VM
python3 install.py --unattended
# verify the trigger fires
python3 uninstall.py --purge
```

If you added an OS, note in the PR which version you tested against and paste
the doctor output.
