"""Five daemon functions the coverage gate found no test entering (#1178).

scripts/function_coverage.py runs the suite under a profiler and fails on any
public function nothing calls. These five were reached only in child
processes - the stop tests start a real bot and press its buttons - or were
stubbed out by every boot test, and a profiler in the test process sees
neither. So the gate failed on main, which teaches people to stop reading it.

Each is driven here in this process, with the dangerous edge replaced: no
real Ctrl-C is raised, no real lock is taken, and the dispatcher's endless
loop is ended by its own sleep after a few passes.
"""

import signal
import threading
import types
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import platform_compat  # noqa: E402
import stopping  # noqa: E402


class InterruptMain(unittest.TestCase):

    def test_it_raises_a_real_sigint_rather_than_setting_a_flag(self):
        # Both branches on every system: os.name decides which one runs, and
        # a test that only ever took this machine's branch let the other one
        # change unnoticed.
        for os_name, expected in (("nt", ("raise", signal.SIGINT)),
                                  ("posix", ("kill", threading.main_thread().ident, signal.SIGINT))):
            with self.subTest(os_name=os_name):
                raised = []
                with mock.patch.object(stopping, "os", types.SimpleNamespace(name=os_name)), \
                        mock.patch.object(signal, "raise_signal",
                                          lambda sig: raised.append(("raise", sig)), create=True), \
                        mock.patch.object(signal, "pthread_kill",
                                          lambda ident, sig: raised.append(("kill", ident, sig)),
                                          create=True), \
                        mock.patch.object(stopping._thread, "interrupt_main") as flag_only:
                    stopping.interrupt_main()
                self.assertEqual(raised, [expected])
                flag_only.assert_not_called()

    def test_it_falls_back_to_the_flag_when_no_signal_can_be_sent(self):
        def refuse(*_args):
            raise OSError("no signal for you")
        with mock.patch.object(signal, "raise_signal", refuse, create=True), \
                mock.patch.object(signal, "pthread_kill", refuse, create=True), \
                mock.patch.object(stopping._thread, "interrupt_main") as flag_only:
            stopping.interrupt_main()
        flag_only.assert_called_once_with()


class RestoreInterrupt(unittest.TestCase):

    def test_an_ignored_ctrl_c_gets_its_handler_back(self):
        with mock.patch.object(signal, "getsignal", return_value=signal.SIG_IGN), \
                mock.patch.object(signal, "signal") as install:
            stopping.restore_interrupt()
        install.assert_called_once_with(signal.SIGINT, signal.default_int_handler)

    def test_a_working_ctrl_c_is_left_alone(self):
        with mock.patch.object(signal, "getsignal", return_value=signal.default_int_handler), \
                mock.patch.object(signal, "signal") as install:
            stopping.restore_interrupt()
        install.assert_not_called()


class RequestStopSoon(unittest.TestCase):

    def test_the_stop_comes_after_the_answer_on_a_thread_of_its_own(self):
        asked = threading.Event()
        seen = {}

        def record(reason, interrupt=None):
            seen["reason"] = reason
            seen["thread"] = threading.current_thread().name
            asked.set()

        with mock.patch.object(stopping, "request_stop", record):
            stopping.request_stop_soon("asked from a test", delay=0.0)
            self.assertTrue(asked.wait(5), "request_stop was never called")
        self.assertEqual(seen["reason"], "asked from a test")
        self.assertEqual(seen["thread"], "stop-request")


class RunningPid(unittest.TestCase):

    def test_a_held_lock_is_a_running_bot_with_its_pid(self):
        def held(path):
            raise platform_compat.AlreadyRunning(path, pid=4321)
        with mock.patch.object(platform_compat, "take_instance_lock", held), \
                mock.patch.object(platform_compat, "release_instance_lock") as release:
            self.assertEqual(stopping.running_pid(), (True, 4321))
        release.assert_not_called()

    def test_a_free_lock_is_let_go_at_once(self):
        with mock.patch.object(platform_compat, "take_instance_lock") as take, \
                mock.patch.object(platform_compat, "release_instance_lock") as release:
            self.assertEqual(stopping.running_pid(), (False, None))
        take.assert_called_once_with(stopping.lock_file())
        release.assert_called_once_with()


class _Enough(Exception):
    """Ends the dispatcher's endless loop from inside its sleep."""


class FetchDispatcherWorker(unittest.TestCase):

    def test_every_pass_checks_the_queue_tells_the_feed_and_survives_an_error(self):
        checks, feeds, sleeps = [], [], []

        def check():
            checks.append(1)
            if len(checks) == 2:
                raise RuntimeError("one bad pass")

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise _Enough

        with mock.patch.object(dcc_fetch, "check_fetch_queue", check), \
                mock.patch.object(dcc_fetch, "tell_the_fetch_feed", lambda: feeds.append(1)), \
                mock.patch.object(dcc_fetch.time, "sleep", sleep), \
                mock.patch("builtins.print"):
            with self.assertRaises(_Enough):
                dcc_fetch.fetch_dispatcher_worker()

        self.assertEqual(len(checks), 3, "a failed pass stopped the loop")
        self.assertEqual(len(feeds), 3, "the feed was not told after every pass, failed or not")
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])


if __name__ == "__main__":
    unittest.main()
