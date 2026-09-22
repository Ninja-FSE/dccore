"""#547, Proposal 1: a first-timer's whole install is "install Python,
extract, double-click the launcher".

Before this the launchers refused an unconfigured tree with "copy the
sample and fill it in", which sent the operator to a terminal for
`configure.py`, and the dashboard's one dependency was discovered at step 7
from a log line. Now the launcher runs configure.py itself when there is no
config, offers the Flask install just before starting if the dashboard is
on and Flask is missing, finds Python where the python.org installer puts
it when the PATH box was missed, and tells the operator that closing the
window stops the bot.

Executed, not grepped, wherever a shell is available: the launchers are
copied into a throwaway tree beside stub scripts that only print a marker,
so what is asserted is which of them ran, in what order, and what the
operator was told. The same pattern tests/test_legacy_config_upgrade.py
established for the legacy branch.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LINUX = os.path.join(REPO_ROOT, "scripts", "linux", "start-dccore.sh")
WINDOWS = os.path.join(REPO_ROOT, "scripts", "windows", "start-dccore.bat")
MACOS = os.path.join(REPO_ROOT, "scripts", "macos", "start-dccore.command")

# The stub declines the browser (#547, Proposal 4: `--setup-in-browser`
# answers 2, "ask here"), so these tests keep exercising the terminal path
# they were written for; the two browser tests below use the stub that
# answers 0.
CONFIGURE = ("import sys\n"
             "print('CONFIGURE-RAN', sys.argv[1:])\n"
             "if '--setup-in-browser' in sys.argv:\n"
             "    sys.exit(2)\n"
             "if '--flask' not in sys.argv:\n"
             "    open('settings.conf', 'w').write('NICKNAME = X\\n')\n")
CONFIGURE_FAILS = ("import sys\nprint('CONFIGURE-RAN')\n"
                   "sys.exit(2 if '--setup-in-browser' in sys.argv else 1)\n")
CONFIGURE_BROWSER = ("import sys\n"
                     "print('CONFIGURE-RAN', sys.argv[1:])\n"
                     "sys.exit(0 if '--setup-in-browser' in sys.argv else 1)\n")
# The page was possible (0) but the daemon could not serve it, and the plain
# call answers the questions: #617's fallback.
CONFIGURE_BROWSER_THEN_ASKS = ("import sys\n"
                               "print('CONFIGURE-RAN', sys.argv[1:])\n"
                               "if not sys.argv[1:]:\n"
                               "    open('settings.conf', 'w').write('NICKNAME = X\\n')\n")
CHECK = "print('CHECK-RAN')\n"
CHECK_FAILS = "print('CHECK-RAN')\nraise SystemExit(1)\n"
OSERVE = "print('OSERVE-RAN')\n"
# oserve.EXIT_SETUP_IN_THE_TERMINAL: the setup page could not finish on a tree
# with no config (#617); once settings.conf exists it starts like the real one.
OSERVE_PAGE_FAILS = ("import os, sys\n"
                     "print('OSERVE-RAN')\n"
                     "if not os.path.exists('settings.conf'):\n"
                     "    print('[SETUP] Could not open the setup page - the port is taken.')\n"
                     "    sys.exit(3)\n")


class _LauncherBehaviour:
    """A mixin, not a TestCase, so unittest does not try to run it bare -
    each real launcher below mixes it in with its own way of invoking."""
    launcher = None            # set by subclasses
    check_path = None
    invoke = None

    def tree(self, files):
        directory = tempfile.mkdtemp(prefix="dccore-first-run-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        target = os.path.join(directory, *self.launcher_rel[:-1])
        os.makedirs(target)
        shutil.copy(self.launcher, target)
        for name, body in files.items():
            full = os.path.join(directory, *name.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with io.open(full, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        return directory

    def run_in(self, directory, *args):
        with io.open(os.devnull) as devnull:
            done = subprocess.run(self.invoke(args), cwd=directory, stdin=devnull,
                                  capture_output=True, text=True, timeout=90)
        return done.returncode, done.stdout + done.stderr

    def markers(self, out):
        return [line.strip() for line in out.splitlines() if "-RAN" in line]

    # --- the behaviour, shared by both launchers -------------------------

    def test_a_fresh_tree_runs_configure_then_starts(self):
        rc, out = self.run_in(self.tree({"configure.py": CONFIGURE, self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertEqual(rc, 0, out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--setup-in-browser']", "CONFIGURE-RAN []",
                                             "CONFIGURE-RAN ['--flask']", "OSERVE-RAN"])
        self.assertIn("Welcome to DCCore", out)

    def test_a_fresh_tree_offers_the_browser_first(self):
        """#547, Proposal 4: the first thing a fresh tree does is ask
        configure.py whether setup can happen in the browser; a 0 means the
        daemon is started at once to serve its own setup page, with neither
        the terminal questions nor the setup check in between - the check
        would refuse the blank tree the page exists to fill in."""
        rc, out = self.run_in(self.tree({"configure.py": CONFIGURE_BROWSER, self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertEqual(rc, 0, out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--setup-in-browser']", "OSERVE-RAN"])
        self.assertIn("Welcome to DCCore", out)

    def test_a_page_that_could_not_be_served_falls_back_to_the_questions_here(self):
        """#617: with Flask there, the browser path was the only path, and a
        taken port (another DCCore in a minimised window) ended every run in
        "exited with code 1" with the terminal questions unreachable. The
        daemon now exits 3 for that, and the launcher asks the questions,
        then checks the setup and starts, as the terminal path does."""
        rc, out = self.run_in(self.tree({"configure.py": CONFIGURE_BROWSER_THEN_ASKS,
                                         self.check_path: CHECK, "oserve.py": OSERVE_PAGE_FAILS}))

        self.assertEqual(rc, 0, out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--setup-in-browser']", "OSERVE-RAN",
                                             "CONFIGURE-RAN []", "CONFIGURE-RAN ['--flask']", "OSERVE-RAN"])
        self.assertIn("questions follow here", out)

    def test_the_fallback_still_runs_the_setup_check(self):
        """The check is silent when it passes (so it is absent above); a
        fallback that skipped it would start a bot on whatever the questions
        wrote, which is the one thing the check exists to refuse."""
        rc, out = self.run_in(self.tree({"configure.py": CONFIGURE_BROWSER_THEN_ASKS,
                                         self.check_path: CHECK_FAILS, "oserve.py": OSERVE_PAGE_FAILS}))

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--setup-in-browser']", "OSERVE-RAN",
                                             "CONFIGURE-RAN []", "CHECK-RAN"])
        self.assertIn("Setup check failed", out)

    def test_the_fallback_is_only_for_the_browser_path(self):
        """A configured tree whose daemon happens to exit 3 is not a first
        run: no questions, the exit code is reported as before."""
        rc, out = self.run_in(self.tree({"settings.conf": "x\n", "configure.py": CONFIGURE_BROWSER_THEN_ASKS,
                                         self.check_path: CHECK,
                                         "oserve.py": "print('OSERVE-RAN')\nraise SystemExit(3)\n"}))

        self.assertEqual(rc, 3, out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--flask']", "OSERVE-RAN"])
        self.assertNotIn("questions follow here", out)
        self.assertIn("exited with code 3", out)

    def test_questions_that_do_not_finish_after_the_page_failed_do_not_start_the_bot(self):
        configure = ("import sys\nprint('CONFIGURE-RAN', sys.argv[1:])\n"
                     "sys.exit(0 if '--setup-in-browser' in sys.argv else 1)\n")
        rc, out = self.run_in(self.tree({"configure.py": configure, self.check_path: CHECK,
                                         "oserve.py": OSERVE_PAGE_FAILS}))

        self.assertNotEqual(rc, 0)
        self.assertIn("did not finish", out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--setup-in-browser']", "OSERVE-RAN",
                                             "CONFIGURE-RAN []"])

    def test_setup_that_does_not_finish_does_not_start_the_bot(self):
        rc, out = self.run_in(self.tree({"configure.py": CONFIGURE_FAILS, self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertNotEqual(rc, 0)
        self.assertIn("did not finish", out)
        self.assertNotIn("OSERVE-RAN", out)

    def test_a_tree_without_configure_is_refused_not_started(self):
        """A broken extract. Worse than the old message would be starting on
        the defaults - that is the one thing this branch must never do."""
        rc, out = self.run_in(self.tree({self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertNotEqual(rc, 0)
        self.assertIn("Extract the download again", out)
        self.assertNotIn("OSERVE-RAN", out)

    def test_a_configured_tree_does_not_ask_the_questions_again(self):
        rc, out = self.run_in(self.tree({"settings.conf": "x\n", "configure.py": CONFIGURE,
                                         self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertEqual(rc, 0, out)
        self.assertEqual(self.markers(out), ["CONFIGURE-RAN ['--flask']", "OSERVE-RAN"])
        self.assertNotIn("Welcome to DCCore", out)

    def test_the_flask_offer_comes_after_the_check_and_before_the_start(self):
        """A failing setup check must stop everything, including the offer:
        nothing should be installed for a bot that is not going to start."""
        rc, out = self.run_in(self.tree({"settings.conf": "x\n", "configure.py": CONFIGURE,
                                         self.check_path: CHECK_FAILS, "oserve.py": OSERVE}))

        self.assertNotEqual(rc, 0)
        self.assertNotIn("--flask", out)
        self.assertNotIn("OSERVE-RAN", out)

    def test_check_mode_still_only_checks(self):
        rc, out = self.run_in(self.tree({"settings.conf": "x\n", "configure.py": CONFIGURE,
                                         self.check_path: CHECK, "oserve.py": OSERVE}), "check")

        self.assertEqual(rc, 0, out)
        self.assertNotIn("OSERVE-RAN", out)
        self.assertNotIn("CONFIGURE-RAN", out)

    def test_the_operator_is_told_closing_the_window_stops_the_bot(self):
        _rc, out = self.run_in(self.tree({"settings.conf": "x\n", "configure.py": CONFIGURE,
                                          self.check_path: CHECK, "oserve.py": OSERVE}))

        self.assertIn("stops the bot too", out)


class TheLinuxLauncher(_LauncherBehaviour, unittest.TestCase):
    launcher = LINUX
    launcher_rel = ("scripts", "linux", "start-dccore.sh")
    check_path = "scripts/linux/check-setup.py"

    @classmethod
    def setUpClass(cls):
        cls.shell = shutil.which("bash") or shutil.which("sh")
        if not cls.shell:
            raise unittest.SkipTest("no POSIX shell on PATH to run the launcher with")

    def invoke(self, args):
        return [self.shell, "scripts/linux/start-dccore.sh"] + list(args)


class TheWindowsLauncher(_LauncherBehaviour, unittest.TestCase):
    launcher = WINDOWS
    launcher_rel = ("scripts", "windows", "start-dccore.bat")
    check_path = "scripts/windows/check-setup.py"

    @classmethod
    def setUpClass(cls):
        cls.cmd = shutil.which("cmd.exe") or shutil.which("cmd")
        if not cls.cmd or os.name != "nt":
            raise unittest.SkipTest("cmd.exe is only available on Windows")

    def invoke(self, args):
        return [self.cmd, "/c", r"scripts\windows\start-dccore.bat"] + list(args)


class TheMacLauncherIsAWrapper(unittest.TestCase):
    """Finder runs a .command in Terminal on a double-click; the file only
    has to hand over to the Linux launcher, which is portable sh."""

    def read(self, path):
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_it_exists_and_execs_the_linux_launcher(self):
        body = self.read(MACOS)
        self.assertTrue(body.startswith("#!/bin/sh"))
        self.assertIn("exec /bin/sh ../linux/start-dccore.sh", body)
        self.assertIn('"$@"', body, "check mode must pass through")

    def test_it_is_executable(self):
        """A .command without the executable bit is what Finder refuses to
        run. On POSIX the checkout carries the bit and os.access() reads it;
        on Windows the filesystem has no such bit, so the answer lives in
        git's index - asked only if git is reachable (preflight's bare-runner
        pass hides host tooling on purpose), skipped otherwise. CI's Linux
        and macOS runners take the first branch, so the property is always
        checked somewhere."""
        if os.name != "nt":
            self.assertTrue(os.access(MACOS, os.X_OK))
            return
        if not shutil.which("git"):
            self.skipTest("no git on PATH to read the index mode with")
        mode = subprocess.run(["git", "ls-files", "-s", "scripts/macos/start-dccore.command"],
                              cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
        self.assertTrue(mode and mode[0] == "100755", mode)

    def test_the_linux_launcher_avoids_readlink_f(self):
        """macOS before 12.3 had no `readlink -f`; the wrapper depends on the
        Linux script staying portable."""
        code = [l for l in self.read(LINUX).splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertFalse(any("readlink -f" in l for l in code), "readlink -f is used in a code line")

    def test_the_interpreter_probe_runs_the_candidate(self):
        """A python3 that exists but is a stub (macOS's Xcode shim, the
        Microsoft Store alias) passes `command -v` and fails to import
        anything; the probe has to run it, and move on to the next."""
        body = self.read(LINUX)
        self.assertIn('"$candidate" -c "import sys"', body)
        self.assertIn("for candidate in python3 python", body)


class TheFlaskHook(unittest.TestCase):
    """configure.py --flask: silent unless the dashboard is on and Flask is
    missing, and never a reason not to start."""

    def setUp(self):
        import configure
        import defaults as config
        self.configure = configure
        self.config = config
        self._enabled = getattr(config, "WEBUI_ENABLED", False)
        self.addCleanup(setattr, config, "WEBUI_ENABLED", self._enabled)
        self._real_input = configure.input if hasattr(configure, "input") else None
        self.asked = []
        configure.input = lambda prompt: self.asked.append(prompt) or "n"
        self.addCleanup(self._restore_input)

    def _restore_input(self):
        if self._real_input is None:
            del self.configure.input
        else:
            self.configure.input = self._real_input

    def run_hook(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.configure.offer_flask_if_the_dashboard_is_on()
        return rc, out.getvalue()

    def test_dashboard_off_says_nothing(self):
        self.config.WEBUI_ENABLED = False
        rc, out = self.run_hook()
        self.assertEqual((rc, out, self.asked), (0, "", []))

    def test_dashboard_on_and_flask_present_says_nothing(self):
        self.config.WEBUI_ENABLED = True
        real = sys.modules.get("flask")
        if real is None:
            self.skipTest("Flask is not installed here")
        rc, out = self.run_hook()
        self.assertEqual((rc, out, self.asked), (0, "", []))

    def test_dashboard_on_and_flask_missing_makes_the_offer(self):
        self.config.WEBUI_ENABLED = True
        real = sys.modules.get("flask")
        sys.modules["flask"] = None                    # import raises ImportError
        self.addCleanup(self._put_back, real)

        rc, out = self.run_hook()

        self.assertEqual(rc, 0, "a declined install is not a reason to stop the start")
        self.assertIn("dashboard is enabled", out)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("pip install -r requirements-web.txt", self.asked[0])

    def _put_back(self, real):
        if real is None:
            sys.modules.pop("flask", None)
        else:
            sys.modules["flask"] = real

    def test_the_flag_is_wired_in_configure_main(self):
        with io.open(os.path.join(REPO_ROOT, "configure.py"), encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn('if "--flask" in sys.argv[1:]:', body)
        self.assertIn("offer_flask_if_the_dashboard_is_on()", body)


if __name__ == "__main__":
    unittest.main()
