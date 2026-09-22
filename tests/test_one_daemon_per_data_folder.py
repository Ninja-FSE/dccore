"""Windows autostart ran an interactive launcher with no single-instance
guard, so a manual start plus the logon task ran two bots on one data
folder (audit L46, #710).

Nothing checked for an already-running instance - not the daemon, not the
launchers. A logon-task start plus a double-click (or a second logon
session of the same account) ran two bots on one data folder: the second
took ALT_NICKNAME, both wrote dcc_queue.txt and stats.txt whole, and their
DCC listeners shared the eleven-port range. And with the dashboard on and
Flask missing, the task's launcher window stopped at configure.py's
input() until somebody answered it.

oserve.startup() takes an OS lock on data/dccore.lock before it reads or
writes anything, and a second instance is refused with a message and its
own exit code, which the launcher names. The lock dies with its process,
so a crash leaves nothing stale. The task passes `autostart`, under which
the Flask offer prints its command instead of asking.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import platform_compat  # noqa: E402

from tests import test_startup as boot  # noqa: E402


HOLD = (
    "import sys, time; sys.path.insert(0, %r); import platform_compat\n"
    "platform_compat.take_instance_lock(%r)\n"
    "print('held', flush=True)\n"
    "sys.stdin.readline()\n"
)


class TheLock(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dccore-lock-")
        self.path = os.path.join(self.dir, "dccore.lock")
        self.addCleanup(platform_compat.release_instance_lock)

    def other_process_holding(self):
        child = subprocess.Popen([sys.executable, "-c", HOLD % (REPO_ROOT, self.path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait, 10)
        self.addCleanup(lambda: child.stdin.close() if not child.stdin.closed else None)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_a_second_process_is_refused_and_told_who_holds_it(self):
        child = self.other_process_holding()

        with self.assertRaises(platform_compat.AlreadyRunning) as caught:
            platform_compat.take_instance_lock(self.path)

        self.assertEqual(caught.exception.pid, child.pid)

    def test_the_lock_dies_with_its_process(self):
        """A crash or a power cut leaves the file, not the lock."""
        child = self.other_process_holding()
        child.stdin.close()
        child.wait(10)

        handle = platform_compat.take_instance_lock(self.path)
        self.assertIsNotNone(handle)

    def test_the_same_process_may_take_it_twice(self):
        first = platform_compat.take_instance_lock(self.path)
        second = platform_compat.take_instance_lock(self.path)

        self.assertIs(first, second)

    def test_released_it_can_be_taken_by_another(self):
        platform_compat.take_instance_lock(self.path)
        platform_compat.release_instance_lock()

        child = self.other_process_holding()
        self.assertIsNotNone(child)


class TheDaemonRefusesASecondCopy(boot.BootCase):

    def test_startup_exits_with_its_own_code_and_says_so(self):
        lock_path = os.path.join(os.path.dirname(os.path.abspath(config.DCC_QUEUE_FILE)), "dccore.lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        child = subprocess.Popen([sys.executable, "-c", HOLD % (REPO_ROOT, lock_path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "held")

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            self.oserve.startup(setup_page=False)

        self.assertEqual(caught.exception.code, self.oserve.EXIT_ALREADY_RUNNING)
        self.assertIn("DCCore is already running on this folder (pid %d)" % child.pid, out.getvalue())
        self.assertIn("Stop the other one first", out.getvalue())

    def test_a_first_start_takes_the_lock_beside_the_queue_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.boot(setup_page=False)

        lock_path = os.path.join(os.path.dirname(os.path.abspath(config.DCC_QUEUE_FILE)), "dccore.lock")
        self.assertEqual(platform_compat._instance_lock["path"], lock_path)
        with io.open(lock_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), str(os.getpid()))


for _name in [n for n in dir(boot.BootCase) if n.startswith("test")]:
    setattr(TheDaemonRefusesASecondCopy, _name, None)


class TheLaunchersKnow(unittest.TestCase):

    def read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_the_task_passes_autostart_and_the_launcher_reads_it(self):
        self.assertIn('/tr "\\"%~dp0start-dccore.bat\\" autostart"', self.read("scripts", "windows", "install-autostart.bat"))
        self.assertIn('if /i "%~1"=="autostart" set "DCCORE_AUTOSTART=1"', self.read("scripts", "windows", "start-dccore.bat"))

    def test_the_launcher_names_an_already_running_bot(self):
        bat = self.read("scripts", "windows", "start-dccore.bat")
        self.assertIn('if "%RC%"=="4" (', bat)
        self.assertIn("DCCore is already running from this folder", bat)

    def test_the_flask_offer_prints_instead_of_asking_under_the_task(self):
        """Executed: with Flask made unimportable and the task's flag set,
        the offer prints the command and returns without calling input()."""
        import builtins
        import configure
        env_before = os.environ.get("DCCORE_AUTOSTART")
        os.environ["DCCORE_AUTOSTART"] = "1"
        self.addCleanup(lambda: os.environ.pop("DCCORE_AUTOSTART", None) if env_before is None
                        else os.environ.__setitem__("DCCORE_AUTOSTART", env_before))
        had_flask = sys.modules.get("flask")
        sys.modules["flask"] = None          # `import flask` raises ImportError
        self.addCleanup(lambda: sys.modules.__setitem__("flask", had_flask) if had_flask is not None
                        else sys.modules.pop("flask", None))
        asked = []
        real_input = builtins.input
        builtins.input = lambda *a, **k: asked.append(a) or "y"
        self.addCleanup(setattr, builtins, "input", real_input)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            configure.offer_to_install_web_requirements()

        self.assertEqual(asked, [], "the task's window was asked a question")
        self.assertIn("Started by the autostart task, so not asking", out.getvalue())
        self.assertIn("pip install -r requirements-web.txt", out.getvalue())

if __name__ == "__main__":
    unittest.main()
