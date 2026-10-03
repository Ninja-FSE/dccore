"""A row held for disk room forgets its old place in the other bot's queue (#1043).

A file that waited hours queued at a busy bot, then did not fit when its turn
came, went back to pending with its old queued_at. Asked again once space was
freed and queued anew, it kept that stamp - handle_bot_reply() only sets one
that is missing - and FETCH_QUEUED_TIMEOUT failed the fresh place soon after.
"""

import time
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_a_file_too_big_for_the_disk_waits_for_room as room  # noqa: E402

HOURS = 3600.0


class HeldThenQueuedAgain(room.RoomCase):
    def test_the_new_queue_place_is_timed_from_now(self):
        rid = self.ask("file", "Big.flac")
        row = config.fetch_queue[rid]
        row.update(state="queued", queued_at=time.time() - 11.5 * HOURS, queue_position=3, reply="Added")
        self.offer(int(1.5 * room.GB), "Big.flac")          # its turn: does not fit
        self.assertEqual(row["state"], "pending")
        for stale in ("queued_at", "queue_position", "reply"):
            self.assertNotIn(stale, row)

        self.free = 2 * room.GB
        dcc_fetch.check_fetch_queue()
        dcc_fetch.handle_bot_reply("ServerOne", "Added Big.flac to your personal queue at position #55 of 60.")
        self.assertEqual(row["state"], "queued")
        self.assertLess(time.time() - row["queued_at"], 60, "the new place, not the old one")
        self.assertEqual(row["queue_position"], 55)


if __name__ == "__main__":
    unittest.main()
