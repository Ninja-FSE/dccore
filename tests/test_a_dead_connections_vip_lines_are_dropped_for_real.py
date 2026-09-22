"""Disconnect reset a send_queue key that never exists, while the VIP lane
kept stale adverts and "Sending:" notices across a reconnect (audit L49,
#713).

Closed by #630 before this issue was filed: the dead reset of
send_queue["channel_announce"] is gone and the epilogue empties
config.vip_queue - the lane adverts, rejoins and command replies actually
use - since everything in it was written for the connection that just
died. #630 pinned that by reading the epilogue's text. Since #663 the
reconnect loop is driven for real against scripted connections, so this
executes it: a VIP line queued before the drop is gone after it, and the
epilogue says how many it dropped.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402

from tests import test_the_reconnect_backs_off as reconnect  # noqa: E402


class TheEpilogueDropsTheLane(reconnect.ASeriesOfConnections):

    def test_a_vip_line_written_for_the_dead_connection_does_not_survive_it(self):
        config.vip_queue[:] = ["PRIVMSG #somechannel :an advert with stale figures\r\n",
                               "NOTICE dave :Sending: a file the link took with it\r\n"]

        _waits, out = self.run_connections([reconnect.WELCOME])

        self.assertEqual(config.vip_queue, [])
        self.assertIn("[CONNECT] Dropped 2 queued VIP line(s) from the dead connection.", out)

    def test_an_empty_lane_says_nothing(self):
        config.vip_queue[:] = []

        _waits, out = self.run_connections([reconnect.WELCOME])

        self.assertNotIn("queued VIP line(s)", out)

    def test_the_standard_lanes_dead_key_is_not_what_is_touched(self):
        """The audit's dead reset: send_queue["channel_announce"] was set to
        [] on a key nothing writes. A user's standard lane, which is held
        across a reconnect on purpose (test_reconnect), is left alone."""
        config.send_queue["dave"] = ["NOTICE dave :held for the next link\r\n"]
        config.vip_queue[:] = ["PRIVMSG #somechannel :stale\r\n"]

        self.run_connections([reconnect.WELCOME])

        self.assertEqual(config.send_queue.get("dave"), ["NOTICE dave :held for the next link\r\n"])
        self.assertNotIn("channel_announce", config.send_queue)


for _name in [n for n in dir(reconnect.ASeriesOfConnections) if n.startswith("test")]:
    setattr(TheEpilogueDropsTheLane, _name, None)


if __name__ == "__main__":
    unittest.main()
