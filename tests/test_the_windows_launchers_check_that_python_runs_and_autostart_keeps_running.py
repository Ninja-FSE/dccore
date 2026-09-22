"""#586 and #587: two launcher defects the audit found on a stock Windows.

#586 - `where python` succeeds on a stock Windows 10/11 with no Python: the
Microsoft Store's stub in %LOCALAPPDATA%\\Microsoft\\WindowsApps is on PATH by
default. The launcher committed to it, never reached its "download Python now?"
offer (the headline of #547), and every call then printed Microsoft's "Python
was not found" and exited 9009 - twice, ending in "Setup did not finish".
allow-firewall.bat had the same. Each candidate is now RUN once and only one
that answers becomes the interpreter (the Linux launcher has done this from the
start, for the same reason).

#587 - `schtasks /create` without settings gets Task Scheduler's defaults for a
maintenance job: stop after 72 hours, do not start on battery, stop when
unplugged, below-normal priority. An autostarted bot silently vanished from IRC
after three days. The settings are replaced after the task is created.

The .bat files only run on Windows; on every OS these tests read them, and the
Windows tests in test_the_launcher_is_the_install.py and
test_the_small_things_that_are_the_os.py run them for real.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WINDOWS = os.path.join(REPO_ROOT, "scripts", "windows")


def read(name):
    with io.open(os.path.join(WINDOWS, name), encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def commands(text):
    """The file without its rem lines."""
    return [line for line in text.split("\n") if line.strip() and not line.strip().lower().startswith("rem")]


class EveryLauncherRunsItsCandidate(unittest.TestCase):

    LAUNCHERS = ("start-dccore.bat", "allow-firewall.bat")

    def test_python_is_run_before_it_is_believed(self):
        for name in self.LAUNCHERS:
            lines = commands(read(name))
            py = [line for line in lines if 'set "PY=python"' in line]
            self.assertEqual(len(py), 1, name)
            self.assertIn('call python -c "import sys"', py[0], name)
            self.assertLess(py[0].index("where python"), py[0].index('call python -c'), name)
            self.assertLess(py[0].index('call python -c'), py[0].index('set "PY=python"'), name)

    def test_the_py_launcher_is_run_too(self):
        """`py` can exist with no Python behind it."""
        for name in self.LAUNCHERS:
            line = [l for l in commands(read(name)) if 'set "PY=py -3"' in l][0]
            self.assertIn('call py -3 -c "import sys"', line, name)

    def test_the_output_of_a_stub_is_not_shown(self):
        """The Store stub prints a paragraph to stderr; the probe is silent."""
        for name in self.LAUNCHERS:
            for line in commands(read(name)):
                if "-c \"import sys\"" in line:
                    self.assertIn(">nul 2>&1", line.split("-c \"import sys\"")[1], line)

    def test_a_shim_is_called_not_run(self):
        """pyenv-win's python.bat: a batch file run without `call` never returns."""
        for name in self.LAUNCHERS:
            for line in commands(read(name)):
                if "-c \"import sys\"" in line:
                    self.assertRegex(line, r"&& call (py -3|python) -c", line)

    def test_no_bare_where_python_decides_any_more(self):
        for name in self.LAUNCHERS:
            for line in commands(read(name)):
                if 'set "PY=python"' in line or 'set "PY=py -3"' in line:
                    self.assertIn("-c", line, name)

    def test_the_offer_still_follows_the_search(self):
        text = read("start-dccore.bat")
        self.assertLess(text.index('call python -c "import sys"'), text.index(":offer_python"))
        self.assertIn("if defined PY goto :have_python", text)

    def test_the_directory_fallbacks_are_unchanged(self):
        for name in self.LAUNCHERS:
            self.assertIn('Programs\\Python\\Python3*', read(name), name)


class TheAutostartTask(unittest.TestCase):

    def setUp(self):
        self.text = read("install-autostart.bat")
        self.lines = commands(self.text)
        self.powershell = [l for l in self.lines if "powershell" in l.lower() and "Set-ScheduledTask" in l]

    def test_the_settings_are_replaced_after_the_task_is_created(self):
        self.assertEqual(len(self.powershell), 1)
        self.assertLess(self.text.index("schtasks /create"), self.text.index("Set-ScheduledTask"))

    def test_it_targets_the_task_it_just_made(self):
        self.assertIn("-TaskName 'DCCore'", self.powershell[0])
        self.assertIn('/tn "DCCore"', "\n".join(self.lines))

    def test_there_is_no_time_limit(self):
        self.assertIn("-ExecutionTimeLimit ([TimeSpan]::Zero)", self.powershell[0])

    def test_battery_is_fine_to_start_and_to_keep_running_on(self):
        self.assertIn("-AllowStartIfOnBatteries", self.powershell[0])
        self.assertIn("-DontStopIfGoingOnBatteries", self.powershell[0])

    def test_it_runs_at_normal_priority(self):
        self.assertIn("-Priority 4", self.powershell[0])

    def test_it_restarts_if_it_fails(self):
        self.assertIn("-RestartCount 3", self.powershell[0])
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 1)", self.powershell[0])

    def test_it_is_called_so_a_wrapper_on_path_returns_here(self):
        self.assertTrue(self.powershell[0].strip().lower().startswith("call powershell"))

    def test_the_command_has_nothing_that_cmd_would_read_itself(self):
        """Inside the double quotes; no percent signs, no unquoted pipe."""
        command = self.powershell[0]
        self.assertNotIn("%", command)
        quoted = re.search(r'-Command "(.*)"$', command.strip())
        self.assertIsNotNone(quoted, "the whole PowerShell command is one quoted argument")
        self.assertNotIn('"', quoted.group(1))

    def test_failing_to_change_them_is_a_warning_not_a_failure(self):
        after = self.text[self.text.index("Set-ScheduledTask"):]
        warning = after[:after.index("echo   Done:")]
        self.assertIn("if errorlevel 1 (", warning)
        self.assertIn("The task was created, but", warning)
        self.assertNotIn("exit /b 1", warning)

    def test_the_success_message_is_unchanged(self):
        self.assertIn("starts the next time you log on", self.text)

    def test_the_refusal_path_is_unchanged(self):
        self.assertIn("Task Scheduler refused", self.text)


if __name__ == "__main__":
    unittest.main()
