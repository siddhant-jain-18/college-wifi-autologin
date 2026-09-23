"""Unit tests for college_wifi_common.

These tests are stdlib-only and never touch the network, Playwright or the
real user configuration.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import college_wifi_common as cwc  # noqa: E402


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cwc-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        # Release log file handles before deleting the temp dir: on Windows an
        # open FileHandler keeps the file locked (WinError 32) and cleanup fails.
        logger = logging.getLogger("college-wifi")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
class TestEnvFile(unittest.TestCase):
    def parse(self, text: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wifi-login.env"
            path.write_text(text, encoding="utf-8")
            return cwc.parse_env_file(path)

    def test_basic(self):
        result = self.parse(
            "export COLLEGE_WIFI_USER='alice'\n"
            "export COLLEGE_WIFI_PASS='s3cret'\n"
        )
        self.assertEqual(result["COLLEGE_WIFI_USER"], "alice")
        self.assertEqual(result["COLLEGE_WIFI_PASS"], "s3cret")

    def test_comments_blanks_and_no_export(self):
        result = self.parse(
            "# a comment\n"
            "\n"
            "COLLEGE_WIFI_USER=bob\n"
            "   \t\n"
            "# COLLEGE_WIFI_PASS='ignored'\n"
        )
        self.assertEqual(result, {"COLLEGE_WIFI_USER": "bob"})

    def test_crlf_line_endings(self):
        result = self.parse("export COLLEGE_WIFI_USER='carol'\r\n"
                            "export COLLEGE_WIFI_PASS='pw'\r\n")
        self.assertEqual(result["COLLEGE_WIFI_USER"], "carol")
        self.assertEqual(result["COLLEGE_WIFI_PASS"], "pw")

    def test_value_containing_equals_and_hash(self):
        result = self.parse("export COLLEGE_WIFI_PASS='a=b#c'\n")
        self.assertEqual(result["COLLEGE_WIFI_PASS"], "a=b#c")

    def test_double_quotes(self):
        result = self.parse('export COLLEGE_WIFI_PASS="pa\\"ss"\n')
        self.assertEqual(result["COLLEGE_WIFI_PASS"], 'pa"ss')

    def test_escaped_single_quote(self):
        result = self.parse("export COLLEGE_WIFI_PASS='it'\\''s'\n")
        self.assertEqual(result["COLLEGE_WIFI_PASS"], "it's")

    def test_indented_assignment(self):
        result = self.parse("    export COLLEGE_WIFI_USER='dave'\n")
        self.assertEqual(result["COLLEGE_WIFI_USER"], "dave")

    def test_invalid_keys_are_skipped(self):
        result = self.parse("1BAD=x\nGOOD=1\n\nno-equals-here\n")
        self.assertEqual(result, {"GOOD": "1"})

    def test_missing_file(self):
        self.assertEqual(cwc.parse_env_file(Path("/nope/does-not-exist")), {})

    def test_empty_value(self):
        self.assertEqual(self.parse("COLLEGE_WIFI_PASS=\n"),
                         {"COLLEGE_WIFI_PASS": ""})


class TestQuoting(unittest.TestCase):
    TRICKY = [
        "",
        "simple",
        "with spaces",
        "it's",
        "qu'ote'inside",
        "'leading",
        "trailing'",
        "back\\slash",
        "$dollar`backtick!bang",
        "tab\tand\nnewline",
        "unicode-\u00e9\u00fc\u4e2d\u6587",
        "semi;colon&amp",
    ]

    def test_round_trip(self):
        for value in self.TRICKY:
            quoted = cwc.quote_value(value)
            self.assertEqual(cwc.unquote_value(quoted), value, msg=repr(value))

    def test_round_trip_ignores_surrounding_whitespace(self):
        self.assertEqual(cwc.unquote_value("  'p added'  "), "p added")

    def test_render_then_parse_round_trip(self):
        # The env file format is line based, so a literal newline in a value
        # cannot survive a round trip (quote/unquote still handles it above).
        values = {
            f"KEY_{i}": value
            for i, value in enumerate(self.TRICKY)
            if "\n" not in value
        }
        text = cwc.render_env_file(values, header="test file")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(cwc.parse_env_file(path), values)

    def test_render_includes_header(self):
        text = cwc.render_env_file({"A": "1"}, header="hello\nworld")
        self.assertIn("# hello", text)
        self.assertIn("# world", text)
        self.assertIn("export A='1'", text)


class TestCoercion(unittest.TestCase):
    def test_bool(self):
        for raw in ("1", "true", "TRUE", "yes", "On", " y "):
            self.assertTrue(cwc.coerce_bool(raw, False), msg=raw)
        for raw in ("0", "false", "no", "OFF", "n"):
            self.assertFalse(cwc.coerce_bool(raw, True), msg=raw)

    def test_bool_invalid_falls_back(self):
        self.assertTrue(cwc.coerce_bool("banana", True))
        self.assertFalse(cwc.coerce_bool("", False))
        self.assertTrue(cwc.coerce_bool(None, True))

    def test_int(self):
        self.assertEqual(cwc.coerce_int("42", 1), 42)
        self.assertEqual(cwc.coerce_int("  7 ", 1), 7)
        self.assertEqual(cwc.coerce_int(9, 1), 9)

    def test_int_invalid_and_clamped(self):
        self.assertEqual(cwc.coerce_int("abc", 30), 30)
        self.assertEqual(cwc.coerce_int("", 30), 30)
        self.assertEqual(cwc.coerce_int(None, 30), 30)
        self.assertEqual(cwc.coerce_int("-5", 30, minimum=1), 1)
        self.assertEqual(cwc.coerce_int("9999", 30, maximum=600), 600)

    def test_float(self):
        self.assertAlmostEqual(cwc.coerce_float("2.5", 1.0), 2.5)
        self.assertEqual(cwc.coerce_float("junk", 3.0), 3.0)
        self.assertEqual(cwc.coerce_float("0", 3.0, minimum=0.5), 0.5)

    def test_split_selectors(self):
        merged = cwc.split_selectors("#mine, input[name=x]\n#dup",
                                     ("#dup", "#default"))
        self.assertEqual(merged, ("#mine", "input[name=x]", "#dup", "#default"))

    def test_split_selectors_empty(self):
        self.assertEqual(cwc.split_selectors(None, ("#a",)), ("#a",))
        self.assertEqual(cwc.split_selectors("  ", ("#a",)), ("#a",))


class TestSettings(unittest.TestCase):
    def write_env(self, body: str) -> Path:
        path = self._dir / "wifi-login.env"
        path.write_text(body, encoding="utf-8")
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(os.environ, {}, clear=True)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_defaults_when_no_file(self):
        settings = cwc.load_settings(self._dir / "missing.env")
        self.assertEqual(settings.portal_url, cwc.DEFAULT_PORTAL_URL)
        self.assertEqual(settings.step_timeout, cwc.DEFAULT_STEP_TIMEOUT)
        self.assertEqual(settings.max_attempts, cwc.DEFAULT_MAX_ATTEMPTS)
        self.assertTrue(settings.headless)
        self.assertTrue(settings.block_resources)
        self.assertFalse(settings.has_credentials)
        self.assertEqual(settings.user_selectors[0],
                         cwc.USERNAME_SELECTORS[0])

    def test_file_values(self):
        env = self.write_env(
            "export COLLEGE_WIFI_USER='alice'\n"
            "export COLLEGE_WIFI_PASS='s3cret'\n"
            "export WIFI_LOGIN_TIMEOUT='99'\n"
            "export WIFI_LOGIN_HEADLESS='0'\n"
        )
        settings = cwc.load_settings(env)
        self.assertEqual(settings.username, "alice")
        self.assertEqual(settings.password, "s3cret")
        self.assertEqual(settings.step_timeout, 99)
        self.assertFalse(settings.headless)
        self.assertTrue(settings.has_credentials)

    def test_bad_numbers_fall_back(self):
        env = self.write_env("export WIFI_LOGIN_TIMEOUT='soon'\n"
                             "export WIFI_LOGIN_ATTEMPTS=''\n")
        settings = cwc.load_settings(env)
        self.assertEqual(settings.step_timeout, cwc.DEFAULT_STEP_TIMEOUT)
        self.assertEqual(settings.max_attempts, cwc.DEFAULT_MAX_ATTEMPTS)

    def test_environment_overrides_file(self):
        env = self.write_env("export COLLEGE_WIFI_USER='fromfile'\n"
                             "export WIFI_LOGIN_TIMEOUT='11'\n")
        with mock.patch.dict(os.environ,
                             {"COLLEGE_WIFI_USER": "fromenv",
                              "WIFI_LOGIN_TIMEOUT": "22"}):
            settings = cwc.load_settings(env)
        self.assertEqual(settings.username, "fromenv")
        self.assertEqual(settings.step_timeout, 22)

    def test_cli_override_wins(self):
        env = self.write_env("export COLLEGE_WIFI_USER='fromfile'\n")
        settings = cwc.load_settings(env, overrides={
            "username": "fromcli",
            "step_timeout": 5,
            "headless": False,
        })
        self.assertEqual(settings.username, "fromcli")
        self.assertEqual(settings.step_timeout, 5)
        self.assertFalse(settings.headless)

    def test_username_is_stripped(self):
        env = self.write_env("export COLLEGE_WIFI_USER='  padded  '\n")
        self.assertEqual(cwc.load_settings(env).username, "padded")

    def test_custom_selectors_are_prepended(self):
        env = self.write_env("export WIFI_LOGIN_USER_SELECTOR='#my-login'\n")
        settings = cwc.load_settings(env)
        self.assertEqual(settings.user_selectors[0], "#my-login")


class TestPaths(unittest.TestCase):
    def test_default_env_file_honours_env_override(self):
        with mock.patch.dict(os.environ,
                             {"COLLEGE_WIFI_CONFIG_FILE": "/custom/x.env"}):
            self.assertEqual(cwc.default_env_file(), Path("/custom/x.env"))

    def test_config_dir_from_env_uses_the_file_parent(self):
        with mock.patch.dict(
                os.environ, {"COLLEGE_WIFI_CONFIG_FILE": "/custom/dir/x.env"}):
            self.assertEqual(cwc.config_dir_from_env(), Path("/custom/dir"))

    def test_config_dir_from_env_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("college_wifi_common.config_dir",
                           return_value=Path("/fallback")):
            self.assertEqual(cwc.config_dir_from_env(), Path("/fallback"))

    def test_venv_python_posix(self):
        with mock.patch("college_wifi_common.platform.system",
                        return_value="Linux"):
            self.assertEqual(cwc.venv_python(Path("/data/venv")),
                             Path("/data/venv/bin/python"))

    def test_venv_python_windows(self):
        with mock.patch("college_wifi_common.platform.system",
                        return_value="Windows"):
            self.assertEqual(cwc.venv_python(Path("C:/data/venv")),
                             Path("C:/data/venv/Scripts/python.exe"))

    def test_fallback_dirs_are_absolute(self):
        with mock.patch("college_wifi_common._try_platformdirs",
                        return_value=None):
            self.assertTrue(cwc.config_dir().is_absolute())
            self.assertTrue(cwc.data_dir().is_absolute())


# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestProbes(unittest.TestCase):
    def test_have_internet_true_on_204_empty(self):
        with mock.patch("college_wifi_common._request",
                        return_value=_FakeResponse(204, b"")):
            self.assertTrue(cwc.have_internet(timeout=0.1))

    def test_have_internet_true_on_success_body(self):
        calls = []

        def fake(url, method="GET", timeout=5.0):
            calls.append(url)
            if "gstatic" in url:
                raise OSError("blocked")
            return _FakeResponse(200, b"success\n")

        with mock.patch("college_wifi_common._request", side_effect=fake):
            self.assertTrue(cwc.have_internet(timeout=0.1))
        self.assertTrue(any("firefox" in url for url in calls))

    def test_have_internet_false_when_portal_intercepts(self):
        # A captive portal answers 200 with a full HTML page instead of 204.
        with mock.patch("college_wifi_common._request",
                        return_value=_FakeResponse(200, b"<html>login</html>")):
            self.assertFalse(cwc.have_internet(timeout=0.1))

    def test_have_internet_false_on_errors(self):
        with mock.patch("college_wifi_common._request",
                        side_effect=OSError("no route to host")):
            self.assertFalse(cwc.have_internet(timeout=0.1))

    def test_portal_reachable_true_on_http_error(self):
        import urllib.error

        with mock.patch("college_wifi_common.urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "https://p", 403, "forbidden", {}, None)):
            self.assertTrue(cwc.portal_reachable("https://p"))

    def test_portal_reachable_false_on_connection_error(self):
        with mock.patch("college_wifi_common.urllib.request.urlopen",
                        side_effect=OSError("boom")):
            self.assertFalse(cwc.portal_reachable("https://p", timeout=0.1))


# ---------------------------------------------------------------------------
class TestLock(TempDirTestCase):
    def test_acquire_release(self):
        lock = self.tmp / "login.lock"
        self.assertTrue(cwc.acquire_lock(lock))
        self.assertTrue(lock.is_file())
        cwc.release_lock(lock)
        self.assertFalse(lock.exists())

    def test_second_acquire_fails(self):
        lock = self.tmp / "login.lock"
        self.assertTrue(cwc.acquire_lock(lock))
        self.assertFalse(cwc.acquire_lock(lock))

    def test_pid_is_recorded(self):
        lock = self.tmp / "login.lock"
        self.assertTrue(cwc.acquire_lock(lock, pid=4321))
        self.assertEqual(cwc.read_lock_pid(lock), 4321)

    def test_stale_lock_is_reclaimed(self):
        lock = self.tmp / "login.lock"
        lock.write_text("99999\n0\n", encoding="ascii")
        old = time.time() - (cwc.LOCK_MAX_AGE + 60)
        os.utime(lock, (old, old))
        self.assertTrue(cwc.lock_is_stale(lock))
        self.assertTrue(cwc.acquire_lock(lock))
        self.assertEqual(cwc.read_lock_pid(lock), os.getpid())

    def test_fresh_lock_is_respected(self):
        lock = self.tmp / "login.lock"
        lock.write_text("1\n0\n", encoding="ascii")
        self.assertFalse(cwc.lock_is_stale(lock))
        self.assertFalse(cwc.acquire_lock(lock))

    def test_release_is_idempotent(self):
        cwc.release_lock(self.tmp / "never-existed.lock")


# ---------------------------------------------------------------------------
class TestLogging(TempDirTestCase):
    def test_writes_and_rotates(self):
        log_file = self.tmp / "logs" / "login.log"

        logger = cwc.setup_logging(log_file, quiet=True)
        logger.info("hello world")
        # Close the handlers before rotating: Windows refuses to rename a file
        # that is still open by this process (unlike POSIX).
        for handler in logger.handlers:
            handler.flush()
            handler.close()
        self.assertIn("hello world", log_file.read_text(encoding="utf-8"))

        # Force rotation by making the file larger than the threshold.
        cwc.rotate_log(log_file, max_bytes=1, backups=1)
        self.assertTrue((self.tmp / "logs" / "login.log.1").is_file())

    def test_verbose_sets_debug_level(self):
        logger = cwc.setup_logging(self.tmp / "l.log", verbose=True, quiet=True)
        self.assertEqual(logger.level, logging.DEBUG)

    def test_handlers_are_not_duplicated(self):
        for _ in range(3):
            logger = cwc.setup_logging(self.tmp / "l.log", quiet=True)
        self.assertEqual(len(logger.handlers), 1)

    def test_survives_unwritable_log_location(self):
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            logger = cwc.setup_logging(Path("/proc/definitely/not/writable.log"),
                                       quiet=True)
            logger.info("this must not raise")
        self.assertTrue(logger.handlers)


class TestPrivateFile(TempDirTestCase):
    def test_creates_parents_and_restricts_permissions(self):
        target = self.tmp / "deep" / "nested" / "wifi-login.env"
        cwc.write_private_file(target, "export A='1'\n")
        self.assertTrue(target.is_file())
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class TestMisc(unittest.TestCase):
    def test_version_is_a_string(self):
        self.assertIsInstance(cwc.__version__, str)
        self.assertTrue(cwc.__version__[0].isdigit())


if __name__ == "__main__":
    unittest.main()
