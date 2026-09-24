"""Asking for files is not flooding, and the mute notice tells the truth (#888).

THE DEFECT, follow-up to #886

Every line to the bot counted toward the flood gate - MAX_REQUESTS (10)
per REQUEST_WINDOW (5 s) - and a file request counted exactly like a
search. So a user pasting fifteen rows from the list, one album and the
ordinary way these lists are used, got lines 1-10 queued, a MUTE on line
11, and a ONE-HOUR BAN on line 12, whenever their client sent the paste
faster than two lines a second. Undernet paces a paste itself, which is
the only reason this was rare rather than routine; any client, bouncer or
network that sends faster turned asking for an album into a ban.

AND THE MUTE NOTICE MADE IT WORSE. It said "Ignored and queue cleared for
30 seconds". What is actually dropped is config.send_queue - the user's
pending outbound REPLIES. config.dcc_queue, their file queue, is untouched
and still sent. So a user was told their queue had gone, asked again
because of it, and asking again during a mute is the one action that
escalates to the hour.

THE FIX (the operator's decision, #888): file requests are not metered at
all. The bound is the queue - MAX_USER_QUEUE, MAX_GLOBAL_QUEUE - which
already exists, and past it the user is told ONCE rather than once per
refused row. Everything else keeps the meter it has always had.

The dispatch side of that exemption - which messages are metered and which
are not - is pinned in tests/test_irc_dispatch.py's FloodGateCoverageTests,
where the expressions are lifted out of irc.py and evaluated. This file is
the rest: what the user is told, and what is actually true when they are
told it.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import security  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

USER = "ivo"


class TheMuteNoticeIsTrue(DCCoreTestCase):
    """It named the wrong queue, and the user acted on what it said."""

    def setUp(self):
        super().setUp()
        config.muted_until.clear()
        config.banned_users.clear()
        config.user_requests.clear()
        self.addCleanup(config.muted_until.clear)
        self.addCleanup(config.banned_users.clear)
        self.addCleanup(config.user_requests.clear)
        silence_debug(announce)

    def mute_them(self):
        """Go past the gate with metered commands, and return the notice."""
        self.set_config(MAX_REQUESTS=3, REQUEST_WINDOW=5, MUTE_TIME=30)
        for _ in range(config.MAX_REQUESTS + 1):
            tripped = security.is_flooding(USER)
        self.assertTrue(tripped, "the gate did not mute at the limit")
        sent = [msg for who, msg, _vip in self.oserve.queued if who == USER]
        self.assertTrue(sent, "the user was told nothing at all")
        return sent[-1]

    def test_it_no_longer_claims_their_queue_was_cleared(self):
        """The sentence that earned people the ban: they were told their
        files had gone, so they asked again, and asking again during a
        mute is what escalates."""
        notice = self.mute_them()

        self.assertNotIn("queue cleared", notice.lower())

    def test_it_says_what_was_actually_ignored_and_that_the_files_are_safe(self):
        notice = self.mute_them()

        self.assertIn("other commands are ignored", notice.lower())
        self.assertIn("safe", notice.lower())

    def test_and_the_file_queue_really_is_untouched(self):
        """The claim, checked rather than asserted in prose: what the mute
        drops is send_queue, never dcc_queue."""
        config.dcc_queue[USER.lower()] = [{"file": "Song.flac"}]
        self.addCleanup(config.dcc_queue.clear)
        config.send_queue[USER.lower()] = ["a pending reply"]

        self.mute_them()

        self.assertEqual(config.dcc_queue[USER.lower()], [{"file": "Song.flac"}],
                         "the mute took the user's queued files")
        self.assertNotIn(USER.lower(), config.send_queue,
                         "the pending replies were supposed to be dropped")


class TheMeterStillBitesEverythingElse(DCCoreTestCase):
    """The control. #888 moved one kind of request out of the gate; it did
    not soften the gate for anything else, and the escalation is intact."""

    def setUp(self):
        super().setUp()
        config.muted_until.clear()
        config.banned_users.clear()
        config.user_requests.clear()
        self.addCleanup(config.muted_until.clear)
        self.addCleanup(config.banned_users.clear)
        self.addCleanup(config.user_requests.clear)
        silence_debug(announce)

    def test_going_too_fast_still_mutes(self):
        self.set_config(MAX_REQUESTS=3, REQUEST_WINDOW=5, MUTE_TIME=30)

        for _ in range(config.MAX_REQUESTS + 1):
            tripped = security.is_flooding(USER)

        self.assertTrue(tripped)
        self.assertIn(USER.lower(), config.muted_until)

    def test_hammering_while_muted_still_earns_the_ban(self):
        self.set_config(FLOOD_BAN_SECONDS=3600)
        config.muted_until[USER.lower()] = time.time() + 30

        self.assertTrue(security.is_flooding(USER))

        self.assertIn(USER.lower(), config.banned_users)


class TheQueueIsFullIsSaidOnce(DCCoreTestCase):
    """With the gate gone from file requests, a 150-row paste against a
    100-file cap would otherwise send fifty identical refusals, each one
    costing the outbound pace every other user's replies wait on."""

    def setUp(self):
        super().setUp()
        announce.forget_queue_full_notice(USER)
        announce.forget_queue_full_notice("other")
        self.addCleanup(announce.forget_queue_full_notice, USER)
        self.addCleanup(announce.forget_queue_full_notice, "other")

    def notices_to(self, who=USER):
        return [msg for user, msg, _vip in self.oserve.queued if user == who]

    def test_a_whole_refused_batch_is_one_notice(self):
        for _ in range(50):
            announce.send_dcc_error(USER, "user_full")

        self.assertEqual(len(self.notices_to()), 1)

    def test_the_global_cap_is_its_own_notice(self):
        """Two different standing conditions; being told about one is not
        being told about the other."""
        announce.send_dcc_error(USER, "user_full")
        announce.send_dcc_error(USER, "global_full")

        self.assertEqual(len(self.notices_to()), 2)

    def test_another_user_is_told_their_own(self):
        """Keyed per user, not a single global latch - the obvious way to
        get this wrong silences everybody after the first refusal."""
        announce.send_dcc_error(USER, "user_full")
        announce.send_dcc_error("other", "user_full")

        self.assertEqual(len(self.notices_to(USER)), 1)
        self.assertEqual(len(self.notices_to("other")), 1)

    def test_every_other_error_still_repeats(self):
        """These name something about THAT request, so repeating one is the
        answer to a different question, not a duplicate."""
        for _ in range(4):
            announce.send_dcc_error(USER, "file_not_found")

        self.assertEqual(len(self.notices_to()), 4)

    def test_a_request_that_succeeds_makes_the_next_refusal_news_again(self):
        """Without this the suppression is a timer, and a user whose queue
        drains would be refused IN SILENCE for the rest of the window - which
        reads as the bot ignoring them, exactly what the old mute notice
        taught them to expect."""
        announce.send_dcc_error(USER, "user_full")
        self.assertEqual(len(self.notices_to()), 1)

        announce.send_dcc_queue_notice(USER, "Song.flac", 3)
        announce.send_dcc_error(USER, "user_full")

        self.assertGreaterEqual(len([msg for msg in self.notices_to()
                                     if "queue limit" in msg]), 2)

    def test_the_memory_is_bounded(self):
        """Every other memory on this path is capped; this one is reachable
        by anybody in the channel."""
        for number in range(announce.QUEUE_FULL_MEMORY + 40):
            announce.send_dcc_error(f"nick{number}", "user_full")

        self.assertLessEqual(len(announce._told_queue_full),
                             announce.QUEUE_FULL_MEMORY)


if __name__ == "__main__":
    unittest.main()
