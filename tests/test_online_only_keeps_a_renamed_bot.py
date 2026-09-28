"""Online only keeps a bot that is here under a new nick (#975).

#376 shows a bot that came back under another nick in one row, under the nick
it has now - but the list we hold stays keyed under the old nick, which is not
in the channel by definition. The sidebar's "Online only" and the search behind
it both asked about that old nick alone, so they hid exactly the renamed bots
that are here and reachable. The sidebar half is tested with the other sidebar
cases in test_online_only_filters_the_sidebar; this is the search's half.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402


class TheSearchKeepsIt(crosslist.IndexCase):
    def setUp(self):
        super().setUp()
        self.index("OldNick", "01 - Opening Song.flac")
        self.index("GoneBot", "02 - Opening Song.flac")
        self.hold("OldNick", "GoneBot")
        config.channel_users["#chan"] = {"newnick", "someuser"}

    def bots(self):
        payload = webserver.build_crosslist_search_payload("opening song", online_only=True)
        return sorted({group["bot"] for group in payload["folders"]})

    def test_a_bot_here_under_its_new_nick_is_searched(self):
        runtime.nick_aliases["oldnick"] = "NewNick"
        self.assertEqual(self.bots(), ["OldNick"])

    def test_without_the_merge_it_is_away(self):
        self.assertEqual(self.bots(), [])


if __name__ == "__main__":
    unittest.main()
