"""The setup check on an empty tree told the novice to copy the sample
files, contradicting "the launcher is the install" (audit L21, #685).

`start-dccore check` is the documented pre-flight. On a tree with neither
admin_config.py nor settings.conf it FAILed with "copy admin_config.py.sample
to admin_config.py, or settings.conf.sample to settings.conf, and fill one of
them in" - exactly the manual step the launchers (#547) replaced - while the
next three FAILs on the same screen already said "Run configure.py". A novice
who followed the stale line created admin_config.py by hand, which is the
launcher's first-run gate: the questions and the browser setup page were
never offered, and the copied sample turned the dashboard and the debug
channel on for them. oserve.py's own refusal said "see admin_config.py.sample
/ settings.conf.sample" too.

Both say the same thing now: nothing is configured yet - run the launcher
(it asks the questions, or opens the setup page), or configure.py.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests import test_check_setup_numeric_sanity as check  # noqa: E402
from tests import test_startup as boot  # noqa: E402


class TheCheckOnAnEmptyTree(unittest.TestCase):
    """check-setup.py for real, with an admin_config.py that sets nothing -
    the one real repository file it would otherwise find is absent in a
    checkout, and settings.conf is pointed at a path that does not exist."""

    def run_check(self):
        import tempfile
        empty = os.path.join(tempfile.mkdtemp(prefix="dccore-empty-"), "settings.conf")
        return check._run_with_admin_config("", extra_env={"DCCORE_SETTINGS_FILE": empty})

    def test_it_says_run_the_launcher_or_configure_py(self):
        result = self.run_check()
        # Once as the FAIL line and once more in the summary at the end.
        lines = [l for l in result.stdout.splitlines() if "no admin_config.py and no settings.conf" in l]

        self.assertTrue(lines, result.stdout)
        for line in lines:
            self.assertIn("nothing is configured yet", line)
            self.assertIn("start-dccore", line)
            self.assertIn("it asks the questions, or opens the setup page", line)
            self.assertIn("configure.py", line)

    def test_and_no_longer_to_copy_the_sample(self):
        result = self.run_check()

        self.assertNotIn("copy admin_config.py.sample", result.stdout)
        self.assertNotIn("settings.conf.sample to settings.conf", result.stdout)
        self.assertNotIn("upstream defaults", result.stdout)

    def test_it_is_still_a_failure(self):
        """The control: the tree is still refused, only the advice changed."""
        result = self.run_check()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL", result.stdout)


class TheDaemonsOwnRefusal(boot.BootCase):

    def refusal(self):
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"])
        buffer = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                self.oserve.startup(setup_page=False)
        return buffer.getvalue()

    def test_it_points_at_the_launcher_and_configure_py(self):
        out = self.refusal()

        self.assertIn("[CRITICAL] Run the launcher (start-dccore", out)
        self.assertIn("configure.py", out)
        self.assertNotIn("admin_config.py.sample", out)
        self.assertNotIn("settings.conf.sample", out)


for _name in [n for n in dir(boot.BootCase) if n.startswith("test")]:
    setattr(TheDaemonsOwnRefusal, _name, None)


class ThePlatformsNameTheirOwnLauncher(unittest.TestCase):

    def test_windows_says_bat_and_linux_says_sh(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import setup_check

        self.assertTrue(setup_check.WINDOWS.start_cmd.endswith("start-dccore.bat"))
        self.assertTrue(setup_check.LINUX.start_cmd.endswith("start-dccore.sh"))
        with io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"), encoding="utf-8") as handle:
            self.assertIn("Run {platform.start_cmd} (it asks the questions", handle.read())


if __name__ == "__main__":
    unittest.main()
