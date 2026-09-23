"""Integration-ish tests for the installer, templates and CLI surfaces.

These do not need a browser and do not touch the network.  Anything that
requires Playwright is skipped automatically when it is not installed.

Run from the repository root:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import college_wifi_common as cwc  # noqa: E402

HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


#: Swallow the login script's logging during tests.
_NULL_LOG = logging.getLogger("cwc-silent")
_NULL_LOG.addHandler(logging.NullHandler())
_NULL_LOG.propagate = False


def quiet(func, *args, **kwargs):
    """Call something whose console chatter we don't want in test output."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return func(*args, **kwargs)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install_mod = load_module(REPO_ROOT / "install.py", "cwa_install")
uninstall_mod = load_module(REPO_ROOT / "uninstall.py", "cwa_uninstall")
doctor_mod = load_module(REPO_ROOT / "bin" / "college-wifi-doctor.py", "cwa_doctor")


class TestRepoTemplates(unittest.TestCase):
    """Guard against a placeholder being renamed on one side only."""

    EXPECTED = {
        "platform/linux/wifi-watch.service.in": ("@PYTHON@", "@LOGIN_SCRIPT@"),
        "platform/linux/wifi-watch.timer": (),
        "platform/linux/90-college-wifi-login.in": (
            "@USER@", "@HOME@", "@PYTHON@", "@LOGIN_SCRIPT@"),
        "platform/macos/com.college-wifi-autologin.watch.plist.in": (
            "@PYTHON@", "@LOGIN_SCRIPT@", "@LOG_FILE@"),
        "platform/windows/task.xml.in": (
            "@PYTHON@", "@LOGIN_SCRIPT@", "@USERID@"),
    }

    def test_placeholders_present(self):
        for relative, placeholders in self.EXPECTED.items():
            path = REPO_ROOT / relative
            self.assertTrue(path.is_file(), msg=relative)
            text = path.read_text(encoding="utf-8")
            for placeholder in placeholders:
                self.assertIn(placeholder, text, msg=f"{relative}: {placeholder}")

    def test_no_unknown_placeholders_left(self):
        import re

        known = {"@PYTHON@", "@LOGIN_SCRIPT@", "@USER@", "@HOME@", "@LOG_FILE@",
                 "@USERID@"}
        for relative in self.EXPECTED:
            path = REPO_ROOT / relative
            # Skip the Windows template: it is UTF-16 in the installer, but the
            # repo copy is UTF-8 and still human readable.
            text = path.read_text(encoding="utf-8", errors="replace")
            found = set(re.findall(r"@[A-Z_]+@", text))
            self.assertTrue(found <= known, msg=f"{relative}: {found - known}")

    def test_windows_template_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        text = (REPO_ROOT / "platform" / "windows" / "task.xml.in").read_text(
            encoding="utf-8")
        text = text.replace("@PYTHON@", "C:/py.exe")
        text = text.replace("@LOGIN_SCRIPT@", "C:/login.py")
        text = text.replace("@USERID@", r"DOMAIN\user")
        ET.fromstring(text)  # raises if malformed

    def test_python_sources_all_exist(self):
        for name in install_mod.PYTHON_SOURCES:
            self.assertTrue((REPO_ROOT / "bin" / name).is_file(), msg=name)


class TestInstallerLayout(unittest.TestCase):
    def test_check_repo_layout_passes(self):
        quiet(install_mod.check_repo_layout)  # dies on failure

    def test_stale_bash_trigger_is_gone_from_the_repo(self):
        self.assertFalse((REPO_ROOT / "bin" / "college-wifi-trigger.sh").exists())


class TestCredentialRoundTrip(unittest.TestCase):
    """What install.py writes must be exactly what the login script reads."""

    PASSWORDS = [
        "simplepassword",
        "has spaces",
        "it's-a-quote",
        "back\\slash",
        '$dollar`tick!bang',
        "with=equals#hash",
        "trailing'",
        "'leading",
        "unicode-\u00fc\u00e9",
    ]

    def test_unattended_write_then_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            for password in self.PASSWORDS:
                with mock.patch.dict(os.environ, {
                    "COLLEGE_WIFI_USER": "user@example.edu",
                    "COLLEGE_WIFI_PASS": password,
                }):
                    env_file = quiet(install_mod.write_credentials,
                                     config_dir, unattended=True, reconfigure=True)
                parsed = cwc.parse_env_file(env_file)
                self.assertEqual(parsed["COLLEGE_WIFI_USER"], "user@example.edu")
                self.assertEqual(parsed["COLLEGE_WIFI_PASS"], password,
                                 msg=repr(password))

    def test_permissions_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            with mock.patch.dict(os.environ, {"COLLEGE_WIFI_USER": "u",
                                              "COLLEGE_WIFI_PASS": "p"}):
                env_file = quiet(install_mod.write_credentials,
                                 config_dir, unattended=True, reconfigure=True)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_existing_file_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            with mock.patch.dict(os.environ, {"COLLEGE_WIFI_USER": "first",
                                              "COLLEGE_WIFI_PASS": "one"}):
                env_file = quiet(install_mod.write_credentials,
                                 config_dir, unattended=True, reconfigure=True)
            with mock.patch.dict(os.environ, {"COLLEGE_WIFI_USER": "second",
                                              "COLLEGE_WIFI_PASS": "two"}):
                same = quiet(install_mod.write_credentials,
                             config_dir, unattended=True, reconfigure=False)
            self.assertEqual(same, env_file)
            self.assertEqual(cwc.parse_env_file(env_file)["COLLEGE_WIFI_USER"],
                             "first")


class TestInstallScripts(unittest.TestCase):
    def test_copies_every_python_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            login, doctor = quiet(install_mod.install_scripts, data)
            for name in install_mod.PYTHON_SOURCES:
                self.assertTrue((data / "bin" / name).is_file(), msg=name)
            self.assertTrue(login.is_file())
            self.assertTrue(doctor.is_file())

    def test_removes_obsolete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            bin_dir = data / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "college-wifi-trigger.sh").write_text("#!/bin/bash\n")
            quiet(install_mod.install_scripts, data)
            self.assertFalse((bin_dir / "college-wifi-trigger.sh").exists())


class TestDoctorReporter(unittest.TestCase):
    def test_exit_code_clean(self):
        def run():
            report = doctor_mod.Reporter(as_json=False)
            report.ok("everything fine")
            return report.finish()

        self.assertEqual(quiet(run), 0)

    def test_exit_code_on_failure(self):
        def run():
            report = doctor_mod.Reporter(as_json=False)
            report.ok("fine")
            report.fail("nope")
            return report.finish()

        self.assertEqual(quiet(run), 1)

    def test_json_output_is_parseable(self):
        report = doctor_mod.Reporter(as_json=True)
        report.section("test")
        report.ok("good")
        report.warn("meh")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = report.finish()
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["warnings"], 1)
        self.assertEqual(payload["failures"], 0)
        self.assertEqual(payload["results"][0]["status"], "OK")

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            quiet(doctor_mod.main, ["--version"])
        self.assertEqual(ctx.exception.code, 0)


class TestUninstaller(unittest.TestCase):
    def test_default_keeps_logs_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "logs").mkdir(parents=True)
            (data / "logs" / "login.log").write_text("hello\n")
            (data / "bin").mkdir()
            (data / "bin" / "college-wifi-login.py").write_text("x\n")
            (data / "venv").mkdir()
            (data / "login.lock").write_text("1\n")

            for name in uninstall_mod.REMOVABLE_IN_DATA:
                quiet(uninstall_mod.remove_path, data / name, dry_run=False)

            self.assertTrue((data / "logs" / "login.log").is_file())
            self.assertFalse((data / "bin").exists())
            self.assertFalse((data / "venv").exists())
            self.assertFalse((data / "login.lock").exists())

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "folder"
            target.mkdir()
            quiet(uninstall_mod.remove_path, target, dry_run=True)
            self.assertTrue(target.exists())

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            quiet(uninstall_mod.main, ["--version"])
        self.assertEqual(ctx.exception.code, 0)


class TestInstallerCli(unittest.TestCase):
    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            quiet(install_mod.main, ["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_parse_args_defaults(self):
        args = install_mod.parse_args([])
        self.assertFalse(args.unattended)
        self.assertFalse(args.no_trigger)
        self.assertFalse(args.skip_playwright)


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright is not installed")
class TestLoginCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.login = load_module(REPO_ROOT / "bin" / "college-wifi-login.py",
                                "cwa_login")

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            quiet(self.login.main, ["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_parse_args_force_alias(self):
        args = self.login.parse_args(["--force", "--timeout", "12"])
        self.assertTrue(args.force)
        self.assertEqual(args.timeout, 12)

    def test_text_markers(self):
        self.assertTrue(self.login.text_is_success("network access granted",
                                                   cwc.SUCCESS_MARKERS))
        self.assertFalse(self.login.text_is_success("please log in",
                                                    cwc.SUCCESS_MARKERS))
        self.assertTrue(self.login.text_is_failure("invalid password",
                                                   cwc.FAILURE_MARKERS))
        self.assertFalse(self.login.text_is_failure("all good",
                                                    cwc.FAILURE_MARKERS))

    def test_exit_codes_are_distinct(self):
        codes = {self.login.EXIT_OK, self.login.EXIT_FAIL,
                 self.login.EXIT_CONFIG, self.login.EXIT_DEPENDENCY,
                 self.login.EXIT_NO_PORTAL}
        self.assertEqual(len(codes), 5)

    # -- run_login outcome mapping ----------------------------------------
    class _DummyPlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    def _settings(self, **overrides):
        overrides.setdefault("username", "user")
        overrides.setdefault("password", "pass")
        overrides.setdefault("max_attempts", 1)
        overrides.setdefault("retry_backoff", 0)
        settings = cwc.load_settings(Path("/nonexistent/wifi-login.env"),
                                     overrides=overrides)
        return settings

    def test_run_login_returns_ok_on_success(self):
        with mock.patch.object(self.login, "LOG", _NULL_LOG), \
                mock.patch.object(self.login, "sync_playwright",
                                  return_value=self._DummyPlaywrightContext()), \
                mock.patch.object(self.login, "launch_chromium",
                                  return_value=object()), \
                mock.patch.object(self.login, "new_page", return_value=object()), \
                mock.patch.object(self.login, "login_once",
                                  return_value=(True, "done")):
            self.assertEqual(self.login.run_login(self._settings()),
                             self.login.EXIT_OK)

    def test_run_login_returns_fail_when_the_portal_fails(self):
        with mock.patch.object(self.login, "LOG", _NULL_LOG), \
                mock.patch.object(self.login, "sync_playwright",
                                  return_value=self._DummyPlaywrightContext()), \
                mock.patch.object(self.login, "launch_chromium",
                                  return_value=object()), \
                mock.patch.object(self.login, "new_page", return_value=object()), \
                mock.patch.object(self.login, "login_once",
                                  return_value=(False, "nope")):
            self.assertEqual(self.login.run_login(self._settings()),
                             self.login.EXIT_FAIL)

    def test_run_login_reports_dependency_error_when_browser_will_not_start(self):
        with mock.patch.object(self.login, "LOG", _NULL_LOG), \
                mock.patch.object(self.login, "sync_playwright",
                                  return_value=self._DummyPlaywrightContext()), \
                mock.patch.object(self.login, "launch_chromium",
                                  side_effect=self.login.PlaywrightError("no browser")):
            self.assertEqual(self.login.run_login(self._settings()),
                             self.login.EXIT_DEPENDENCY)


if __name__ == "__main__":
    unittest.main()
