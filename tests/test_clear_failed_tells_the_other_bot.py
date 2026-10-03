"""Clear failed lets the other bot go of a request it may still hold (#1047).

A file given up on for silence ("no response") may still sit in a busy DCCore
peer's queue, and its own Delete asks that peer to remove it. The Downloads
page's "Clear failed" forgot such rows here only, so the peer sent the file
when its turn came and it was refused as unsolicited, the peer's send slot
wasted. Clear now asks, as Delete does - only for those rows, and never for a
request that never left.
"""

import time
import unittest
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402


class ClearFailed(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={})
        config.fetch_request_queue = []
        patch = mock.patch.object(dcc_fetch, "drop_our_request_at", return_value=True)
        self.drop = patch.start()
        self.addCleanup(patch.stop)

    def row(self, name, **fields):
        rid = dcc_fetch.enqueue_fetch("PeerBot", name)
        config.fetch_queue[rid].update(fields)
        return rid

    def clear(self, which="failed"):
        return webserver.build_fetch_clear_result({"which": which})

    def test_a_request_given_up_on_for_silence_is_let_go_at_the_peer(self):
        self.row("Silent.flac", state="failed", reason="no response", offered_at=time.time())
        self.assertEqual(self.clear(), (200, {"cleared": 1}))
        self.drop.assert_called_once_with("PeerBot", "Silent.flac")

    def test_a_request_that_failed_for_another_reason_is_not(self):
        self.row("Refused.flac", state="failed", reason="not found")
        self.clear()
        self.drop.assert_not_called()

    def test_a_request_that_never_left_is_not(self):
        rid = self.row("Unsent.flac", state="failed", reason="no response",
                       request_line="PRIVMSG #chan :!PeerBot Unsent.flac\r\n")
        config.fetch_request_queue.append(config.fetch_queue[rid]["request_line"])
        self.clear()
        self.drop.assert_not_called()
        self.assertEqual(config.fetch_request_queue, [], "its line is taken back")

    def test_clearing_downloaded_rows_tells_nobody(self):
        self.row("Done.flac", state="complete")
        self.clear("complete")
        self.drop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
