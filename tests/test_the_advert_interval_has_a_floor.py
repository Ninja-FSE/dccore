"""#1272: ANNOUNCE_INTERVAL = 0 made the advert worker spin.

time.sleep(0) between cycles: the worker rebuilt and queued the advert in a
tight loop, about 1,700 lines a second, one core busy, and the channel got
an advert every MSG_DELAY instead of every five minutes. settings_file now
refuses the value (tests/test_the_settings_refuse_what_cannot_work.py);
this is the worker's own floor, for a value that reaches config without
coerce() - admin_config.py is Python.
"""

import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

FLOOR = settings_file.MINIMUMS["ANNOUNCE_INTERVAL"]


class TheIntervalTheWorkerSleeps(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, announce, "_floored_interval_reported",
                        announce._floored_interval_reported)
        announce._floored_interval_reported = None

    def interval(self, value):
        self.set_config(ANNOUNCE_INTERVAL=value)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            seconds = announce.advert_interval()
        return seconds, buffer.getvalue()

    def test_zero_gets_the_floor(self):
        seconds, said = self.interval(0)
        self.assertEqual(seconds, FLOOR)
        self.assertIn("below the minimum", said)

    def test_negative_and_not_a_number_get_the_floor(self):
        for value in (-5, "soon", None, float("nan")):
            with self.subTest(value=value):
                self.assertEqual(self.interval(value)[0], FLOOR)

    def test_a_real_interval_is_kept(self):
        self.assertEqual(self.interval(300), (300, ""))
        self.assertEqual(self.interval(FLOOR), (FLOOR, ""))

    def test_the_floor_is_said_once_not_every_cycle(self):
        _seconds, first = self.interval(0)
        _seconds, second = self.interval(0)
        self.assertIn("below the minimum", first)
        self.assertEqual(second, "")


class TheWorkerSleepsThatInterval(DCCoreTestCase):
    """One real cycle of announce_worker(): the wait after the channel loop
    must be advert_interval()'s answer, not the raw setting."""

    def setUp(self):
        super().setUp()
        for owner, name in ((announce, "current_worker_id"), (announce, "is_ready"),
                            (announce, "advert_interval"),
                            (library, "list_name_for_request"),
                            (list_mod, "get_file_count_date_size_and_raw_bytes")):
            self.addCleanup(setattr, owner, name, getattr(owner, name))
        library.list_name_for_request = lambda channel=None: "somelist"
        list_mod.get_file_count_date_size_and_raw_bytes = (
            lambda name: (10, "2026-09-01", "1.0GB", 10 ** 9))
        self.set_config(ANNOUNCE_INTERVAL=0, CHANNEL="#example-room")
        self.asked = threading.Event()

        def recorded():
            self.asked.set()
            return 0.01
        announce.advert_interval = recorded

    def test_the_cycle_ends_by_asking_for_the_floored_interval(self):
        announce.is_ready = True
        thread = threading.Thread(target=announce.announce_worker, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.asked.wait(5),
                            "the worker never asked advert_interval() how long to wait")
        finally:
            announce.current_worker_id = object()
            thread.join(5)
        self.assertFalse(thread.is_alive(), "the advert worker must be gone")


if __name__ == "__main__":
    unittest.main()
