#!/usr/bin/env python3
"""
Uninstall college-wifi-autologin.

By default, keeps your credentials file and state (log/screenshots). Pass
--purge to remove everything.

Usage:
    python uninstall.py
    python uninstall.py --purge
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from platformdirs import user_config_dir, user_data_dir
except ImportError:
    print("error: 'platformdirs' not installed", file=sys.stderr)
    sys.exit(2)

APP_NAME = "college-wifi-autologin"
SYSTEM = platform.system()


def say(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m!!\033[0m  {msg}", file=sys.stderr)


def remove_linux() -> None:
    say("removing systemd user timer")
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    subprocess.run(["systemctl", "--user", "disable", "--now", "wifi-watch.timer"],
                   check=False, capture_output=True)
    for name in ("wifi-watch.service", "wifi-watch.timer"):
        f = systemd_dir / name
        if f.exists():
            f.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    nm_dispatch = Path("/etc/NetworkManager/dispatcher.d/90-college-wifi-login")
    if nm_dispatch.is_file():
        say("removing NetworkManager dispatcher (requires sudo)")
        subprocess.run(["sudo", "rm", "-f", str(nm_dispatch)], check=False)


def remove_macos() -> None:
    say("removing launchd agent")
    label = "com.college-wifi-autologin.watch"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                   check=False, capture_output=True)
    if plist.exists():
        plist.unlink()


def remove_windows() -> None:
    say("removing scheduled task")
    subprocess.run(["schtasks", "/Delete", "/TN", "CollegeWiFiAutoLogin", "/F"],
                   check=False, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--purge", action="store_true",
                   help="Also remove credentials and state (logs, screenshots).")
    p.add_argument("--install-dir", metavar="PATH",
                   help="Override install dir if you used a custom one.")
    args = p.parse_args(argv)

    if SYSTEM == "Linux":
        remove_linux()
    elif SYSTEM == "Darwin":
        remove_macos()
    elif SYSTEM == "Windows":
        remove_windows()
    else:
        warn(f"unknown OS: {SYSTEM}; skipping trigger removal")

    data_dir = Path(args.install_dir) if args.install_dir else Path(user_data_dir(APP_NAME))
    config_dir = Path(user_config_dir(APP_NAME))

    if data_dir.exists():
        say(f"removing install dir: {data_dir}")
        shutil.rmtree(data_dir, ignore_errors=True)

    if args.purge:
        if config_dir.exists():
            say(f"removing config dir: {config_dir}")
            shutil.rmtree(config_dir, ignore_errors=True)
    else:
        env_file = config_dir / "wifi-login.env"
        if env_file.is_file():
            say(f"kept credentials file: {env_file}")
        say("(pass --purge to remove credentials too)")

    say("uninstalled")
    return 0


if __name__ == "__main__":
    sys.exit(main())