#!/usr/bin/env python3
"""
Shared support code for college-wifi-autologin.

This module is deliberately dependency-light: it only needs the standard
library.  ``platformdirs`` is used when available (it gives the "correct"
per-OS directory names), but if it is missing we fall back to the same
locations computed by hand so the installer can still bootstrap itself
before any package has been installed.

Everything here is importable and unit-testable without Playwright, a
browser, or a network connection.

Layout of the module:

    paths        - where config / data / logs / lock live on this OS
    env files    - robust parsing *and* generation of KEY='value' files
    Settings     - the fully-resolved, validated configuration object
    logging      - rotating file + stdout logger used by every entry point
    probes       - "do we have real internet?" / "is the portal up?"
    lock         - cross-platform advisory lock (atomic create, PID + age)
"""

from __future__ import annotations

import errno
import logging
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

__version__ = "1.1.0"

APP_NAME = "college-wifi-autologin"

# ---------------------------------------------------------------------------
# Defaults (all overridable from the env file or the environment)
# ---------------------------------------------------------------------------
DEFAULT_PORTAL_URL = "https://172.22.2.6/connect/PortalMain"
DEFAULT_STEP_TIMEOUT = 30          # seconds per wait step
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF = 5          # seconds between attempts
DEFAULT_POST_SUBMIT_TIMEOUT = 25   # seconds to wait for the success page
DEFAULT_RSA_TIMEOUT = 15           # seconds to wait for cpRSAobj
DEFAULT_CONNECTIVITY_TIMEOUT = 5.0

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 1
LOCK_MAX_AGE = 600                 # seconds before a stale lock is ignored

#: Sent by the login script and the probes.  Some captive portals (and some
#: transparent proxies) behave differently for the default Python UA.
USER_AGENT = (
    "Mozilla/5.0 ({platform}) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

#: (url, expected_status, expected_body_prefix) triples.  A captive portal
#: typically answers 200 with a redirect body, or 302, so an exact match on
#: status *and* body is what tells us the network is genuinely open.
CONNECTIVITY_PROBES: tuple[tuple[str, int, bytes], ...] = (
    ("http://connectivitycheck.gstatic.com/generate_204", 204, b""),
    ("http://detectportal.firefox.com/success.txt", 200, b"success"),
    # Cloudflare's endpoint is a useful third opinion on networks that block
    # both of the above.  It returns 204 with an empty body.
    ("http://cp.cloudflare.com/generate_204", 204, b""),
)

# ---------------------------------------------------------------------------
# Portal defaults — keep in one place so every tool agrees
# ---------------------------------------------------------------------------
USERNAME_SELECTORS: tuple[str, ...] = (
    "#LoginUserPassword_auth_username",
    'input[name="username"]',
    'input[name="user"]',
    'input[id*="username" i]',
    'input[autocomplete="username"]',
    'input[type="text"]:visible',
)

PASSWORD_SELECTORS: tuple[str, ...] = (
    "#LoginUserPassword_auth_password",
    'input[name="password"]',
    'input[id*="password" i]',
    'input[autocomplete="current-password"]',
    'input[type="password"]:visible',
)

SUBMIT_SELECTORS: tuple[str, ...] = (
    'input[type="submit"]:visible',
    'button[type="submit"]:visible',
    'a.button:has-text("Login")',
    'a.button:has-text("Log In")',
    'a.button:has-text("Sign In")',
    'button:has-text("Login")',
    'button:has-text("Log In")',
    'button:has-text("Sign In")',
    'input[value*="Login" i]:visible',
)

SUCCESS_MARKERS: tuple[str, ...] = (
    "network access granted",
    "welcome to the network",
    "access has been granted",
    "you are now connected",
    "login successful",
    "authentication succeeded",
)

FAILURE_MARKERS: tuple[str, ...] = (
    "invalid",
    "incorrect",
    "failed",
    "denied",
    "locked out",
    "too many",
    "authentication error",
)

#: Chromium flags that are safe and useful everywhere.  ``--no-sandbox`` is
#: deliberately *not* here: it weakens the browser sandbox, so we only add it
#: if a normal launch actually fails (containers, some CI images).
BASE_CHROMIUM_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
)

FALLBACK_CHROMIUM_ARGS: tuple[str, ...] = ("--no-sandbox",)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _fallback_config_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME


def _fallback_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _try_platformdirs(name: str) -> Optional[Path]:
    try:
        import platformdirs  # type: ignore
    except ImportError:
        return None
    try:
        fn = getattr(platformdirs, name)
        return Path(fn(APP_NAME))
    except Exception:  # pragma: no cover - defensive
        return None


def config_dir() -> Path:
    """Directory holding ``wifi-login.env``."""
    return _try_platformdirs("user_config_dir") or _fallback_config_dir()


def data_dir() -> Path:
    """Directory holding the venv, installed scripts, logs and screenshots."""
    return _try_platformdirs("user_data_dir") or _fallback_data_dir()


def config_dir_from_env() -> Path:
    """Config directory, honouring an explicit config-file override.

    ``COLLEGE_WIFI_CONFIG_FILE``/``WIFI_LOGIN_CONFIG_FILE`` point at a file,
    so the directory that contains it wins over the platform default.  The
    installer and uninstaller both use this so they always agree on where the
    credentials live.
    """
    override = (os.environ.get("COLLEGE_WIFI_CONFIG_FILE")
                or os.environ.get("WIFI_LOGIN_CONFIG_FILE"))
    if override:
        return Path(override).expanduser().parent
    return config_dir()


def default_env_file() -> Path:
    override = os.environ.get("COLLEGE_WIFI_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    if os.environ.get("WIFI_LOGIN_CONFIG_FILE"):
        return Path(os.environ["WIFI_LOGIN_CONFIG_FILE"]).expanduser()
    return config_dir() / "wifi-login.env"


def default_log_file() -> Path:
    return data_dir() / "logs" / "login.log"


def default_screenshot_dir() -> Path:
    return data_dir() / "screenshots"


def default_lock_file() -> Path:
    return data_dir() / "login.lock"


def venv_python(venv_dir: Path) -> Path:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


# ---------------------------------------------------------------------------
# Env file parsing / generation
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def unquote_value(value: str) -> str:
    """Undo shell-style quoting for a single value.

    Handles the forms that ``quote_value`` emits plus the common hand-written
    variants: plain values, ``'single'``, ``"double"`` and the ``'it'\\''s'``
    idiom for embedding a single quote inside single quotes.
    """
    value = value.strip()
    if not value:
        return ""

    if value[0] == "'":
        inner = value[1:-1] if len(value) >= 2 and value[-1] == "'" else value[1:]
        return inner.replace("'\\''", "'")

    if value[0] == '"':
        inner = value[1:-1] if len(value) >= 2 and value[-1] == '"' else value[1:]
        # Minimal double-quote unescaping; env files are not a shell.
        return inner.replace('\\"', '"').replace("\\\\", "\\")

    return value


def quote_value(value: str) -> str:
    """Wrap a value in single quotes, escaping embedded single quotes."""
    return "'" + value.replace("'", "'\\''") + "'"


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style ``KEY=value`` file.

    Never touches ``os.environ``.  Blank lines, comments, an optional
    ``export`` prefix, CRLF line endings and quoted values are all handled.
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            continue
        result[key] = unquote_value(value)
    return result


def render_env_file(values: Mapping[str, str], header: str = "") -> str:
    """Render a mapping as an env file body (order preserved)."""
    lines: list[str] = []
    if header:
        for line in header.splitlines():
            lines.append(f"# {line}".rstrip())
        lines.append("")
    for key, value in values.items():
        lines.append(f"export {key}={quote_value(str(value))}")
    lines.append("")
    return "\n".join(lines)


def write_private_file(path: Path, content: str) -> None:
    """Write a file, creating parents, and restrict it to the owner on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Coercion helpers (never raise on bad config)
# ---------------------------------------------------------------------------
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def coerce_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    token = str(value).strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    return default


def coerce_int(
    value: Optional[str],
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    number = default
    if value is not None:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            number = default
    if minimum is not None and number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def coerce_float(value: Optional[str], default: float,
                 minimum: Optional[float] = None) -> float:
    number = default
    if value is not None:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            number = default
    if minimum is not None and number < minimum:
        number = minimum
    return number


def split_selectors(raw: Optional[str], defaults: Sequence[str]) -> tuple[str, ...]:
    """Prepend user-supplied selectors (newline/comma separated) to defaults."""
    extra: list[str] = []
    if raw:
        for chunk in re.split(r"[\n,]+", raw):
            chunk = chunk.strip()
            if chunk:
                extra.append(chunk)
    merged: list[str] = []
    for sel in (*extra, *defaults):
        if sel not in merged:
            merged.append(sel)
    return tuple(merged)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    """Everything the login script needs, fully resolved and validated."""

    portal_url: str
    username: str
    password: str
    headless: bool
    step_timeout: int
    max_attempts: int
    retry_backoff: int
    post_submit_timeout: int
    rsa_timeout: int
    connectivity_timeout: float
    log_file: Path
    screenshot_dir: Path
    lock_file: Path
    env_file: Path
    skip_internet_check: bool = False
    block_resources: bool = True
    user_selectors: tuple[str, ...] = field(default_factory=lambda: USERNAME_SELECTORS)
    password_selectors: tuple[str, ...] = field(default_factory=lambda: PASSWORD_SELECTORS)
    submit_selectors: tuple[str, ...] = field(default_factory=lambda: SUBMIT_SELECTORS)
    success_markers: tuple[str, ...] = field(default_factory=lambda: SUCCESS_MARKERS)
    failure_markers: tuple[str, ...] = field(default_factory=lambda: FAILURE_MARKERS)

    @property
    def has_credentials(self) -> bool:
        return bool(self.username) and bool(self.password)


def load_settings(
    env_file: Optional[Path] = None,
    overrides: Optional[Mapping[str, object]] = None,
) -> Settings:
    """Resolve configuration from CLI overrides > environment > env file > defaults."""
    overrides = dict(overrides or {})
    env_file = Path(env_file).expanduser() if env_file else default_env_file()
    file_env = parse_env_file(env_file)

    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        for source in (os.environ, file_env):
            value = source.get(key)
            if value not in (None, ""):
                return value
        return default

    def override(name: str):
        value = overrides.get(name)
        return None if value is None else value

    def resolved_raw(override_name: str, env_key: str, default: Optional[str] = None):
        value = override(override_name)
        if value is None:
            return get(env_key, default)
        return value

    def pick_str(override_name: str, env_key: str, default: str) -> str:
        value = override(override_name)
        if value is None:
            value = get(env_key)
        if value in (None, ""):
            return default
        return str(value)

    def pick_path(override_name: str, env_key: str, default: Path) -> Path:
        value = override(override_name)
        if value is None:
            value = get(env_key)
        if value in (None, ""):
            return default
        return Path(str(value)).expanduser()

    headless_override = overrides.get("headless")
    headless = (coerce_bool(str(headless_override), True)
                if headless_override is not None
                else coerce_bool(get("WIFI_LOGIN_HEADLESS"), True))

    portal_url = pick_str("portal_url", "COLLEGE_WIFI_PORTAL_URL",
                          DEFAULT_PORTAL_URL).strip() or DEFAULT_PORTAL_URL

    return Settings(
        portal_url=portal_url,
        username=(pick_str("username", "COLLEGE_WIFI_USER", "") or "").strip(),
        password=pick_str("password", "COLLEGE_WIFI_PASS", "") or "",
        headless=headless,
        step_timeout=coerce_int(
            resolved_raw("step_timeout", "WIFI_LOGIN_TIMEOUT"), DEFAULT_STEP_TIMEOUT,
            minimum=1, maximum=600),
        max_attempts=coerce_int(
            resolved_raw("max_attempts", "WIFI_LOGIN_ATTEMPTS"), DEFAULT_MAX_ATTEMPTS,
            minimum=1, maximum=20),
        retry_backoff=coerce_int(
            resolved_raw("retry_backoff", "WIFI_LOGIN_BACKOFF"), DEFAULT_RETRY_BACKOFF,
            minimum=0, maximum=600),
        post_submit_timeout=coerce_int(
            resolved_raw("post_submit_timeout", "WIFI_LOGIN_POST_SUBMIT"),
            DEFAULT_POST_SUBMIT_TIMEOUT, minimum=1, maximum=600),
        rsa_timeout=coerce_int(
            resolved_raw("rsa_timeout", "WIFI_LOGIN_RSA_TIMEOUT"), DEFAULT_RSA_TIMEOUT,
            minimum=0, maximum=300),
        connectivity_timeout=coerce_float(
            resolved_raw("connectivity_timeout", "WIFI_LOGIN_CONNECTIVITY_TIMEOUT"),
            DEFAULT_CONNECTIVITY_TIMEOUT, minimum=0.5),
        log_file=pick_path("log_file", "WIFI_LOGIN_LOG_FILE", default_log_file()),
        screenshot_dir=pick_path("screenshot_dir", "WIFI_LOGIN_SCREENSHOT_DIR",
                                 default_screenshot_dir()),
        lock_file=pick_path("lock_file", "WIFI_LOGIN_LOCK_FILE", default_lock_file()),
        env_file=env_file,
        skip_internet_check=bool(overrides.get("skip_internet_check", False)),
        block_resources=(
            coerce_bool(str(overrides["block_resources"]), True)
            if overrides.get("block_resources") is not None
            else coerce_bool(get("WIFI_LOGIN_BLOCK_RESOURCES"), True)
        ),
        user_selectors=split_selectors(get("WIFI_LOGIN_USER_SELECTOR"), USERNAME_SELECTORS),
        password_selectors=split_selectors(get("WIFI_LOGIN_PASSWORD_SELECTOR"),
                                           PASSWORD_SELECTORS),
        submit_selectors=split_selectors(get("WIFI_LOGIN_SUBMIT_SELECTOR"),
                                         SUBMIT_SELECTORS),
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def rotate_log(log_file: Path, max_bytes: int = LOG_MAX_BYTES,
               backups: int = LOG_BACKUPS) -> None:
    """Size-based rotation: ``login.log`` -> ``login.log.1`` -> ..."""
    try:
        if not log_file.is_file() or log_file.stat().st_size <= max_bytes:
            return
        for index in range(backups - 1, 0, -1):
            src = log_file.with_name(f"{log_file.name}.{index}")
            dst = log_file.with_name(f"{log_file.name}.{index + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        first = log_file.with_name(f"{log_file.name}.1")
        if first.exists():
            first.unlink()
        log_file.rename(first)
    except OSError:
        pass


def setup_logging(log_file: Path, verbose: bool = False,
                  quiet: bool = False) -> logging.Logger:
    """Return the shared logger, writing to a rotating file and stdout."""
    rotate_log(log_file)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"warning: cannot create log directory {log_file.parent}: {exc}",
              file=sys.stderr)

    logger = logging.getLogger("college-wifi")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        print(f"warning: cannot write to log file {log_file}: {exc}", file=sys.stderr)

    if not quiet:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    if not logger.handlers:  # last resort so callers never crash on logging
        logger.addHandler(logging.NullHandler())

    return logger


# ---------------------------------------------------------------------------
# Network probes
# ---------------------------------------------------------------------------
def _request(url: str, method: str = "GET", timeout: float = 5.0):
    headers = {"User-Agent": USER_AGENT.format(platform=platform.system())}
    req = urllib.request.Request(url, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def have_internet(timeout: float = DEFAULT_CONNECTIVITY_TIMEOUT,
                  probes: Optional[Iterable[tuple[str, int, bytes]]] = None) -> bool:
    """True when the network looks genuinely open (not a captive portal)."""
    for url, expected_status, expected_body in (probes or CONNECTIVITY_PROBES):
        try:
            with _request(url, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if status != expected_status:
                    continue
                body = resp.read(max(len(expected_body), 1) + 8).strip()
                if expected_body:
                    if body == expected_body or body.startswith(expected_body):
                        return True
                elif not body:
                    return True
        except Exception:
            continue
    return False


def portal_reachable(url: str, timeout: float = 10.0,
                     allow_insecure: bool = True) -> bool:
    """Any HTTP response means the portal host is up (even an error code)."""
    import ssl

    context = None
    if allow_insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    for method in ("HEAD", "GET"):
        try:
            headers = {"User-Agent": USER_AGENT.format(platform=platform.system())}
            req = urllib.request.Request(url, method=method, headers=headers)
            urllib.request.urlopen(req, timeout=timeout, context=context)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Cross-platform advisory lock
# ---------------------------------------------------------------------------
def lock_is_stale(lock_file: Path, max_age: int = LOCK_MAX_AGE) -> bool:
    try:
        age = time.time() - lock_file.stat().st_mtime
    except OSError:
        return True
    return age >= max_age


def acquire_lock(lock_file: Path, max_age: int = LOCK_MAX_AGE,
                 pid: Optional[int] = None) -> bool:
    """Atomically create ``lock_file``.  Returns False if someone else holds it."""
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True  # cannot even create the dir; don't block the run

    if lock_file.exists():
        if not lock_is_stale(lock_file, max_age):
            return False
        try:
            lock_file.unlink()
        except OSError:
            return False

    payload = f"{pid if pid is not None else os.getpid()}\n{time.time():.0f}\n"
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOTSUP):
            return True  # best effort on filesystems without O_EXCL
        return True
    try:
        os.write(fd, payload.encode("ascii", "replace"))
    finally:
        os.close(fd)
    return True


def release_lock(lock_file: Path) -> None:
    try:
        lock_file.unlink()
    except OSError:
        pass


def read_lock_pid(lock_file: Path) -> Optional[int]:
    try:
        first = lock_file.read_text(encoding="ascii", errors="replace").splitlines()[0]
        return int(first.strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def is_windows() -> bool:
    return os.name == "nt"
