"""What an mxrarserver 2.x bot says about our request is understood (#1209).

fetch_replies.classify() knew the wordings of OmenServe, SDFind, BWI, SpR
Jukebox and DCCore, and none of mxrarserver's. So a file it queued was asked
for again three times as "no response" and failed, a refusal waited out its
timeout, and a list that took longer than a minute to build - mxrarserver
packs every list into a RAR before it sends it - was refused as unsolicited
when it arrived.

The wordings below are read out of mxrarserver 2.1.5's script; the names in
them are invented. Each maps onto an outcome classify() already has:

    queued     "Request accepted ... Queue position", the list forms of it,
               and "Compression completed" (packed, about to be sent)
    duplicate  "Request denied, you already have ... in the position"
    busy       "You reached ... allowed requests", "... list build in progress"
    refused    "File not found", "Folder not found", "Compression failed",
               "Compression timeout exceeded", "Manual cancellation"

And a list offer that arrives after its row gave up is taken, the way a late
file already was.
"""

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

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "SomeServer"
FOLDER = "E:\\Music\\Some Artist\\Some Album.rar"

CASES = [
    ("Request accepted: Some Track.mp3 | Queue position: 3 | Allowed requests: 1 of 5",
     "queued", 3),
    ("Request accepted: " + FOLDER + " | Queue position: 12 | Allowed requests: 2 of 5 "
     "Transfer starts automatically after compression.", "queued", 12),
    ("List Request Accepted: Files, Queue Position: 1", "queued", 1),
    ("List Request Accepted: Folders, Queue Position: 7", "queued", 7),
    ("Complete list request accepted: Files + Folders, Queue Position 2. The list will be "
     "prepared and sent automatically.", "queued", 2),
    ("Compression completed: SomeAlbum.rar Size: 120 MB Processing time: 31s", "queued", None),
    ("Request denied, you already have Some Track.mp3 in the position: 4", "duplicate", 4),
    ("You reached 5 of 5 allowed requests", "busy", None),
    ("Files list build in progress ... temporarily disabled", "busy", None),
    ("Folders list build in progress ... temporarily disabled", "busy", None),
    ("Public list build in progress: Masterlist 42%", "busy", None),
    ("File not found. Check the requested filename, or the file may not be available in "
     "this channel.", "refused", None),
    ("Folder not found in the list assigned to this channel", "refused", None),
    ("Compression failed", "refused", None),
    ("Compression timeout exceeded 600 seconds. The stalled worker was terminated", "refused", None),
    ("Manual cancellation: Folder compression was cancelled manually. Please try again if "
     "needed.", "refused", None),
]


class WhatALineMeans(unittest.TestCase):

    def test_each_reply(self):
        for text, outcome, position in CASES:
            with self.subTest(text=text):
                reply = fetch_replies.classify(text)
                self.assertIsNotNone(reply)
                self.assertEqual((reply.outcome, reply.position), (outcome, position))

    def test_in_colour(self):
        reply = fetch_replies.classify(
            "\x0304Request accepted:\x03 \x02Some Track.mp3\x02 \x0310|\x03 Queue position: "
            "\x0308 9\x0f | Allowed requests: 1 of 5")

        self.assertEqual((reply.outcome, reply.position), ("queued", 9))

    def test_the_words_have_to_come_in_order(self):
        self.assertIsNone(fetch_replies.classify("Queue position 3, request accepted"))


class WhatItDoesToTheRequest(DCCoreTestCase):

    def row(self, name="Some Track.mp3", request_type="file", state="offered"):
        rid = dcc_fetch.enqueue_fetch(BOT, name, request_type=request_type)
        config.fetch_queue[rid].update(state=state, offered_at=time.time())
        return config.fetch_queue[rid]

    def test_accepted_waits_in_their_queue(self):
        row = self.row()

        dcc_fetch.handle_bot_reply(
            BOT, "Request accepted: Some Track.mp3 | Queue position: 3 | Allowed requests: 1 of 5")

        self.assertEqual((row["state"], row["queue_position"]), ("queued", 3))

    def test_a_list_request_accepted_waits_too(self):
        row = self.row(name="", request_type="list")

        dcc_fetch.handle_bot_reply(BOT, "Complete list request accepted: Files + Folders, "
                                        "Queue Position 2. The list will be prepared and sent automatically.")

        self.assertEqual((row["state"], row["queue_position"]), ("queued", 2))

    def test_already_queued_waits(self):
        row = self.row()

        dcc_fetch.handle_bot_reply(BOT, "Request denied, you already have Some Track.mp3 in the position: 4")

        self.assertEqual((row["state"], row["queue_position"]), ("queued", 4))

    def test_compression_completed_keeps_it_waiting(self):
        row = self.row(name=FOLDER, request_type="folder")

        dcc_fetch.handle_bot_reply(BOT, "Compression completed: SomeAlbum.rar Size: 120 MB "
                                        "Processing time: 31s")

        self.assertEqual(row["state"], "queued")

    def test_not_found_ends_it(self):
        row = self.row()

        dcc_fetch.handle_bot_reply(BOT, "File not found. Check the requested filename, or the "
                                        "file may not be available in this channel.")

        self.assertEqual(row["state"], "failed")
        self.assertIn("File not found", row["reason"])

    def test_a_failed_pack_ends_it(self):
        for text in ("Compression failed",
                     "Compression timeout exceeded 600 seconds. The stalled worker was terminated",
                     "Manual cancellation: Folder compression was cancelled manually.",
                     "Folder not found in the list assigned to this channel"):
            with self.subTest(text=text):
                row = self.row(name=FOLDER, request_type="folder")

                dcc_fetch.handle_bot_reply(BOT, text)

                self.assertEqual(row["state"], "failed")
                config.fetch_queue.clear()

    def test_a_limit_is_asked_again_later(self):
        row = self.row()

        dcc_fetch.handle_bot_reply(BOT, "You reached 5 of 5 allowed requests")

        self.assertEqual((row["state"], row["waiting"]), ("pending", "retry"))
        self.assertTrue(row["reason"].startswith("busy:"))

    def test_a_list_build_is_asked_again_later(self):
        row = self.row(name="", request_type="list")

        dcc_fetch.handle_bot_reply(BOT, "Files list build in progress ... temporarily disabled")

        self.assertEqual((row["state"], row["waiting"]), ("pending", "retry"))


class ALateList(DCCoreTestCase):
    """A list request gives up after FETCH_OFFER_TIMEOUT - a minute - and
    mxrarserver builds and packs a list before it sends it."""

    def failed_list_row(self, offered_ago=300, reason="no response", bot=BOT):
        rid = dcc_fetch.enqueue_fetch(bot, "", request_type="list")
        config.fetch_queue[rid].update(state="failed", reason=reason,
                                       offered_at=time.time() - offered_ago)
        return rid, config.fetch_queue[rid]

    def claim(self, name="SomeServer-Files(1234)-MX.rar", bot=BOT):
        with dcc_fetch._fetch_lock():
            return dcc_fetch._claim_matching_offer_locked(config.fetch_queue, bot, name)

    def test_is_taken_by_the_request_that_gave_up(self):
        rid, row = self.failed_list_row()

        self.assertEqual(self.claim()[0], rid)
        self.assertEqual(row["state"], "receiving")
        self.assertEqual(row["filename"], "SomeServer-Files(1234)-MX.rar")
        self.assertNotIn("reason", row)

    def test_not_after_the_grace(self):
        _rid, row = self.failed_list_row(offered_ago=dcc_fetch._LATE_OFFER_GRACE + 60)

        self.assertEqual(self.claim(), (None, None))
        self.assertEqual(row["state"], "failed")

    def test_not_for_a_request_that_was_refused(self):
        _rid, row = self.failed_list_row(reason="refused: File not found")

        self.assertEqual(self.claim(), (None, None))

    def test_not_from_another_bot(self):
        self.failed_list_row()

        self.assertEqual(self.claim(bot="SomeoneElse"), (None, None))

    def test_a_failed_folder_request_is_taken_too(self):
        """#1244 review: a folder request names nothing either, so it used
        to be refused the same late-offer allowance a list already gets -
        but #1234's one-at-a-time dispatch promotes a bot's NEXT folder the
        moment one times out, so a slow bot's archive for the FIRST
        (now-failed) one arriving late was claimed by the row that replaced
        it instead. A failed folder row's late answer is now taken by the
        bot-alone match, exactly like a failed list row's already was."""
        rid = dcc_fetch.enqueue_fetch(BOT, "!rar Artist/Album", request_type="folder")
        config.fetch_queue[rid].update(state="failed", reason="no response",
                                       offered_at=time.time() - 300)

        claimed_id, row = self.claim(name="Album.rar")

        self.assertEqual(claimed_id, rid)
        self.assertEqual(row["state"], "receiving")
        self.assertNotIn("reason", row)

    def test_a_request_still_waiting_comes_first(self):
        self.failed_list_row()
        waiting = dcc_fetch.enqueue_fetch(BOT, "!rar Artist/Album", request_type="folder")
        config.fetch_queue[waiting].update(state="offered", offered_at=time.time())

        self.assertEqual(self.claim(name="Album.rar")[0], waiting)

    def test_the_newest_request_takes_it(self):
        older, _ = self.failed_list_row(offered_ago=900)
        config.fetch_queue[older]["requested_at"] = time.time() - 900
        newer, _ = self.failed_list_row(offered_ago=200)

        self.assertEqual(self.claim()[0], newer)


if __name__ == "__main__":
    unittest.main()
