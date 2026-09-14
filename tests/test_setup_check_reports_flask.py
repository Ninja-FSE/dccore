"""The setup check answers "will the dashboard actually start?".

FOUND IN BETA, and the shape is worth keeping in mind rather than just the
fix. The daemon was started from a real install and the dashboard was not
there. The only evidence was one line, after the bot had already connected to
IRC and joined its channels:

    [WEBUI] Flask not installed; dashboard disabled.

Flask *was* installed. The machine had two Pythons - `C:\\Python314` reached
by the `py` launcher, and a `Python313` first on PATH - and:

  * `start-dccore.bat` runs the daemon with `py -3`, falling back to `python`
  * the documented `pip install -r requirements-web.txt` follows `python`

So the package went into one interpreter and the daemon started under the
other. `check-setup` said "Ready to start" and was not wrong; it simply had
never been asked this question.

WHY THE CHECK IS THE RIGHT PLACE

It runs through the same `%PY%` / `$PY` the daemon does - the launcher invokes
both - so importing Flask *here* answers exactly the question that matters,
for exactly the interpreter that matters. A check that shelled out to `pip
list`, or read a requirements file, would have agreed with the docs and been
just as wrong.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import setup_check  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheCheckNamesTheInterpreterItLookedIn(DCCoreTestCase):

    def run_check(self, **settings):
        """The real script, in a subprocess, against a throwaway config."""
        work = tempfile.mkdtemp(prefix="dccore-check-")
        conf = os.path.join(work, "settings.conf")
        lines = ["NICKNAME = TestBot", "CHANNEL = #x", "ADMIN_NICK = admin"]
        lines += [f"{name} = {value}" for name, value in settings.items()]
        with io.open(conf, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        script = ("windows/check-setup.py" if os.name == "nt"
                  else "linux/check-setup.py")
        completed = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", script)],
            capture_output=True, text=True, errors="replace", timeout=300,
            cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
            env=dict(os.environ, DCCORE_SETTINGS_FILE=conf))
        return completed.stdout

    def test_it_reports_the_dashboard_when_the_switch_is_on(self):
        """Asserted on WHICH answer, not on the word "Flask" appearing.

        The first version checked only for "Flask" - and the off-branch says
        "Flask not needed", so disabling the check entirely left this test
        passing. Mutation caught it."""
        out = self.run_check(WEBUI_ENABLED="Yes")

        self.assertNotIn("Flask not needed", out,
                         "the check reported the dashboard as switched off "
                         "when it is on")
        self.assertTrue(
            "will start" in out or "not installed FOR THIS INTERPRETER" in out,
            "the check says nothing about whether the dashboard will come up")

    def test_it_says_nothing_is_needed_when_the_dashboard_is_off(self):
        """A warning about an optional dependency nobody asked for is the kind
        that teaches people to skip warnings."""
        out = self.run_check(WEBUI_ENABLED="No")

        self.assertIn("Flask not needed", out)

    def test_the_answer_is_about_this_interpreter(self):
        """The whole defect. `pip list` or a requirements file would agree
        with the documentation and be just as wrong."""
        out = self.run_check(WEBUI_ENABLED="Yes")

        try:
            import flask  # noqa: F401
            self.assertIn("will start", out)
        except ImportError:
            self.assertIn(sys.executable, out,
                          "the warning must name the interpreter it looked "
                          "in - that is what makes the two-Python case "
                          "diagnosable")


class TheAdviceInstallsIntoTheRightPython(unittest.TestCase):
    """A bare `pip` is what sent the package to the wrong interpreter, so the
    hint the check prints must not be a bare `pip`."""

    def test_windows_uses_the_py_launcher(self):
        """`start-dccore.bat` prefers `py -3`, so that is where the package
        has to go."""
        self.assertIn("py -3 -m pip", setup_check.WINDOWS.pip_hint)
        self.assertNotIn("pip install -r", setup_check.WINDOWS.pip_hint[:4])

    def test_linux_names_its_interpreter_too(self):
        self.assertIn("python3 -m pip", setup_check.LINUX.pip_hint)

    def test_neither_is_a_bare_pip(self):
        for platform in (setup_check.WINDOWS, setup_check.LINUX):
            with self.subTest(platform=platform.display):
                self.assertFalse(platform.pip_hint.startswith("pip "),
                                 "a bare pip follows whatever `python` "
                                 "resolves to, which is the bug")

    def test_the_launcher_still_prefers_py(self):
        """The reason the Windows hint says `py -3`. If the launcher ever
        stops preferring it, the advice needs to change with it."""
        with io.open(os.path.join(REPO_ROOT, "scripts", "windows",
                                  "start-dccore.bat"), encoding="utf-8") as handle:
            launcher = handle.read()

        self.assertIn("where py", launcher)
        self.assertIn('set "PY=py -3"', launcher)

    def test_the_docs_do_not_send_people_to_a_bare_pip(self):
        """Both guides said `pip install -r requirements-web.txt`, which is
        how the package reached the wrong interpreter in the first place."""
        for name in ("INSTALL.md", "WINDOWS.md"):
            with self.subTest(document=name):
                with io.open(os.path.join(REPO_ROOT, "docs", name),
                             encoding="utf-8") as handle:
                    text = handle.read()

                self.assertNotIn("\npip install -r requirements-web.txt", text)
                self.assertNotIn("   pip install -r requirements-web.txt", text)


if __name__ == "__main__":
    unittest.main()
