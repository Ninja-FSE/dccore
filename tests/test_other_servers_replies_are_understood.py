"""#926: what another file server says about our request is understood.

"You are number 12 in my queue" used to be ignored: the request failed after
FETCH_OFFER_TIMEOUT as "no response", and the file that came an hour later was
refused as unsolicited. Now the request is marked queued there and still takes
its file; "I don't have that" and "queue full" end it at once with their own
words; and nothing a bot says can move a request sent to another bot.

The lines below are built on the phrases Autoget 7.40 recognised (the words
it matched, in order); nicks and filenames are invented.
"""

import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import fetch_replies  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class WhatALineMeans(unittest.TestCase):
    CASES = [
        # DCCore, colour blocks and all
        ("\x0300,10 \x0301,04 \x0300,01 Added Song One.flac to your personal queue at position #3 of 100.\x0f",
         "queued", 3),
        ("Error: File not found.", "refused", None),
        ("Error: The server's global queue is full (200 max).", "busy", None),
        ("System Message: MasterList is currently rebuilding. File requests temporarily paused.", "busy", None),
        # OmeNServE
        ("Request Accepted - File: Song One.mp3 - Position: 4 - Queued 1/3 OmeNServE v2.60", "queued", 4),
        ("Request Denied - I Don't Have Song One.mp3, Check Your Spelling Or Get My Newest List - OmenServE v2.60",
         "refused", None),
        ("Request Denied - You Already Have Song One.mp3 In My Queue - Position 2 - OmenServE v2.60",
         "duplicate", 2),
        ("Request Denied - You need sharing to use me - OmenServE v2.60", "refused", None),
        ("Removed From Queue: Song One.mp3 - File Has Been Moved Or Deleted - OmeNServE v2.60", "refused", None),
        # SDFind
        ("I have added Song One.mp3 in my que. You are 5 in my que. this makes 1 you are allowed 3",
         "queued", None),
        ("I don't have Song One.mp3. Please check your spelling or get my newest list by typing @Server in the channel.",
         "refused", None),
        # BWI
        ("Sorry, but Song One.mp3 is not found", "refused", None),
        # SpR Jukebox
        ("I am totally maxed out even in que list. Try it later.", "busy", None),
        ("All Available Que are Taken", "busy", None),
        ("You are in que now with Song One.mp3 in rank. 7", "queued", 7),
        ("Tu es Maintenant dans ta liste d'attente comme étant numéro 4", "queued", 4),
    ]

    def test_each_server_line(self):
        for line, outcome, position in self.CASES:
            with self.subTest(line=line[:50]):
                reply = fetch_replies.classify(line)
                self.assertIsNotNone(reply)
                self.assertEqual(reply.outcome, outcome)
                self.assertEqual(reply.position, position)

    def test_everything_else_is_nothing(self):
        for line in ("Thanks for stopping by!", "Queue: 3 files", "", "   ",
                     "Position Accepted Request OmenServE",   # the words, out of order
                     "\x01DCC SEND Song.mp3 1 2 3\x01"):
            with self.subTest(line=line):
                self.assertIsNone(fetch_replies.classify(line))


class FetchCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=3, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_QUEUED_TIMEOUT=43200)

    def offered(self, bot="ServerOne", name="Song One.mp3", request_type="file", ago=0):
        rid = dcc_fetch.enqueue_fetch(bot, name, request_type=request_type)
        row = config.fetch_queue[rid]
        row["state"] = "offered"
        row["offered_at"] = time.time()
        row["requested_at"] = time.time() - 100 + ago
        return rid

    def row(self, rid):
        return config.fetch_queue[rid]


class WhatAReplyDoesToTheRequest(FetchCase):
    def test_queued_there_stops_holding_a_slot_and_keeps_its_position(self):
        rid = self.offered()
        self.assertEqual(dcc_fetch.count_active_fetches(), 1)

        outcome = dcc_fetch.handle_bot_reply(
            "ServerOne", "Request Accepted - File: Song One.mp3 - Position: 12 - OmeNServE v2.60")

        self.assertEqual(outcome, "queued")
        self.assertEqual(self.row(rid)["state"], "queued")
        self.assertEqual(self.row(rid)["queue_position"], 12)
        self.assertIn("Position: 12", self.row(rid)["reply"])
        self.assertEqual(dcc_fetch.count_active_fetches(), 0, "a queued request holds no slot")

    def test_and_its_file_is_taken_when_it_finally_comes(self):
        """The whole point: before #926 this offer was refused as unsolicited."""
        rid = self.offered()
        dcc_fetch.handle_bot_reply("ServerOne", "Request Accepted - Position: 1 - OmeNServE v2.60")
        with dcc_fetch._fetch_lock():
            claimed, row = dcc_fetch._claim_matching_offer_locked(
                config.fetch_queue, "ServerOne", "Song_One.mp3")
        self.assertEqual(claimed, rid)
        self.assertEqual(row["state"], "receiving")

    def test_a_list_request_queued_there_takes_its_list_too(self):
        rid = self.offered(name="", request_type="list")
        dcc_fetch.handle_bot_reply("ServerOne", "Added list to your personal queue at position #2 of 100.")
        with dcc_fetch._fetch_lock():
            claimed, _row = dcc_fetch._claim_matching_offer_locked(
                config.fetch_queue, "ServerOne", "ServerOne-list.zip")
        self.assertEqual(claimed, rid)

    def test_a_later_position_updates_it(self):
        rid = self.offered()
        dcc_fetch.handle_bot_reply("ServerOne", "Request Accepted - Position: 12 - OmeNServE")
        dcc_fetch.handle_bot_reply("ServerOne",
                                   "Request Denied - You Already Have Song One.mp3 In My Queue - Position 3 - OmenServE")
        self.assertEqual(self.row(rid)["queue_position"], 3)

    def test_refused_ends_it_with_their_words(self):
        rid = self.offered()
        outcome = dcc_fetch.handle_bot_reply(
            "ServerOne", "Request Denied - I Don't Have Song One.mp3, Check Your Spelling - OmenServE")
        self.assertEqual(outcome, "refused")
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertTrue(self.row(rid)["reason"].startswith("refused: Request Denied"))

    def test_busy_is_asked_again_later_then_ends_as_busy(self):
        """Busy is not never: back to pending with a time, BUSY_RETRIES times."""
        rid = self.offered()
        for attempt in range(dcc_fetch.BUSY_RETRIES):
            self.row(rid).update(state="offered")
            dcc_fetch.handle_bot_reply("ServerOne", "I am totally maxed out even in que list. Try it later.")
            self.assertEqual(self.row(rid)["state"], "pending", attempt)
            self.assertGreater(self.row(rid)["retry_at"], time.time() + dcc_fetch.BUSY_RETRY_SECONDS - 5)
        self.row(rid).update(state="offered")
        dcc_fetch.handle_bot_reply("ServerOne", "I am totally maxed out even in que list. Try it later.")
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertTrue(self.row(rid)["reason"].startswith("busy: "))

    def test_a_folder_rar_refusal_still_works_the_old_way(self):
        rid = self.offered(name="!rar Artist/Album", request_type="folder")
        self.assertEqual(dcc_fetch.handle_bot_reply("ServerOne", "Rar Server is currently disabled."), "refused")
        self.assertEqual(self.row(rid)["state"], "failed")


class OnlyTheRightRequestMoves(FetchCase):
    def test_another_bot_cannot_touch_our_request(self):
        rid = self.offered(bot="ServerOne")
        self.assertIsNone(dcc_fetch.handle_bot_reply(
            "SomeoneElse", "Request Denied - Check Your Spelling - OmenServE"))
        self.assertEqual(self.row(rid)["state"], "offered")

    def test_a_reply_naming_a_file_acts_on_that_file(self):
        first = self.offered(name="Song One.mp3", ago=0)
        second = self.offered(name="Song Two.mp3", ago=10)
        dcc_fetch.handle_bot_reply("ServerOne",
                                   "Request Denied - I Don't Have Song_Two.mp3, Check Your Spelling - OmenServE")
        self.assertEqual(self.row(first)["state"], "offered")
        self.assertEqual(self.row(second)["state"], "failed")

    def test_an_unnamed_refusal_among_several_fails_nothing(self):
        first = self.offered(name="Song One.mp3", ago=0)
        second = self.offered(name="Song Two.mp3", ago=10)
        self.assertIsNone(dcc_fetch.handle_bot_reply("ServerOne", "Available Que are Taken"))
        self.assertEqual({self.row(first)["state"], self.row(second)["state"]}, {"offered"})

    def test_an_unnamed_queued_reply_among_several_goes_to_the_oldest(self):
        first = self.offered(name="Song One.mp3", ago=0)
        second = self.offered(name="Song Two.mp3", ago=10)
        dcc_fetch.handle_bot_reply("ServerOne", "Request Accepted - Position: 5 - OmeNServE")
        self.assertEqual(self.row(first)["state"], "queued")
        self.assertEqual(self.row(second)["state"], "offered")

    def test_nothing_outstanding_means_nothing_happens(self):
        self.assertIsNone(dcc_fetch.handle_bot_reply("ServerOne", "Error: File not found."))


class HowLongAQueuedRequestWaits(FetchCase):
    def queued_since(self, seconds_ago):
        rid = self.offered()
        dcc_fetch.handle_bot_reply("ServerOne", "Request Accepted - Position: 1 - OmeNServE")
        self.row(rid)["queued_at"] = time.time() - seconds_ago
        return rid

    def test_long_past_the_offer_timeout(self):
        rid = self.queued_since(3600)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "queued")

    def test_but_not_for_ever(self):
        rid = self.queued_since(43200 + 60)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "failed")
        self.assertIn("still queued at ServerOne", self.row(rid)["reason"])

    def test_zero_means_for_ever(self):
        self.set_config(FETCH_QUEUED_TIMEOUT=0)
        rid = self.queued_since(10 ** 7)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.row(rid)["state"], "queued")

    def test_it_can_be_let_go(self):
        rid = self.queued_since(10)
        status, _payload = webserver.build_fetch_delete_result(rid)
        self.assertEqual(status, 200)
        self.assertNotIn(rid, config.fetch_queue)


class TheRepliesReachIt(unittest.TestCase):
    """irc.py hands every private NOTICE, and every private non-CTCP message,
    to dcc_fetch.handle_bot_reply()."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_private_notices(self):
        self.assertIn("dcc_fetch.handle_bot_reply(notice_user, notice_text)", self.source())

    def test_private_messages_that_are_not_ctcp(self):
        code = self.source()
        at = code.index("dcc_fetch.handle_bot_reply(user, msg)")
        guard = code[code.rindex("if (target_chan.lower() == config.NICKNAME.lower()", 0, at):at]
        self.assertIn('not msg.startswith("\\x01")', guard)


if __name__ == "__main__":
    unittest.main()
