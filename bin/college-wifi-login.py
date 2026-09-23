#!/usr/bin/env python3
"""
Cross-platform college WiFi captive-portal auto-login.

Runs standalone on Linux, macOS, and Windows. Reads its own credentials file
(no shell wrapper needed), resolves paths via platformdirs, and exits 0 on
success or if already authenticated.

Usage:
    college-wifi-login.py                 # normal run
    college-wifi-login.py --dry-run       # report what it would do, don't touch the portal
    college-wifi-login.py --no-headless   # visible browser, for debugging
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --- Dependency checks ------------------------------------------------------
try:
    from platformdirs import user_config_dir, user_data_dir
except ImportError:
    print(
        "error: 'platformdirs' is not installed.\n"
        "       install with: python -m pip install platformdirs",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeout,
        Page,
    )
except ImportError:
    print(
        "error: 'playwright' is not installed.\n"
        "       install with: python -m pip install playwright\n"
        "       then:         python -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(2)

APP_NAME = "college-wifi-autologin"

# ---------------------------------------------------------------------------
# Defaults and constants
# ---------------------------------------------------------------------------
DEFAULT_PORTAL_URL = "https://172.22.2.6/connect/PortalMain"
DEFAULT_STEP_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF = 5
DEFAULT_POST_SUBMIT_TIMEOUT = 25
LOG_MAX_BYTES = 1024 * 1024
LOCK_MAX_AGE = 600  # seconds before a stale lock is ignored

SUCCESS_MARKERS = (
    "network access granted",
    "welcome to the network",
    "access has been granted",
)

FAILURE_MARKERS = (
    "invalid",
    "incorrect",
    "failed",
    "denied",
    "locked out",
    "too many",
)

USERNAME_SELECTORS = (
    "#LoginUserPassword_auth_username",
    'input[name="username"]',
    'input[name="user"]',
    'input[id*="username" i]',
    'input[type="text"]:visible',
)

PASSWORD_SELECTORS = (
    "#LoginUserPassword_auth_password",
    'input[name="password"]',
    'input[id*="password" i]',
    'input[type="password"]:visible',
)

SUBMIT_SELECTORS = (
    'input[type="submit"]:visible',
    'button[type="submit"]:visible',
    'a.button:has-text("Login")',
    'a.button:has-text("Log In")',
    'a.button:has-text("Sign In")',
    'button:has-text("Login")',
    'button:has-text("Log In")',
    'button:has-text("Sign In")',
)

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--ignore-certificate-errors",
    "--disable-web-security",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _default_env_file() -> Path:
    return Path(user_config_dir(APP_NAME)) / "wifi-login.env"


def _default_data_dir() -> Path:
    return Path(user_data_dir(APP_NAME))


@dataclass
class Config:
    portal_url: str
    username: str
    password: str
    headless: bool
    step_timeout: int
    max_attempts: int
    retry_backoff: int
    post_submit_timeout: int
    log_file: Path
    screenshot_dir: Path
    lock_file: Path
    skip_internet_check: bool = False


# ---------------------------------------------------------------------------
# Env file
# ---------------------------------------------------------------------------
def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style KEY=value file. Returns a dict, doesn't touch os.environ."""
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
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def build_config(args: argparse.Namespace) -> Config:
    env_file = Path(args.config) if args.config else _default_env_file()
    file_env = parse_env_file(env_file)

    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(key) or file_env.get(key) or default

    data_dir = _default_data_dir()

    log_file = Path(
        args.log_file
        or get("WIFI_LOGIN_LOG_FILE")
        or (data_dir / "logs" / "login.log")
    )
    screenshot_dir = Path(
        args.screenshot_dir
        or get("WIFI_LOGIN_SCREENSHOT_DIR")
        or (data_dir / "screenshots")
    )
    lock_file = Path(
        get("WIFI_LOGIN_LOCK_FILE") or (data_dir / "login.lock")
    )

    def get_bool(key: str, default: bool) -> bool:
        raw = get(key)
        if raw is None:
            return default
        return raw.strip().lower() not in ("0", "false", "no", "off")

    headless = args.headless if args.headless is not None else get_bool(
        "WIFI_LOGIN_HEADLESS", True
    )

    return Config(
        portal_url=get("COLLEGE_WIFI_PORTAL_URL", DEFAULT_PORTAL_URL) or DEFAULT_PORTAL_URL,
        username=get("COLLEGE_WIFI_USER", "") or "",
        password=get("COLLEGE_WIFI_PASS", "") or "",
        headless=headless,
        step_timeout=int(args.timeout or get("WIFI_LOGIN_TIMEOUT", DEFAULT_STEP_TIMEOUT)),
        max_attempts=int(get("WIFI_LOGIN_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        retry_backoff=int(get("WIFI_LOGIN_BACKOFF", DEFAULT_RETRY_BACKOFF)),
        post_submit_timeout=int(get("WIFI_LOGIN_POST_SUBMIT", DEFAULT_POST_SUBMIT_TIMEOUT)),
        log_file=log_file,
        screenshot_dir=screenshot_dir,
        lock_file=lock_file,
        skip_internet_check=bool(args.skip_internet_check),
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_file: Path, verbose: bool = False) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Rotate if > 1 MB
    try:
        if log_file.is_file() and log_file.stat().st_size > LOG_MAX_BYTES:
            backup = log_file.with_suffix(log_file.suffix + ".1")
            if backup.exists():
                backup.unlink()
            log_file.rename(backup)
    except OSError:
        pass

    logger = logging.getLogger("college-wifi")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        print(f"warning: cannot write to log file {log_file}: {exc}", file=sys.stderr)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def have_internet(timeout: float = 5.0) -> bool:
    """True if we appear to have real internet (not a captive portal)."""
    # gstatic returns 204 with empty body when the network is clear.
    try:
        with urllib.request.urlopen(
            "http://connectivitycheck.gstatic.com/generate_204", timeout=timeout
        ) as resp:
            if resp.status == 204 and not resp.read(1):
                return True
    except Exception:
        pass

    # Firefox's endpoint returns 200 with "success".
    try:
        with urllib.request.urlopen(
            "http://detectportal.firefox.com/success.txt", timeout=timeout
        ) as resp:
            if resp.status == 200 and resp.read(20).strip() == b"success":
                return True
    except Exception:
        pass

    return False


def portal_reachable(url: str, timeout: float = 10.0) -> bool:
    """Any HTTP response (even an error) means the portal host is up."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------
def acquire_lock(lock_file: Path, max_age: int = LOCK_MAX_AGE) -> bool:
    """Cross-platform advisory lock via atomic file creation."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            age = time.time() - lock_file.stat().st_mtime
            if age < max_age:
                return False
            lock_file.unlink()
        except OSError:
            return False

    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        # Filesystem doesn't support O_EXCL (rare); fall back to best-effort.
        return True


def release_lock(lock_file: Path) -> None:
    try:
        lock_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
def find_visible(page: Page, selectors, timeout_ms: int = 0) -> Optional[str]:
    """Return the first selector matching a visible element, or None."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return sel
            except Exception:
                continue
        if timeout_ms <= 0 or time.monotonic() >= deadline:
            return None
        try:
            page.wait_for_timeout(200)
        except Exception:
            return None


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1_000).lower()
    except Exception:
        return ""


def screenshot(page: Page, directory: Path, name: str, log: logging.Logger) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{int(time.time())}-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("screenshot saved: %s", path)
    except Exception as exc:
        log.debug("screenshot failed: %s", exc)


def is_logged_in(page: Page) -> bool:
    """True if the portal shows the post-login success page."""
    text = body_text(page)
    if any(m in text for m in SUCCESS_MARKERS):
        return True

    # Fallback: login form gone AND a logout control present.
    try:
        has_form = any(page.locator(sel).count() > 0 for sel in USERNAME_SELECTORS)
        has_logout = any(
            page.locator(sel).count() > 0
            for sel in (
                'text="Log Out"',
                'text="Logout"',
                'input[value*="Log Out" i]',
                'button:has-text("Log Out")',
                'button:has-text("Logout")',
                'a:has-text("Log Out")',
                'a:has-text("Logout")',
            )
        )
        return (not has_form) and has_logout
    except Exception:
        return False


def do_submit(page: Page, log: logging.Logger) -> bool:
    """Portal's own submit hook → CSS selectors → Enter key."""
    try:
        ready = page.evaluate(
            "() => typeof oAuthentication !== 'undefined' "
            "&& typeof oAuthentication.submitActiveForm === 'function'"
        )
        if ready:
            page.evaluate("() => oAuthentication.submitActiveForm()")
            log.info("submitted via oAuthentication.submitActiveForm()")
            return True
    except Exception as exc:
        log.debug("JS submit failed: %s", exc)

    for sel in SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                log.info("submitted via click on %r", sel)
                return True
        except Exception as exc:
            log.debug("click on %r failed: %s", sel, exc)

    pw_sel = find_visible(page, PASSWORD_SELECTORS)
    if pw_sel:
        try:
            page.locator(pw_sel).first.press("Enter")
            log.info("submitted via Enter key")
            return True
        except Exception as exc:
            log.debug("Enter-key submit failed: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------
def login_once(page: Page, cfg: Config, log: logging.Logger) -> bool:
    log.info("navigating to %s", cfg.portal_url)
    try:
        page.goto(cfg.portal_url, wait_until="domcontentloaded",
                  timeout=cfg.step_timeout * 1000)
    except PlaywrightTimeout:
        log.error("page load timed out")
        screenshot(page, cfg.screenshot_dir, "load-timeout", log)
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeout:
        log.debug("networkidle not reached; continuing")

    if is_logged_in(page):
        log.info("already authenticated")
        return True

    user_sel = find_visible(page, USERNAME_SELECTORS,
                            timeout_ms=cfg.step_timeout * 1000)
    if not user_sel:
        if is_logged_in(page):
            log.info("no form, but authenticated")
            return True
        log.error("login form did not appear")
        screenshot(page, cfg.screenshot_dir, "no-form", log)
        return False

    pw_sel = find_visible(page, PASSWORD_SELECTORS, timeout_ms=5_000)
    if not pw_sel:
        log.error("password field did not appear")
        screenshot(page, cfg.screenshot_dir, "no-password", log)
        return False

    log.info("form found (user=%r pass=%r)", user_sel, pw_sel)

    try:
        page.wait_for_function(
            "() => typeof cpRSAobj !== 'undefined' "
            "&& typeof cpRSAobj.isReadyToEncrypt === 'function' "
            "&& cpRSAobj.isReadyToEncrypt() === true",
            timeout=15_000,
        )
        log.info("RSA ready")
    except PlaywrightTimeout:
        log.warning("RSA not ready; continuing anyway")

    page.fill(user_sel, cfg.username)
    page.fill(pw_sel, cfg.password)

    if not do_submit(page, log):
        log.error("no submit control matched any known selector")
        screenshot(page, cfg.screenshot_dir, "no-submit", log)
        return False

    deadline = time.monotonic() + cfg.post_submit_timeout
    while time.monotonic() < deadline:
        try:
            if page.evaluate("() => !!(window.cpRSAobj && cpRSAobj.isAuthenticated)"):
                log.info("portal reports isAuthenticated = true")
                return True
        except Exception:
            pass

        try:
            url = page.url
            if "PortalMain" not in url and "/connect/" not in url:
                log.info("navigated away to %s", url)
                return True
        except Exception:
            pass

        if is_logged_in(page):
            log.info("success markers found")
            return True

        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    text = body_text(page)
    if any(m in text for m in FAILURE_MARKERS):
        log.error("portal reported a failure")
        screenshot(page, cfg.screenshot_dir, "failed", log)
        return False

    log.warning("login result unclear")
    screenshot(page, cfg.screenshot_dir, "unclear", log)
    return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_login(cfg: Config, log: logging.Logger) -> bool:
    if not cfg.username or not cfg.password:
        log.error("COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS must be set")
        log.error("(config file: %s)", _default_env_file())
        return False

    log.info("starting login (headless=%s timeout=%ss)", cfg.headless, cfg.step_timeout)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=cfg.headless, args=CHROMIUM_ARGS)
        except Exception as exc:
            log.error("browser launch failed: %s", exc)
            log.error("fix: %s -m playwright install chromium", sys.executable)
            return False

        try:
            context = browser.new_context(
                ignore_https_errors=True,
                user_agent=USER_AGENT,
            )
            page = context.new_page()

            for attempt in range(1, cfg.max_attempts + 1):
                log.info("--- attempt %d/%d ---", attempt, cfg.max_attempts)
                try:
                    if login_once(page, cfg, log):
                        return True
                except Exception as exc:
                    log.exception("unexpected error: %s", exc)

                if attempt < cfg.max_attempts:
                    log.info("retrying in %ds", cfg.retry_backoff)
                    time.sleep(cfg.retry_backoff)

            return False
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def dry_run(cfg: Config, log: logging.Logger) -> int:
    log.info("=== dry run ===")
    log.info("portal url:      %s", cfg.portal_url)
    log.info("username:        %s", cfg.username or "<unset>")
    log.info("password:        %s",
             f"<set, {len(cfg.password)} chars>" if cfg.password else "<unset>")
    log.info("headless:        %s", cfg.headless)
    log.info("step timeout:    %ss", cfg.step_timeout)
    log.info("log file:        %s", cfg.log_file)
    log.info("screenshot dir:  %s", cfg.screenshot_dir)
    log.info("lock file:       %s", cfg.lock_file)
    log.info("config file:     %s", _default_env_file())
    log.info("config exists:   %s", _default_env_file().is_file())

    online = have_internet()
    log.info("internet:        %s", "yes" if online else "no / captive portal")
    log.info("portal reachable: %s", "yes" if portal_reachable(cfg.portal_url) else "no")

    if not cfg.username or not cfg.password:
        log.error("dry run: credentials missing")
        return 1

    log.info("dry run complete (no changes made)")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="College WiFi captive-portal auto-login.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", metavar="PATH",
                   help="Path to the credentials env file (default: platform config dir).")
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None,
                   help="Run the browser headless (default: from config / True).")
    p.add_argument("--timeout", type=int,
                   help="Per-step timeout in seconds.")
    p.add_argument("--log-file", metavar="PATH",
                   help="Override the log file path.")
    p.add_argument("--screenshot-dir", metavar="PATH",
                   help="Override the screenshot directory.")
    p.add_argument("--verbose", action="store_true",
                   help="Verbose (debug) logging.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report config and connectivity, don't log in.")
    p.add_argument("--skip-internet-check", action="store_true",
                   help="Skip the fast-path internet check (always try to log in).")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    log = setup_logging(cfg.log_file, verbose=args.verbose)

    if args.dry_run:
        return dry_run(cfg, log)

    # Fast-path: nothing to do if we already have internet.
    if not cfg.skip_internet_check and have_internet():
        log.info("already online; nothing to do")
        return 0

    if not acquire_lock(cfg.lock_file):
        log.info("another instance is running; exiting")
        return 0

    try:
        return 0 if run_login(cfg, log) else 1
    finally:
        release_lock(cfg.lock_file)


if __name__ == "__main__":
    sys.exit(main())