"""!rehash must not silently break mutual exclusion or corrupt the live queue.

TWO FINDINGS, ONE ROOT CAUSE

importlib.reload() re-executes a module body. runtime.py exists because that
rebinds every `dcc_queue = {}` config.py assigns - a fresh, empty container,
detached from whatever the daemon had accumulated. The same reload rebinds a
module-level `threading.Lock()` exactly the same way: a thread already inside
`with dcc.queue_lock:` when a rehash lands goes on holding the OLD, now
invisible object, while the next caller acquires the FRESH one the reload
just created - two threads in the critical section that lock exists to keep
them out of, at once. The trigger is routine: the web dashboard fires a
rehash on every Settings save, reachable by an operator clicking Save while a
transfer is running.

dcc.py, announce.py and db.py are all reloaded by !rehash
(commands.CORE_MODULES) and each used to allocate its own lock at module
level. All three now bind to an object runtime.py owns instead - the same
fix runtime.py's own _channel_users_lock already used, generalised.

THE SECOND FINDING IS THE INVERSE MISTAKE

dcc_queue is a runtime.py-bound container, so a reload never actually empties
or replaces it - the object commands.py sees before and after
reload_modules_in_order() is the identical one, with every write made during
the reload window already on it. handle_rehash_request() used to snapshot it
anyway before the reload and overwrite it with that snapshot afterwards -
which, since the live object never truly changed, was purely destructive: a
request that arrived during the reload window was wiped by the snapshot's
`.clear()`, and a transfer that COMPLETED during the window - removed from the
live queue - came back from the stale snapshot and was sent to the user a
second time. Deleted rather than fixed, since runtime.py's binding already
does the right thing on its own.
"""

import contextlib
import importlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ALockSurvivesAReloadOfItsOwningModule(unittest.TestCase):
    """One case per lock #212 found. Each reloads the real module - dcc.py,
    announce.py or db.py - and checks the SAME object comes back, the same
    way tests/test_runtime_state.py already does for containers on config.py.

    Reloading afterward a second time (`addCleanup`) restores the module to
    its normal post-import state for every test that runs after this one,
    exactly as test_runtime_state.py's own ContainersSurviveAReload does.
    """

    def _reload(self, module):
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(module)
        self.addCleanup(self._reload_quietly, module)

    def _reload_quietly(self, module):
        with contextlib.redirect_stdout(io.StringIO()):
            importlib.reload(module)

    def _assert_survives(self, module, attr_name, runtime_name):
        before = getattr(module, attr_name)
        self.assertIs(before, getattr(runtime, runtime_name),
                      f"{module.__name__}.{attr_name} is not runtime.{runtime_name} "
                      f"even before a reload - fixture assumption is wrong")

        self._reload(module)

        after = getattr(module, attr_name)
        self.assertIs(after, before,
                      f"reloading {module.__name__} rebound {attr_name} to a "
                      f"new lock - a thread already holding the old one would "
                      f"share the critical section with the next caller")

        # Behavioural half, not just identity: hold the lock across the point
        # where the module *would* rebind it if the fix regressed, and prove
        # a second acquire still cannot get in. This is what identity alone
        # does not show - a rebound lock is still A lock, just not the one
        # anybody is holding.
        self.assertTrue(before.acquire(timeout=1),
                        "could not acquire the lock at all - broken fixture")
        try:
            self._reload(module)
            still_the_same = getattr(module, attr_name)
            self.assertIs(still_the_same, before)

            second_acquired = still_the_same.acquire(timeout=0.1)
            if second_acquired:
                still_the_same.release()
            self.assertFalse(
                second_acquired,
                "a second acquire succeeded while the first caller still held "
                "the lock across the reload - mutual exclusion was lost")
        finally:
            before.release()

    def test_dcc_queue_lock(self):
        self._assert_survives(dcc, "queue_lock", "queue_lock")

    def test_announce_debug_drain_guard(self):
        self._assert_survives(announce, "_debug_drain_guard", "debug_drain_guard")

    def test_announce_debug_sinks_lock(self):
        self._assert_survives(announce, "_debug_sinks_lock", "debug_sinks_lock")

    def test_db_disk_lock(self):
        self._assert_survives(db, "_disk_lock", "disk_lock")


class ARehashNoLongerTouchesDccQueueDirectly(DCCoreTestCase):
    """#216. handle_rehash_request() used to snapshot dcc_queue before the
    reload and overwrite it with that snapshot afterwards - destructive,
    since the live object was never actually replaced by the reload at all.

    reload_modules_in_order() - the exact call between "snapshot" and
    "restore" in handle_rehash_request() - is stubbed rather than
    importlib.reload itself, for two reasons: reloading the real modules
    mid-suite would reset state the rest of the run depends on (the same
    reason tests/test_uncovered_daemon_functions.py's
    TheRehashHandlerIsEntered stubs importlib.reload directly), and this
    interception point doubles as the hook for simulating a queue change
    that lands INSIDE the reload window - the exact scenario #216 is about.
    A first version of these tests stubbed importlib.reload with a no-op and
    never actually simulated concurrent modification, so the old, reverted
    destructive code passed them too; this is the fix for that gap, checked
    by literally reverting the production fix and confirming these go red.
    """

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="TestBot", CHANNEL="#chan", ADMIN_NICK="operator")

        # A live socket, so the handler's channel-sync step does not raise
        # trying to use one that is absent - out of scope for this test.
        import oserve
        real_conn = getattr(oserve, "irc_connection", None)
        oserve.irc_connection = _RecordingSocket()
        self.addCleanup(setattr, oserve, "irc_connection", real_conn)

    def _stub_reload_with(self, during_reload):
        """Replace commands.reload_modules_in_order with a stub that runs
        `during_reload()` instead of a real reload - simulating a dcc_queue
        change landing exactly inside the window a real reload would open,
        without ever calling importlib.reload for real."""
        real = commands.reload_modules_in_order

        def stub(*args, **kwargs):
            during_reload()
            return []

        commands.reload_modules_in_order = stub
        self.addCleanup(setattr, commands, "reload_modules_in_order", real)

    def test_a_row_added_during_the_reload_window_is_not_wiped(self):
        """The snapshot used to be taken before this row could exist, so the
        old restore's `.clear()` would have erased it."""
        config.dcc_queue["existing"] = ["Other.flac"]
        self._stub_reload_with(
            lambda: config.dcc_queue.__setitem__("late_arrival", ["Track.flac"]))

        commands.handle_rehash_request("operator", "#chan", authorised=True)

        self.assertIn("late_arrival", config.dcc_queue,
                      "handle_rehash_request removed a queue row that arrived "
                      "during the reload window")
        self.assertEqual(config.dcc_queue["late_arrival"], ["Track.flac"])

    def test_a_row_that_completed_during_the_window_is_not_resurrected(self):
        """The exact failure #216 reports: a delivered transfer, removed from
        the queue during the window, coming back and being sent again."""
        config.dcc_queue["someuser"] = ["Track.flac"]
        self._stub_reload_with(
            lambda: config.dcc_queue.__delitem__("someuser"))

        commands.handle_rehash_request("operator", "#chan", authorised=True)

        self.assertNotIn("someuser", config.dcc_queue,
                         "a transfer that completed during the reload window "
                         "was restored as still-queued - it would be sent to "
                         "the user a second time")

    def test_dcc_queue_is_the_exact_same_object_afterwards(self):
        """The property the fix rests on: nothing in handle_rehash_request
        rebinds config.dcc_queue away from runtime's object."""
        self._stub_reload_with(lambda: None)
        before = config.dcc_queue

        commands.handle_rehash_request("operator", "#chan", authorised=True)

        self.assertIs(config.dcc_queue, before)
        self.assertIs(config.dcc_queue, runtime.dcc_queue)


class _RecordingSocket:
    def send(self, data):
        pass

    def sendall(self, data):
        pass


if __name__ == "__main__":
    unittest.main()
