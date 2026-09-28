"""A request queued at another bot is asked again after a restart (#978).

A restart QUITs, and a file server drops the queue of a user who quits. Rows
kept as "queued" across the restart waited for files that were never coming -
and counted toward FETCH_MAX_PER_BOT while they did, so every other request to
that bot waited "their-turn" until FETCH_QUEUED_TIMEOUT failed them, twelve
hours later. Now they come back pending and are asked again; a server that did
keep a request says "already in my queue", and the row is queued again.
"""

import time
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_fetch_queue_waits_and_paces_itself as paces  # noqa: E402


class AfterARestart(paces.QueueCase):
    def restart(self):
        for rid, row in list(config.fetch_queue.items()):
            config.fetch_queue[rid] = dcc_fetch._restart_form(row)

    def test_the_other_requests_are_not_held_behind_them(self):
        """The audit's case: three queued there, the rest waiting their turn."""
        rids = self.queue_up(6)
        for rid in rids[:3]:
            config.fetch_queue[rid].update(state="queued", queued_at=time.time(), queue_position=5,
                                           reply="Added ... at position #5")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rids[3]]["waiting"], "their-turn", "the premise")

        self.restart()
        self.oserve.queued.clear()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"] * 3 + ["pending"] * 3)
        self.assertEqual(len(self.asked()), 3, "the three asked again, oldest first")

    def test_a_request_the_server_kept_is_queued_again(self):
        (rid,) = self.queue_up(1)
        config.fetch_queue[rid].update(state="queued", queued_at=time.time(), queue_position=5)
        self.restart()
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")
        self.assertNotIn("queue_position", config.fetch_queue[rid])

        dcc_fetch.check_fetch_queue()
        dcc_fetch.handle_bot_reply(
            "ServerOne", "Request Denied - You Already Have Track 00.flac In My Queue - Position 5 - OmenServE v2.60")
        self.assertEqual(config.fetch_queue[rid]["state"], "queued")
        self.assertEqual(config.fetch_queue[rid]["queue_position"], 5)


if __name__ == "__main__":
    unittest.main()
