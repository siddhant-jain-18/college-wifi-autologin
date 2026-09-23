#!/usr/bin/env python3
"""
Diagnostic tool for college-wifi-autologin.

Reports the state of the installation without touching the portal (unless you
pass ``--launch-test``).  Run this first when something isn't working.

    college-wifi-doctor.py
    college-wifi-doctor.py --launch-test    # also try to launch Chromium
    college-wifi-doctor.py --json           # machine-readable output

Exit codes:
    0  no problems found (warnings are allowed)
    1  at least one check FAILed
    2  the doctor could not run at all
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from college_wifi_common import (  # noqa: E402
    __version__,
    config_dir,
    data_dir,
    default_env_file,
    default_lock_file,
    have_internet,
    load_settings,
    parse_env_file,
    portal_reachable,
    read_lock_pid,
    venv_python,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"


class Reporter:
    def __init__(self, as_json: bool = False) -> None:
        self.as_json = as_json
        self.results: list[dict[str, str]] = []
        self.section_name = ""

    # -- output ------------------------------------------------------------
    def section(self, title: str) -> None:
        self.section_name = title
        if not self.as_json:
            print(f"\n== {title} ==")

    def _record(self, status: str, message: str) -> None:
        self.results.append({"section": self.section_name,
                             "status": status,
                             "message": message})
        if self.as_json:
            return
        if status == INFO:
            print(f"        {message}")
            return
        colour = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}.get(status, "")
        print(f"  {colour}{status:<4}\033[0m  {message}")

    def ok(self, message: str) -> None:
        self._record(OK, message)

    def warn(self, message: str) -> None:
        self._record(WARN, message)

    def fail(self, message: str) -> None:
        self._record(FAIL, message)

    def info(self, message: str) -> None:
        self._record(INFO, message)

    # -- summary -----------------------------------------------------------
    @property
    def failures(self) -> list[dict[str, str]]:
        return [r for r in self.results if r["status"] == FAIL]

    @property
    def warnings(self) -> list[dict[str, str]]:
        return [r for r in self.results if r["status"] == WARN]

    def finish(self) -> int:
        if self.as_json:
            print(json.dumps(
                {
                    "version": __version__,
                    "platform": platform.platform(),
                    "results": self.results,
                    "failures": len(self.failures),
                    "warnings": len(self.warnings),
                },
                indent=2,
            ))
            return EXIT_FAIL if self.failures else EXIT_OK

        print()
        if self.failures:
            print(f"\033[31m{len(self.failures)} check(s) failed\033[0m"
                  f" - see the FAIL lines above.")
        elif self.warnings:
            print(f"\033[33mAll checks passed with {len(self.warnings)} warning(s).\033[0m")
        else:
            print("\033[32mAll checks passed.\033[0m")
        return EXIT_FAIL if self.failures else EXIT_OK


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_platform(report: Reporter) -> None:
    report.section("platform")
    report.info(f"{platform.system()} {platform.release()} ({platform.machine()})")
    report.info(f"python {sys.version.split()[0]}  ({sys.executable})")
    report.info(f"college-wifi-autologin {__version__}")

    if sys.version_info < (3, 9):
        report.fail("python 3.9 or newer is required")


def check_paths(report: Reporter) -> tuple[Path, Path, Path]:
    report.section("paths")
    cfg = config_dir()
    dat = data_dir()
    env_file = default_env_file()

    report.info(f"config dir: {cfg}")
    report.info(f"data dir:   {dat}")

    if env_file.is_file():
        report.ok(f"env file exists: {env_file}")
    else:
        report.warn(f"env file missing: {env_file}")

    for name in ("logs/login.log", "screenshots"):
        path = dat / name
        if path.exists():
            report.ok(f"{name}: {path}")
        else:
            report.info(f"{name}: not created yet ({path})")

    installed_login = dat / "bin" / "college-wifi-login.py"
    if installed_login.is_file():
        report.ok(f"installed login script: {installed_login}")
    else:
        report.warn(f"installed login script missing: {installed_login}")

    return cfg, dat, env_file


def check_credentials(report: Reporter, env_file: Path) -> dict[str, str]:
    report.section("credentials")
    env = parse_env_file(env_file)
    if not env_file.is_file():
        report.fail(f"env file not found: {env_file}")
        return env

    if os.name == "posix":
        mode = env_file.stat().st_mode & 0o777
        if mode in (0o600, 0o400):
            report.ok(f"permissions: {oct(mode)}")
        else:
            report.warn(f"permissions are {oct(mode)} (recommended: 0600)")

    username = env.get("COLLEGE_WIFI_USER", "")
    password = env.get("COLLEGE_WIFI_PASS", "")

    report.ok(f"COLLEGE_WIFI_USER = {username!r}") if username \
        else report.fail("COLLEGE_WIFI_USER is unset")
    report.ok(f"COLLEGE_WIFI_PASS set ({len(password)} chars)") if password \
        else report.fail("COLLEGE_WIFI_PASS is unset")

    if password:
        if password == password.strip():
            report.ok("password has no leading/trailing whitespace")
        else:
            report.warn("password has leading/trailing whitespace; "
                        "quote it carefully in the env file")

    return env


def check_internet(report: Reporter) -> bool:
    report.section("internet")
    if have_internet():
        report.ok("online (captive portal not intercepting)")
        return True
    report.info("offline or behind a captive portal")
    return False


def check_portal(report: Reporter, url: str) -> bool:
    report.section("portal")
    report.info(f"url: {url}")
    if portal_reachable(url):
        report.ok("reachable (or answered with an HTTP error, which still means "
                  "the host is up)")
        return True
    report.fail("not reachable")
    report.info("check the URL, the WiFi connection, and whether the portal "
                "is only reachable while on campus")
    return False


def check_playwright(report: Reporter, launch_test: bool) -> None:
    report.section("playwright")
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        report.fail(f"import failed: {exc}")
        report.info(f"install with: {sys.executable} -m pip install playwright")
        return
    report.ok("playwright importable")

    if not launch_test:
        report.info("(skipping browser launch; pass --launch-test to try)")
        return

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            browser.close()
        report.ok("chromium launches")
    except Exception as exc:  # noqa: BLE001
        report.fail(f"chromium launch failed: {exc}")
        report.info(f"fix: {sys.executable} -m playwright install chromium")


def check_venv(report: Reporter, dat: Path) -> None:
    report.section("venv")
    python = venv_python(dat / "venv")
    if python.is_file():
        report.ok(f"venv python: {python}")
    else:
        report.warn(f"venv python missing: {python}")
        report.info("re-run install.py to recreate it")

    if python.is_file():
        try:
            running_in_venv = Path(sys.executable).resolve() == python.resolve()
        except OSError:
            running_in_venv = False
        if running_in_venv:
            report.ok("doctor is running inside the venv")
        else:
            report.info("doctor is NOT running inside the venv; re-run it with "
                        f"{python}")


def check_lock(report: Reporter, lock_file: Optional[Path] = None) -> None:
    report.section("lock")
    lock = lock_file or default_lock_file()
    if not lock.exists():
        report.ok("no lock held (no run in progress)")
        return
    pid = read_lock_pid(lock)
    try:
        age = int(time.time() - lock.stat().st_mtime)
    except OSError:
        age = -1
    if pid is not None:
        report.warn(f"lock present: {lock} (pid={pid}, age={age}s)")
    else:
        report.warn(f"lock present: {lock} (age={age}s)")
    report.info("a lock older than 10 minutes is ignored automatically; "
                "delete it if you are sure no run is in progress")


def check_triggers(report: Reporter) -> None:
    report.section("triggers")
    system = platform.system()

    if system == "Linux":
        if shutil.which("systemctl"):
            result = subprocess.run(
                ["systemctl", "--user", "list-timers", "--all", "--no-pager"],
                capture_output=True, text=True,
            )
            if "wifi-watch.timer" in result.stdout:
                report.ok("wifi-watch.timer is registered")
            else:
                report.warn("wifi-watch.timer not found")
        else:
            report.warn("systemctl not found; the systemd trigger is unavailable")

        dispatcher = Path("/etc/NetworkManager/dispatcher.d/90-college-wifi-login")
        if dispatcher.is_file():
            report.ok(f"NetworkManager dispatcher: {dispatcher}")
        else:
            report.info("NetworkManager dispatcher not installed (optional)")

    elif system == "Darwin":
        plist = (Path.home() / "Library" / "LaunchAgents"
                 / "com.college-wifi-autologin.watch.plist")
        if plist.is_file():
            report.ok(f"launchd plist: {plist}")
        else:
            report.warn(f"launchd plist not found: {plist}")

        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        if "com.college-wifi-autologin.watch" in result.stdout:
            report.ok("launchd agent is loaded")
        else:
            report.info("launchd agent not currently loaded")

    elif system == "Windows":
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", "CollegeWiFiAutoLogin"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                report.ok("scheduled task 'CollegeWiFiAutoLogin' exists")
            else:
                report.warn("scheduled task not found")
        except FileNotFoundError:
            report.warn("schtasks not found")
    else:
        report.warn(f"unknown OS: {system}; no trigger expected")


def check_recent_log(report: Reporter, env_log: Optional[str] = None) -> None:
    report.section("recent log")
    log_file = Path(env_log) if env_log else data_dir() / "logs" / "login.log"
    if not log_file.is_file():
        report.info(f"no log yet: {log_file}")
        return
    try:
        lines = log_file.read_text(errors="replace").splitlines()
    except OSError as exc:
        report.warn(f"cannot read log: {exc}")
        return
    report.info(f"{log_file} ({len(lines)} lines)")
    for line in lines[-20:]:
        report.info(line)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose college-wifi-autologin.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--launch-test", action="store_true",
                        help="Actually try to launch Chromium.")
    parser.add_argument("--portal-url", default=None,
                        help="Override the portal URL for the reachability check.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of coloured text.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    report = Reporter(as_json=args.json)

    settings = load_settings()
    env = {}

    check_platform(report)
    _, dat, env_file = check_paths(report)
    env = check_credentials(report, env_file)
    check_internet(report)

    url = args.portal_url or env.get("COLLEGE_WIFI_PORTAL_URL") or settings.portal_url
    check_portal(report, url)

    check_playwright(report, args.launch_test)
    check_venv(report, dat)
    check_lock(report, settings.lock_file)
    check_triggers(report)
    check_recent_log(report, env.get("WIFI_LOGIN_LOG_FILE"))

    return report.finish()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)
