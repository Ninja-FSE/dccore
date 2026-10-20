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
        checked somewhere.

        Nor is there an index to ask in a tree that is not this checkout:
        the release extraction runs the suite in a tree with no .git (#1441),
        where `git ls-files` answers nothing, or answers for some other
        repository the folder sits in. That skips, saying what git said; the
        dev repository's extraction test checks the modes it reports, on every
        OS."""
        if os.name != "nt":
            self.assertTrue(os.access(MACOS, os.X_OK))
            return
        if not shutil.which("git"):
            self.skipTest("no git on PATH to read the index mode with")

        def git(*args):
            try:
                return subprocess.run(["git"] + list(args), cwd=REPO_ROOT, capture_output=True,
                                      encoding="utf-8", errors="replace", timeout=30)
            except (OSError, subprocess.TimeoutExpired) as err:
                self.skipTest(f"git could not run to read the index mode: {err}")

        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            self.skipTest("no git work tree here (an extracted tree has no .git), so no index mode"
                          f" to read; git said: {(top.stderr or '').strip() or 'nothing'}")
        try:
            ours = os.path.samefile(top.stdout.strip(), REPO_ROOT)
        except OSError:
            ours = False
        if not ours:
            self.skipTest(f"git's work tree here is {top.stdout.strip()!r}, not this tree,"
                          " so its index says nothing about it")
        mode = git("ls-files", "-s", "scripts/macos/start-dccore.command").stdout.split()
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


class _Over:
    """A module with some names replaced: everything else is the real one."""

    def __init__(self, real, **replaced):
        self._real = real
        self.__dict__.update(replaced)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TheExecutableCheckKnowsWhoseIndexItReads(unittest.TestCase):
    """#1441: test_it_is_executable, run with git and the OS faked, so its
    guard is evaluated on every runner and not only on a Windows one.

    On Windows it asks git's index; in the release extraction's tree there
    is no .git, `git ls-files` answers nothing, and the check used to fail
    the whole release run. It must skip there, saying why - and still be a
    real check in a checkout.

    The fake decodes git's bytes the way subprocess.run would with the
    arguments it was given, and a bare text=True as a Greek Windows box
    does (cp1253), so the encoding of the call is part of what is tested."""

    LAUNCHER_MODE = "100755 0123456789abcdef0123456789abcdef01234567 0\tscripts/macos/start-dccore.command\n"

    @staticmethod
    def decoded(text, kwargs):
        raw = text.encode("utf-8")
        if kwargs.get("encoding") or kwargs.get("errors"):
            return raw.decode(kwargs.get("encoding") or "cp1253", kwargs.get("errors") or "strict")
        if kwargs.get("text") or kwargs.get("universal_newlines"):
            return raw.decode("cp1253")
        return raw

    def outcome(self, os_name="nt", toplevel=None, toplevel_code=0, listed="", git="git-placeholder",
                executable=True, stderr="", raises=None, root=None):
        calls = []
        self.timeouts = []

        def run(argv, **kwargs):
            calls.append(argv[1])
            self.timeouts.append(kwargs.get("timeout"))
            if raises is not None:
                raise raises
            if argv[1] == "rev-parse":
                return subprocess.CompletedProcess(argv, toplevel_code, self.decoded(toplevel or "", kwargs),
                                                   self.decoded(stderr, kwargs))
            if argv[1] == "ls-files":
                return subprocess.CompletedProcess(argv, 0, self.decoded(listed, kwargs), self.decoded("", kwargs))
            raise AssertionError(f"unexpected command {argv}")

        module = sys.modules[__name__]
        saved = (module.os, module.shutil, module.subprocess, module.REPO_ROOT)
        module.os = _Over(os, name=os_name, access=lambda path, mode: executable)
        module.shutil = _Over(shutil, which=lambda name: git if name == "git" else None)
        module.subprocess = _Over(subprocess, run=run)
        module.REPO_ROOT = root or REPO_ROOT
        try:
            result = unittest.TestResult()
            TheMacLauncherIsAWrapper("test_it_is_executable").run(result)
        finally:
            module.os, module.shutil, module.subprocess, module.REPO_ROOT = saved
        if result.errors:
            self.fail(result.errors[0][1])
        said = "skip" if result.skipped else "fail" if result.failures else "pass"
        self.reason = result.skipped[0][1] if result.skipped else None
        return said, calls

    def ours(self):
        """REPO_ROOT as git prints it on Windows: forward slashes, a newline."""
        return REPO_ROOT.replace(os.sep, "/") + "\n"

    def test_an_extracted_tree_with_no_git_skips(self):
        """`git rev-parse` fails where there is no .git: a skip, not the
        failure on empty `ls-files` output it was."""
        said, calls = self.outcome(toplevel_code=128, listed="")
        self.assertEqual(said, "skip")
        self.assertNotIn("ls-files", calls)

    def test_a_tree_inside_some_other_repository_skips(self):
        """The extraction sits in a folder another checkout owns: that
        index is not this tree's."""
        elsewhere = os.path.dirname(REPO_ROOT).replace(os.sep, "/") + "\n"
        said, calls = self.outcome(toplevel=elsewhere, listed="")
        self.assertEqual(said, "skip")
        self.assertNotIn("ls-files", calls)

    def test_a_toplevel_that_is_not_there_skips(self):
        """samefile() raising - a path that does not exist - is not ours."""
        gone = os.path.join(REPO_ROOT, "no-such-folder-1441").replace(os.sep, "/") + "\n"
        said, _calls = self.outcome(toplevel=gone, listed="")
        self.assertEqual(said, "skip")

    def test_this_checkout_still_reads_the_index_mode(self):
        said, calls = self.outcome(toplevel=self.ours(), listed=self.LAUNCHER_MODE)
        self.assertEqual(said, "pass")
        self.assertEqual(calls, ["rev-parse", "ls-files"])

    def test_this_checkout_still_fails_a_launcher_that_lost_its_bit(self):
        """The guard did not turn the check into a skip: 100644 in this
        checkout's own index is still red."""
        said, _calls = self.outcome(toplevel=self.ours(), listed=self.LAUNCHER_MODE.replace("100755", "100644"))
        self.assertEqual(said, "fail")

    def test_this_checkout_fails_when_the_index_says_nothing(self):
        said, _calls = self.outcome(toplevel=self.ours(), listed="")
        self.assertEqual(said, "fail")

    def test_no_git_still_skips(self):
        said, calls = self.outcome(git=None)
        self.assertEqual((said, calls), ("skip", []))

    def test_posix_reads_the_bit_and_never_asks_git(self):
        self.assertEqual(self.outcome(os_name="posix", executable=True), ("pass", []))
        self.assertEqual(self.outcome(os_name="posix", executable=False), ("fail", []))

    def test_the_skip_says_what_git_said(self):
        said, _calls = self.outcome(toplevel_code=128,
                                    stderr="fatal: not a git repository (or any of the parent directories): .git\n")
        self.assertEqual(said, "skip")
        self.assertIn("fatal: not a git repository", self.reason)

    def test_the_skip_names_the_work_tree_git_reported(self):
        elsewhere = os.path.dirname(REPO_ROOT).replace(os.sep, "/")
        said, _calls = self.outcome(toplevel=elsewhere + "\n")
        self.assertEqual(said, "skip")
        self.assertIn(elsewhere, self.reason)

    def test_git_that_hangs_or_cannot_start_skips(self):
        """A skip with the reason, not an error out of the suite."""
        for error in (subprocess.TimeoutExpired(["git", "rev-parse"], 30), OSError("placeholder: cannot start git")):
            with self.subTest(error=type(error).__name__):
                said, _calls = self.outcome(raises=error)
                self.assertEqual(said, "skip")
                self.assertIn(str(error), self.reason)

    def test_every_git_call_has_a_timeout(self):
        self.outcome(toplevel=self.ours(), listed=self.LAUNCHER_MODE)
        self.assertEqual(len(self.timeouts), 2)
        self.assertNotIn(None, self.timeouts)

    def test_a_checkout_whose_path_is_not_ascii_is_still_checked(self):
        """git prints the toplevel in UTF-8. Decoded in the console's code
        page instead, it names no folder, and the real check would skip in
        every such checkout without saying why."""
        try:
            root = tempfile.mkdtemp(prefix="dccore-δοκιμή-")
        except (OSError, UnicodeError) as err:
            self.skipTest(f"this filesystem cannot hold a non-ASCII folder name: {err}")
        self.addCleanup(shutil.rmtree, root, True)
        said, calls = self.outcome(toplevel=root.replace(os.sep, "/") + "\n", listed=self.LAUNCHER_MODE, root=root)
        self.assertEqual((said, calls), ("pass", ["rev-parse", "ls-files"]), self.reason)


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
