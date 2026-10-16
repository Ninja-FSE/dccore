"""LIST_REQUEST_TAG (#1293, was #1051): a short tag after a list request.

AutoQ's "Get Listfile" sends "@<nick>  ::AutoQ::", so a channel can see what
is fetching. DCCore can do the same, but only where it is safe: many server
scripts answer only the exact "@<nick>" and would silently never send the
list, so the tag goes only to bots known to be DCCore - and every DCCore
that can be recognised as one answers "@<nick>" followed by anything.

Plain text only, at most 32 characters, and never on a file or folder
request, whose lines AutoQ copies verbatim.

Every nick, tag and channel here is invented.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase, parse_source  # noqa: E402

BOT = "SomePeer"
KEY = BOT.lower()
TAG = "::SomeTag::"


class WhatATagMayBe(unittest.TestCase):

    def test_plain_text_is_fine(self):
        for value in ("", TAG, "fetching via SomeTag", "x" * settings_file.LIST_REQUEST_TAG_MAX):
            with self.subTest(value=value):
                self.assertIsNone(settings_file.request_tag_problem(value))

    def test_too_long_is_refused(self):
        problem = settings_file.request_tag_problem("x" * (settings_file.LIST_REQUEST_TAG_MAX + 1))
        self.assertIn("the most is 32", problem)

    def test_a_colour_code_or_a_line_break_is_refused(self):
        for value in ("\x0304red", "bold\x02", "two\r\nlines", "ctcp\x01", "del\x7f"):
            with self.subTest(value=value):
                self.assertIn("control character", settings_file.request_tag_problem(value))

    def test_a_save_refuses_it_and_accepts_plain_text(self):
        with self.assertRaises(ValueError):
            settings_file.coerce("LIST_REQUEST_TAG", "\x0304red", "")
        self.assertEqual(settings_file.coerce("LIST_REQUEST_TAG", "  " + TAG + " ", ""), TAG)

    def test_it_is_off_out_of_the_box(self):
        self.assertEqual(_default_of("LIST_REQUEST_TAG"), "")


def _default_of(name):
    """The value defaults.py itself assigns, not whatever a test left."""
    import ast
    with open(os.path.join(REPO_ROOT, "src", "defaults.py"), encoding="utf-8") as handle:
        tree = parse_source(handle.read())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not assigned in defaults.py")


class Peers(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(LIST_REQUEST_TAG=TAG, fetch_queue={}, MAX_FETCH_SLOTS=10,
                        fetch_feature_disabled=False, CHANNEL="#somechannel", FETCH_MAX_PER_BOT=10,
                        fetch_request_queue=[])
        config.channel_users["#somechannel"] = {KEY, "someuser"}
        saved = dict(runtime.chat_peers)
        self.addCleanup(lambda: (runtime.chat_peers.clear(), runtime.chat_peers.update(saved)))
        runtime.chat_peers.clear()

    def make_peer(self, nick=KEY):
        with runtime.chat_lock:
            runtime.chat_peers[nick] = {"#somechannel": 0}

    def asked(self):
        return [msg for _user, msg, *_ in self.oserve.queued if "PRIVMSG" in msg]


class TheLineThatGoesOut(Peers):

    def test_a_dccore_peer_gets_the_tag_two_spaces_after(self):
        self.make_peer()
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{BOT}  {TAG}\r\n"])

    def test_any_other_bot_gets_the_bare_request(self):
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{BOT}\r\n"])

    def test_empty_means_no_tag_even_for_a_peer(self):
        self.make_peer()
        config.LIST_REQUEST_TAG = ""
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{BOT}\r\n"])

    def test_a_tag_read_in_with_a_colour_code_is_not_sent(self):
        # settings.conf is read as text even where a save would refuse it.
        self.make_peer()
        config.LIST_REQUEST_TAG = "\x0304" + TAG
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :@{BOT}\r\n"])

    def test_a_file_or_folder_request_never_carries_it(self):
        self.make_peer()
        dcc_fetch.enqueue_fetch(BOT, "Some Track.mp3")
        dcc_fetch.enqueue_fetch(BOT, "!rar Some Folder", request_type="folder")
        dcc_fetch.check_fetch_queue()

        asked = self.asked()
        self.assertEqual(len(asked), 2, asked)
        for line in asked:
            self.assertNotIn(TAG, line)

    def test_the_peer_is_matched_whatever_the_case(self):
        self.make_peer("somepeer")
        self.assertEqual(dcc_fetch.list_request_tag("SOMEPEER"), TAG)


class AnEarlierAskIsTakenBackEitherWay(Peers):
    """#1028: asking again replaces a line still waiting to go out. A bot can
    become a known peer between two asks, so the earlier line may be the
    other form."""

    def test_a_bare_line_waiting_is_taken_back_by_a_tagged_ask(self):
        bare = f"PRIVMSG #somechannel :@{BOT}\r\n"
        config.fetch_request_queue.append(bare)
        self.make_peer()
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertNotIn(bare, config.fetch_request_queue)

    def test_a_tagged_line_waiting_is_taken_back_by_a_bare_ask(self):
        tagged = f"PRIVMSG #somechannel :@{BOT}  {TAG}\r\n"
        config.fetch_request_queue.append(tagged)
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        dcc_fetch.check_fetch_queue()

        self.assertNotIn(tagged, config.fetch_request_queue)

    def test_with_no_tag_set_there_is_no_other_form(self):
        config.LIST_REQUEST_TAG = ""
        self.assertEqual(dcc_fetch.list_request_lines("#somechannel", BOT, BOT),
                         (f"PRIVMSG #somechannel :@{BOT}\r\n", []))


class ADCCoreBotAnswersTheTaggedForm(DCCoreTestCase):
    """The reason it is safe to send to a peer: the receiving side."""

    def test_the_tagged_request_is_a_list_request(self):
        self.set_config(NICKNAME="SomePeer")
        msg = f"@SomePeer  {TAG}"
        self.assertTrue(irc.is_list_request(msg, msg.lower()))


if __name__ == "__main__":
    unittest.main()
