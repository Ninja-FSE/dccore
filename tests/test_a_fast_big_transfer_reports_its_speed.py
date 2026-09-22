"""A big file sent in under a second was reported "at 0B/s".

Seen live on a LAN: 34-45 MB tracks finished in 0:00-0:01 and the window said
"34.3MB in 0:01 at 0B/s". The speed was real - about 35 MB/s. The one-second
floor for a reportable speed (stats_mgr.MIN_RECORD_SECONDS) exists because a file
that fits in the socket's send buffer is "sent" in one go and the clock measures
a memory copy. A file well past the buffer cannot be that, and since #526 the
clock stops at the receiver's final acknowledgement. So: a file of at least
twice the buffer, taking at least a tenth of a second, has a measurable speed.
A small file still does not, and neither does anything under a tenth of a
second. And when there is no speed, mIRC says "at n/a" rather than "0B/s".
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import stats_mgr  # noqa: E402

MB = 1024 * 1024


class TheRule(unittest.TestCase):

    def test_the_users_own_transfers_are_now_measurable(self):
        """34.3 MB in just under a second; 45 MB in a second and a bit."""
        self.assertTrue(stats_mgr.speed_is_measurable(0.96, 34.3 * MB))
        self.assertTrue(stats_mgr.speed_is_measurable(0.7, 36.8 * MB))
        self.assertTrue(stats_mgr.speed_is_measurable(0.4, 13 * MB))

    def test_a_small_file_is_still_not(self):
        self.assertFalse(stats_mgr.speed_is_measurable(0.5, 1 * MB))
        self.assertFalse(stats_mgr.speed_is_measurable(0.5, 4 * MB), "the buffer's own size")
        self.assertFalse(stats_mgr.speed_is_measurable(0.9, 7.9 * MB))

    def test_the_threshold_is_twice_the_default_buffer(self):
        self.assertEqual(stats_mgr.LARGE_TRANSFER_BYTES, 8 * MB)
        self.assertTrue(stats_mgr.speed_is_measurable(0.5, stats_mgr.LARGE_TRANSFER_BYTES))
        self.assertFalse(stats_mgr.speed_is_measurable(0.5, stats_mgr.LARGE_TRANSFER_BYTES - 1))

    def test_nothing_under_a_tenth_of_a_second_is_reported_however_big(self):
        self.assertFalse(stats_mgr.speed_is_measurable(0.05, 500 * MB))
        self.assertFalse(stats_mgr.speed_is_measurable(stats_mgr.MIN_LARGE_TRANSFER_SECONDS - 0.001, 500 * MB))
        self.assertTrue(stats_mgr.speed_is_measurable(stats_mgr.MIN_LARGE_TRANSFER_SECONDS, 500 * MB))

    def test_a_second_or_more_is_measurable_whatever_the_size(self):
        """Unchanged: the duration alone was always enough."""
        self.assertTrue(stats_mgr.speed_is_measurable(1.0))
        self.assertTrue(stats_mgr.speed_is_measurable(1.0, 1))
        self.assertTrue(stats_mgr.speed_is_measurable(12.0, None))

    def test_without_a_size_it_behaves_as_before(self):
        self.assertFalse(stats_mgr.speed_is_measurable(0.5))
        self.assertFalse(stats_mgr.speed_is_measurable(0.999))
        self.assertFalse(stats_mgr.speed_is_measurable(None, 100 * MB))

    def test_rubbish_is_not_measurable_and_does_not_raise(self):
        self.assertFalse(stats_mgr.speed_is_measurable("ages", 100 * MB))
        self.assertFalse(stats_mgr.speed_is_measurable(0.5, "big"))
        self.assertFalse(stats_mgr.speed_is_measurable(0.5, None))

    def test_the_record_keeps_its_own_floor(self):
        """This changes what is SHOWN. The record still needs a full second."""
        self.assertEqual(stats_mgr.MIN_RECORD_SECONDS, 1.0)


class TheWiring(unittest.TestCase):

    def test_the_send_passes_the_size(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("speed_is_measurable(acute_duration, file_size)", source)

    def test_the_record_call_is_unchanged(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("update_speed_record(final_calc_speed, acute_duration)", source)


class TheWindow(unittest.TestCase):

    def line(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
            text = handle.read().replace("\r\n", "\n")
        start = text.index("if (%type == SENT) {")
        return text[start:text.index("\n  }", start)]

    def test_no_speed_says_n_a_not_zero(self):
        self.assertIn("$iif($6 > 0,at $dccore.speed($6),at n/a)", self.line())

    def test_a_speed_is_still_shown_as_before(self):
        self.assertIn("$dccore.speed($6)", self.line())
        self.assertIn("in $dccore.dur($5)", self.line())


if __name__ == "__main__":
    unittest.main()
