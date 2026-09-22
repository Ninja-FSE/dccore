"""#576: a rehash silently detached the structured feed.

importlib.reload(announce) resets announce._event_sinks to [] as well as
_debug_sinks, but the rehash put back only the debug sinks. A structured
console session (the mIRC script after `hello`) receives REQUEST, QUEUED,
SENDING, SENT, FAIL, SEARCH and RESUMED, and the event-driven STATUS burst,
through its event sink - and drops those kinds from its debug sink because it
expects them as fields. After any rehash it heard neither.

reattach_event_sinks() is tested against a stand-in module, like
reattach_debug_sinks(), because the real reload is what handle_rehash_request
does; the wiring in _handle_rehash_request is checked from its source.
"""

import inspect
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402


class FakeAnnounce:
    def __init__(self):
        self._debug_sinks = []
        self._event_sinks = []
        self._debug_sinks_lock = threading.Lock()


class ReattachEventSinksTests(unittest.TestCase):
    def test_a_live_event_sink_is_reattached(self):
        fake = FakeAnnounce()
        sink = lambda *a, **k: None

        reattached = commands.reattach_event_sinks(fake, [sink])

        self.assertEqual(reattached, [sink])
        self.assertEqual(fake._event_sinks, [sink])

    def test_no_live_sinks_reattaches_nothing(self):
        fake = FakeAnnounce()
        self.assertEqual(commands.reattach_event_sinks(fake, []), [])
        self.assertEqual(fake._event_sinks, [])

    def test_several_sessions_are_all_reattached_in_order(self):
        fake = FakeAnnounce()
        first, second = (lambda *a, **k: 1), (lambda *a, **k: 2)

        commands.reattach_event_sinks(fake, [first, second])

        self.assertEqual(fake._event_sinks, [first, second])

    def test_a_sink_already_present_is_not_added_twice(self):
        fake = FakeAnnounce()
        sink = lambda *a, **k: None
        fake._event_sinks.append(sink)

        reattached = commands.reattach_event_sinks(fake, [sink])

        self.assertEqual(reattached, [])
        self.assertEqual(fake._event_sinks, [sink])

    def test_the_debug_registry_is_left_alone(self):
        fake = FakeAnnounce()
        commands.reattach_event_sinks(fake, [lambda *a, **k: None])
        self.assertEqual(fake._debug_sinks, [])


class RehashWiringTests(unittest.TestCase):
    """_handle_rehash_request must snapshot the event sinks before the reload
    and put them back after it."""

    def setUp(self):
        self.src = inspect.getsource(commands._handle_rehash_request)

    def test_the_event_sinks_are_snapshotted_before_the_reload(self):
        snap = self.src.index("list(announce._event_sinks)")
        reload_at = self.src.index("\n        reload_modules_in_order()\n")
        self.assertLess(snap, reload_at)

    def test_the_snapshot_is_taken_under_the_sink_lock(self):
        snap = self.src.index("list(announce._event_sinks)")
        lock = self.src.rindex("announce._debug_sinks_lock", 0, snap)
        self.assertLess(snap - lock, 200)

    def test_the_event_sinks_are_reattached_after_the_reload(self):
        reload_at = self.src.index("\n        reload_modules_in_order()\n")
        back = self.src.index("reattach_event_sinks(_ann, live_event_sinks)")
        self.assertGreater(back, reload_at)


class RealAnnounceTests(unittest.TestCase):
    """The reset really happens, and the real registry is what gets refilled."""

    def test_a_reload_empties_the_event_registry_and_reattach_refills_it(self):
        import importlib

        import announce
        sink = lambda *a, **k: None
        announce.add_event_sink(sink)
        live = list(announce._event_sinks)
        try:
            importlib.reload(announce)
            self.assertNotIn(sink, announce._event_sinks)   # the bug's cause
            commands.reattach_event_sinks(announce, live)
            self.assertIn(sink, announce._event_sinks)
        finally:
            announce._event_sinks[:] = [s for s in announce._event_sinks if s is not sink]


if __name__ == "__main__":
    unittest.main()
