"""The day's statistics rollover is checked once a day, not once a message.

#1132: rotate_the_day_without_stopping_the_bot() runs for every channel line
that passes the ban check - plain chatter included, not only commands - and
each run took runtime.disk_lock, opened and parsed stats.txt, and compared one
date. disk_lock is the single lock behind every db.py write, so a slow writer
holding it (record_download() on a large download_counts.json held it for
over half a second) stalled the IRC read thread on the next ordinary line.

Now the local date the rollover last succeeded for is remembered, and the rest
of that day's lines skip the check. What must not change is #592's failure
handling (see test_a_failed_midnight_rotation_does_not_stop_every_command.py):
a failed rollover is said once, retried once a minute, and never stops the
message being handled.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import irc  # noqa: E402


class TheRotationIsCheckedOnceADay(unittest.TestCase):

    def setUp(self):
        irc._day_rotation_failed_at = None
        self.addCleanup(setattr, irc, "_day_rotation_failed_at", None)
        irc._day_rotated_for = None
        self.addCleanup(setattr, irc, "_day_rotated_for", None)
        self.day = ["2026-10-03"]
        self.clock = [1000.0]
        real_strftime = irc.time.strftime

        def fake_strftime(fmt, *rest):
            # Only the date the gate asks for is faked; anything else that
            # formats a time meanwhile (a console timestamp) gets the real one.
            if fmt == "%Y-%m-%d" and not rest:
                return self.day[0]
            return real_strftime(fmt, *rest)

        for patcher in (mock.patch.object(irc.time, "strftime", fake_strftime),
                        mock.patch.object(irc.time, "monotonic", lambda: self.clock[0])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_day_of_messages_checks_the_disk_once(self):
        with mock.patch.object(db, "check_and_rotate_day") as rotate:
            for _ in range(500):
                self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
        rotate.assert_called_once()

    def test_the_first_message_of_a_new_day_checks_again(self):
        with mock.patch.object(db, "check_and_rotate_day") as rotate:
            irc.rotate_the_day_without_stopping_the_bot()
            irc.rotate_the_day_without_stopping_the_bot()
            self.day[0] = "2026-10-04"
            irc.rotate_the_day_without_stopping_the_bot()
            irc.rotate_the_day_without_stopping_the_bot()
        self.assertEqual(rotate.call_count, 2)

    def test_a_check_that_runs_across_midnight_counts_for_the_day_it_started(self):
        """The date is read BEFORE the rollover runs. Read after, a check that
        began at 23:59:59 would be recorded as the new day's, and the new day
        would never be rolled over from the read loop."""
        def crossing_midnight():
            self.day[0] = "2026-10-04"

        with mock.patch.object(db, "check_and_rotate_day", side_effect=crossing_midnight) as rotate:
            irc.rotate_the_day_without_stopping_the_bot()
            irc.rotate_the_day_without_stopping_the_bot()
        self.assertEqual(rotate.call_count, 2)
        self.assertEqual(irc._day_rotated_for, "2026-10-04")

    def test_a_failure_does_not_mark_the_day_and_is_retried_on_the_minute(self):
        failing = mock.patch.object(db, "check_and_rotate_day",
                                    side_effect=OSError(28, "No space left on device"))
        with failing as rotate, mock.patch("builtins.print"):
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())
            self.assertEqual(rotate.call_count, 1)
            self.clock[0] += irc.DAY_ROTATION_RETRY_SECONDS + 1
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())
            self.assertEqual(rotate.call_count, 2)
        self.assertIsNone(irc._day_rotated_for)
        self.clock[0] += irc.DAY_ROTATION_RETRY_SECONDS + 1
        with mock.patch.object(db, "check_and_rotate_day") as rotate:
            self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
            self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
        rotate.assert_called_once()

    def test_a_failure_on_the_next_day_is_retried_not_skipped(self):
        """Yesterday's success must not cover today: a rollover that fails
        after midnight is retried on the minute until it works."""
        with mock.patch.object(db, "check_and_rotate_day"):
            irc.rotate_the_day_without_stopping_the_bot()
        self.day[0] = "2026-10-04"
        with mock.patch.object(db, "check_and_rotate_day", side_effect=OSError("read-only")) as rotate, \
                mock.patch("builtins.print"):
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())
            self.clock[0] += irc.DAY_ROTATION_RETRY_SECONDS + 1
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())
        self.assertEqual(rotate.call_count, 2)
        self.assertEqual(irc._day_rotated_for, "2026-10-03")

    def test_the_day_is_the_local_date_in_the_form_db_compares(self):
        """db._rotate_day_unlocked() compares datetime.now() as %Y-%m-%d, so
        the gate must use the same local date and the same format."""
        formats = []

        real_strftime = irc.time.strftime

        def recording_strftime(fmt, *rest):
            if rest:
                return real_strftime(fmt, *rest)
            formats.append(fmt)
            return self.day[0]

        with mock.patch.object(irc.time, "strftime", recording_strftime), \
                mock.patch.object(db, "check_and_rotate_day"):
            irc.rotate_the_day_without_stopping_the_bot()
        self.assertEqual(formats, ["%Y-%m-%d"])


class TheMarkSurvivesARehash(unittest.TestCase):

    def test_the_mark_is_carried_across_a_module_reload(self):
        """irc.py is reloaded by !rehash. The mark is read back from the old
        module globals the same way _day_rotation_failed_at is."""
        with open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
        self.assertIn('_day_rotated_for = globals().get("_day_rotated_for")', lines)


if __name__ == "__main__":
    unittest.main()
