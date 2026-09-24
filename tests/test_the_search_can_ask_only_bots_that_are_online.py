"""#926 item 7: the cross-list search can be limited to bots that are online.

AutoGet's "Online Only": of every list held, only the ones whose bot is in one
of our channels right now - the ones a request can reach today. The others are
reported with the lists that had no match, so the sidebar dims them.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.test_crosslist_search import IndexCase  # noqa: E402


class OnlineOnly(IndexCase):
    def setUp(self):
        super().setUp()
        self.index("ServerOne", "01 - Opening Song.flac")
        self.index("ServerTwo", "02 - Opening Song.flac")
        self.hold("ServerOne", "ServerTwo")
        config.channel_users["#chan"] = {"serverone", "someuser"}

    def bots(self, payload):
        return sorted({group["bot"] for group in payload["folders"]})

    def test_off_every_list_is_searched(self):
        payload = webserver.build_crosslist_search_payload("opening song")
        self.assertEqual(self.bots(payload), ["ServerOne", "ServerTwo"])

    def test_on_only_bots_in_a_channel_are(self):
        payload = webserver.build_crosslist_search_payload("opening song", online_only=True)
        self.assertEqual(self.bots(payload), ["ServerOne"])
        self.assertIn("servertwo", payload["empty"], "the sidebar dims it")
        self.assertNotIn("servertwo", payload["matched"])

    def test_with_nobody_online_nothing_matches_and_everything_is_dimmed(self):
        config.channel_users["#chan"] = {"someuser"}
        payload = webserver.build_crosslist_search_payload("opening song", online_only=True)
        self.assertEqual(payload["folders"], [])
        self.assertEqual(sorted(payload["empty"]), ["serverone", "servertwo"])


class TheRouteAndThePage(unittest.TestCase):
    def read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_route_passes_the_switch(self):
        self.assertIn('online_only=request.args.get("online", "") in ("1", "true")',
                      self.read("webserver.py"))

    def test_the_page_sends_it(self):
        self.assertIn('(state.filelistsOnlineOnly ? "&online=1" : "")', self.read("web", "app.js"))
        self.assertIn('id="filelists-online-only"', self.read("web", "index.html"))


if __name__ == "__main__":
    unittest.main()
