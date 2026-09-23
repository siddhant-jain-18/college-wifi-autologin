#!/usr/bin/env python3
"""
Uninstall college-wifi-autologin.

By default this removes the trigger, the virtualenv and the installed
scripts, but **keeps** your credentials file and your logs/screenshots so you
can inspect them.  Pass ``--purge`` to remove absolutely everything.

Usage:
    python uninstall.py              # keep credentials + logs
    python uninstall.py --purge      # remove everything
    python uninstall.py --dry-run    # show what would be removed
    python uninstall.py --version
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))

from college_wifi_common import (  # noqa: E402
    __version__,
    config_dir_from_env,
    data_dir as default_data_dir,
    is_windows,
)

SYSTEM = platform.system()

#: Entries inside the data dir that are never user data.
REMOVABLE_IN_DATA = ("venv", "bin", "login.lock")


def say(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m!!\033[0m  {msg}", file=sys.stderr)


def _sudo() -> Optional[list[str]]:
    sudo = shutil.which("sudo")
    if sudo is None:
        return None
    interactive = sys.stdin is not None and sys.stdin.isatty()
    return [sudo] if interactive else [sudo, "-n"]


def remove_linux(dry_run: bool) -> None:
    say("removing systemd user timer")
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not dry_run and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "wifi-watch.timer"],
                       check=False, capture_output=True)
    for name in ("wifi-watch.service", "wifi-watch.timer"):
        path = systemd_dir / name
        if path.exists():
            say(f"  removing {path}")
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    warn(f"could not remove {path}: {exc}")
    if not dry_run and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    dispatcher = Path("/etc/NetworkManager/dispatcher.d/90-college-wifi-login")
    if dispatcher.is_file():
        sudo = _sudo()
        if sudo is None:
            warn(f"sudo not found; remove {dispatcher} manually")
        else:
            say("removing NetworkManager dispatcher (requires root)")
            if not dry_run:
                subprocess.run([*sudo, "rm", "-f", str(dispatcher)], check=False)


def remove_macos(dry_run: bool) -> None:
    say("removing launchd agent")
    label = "com.college-wifi-autologin.watch"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if shutil.which("launchctl"):
        if not dry_run:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                           check=False, capture_output=True)
    if plist.exists():
        say(f"  removing {plist}")
        if not dry_run:
            try:
                plist.unlink()
            except OSError as exc:
                warn(f"could not remove {plist}: {exc}")


def remove_windows(dry_run: bool) -> None:
    say("removing scheduled task")
    if shutil.which("schtasks") is None:
        warn("schtasks not found; skipping")
        return
    if not dry_run:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", "CollegeWiFiAutoLogin", "/F"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            warn("the scheduled task was not present (or could not be removed)")


def remove_path(path: Path, dry_run: bool) -> None:
    if not path.exists():
        return
    say(f"removing {path}")
    if dry_run:
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except OSError as exc:
        warn(f"could not remove {path}: {exc}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--purge", action="store_true",
                        help="Also remove credentials and logs/screenshots.")
    parser.add_argument("--install-dir", metavar="PATH",
                        help="Override the install dir if you used a custom one.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be removed without changing anything.")
    args = parser.parse_args(argv)

    data_dir = (Path(args.install_dir).expanduser() if args.install_dir
                else Path(default_data_dir()))
    config_dir = Path(config_dir_from_env())

    if args.purge:
        say("purging everything (credentials, logs, screenshots)")

    if SYSTEM == "Linux":
        remove_linux(args.dry_run)
    elif SYSTEM == "Darwin":
        remove_macos(args.dry_run)
    elif SYSTEM == "Windows":
        remove_windows(args.dry_run)
    else:
        warn(f"unknown OS: {SYSTEM}; skipping trigger removal")

    if args.purge:
        remove_path(data_dir, args.dry_run)
        remove_path(config_dir, args.dry_run)
    else:
        for name in REMOVABLE_IN_DATA:
            remove_path(data_dir / name, args.dry_run)
        # Anything else at the top level of the data dir (a custom venv name,
        # stray files) is left alone; only purge clears it out.
        if not args.dry_run and data_dir.exists():
            leftovers = sorted(p.name for p in data_dir.iterdir())
            if leftovers:
                say(f"kept in {data_dir}: {', '.join(leftovers)}")
        env_file = config_dir / "wifi-login.env"
        if env_file.is_file():
            say(f"kept credentials file: {env_file}")
        say("(pass --purge to remove credentials and logs too)")

    print()
    say("dry run complete (nothing was changed)" if args.dry_run else "uninstalled")
    if is_windows():
        say("(no admin rights were required)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
