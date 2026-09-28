"""A row enqueued while the dispatcher looked around waits for the next tick (#970).

check_fetch_queue() notes which bots have rows pending, lets go of the lock
to read their presence, their pauses and the disk, then takes it again to send
requests. A request queued in that gap was not among what had been read, and
counted as ready: sent to a bot the operator had just paused, or onto a nearly
full disk when nothing else had been pending, so the disk was never looked at.
"""

import shutil
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_fetch_queue_waits_and_paces_itself as paces  # noqa: E402


class InTheGap(paces.QueueCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(dcc_fetch._paused.clear)
        self.real_readiness = dcc_fetch._bot_readiness
        self.addCleanup(setattr, dcc_fetch, "_bot_readiness", self.real_readiness)
        self.real_disk_is_low = dcc_fetch._disk_is_low
        self.addCleanup(setattr, dcc_fetch, "_disk_is_low", self.real_disk_is_low)

    def during_readiness(self, action):
        """Run `action` while the dispatcher is reading presence - off the lock."""
        def readiness(bots, now):
            ready = self.real_readiness(bots, now)
            action()
            return ready
        dcc_fetch._bot_readiness = readiness

    def state(self, rid):
        return config.fetch_queue[rid]["state"]

    def test_a_request_for_a_bot_just_paused_is_not_sent(self):
        added = []

        def enqueue_and_pause():
            added.append(dcc_fetch.enqueue_fetch("ServerTwo", "Late.flac"))
            dcc_fetch.pause_bot("ServerTwo", "the operator said so")

        self.queue_up(1)                     # ServerOne: something was pending
        self.during_readiness(enqueue_and_pause)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(added[0]), "pending")
        self.assertFalse(any("ServerTwo" in line for line in self.asked()))

        dcc_fetch._bot_readiness = self.real_readiness
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[added[0]]["waiting"], "paused")

    def test_a_request_onto_a_full_disk_is_not_sent(self):
        """Nothing pending at the snapshot: the disk was never looked at."""
        added = []
        real_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 12, 0, 1024)
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)
        self.during_readiness(lambda: added.append(dcc_fetch.enqueue_fetch("ServerOne", "Late.flac")))

        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(added[0]), "pending")
        self.assertEqual(self.asked(), [])

        dcc_fetch._bot_readiness = self.real_readiness
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[added[0]]["waiting"], "disk-full")

    def test_a_pause_after_the_readiness_was_read_still_holds(self):
        rid = self.queue_up(1)[0]

        def low_and_pause():
            dcc_fetch.pause_bot("ServerOne", "the operator said so")
            return False

        dcc_fetch._disk_is_low = low_and_pause
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(rid), "pending")
        self.assertEqual(config.fetch_queue[rid]["waiting"], "paused")

    def test_the_next_tick_sends_it(self):
        added = []
        self.during_readiness(lambda: added.append(dcc_fetch.enqueue_fetch("ServerTwo", "Late.flac")))
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(added[0]), "pending")
        dcc_fetch._bot_readiness = self.real_readiness
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(added[0]), "offered")


if __name__ == "__main__":
    unittest.main()
