#!/usr/bin/env python3
"""
Robust college WiFi captive-portal auto-login.

Runs non-interactively from a systemd timer and a NetworkManager dispatcher.
Exits 0 on success (or if already authenticated), non-zero otherwise.

Environment:
    COLLEGE_WIFI_USER       (required)  username
    COLLEGE_WIFI_PASS       (required)  password
    WIFI_LOGIN_HEADLESS     (optional)  "0" for a visible browser; default "1"
    WIFI_LOGIN_TIMEOUT      (optional)  per-step timeout in seconds; default 30
    WIFI_LOGIN_SCREENSHOTS  (optional)  screenshot dir;
                                        default $XDG_STATE_HOME/college-wifi/screenshots
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
    Page,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORTAL_URL = "https://172.22.2.6/connect/PortalMain"

USERNAME = os.environ.get("COLLEGE_WIFI_USER", "")
PASSWORD = os.environ.get("COLLEGE_WIFI_PASS", "")
HEADLESS = os.environ.get("WIFI_LOGIN_HEADLESS", "1") != "0"
STEP_TIMEOUT = int(os.environ.get("WIFI_LOGIN_TIMEOUT", "30"))

_default_shot_dir = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "college-wifi" / "screenshots"
)
SCREENSHOT_DIR = Path(os.environ.get("WIFI_LOGIN_SCREENSHOTS", _default_shot_dir))

MAX_ATTEMPTS = 3
RETRY_BACKOFF = 5           # seconds between attempts
POST_SUBMIT_TIMEOUT = 25    # seconds to wait for the portal to accept login

# Success markers on the post-login page (kept lowercase).
SUCCESS_MARKERS = (
    "network access granted",
    "welcome to the network",
    "access has been granted",
)

# Failure markers on the login page.
FAILURE_MARKERS = (
    "invalid",
    "incorrect",
    "failed",
    "denied",
    "locked out",
    "too many",
)

# Selector fallbacks, tried in order. The first that matches a visible element
# wins, so a portal upgrade that renames IDs still works.
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("wifi-login")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _screenshot(page: Page, name: str) -> None:
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{int(time.time())}-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("screenshot: %s", path)
    except Exception as exc:
        log.debug("screenshot failed: %s", exc)


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1_000).lower()
    except Exception:
        return ""


def _find_first_visible(page: Page, selectors, timeout_ms: int = 0):
    """Return the first selector that matches a visible element, or None."""
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
        page.wait_for_timeout(200)


def is_logged_in(page: Page) -> bool:
    """True if the portal shows the post-login success page."""
    text = _body_text(page)
    if any(marker in text for marker in SUCCESS_MARKERS):
        return True

    # Fallback: no login form AND a logout control present.
    try:
        has_form = any(page.locator(sel).count() > 0 for sel in USERNAME_SELECTORS)
        has_logout = any(
            page.locator(sel).count() > 0
            for sel in (
                'text="Log Out"',
                'input[value*="Log Out" i]',
                'button:has-text("Log Out")',
                'a:has-text("Log Out")',
                'a:has-text("Logout")',
            )
        )
        return (not has_form) and has_logout
    except Exception:
        return False


def _submit(page: Page) -> bool:
    """Try the portal's own submit hook, then several DOM fallbacks."""
    # Preferred: the portal's own submit function (handles RSA encryption).
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

    # Fallback 1: click a submit control.
    for sel in SUBMIT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                log.info("submitted via click on %r", sel)
                return True
        except Exception as exc:
            log.debug("click on %r failed: %s", sel, exc)

    # Fallback 2: press Enter in the password field.
    pw_sel = _find_first_visible(page, PASSWORD_SELECTORS)
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
def login_once(page: Page) -> bool:
    log.info("navigating to portal")
    try:
        page.goto(PORTAL_URL, wait_until="domcontentloaded",
                  timeout=STEP_TIMEOUT * 1000)
    except PlaywrightTimeout:
        log.error("page load timed out")
        _screenshot(page, "load-timeout")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeout:
        log.debug("networkidle not reached; continuing")

    if is_logged_in(page):
        log.info("already authenticated")
        return True

    user_sel = _find_first_visible(page, USERNAME_SELECTORS,
                                   timeout_ms=STEP_TIMEOUT * 1000)
    if not user_sel:
        if is_logged_in(page):
            log.info("no form, but authenticated")
            return True
        log.error("login form did not appear")
        _screenshot(page, "no-form")
        return False

    pw_sel = _find_first_visible(page, PASSWORD_SELECTORS, timeout_ms=5_000)
    if not pw_sel:
        log.error("password field did not appear")
        _screenshot(page, "no-password")
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
        log.warning("RSA not ready; continuing")

    page.fill(user_sel, USERNAME)
    page.fill(pw_sel, PASSWORD)

    if not _submit(page):
        log.error("no submit control found")
        _screenshot(page, "no-submit")
        return False

    # Poll frequently so we don't burn 25 s on every successful login.
    deadline = time.monotonic() + POST_SUBMIT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if page.evaluate(
                "() => !!(window.cpRSAobj && cpRSAobj.isAuthenticated)"
            ):
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

        page.wait_for_timeout(500)

    text = _body_text(page)
    if any(marker in text for marker in FAILURE_MARKERS):
        log.error("portal reported a failure")
        _screenshot(page, "failed")
        return False

    log.warning("login result unclear")
    _screenshot(page, "unclear")
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_login() -> bool:
    if not USERNAME or not PASSWORD:
        log.error("COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS must be set")
        return False

    log.info("starting login (headless=%s timeout=%ss)", HEADLESS, STEP_TIMEOUT)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--ignore-certificate-errors",
                    "--disable-web-security",
                ],
            )
        except Exception as exc:
            log.error("browser launch failed: %s", exc)
            log.error("run: %s -m playwright install chromium", sys.executable)
            return False

        try:
            context = browser.new_context(
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            for attempt in range(1, MAX_ATTEMPTS + 1):
                log.info("--- attempt %d/%d ---", attempt, MAX_ATTEMPTS)
                try:
                    if login_once(page):
                        return True
                except Exception as exc:
                    log.exception("unexpected error: %s", exc)

                if attempt < MAX_ATTEMPTS:
                    log.info("retrying in %ds", RETRY_BACKOFF)
                    time.sleep(RETRY_BACKOFF)

            return False
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(0 if run_login() else 1)