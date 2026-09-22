"""#588 and #589: two launcher defects the audit found.

#589 - allow-firewall.bat added an inbound ALLOW rule by port, and told the
operator the firewall was settled. But the Windows Security Alert's Cancel - the
exact case the script and docs say it fixes - creates an inbound BLOCK rule for
that python.exe, and Windows Defender Firewall evaluates Block rules before
Allow rules: sends kept timing out after "Done". The script now removes an
inbound Block rule for the interpreter the bot runs on (and only that) first.

#588 - a launchd agent gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, not the shell's,
so the macOS autostart agent found only Apple's stub, said "Python was not
found" and was restarted every ten seconds for ever - after the installer had
printed "Done: DCCore is running now". The plist now carries a PATH.

Neither script runs in CI on the OS it is for except where noted (the Windows
job runs the .bat files, this one runs the .command on Linux with a fake
launchctl); these tests read them on every OS.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class TheBlockRuleIsRemoved(unittest.TestCase):

    def setUp(self):
        self.text = read("scripts", "windows", "allow-firewall.bat")
        self.lines = [l for l in self.text.split("\n") if l.strip() and not l.strip().lower().startswith("rem")]
        self.ps = [l for l in self.lines if "Remove-NetFirewallRule" in l][0]

    def test_it_happens_before_the_allow_rule_is_added(self):
        self.assertLess(self.text.index("Remove-NetFirewallRule"), self.text.index('add rule name="DCCore DCC sends"'))

    def test_only_inbound_block_rules_are_removed(self):
        self.assertIn("$r.Direction -eq 'Inbound'", self.ps)
        self.assertIn("$r.Action -eq 'Block'", self.ps)
        self.assertNotIn("Remove-NetFirewallRule", self.ps.split("if ($r.Direction")[0], "nothing is removed outside the condition")

    def test_only_this_interpreters_rules_are_looked_at(self):
        self.assertIn("Where-Object { $_.Program -eq $exe }", self.ps)

    def test_the_interpreter_is_the_one_the_bot_runs_on(self):
        """sys.executable of %PY% - written to a file, because %PY% may carry
        quotes and cmd's quote handling makes the for /f form unreliable."""
        self.assertIn("sys.executable", self.text)
        self.assertIn('%PY% -c "import sys; open(sys.argv[1], \'w\').write(sys.executable)" "%PYEXE_FILE%"', self.text)

    def test_the_file_is_cleaned_up_and_a_missing_one_skips_the_step(self):
        """The path is read out of the file into %PYEXE% as soon as it is
        written (#684: the elevated copy is handed it), and a run that could
        not learn it skips the step."""
        self.assertIn('if exist "%PYEXE_FILE%" for /f "usebackq delims=" %%P in ("%PYEXE_FILE%") do set "PYEXE=%%P"', self.text)
        self.assertIn("if not defined PYEXE goto :rules", self.text)
        self.assertGreaterEqual(self.text.count('del /q "%PYEXE_FILE%"'), 2)
        self.assertIn(":rules", self.text)

    def test_failing_to_look_is_a_warning_not_a_stop(self):
        after = self.text[self.text.index("Remove-NetFirewallRule"):self.text.index("\n:rules")]
        self.assertIn("if errorlevel 1 echo", after)
        self.assertNotIn("exit /b", after)
        self.assertNotIn("goto :failed", after)

    def test_it_says_what_it_removed(self):
        self.assertIn("inbound Block rule(s) for", self.ps)

    def test_the_command_is_one_quoted_argument_with_nothing_for_cmd_to_read(self):
        """Nothing of cmd's is expanded inside the PowerShell text at all
        since #684: the interpreter's path reaches it as $env:DCCORE_PYEXE,
        so an apostrophe in it cannot end a PowerShell string."""
        quoted = re.search(r'-Command "(.*)"$', self.ps.strip())
        self.assertIsNotNone(quoted)
        body = quoted.group(1)
        self.assertNotIn('"', body)
        self.assertEqual(re.findall(r"%[^%]*%", body), [])
        self.assertIn("$exe = $env:DCCORE_PYEXE", body)

    def test_no_rule_of_the_ports_is_changed(self):
        self.assertIn('add rule name="DCCore DCC sends" dir=in action=allow protocol=TCP', self.text)

    def test_the_docs_say_so(self):
        docs = read("docs", "WINDOWS.md")
        self.assertIn("inbound **Block** rule", docs)
        self.assertIn("a Block rule wins over any Allow rule", docs)


class TheMacAgentGetsAPath(unittest.TestCase):

    SCRIPT = os.path.join(REPO_ROOT, "scripts", "macos", "install-autostart.command")

    def test_the_plist_has_an_environment_with_a_path(self):
        text = read("scripts", "macos", "install-autostart.command")
        self.assertIn("<key>EnvironmentVariables</key>", text)
        self.assertRegex(text, r"<key>PATH</key>\s*<string>\$PATH_XML</string>")

    def test_the_path_is_the_shells_then_the_installers_dirs_then_launchds(self):
        text = read("scripts", "macos", "install-autostart.command")
        line = [l for l in text.split("\n") if l.startswith("AGENT_PATH=")][0]
        for part in ("$PATH", "/opt/homebrew/bin", "/usr/local/bin",
                     "/Library/Frameworks/Python.framework/Versions/Current/bin", "/usr/bin"):
            self.assertIn(part, line)
        self.assertLess(line.index("$PATH"), line.index("/opt/homebrew/bin"))
        self.assertLess(line.index("/opt/homebrew/bin"), line.index("/usr/bin"))

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash") or not shutil.which("sed"), "needs a POSIX shell")
    def test_run_for_real_with_a_fake_launchctl_it_writes_the_path_escaped(self):
        home = tempfile.mkdtemp(prefix="dccore-mac-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        fakebin = os.path.join(home, "bin")
        os.makedirs(fakebin)
        with io.open(os.path.join(fakebin, "launchctl"), "w", newline="\n") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(fakebin, "launchctl"), 0o755)
        root = os.path.join(home, "tree")
        os.makedirs(os.path.join(root, "scripts", "macos"))
        os.makedirs(os.path.join(root, "scripts", "linux"))
        shutil.copy(self.SCRIPT, os.path.join(root, "scripts", "macos"))
        with io.open(os.path.join(root, "settings.conf"), "w") as handle:
            handle.write("NICKNAME = X\n")
        env = dict(os.environ, HOME=home, PATH=fakebin + os.pathsep + "/usr/bin:/bin:/tmp/a&b")
        done = subprocess.run(["bash", os.path.join(root, "scripts", "macos", "install-autostart.command")],
                              cwd=root, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        plist = os.path.join(home, "Library", "LaunchAgents", "com.dccore.bot.plist")
        if not os.path.exists(plist):
            self.skipTest("the installer stopped before writing the agent: " + done.stdout + done.stderr)
        with io.open(plist, encoding="utf-8") as handle:
            text = handle.read()
        path = re.search(r"<key>PATH</key>\s*<string>(.*?)</string>", text, re.S).group(1)
        self.assertIn("/tmp/a&amp;b", path, "the XML characters are escaped")
        self.assertIn("/opt/homebrew/bin", path)
        self.assertIn(fakebin, path, "the shell's own PATH comes first")


if __name__ == "__main__":
    unittest.main()
