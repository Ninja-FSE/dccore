"""Ticking AUTO_REFETCH_LISTS on a running bot starts the refresh worker (#625).

WHAT WENT WRONG

list_fetch.auto_refetch_worker was started in exactly one place: oserve.
startup(), and only when the setting was already on at boot. The dashboard's
Settings page saves the file and fires a rehash, the rehash body never looked
at the setting, and AUTO_REFETCH_LISTS was not in webserver.SETTINGS_RESTART_
ONLY either - so the operator got a green save, "rehash started", no restart
notice, and no worker. Held lists went stale until the next restart; the only
live effect was the one-shot sweep irc.py runs on a reconnect.

WHAT CHANGED

list_fetch.ensure_auto_refetch_worker() starts the loop if the setting is on
and it is not running yet, and both startup() and the rehash body call it. The
"already running" state is runtime.py's - a lock and a flag - so a rehash
cannot forget it and start a second worker on every Settings save.

The rehash tests below drive the real commands._handle_rehash_request() with
the reload, the transfer wait and the debug line stubbed, since those are the
parts that need a socket; the thread starter is injected, so nothing here ever
starts a thread that outlives the test.
"""

import contextlib
import io
import os
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import dcc  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class StartsOnceAndOnlyWhenOn(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "auto_refetch_started",
                        runtime.auto_refetch_started)
        runtime.auto_refetch_started = False
        self.starts = []
        self.start = lambda: self.starts.append(1)

    def test_off_starts_nothing(self):
        self.set_config(AUTO_REFETCH_LISTS=False)

        self.assertFalse(list_fetch.ensure_auto_refetch_worker(start=self.start))
        self.assertEqual(self.starts, [])
        self.assertFalse(runtime.auto_refetch_started)

    def test_on_starts_it_exactly_once(self):
        """The second call is every later rehash: a dashboard save that
        changed something else entirely must not start another worker."""
        self.set_config(AUTO_REFETCH_LISTS=True)

        first = list_fetch.ensure_auto_refetch_worker(start=self.start)
        second = list_fetch.ensure_auto_refetch_worker(start=self.start)

        self.assertEqual((first, second), (True, False))
        self.assertEqual(self.starts, [1])

    def test_the_started_state_is_the_one_runtime_holds(self):
        """The guard consults runtime.auto_refetch_started and nothing of its
        own: clearing that one flag is what lets a start happen again, so a
        private copy in list_fetch.py - which a rehash could reset - would
        show up here as a start that never came."""
        self.set_config(AUTO_REFETCH_LISTS=True)
        list_fetch.ensure_auto_refetch_worker(start=self.start)

        self.assertTrue(runtime.auto_refetch_started)
        runtime.auto_refetch_started = False

        self.assertTrue(list_fetch.ensure_auto_refetch_worker(start=self.start))
        self.assertEqual(self.starts, [1, 1])

    def test_a_failed_start_leaves_the_flag_down(self):
        """So the next rehash tries again instead of believing a worker is
        running that never was."""
        self.set_config(AUTO_REFETCH_LISTS=True)

        def boom():
            raise RuntimeError("cannot start a thread")

        with self.assertRaises(RuntimeError):
            list_fetch.ensure_auto_refetch_worker(start=boom)

        self.assertFalse(runtime.auto_refetch_started)
        self.assertTrue(list_fetch.ensure_auto_refetch_worker(start=self.start))

    def test_the_guard_lives_in_runtime(self):
        self.assertIsInstance(runtime.auto_refetch_guard, type(threading.Lock()))
        self.assertIs(runtime.auto_refetch_started, False)


class ARehashStartsIt(DCCoreTestCase):
    """The rehash is what a dashboard save fires, so the rehash is where the
    setting has to take effect."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "auto_refetch_started",
                        runtime.auto_refetch_started)
        runtime.auto_refetch_started = False
        self.starts = []
        real = list_fetch.ensure_auto_refetch_worker
        # The body calls it with no arguments; hand the real function a
        # starter that records instead of spawning.
        patches = [
            mock.patch.object(list_fetch, "ensure_auto_refetch_worker",
                              lambda: real(start=lambda: self.starts.append(1))),
            mock.patch.object(commands, "reload_modules_in_order",
                              lambda *a, **k: []),
            mock.patch.object(dcc, "wait_for_transfers_to_finish",
                              lambda *a, **k: None),
            mock.patch.object(announce, "send_debug", lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.set_config(CHANNEL="#somewhere")

    def rehash(self):
        with contextlib.redirect_stdout(io.StringIO()):
            commands._handle_rehash_request("SomeOp", "#somewhere")
        self.assertFalse(runtime.rehash_lock.locked())

    def test_a_rehash_with_the_setting_on_starts_the_worker(self):
        self.set_config(AUTO_REFETCH_LISTS=True)

        self.rehash()

        self.assertEqual(self.starts, [1])

    def test_a_second_rehash_does_not_start_a_second_worker(self):
        self.set_config(AUTO_REFETCH_LISTS=True)

        self.rehash()
        self.rehash()

        self.assertEqual(self.starts, [1])

    def test_a_rehash_with_the_setting_off_starts_nothing(self):
        self.set_config(AUTO_REFETCH_LISTS=False)

        self.rehash()

        self.assertEqual(self.starts, [])

    def test_no_thread_outlives_the_rehash(self):
        """The starter is injected precisely so this holds; asserted so a
        change that bypasses ensure_auto_refetch_worker() is caught."""
        self.set_config(AUTO_REFETCH_LISTS=True)
        before = set(threading.enumerate())

        self.rehash()

        self.assertEqual(set(threading.enumerate()) - before, set())


class TheSettingIsLive(unittest.TestCase):
    """No restart is required, so nothing may say one is."""

    def test_it_is_not_a_restart_only_setting(self):
        try:
            import webserver
        except Exception as err:  # pragma: no cover - Flask is optional
            self.skipTest(f"webserver unavailable: {err}")

        self.assertNotIn("AUTO_REFETCH_LISTS", webserver.SETTINGS_RESTART_ONLY)

    def test_the_help_does_not_ask_for_a_restart(self):
        import settings_help

        self.assertNotIn("restart",
                         settings_help.PLAIN_HELP["AUTO_REFETCH_LISTS"].lower())


if __name__ == "__main__":
    unittest.main()
