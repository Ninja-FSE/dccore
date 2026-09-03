"""A required setting cannot be blanked, and the pre-flight says so first.

TWO HALVES OF ONE HOLE

settings_file.REQUIRED - NICKNAME, CHANNEL, ADMIN_NICK - was enforced in
exactly one place: oserve.startup(), at boot. Nothing checked it on the WRITE
path, and the write path is the web dashboard's Settings page and configure.py.

So: clear the nickname field in the dashboard, save, and be told it saved. It
did save. The daemon then refuses to start - and the dashboard is served by the
daemon, so the one screen that value could be corrected from is gone with it.
Nothing anywhere says that hand-editing settings.conf is now the only way back
in. An operator who has never opened that file has no reason to guess.

The second half is the pre-flight. scripts/setup_check.py existed to be the
friendlier, earlier warning, and deliberately skipped this check on the
reasoning that boot catches it "hard". Boot catches it by exiting. So a fresh
install was told "Ready to start" and then refused seconds later, with the
pre-flight's verdict being the wrong one.

WHY THE WRITE GUARD LIVES IN _check_writable

Every writer goes through settings_file.save(): the dashboard
(webserver.py:1344), configure.py's wizard (configure.py:230) and the admin
console's CLI. Putting it at that chokepoint covers all three and cannot be
bypassed by a fourth arriving later.

NOT the same rule as unconfigured_required(). That treats "still the shipped
default" as unconfigured, which is right for a boot check and wrong for a write
check - an operator is allowed to set a value that happens to equal the
default. Only blanking is refused here.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class SavingCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "settings.conf")
        self.set_config(NICKNAME="TestBot", CHANNEL="#test",
                        ADMIN_NICK="operator")

    def save(self, **changes):
        return settings_file.save(vars(config), changes, path=self.path,
                                  log=lambda *a, **k: None)

    def file_text(self):
        if not os.path.exists(self.path):
            return ""
        with io.open(self.path, encoding="utf-8") as handle:
            return handle.read()


class ARequiredSettingCannotBeBlanked(SavingCase):

    def test_each_one_is_refused(self):
        for name in sorted(settings_file.REQUIRED):
            with self.subTest(setting=name):
                with self.assertRaises(settings_file.SettingsWriteError) as caught:
                    self.save(**{name: ""})

                self.assertIn(name, str(caught.exception))

    def test_whitespace_is_not_a_way_round_it(self):
        """The file strips on read, so a run of spaces reads back as blank.

        The MESSAGE is asserted, not just the exception type. A separate check
        further down rejects leading or trailing spaces on any setting and
        raises the same class, so dropping .strip() from this guard still
        raised something - and a mutation run showed this test passing against
        exactly that. It proved an error happened, not that this guard was the
        one that caused it.
        """
        for blank in ("   ", "\t", " \t "):
            with self.subTest(value=repr(blank)):
                with self.assertRaises(settings_file.SettingsWriteError) as caught:
                    self.save(NICKNAME=blank)

                self.assertIn("cannot be blank", str(caught.exception))

    def test_the_message_says_what_to_do(self):
        """An operator who hits this is one click from an unbootable install.
        The error is the only thing that will tell them why."""
        with self.assertRaises(settings_file.SettingsWriteError) as caught:
            self.save(NICKNAME="")
        message = str(caught.exception).lower()

        self.assertIn("blank", message)
        self.assertIn("start", message)

    def test_nothing_is_written(self):
        """The refusal has to happen before the file is touched, or a rejected
        save still half-applies."""
        self.save(NICKNAME="GoodNick")
        before = self.file_text()

        with self.assertRaises(settings_file.SettingsWriteError):
            self.save(NICKNAME="")

        self.assertEqual(self.file_text(), before)

    def test_a_good_value_in_the_same_save_is_not_written_either(self):
        """save() takes several changes at once - the dashboard sends the whole
        form. One bad value must not leave the other half applied."""
        with self.assertRaises(settings_file.SettingsWriteError):
            self.save(CHANNEL="#kept", NICKNAME="")

        self.assertNotIn("#kept", self.file_text())


class OrdinarySavesStillWork(SavingCase):
    """Controls. A guard that refuses everything would pass every test above."""

    def test_a_required_setting_can_be_changed(self):
        self.save(NICKNAME="RenamedBot")

        self.assertIn("RenamedBot", self.file_text())

    def test_a_non_required_setting_can_still_be_blanked(self):
        """DEBUG_CHANNEL ships blank on purpose (#193) and having no debug
        channel is a supported configuration. The guard must not spread."""
        self.assertNotIn("DEBUG_CHANNEL", settings_file.REQUIRED)

        self.save(DEBUG_CHANNEL="")

        self.assertIn("DEBUG_CHANNEL", self.file_text())

    def test_file_directory_can_still_be_blanked(self):
        """Deliberately not REQUIRED: a blank one means "not chosen yet", and
        the dashboard is the place it gets chosen."""
        self.assertNotIn("FILE_DIRECTORY", settings_file.REQUIRED)

        self.save(FILE_DIRECTORY="")

        self.assertIn("FILE_DIRECTORY", self.file_text())


class TheGuardCoversEveryWriter(unittest.TestCase):
    """The dashboard, configure.py and the admin console CLI all reach the
    file through save(). Pinned so a fourth writer cannot appear beside it."""

    def test_configure_py_writes_through_settings_file_save(self):
        with io.open(os.path.join(REPO_ROOT, "configure.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("settings_file.save(", source)

    def test_the_dashboard_writes_through_settings_file_save(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("settings_file.save(", source)


def _run_setup_check(admin_config_source):
    """Run the real pre-flight as a subprocess with a throwaway admin_config.py
    earlier on sys.path than the repository's own.

    The same harness tests/test_check_setup_numeric_sanity.py uses, and for the
    same reason: a machine actually running this daemon has a real
    admin_config.py with real content, and a test must never write to it.
    """
    import shutil
    import subprocess
    import tempfile

    check = os.path.join(REPO_ROOT, "scripts", "linux", "check-setup.py")
    tmp_dir = tempfile.mkdtemp(prefix="dccore-required-")
    try:
        with io.open(os.path.join(tmp_dir, "admin_config.py"), "w",
                     encoding="utf-8") as handle:
            handle.write(admin_config_source)
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp_dir + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run([sys.executable, check], cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60, text=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)



CONFIGURED = '''
NICKNAME = "TestBot"
CHANNEL = "#test"
ADMIN_NICK = "operator"
'''

ONLY_NICKNAME_BLANK = '''
NICKNAME = ""
CHANNEL = "#test"
ADMIN_NICK = "operator"
'''


class ThePreflightAgreesWithStartup(unittest.TestCase):
    """setup_check.py is the friendlier, earlier warning. It used to skip this
    check entirely, deferring to boot on the reasoning that boot catches it
    "hard" - which it does, by calling sys.exit(1). So the pre-flight said
    "Ready to start" and the daemon refused seconds later, and of the two
    verdicts the pre-flight's was the wrong one.

    It calls settings_file.unconfigured_required() rather than reimplementing
    the rule, so there is one definition of what "configured" means and the two
    cannot drift apart again.
    """

    def test_a_blank_required_setting_is_a_problem_not_a_pass(self):
        result = _run_setup_check(ONLY_NICKNAME_BLANK)

        # It must be a PROBLEM, not a warning. The exit code alone does not
        # show that: this harness supplies admin_config.py on PYTHONPATH while
        # a separate check wants the file in the repository directory, so the
        # run already exits 1 for its own reasons. warn() in place of fail()
        # survived a mutation run against the earlier version of this
        # assertion, which checked only the exit code and the absence of
        # "Ready to start" - both of which that unrelated failure guaranteed.
        flagged = [line for line in result.stdout.splitlines()
                   if "NICKNAME" in line and "unconfigured" in line]
        self.assertTrue(flagged, "NICKNAME was not reported at all")
        self.assertTrue(
            any("FAIL" in line for line in flagged),
            "only the verdict list mentions it, so it was reported "
            "as a warning and the run would still say Ready to start")

        listed = [line.strip() for line in result.stdout.splitlines()
                  if line.strip().startswith("- NICKNAME")]
        self.assertTrue(listed,
                        "not carried into the 'fix these before starting' list")

    def test_it_names_every_unconfigured_setting_not_just_the_first(self):
        """An operator fixing them one run at a time is the slow version of
        this check being useful."""
        result = _run_setup_check("")

        for name in ("NICKNAME", "CHANNEL", "ADMIN_NICK"):
            with self.subTest(setting=name):
                self.assertIn(name, result.stdout)

    def test_a_configured_install_is_not_flagged(self):
        """Control. A check that failed everything would satisfy both tests
        above and be worth nothing.

        Asserted on this check's own output rather than on the overall verdict:
        the harness supplies admin_config.py through PYTHONPATH, and a separate
        check looks for the FILE in the repository directory, so the run's exit
        code turns on something this change does not touch."""
        result = _run_setup_check(CONFIGURED)

        self.assertNotIn("unconfigured", result.stdout)
        self.assertIn("nickname TestBot", result.stdout)
        self.assertIn("admin nick operator", result.stdout)


if __name__ == "__main__":
    unittest.main()
