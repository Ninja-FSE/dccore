"""A test leaves the process as it found it.

The suite runs in four processes, each a hundred-odd modules long, and the
flakes CI kept re-running were all one test reaching into a later one in the
same process:
  - a settings save in test_dashboard_routes started a real !rehash on a
    thread nobody waited for. It reloaded defaults while the next test was
    logging in, and the login answered 401;
  - every real DCC send left a 3, 15 or 45 s queue sweep behind, which woke
    inside a later test and thawed, dropped or re-froze that test's queue;
  - console listeners held six of a shard's eleven DCC ports for a minute;
  - a debug line queued by one test was delivered by another test's drain as
    a ninth line where it sent eight;
  - settings set directly and never put back: the DCC port range collapsed
    to one port, a password hash, a debug channel, the list folders.

tests/__init__.py now checks every test, whatever its base class, after its
last cleanup: a thread it started that is still running, or a setting it
changed and left, fails it. tests/support.py holds a real send's follow-up
sweeps and the debug drain to the test that started them. This proves each
part still does its job, so the guard cannot quietly stop guarding.
"""

import io
import os
import socket
import tempfile
import threading
import time
import unittest

import tests
from tests import support
from tests.support import DCCoreTestCase

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402


def run_inner(case_class):
    """Run one test class in a runner of its own and return the result."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_class)
    return unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)


def complaints(result):
    return " ".join(tb for _test, tb in result.failures + result.errors)


def alive(target_name):
    return [thread for thread in threading.enumerate()
            if getattr(getattr(thread, "_target", None), "__name__", None) == target_name
            and thread.is_alive()]


class TheGuardIsInPlace(unittest.TestCase):

    def test_every_test_is_checked_before_its_setup_runs(self):
        """The hook is unittest's own _callSetUp. If a Python release renames
        it, the guard is gone without a word; this says so instead."""
        self.assertIs(unittest.TestCase._callSetUp,
                      tests._call_set_up_and_check_afterwards)


class AThreadLeftRunningFailsTheTest(unittest.TestCase):

    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def test_a_queue_sweep_left_behind_is_named(self):
        """Named, and without waiting out the grace: a sweep that ends a
        little later is exactly how it reaches the next test."""
        release = self.release
        left = []

        def delayed_queue_trigger_fallback():
            release.wait(30)

        class Inner(unittest.TestCase):
            def test_it(self):
                thread = threading.Thread(target=delayed_queue_trigger_fallback, daemon=True)
                left.append(thread)
                thread.start()

        started = time.monotonic()
        result = run_inner(Inner)
        release.set()
        left[0].join(5)

        self.assertFalse(result.wasSuccessful())
        self.assertIn("'delayed_queue_trigger_fallback'", complaints(result))
        self.assertLess(time.monotonic() - started, tests.LEAK_GRACE_SECONDS)

    def test_a_thread_still_running_after_the_grace_is_named(self):
        release = self.release
        self.addCleanup(setattr, tests, "LEAK_GRACE_SECONDS", tests.LEAK_GRACE_SECONDS)
        tests.LEAK_GRACE_SECONDS = 0.1

        def a_worker_nobody_stopped():
            release.wait(30)

        class Inner(unittest.TestCase):
            def test_it(self):
                threading.Thread(target=a_worker_nobody_stopped, daemon=True).start()

        result = run_inner(Inner)

        self.assertFalse(result.wasSuccessful())
        self.assertIn("'a_worker_nobody_stopped'", complaints(result))

    def test_a_thread_that_finishes_within_the_grace_is_not_a_leak(self):
        def a_worker_that_is_finishing():
            time.sleep(0.05)

        class Inner(unittest.TestCase):
            def test_it(self):
                threading.Thread(target=a_worker_that_is_finishing, daemon=True).start()

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))

    def test_a_thread_joined_in_a_cleanup_is_not_a_leak(self):
        release = self.release

        def a_worker_stopped_in_a_cleanup():
            release.wait(30)

        class Inner(unittest.TestCase):
            def test_it(self):
                thread = threading.Thread(target=a_worker_stopped_in_a_cleanup, daemon=True)
                thread.start()
                self.addCleanup(thread.join, 5)
                self.addCleanup(release.set)

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))


class ASettingLeftChangedFailsTheTest(unittest.TestCase):

    def setUp(self):
        self.addCleanup(setattr, config, "MAX_FETCH_SLOTS", config.MAX_FETCH_SLOTS)
        self.shipped = config.SHIPPED_VALUES["MAX_FETCH_SLOTS"]

    def test_a_setting_left_changed_is_named(self):
        changed = self.shipped + 7

        class Inner(unittest.TestCase):
            def test_it(self):
                config.MAX_FETCH_SLOTS = changed

        result = run_inner(Inner)

        self.assertFalse(result.wasSuccessful())
        self.assertIn("MAX_FETCH_SLOTS", complaints(result))

    def test_a_setting_put_back_is_not(self):
        changed = self.shipped + 7

        class Inner(unittest.TestCase):
            def test_it(self):
                self.addCleanup(setattr, config, "MAX_FETCH_SLOTS", config.MAX_FETCH_SLOTS)
                config.MAX_FETCH_SLOTS = changed

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))

    def test_a_setting_left_at_its_shipped_value_is_not(self):
        """What a test that reloads defaults leaves: the shipped values."""
        config.MAX_FETCH_SLOTS = self.shipped + 7
        shipped = self.shipped

        class Inner(unittest.TestCase):
            def test_it(self):
                config.MAX_FETCH_SLOTS = shipped

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))


class WhatTheHarnessResets(DCCoreTestCase):

    def test_what_it_resets_for_every_test_is_not_counted_against_one(self):
        class Inner(DCCoreTestCase):
            def test_it(self):
                config.NICKNAME = "SomeBot"

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))

    def test_every_setting_it_says_it_resets_is_reset_to_that_value(self):
        """The guard lets a DCCoreTestCase leave these changed, and any test
        leave one at this value, on the strength of this list: a name on it
        that reset_config() does not reset, or resets to something else,
        would be a leak nobody checks."""
        for name, value in sorted(support.SETTINGS_RESET_VALUES.items()):
            with self.subTest(name=name):
                setattr(config, name, object())

                support.reset_config()

                self.assertEqual(getattr(config, name), value)

    def test_a_plain_test_that_resets_the_config_is_not_blamed_for_it(self):
        """What the reset puts back is not the plain test's leak, whatever
        the test before had left."""
        config.NICKNAME = "SomeBot"

        class Inner(unittest.TestCase):
            def test_it(self):
                support.reset_config()

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))

    def test_a_password_hash_or_a_debug_channel_is_reset_to_the_shipped_blank(self):
        """Eleven tests set the one and three the other directly, and every
        later test in the process ran with them."""
        for name, value in (("ADMIN_PASSWORD_HASH", "pbkdf2_sha256$1000$salt$hash"),
                            ("DEBUG_CHANNEL", "#somechannel")):
            with self.subTest(name=name):
                setattr(config, name, value)

                support.reset_config()

                self.assertEqual(getattr(config, name), config.SHIPPED_VALUES[name])

    def test_the_freeze_clock_starts_unpaused(self):
        runtime.freeze_clock_paused_at = time.time() - 600

        support.reset_config()

        self.assertIsNone(runtime.freeze_clock_paused_at)

    def test_the_dashboard_starts_with_no_failed_logins(self):
        import webserver

        webserver._web_bad_ips["127.0.0.1"] = [2, time.time()]

        support.reset_config()

        self.assertEqual(webserver._web_bad_ips, {})


class TheFoldersATestIsGiven(DCCoreTestCase):

    def test_without_a_tree_they_are_the_tests_own_not_the_checkouts(self):
        shipped = config.SHIPPED_VALUES
        temp = os.path.realpath(tempfile.gettempdir())
        for name in ("LOCAL_LIST_DIR", "TMP_ZIP_DIR", "HARD_BANS_FILE"):
            with self.subTest(name=name):
                value = getattr(config, name)
                self.assertNotEqual(value, shipped[name])
                self.assertTrue(os.path.realpath(value).startswith(temp), value)

    def test_a_tree_gives_them_back_when_the_test_ends(self):
        before = (config.FILE_DIRECTORY, config.LOCAL_LIST_DIR, config.TMP_ZIP_DIR)

        class Inner(DCCoreTestCase):
            def test_it(self):
                tree = self.make_tree()
                self.assertEqual(config.LOCAL_LIST_DIR, tree.lists)

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))
        self.assertEqual((config.FILE_DIRECTORY, config.LOCAL_LIST_DIR, config.TMP_ZIP_DIR), before)


class TheDebugDrainIsTheTestsOwn(DCCoreTestCase):

    def test_a_line_an_earlier_test_queued_is_not_this_tests(self):
        """The ninth line: an earlier test's send_debug() queued it with no
        socket to drain to, and test_a_shared_outbound_pace's own drain
        delivered it as one of its own."""
        announce._debug_queue.append("PRIVMSG #somechannel :queued by an earlier test\r\n")
        self.addCleanup(announce._debug_queue.clear)
        found = []

        class Inner(DCCoreTestCase):
            def test_it(self):
                found.extend(announce._debug_queue)

        result = run_inner(Inner)

        self.assertTrue(result.wasSuccessful(), complaints(result))
        self.assertEqual(found, [])

    def test_a_drain_a_test_starts_is_retired_when_it_ends(self):
        started = []

        class Inner(DCCoreTestCase):
            def test_it(self):
                self.set_config(DEBUG_CHANNEL="#somechannel")
                announce.send_debug("a line for the debug channel")
                started.extend(alive("_debug_drain_worker"))

        result = run_inner(Inner)

        self.assertTrue(started, "the test never started a drain, so proves nothing")
        self.assertTrue(result.wasSuccessful(), complaints(result))
        self.assertEqual(alive("_debug_drain_worker"), [])
        self.assertFalse(announce._debug_drain_started)


class ARealSendsSweepIsTheTestsOwn(DCCoreTestCase):
    """Driven through the real start_dcc_send(), on the branch that schedules
    the longest one: no free port, a retry 45 s out."""

    def setUp(self):
        super().setUp()
        self.held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.held.bind(("0.0.0.0", 0))
        self.held.listen(1)
        self.addCleanup(self.held.close)
        tree = self.make_tree()
        self.track = tree.tracks[0]

    def refused_send_class(self, seen):
        busy = self.held.getsockname()[1]
        track = self.track

        class Inner(DCCoreTestCase):
            def test_it(self):
                self.set_config(DCC_PORT_START=busy, DCC_PORT_END=busy, MY_IP_OR_DOCK="8.8.8.8")
                sock = support.RecordingSocket()
                self.oserve.irc_connection = sock
                dcc.start_dcc_send(sock, "dave", track, os.path.basename(track), "#somechannel",
                                   {"file": os.path.basename(track), "path": track})
                seen.extend(alive("delayed_port_retry"))
        return Inner

    def test_the_retry_does_not_outlive_the_test(self):
        seen = []

        started = time.monotonic()
        result = run_inner(self.refused_send_class(seen))

        self.assertTrue(seen, "the send scheduled no retry, so this proves nothing")
        self.assertTrue(result.wasSuccessful(), complaints(result))
        self.assertEqual(alive("delayed_port_retry"), [])
        self.assertLess(time.monotonic() - started, 30)

    def test_without_the_hold_the_guard_names_it(self):
        """Control: the hold is what ends it, and the guard sees it if not.
        The retry left running here is held by this test instead."""
        seen = []
        self.addCleanup(setattr, support, "hold_send_follow_ups", support.hold_send_follow_ups)
        support.hold_send_follow_ups = lambda test: []

        result = run_inner(self.refused_send_class(seen))

        self.assertTrue(seen, "the send scheduled no retry, so this proves nothing")
        self.assertFalse(result.wasSuccessful())
        self.assertIn("'delayed_port_retry'", complaints(result))


if __name__ == "__main__":
    unittest.main()
