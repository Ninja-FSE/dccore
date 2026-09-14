"""Defects in the files no audit lens was pointed at.

Six lenses covered 21 of the 25 top-level modules and all of web/app.js.
Nobody was assigned theme.py, stats_mgr.py, on_connect.py,
omenserve_import.py, web/index.html, web/style.css, or any of scripts/ - and
the launchers under scripts/ are exactly the sort of thing a Python-shaped
audit walks past.

Two real defects came out of reading them.
"""

import io
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import on_connect  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheWindowsLauncherReportsTheCheckResult(unittest.TestCase):
    """`start-dccore.bat check` is the documented Windows pre-flight, named in
    README.md, docs/INSTALL.md and docs/WINDOWS.md.

    It printed "FAIL ..." and "1 problem(s) - fix these before starting", and
    then exited 0. Any wrapper, scheduled task or CI step gating on the exit
    code treated a broken config as verified.
    """

    BAT = os.path.join(REPO_ROOT, "scripts", "windows", "start-dccore.bat")

    def test_the_launcher_does_not_read_errorlevel_inside_a_block(self):
        """The source guard, which is the half that runs everywhere - the
        daemon's own home is a Linux LXC, and this file is only executable on
        Windows."""
        with io.open(self.BAT, encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn("    exit /b %errorlevel%", source)
        self.assertIn("goto :run_check", source)

    def test_the_linux_twin_still_propagates_it(self):
        """The two launchers exist to behave identically. This is the thing
        they had drifted on, so both halves are pinned."""
        with io.open(os.path.join(REPO_ROOT, "scripts", "linux",
                                  "start-dccore.sh"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("exit $?", source)

    def test_cmd_really_does_lose_it_inside_a_block(self):
        """The reasoning, executed rather than asserted.

        cmd.exe expands %errorlevel% when it PARSES a parenthesised block,
        before anything inside has run. Without this, the source guard above
        is a rule nobody can check - and a future reader would be entitled to
        think the parenthesised form was fine.

        Skipped where there is no cmd.exe, per this project's rule about
        environment-dependent preconditions: probe the hazard, do not assert
        it universally.
        """
        if os.name != "nt":
            self.skipTest("cmd.exe is a Windows shell")

        import tempfile

        workdir = tempfile.mkdtemp()
        block = os.path.join(workdir, "block.bat")
        goto = os.path.join(workdir, "goto.bat")
        with io.open(block, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write('@echo off\r\nif "%~1"=="go" (\r\n'
                         '    cmd /c exit /b 7\r\n    exit /b %errorlevel%\r\n)\r\n')
        with io.open(goto, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write('@echo off\r\nif "%~1"=="go" goto :run\r\ngoto :eof\r\n'
                         ':run\r\ncmd /c exit /b 7\r\nexit /b %errorlevel%\r\n')

        block_rc = subprocess.run([block, "go"], shell=True,
                                  capture_output=True).returncode
        goto_rc = subprocess.run([goto, "go"], shell=True,
                                 capture_output=True).returncode

        self.assertEqual(goto_rc, 7, "the form the launcher now uses lost the code")
        self.assertNotEqual(block_rc, 7,
                            "the parenthesised form kept the exit code, so the "
                            "reason this fix exists no longer holds - re-check "
                            "before simplifying the launcher back")


class AValidationErrorNamesTheLineTheOperatorTyped(DCCoreTestCase):
    """`problems()` reports a fault by POSITION, deliberately - the text may be
    a password and must not be echoed back - so the number is the only handle
    the operator has for finding the line.

    `save()` stripped blank lines and then numbered the filtered list, while
    the dashboard sends a textarea split with `splitlines()` and no filtering.
    The two therefore disagreed by the count of preceding blanks.
    """

    LONG = "PRIVMSG X@channels.undernet.org :LOGIN " + ("x" * 520)

    def test_the_position_counts_blank_lines_the_operator_can_see(self):
        """The ordinary shape of the block this feature exists for: a login, a
        blank separator, a mode line, then the offending one."""
        typed = ["PRIVMSG X :LOGIN me secret", "", "MODE %nick% +x", self.LONG]

        with self.assertRaises(ValueError) as raised:
            on_connect.save(typed, 2)

        self.assertIn("command 4", str(raised.exception))
        self.assertNotIn("command 3", str(raised.exception))

    def test_a_blank_line_is_not_itself_reported_as_a_fault(self):
        """save() strips them on purpose: in a pasted block a blank line is
        formatting, not a command."""
        on_connect.save(["MODE %nick% +x", "", "JOIN #somewhere"], 2)

        commands, _delay = on_connect.load()
        self.assertEqual(commands, ["MODE %nick% +x", "JOIN #somewhere"])

    def test_a_blank_is_still_a_fault_when_the_caller_wants_one(self):
        """problems() is the validator for a raw list, and keeps its own
        behaviour - only save() opts out."""
        found = on_connect.problems(["MODE %nick% +x", ""], 2)

        self.assertTrue(any("blank" in line for line in found))

    def test_no_blank_lines_still_numbers_from_one(self):
        typed = ["MODE %nick% +x", self.LONG]

        with self.assertRaises(ValueError) as raised:
            on_connect.save(typed, 2)

        self.assertIn("command 2", str(raised.exception))

    def test_the_password_is_still_never_in_the_message(self):
        """The reason positions are used at all. Widening what problems()
        sees must not widen what it says."""
        typed = ["PRIVMSG X :LOGIN myname hunter2", "", self.LONG]

        with self.assertRaises(ValueError) as raised:
            on_connect.save(typed, 2)

        self.assertNotIn("hunter2", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
