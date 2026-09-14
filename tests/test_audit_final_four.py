"""The last four confirmed audit findings. Two fixed, two deliberately not.

Recording the two non-changes matters as much as the fixes: both are real
observations, and both have a "fix" that would make the daemon worse. Without
this written down the next reader finds them in the audit log and applies the
obvious change.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import list_index  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ABusyNeighbourCannotHideAMatch(DCCoreTestCase):
    """`bot:"Dude"` is an FTS5 PHRASE over a tokenised column, not equality -
    unicode61 splits on punctuation, so it also matches `Dude|away`.

    The equality check that compensated for that ran in Python over `LIMIT 25`
    rows. A neighbour holding fifty matching files fills the whole window, so
    the real bot underneath was reported as having NO match: the sidebar
    dimmed a list that did contain the file, while search() for the same term
    listed it.

    Whatever number is chosen there, a busy neighbour can exceed it. The
    equality is part of the query now.
    """

    def index(self, bot, *filenames):
        """Through the REAL producer, as tests/test_crosslist_search.py does
        and for the reason its own helper documents: a hand-built row carrying
        the wrong key once let every test in that file pass while the
        production feed indexed empty names."""
        import list as list_mod

        backslash = chr(92)
        folder = "D:" + backslash + "MEDIA" + backslash + "Some Folder" + backslash
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": name, "size": "4.00MB", "folder": folder}
             for name in filenames],
            bot)
        return list_index.index_bot_list(bot, rows)

    def test_a_neighbour_filling_the_window_does_not_hide_the_real_bot(self):
        self.index("Dude|away", *[f"metallica {i:03d}.flac" for i in range(50)])
        self.index("Dude", "metallica one.flac")

        matched, empty = list_index.bots_with_a_match(["metallica"],
                                                      ["Dude", "Dude|away"])

        self.assertIn("dude", matched)
        self.assertNotIn("dude", empty)

    def test_the_neighbour_is_still_matched_on_its_own_account(self):
        self.index("Dude|away", "metallica one.flac")
        self.index("Dude", "something else.flac")

        matched, empty = list_index.bots_with_a_match(["metallica"],
                                                      ["Dude", "Dude|away"])

        self.assertIn("dude|away", matched)
        self.assertIn("dude", empty)

    def test_a_bot_with_nothing_is_still_reported_empty(self):
        """Control: the point is not to mark everybody matched."""
        self.index("Dude", "something else.flac")

        matched, empty = list_index.bots_with_a_match(["metallica"], ["Dude"])

        self.assertEqual(matched, set())
        self.assertIn("dude", empty)

    def test_the_answer_agrees_with_the_search_that_lists_the_rows(self):
        """The visible symptom was the two disagreeing: the sidebar dimmed a
        list the filter would then show rows from."""
        self.index("Dude|away", *[f"metallica {i:03d}.flac" for i in range(50)])
        self.index("Dude", "metallica one.flac")

        rows = list_index.search(["metallica"], bots=["Dude"])
        matched, _empty = list_index.bots_with_a_match(["metallica"], ["Dude"])

        listed = bool(rows[0] if isinstance(rows, tuple) else rows)
        self.assertEqual(listed, "dude" in matched)


class TheAdvertWorkerSurvivesTheReload(DCCoreTestCase):
    """`announce.current_worker_id` is how a running worker knows it is still
    the current one. importlib.reload() re-executes announce's body and resets
    it to 0, and the worker wakes every five seconds to compare.

    The restore used to sit seventy lines below the reload. A wake landing in
    that window read 0, concluded it had been replaced, and retired - leaving
    no advert worker and the channels silent until the next reconnect.
    """

    def setUp(self):
        super().setUp()
        self._real = getattr(announce, "current_worker_id", 0)
        self.addCleanup(setattr, announce, "current_worker_id", self._real)

    def test_a_zeroed_token_is_put_back(self):
        announce.current_worker_id = 0

        self.assertTrue(commands._restore_advert_worker_token(7))
        self.assertEqual(announce.current_worker_id, 7)

    def test_a_newer_worker_is_not_retired(self):
        """A reconnect completing inside the reload window starts a fresh
        worker and stamps a higher id. Restoring blindly would retire that
        one and cause the very silence this prevents."""
        announce.current_worker_id = 9

        self.assertFalse(commands._restore_advert_worker_token(7))
        self.assertEqual(announce.current_worker_id, 9)

    def test_nothing_to_restore_is_not_an_error(self):
        announce.current_worker_id = 0

        self.assertFalse(commands._restore_advert_worker_token(None))
        self.assertFalse(commands._restore_advert_worker_token(0))

    def test_the_restore_happens_immediately_after_the_reload(self):
        """The window is the defect. Asserted on ordering because the
        alternative is a live worker thread and a real reload, and what
        matters is precisely how much code runs in between."""
        import inspect

        # Split on the CALL, with its indentation - not on the bare name.
        # The first occurrence of "reload_modules_in_order()" in this function
        # is inside a comment two lines above it, so a bare split measured the
        # distance from the comment and reported 91 lines.
        source = inspect.getsource(commands._handle_rehash_request)
        newline = chr(10)
        marker = newline + "        reload_modules_in_order()" + newline
        self.assertIn(marker, source)
        after_reload = source.split(marker, 1)[1]
        restore_at = after_reload.find("_restore_advert_worker_token(")

        self.assertGreaterEqual(restore_at, 0, "the token is never restored")
        self.assertLess(
            after_reload[:restore_at].count("\n"), 20,
            "the restore drifted away from the reload; every line between "
            "them is a five-second window in which the worker retires")


class ConsideredAndDeliberatelyNotChanged(DCCoreTestCase):
    """Two confirmed findings whose obvious fix would make things worse.

    Written as tests so the reasoning is attached to the behaviour rather than
    living only in a changelog nobody greps.
    """

    def test_a_bot_alone_row_may_still_claim_a_near_miss_offer(self):
        """FINDING: with both a "list" row and a "file" row outstanding for
        one bot, an offer whose filename does not exactly match the file row
        falls through to the bot-alone "list" branch and is claimed there.

        NOT CHANGED. The file branch runs first and takes any exact match
        (space/underscore normalised, which is the one transformation every
        DCC client applies). Reaching the list branch therefore means the bot
        sent a name that is genuinely not the file we asked for - and for a
        bare `@bot` list request, whose answer we cannot predict, that is
        exactly what a real list reply looks like.

        Refusing the fall-through would reject legitimate list answers
        whenever a file request to the same bot happened to be outstanding.
        The trade is: an unusual near-miss can be mis-attributed, versus
        ordinary list fetches failing. Admission control still holds either
        way - an offer from a bot with NO outstanding request is refused.
        """
        import dcc_fetch
        import inspect

        source = inspect.getsource(dcc_fetch._claim_matching_offer_locked)

        # The file branch first, and still exact.
        file_at = source.find('!= "file"')
        list_at = source.find('== "list"')
        self.assertGreaterEqual(file_at, 0)
        self.assertLess(file_at, list_at,
                        "the exact-match branch must be tried before the "
                        "bot-alone ones")
        self.assertIn("_normalize_filename_for_match(row.get(\"filename\", \"\")) != wanted_name",
                      source)

    def test_the_passive_reply_still_goes_through_the_paced_queue(self):
        """FINDING: the passive DCC SEND reply is queued behind the 5s
        per-message pacer while the 60s accept clock is already running, so a
        busy outbound queue can eat much of the window.

        NOT CHANGED without a decision. Sending it unpaced is what the pacer
        exists to prevent - queue_mgr is the reason this bot does not meet
        Excess Flood - and the 513/PONG line in irc.py is on record as the
        one place a raw unpaced write was allowed and the trouble it caused.
        Raising PASSIVE_LISTEN_TIMEOUT instead is a settings change with no
        such risk.

        Pinned so that if somebody does route it around the pacer, they do it
        knowingly.
        """
        import dcc_fetch
        import inspect

        source = inspect.getsource(dcc_fetch._serve_passive_offer)

        # "irc_sock.send" without the paren: since #504 the spelling is
        # sendall(), and a check for the old one would pass against a direct
        # write that simply used the new name.
        self.assertNotIn("irc_sock.send", source,
                         "the passive reply now writes to the socket "
                         "directly, bypassing queue_mgr's pacing")


if __name__ == "__main__":
    unittest.main()
