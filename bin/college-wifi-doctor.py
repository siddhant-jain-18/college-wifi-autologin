#!/usr/bin/env python3
"""
Diagnostic tool for college-wifi-autologin.

Reports the state of the installation without touching the portal.
Run this first when something isn't working.

    college-wifi-doctor.py
    college-wifi-doctor.py --launch-test   # also try launching Chromium
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    from platformdirs import user_config_dir, user_data_dir
except ImportError:
    print("error: 'platformdirs' not installed", file=sys.stderr)
    sys.exit(2)

APP_NAME = "college-wifi-autologin"
PORTAL_URL_DEFAULT = "https://172.22.2.6/connect/PortalMain"


def _ok(msg: str) -> None:
    print(f"  \033[32mOK\033[0m    {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33mWARN\033[0m  {msg}")


def _fail(msg: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {msg}")


def _info(msg: str) -> None:
    print(f"        {msg}")


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        result[k.strip()] = v
    return result


def section(title: str) -> None:
    print(f"\n== {title} ==")


def check_platform() -> None:
    section("platform")
    _info(f"{platform.system()} {platform.release()} ({platform.machine()})")
    _info(f"python {sys.version.split()[0]}  ({sys.executable})")


def check_paths() -> tuple[Path, Path, Path]:
    section("paths")
    config_dir = Path(user_config_dir(APP_NAME))
    data_dir = Path(user_data_dir(APP_NAME))
    env_file = config_dir / "wifi-login.env"

    _info(f"config dir: {config_dir}")
    _info(f"data dir:   {data_dir}")

    for label, p in (
        ("env file", env_file),
        ("log file", data_dir / "logs" / "login.log"),
    ):
        if p.is_file():
            _ok(f"{label} exists: {p}")
        else:
            _warn(f"{label} missing: {p}")

    return config_dir, data_dir, env_file


def check_credentials(env_file: Path) -> dict[str, str]:
    section("credentials")
    env = parse_env_file(env_file)
    if not env_file.is_file():
        _fail(f"env file not found: {env_file}")
        return env

    if os.name == "posix":
        mode = oct(env_file.stat().st_mode & 0o777)
        if mode in ("0o600", "0o400"):
            _ok(f"permissions: {mode}")
        else:
            _warn(f"permissions are {mode} (recommend 0600)")

    user = env.get("COLLEGE_WIFI_USER", "")
    passwd = env.get("COLLEGE_WIFI_PASS", "")

    _ok(f"COLLEGE_WIFI_USER = {user!r}") if user else _fail("COLLEGE_WIFI_USER is unset")
    _ok(f"COLLEGE_WIFI_PASS set ({len(passwd)} chars)") if passwd else _fail("COLLEGE_WIFI_PASS is unset")
    return env


def check_internet() -> bool:
    section("internet")
    try:
        with urllib.request.urlopen(
            "http://connectivitycheck.gstatic.com/generate_204", timeout=5
        ) as resp:
            if resp.status == 204 and not resp.read(1):
                _ok("online")
                return True
    except Exception:
        pass

    try:
        with urllib.request.urlopen(
            "http://detectportal.firefox.com/success.txt", timeout=5
        ) as resp:
            if resp.status == 200 and resp.read(20).strip() == b"success":
                _ok("online")
                return True
    except Exception:
        pass

    _info("offline or behind a captive portal")
    return False


def check_portal(url: str) -> bool:
    section("portal")
    _info(f"url: {url}")
    try:
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=10)
        _ok("reachable")
        return True
    except urllib.error.HTTPError:
        _ok("reachable (HTTP error response, host is up)")
        return True
    except Exception as exc:
        _fail(f"not reachable: {exc}")
        return False


def check_playwright(launch_test: bool) -> None:
    section("playwright")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        _fail(f"import failed: {exc}")
        _info(f"install with: {sys.executable} -m pip install playwright")
        return
    _ok("playwright importable")

    if not launch_test:
        _info("(skipping browser launch; pass --launch-test to try)")
        return

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            b.close()
        _ok("chromium launches")
    except Exception as exc:
        _fail(f"chromium launch failed: {exc}")
        _info(f"fix: {sys.executable} -m playwright install chromium")


def check_triggers() -> None:
    section("triggers")
    system = platform.system()
    if system == "Linux":
        if shutil.which("systemctl"):
            out = subprocess.run(
                ["systemctl", "--user", "list-timers", "--no-pager"],
                capture_output=True, text=True,
            )
            if "wifi-watch.timer" in out.stdout:
                _ok("wifi-watch.timer registered")
            else:
                _warn("wifi-watch.timer not found")
        else:
            _warn("systemctl not found; systemd trigger unavailable")

        nm_dispatch = Path("/etc/NetworkManager/dispatcher.d/90-college-wifi-login")
        if nm_dispatch.is_file():
            _ok(f"NetworkManager dispatcher: {nm_dispatch}")
        else:
            _info("NetworkManager dispatcher not installed (optional)")

    elif system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.college-wifi-autologin.watch.plist"
        if plist.is_file():
            _ok(f"launchd plist: {plist}")
        else:
            _warn(f"launchd plist not found: {plist}")

    elif system == "Windows":
        try:
            out = subprocess.run(
                ["schtasks", "/Query", "/TN", "CollegeWiFiAutoLogin"],
                capture_output=True, text=True,
            )
            if out.returncode == 0:
                _ok("scheduled task 'CollegeWiFiAutoLogin' exists")
            else:
                _warn("scheduled task not found")
        except FileNotFoundError:
            _warn("schtasks not found")


def check_recent_log(data_dir: Path) -> None:
    section("recent log")
    log_file = data_dir / "logs" / "login.log"
    if not log_file.is_file():
        _info("no log yet")
        return
    lines = log_file.read_text(errors="replace").splitlines()
    for line in lines[-20:]:
        _info(line)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--launch-test", action="store_true",
                   help="Actually try to launch Chromium.")
    p.add_argument("--portal-url", default=None,
                   help="Override portal URL for the reachability check.")
    args = p.parse_args(argv)

    check_platform()
    _, data_dir, env_file = check_paths()
    env = check_credentials(env_file)
    check_internet()

    url = args.portal_url or env.get("COLLEGE_WIFI_PORTAL_URL", PORTAL_URL_DEFAULT)
    check_portal(url)

    check_playwright(args.launch_test)
    check_triggers()
    check_recent_log(data_dir)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())