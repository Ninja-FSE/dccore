"""The upgrade path from a pre-#170 install has to survive its own launcher.

#170 renamed two files. defaults.py was TRACKED, so `git pull` renamed it on
every operator's disk. local_config.py -> admin_config.py was NOT: that file is
gitignored, so it was never in the repository for git to rename, and an
upgrading install keeps its old name. defaults.migrate_local_config_to_admin_config()
handles that at import time.

The launchers defeated it. Their "no config found" test runs in shell, BEFORE
any Python is imported, so it fired before the migration could - and the advice
it printed ("copy admin_config.py.sample to admin_config.py") is exactly the
condition that makes the migration skip for good, because it only acts when
admin_config.py does NOT already exist. Reproduced against the real v1.10.0 tag:
the operator followed the instruction and ended with an empty sample config,
their real settings stranded in local_config.py, NICKNAME None, and the daemon
still refusing to boot.

scripts/setup_check.py carried the same test, and the launcher refuses to start
whenever the check fails - so the check had to stop failing too. It does not
merely report differently: by the time anyone reads its output the rename has
already happened, because the check imports defaults, and importing defaults IS
the migration. So it reports what was done rather than asking for anything.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LINUX = os.path.join(REPO_ROOT, "scripts", "linux", "start-dccore.sh")
WINDOWS = os.path.join(REPO_ROOT, "scripts", "windows", "start-dccore.bat")
CHECK = os.path.join(REPO_ROOT, "scripts", "setup_check.py")

LEGACY = "local_config.py"
CURRENT = "admin_config.py"


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class BothLaunchersRecogniseAPreRenameInstall(unittest.TestCase):
    """The shell half. These run before Python exists, so the migration cannot
    have happened yet and the message is all the operator has."""

    def test_each_launcher_tests_for_the_old_filename(self):
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                self.assertIn(LEGACY, read(path),
                              "the launcher cannot tell an upgrade from a fresh install")

    def test_the_legacy_branch_comes_before_the_generic_refusal(self):
        """Order is the whole fix. Both branches match an upgrading install -
        it has neither admin_config.py nor settings.conf - so whichever is
        tested first decides what the operator is told."""
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                body = read(path)
                self.assertLess(body.index(LEGACY), body.index("Copy admin_config.py.sample"),
                                "the generic 'copy the sample' advice is reached first")

    def test_neither_launcher_tells_an_upgrading_operator_to_copy_the_sample(self):
        """That single instruction is what stranded the config: creating
        admin_config.py is the exact condition the migration skips on."""
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                body = read(path)
                start = body.index(LEGACY)
                window = body[start:start + 900]

                self.assertNotIn("admin_config.py.sample to admin_config.py", window,
                                 "the legacy branch still sends them to the sample")

    def test_the_legacy_branch_says_what_to_do_instead(self):
        """A refusal that does not name the remedy is how somebody guesses, and
        the obvious guess here is the destructive one."""
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                body = read(path)
                window = body[body.index(LEGACY):][:900]

                self.assertIn("oserve.py", window,
                              "nothing tells them starting the daemon migrates it")

    def test_the_generic_refusal_still_exists_for_a_genuinely_fresh_install(self):
        """The control. A launcher that stopped refusing an unconfigured
        install would be a worse bug than the one being fixed - it would let a
        bot start on the upstream defaults."""
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                self.assertIn("admin_config.py.sample to admin_config.py", read(path))

    def test_no_launcher_still_names_the_pre_rename_config_module(self):
        """Both messages said "the defaults in config.py", which #170 renamed."""
        for path in (LINUX, WINDOWS):
            with self.subTest(launcher=os.path.basename(path)):
                self.assertNotIn("defaults in config.py", read(path))


class TheLauncherActuallyTakesThatBranch(unittest.TestCase):
    """The tests above read the launcher as text, and every one of them still
    passes with the branch disabled - `if false; then` leaves the message in the
    file, and a string being present proves nothing about it being reachable.
    That is the exact defect class this project's own audit found throughout its
    suite, so the branch has to be executed rather than grepped.

    Probes for a shell rather than assuming one: CI runs on both platforms and
    the Windows runner has no POSIX sh on PATH by default. Skipping where the
    tool is absent is this repo's established pattern - asserting it universally
    is what breaks a runner nobody was thinking about.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.bash = shutil.which("bash") or shutil.which("sh")
        if not cls.bash:
            raise unittest.SkipTest("no POSIX shell on PATH to run the launcher with")

    def run_launcher(self, files):
        """Run the real launcher in a throwaway tree holding `files`."""
        import subprocess, tempfile, shutil
        directory = tempfile.mkdtemp(prefix="dccore-launcher-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        os.makedirs(os.path.join(directory, "scripts", "linux"))
        shutil.copy(LINUX, os.path.join(directory, "scripts", "linux"))
        for name, body in files.items():
            with io.open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(body)
        done = subprocess.run([self.bash, os.path.join("scripts", "linux", "start-dccore.sh")],
                              cwd=directory, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

    def test_an_upgrading_install_is_told_to_start_the_daemon(self):
        out = self.run_launcher({LEGACY: "NICKNAME = 'MyBot'\n"})

        self.assertIn("oserve.py", out)
        self.assertIn(LEGACY, out)

    def test_an_upgrading_install_is_NOT_told_to_copy_the_sample(self):
        """The instruction that stranded the config. This is the assertion the
        text-only version could not make honestly."""
        out = self.run_launcher({LEGACY: "NICKNAME = 'MyBot'\n"})

        self.assertNotIn("admin_config.py.sample to admin_config.py", out)

    def test_a_genuinely_fresh_install_still_gets_the_sample_instruction(self):
        """The control, executed rather than grepped."""
        out = self.run_launcher({})

        self.assertIn("admin_config.py.sample", out)
        self.assertNotIn(LEGACY, out)


class TheWindowsLauncherTakesThatBranchToo(unittest.TestCase):
    """Windows is where the native port is headed, so its launcher gets the
    same executed test rather than the text one - which, as the class above
    records, passes with the branch disabled.

    Probes for cmd.exe and skips elsewhere. Note the batch file ends its
    refusal with `pause`, so stdin must be closed or the test would hang
    forever rather than fail.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.cmd = shutil.which("cmd.exe") or shutil.which("cmd")
        if not cls.cmd or os.name != "nt":
            raise unittest.SkipTest("cmd.exe is only available on Windows")

    def run_launcher(self, files):
        import subprocess, tempfile, shutil
        directory = tempfile.mkdtemp(prefix="dccore-bat-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        os.makedirs(os.path.join(directory, "scripts", "windows"))
        shutil.copy(WINDOWS, os.path.join(directory, "scripts", "windows"))
        for name, body in files.items():
            with io.open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(body)
        with io.open(os.devnull) as devnull:
            done = subprocess.run(
                [self.cmd, "/c", os.path.join("scripts", "windows", "start-dccore.bat")],
                cwd=directory, stdin=devnull, capture_output=True, text=True, timeout=60)
        return done.stdout + done.stderr

    def test_an_upgrading_install_is_told_to_start_the_daemon(self):
        out = self.run_launcher({LEGACY: "NICKNAME = 'MyBot'\n"})

        self.assertIn("oserve.py", out)
        self.assertIn(LEGACY, out)

    def test_an_upgrading_install_is_NOT_told_to_copy_the_sample(self):
        out = self.run_launcher({LEGACY: "NICKNAME = 'MyBot'\n"})

        self.assertNotIn("admin_config.py.sample to admin_config.py", out)

    def test_a_genuinely_fresh_install_still_gets_the_sample_instruction(self):
        out = self.run_launcher({})

        self.assertIn("admin_config.py.sample", out)


class TheSetupCheckDoesNotFailAnUpgrade(unittest.TestCase):
    """The launcher refuses to start whenever this check fails, so a FAIL here
    is as blocking as the launcher's own refusal."""

    def source(self):
        return read(CHECK)

    def test_it_recognises_the_old_filename(self):
        self.assertIn(LEGACY, self.source())

    def test_the_legacy_case_is_not_a_failure(self):
        """It reports; it does not refuse. The rename has already happened by
        the time this line runs, because importing defaults performs it."""
        body = self.source()
        start = body.index('legacy_present = ')
        window = body[start:start + 1600]
        branch = window[window.index("and legacy_present"):]
        branch = branch[:branch.index("elif")]

        self.assertIn("ok(", branch, "the upgrade path is still reported as a failure")
        self.assertNotIn("fail(", branch)

    def test_the_generic_unconfigured_case_still_fails(self):
        """The control, again: an install with no config at all must still be
        refused, or it starts on somebody else's nickname and channels."""
        body = self.source()
        window = body[body.index('legacy_present = '):][:2000]

        self.assertIn('fail("no admin_config.py and no settings.conf', window)


class TheMigrationItselfStillHoldsTheLineOnOverwriting(unittest.TestCase):
    """The launcher fix only matters because the migration refuses to clobber.
    If that ever changed, the old advice would become merely useless rather
    than destructive - and this test would be the one to notice."""

    def test_an_existing_admin_config_is_never_overwritten(self):
        import tempfile, shutil
        import settings_file  # noqa: F401  - imported for the module path only
        import defaults

        directory = tempfile.mkdtemp(prefix="dccore-legacy-upgrade-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        for name, body in ((LEGACY, "NICKNAME = 'OLD'\n"), (CURRENT, "NICKNAME = 'NEW'\n")):
            with io.open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(body)

        moved = defaults._migrate_local_config_to_admin_config(
            directory=directory, log=lambda *_: None)

        self.assertFalse(moved)
        with io.open(os.path.join(directory, CURRENT), encoding="utf-8") as handle:
            self.assertIn("NEW", handle.read())


if __name__ == "__main__":
    unittest.main()
