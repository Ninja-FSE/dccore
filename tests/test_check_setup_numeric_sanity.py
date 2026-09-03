"""Regression tests for scripts/linux/check-setup.py's numeric sanity checks.

check-setup.py is a standalone script (not a set of importable functions -
`import config` at module scope, print()s a report, sys.exit(1)s on a real
problem), so it is run as a real subprocess here rather than imported, the
same way tests/test_console_encoding.py drives platform_compat's console
guard as a child process.

Before this fix, two misconfigurations passed the check silently and only
surfaced once the daemon was actually running:

* MAX_DCC_SLOTS <= 0 - the bot boots, joins its channels, and then never
  dispatches a single queued transfer, because dcc.py's own slot-count gate
  (`len(config.active_transfers) < config.MAX_DCC_SLOTS`) can never be true.
  Every request just sits in the queue forever, with nothing in the startup
  output saying why.
* DCC_PORT_START > DCC_PORT_END - range(start, end + 1) is empty, so the
  ports loop found 0 free ports and reported "no port in X-Y could be
  bound - something else is using them", which sends an operator looking
  for a phantom port conflict instead of the swapped values that are
  actually the problem.

admin_config.py is gitignored and gets `from admin_config import *`-applied
by config.py if present (see config.py's own comment on that mechanism) -
these tests inject one through PYTHONPATH rather than writing into the real
repository's admin_config.py, which a machine actually running this daemon
could have real, meaningful content in.
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_SETUP = os.path.join(REPO_ROOT, "scripts", "linux", "check-setup.py")


def _run_with_admin_config(overrides_source, extra_env=None):
    """Run check-setup.py as a real subprocess with a throwaway admin_config.py
    placed earlier on sys.path than the real repository - config.py's own
    `from admin_config import *` then picks up ours, not (a nonexistent, in
    this checkout) real one, and the real repository is never touched.
    """
    tmp_dir = tempfile.mkdtemp(prefix="dccore-checksetup-")
    try:
        with open(os.path.join(tmp_dir, "admin_config.py"), "w", encoding="utf-8") as handle:
            handle.write(overrides_source)

        env = dict(os.environ)
        env["PYTHONPATH"] = tmp_dir + os.pathsep + env.get("PYTHONPATH", "")
        if extra_env:
            env.update(extra_env)

        return subprocess.run(
            [sys.executable, CHECK_SETUP],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True,
        )
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class MaxDccSlotsSanityTests(unittest.TestCase):

    def test_zero_slots_is_reported_as_a_failure(self):
        result = _run_with_admin_config("MAX_DCC_SLOTS = 0\n")
        self.assertIn("MAX_DCC_SLOTS is 0", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_negative_slots_is_reported_as_a_failure(self):
        result = _run_with_admin_config("MAX_DCC_SLOTS = -2\n")
        self.assertIn("MAX_DCC_SLOTS is -2", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_a_positive_slot_count_is_not_flagged(self):
        result = _run_with_admin_config("MAX_DCC_SLOTS = 5\n")
        self.assertNotIn("MAX_DCC_SLOTS is", result.stdout)
        self.assertIn("max DCC slots 5", result.stdout)


class DccPortRangeSanityTests(unittest.TestCase):

    def test_a_swapped_range_is_reported_as_a_failure_not_a_phantom_conflict(self):
        result = _run_with_admin_config(
            "DCC_PORT_START = 55010\nDCC_PORT_END = 55000\n")
        self.assertIn(
            "DCC_PORT_START (55010) is greater than DCC_PORT_END (55000)",
            result.stdout)
        # The old, misleading message for this exact case must not appear -
        # this range was never actually contended, it is just empty.
        self.assertNotIn("something else is using them", result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_an_ordinary_range_is_not_flagged(self):
        result = _run_with_admin_config(
            "DCC_PORT_START = 55000\nDCC_PORT_END = 55010\n")
        self.assertNotIn("is greater than DCC_PORT_END", result.stdout)


class AdminNickIsReportedPlainly(unittest.TestCase):
    """#162 finding #20 originally added an upstream-identity comparison
    here (NICKNAME/CHANNEL/ADMIN_NICK against a hardcoded list of the real
    production bot's own values) - removed again: settings_file.REQUIRED
    (enforced hard by oserve.startup()) already refuses to boot on a blank
    NICKNAME/CHANNEL/ADMIN_NICK regardless of whether this check ever runs,
    which is the load-bearing protection; comparing an operator's own,
    deliberately-chosen ADMIN_NICK against a fixed list of specific people's
    real nicks added a second, narrower layer that could not tell "forgot to
    change it" apart from "this genuinely is that person" - any value here
    is just reported, never judged."""

    def test_any_admin_nick_is_reported_plainly_with_no_judgement(self):
        result = _run_with_admin_config("ADMIN_NICK = 'SysOp,Op2'\n")
        self.assertIn("ok     admin nick SysOp,Op2", result.stdout)
        self.assertNotIn("WARN   ADMIN_NICK", result.stdout)
        self.assertNotIn("FAIL   ADMIN_NICK", result.stdout)


class FileDirectorySanityTests(unittest.TestCase):
    """FILE_DIRECTORY is deliberately NOT in settings_file.REQUIRED (see its
    own comment) - found live, running configure.py against a real install:
    requiring it blocked the daemon from ever reaching the web dashboard,
    the one place that is genuinely easier to set it from. Unset is a WARN
    here, not a FAIL; a value that IS set but wrong stays a FAIL - that is a
    real misconfiguration, not an unmade choice."""

    def test_unset_is_a_warning_not_a_failure(self):
        result = _run_with_admin_config("ADMIN_NICK = 'X'\n")
        self.assertIn("WARN   FILE_DIRECTORY is not set yet", result.stdout)
        self.assertNotIn("FAIL   FILE_DIRECTORY", result.stdout)

    def test_a_set_but_missing_directory_is_still_a_failure(self):
        result = _run_with_admin_config(
            "ADMIN_NICK = 'X'\nFILE_DIRECTORY = '/definitely/not/a/real/path'\n")
        self.assertIn("FAIL   FILE_DIRECTORY does not exist", result.stdout)

    def test_a_real_directory_is_ok(self):
        tmp_dir = tempfile.mkdtemp(prefix="dccore-checksetup-music-")
        try:
            result = _run_with_admin_config(
                f"ADMIN_NICK = 'X'\nFILE_DIRECTORY = {tmp_dir!r}\n")
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self.assertIn(f"ok     music directory {tmp_dir}", result.stdout)
        self.assertNotIn("WARN   FILE_DIRECTORY", result.stdout)
        self.assertNotIn("FAIL   FILE_DIRECTORY", result.stdout)


class CleanConfigurationPassesEndToEnd(unittest.TestCase):
    """Control: a configuration with nothing wrong except (an artifact of
    how this test injects overrides, not a real misconfiguration - see
    _run_with_admin_config's docstring) the literal <repo>/admin_config.py
    and <repo>/settings.conf existence check (#162 finding #19: neither
    exists in a fresh checkout, and check-setup.py now only fails when
    BOTH are absent - see scripts/setup_check.py's own comment) reports
    only THAT one problem, proving the two new checks above do not
    false-positive on ordinary, valid values."""

    def test_only_the_unrelated_admin_config_path_check_fires(self):
        tmp_dir = tempfile.mkdtemp(prefix="dccore-checksetup-music-")
        try:
            result = _run_with_admin_config(
                "NICKNAME = 'TotallyNotUpstream'\n"
                "ADMIN_NICK = 'TotallyNotUpstreamEither'\n"
                f"FILE_DIRECTORY = {tmp_dir!r}\n"
                "MAX_DCC_SLOTS = 3\n"
                "DCC_PORT_START = 55000\n"
                "DCC_PORT_END = 55010\n"
            )
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertNotIn("MAX_DCC_SLOTS is", result.stdout)
        self.assertNotIn("is greater than DCC_PORT_END", result.stdout)
        self.assertIn("ok     max DCC slots 3", result.stdout)
        # Not "all N ports free": some of 55000-55010 may genuinely be busy on
        # a machine already running other tests or another instance of the
        # daemon - that outcome (and "no port ... could be bound" if every one
        # of them happens to be busy) is pre-existing, unrelated behaviour of
        # the bind-probing loop this PR does not touch, so it is not asserted
        # on either way here.
        self.assertIn("no admin_config.py and no settings.conf", result.stdout)


class SettingsConfSatisfiesTheConfigurationRequirement(unittest.TestCase):
    """#162 finding #19's core claim, exercised for real: settings.conf ALONE,
    with no admin_config.py at all, must satisfy check-setup.py's "you
    configured something" requirement. It used to hard-fail unconditionally
    whenever admin_config.py was absent, even though the daemon starts fine
    from settings.conf alone (config.py's settings_file.apply_to() applies it
    the same way regardless of which file provided it) - contradicting the
    check's own "Applied N setting(s)" line on the very same run."""

    def test_settings_conf_alone_satisfies_the_check(self):
        tmp_dir = tempfile.mkdtemp(prefix="dccore-checksetup-settingsconf-")
        music_dir = tempfile.mkdtemp(prefix="dccore-checksetup-music-")
        try:
            settings_conf = os.path.join(tmp_dir, "settings.conf")
            with open(settings_conf, "w", encoding="utf-8") as handle:
                handle.write(
                    "NICKNAME = TotallyNotUpstream\n"
                    f"FILE_DIRECTORY = {music_dir}\n"
                )
            env = dict(os.environ)
            env["DCCORE_SETTINGS_FILE"] = settings_conf
            result = subprocess.run(
                [sys.executable, CHECK_SETUP],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True,
            )
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            shutil.rmtree(music_dir, ignore_errors=True)

        self.assertNotIn("no admin_config.py and no settings.conf", result.stdout)
        self.assertIn("configured via settings.conf (no admin_config.py)", result.stdout)
        self.assertIn("Applied", result.stdout)


if __name__ == "__main__":
    unittest.main()
