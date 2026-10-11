"""A stranger's private message costs no disk write on the IRC read thread,
and the per-sender cooldown table does not grow for ever.

A private message the bot does not answer is recorded for the Messages page
(announce.record_private_message(), on the read thread). Two costs came with
every new sender:

- an fsync'd write of the whole private-messages file, under the shared disk
  lock, before the next server line was read - one per message, although the
  page keeps 50 rows;
- one more entry in the cooldown table, which nothing ever removed: a set of
  clones cycling through nicks grew it for as long as the bot ran.

The write now happens on a timer a moment later, one write for every message
recorded before it fires, and a sender is forgotten once its cooldown is over
(or, past a cap, oldest first).
"""

import os
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class PmCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()
        self.addCleanup(announce._pm_last_recorded.clear)
        self.set_config(PRIVATE_MESSAGE_COOLDOWN_SECONDS=300)
        self.writes = []
        real = db.save_private_messages

        def counting(rows, state):
            # Counted once the write is DONE, so a test waiting for a count
            # can read the file as soon as it sees one.
            result = real(rows, state)
            self.writes.append(threading.current_thread())
            return result
        db.save_private_messages = counting
        self.addCleanup(setattr, db, "save_private_messages", real)

    def record_many(self, count, prefix="alfa"):
        for index in range(count):
            announce.record_private_message("%s%d" % (prefix, index), "are you there?")


class NoWriteOnTheThreadThatRecords(PmCase):

    def test_a_burst_of_strangers_writes_nothing_here_and_once_later(self):
        self.set_config()
        real_delay = announce.PRIVATE_MESSAGES_SAVE_DELAY
        announce.PRIVATE_MESSAGES_SAVE_DELAY = 3600.0   # the timer must not race the count
        self.addCleanup(setattr, announce, "PRIVATE_MESSAGES_SAVE_DELAY", real_delay)

        self.record_many(500)

        self.assertEqual(self.writes, [])
        self.assertEqual(len(config.private_messages), announce.PRIVATE_MESSAGES_MAX)
        self.assertTrue(announce.flush_private_messages())
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(announce.flush_private_messages())
        self.assertEqual(len(self.writes), 1)

    def test_the_timer_writes_them_from_its_own_thread(self):
        real_delay = announce.PRIVATE_MESSAGES_SAVE_DELAY
        announce.PRIVATE_MESSAGES_SAVE_DELAY = 0.05
        self.addCleanup(setattr, announce, "PRIVATE_MESSAGES_SAVE_DELAY", real_delay)

        announce.record_private_message("alfa", "hello")
        announce.record_private_message("bravo", "hello")
        deadline = time.monotonic() + 10
        while not self.writes and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(self.writes), 1, "the timer never wrote")
        self.assertIsNot(self.writes[0], threading.current_thread())
        self.writes[0].join(5.0)   # it has written; let it finish before the leak check
        rows, _state = db.load_private_messages()
        self.assertEqual([row["nick"] for row in rows], ["alfa", "bravo"])
        self.assertFalse(runtime.private_messages_save_pending)


class TheCooldownTableIsBounded(PmCase):

    def setUp(self):
        super().setUp()
        real_delay = announce.PRIVATE_MESSAGES_SAVE_DELAY
        announce.PRIVATE_MESSAGES_SAVE_DELAY = 3600.0
        self.addCleanup(setattr, announce, "PRIVATE_MESSAGES_SAVE_DELAY", real_delay)

    def test_many_senders_inside_one_cooldown_stop_at_the_cap(self):
        cap = announce.PRIVATE_MESSAGE_SENDERS_REMEMBERED

        self.record_many(cap + 500)

        self.assertEqual(len(announce._pm_last_recorded), cap)
        # The newest are the ones kept: they are the ones still cooling down.
        self.assertIn("alfa%d" % (cap + 499), announce._pm_last_recorded)
        self.assertNotIn("alfa0", announce._pm_last_recorded)

    def test_a_sender_whose_cooldown_is_over_is_forgotten(self):
        announce._pm_last_recorded["alfa"] = time.time() - 301

        announce.record_private_message("bravo", "hello")

        self.assertEqual(list(announce._pm_last_recorded), ["bravo"])

    def test_a_sender_recorded_again_moves_to_the_back(self):
        """Oldest first is what lets the front be dropped without a scan: a
        sender recorded again must not keep its old place there, holding up
        everyone whose cooldown ended behind it."""
        now = time.time()
        announce._pm_last_recorded["alfa"] = now - 400
        announce._pm_last_recorded["bravo"] = now - 350

        announce.record_private_message("alfa", "still there?")

        self.assertEqual(list(announce._pm_last_recorded), ["alfa"])

    def test_the_cooldown_still_holds_back_a_repeat(self):
        self.assertIsNotNone(announce.record_private_message("alfa", "hello"))
        self.assertIsNone(announce.record_private_message("ALFA", "hello?"))
        self.assertEqual(len(config.private_messages), 1)

    def test_with_no_cooldown_nothing_is_remembered(self):
        self.set_config(PRIVATE_MESSAGE_COOLDOWN_SECONDS=0)

        self.record_many(50)

        self.assertEqual(announce._pm_last_recorded, {})
        self.assertEqual(len(config.private_messages), 50)


if __name__ == "__main__":
    import unittest
    unittest.main()
