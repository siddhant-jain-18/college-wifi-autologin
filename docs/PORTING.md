# Porting notes

The project has three layers. Only one is OS-specific.

## Layer 1 — Login logic (portable)

`bin/college-wifi-login.py` runs standalone on Python 3.9+ with
`playwright` and `platformdirs`. No shell, no wrapper, no OS assumptions
beyond what `platformdirs` abstracts.

If you're adapting this to a non-Cisco-ISE portal, everything you need to
change is at the top of that file:

- `DEFAULT_PORTAL_URL`
- `USERNAME_SELECTORS`, `PASSWORD_SELECTORS`, `SUBMIT_SELECTORS`
- `SUCCESS_MARKERS`, `FAILURE_MARKERS`
- Possibly `CHROMIUM_ARGS` if the portal needs different TLS or sandbox
  flags

## Layer 2 — Trigger (OS-specific)

Each OS has a small file that runs the login script on a schedule. All are
committed already; here's the contract each fulfills:

| OS      | Trigger                              | File                                            |
|---------|--------------------------------------|-------------------------------------------------|
| Linux   | systemd user timer (5 min)           | `platform/linux/wifi-watch.timer`               |
| Linux   | NetworkManager dispatcher (on join)  | `platform/linux/90-college-wifi-login.in`       |
| macOS   | launchd LaunchAgent (5 min)          | `platform/macos/com.college-wifi-autologin.watch.plist.in` |
| Windows | Task Scheduler (5 min + on logon)    | `platform/windows/task.xml.in`                  |

Every trigger file has three `@PLACEHOLDER@` tokens that `install.py`
substitutes: `@PYTHON@`, `@LOGIN_SCRIPT@`, `@USER@` (Linux dispatcher only),
`@LOG_FILE@` (macOS only).

## Layer 3 — Installer (portable)

`install.py` detects the OS and:
1. Creates the venv under `platformdirs.user_data_dir(APP_NAME)`.
2. Installs `requirements.txt` into it.
3. Runs `python -m playwright install chromium`.
4. Prompts for and writes credentials to
   `platformdirs.user_config_dir(APP_NAME)/wifi-login.env`.
5. Copies `bin/*.py` into `<data_dir>/bin`.
6. Substitutes placeholders into the OS-specific trigger file and installs
   it.

Adding a new OS means adding a `check_*`/`install_*` pair in `install.py` and
the matching template under `platform/<os>/`.

## Adding an OS: checklist

1. Add the template file under `platform/<os>/`.
2. In `install.py`, add a branch under `main()` and a
   `install_<os>(py, login, ...)` function.
3. In `uninstall.py`, add a matching `remove_<os>()`.
4. In `college-wifi-doctor.py`, add a branch under `check_triggers()`.
5. Add a Requirements / Install / Verify section to `README.md`.
6. Test the full cycle: install → doctor → manual trigger → uninstall.

## Notes for a new portal type

If the portal isn't Cisco ISE:

- **No `cpRSAobj`?** Remove the `wait_for_function` call in `login_once`
  that waits for RSA readiness. The form-fill step will still work for
  plain-text submissions.
- **No `oAuthentication.submitActiveForm`?** The submit ladder already
  handles `<input type=submit>`, `<button>`, and `<a class=button>`. Add
  a custom selector to `SUBMIT_SELECTORS` if yours is different.
- **Multi-page login (username → next → password)?** Not supported yet.
  You'd need to split `login_once` into stages. Open an issue if you build
  this — it's a common portal pattern.
- **Captcha?** Out of scope. The tool can't solve captchas and shouldn't.

## Testing changes

Before opening a PR:

```bash
# 1. Syntax check
python3 -m py_compile bin/college-wifi-login.py bin/college-wifi-doctor.py install.py uninstall.py

# 2. Dry run
python3 bin/college-wifi-login.py --dry-run

# 3. Full cycle on a clean VM
python3 install.py --unattended
# verify the trigger fires
python3 uninstall.py --purge
```

If you added an OS, note in the PR which version you tested against and
paste the doctor output.