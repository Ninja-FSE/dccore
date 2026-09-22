"""A search's header - "Found: 3 Match(es) ..." - is the first line the
requester receives, not the last.

The header is a private message to one user, and the rows that follow it are
queued in that user's own lane. It used to be queued as "channel_announce",
which is the VIP lane the channel advert shares. queue_mgr serves the two
lanes in strict turns (one VIP line, one standard line), so while an advert
cycle had lines queued the header waited behind them and came out after its
own results. Seen live: the header at 07:22:47 under three results from
07:22:07, 07:22:17 and 07:22:27.
"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import queue_mgr  # noqa: E402
from tests.support import silence_debug  # noqa: E402


def real_oserve():
    spec = importlib.util.spec_from_file_location(
        "oserve_under_test", os.path.join(REPO_ROOT, "oserve.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheHeaderIsInTheRequestersOwnLane(unittest.TestCase):

    def setUp(self):
        silence_debug(announce)
        self.oserve = real_oserve()
        patcher = mock.patch.dict(sys.modules, {"oserve": self.oserve})
        patcher.start()
        self.addCleanup(patcher.stop)
        config.vip_queue.clear()
        config.send_queue.clear()
        self.addCleanup(config.vip_queue.clear)
        self.addCleanup(config.send_queue.clear)

    def queue_a_search_reply(self, rows=3):
        announce.send_search_result_header("dave", "metallica", rows, "#dccore-test")
        for n in range(rows):
            self.oserve.queue_message("dave", f"PRIVMSG dave :row {n}\r\n")

    def test_it_is_not_in_the_shared_vip_lane(self):
        self.queue_a_search_reply()

        self.assertEqual(list(config.vip_queue), [],
                         "the header is a private message, not a channel line")

    def test_it_is_queued_ahead_of_its_own_rows(self):
        self.queue_a_search_reply()

        lane = config.send_queue["dave"]
        self.assertIn("Search Result", lane[0])
        self.assertEqual(lane[1:], [f"PRIVMSG dave :row {n}\r\n" for n in range(3)])

    def test_it_is_served_first_even_with_an_advert_backlog(self):
        """The reproduction: thirteen advert lines already waiting in the VIP
        lane, then a search. Serving the way queue_mgr does - one VIP line,
        one standard line, in turn - the requester's first line is the header."""
        for n in range(13):
            self.oserve.queue_message("channel_announce", f"PRIVMSG #chan{n} :advert\r\n")
        self.queue_a_search_reply()

        served_to_dave = []
        last_served = None
        while config.vip_queue or config.send_queue:
            if config.vip_queue:
                config.vip_queue.pop(0)
            picked = queue_mgr.next_standard_line(config.send_queue, last_served)
            if picked is not None:
                user, msg = picked
                last_served = user
                served_to_dave.append(msg)

        self.assertIn("Search Result", served_to_dave[0])
        self.assertEqual(len(served_to_dave), 4)


if __name__ == "__main__":
    unittest.main()
