#!/usr/bin/env python3
"""
Cross-platform installer for college-wifi-autologin.

Detects the OS and installs the appropriate trigger layer:
  - Linux:   systemd user timer (+ optional NetworkManager dispatcher via sudo)
  - macOS:   launchd LaunchAgent
  - Windows: Task Scheduler task

Usage:
    python install.py
    python install.py --unattended            # read credentials from env vars
    python install.py --install-dir DIR       # custom install location
    python install.py --reconfigure           # re-prompt for credentials
    python install.py --skip-playwright       # skip the Chromium download
    python install.py --no-trigger            # install files only, no scheduler
    python install.py --version

The installer is safe to re-run: an existing venv is reused and an existing
credentials file is left untouched unless you pass ``--reconfigure``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

from college_wifi_common import (  # noqa: E402
    APP_NAME,
    __version__,
    config_dir_from_env,
    data_dir as default_data_dir,
    render_env_file,
    venv_python,
    write_private_file,
)

LINUX_SERVICE_TEMPLATE = REPO_ROOT / "platform" / "linux" / "wifi-watch.service.in"
LINUX_TIMER_TEMPLATE = REPO_ROOT / "platform" / "linux" / "wifi-watch.timer"
LINUX_NM_TEMPLATE = REPO_ROOT / "platform" / "linux" / "90-college-wifi-login.in"
MACOS_PLIST_TEMPLATE = (REPO_ROOT / "platform" / "macos"
                        / "com.college-wifi-autologin.watch.plist.in")
WINDOWS_TASK_TEMPLATE = REPO_ROOT / "platform" / "windows" / "task.xml.in"

PYTHON_SOURCES = (
    "college-wifi-login.py",
    "college-wifi-doctor.py",
    "college_wifi_common.py",
)

#: Files from older installs that should be cleaned up on upgrade.
STALE_FILES = ("college-wifi-trigger.sh",)

SYSTEM = platform.system()  # "Linux" | "Darwin" | "Windows"

CREDENTIAL_HEADER = f"""wifi-login.env - managed by {APP_NAME} install.py
Edit the two credentials below, or delete this file and re-run install.py.
Use single quotes; to include a single quote write it as '\\''"""


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------
def say(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m!!\033[0m  {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    print(f"\033[1;31mxx\033[0m  {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: Sequence[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=check, **kwargs)


def is_interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def check_python() -> None:
    if sys.version_info < (3, 9):
        die(f"Python 3.9+ required, found {sys.version.split()[0]}")
    say(f"python {sys.version.split()[0]}")


def check_repo_layout() -> None:
    missing = [
        REPO_ROOT / "bin" / name for name in PYTHON_SOURCES
        if not (REPO_ROOT / "bin" / name).is_file()
    ]
    if not (REPO_ROOT / "requirements.txt").is_file():
        missing.append(REPO_ROOT / "requirements.txt")
    if not (REPO_ROOT / "config" / "wifi-login.env.example").is_file():
        missing.append(REPO_ROOT / "config" / "wifi-login.env.example")

    if SYSTEM == "Linux":
        for template in (LINUX_SERVICE_TEMPLATE, LINUX_TIMER_TEMPLATE, LINUX_NM_TEMPLATE):
            if not template.is_file():
                missing.append(template)
    elif SYSTEM == "Darwin":
        if not MACOS_PLIST_TEMPLATE.is_file():
            missing.append(MACOS_PLIST_TEMPLATE)
    elif SYSTEM == "Windows":
        if not WINDOWS_TASK_TEMPLATE.is_file():
            missing.append(WINDOWS_TASK_TEMPLATE)

    if missing:
        for path in missing:
            print(f"    missing: {path}", file=sys.stderr)
        die("repository is incomplete; did you download all files?")
    say("repo layout OK")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def resolve_dirs(custom_install: Optional[str]) -> tuple[Path, Path]:
    # config_dir_from_env() honours an explicit config-file override, which is
    # mainly useful for automated installs and tests.
    config = Path(config_dir_from_env())
    data = (Path(custom_install).expanduser() if custom_install
            else Path(default_data_dir()))
    return config, data


# ---------------------------------------------------------------------------
# Venv + packages
# ---------------------------------------------------------------------------
def create_venv(venv_dir: Path) -> Path:
    python = venv_python(venv_dir)
    if python.is_file():
        say(f"venv already exists: {venv_dir}")
        return python

    say(f"creating venv at {venv_dir}")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        venv.create(venv_dir, with_pip=True, clear=False)
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if SYSTEM == "Linux":
            hint = ("\n    on Debian/Ubuntu this usually means python3-venv is missing:\n"
                    "        sudo apt install python3-venv")
        die(f"failed to create venv: {exc}{hint}")

    if not venv_python(venv_dir).is_file():
        die(f"venv python not found at {python}")

    # Some minimal installs create the venv without pip.
    probe = run([str(python), "-m", "pip", "--version"], check=False,
                capture_output=True, text=True)
    if probe.returncode != 0:
        say("bootstrapping pip in the venv")
        run([str(python), "-m", "ensurepip", "--upgrade"], check=False)
    return python


def install_packages(python: Path, verbose: bool, skip_playwright: bool) -> None:
    quiet = [] if verbose else ["--quiet", "--disable-pip-version-check"]

    say("upgrading pip")
    run([str(python), "-m", "pip", "install", *quiet, "--upgrade", "pip"], check=False)

    say("installing python packages from requirements.txt")
    run([str(python), "-m", "pip", "install", *quiet,
         "-r", str(REPO_ROOT / "requirements.txt")], check=True)

    if skip_playwright:
        warn("skipping the Chromium download (--skip-playwright)")
        return

    say("installing playwright chromium (this can take a minute)")
    result = run([str(python), "-m", "playwright", "install", "chromium"],
                 check=False)
    if result.returncode != 0:
        warn("'playwright install chromium' failed; re-run it later with:")
        warn(f"    {python} -m playwright install chromium")


def verify_chromium(python: Path) -> bool:
    """Launch Chromium once so a broken install is caught now, not at 3 AM."""
    say("verifying that Chromium actually launches")
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch(headless=True,\n"
        "                          args=['--disable-dev-shm-usage', '--no-sandbox'])\n"
        "    b.close()\n"
    )
    result = run([str(python), "-c", probe], check=False,
                 capture_output=True, text=True)
    if result.returncode == 0:
        say("chromium OK")
        return True
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    warn(f"chromium launch test failed: {detail[-1] if detail else 'unknown error'}")
    warn(f"fix: {python} -m playwright install chromium")
    return False


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def write_credentials(config: Path, unattended: bool, reconfigure: bool) -> Path:
    config.mkdir(parents=True, exist_ok=True)
    env_file = config / "wifi-login.env"

    if env_file.is_file() and not reconfigure:
        say(f"credentials file exists, leaving untouched: {env_file}")
        return env_file

    if unattended:
        username = os.environ.get("COLLEGE_WIFI_USER", "").strip()
        password = os.environ.get("COLLEGE_WIFI_PASS", "")
        if not username or not password:
            die("unattended mode requires COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS "
                "in the environment")
    else:
        if not is_interactive():
            die("no terminal available; use --unattended with "
                "COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS")
        say("entering credentials (the password is not echoed)")
        username = input("    College WiFi username: ").strip()
        password = getpass.getpass("    College WiFi password: ")
        if not username or not password:
            die("username and password are required")

    if password != password.strip():
        warn("the password has leading/trailing whitespace; it was kept as typed")

    values = {"COLLEGE_WIFI_USER": username, "COLLEGE_WIFI_PASS": password}
    write_private_file(env_file, render_env_file(values, header=CREDENTIAL_HEADER))

    if os.name == "posix" and env_file.stat().st_mode & 0o777 != 0o600:
        warn(f"could not restrict permissions on {env_file}; run: chmod 600 {env_file}")

    say(f"wrote {env_file}")
    return env_file


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------
def install_scripts(data: Path) -> tuple[Path, Path]:
    bin_dir = data / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for name in PYTHON_SOURCES:
        shutil.copy2(REPO_ROOT / "bin" / name, bin_dir / name)

    for stale in STALE_FILES:
        stale_path = bin_dir / stale
        if stale_path.exists():
            stale_path.unlink()
            say(f"removed obsolete {stale_path}")

    login = bin_dir / "college-wifi-login.py"
    doctor = bin_dir / "college-wifi-doctor.py"
    if os.name == "posix":
        for path in (login, doctor):
            os.chmod(path, 0o755)

    say(f"installed scripts to {bin_dir}")
    return login, doctor


# ---------------------------------------------------------------------------
# Linux trigger
# ---------------------------------------------------------------------------
def _sudo_prefix(unattended: bool) -> Optional[list[str]]:
    sudo = shutil.which("sudo")
    if sudo is None:
        return None
    # In unattended mode never sit waiting for a password prompt.
    non_interactive = unattended or not is_interactive()
    return [sudo, "-n"] if non_interactive else [sudo]


def install_linux(python: Path, login: Path, home: Path, unattended: bool,
                  with_dispatcher: bool) -> None:
    say("installing systemd user timer")
    systemd_dir = home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    service = (LINUX_SERVICE_TEMPLATE.read_text(encoding="utf-8")
               .replace("@PYTHON@", str(python))
               .replace("@LOGIN_SCRIPT@", str(login)))
    (systemd_dir / "wifi-watch.service").write_text(service, encoding="utf-8")
    shutil.copy2(LINUX_TIMER_TEMPLATE, systemd_dir / "wifi-watch.timer")

    have_systemctl = shutil.which("systemctl") is not None
    if have_systemctl:
        run(["systemctl", "--user", "daemon-reload"], check=False)
        result = run(["systemctl", "--user", "enable", "--now", "wifi-watch.timer"],
                     check=False, capture_output=True, text=True)
        if result.returncode == 0:
            say("systemd user timer enabled (every 5 minutes)")
        else:
            warn("could not enable the systemd timer (no user session bus?)")
            warn(f"    enable it manually with: systemctl --user enable --now "
                 f"wifi-watch.timer")
    else:
        warn("systemctl not found; the timer was written but not enabled")
        warn("    install cron or run the login script from your own scheduler")

    if not with_dispatcher:
        return

    nm_dir = Path("/etc/NetworkManager/dispatcher.d")
    if not nm_dir.is_dir():
        say("NetworkManager dispatcher directory not present; skipping")
        return

    sudo = _sudo_prefix(unattended)
    if sudo is None:
        warn("sudo not found; skipping the NetworkManager dispatcher install")
        return

    say("installing NetworkManager dispatcher (requires root)")
    content = (LINUX_NM_TEMPLATE.read_text(encoding="utf-8")
               .replace("@USER@", os.environ.get("USER") or home.name)
               .replace("@HOME@", str(home))
               .replace("@PYTHON@", str(python))
               .replace("@LOGIN_SCRIPT@", str(login)))

    target = str(nm_dir / "90-college-wifi-login")
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tmp",
                                     encoding="utf-8") as handle:
        handle.write(content)
        tmp = handle.name

    os.chmod(tmp, 0o700)
    try:
        run([*sudo, "install", "-m", "0700", "-o", "root", "-g", "root",
             tmp, target], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        warn(f"NetworkManager dispatcher install failed: "
             f"{detail[-1] if detail else exc.returncode}")
        warn("it is optional; the 5-minute timer still works. To install it "
             "manually:")
        warn(f"    sudo install -m 0700 -o root -g root {tmp} {target}")
        return

    if os.path.exists(tmp):
        os.unlink(tmp)
    say(f"installed {target}")


# ---------------------------------------------------------------------------
# macOS trigger
# ---------------------------------------------------------------------------
def install_macos(python: Path, login: Path, data: Path, home: Path) -> None:
    say("installing launchd LaunchAgent")
    agents_dir = home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    log_file = data / "logs" / "login.log"
    launchd_log = data / "logs" / "launchd.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # The login script does its own logging + rotation; launchd only needs a
    # place to dump anything written to stdout/stderr before that is set up.
    plist_content = (MACOS_PLIST_TEMPLATE.read_text(encoding="utf-8")
                     .replace("@PYTHON@", str(python))
                     .replace("@LOGIN_SCRIPT@", str(login))
                     .replace("@LOG_FILE@", str(launchd_log)))

    label = "com.college-wifi-autologin.watch"
    plist_path = agents_dir / f"{label}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = str(os.getuid())
    run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False,
        capture_output=True)
    result = run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                 check=False, capture_output=True, text=True)
    if result.returncode == 0:
        run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)
        say(f"launchd agent loaded: {label}")
    else:
        detail = (result.stderr or "").strip()
        warn(f"could not load the launchd agent: {detail or result.returncode}")
        warn(f"    load it manually with: launchctl bootstrap gui/{uid} {plist_path}")


# ---------------------------------------------------------------------------
# Windows trigger
# ---------------------------------------------------------------------------
def install_windows(python: Path, login: Path) -> None:
    say("registering the Task Scheduler task")
    if shutil.which("schtasks") is None:
        warn("schtasks not found; skipping the scheduled task")
        return

    task_content = (WINDOWS_TASK_TEMPLATE.read_text(encoding="utf-8")
                    .replace("@PYTHON@", str(python))
                    .replace("@LOGIN_SCRIPT@", str(login)))

    # Prefer DOMAIN\user; schtasks accepts either but the qualified form is
    # more reliable on domain-joined machines.  If we cannot determine it,
    # drop the element entirely so the task defaults to the current user.
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    user_id = (domain + "\\" + user) if (domain and user) else user
    if user_id:
        task_content = task_content.replace("@USERID@", user_id)
    else:
        task_content = task_content.replace("      <UserId>@USERID@</UserId>\n", "")

    task_name = "CollegeWiFiAutoLogin"
    xml_path = ""
    created = False
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".xml",
                                         encoding="utf-16") as handle:
            handle.write(task_content)
            xml_path = handle.name

        run(["schtasks", "/Delete", "/TN", task_name, "/F"],
            check=False, capture_output=True)
        result = run(["schtasks", "/Create", "/TN", task_name,
                      "/XML", xml_path, "/F"],
                     check=False, capture_output=True, text=True)
        if result.returncode == 0:
            created = True
            say(f"scheduled task created: {task_name} (every 5 minutes, at logon)")
        else:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            warn(f"failed to create the scheduled task: "
                 f"{detail[-1] if detail else result.returncode}")
            warn(f"    you can create it manually from: {xml_path}")
    finally:
        if created and xml_path and os.path.exists(xml_path):
            os.unlink(xml_path)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
def smoke_test(python: Path, login: Path) -> None:
    say("running dry-run smoke test")
    result = run([str(python), str(login), "--dry-run"], check=False)
    if result.returncode == 0:
        say("smoke test OK")
    else:
        warn(f"smoke test returned {result.returncode}; "
             f"run the doctor script for details")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--install-dir", metavar="PATH",
                        help="Override the install directory "
                             "(default: platform data dir).")
    parser.add_argument("--unattended", action="store_true",
                        help="Don't prompt for credentials; read from env vars.")
    parser.add_argument("--reconfigure", action="store_true",
                        help="Re-prompt for credentials even if a file exists.")
    parser.add_argument("--skip-playwright", action="store_true",
                        help="Do not download Chromium (useful in CI).")
    parser.add_argument("--skip-deps", action="store_true",
                        help="Do not pip-install anything into the venv "
                             "(assumes the environment already has them).")
    parser.add_argument("--no-trigger", action="store_true",
                        help="Install files only; do not register any scheduler.")
    parser.add_argument("--no-dispatcher", action="store_true",
                        help="Linux only: skip the NetworkManager dispatcher.")
    parser.add_argument("--no-smoke-test", action="store_true",
                        help="Skip the --dry-run smoke test.")
    parser.add_argument("--verbose", action="store_true",
                        help="Show full pip/playwright output.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    home = Path.home()

    say(f"{APP_NAME} installer {__version__}")
    say(f"detected OS: {SYSTEM}")

    check_python()
    check_repo_layout()

    config, data = resolve_dirs(args.install_dir)
    say(f"config dir: {config}")
    say(f"data dir:   {data}")

    python = create_venv(data / "venv")
    if args.skip_deps:
        say("skipping python package install (--skip-deps)")
    else:
        install_packages(python, verbose=args.verbose,
                         skip_playwright=args.skip_playwright)
        if not args.skip_playwright:
            verify_chromium(python)

    write_credentials(config, unattended=args.unattended,
                      reconfigure=args.reconfigure)

    login, doctor = install_scripts(data)

    if args.no_trigger:
        say("skipping scheduler registration (--no-trigger)")
    elif SYSTEM == "Linux":
        install_linux(python, login, home, unattended=args.unattended,
                      with_dispatcher=not args.no_dispatcher)
    elif SYSTEM == "Darwin":
        install_macos(python, login, data, home)
    elif SYSTEM == "Windows":
        install_windows(python, login)
    else:
        die(f"unsupported OS: {SYSTEM}")

    if not args.no_smoke_test:
        smoke_test(python, login)

    print()
    say("installation complete")
    say(f"log:     {data / 'logs' / 'login.log'}")
    say(f"doctor:  {python} {doctor} --launch-test")
    say(f"uninstall: python {REPO_ROOT / 'uninstall.py'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        sys.exit(130)
