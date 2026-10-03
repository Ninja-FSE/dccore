"""The launchers find a set-up install in conf/ as well as at the root (#959).

The daemon moves settings.conf and admin_config.py into conf/ the first time
it starts. Each launcher decides "first run, ask the setup questions" by
looking for those files before any Python runs - and looked at the root only,
so the SECOND start of an upgraded install, an unattended one after a reboot
included, went back to first-run setup. A unit test of the migration cannot
see this, since the check runs first; these read each launcher's own check,
and run the shell one where bash is available.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SHELL_LAUNCHERS = ("scripts/linux/start-dccore.sh", "scripts/linux/install-autostart.sh",
                   "scripts/macos/install-autostart.command")


def read(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read().replace("\r\n", "\n")


def first_run_condition(text):
    """The `if [ ! -f "admin_config.py" ] ...; then` line, continuations joined."""
    start = text.index('if [ ! -f "admin_config.py" ]')
    end = text.index("; then", start) + len("; then")
    return text[start:end].replace("\\\n", " ")


class EachLauncherLooksInConf(unittest.TestCase):

    def test_the_shell_launchers(self):
        for relative in SHELL_LAUNCHERS:
            with self.subTest(launcher=relative):
                condition = first_run_condition(read(relative))
                for wanted in ('! -f "settings.conf"', '! -f "admin_config.py"',
                               '! -f "conf/settings.conf"', '! -f "conf/admin_config.py"'):
                    self.assertIn(wanted, condition)

    def test_the_windows_launcher(self):
        lines = [line for line in read("scripts/windows/start-dccore.bat").split("\n")
                 if not line.lstrip().lower().startswith("rem")
                 and 'if not exist "settings.conf"' in line]
        self.assertEqual(len(lines), 3, "the local_config, configure and no-configure branches")
        for line in lines:
            with self.subTest(line=line):
                self.assertIn('if not exist "conf\\settings.conf"', line)
                self.assertIn('if not exist "conf\\admin_config.py"', line)

    def test_the_windows_autostart_installer(self):
        """#1084: it kept the root-only check after #983, so once the first
        start had moved the files into conf\\ it refused every install."""
        lines = [line for line in read("scripts/windows/install-autostart.bat").split("\n")
                 if not line.lstrip().lower().startswith("rem")
                 and 'if not exist "settings.conf"' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('if not exist "conf\\settings.conf"', lines[0])
        self.assertIn('if not exist "conf\\admin_config.py"', lines[0])

    def test_the_setup_check(self):
        text = read("scripts/setup_check.py")
        self.assertIn('admin_config_present = any(os.path.exists(os.path.join(REPO, *where, "admin_config.py"))\n'
                      '                               for where in ((), ("conf",)))', text)


def bash_runs_in_a_folder():
    """Whether a `bash` here can cd into a temp folder and run a line there.
    Found is not enough: a Windows runner's `bash` can be WSL's launcher with
    no Linux installed, which fails every command and says nothing."""
    if not shutil.which("bash"):
        return False
    folder = tempfile.mkdtemp(prefix="dccore-bash-probe-")
    try:
        done = subprocess.run(["bash", "-c", 'cd "$1" && echo ok', "bash", folder],
                              capture_output=True, text=True, timeout=30)
        return done.returncode == 0 and done.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@unittest.skipUnless(bash_runs_in_a_folder(), "no bash here that can run in a folder; "
                     "the Linux and macOS runners run this, and the text checks above run everywhere")
class TheShellCheckDecides(unittest.TestCase):

    def decides(self, *present):
        """What the launcher's own condition says in a tree holding `present`."""
        tree = tempfile.mkdtemp(prefix="dccore-launcher-")
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        for relative in present:
            os.makedirs(os.path.dirname(os.path.join(tree, relative)) or tree, exist_ok=True)
            open(os.path.join(tree, relative), "w").close()
        condition = first_run_condition(read("scripts/linux/start-dccore.sh"))
        script = f'cd "$1" || exit 2\n{condition} echo first-run; else echo set-up; fi\n'
        done = subprocess.run(["bash", "-c", script, "bash", tree], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_nothing_anywhere_is_a_first_run(self):
        self.assertEqual(self.decides(), "first-run")

    def test_moved_into_conf_is_set_up(self):
        self.assertEqual(self.decides("conf/settings.conf"), "set-up")
        self.assertEqual(self.decides("conf/admin_config.py"), "set-up")

    def test_not_yet_moved_is_set_up(self):
        self.assertEqual(self.decides("settings.conf"), "set-up")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.name == "nt", "runs the real batch file under cmd.exe; the source check above runs everywhere")
class TheWindowsAutostartInstallerForReal(unittest.TestCase):
    """install-autostart.bat run under cmd.exe in a throwaway tree, with
    schtasks and powershell replaced by stand-ins on PATH that only note
    they were called - nothing is scheduled on this machine."""

    def run_it(self, config_where):
        tree = tempfile.mkdtemp(prefix="dccore-autostart-")
        self.addCleanup(shutil.rmtree, tree, True)
        windows = os.path.join(tree, "scripts", "windows")
        os.makedirs(windows)
        shutil.copy(os.path.join(REPO_ROOT, "scripts", "windows", "install-autostart.bat"), windows)
        if config_where is not None:
            folder = os.path.join(tree, config_where) if config_where else tree
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "settings.conf"), "w") as handle:
                handle.write("NICKNAME = SomeBot\n")
        fakes = os.path.join(tree, "fakes")
        os.makedirs(fakes)
        called = os.path.join(tree, "called.txt")
        for name in ("schtasks", "powershell"):
            with open(os.path.join(fakes, name + ".bat"), "w") as handle:
                handle.write("@echo " + name + ">>\"" + called + "\"\r\n@exit /b 0\r\n")
        env = dict(os.environ, PATH=fakes + os.pathsep + os.environ.get("PATH", ""))
        done = subprocess.run(["cmd", "/c", os.path.join(windows, "install-autostart.bat")],
                              input=b"\r\n\r\n", capture_output=True, env=env, timeout=60)
        made = open(called).read().split() if os.path.exists(called) else []
        return done.returncode, done.stdout.decode("utf-8", "replace"), made

    def test_config_moved_into_conf_installs_the_task(self):
        code, said, made = self.run_it("conf")
        self.assertEqual(code, 0, said)
        self.assertIn("schtasks", made)
        self.assertNotIn("not set up yet", said)

    def test_config_still_at_the_root_installs_it_too(self):
        code, said, made = self.run_it("")
        self.assertEqual(code, 0, said)
        self.assertIn("schtasks", made)

    def test_nothing_set_up_is_still_refused(self):
        code, said, made = self.run_it(None)
        self.assertEqual(code, 1)
        self.assertIn("not set up yet", said)
        self.assertEqual(made, [])
