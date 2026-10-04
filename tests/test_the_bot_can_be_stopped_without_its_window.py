"""The bot can be stopped without its window (#1065).

Closing the window or Ctrl-C in it were the only ways. Now `start-dccore stop`
(oserve.py --stop), the admin console's `shutdown now` and the dashboard's
Stop the bot all end in the same KeyboardInterrupt Ctrl-C raises. The command
asks through a file, data/dccore.stop, and the instance lock says when the bot
has really gone - no kill, which on Windows would skip the shutdown.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import platform_compat  # noqa: E402
import stopping  # noqa: E402

REPO_ROOT = support.REPO_ROOT


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="dccore-stop-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.set_config(DCC_QUEUE_FILE=os.path.join(self.dir, "dcc_queue.txt"))


class TheStopItself(Case):
    def test_it_says_why_sends_quit_and_interrupts_like_ctrl_c(self):
        sent, interrupted = [], []
        sock = types.SimpleNamespace(sendall=lambda data: sent.append(data))
        old = sys.modules.get("oserve")
        sys.modules["oserve"] = types.SimpleNamespace(irc_connection=sock)
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", old) if old
                        else sys.modules.pop("oserve", None))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            stopping.request_stop("a test asked", interrupt=lambda: interrupted.append(True))
        self.assertEqual(sent, [b"QUIT :DCCore is stopping\r\n"])
        self.assertEqual(interrupted, [True])
        self.assertIn("[STOP] Stopping the bot: a test asked.", out.getvalue())

    def test_with_no_connection_it_still_stops(self):
        interrupted = []
        old = sys.modules.get("oserve")
        sys.modules["oserve"] = types.SimpleNamespace(irc_connection=None)
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", old) if old
                        else sys.modules.pop("oserve", None))
        with mock.patch("sys.stdout", io.StringIO()):
            stopping.request_stop("x", interrupt=lambda: interrupted.append(True))
        self.assertEqual(interrupted, [True])


class TheStopFile(Case):
    def test_no_file_no_stop(self):
        stopped = []
        self.assertFalse(stopping.check_stop_file(stop=stopped.append))
        self.assertEqual(stopped, [])

    def test_a_file_stops_it_once_and_goes(self):
        with open(stopping.stop_file(), "w") as handle:
            handle.write("stop\n")
        stopped = []
        self.assertTrue(stopping.check_stop_file(stop=stopped.append))
        self.assertEqual(stopped, ["asked to by the stop command (start-dccore stop)"])
        self.assertFalse(os.path.exists(stopping.stop_file()))

    def test_one_left_from_before_is_cleared_at_startup(self):
        with open(stopping.stop_file(), "w") as handle:
            handle.write("stop\n")
        stopping.clear_stale_stop_file()
        self.assertFalse(os.path.exists(stopping.stop_file()))

    def test_the_watcher_starts_once(self):
        import runtime
        self.addCleanup(setattr, runtime, "stop_watcher_thread", runtime.stop_watcher_thread)
        runtime.stop_watcher_thread = None
        gate = __import__("threading").Event()
        self.addCleanup(gate.set)
        with mock.patch.object(stopping, "_watch", gate.wait):
            stopping.ensure_watcher()
            first = runtime.stop_watcher_thread
            stopping.ensure_watcher()
        self.assertIs(runtime.stop_watcher_thread, first)
        self.assertTrue(first.is_alive())


    def test_the_watcher_goes_on_after_a_stop(self):
        """#1065 review: it returned after the first stop, so if that one did
        not take, no later `start-dccore stop` was ever read."""
        looks = []

        def sleep(_seconds):
            if len(looks) == 3:
                raise SystemExit   # out of the endless loop, for the test
        with mock.patch.object(stopping.time, "sleep", sleep), \
                mock.patch.object(stopping, "check_stop_file", lambda: looks.append(1) or True):
            with self.assertRaises(SystemExit):
                stopping._watch()
        self.assertEqual(len(looks), 3)

    def test_the_file_is_looked_for_beside_the_lock_held(self):
        """#1065 review: a settings save reloads DCC_QUEUE_FILE live, and the
        watcher then looked in a folder the lock was never in."""
        held = os.path.join(self.dir, "held")
        os.makedirs(held)
        self.addCleanup(platform_compat._instance_lock.update, dict(platform_compat._instance_lock))
        platform_compat._instance_lock["path"] = os.path.join(held, "dccore.lock")
        self.set_config(DCC_QUEUE_FILE=os.path.join(self.dir, "moved", "dcc_queue.txt"))
        with open(os.path.join(held, stopping.STOP_FILE_NAME), "w") as handle:
            handle.write("stop\n")
        asked = []
        self.assertTrue(stopping.check_stop_file(stop=asked.append))
        self.assertEqual(len(asked), 1)
        self.assertFalse(os.path.exists(os.path.join(held, stopping.STOP_FILE_NAME)))


# A real interrupt, in a child of its own so the signal never reaches the test
# runner: the main thread sleeps, a thread asks it to stop after half a second.
SLEEPER = textwrap.dedent("""
    import signal, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    import stopping
    if sys.argv[2] == "ignored":
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # as a background start leaves it
        stopping.restore_interrupt()
    threading.Timer(0.5, stopping.interrupt_main).start()
    begun = time.time()
    try:
        time.sleep(20)
        print("slept", round(time.time() - begun, 1))
    except KeyboardInterrupt:
        print("interrupted", round(time.time() - begun, 1))
""")


class TheInterrupt(unittest.TestCase):
    def run_child(self, mode):
        # In a folder of its own, not loose in the temp folder (#1149): the
        # script's folder is the child's sys.path[0], so every import it made
        # listed the whole temp folder first, and a temp folder that has
        # filled up made this test slow.
        with tempfile.TemporaryDirectory(prefix="dccore-interrupt-") as folder:
            handle, path = tempfile.mkstemp(suffix=".py", dir=folder)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(SLEEPER)
            done = subprocess.run([sys.executable, path, os.path.join(support.REPO_ROOT, "src"), mode],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        word, seconds = done.stdout.split()
        return word, float(seconds)

    def test_a_sleeping_main_thread_wakes_at_once(self):
        """#1065 review: _thread.interrupt_main() only set a flag, and the
        reconnect wait (up to five minutes) did not look at it until it woke."""
        word, seconds = self.run_child("normal")
        self.assertEqual(word, "interrupted")
        self.assertLess(seconds, 5)

    def test_one_started_with_ctrl_c_ignored_can_still_be_stopped(self):
        """#1065 review: a background start inherits SIGINT ignored, and then
        nothing but QUIT happened - the bot was back ten seconds later."""
        word, seconds = self.run_child("ignored")
        self.assertEqual(word, "interrupted")
        self.assertLess(seconds, 5)


class TheShutdown(support.DCCoreTestCase):
    """#1065 review: a second interrupt during the shutdown, or one in the
    reconnect wait, escaped as a traceback that skipped the flush and exited
    non-zero - which launchd restarts."""

    def setUp(self):
        super().setUp()
        # The real module: the test case puts a stub in sys.modules["oserve"].
        from tests.test_startup import real_oserve
        self.oserve = real_oserve()
        self.flushed = []

    def escaping_is_a_failure(self, call):
        """An interrupt that escapes would end the test runner itself."""
        try:
            call()
        except KeyboardInterrupt:
            self.fail("a KeyboardInterrupt escaped the shutdown")

    def test_asked_twice_it_still_flushes_what_it_can_and_exits_0(self):
        import irc

        def flush(force=False):
            self.flushed.append(force)
            raise KeyboardInterrupt   # the second Ctrl-C, mid-flush
        with mock.patch.object(irc, "_flush_known_bots", flush), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                self.escaping_is_a_failure(self.oserve._shut_down)
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.flushed, [True])

    def test_a_stop_in_the_reconnect_wait_shuts_down_too(self):
        import announce
        import irc

        def sleep(_seconds):
            raise KeyboardInterrupt
        with mock.patch.object(irc, "irc_loop", lambda: None), \
                mock.patch.object(irc, "_flush_known_bots", lambda force=False: self.flushed.append(force)), \
                mock.patch.object(self.oserve.time, "sleep", sleep), \
                mock.patch.object(announce, "is_ready", announce.is_ready), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                self.escaping_is_a_failure(self.oserve.run_forever)
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.flushed, [True])

    def test_ctrl_c_works_however_the_program_was_started(self):
        entry = read("oserve.py").rsplit('\nif __name__ == "__main__":', 1)[1]
        self.assertLess(entry.index("    stopping.restore_interrupt()"), entry.index("    run_forever()"))


# A stand-in for the bot: holds the instance lock the way oserve.startup()
# does, and lets it go when the stop file appears - or, told to, never does.
CHILD = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    import platform_compat
    lock, stop_file, mode = sys.argv[2], sys.argv[3], sys.argv[4]
    platform_compat.take_instance_lock(lock)
    print("holding", flush=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        if mode == "obey" and os.path.exists(stop_file):
            os.remove(stop_file)
            break
        time.sleep(0.1)
""")


class TheStopCommand(Case):
    def child(self, mode):
        handle_fd, script = tempfile.mkstemp(suffix=".py", dir=self.dir)
        with os.fdopen(handle_fd, "w") as handle:
            handle.write(CHILD)
        proc = subprocess.Popen(
            [sys.executable, script, os.path.join(REPO_ROOT, "src"), stopping.lock_file(),
             stopping.stop_file(), mode], stdout=subprocess.PIPE, text=True)
        def end():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.addCleanup(end)
        self.assertEqual(proc.stdout.readline().strip(), "holding")
        return proc

    def test_not_running_says_so_and_asks_nothing(self):
        said = []
        self.assertEqual(stopping.stop_from_outside(log=said.append), 0)
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("DCCore is not running from this folder - nothing to stop."), said)
        self.assertFalse(os.path.exists(stopping.stop_file()))
        # Let go again: a stop command that kept the lock would itself be the
        # "running bot" the next start is refused for.
        self.assertIsNone(platform_compat._instance_lock["handle"])

    def test_a_running_bot_is_asked_and_waited_for(self):
        proc = self.child("obey")
        said = []
        self.assertEqual(stopping.stop_from_outside(wait=30, log=said.append), 0)
        self.assertEqual(said[-1], "DCCore has stopped.")
        self.assertTrue(said[0].startswith(f"Asked DCCore (pid {proc.pid}) to stop."), said)
        proc.wait(timeout=10)

    def test_one_that_does_not_stop_is_named_with_the_way_to_end_it(self):
        proc = self.child("ignore")
        said = []
        self.assertEqual(stopping.stop_from_outside(wait=2, log=said.append), 1)
        self.assertIn(f"has not stopped after 2 seconds", said[-1])
        self.assertIn(str(proc.pid), said[-1])


class TheConsoleCommand(unittest.TestCase):
    def session(self):
        lines = []
        return types.SimpleNamespace(nick="SomeAdmin", send=lines.append, lines=lines)

    def test_it_asks_for_now_first(self):
        session = self.session()
        with mock.patch.object(stopping, "request_stop_soon") as soon:
            adminchat.handle_command(session, "shutdown")
        soon.assert_not_called()
        self.assertIn("Type 'shutdown now'", session.lines[0])

    def test_shutdown_now_stops_and_says_who(self):
        session = self.session()
        with mock.patch.object(stopping, "request_stop_soon") as soon:
            adminchat.handle_command(session, "shutdown now")
        soon.assert_called_once_with("asked by SomeAdmin in the admin console")
        self.assertIn("Stopping the bot", session.lines[0])


def read(path):
    with io.open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheWiring(unittest.TestCase):
    def test_oserve_stop_runs_before_anything_starts(self):
        code = read("oserve.py")
        self.assertLess(code.index('if "--stop" in sys.argv[1:]:'),
                        code.index("platform_compat.install_console_log(_console_log_settings)"))
        self.assertIn("        sys.exit(stopping.stop_from_outside())", code)

    def test_startup_clears_an_old_file_after_taking_the_lock(self):
        # startup() alone: the shutdown in run_forever() clears it too.
        body = read("oserve.py").split("def startup(", 1)[1].split("\ndef ", 1)[0]
        self.assertLess(body.index("platform_compat.take_instance_lock(lock_path)"),
                        body.index("stopping.clear_stale_stop_file()"))

    def test_the_watcher_starts_at_the_entry_point_only(self):
        """Not inside run_forever(): tests drive that in-process, and a watcher
        started there once interrupted the whole test runner."""
        code = read("oserve.py")
        self.assertNotIn("ensure_watcher", code.split("def run_forever(", 1)[1].split('\nif __name__ == "__main__":', 1)[0])
        entry = code.rsplit('\nif __name__ == "__main__":', 1)[1]
        self.assertLess(entry.index("    startup()"), entry.index("    stopping.ensure_watcher()"))
        self.assertLess(entry.index("    stopping.ensure_watcher()"), entry.index("    run_forever()"))

    def test_both_launchers_have_stop(self):
        bat = read("scripts/windows/start-dccore.bat")
        self.assertIn('if /i "%~1"=="stop" goto :run_stop', bat)
        self.assertIn(":run_stop\n%PY% oserve.py --stop", bat.replace("\r\n", "\n"))
        sh = read("scripts/linux/start-dccore.sh")
        self.assertIn('if [ "$1" = "stop" ]; then\n    "$PY" oserve.py --stop\n    exit $?', sh)

    def test_the_dashboard_button_confirms_then_posts(self):
        js = read("web/app.js")
        handler = js.split('el.stopBotRunBtn.addEventListener("click", function () {', 1)[1][:600]
        self.assertLess(handler.index('window.confirm(t("tools.stopBotConfirm"))'),
                        handler.index('postJson("/api/tools/stop", {})'))


@unittest.skipUnless(getattr(__import__("webserver"), "HAVE_FLASK", False), "Flask not installed")
class TheRoute(support.DCCoreTestCase):
    def test_it_answers_then_stops(self):
        import adminchat as _admin
        import webserver
        from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client
        self.set_config(ADMIN_PASSWORD_HASH=_admin.make_password_hash(WEBUI_TEST_PASSWORD, iterations=1000))
        client = webserver.create_app().test_client()
        log_in_test_client(client)
        with mock.patch.object(stopping, "request_stop_soon") as soon:
            resp = client.post("/api/tools/stop", json={})
        self.assertEqual(resp.status_code, 200)
        soon.assert_called_once_with("asked from the dashboard")


if __name__ == "__main__":
    unittest.main()
