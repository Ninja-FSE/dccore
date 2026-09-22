"""#592: after midnight, a stats.txt write failure made the bot skip every command.

db.check_and_rotate_day() runs first in the per-message block and raises when
the day's rollover cannot be written (disk full, read-only data/, a file another
program holds). It raises on purpose, but the exception unwound the whole
block: no command, no admin command, no !rehash, no download request was
dispatched until the write worked - and the operator could not recover from IRC.

The rollover is bookkeeping. It is now caught at the call site, reported once a
minute, and retried on that cadence; the message is handled either way.
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


class TheRotationCheck(unittest.TestCase):

    def setUp(self):
        irc._day_rotation_failed_at = None
        self.addCleanup(setattr, irc, "_day_rotation_failed_at", None)

    def failing(self):
        return mock.patch.object(db, "check_and_rotate_day", side_effect=OSError(28, "No space left on device"))

    def test_a_failed_write_does_not_raise(self):
        with self.failing():
            self.assertFalse(irc.rotate_the_day_without_stopping_the_bot())

    def test_a_working_write_reports_that_it_ran(self):
        with mock.patch.object(db, "check_and_rotate_day") as rotate:
            self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
        rotate.assert_called_once()

    def test_the_failure_is_said_and_names_the_cause(self):
        with self.failing(), mock.patch("builtins.print") as said:
            irc.rotate_the_day_without_stopping_the_bot()
        text = " ".join(str(call.args[0]) for call in said.call_args_list)
        self.assertIn("No space left on device", text)
        self.assertIn("Commands carry on", text)

    def test_a_burst_of_messages_tries_once_and_says_it_once(self):
        with self.failing() as rotate, mock.patch("builtins.print") as said:
            for _ in range(50):
                irc.rotate_the_day_without_stopping_the_bot()
        self.assertEqual(rotate.call_count, 1)
        self.assertEqual(said.call_count, 1)

    def test_it_tries_again_after_the_interval(self):
        clock = [1000.0]
        with mock.patch.object(irc.time, "monotonic", lambda: clock[0]), self.failing() as rotate:
            irc.rotate_the_day_without_stopping_the_bot()
            clock[0] += irc.DAY_ROTATION_RETRY_SECONDS - 1
            irc.rotate_the_day_without_stopping_the_bot()
            self.assertEqual(rotate.call_count, 1)
            clock[0] += 2
            irc.rotate_the_day_without_stopping_the_bot()
            self.assertEqual(rotate.call_count, 2)

    def test_recovery_clears_the_failure(self):
        clock = [1000.0]
        with mock.patch.object(irc.time, "monotonic", lambda: clock[0]):
            with self.failing():
                irc.rotate_the_day_without_stopping_the_bot()
            clock[0] += irc.DAY_ROTATION_RETRY_SECONDS + 1
            with mock.patch.object(db, "check_and_rotate_day") as rotate:
                self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
                self.assertTrue(irc.rotate_the_day_without_stopping_the_bot())
            self.assertEqual(rotate.call_count, 2, "a working rotation is checked on every message again")

    def test_only_a_real_error_is_swallowed_not_a_keyboard_interrupt(self):
        with mock.patch.object(db, "check_and_rotate_day", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                irc.rotate_the_day_without_stopping_the_bot()


class TheReadLoop(unittest.TestCase):

    def test_the_message_block_uses_the_guarded_call(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("rotate_the_day_without_stopping_the_bot()\n", source)
        calls = [line for line in source.splitlines() if line.strip() == "db.check_and_rotate_day()"]
        self.assertEqual(len(calls), 1, "only the guarded helper may call the raising version")


class DbKeepsItsLoudContract(unittest.TestCase):
    def test_check_and_rotate_day_still_raises(self):
        """The helper exists so this can stay loud for every other caller."""
        with mock.patch.object(db, "_atomic_write", side_effect=OSError("disk full")), \
                mock.patch.object(db, "_rotate_day_unlocked", return_value=True), \
                mock.patch.object(db, "_load_advanced_stats_unlocked", return_value=[0] * 12):
            with self.assertRaises(OSError):
                db.check_and_rotate_day()


if __name__ == "__main__":
    unittest.main()
