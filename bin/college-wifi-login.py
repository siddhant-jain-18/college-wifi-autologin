#!/usr/bin/env python3
"""
Cross-platform college WiFi captive-portal auto-login.

Runs standalone on Linux, macOS and Windows.  It reads its own credentials
file (no shell wrapper needed), resolves paths via
:mod:`college_wifi_common`, and exits 0 on success or when already
authenticated.

Usage:
    college-wifi-login.py                 # normal run
    college-wifi-login.py --dry-run       # report config/connectivity, don't touch the portal
    college-wifi-login.py --no-headless   # visible browser, for debugging
    college-wifi-login.py --version

Exit codes:
    0  logged in, already online, or another instance is already running
    1  login attempted but failed (bad credentials, portal down, ...)
    2  configuration problem (missing credentials, unreadable config)
    3  environment problem (playwright/browser missing or broken)
    4  portal not reachable and no internet
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional

# --- Make the sibling shared module importable in every launch style --------
sys.path.insert(0, str(Path(__file__).resolve().parent))

from college_wifi_common import (  # noqa: E402
    BASE_CHROMIUM_ARGS,
    FALLBACK_CHROMIUM_ARGS,
    Settings,
    USER_AGENT,
    __version__,
    acquire_lock,
    have_internet,
    load_settings,
    portal_reachable,
    release_lock,
    setup_logging,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_DEPENDENCY = 3
EXIT_NO_PORTAL = 4

LOG = logging.getLogger("college-wifi")

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:
    print(
        "error: 'playwright' is not installed.\n"
        "       install with: python -m pip install playwright\n"
        "       then:         python -m playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(EXIT_DEPENDENCY)


BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}

#: Logout controls that only exist on the post-login page.  Kept in one place
#: so is_logged_in() and _await_result() agree.
LOGOUT_SELECTORS: tuple[str, ...] = (
    'text="Log Out"',
    'text="Logout"',
    'input[value*="Log Out" i]',
    'button:has-text("Log Out")',
    'button:has-text("Logout")',
    'a:has-text("Log Out")',
    'a:has-text("Logout")',
)


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
def find_visible(page: Page, selectors, timeout_ms: int = 0) -> Optional[str]:
    """Return the first selector that matches a visible element, or None."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return selector
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
        return page.locator("body").inner_text(timeout=2_000).lower()
    except Exception:
        return ""


def text_is_success(text: str, markers) -> bool:
    return any(marker in text for marker in markers)


def text_is_failure(text: str, markers) -> bool:
    return any(marker in text for marker in markers)


def is_logged_in(page: Page, settings: Settings) -> bool:
    """True when the portal shows the post-login success page."""
    text = body_text(page)
    if text_is_success(text, settings.success_markers):
        return True

    # Fallback: the login form is gone AND a logout control is present.
    try:
        has_form = any(page.locator(sel).count() > 0
                       for sel in settings.user_selectors)
        has_logout = any(
            page.locator(sel).count() > 0
            for sel in LOGOUT_SELECTORS
        )
        return (not has_form) and has_logout
    except Exception:
        return False


def describe_page(page: Page) -> str:
    """One-line summary of the portal state, for logs and debugging.

    Never raises: every probe is guarded because this runs on failure paths
    where the page may already be half-dead.
    """
    bits: list[str] = []
    try:
        bits.append(f"url={page.url!r}")
    except Exception:
        pass
    try:
        bits.append(f"title={page.title()!r}")
    except Exception:
        pass
    for name, script in (
        ("oAuth", "typeof oAuthentication !== 'undefined'"),
        ("submitFn", "typeof oAuthentication !== 'undefined' "
                     "&& typeof oAuthentication.submitActiveForm === 'function'"),
        ("rsaReady", "!!(window.cpRSAobj && cpRSAobj.isReadyToEncrypt "
                     "&& cpRSAobj.isReadyToEncrypt())"),
        ("rsaAuth", "!!(window.cpRSAobj && cpRSAobj.isAuthenticated)"),
    ):
        try:
            bits.append(f"{name}={bool(page.evaluate(f'() => {script}'))}")
        except Exception:
            bits.append(f"{name}=?")
    for name, sels in (("user", ("input[type='text'], input:not([type])")),
                       ("pass", ("input[type='password']",)),
                       ("btn", ("button, input[type='submit'], input[type='button']",))):
        try:
            counts = [page.locator(sel).count() for sel in sels]
            bits.append(f"{name}={counts}")
        except Exception:
            bits.append(f"{name}=?")
    return " ".join(bits)


def screenshot(page: Page, settings: Settings, name: str) -> None:
    try:
        settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = settings.screenshot_dir / f"{int(time.time() * 1000)}-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        LOG.info("screenshot saved: %s", path)
    except Exception as exc:
        LOG.debug("screenshot failed: %s", exc)


def fill_field(page: Page, selector: str, value: str) -> bool:
    """Fill a field and make sure the value actually landed.

    ``fill`` is instant but only fires an ``input`` event; some portals only
    enable their submit button on real keystrokes.  If the readback doesn't
    match we type the value out instead.  Either way we finish by dispatching
    ``input``/``change`` events so JS-gated submit buttons enable.
    """
    locator = page.locator(selector).first
    try:
        locator.click(timeout=5_000)
    except Exception:
        pass

    try:
        locator.fill(value, timeout=10_000)
    except Exception as exc:
        LOG.debug("fill(%r) failed: %s", selector, exc)

    try:
        if locator.input_value(timeout=2_000) != value:
            LOG.debug("fill(%r) did not stick; typing instead", selector)
            try:
                locator.fill("")
                locator.press_sequentially(value, delay=15, timeout=20_000)
            except Exception as exc:
                LOG.debug("typing into %r failed: %s", selector, exc)
                return False
            try:
                if locator.input_value(timeout=2_000) != value:
                    return False
            except Exception:
                pass  # not an <input> we can read back; assume it worked
    except Exception:
        pass  # not an <input> we can read back; assume fill worked

    try:
        locator.dispatch_event("input")
        locator.dispatch_event("change")
    except Exception as exc:
        LOG.debug("dispatch events on %r failed (non-fatal): %s", selector, exc)
    return True


def do_submit(page: Page, settings: Settings) -> bool:
    """Portal's own submit hook -> container-aware JS click -> CSS -> form -> Enter."""
    try:
        ready = page.evaluate(
            "() => typeof oAuthentication !== 'undefined' "
            "&& typeof oAuthentication.submitActiveForm === 'function'"
        )
        if ready:
            page.evaluate("() => oAuthentication.submitActiveForm()")
            LOG.info("submitted via oAuthentication.submitActiveForm()")
            return True
        else:
            LOG.debug("oAuthentication.submitActiveForm() not present; "
                      "trying click fallbacks")
    except Exception as exc:
        LOG.debug("JS submit failed: %s", exc)

    # The AJAX view renders its OK button inside #usercheck_ok_div /
    # #LoginSequencePage_Content, which may not match the generic selectors
    # below on the first pass — try a direct JS click there first.
    try:
        clicked = page.evaluate(
            """() => {
                for (const root of ['#usercheck_ok_div', '#LoginSequencePage_Content',
                                    '#portal_main_view', 'form']) {
                    const scope = root === 'form' ? document
                        : document.querySelector(root);
                    if (!scope) continue;
                    const els = scope === document ? Array.from(document.forms)
                        .flatMap(f => Array.from(f.elements)) : null;
                    const candidates = els || scope.querySelectorAll(
                        'button, input[type="submit"], input[type="button"], a');
                    for (const el of candidates) {
                        if (el.disabled) continue;
                        const r = el.getBoundingClientRect
                            ? el.getBoundingClientRect() : null;
                        if (r && (r.width === 0 || r.height === 0)) continue;
                        el.click();
                        return root;
                    }
                }
                return null;
            }"""
        )
        if clicked:
            LOG.info("submitted via JS click inside %r", clicked)
            return True
    except Exception as exc:
        LOG.debug("JS container click failed: %s", exc)

    for selector in settings.submit_selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=5_000)
                LOG.info("submitted via click on %r", selector)
                return True
        except Exception as exc:
            LOG.debug("click on %r failed: %s", selector, exc)

    # Ask the form itself to submit (works even when the button is a div/span).
    try:
        submitted = page.evaluate(
            """() => {
                const form = document.querySelector(
                    '#LoginSequencePage_Content form, #portal_main_view form, form');
                if (!form) return false;
                if (typeof form.requestSubmit === 'function') form.requestSubmit();
                else form.submit();
                return true;
            }"""
        )
        if submitted:
            LOG.info("submitted via form.requestSubmit()/submit()")
            return True
    except Exception as exc:
        LOG.debug("form submit failed: %s", exc)

    password_selector = find_visible(page, settings.password_selectors)
    if password_selector:
        try:
            page.locator(password_selector).first.press("Enter")
            LOG.info("submitted via Enter key")
            return True
        except Exception as exc:
            LOG.debug("Enter-key submit failed: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------
def launch_chromium(playwright, headless: bool):
    """Launch Chromium, retrying with ``--no-sandbox`` only if needed."""
    base = list(BASE_CHROMIUM_ARGS)
    attempts = [base]
    if os.environ.get("WIFI_LOGIN_NO_SANDBOX") in ("1", "true", "yes"):
        attempts = [base + list(FALLBACK_CHROMIUM_ARGS)]
    else:
        attempts.append(base + list(FALLBACK_CHROMIUM_ARGS))

    last_error: Optional[Exception] = None
    for index, args in enumerate(attempts):
        try:
            browser = playwright.chromium.launch(headless=headless, args=args)
            if index > 0:
                LOG.warning("chromium needed --no-sandbox on this system")
            return browser
        except Exception as exc:  # noqa: BLE001 - report whichever error is last
            last_error = exc
            LOG.debug("chromium launch failed with %s: %s", args, exc)

    raise PlaywrightError(f"chromium launch failed: {last_error}")


def new_page(browser, settings: Settings) -> Page:
    context = browser.new_context(
        ignore_https_errors=True,
        user_agent=USER_AGENT.format(platform=platform.system()),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        java_script_enabled=True,
    )
    if settings.block_resources:
        def _route(route):
            try:
                if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        try:
            context.route("**/*", _route)
        except Exception as exc:
            LOG.debug("resource blocking unavailable: %s", exc)

    page = context.new_page()
    page.set_default_timeout(settings.step_timeout * 1000)
    return page


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------
def login_once(page: Page, settings: Settings) -> tuple[bool, str]:
    LOG.info("navigating to %s", settings.portal_url)
    try:
        page.goto(
            settings.portal_url,
            wait_until="domcontentloaded",
            timeout=settings.step_timeout * 1000,
        )
    except PlaywrightTimeout:
        screenshot(page, settings, "load-timeout")
        return False, "page load timed out"
    except PlaywrightError as exc:
        screenshot(page, settings, "load-error")
        return False, f"navigation error: {exc}"

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeout:
        LOG.debug("networkidle not reached; continuing "
                  "(expected: the portal keeps polling via AJAX)")

    # The PortalMain shell renders its Authentication view via AJAX
    # (viewManager.gotoNextState -> GetViewData), so give the portal's own
    # JS object a moment to appear before looking for fields.  This is only
    # a hint — the field poll below is what actually gates progress.
    try:
        page.wait_for_function(
            "() => typeof oAuthentication !== 'undefined'",
            timeout=10_000,
        )
        LOG.debug("oAuthentication object present")
    except PlaywrightTimeout:
        LOG.debug("oAuthentication not seen after 10s; continuing anyway")
    except PlaywrightError as exc:
        LOG.debug("oAuthentication probe failed: %s", exc)

    if is_logged_in(page, settings):
        return True, "already authenticated"

    user_selector = find_visible(page, settings.user_selectors,
                                 timeout_ms=settings.step_timeout * 1000)
    if not user_selector:
        if is_logged_in(page, settings):
            return True, "no form, but authenticated"
        screenshot(page, settings, "no-form")
        LOG.warning("portal state at no-form: %s", describe_page(page))
        return False, "login form did not appear"

    password_selector = find_visible(page, settings.password_selectors, timeout_ms=5_000)
    if not password_selector:
        screenshot(page, settings, "no-password")
        LOG.warning("portal state at no-password: %s", describe_page(page))
        return False, "password field did not appear"

    LOG.info("form found (user=%r pass=%r)", user_selector, password_selector)
    LOG.debug("portal state at form: %s", describe_page(page))

    if settings.rsa_timeout > 0:
        try:
            page.wait_for_function(
                "() => typeof cpRSAobj !== 'undefined' "
                "&& typeof cpRSAobj.isReadyToEncrypt === 'function' "
                "&& cpRSAobj.isReadyToEncrypt() === true",
                timeout=settings.rsa_timeout * 1000,
            )
            LOG.info("portal RSA encryption ready")
        except PlaywrightTimeout:
            LOG.warning("portal RSA not ready after %ss; continuing anyway",
                        settings.rsa_timeout)

    if not fill_field(page, user_selector, settings.username):
        LOG.warning("could not confirm the username field was filled")
    if not fill_field(page, password_selector, settings.password):
        LOG.warning("could not confirm the password field was filled")

    if not do_submit(page, settings):
        screenshot(page, settings, "no-submit")
        return False, "no submit control matched any known selector"

    return _await_result(page, settings)


def _await_result(page: Page, settings: Settings) -> tuple[bool, str]:
    deadline = time.monotonic() + settings.post_submit_timeout
    while time.monotonic() < deadline:
        try:
            if page.evaluate("() => !!(window.cpRSAobj && cpRSAobj.isAuthenticated)"):
                return True, "portal reports isAuthenticated = true"
        except Exception:
            pass

        try:
            url = page.url
            if "PortalMain" not in url and "/connect/" not in url:
                return True, f"navigated away to {url}"
        except Exception:
            pass

        if is_logged_in(page, settings):
            return True, "success markers found"

        # Fail fast: the portal answered with an error instead of logging in.
        # (Previously we waited out the whole post-submit timeout first.)
        text = body_text(page)
        if text and text_is_failure(text, settings.failure_markers):
            screenshot(page, settings, "failed")
            return False, "portal reported a failure (check credentials / account lock)"

        try:
            page.wait_for_timeout(500)
        except PlaywrightError:
            break
        except Exception:
            break

    # One last look: the AJAX view may have swapped after our final poll.
    if is_logged_in(page, settings):
        return True, "success markers found"

    try:
        if page.evaluate("() => !!(window.cpRSAobj && cpRSAobj.isAuthenticated)"):
            return True, "portal reports isAuthenticated = true"
    except Exception:
        pass

    text = body_text(page)
    if text_is_failure(text, settings.failure_markers):
        screenshot(page, settings, "failed")
        return False, "portal reported a failure (check credentials / account lock)"

    # Success without markers: the form is gone and a logout control remains
    # (e.g. the AJAX view swapped to Final with no headline text we know).
    try:
        has_form = any(page.locator(sel).count() > 0
                       for sel in settings.password_selectors)
        has_logout = any(page.locator(sel).count() > 0
                         for sel in LOGOUT_SELECTORS)
        if (not has_form) and has_logout:
            return True, "login form gone and logout control present"
    except Exception:
        pass

    LOG.warning("portal state at unclear: %s", describe_page(page))
    screenshot(page, settings, "unclear")
    return False, "login result unclear"


def run_login(settings: Settings) -> int:
    """Drive all attempts.  Returns an EXIT_* code."""
    LOG.info("starting login (headless=%s timeout=%ss attempts=%d)",
             settings.headless, settings.step_timeout, settings.max_attempts)

    with sync_playwright() as playwright:
        browser = None
        page = None
        launched_once = False

        def drop_browser() -> None:
            nonlocal browser, page
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            browser = None
            page = None

        try:
            for attempt in range(1, settings.max_attempts + 1):
                LOG.info("--- attempt %d/%d ---", attempt, settings.max_attempts)
                try:
                    if browser is None:
                        browser = launch_chromium(playwright, settings.headless)
                        page = new_page(browser, settings)
                        launched_once = True

                    ok, reason = login_once(page, settings)
                    if ok:
                        LOG.info("login succeeded: %s", reason)
                        return EXIT_OK
                    LOG.warning("attempt %d failed: %s", attempt, reason)
                except PlaywrightError as exc:
                    LOG.warning("browser error (%s); restarting the browser", exc)
                    drop_browser()
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("unexpected error: %s", exc)
                    drop_browser()

                if attempt < settings.max_attempts and settings.retry_backoff:
                    LOG.info("retrying in %ds", settings.retry_backoff)
                    time.sleep(settings.retry_backoff)

            if not launched_once:
                # Never even got a browser: that is an environment problem, not
                # a portal problem, and it has a one-line fix.
                LOG.error("could not start Chromium")
                LOG.error("fix: %s -m playwright install chromium", sys.executable)
                return EXIT_DEPENDENCY
            return EXIT_FAIL
        finally:
            drop_browser()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def dry_run(settings: Settings) -> int:
    LOG.info("=== dry run (nothing is changed) ===")
    LOG.info("version:          %s", __version__)
    LOG.info("portal url:       %s", settings.portal_url)
    LOG.info("username:         %s", settings.username or "<unset>")
    LOG.info("password:         %s",
             f"<set, {len(settings.password)} chars>" if settings.password
             else "<unset>")
    LOG.info("headless:         %s", settings.headless)
    LOG.info("step timeout:     %ss", settings.step_timeout)
    LOG.info("attempts:         %s", settings.max_attempts)
    LOG.info("log file:         %s", settings.log_file)
    LOG.info("screenshot dir:   %s", settings.screenshot_dir)
    LOG.info("lock file:        %s", settings.lock_file)
    LOG.info("config file:      %s", settings.env_file)
    LOG.info("config exists:    %s", settings.env_file.is_file())

    LOG.info("internet:         %s",
             "yes" if have_internet(settings.connectivity_timeout)
             else "no / captive portal")
    LOG.info("portal reachable: %s",
             "yes" if portal_reachable(settings.portal_url,
                                        timeout=settings.portal_timeout) else "no")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        LOG.info("playwright:       importable")
    except ImportError:
        LOG.error("playwright:       NOT importable")
        return EXIT_DEPENDENCY

    if not settings.has_credentials:
        LOG.error("dry run: credentials missing (see %s)", settings.env_file)
        return EXIT_CONFIG

    LOG.info("dry run complete")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="College WiFi captive-portal auto-login.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--config", metavar="PATH",
                        help="Path to the credentials env file (default: platform config dir).")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Run the browser headless (default: from config / True).")
    parser.add_argument("--timeout", type=int,
                        help="Per-step timeout in seconds.")
    parser.add_argument("--log-file", metavar="PATH",
                        help="Override the log file path.")
    parser.add_argument("--screenshot-dir", metavar="PATH",
                        help="Override the screenshot directory.")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose (debug) logging.")
    parser.add_argument("--quiet", action="store_true",
                        help="Log to the file only, not to stdout.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report config and connectivity, don't log in.")
    parser.add_argument("--skip-internet-check", action="store_true",
                        help="Skip the fast-path internet check and the portal "
                             "reachability pre-check (always try to log in).")
    parser.add_argument("--force", action="store_true",
                        help="Alias for --skip-internet-check.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    overrides = {
        "headless": args.headless,
        "step_timeout": args.timeout,
        "log_file": args.log_file,
        "screenshot_dir": args.screenshot_dir,
        "skip_internet_check": bool(args.skip_internet_check or args.force),
    }
    settings = load_settings(
        env_file=Path(args.config) if args.config else None,
        overrides=overrides,
    )

    global LOG
    LOG = setup_logging(settings.log_file, verbose=args.verbose, quiet=args.quiet)

    if args.dry_run:
        return dry_run(settings)

    if not settings.has_credentials:
        LOG.error("COLLEGE_WIFI_USER and COLLEGE_WIFI_PASS must be set")
        LOG.error("(config file: %s)", settings.env_file)
        return EXIT_CONFIG

    # Fast path: nothing to do if we already have internet.
    if not settings.skip_internet_check:
        if have_internet(settings.connectivity_timeout):
            LOG.info("already online; nothing to do")
            return EXIT_OK

        # Avoid spawning a browser when there is clearly nothing to talk to.
        if not portal_reachable(settings.portal_url,
                    timeout=settings.portal_timeout):
            LOG.error("no internet and the portal is not reachable: %s",
                      settings.portal_url)
            LOG.error("(use --skip-internet-check to try anyway)")
            return EXIT_NO_PORTAL

    if not acquire_lock(settings.lock_file):
        LOG.info("another instance is running; exiting")
        return EXIT_OK

    try:
        return run_login(settings)
    finally:
        release_lock(settings.lock_file)


if __name__ == "__main__":
    sys.exit(main())
