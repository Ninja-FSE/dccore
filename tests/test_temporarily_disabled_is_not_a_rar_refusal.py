"""A notice that something is "temporarily disabled" does not fail a folder
request (#1209).

handle_refusal_notice() fails a waiting folder row at once when the bot says
"!rar is disabled here", instead of letting it hold a fetch slot for half an
hour. It recognised that by two SUBSTRINGS, "disabled" and "rar" - and
"tempoRARily disabled" holds both. mxrarserver says exactly that while it
rebuilds a list ("Files list build in progress ... temporarily disabled"), so
every folder request waiting on such a bot was failed as refused by a notice
that was not about it, and the same went for any bot that says anything is
temporarily disabled.

Both markers are now whole words. The two real refusals the check was written
for must still be recognised, and they are tested here beside the one it must
not mistake.
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

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "SomeServer"


class TheFolderRefusal(DCCoreTestCase):

    def offered_folder_row(self, folder="!rar Artist/Album"):
        rid = dcc_fetch.enqueue_fetch(BOT, folder, request_type="folder")
        config.fetch_queue[rid].update(state="offered", offered_at=time.time())
        return config.fetch_queue[rid]

    def test_temporarily_disabled_is_not_a_rar_refusal(self):
        for text in ("Files list build in progress ... temporarily disabled",
                     "Folders list build in progress ... temporarily disabled",
                     "Requests are temporarily disabled, try later"):
            with self.subTest(text=text):
                row = self.offered_folder_row()

                self.assertFalse(dcc_fetch.handle_refusal_notice(BOT, text))
                self.assertEqual(row["state"], "offered")
                config.fetch_queue.clear()

    def test_nor_through_the_reply_handler(self):
        """irc.py hands every private notice to handle_bot_reply(), which asks
        the refusal check first: the row may be asked again later, but it is
        not failed."""
        row = self.offered_folder_row(folder="E:\\Music\\Artist\\Album.rar")

        dcc_fetch.handle_bot_reply(BOT, "Folders list build in progress ... temporarily disabled")

        self.assertNotEqual(row["state"], "failed")

    def test_dccore_s_own_refusal_still_fails_it(self):
        row = self.offered_folder_row()

        self.assertTrue(dcc_fetch.handle_refusal_notice(
            BOT, "Error: Folder packing (!rar) is disabled on this bot."))
        self.assertEqual(row["state"], "failed")

    def test_omenserve_s_refusal_still_fails_it(self):
        row = self.offered_folder_row()

        self.assertTrue(dcc_fetch.handle_refusal_notice(BOT, "Rar Server is currently disabled."))
        self.assertEqual(row["state"], "failed")

    def test_neither_word_may_be_part_of_another(self):
        """"Undisabled" is not "disabled", and "rare" is not "rar"."""
        row = self.offered_folder_row()

        self.assertFalse(dcc_fetch.handle_refusal_notice(BOT, "The rar server is undisabled now"))
        self.assertFalse(dcc_fetch.handle_refusal_notice(BOT, "Rare tracks are disabled for guests"))
        self.assertEqual(row["state"], "offered")


if __name__ == "__main__":
    unittest.main()
