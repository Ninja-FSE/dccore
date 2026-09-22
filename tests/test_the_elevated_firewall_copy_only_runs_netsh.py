"""The elevated relaunch of allow-firewall.bat ran as the admin account, so
a per-user Python installed for a standard-user operator was not found
(audit L20, #684).

After `Start-Process -Verb RunAs` the script ran under whichever account
answered UAC and searched for Python again with that account's
%LOCALAPPDATA% and `py -3`. A standard-user operator whose parent typed the
admin password got "Python was not found - run start-dccore.bat first" on
a machine where start-dccore.bat works fine, and no rule was added. And a
folder with an apostrophe in its name (C:/Users/O'Brien/... spelled with backslashes) ended the
PowerShell string in the relaunch line early: no elevation at all.

The unelevated half now finds the interpreter and reads the ports as the
operator, and hands them to the elevated copy as arguments - `elevated
<dcc start> <dcc end> <web port> <web on> "<python.exe>"` - through
$env:, not the command line, so the copy only runs netsh and never looks
for Python; remove-firewall.bat's relaunch goes through $env: too.
"""

import io
import os
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests import test_the_small_things_that_are_the_os as things  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read()


@unittest.skipUnless(os.name == "nt" and (shutil.which("cmd.exe") or shutil.which("cmd")),
                     "runs the .bat files for real; the Windows CI job does")
class TheTwoHalves(things.TheWindowsHelpers):

    def recording_powershell(self):
        self.fake("powershell", f'echo powershell %* [%DCCORE_SELF%] [%DCCORE_ARGS%]>> "{self.calls}"\nexit /b 0\n')

    def test_the_relaunch_carries_the_operators_ports_and_interpreter(self):
        self.recorder("net", rc=2)    # not an administrator
        self.recording_powershell()
        self.recorder("netsh")

        self.run_bat("allow-firewall.bat")

        relaunch = [c for c in self.calls_made() if "-Verb RunAs" in c][0]
        self.assertIn("[elevated 55000 55010 8420 0 \"", relaunch)
        self.assertIn(os.path.basename(sys.executable).lower(), relaunch.lower())
        self.assertNotIn("netsh", "".join(self.calls_made()).replace("powershell", ""))

    def test_the_elevated_half_runs_with_no_python_at_all(self):
        """The audit's scenario: the elevated account has no Python. The
        copy is handed everything and never looks."""
        self.recorder("net")          # elevated
        self.recorder("powershell")
        self.recorder("netsh")
        cmd = shutil.which("cmd.exe") or shutil.which("cmd")
        import subprocess
        from tests.test_python_missing_help_do_not_fail import env_with
        system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        env = env_with({"PATH": self.fakebin + os.pathsep + system32, "TEMP": self.root, "TMP": self.root,
                        "LOCALAPPDATA": self.home, "ProgramFiles": self.home,
                        "ProgramW6432": self.home, "ProgramFiles(x86)": self.home})
        with io.open(os.devnull) as devnull:
            done = subprocess.run([cmd, "/c", os.path.join("scripts", "windows", "allow-firewall.bat"),
                                   "elevated", "55100", "55110", "9000", "1", r"C:\nowhere\python.exe"],
                                  cwd=self.root, stdin=devnull, capture_output=True, text=True,
                                  errors="replace", timeout=120, env=env)

        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertNotIn("Python was not found", done.stdout)
        adds = [c for c in self.calls_made() if "add rule" in c]
        self.assertEqual(len(adds), 2, self.calls_made())
        self.assertIn("localport=55100-55110", adds[0])
        self.assertIn("localport=9000", adds[1])
        self.assertFalse(any("-Verb RunAs" in c for c in self.calls_made()), "the elevated copy relaunched itself")

    def test_an_apostrophe_in_the_folder_reaches_powershell_whole(self):
        """The path goes through $env:DCCORE_SELF, never inside a quoted
        PowerShell string."""
        self.recorder("net", rc=2)
        self.recording_powershell()
        self.recorder("netsh")
        quoted = os.path.join(self.root, "O'Brien")
        shutil.copytree(os.path.join(self.root, "scripts"), os.path.join(quoted, "scripts"))
        shutil.copy(os.path.join(self.root, "settings.conf"), os.path.join(quoted, "settings.conf"))
        self.root = quoted

        self.run_bat("allow-firewall.bat")

        relaunch = [c for c in self.calls_made() if "-Verb RunAs" in c]
        self.assertEqual(len(relaunch), 1, self.calls_made())
        self.assertIn("O'Brien", relaunch[0])
        self.assertIn("[elevated 55000 55010", relaunch[0])


for _name in [n for n in dir(things.TheWindowsHelpers) if n.startswith("test")]:
    setattr(TheTwoHalves, _name, None)


class TheScriptsAreShapedSo(unittest.TestCase):
    """Read on every platform: the CI's Linux and macOS jobs cannot run
    cmd.exe."""

    def test_the_elevated_copy_takes_its_arguments_and_skips_the_search(self):
        text = read("scripts", "windows", "allow-firewall.bat")
        entry = text.index('if /i "%~1"=="elevated"')
        search = text.index("where py >nul")
        self.assertLess(entry, search)
        self.assertIn('set "PYEXE=%~6"', text)
        self.assertIn("goto :block_rule", text[entry:search])

    def test_both_relaunches_go_through_the_environment(self):
        for name in ("allow-firewall.bat", "remove-firewall.bat"):
            text = read("scripts", "windows", name)
            self.assertIn("-FilePath $env:DCCORE_SELF", text, name)
            self.assertNotIn("-FilePath '%~f0'", text, name)

    def test_the_arguments_are_the_ports_and_the_interpreter_in_order(self):
        text = read("scripts", "windows", "allow-firewall.bat")
        self.assertIn('set "DCCORE_ARGS=elevated %DCC_START% %DCC_END% %WEB_PORT% %WEB_ON% "%PYEXE%""', text)


if __name__ == "__main__":
    unittest.main()
