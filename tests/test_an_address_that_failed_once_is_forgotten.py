"""_web_bad_ips never forgot an address that failed fewer than three times
(audit L13, #677).

Entries were deleted only by a successful login from the address or by an
expired block, and a block was only ever set at MAX_PASSWORD_ATTEMPTS
failures. An address with one or two had blocked_until == 0.0, never met
the expiry test, and stayed for the life of the process - on an
internet-exposed WEBUI_HOST bind, one entry per scanner that ever sent a
POST /login, for ever. adminchat's own pool for the DCC console had the
same shape.

Both pools record when the address last failed, and on every new failure
drop the addresses that never reached a block and have not failed inside
BAD_IP_BLOCK_SECONDS. A failure that old does not count towards a block
either: two typos a day apart are not an attack. Blocked addresses are
left to the expiry that already forgets them.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import webserver  # noqa: E402

T0 = 1_700_000_000.0
WINDOW = adminchat.BAD_IP_BLOCK_SECONDS


class _Clock:
    def __init__(self, module):
        self.module, self.now = module, T0
        self._real = module.time.time
        module.time.time = lambda: self.now

    def restore(self):
        self.module.time.time = self._real


class TheWebPool(unittest.TestCase):
    pool, lock = webserver._web_bad_ips, webserver._web_bad_ips_lock
    note, is_bad = staticmethod(webserver._note_bad_web_login), staticmethod(webserver._is_bad_web_ip)
    clocked = webserver

    def setUp(self):
        self.pool.clear()
        self.addCleanup(self.pool.clear)
        self.clock = _Clock(self.clocked)
        self.addCleanup(self.clock.restore)

    def test_the_audits_scan_does_not_grow_the_pool_for_ever(self):
        for n in range(2000):
            self.note("10.%d.%d.%d" % (n >> 16 & 255, n >> 8 & 255, n & 255))
        self.assertEqual(len(self.pool), 2000)

        self.clock.now = T0 + WINDOW
        self.note("192.0.2.1")

        self.assertEqual(sorted(self.pool), ["192.0.2.1"])

    def test_an_address_that_failed_twice_is_forgotten_after_the_window(self):
        self.note("192.0.2.1")
        self.note("192.0.2.1")
        self.clock.now = T0 + WINDOW
        self.note("192.0.2.2")

        self.assertNotIn("192.0.2.1", self.pool)

    def test_and_kept_inside_it(self):
        self.note("192.0.2.1")
        self.clock.now = T0 + WINDOW - 1
        self.note("192.0.2.2")

        self.assertIn("192.0.2.1", self.pool)

    def test_old_failures_do_not_count_towards_a_block(self):
        """Two typos a day apart, then one more: not three in a row."""
        self.note("192.0.2.1")
        self.note("192.0.2.1")
        self.clock.now = T0 + 86400
        self.note("192.0.2.1")

        self.assertFalse(self.is_bad("192.0.2.1"))
        self.assertEqual(self.pool["192.0.2.1"][0], 1)

    def test_three_inside_the_window_still_block(self):
        """The control: the throttle itself is not weakened."""
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.note("192.0.2.1")

        self.assertTrue(self.is_bad("192.0.2.1"))
        # and once the block has run out, the expiry that was always there
        # forgets it
        self.clock.now = T0 + WINDOW + 1
        self.assertFalse(self.is_bad("192.0.2.1"))
        self.assertNotIn("192.0.2.1", self.pool)

    def test_a_blocked_address_is_left_to_the_expiry(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.note("192.0.2.1")
        self.clock.now = T0 + WINDOW - 1
        self.note("192.0.2.2")

        self.assertIn("192.0.2.1", self.pool, "swept while still blocked")
        self.assertTrue(self.is_bad("192.0.2.1"))


class TheConsolePool(TheWebPool):
    """adminchat's own pool, the same policy."""
    pool, lock = adminchat._bad_ips, adminchat._bad_lock
    note, is_bad = staticmethod(adminchat.note_bad_ip), staticmethod(adminchat.is_bad_ip)
    clocked = adminchat

    def setUp(self):
        import contextlib
        import io
        super().setUp()
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.addCleanup(self._quiet.__exit__, None, None, None)


class TheSweepOnItsOwn(unittest.TestCase):

    def test_an_entry_from_before_the_stamp_is_treated_as_fresh(self):
        """A two-field entry (an older shape) is not thrown away by the
        first sweep that sees it."""
        pool = {"a": [1, 0.0]}

        self.assertEqual(adminchat.forget_stale_failures(pool, T0), 0)
        self.assertIn("a", pool)

    def test_it_reports_how_many_went(self):
        pool = {"a": [1, 0.0, T0 - WINDOW], "b": [2, 0.0, T0 - 1], "c": [3, T0 + 100, T0 - WINDOW]}

        self.assertEqual(adminchat.forget_stale_failures(pool, T0), 1)
        self.assertEqual(sorted(pool), ["b", "c"])


if __name__ == "__main__":
    unittest.main()
