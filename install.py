#!/usr/bin/env python3
"""
Cross-platform installer for college-wifi-autologin.

Detects the OS and installs the appropriate trigger layer:
  - Linux:   systemd user timer (+ optional NetworkManager dispatcher via sudo)
  - macOS:   launchd LaunchAgent
  - Windows: Task Scheduler task

Usage:
    python install.py
    python install.py --no-venv          # use system Python (advanced)
    python install.py --install-dir DIR  # custom install location
    python install.py --unattended       # don't prompt; read creds from env
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Optional

try:
    from platformdirs import user_config_dir, user_data_dir
except ImportError:
    print("error: 'platformdirs' not installed. Run: pip install platformdirs",
          file=sys.stderr)
    sys.exit(2)


APP_NAME = "college-wifi-autologin"
REPO_ROOT = Path(__file__).resolve().parent

LINUX_SERVICE_TEMPLATE = REPO_ROOT / "platform" / "linux" / "wifi-watch.service.in"
LINUX_TIMER_TEMPLATE = REPO_ROOT / "platform" / "linux" / "wifi-watch.timer"
LINUX_NM_TEMPLATE = REPO_ROOT / "platform" / "linux" / "90-college-wifi-login.in"
MACOS_PLIST_TEMPLATE = REPO_ROOT / "platform" / "macos" / "com.college-wifi-autologin.watch.plist.in"
WINDOWS_TASK_TEMPLATE = REPO_ROOT / "platform" / "windows" / "task.xml.in"

SYSTEM = platform.system()  # "Linux" | "Darwin" | "Windows"


def say(msg: str) -> None:
    print(f"\033[1;34m==>\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m!!\033[0m  {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    print(f"\033[1;31mxx\033[0m  {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def check_python() -> None:
    if sys.version_info < (3, 9):
        die(f"Python 3.9+ required, found {sys.version.split()[0]}")
    say(f"python {sys.version.split()[0]}")


def check_repo_layout() -> None:
    for path in (
        REPO_ROOT / "bin" / "college-wifi-login.py",
        REPO_ROOT / "bin" / "college-wifi-doctor.py",
        REPO_ROOT / "requirements.txt",
        REPO_ROOT / "config" / "wifi-login.env.example",
    ):
        if not path.exists():
            die(f"repo is missing {path}")
    if SYSTEM == "Linux" and not (LINUX_SERVICE_TEMPLATE.is_file() and LINUX_TIMER_TEMPLATE.is_file()):
        die("repo is missing platform/linux templates")
    if SYSTEM == "Darwin" and not MACOS_PLIST_TEMPLATE.is_file():
        die("repo is missing platform/macos template")
    if SYSTEM == "Windows" and not WINDOWS_TASK_TEMPLATE.is_file():
        die("repo is missing platform/windows template")
    say("repo layout OK")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def resolve_dirs(custom_install: Optional[str]) -> tuple[Path, Path]:
    config_dir = Path(user_config_dir(APP_NAME))
    data_dir = Path(custom_install) if custom_install else Path(user_data_dir(APP_NAME))
    return config_dir, data_dir


# ---------------------------------------------------------------------------
# Venv
# ---------------------------------------------------------------------------
def venv_python(venv_dir: Path) -> Path:
    if SYSTEM == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_venv(venv_dir: Path) -> Path:
    if venv_dir.is_dir() and venv_python(venv_dir).is_file():
        say(f"venv already exists: {venv_dir}")
        return venv_python(venv_dir)

    say(f"creating venv at {venv_dir}")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        venv.create(venv_dir, with_pip=True, clear=False)
    except Exception as exc:
        die(
            f"failed to create venv: {exc}\n"
            "    on Debian/Ubuntu this usually means the 'python3-venv' package is missing:\n"
            "        sudo apt install python3-venv"
        )

    py = venv_python(venv_dir)
    if not py.is_file():
        die(f"venv python not found at {py}")
    return py


def install_packages(py: Path) -> None:
    say("upgrading pip")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   check=True)

    say("installing python packages from requirements.txt")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                    "-r", str(REPO_ROOT / "requirements.txt")],
                   check=True)

    say("installing playwright chromium (may take a minute)")
    subprocess.run([str(py), "-m", "playwright", "install", "chromium"],
                   check=True)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def write_credentials(config_dir: Path, unattended: bool) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    env_file = config_dir / "wifi-login.env"

    if env_file.is_file():
        say(f"credentials file exists, leaving untouched: {env_file}")
        return env_file

    if unattended:
        user = os.environ.get("COLLEGE_WIFI_USER", "")
        passwd = os.environ.get("COLLEGE_WIFI_PASS", "")
        if not user or not passwd:
            die("unattended mode requires COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS "
                "in the environment")
    else:
        say("entering credentials (input will not be echoed for password)")
        user = input("    College WiFi username: ").strip()
        import getpass
        passwd = getpass.getpass("    College WiFi password: ")
        if not user or not passwd:
            die("username and password are required")

    def sq(s: str) -> str:
        return s.replace("'", "'\\''")

    content = (
        "# Managed by college-wifi-autologin install.py\n"
        f"export COLLEGE_WIFI_USER='{sq(user)}'\n"
        f"export COLLEGE_WIFI_PASS='{sq(passwd)}'\n"
    )
    env_file.write_text(content, encoding="utf-8")

    if os.name == "posix":
        os.chmod(env_file, 0o600)

    say(f"wrote {env_file}")
    return env_file


# ---------------------------------------------------------------------------
# Copy scripts into install dir
# ---------------------------------------------------------------------------
def install_scripts(data_dir: Path) -> tuple[Path, Path]:
    bin_dir = data_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    login = bin_dir / "college-wifi-login.py"
    doctor = bin_dir / "college-wifi-doctor.py"

    shutil.copy2(REPO_ROOT / "bin" / "college-wifi-login.py", login)
    shutil.copy2(REPO_ROOT / "bin" / "college-wifi-doctor.py", doctor)

    if os.name == "posix":
        os.chmod(login, 0o755)
        os.chmod(doctor, 0o755)

    say(f"installed scripts to {bin_dir}")
    return login, doctor


# ---------------------------------------------------------------------------
# Linux trigger
# ---------------------------------------------------------------------------
def install_linux(py: Path, login: Path) -> None:
    say("installing systemd user timer")
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)

    service_src = LINUX_SERVICE_TEMPLATE.read_text()
    service = (service_src
               .replace("@PYTHON@", str(py))
               .replace("@LOGIN_SCRIPT@", str(login)))
    (systemd_dir / "wifi-watch.service").write_text(service)

    shutil.copy2(LINUX_TIMER_TEMPLATE, systemd_dir / "wifi-watch.timer")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "wifi-watch.timer"],
                   check=False)
    say("systemd timer enabled")

    # NetworkManager dispatcher (optional, needs sudo)
    nm_dir = Path("/etc/NetworkManager/dispatcher.d")
    if nm_dir.is_dir():
        if shutil.which("sudo") is None:
            warn("sudo not found; skipping NetworkManager dispatcher install")
            return
        say("installing NetworkManager dispatcher (requires sudo)")
        nm_src = LINUX_NM_TEMPLATE.read_text()
        nm_content = (nm_src
                      .replace("@USER@", os.getenv("USER", Path.home().name))
                      .replace("@PYTHON@", str(py))
                      .replace("@LOGIN_SCRIPT@", str(login)))
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".tmp") as f:
            f.write(nm_content)
            tmp = f.name
        try:
            target = str(nm_dir / "90-college-wifi-login")
            subprocess.run(["sudo", "install", "-m", "0700",
                            "-o", "root", "-g", "root", tmp, target],
                           check=True)
            say(f"installed {target}")
        except subprocess.CalledProcessError as exc:
            warn(f"NetworkManager dispatcher install failed: {exc}")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# macOS trigger
# ---------------------------------------------------------------------------
def install_macos(py: Path, login: Path, data_dir: Path) -> None:
    say("installing launchd LaunchAgent")
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    log_file = data_dir / "logs" / "login.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    plist_content = (MACOS_PLIST_TEMPLATE.read_text()
                     .replace("@PYTHON@", str(py))
                     .replace("@LOGIN_SCRIPT@", str(login))
                     .replace("@LOG_FILE@", str(log_file)))

    label = "com.college-wifi-autologin.watch"
    plist_path = agents_dir / f"{label}.plist"
    plist_path.write_text(plist_content)

    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False,
                   capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=False)
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)
    say(f"launchd agent loaded: {label}")


# ---------------------------------------------------------------------------
# Windows trigger
# ---------------------------------------------------------------------------
def install_windows(py: Path, login: Path) -> None:
    say("installing Task Scheduler task")
    task_content = (WINDOWS_TASK_TEMPLATE.read_text()
                    .replace("@PYTHON@", str(py))
                    .replace("@LOGIN_SCRIPT@", str(login)))

    task_name = "CollegeWiFiAutoLogin"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".xml",
                                     encoding="utf-16") as f:
        f.write(task_content)
        xml_path = f.name

    try:
        # Remove any existing task first (ignore failure).
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                       check=False, capture_output=True)
        subprocess.run(["schtasks", "/Create", "/TN", task_name,
                        "/XML", xml_path, "/F"], check=True)
        say(f"scheduled task created: {task_name}")
    except subprocess.CalledProcessError as exc:
        warn(f"failed to create scheduled task: {exc}")
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
def smoke_test(py: Path, login: Path) -> None:
    say("running dry-run smoke test")
    result = subprocess.run([str(py), str(login), "--dry-run"],
                            capture_output=False)
    if result.returncode == 0:
        say("smoke test OK")
    else:
        warn(f"smoke test returned {result.returncode}; "
             f"run the doctor script for details")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--install-dir", metavar="PATH",
                   help="Override the install directory (default: platform data dir).")
    p.add_argument("--unattended", action="store_true",
                   help="Don't prompt for credentials; read from env vars.")
    args = p.parse_args(argv)

    say(f"detected OS: {SYSTEM}")

    check_python()
    check_repo_layout()

    config_dir, data_dir = resolve_dirs(args.install_dir)
    say(f"config dir: {config_dir}")
    say(f"data dir:   {data_dir}")

    venv_dir = data_dir / "venv"
    py = create_venv(venv_dir)
    install_packages(py)

    write_credentials(config_dir, args.unattended)
    login, _doctor = install_scripts(data_dir)

    if SYSTEM == "Linux":
        install_linux(py, login)
    elif SYSTEM == "Darwin":
        install_macos(py, login, data_dir)
    elif SYSTEM == "Windows":
        install_windows(py, login)
    else:
        die(f"unsupported OS: {SYSTEM}")

    smoke_test(py, login)

    print()
    say("installation complete")
    say(f"log:     {data_dir / 'logs' / 'login.log'}")
    say(f"doctor:  {py} {data_dir / 'bin' / 'college-wifi-doctor.py'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())