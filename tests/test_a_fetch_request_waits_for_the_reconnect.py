"""A fetch request that has not gone out survives a lost connection (#1044).

Requests wait in a lane of their own (#1028), and the row that owns one counts
it as sent once it has left that lane. A disconnect, or a failed send, used to
empty the whole lane, so every row whose request had not gone out read it as
sent and timed out as "no response" - a folder or list row after up to 30
minutes, with no request ever made. Now the lines wait for the next connection,
a line whose send failed goes back to the front, and a line the lane's cap
trims sends its row back to pending.
"""

import re
import socket
import time
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as modules, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_vip_lane_gets_one_slot_per_pass as lanes  # noqa: E402
import tests.test_a_shared_outbound_pace as pace  # noqa: E402

LINE = "PRIVMSG #chan :!ServerOne !rar Artist - Album\r\n"


def an_offered_row(line=LINE, request_type="folder", text="!rar Artist - Album", bot="ServerOne"):
    rid = dcc_fetch.enqueue_fetch(bot, text, request_type=request_type)
    config.fetch_queue[rid].update(state="offered", offered_at=time.time(), request_line=line,
                                   filename="Artist_-_Album.rar")
    return rid


class TheRowsAreToldTheirLineDidNotGo(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={})

    def test_a_row_whose_line_was_dropped_is_asked_again(self):
        rid = an_offered_row()
        self.assertEqual(dcc_fetch.requests_not_sent([LINE]), 1)
        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "pending")
        self.assertNotIn("request_line", row)
        self.assertEqual(row["filename"], "!rar Artist - Album", "asked for as asked (#963)")

    def test_another_rows_line_or_a_row_that_moved_on_is_left_alone(self):
        rid = an_offered_row()
        config.fetch_queue[rid]["state"] = "receiving"
        self.assertEqual(dcc_fetch.requests_not_sent([LINE, "PRIVMSG #chan :@Other\r\n"]), 0)
        self.assertEqual(config.fetch_queue[rid]["state"], "receiving")


class FailingOnce(pace.TimestampedSocket):
    def __init__(self):
        super().__init__()
        self.failed = False

    def sendall(self, payload):
        if not self.failed:
            self.failed = True
            raise socket.error("Connection reset by peer")
        return super().sendall(payload)


class ThePump(lanes._WorkerCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={})
        config.fetch_request_queue = []

    def test_a_line_whose_send_failed_goes_out_on_the_next_try(self):
        self.sock = FailingOnce()
        self.oserve.irc_connection = self.sock
        config.fetch_request_queue.extend([LINE, "PRIVMSG #chan :@ServerTwo\r\n"])
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: len(self.sent()) >= 2), self.sent())
        self.assertEqual(self.sent()[:2], [LINE, "PRIVMSG #chan :@ServerTwo\r\n"])

    def test_a_line_the_cap_trims_sends_its_row_back(self):
        self.set_config(MAX_VIP_QUEUE=1)
        config.activation_triggered = False          # nothing is sent: the cap still runs
        oldest = an_offered_row()
        newest_line = "PRIVMSG #chan :@ServerTwo\r\n"
        newest = an_offered_row(newest_line, "list", "", bot="ServerTwo")
        config.fetch_request_queue.extend([LINE, newest_line])
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: config.fetch_queue[oldest]["state"] == "pending"))
        self.assertEqual(config.fetch_queue[newest]["state"], "offered")
        self.assertEqual(config.fetch_request_queue, [newest_line])


class TheReconnectKeepsThem(unittest.TestCase):
    def test_the_disconnect_epilogue_does_not_empty_the_lane(self):
        with open(support.REPO_ROOT + "/src/irc.py", encoding="utf-8") as handle:
            code = [line.split("#", 1)[0] for line in handle.read().split("\n")]
        self.assertFalse([line for line in code if re.search(r"del\s+config\.fetch_request_queue", line)])


if __name__ == "__main__":
    unittest.main()
