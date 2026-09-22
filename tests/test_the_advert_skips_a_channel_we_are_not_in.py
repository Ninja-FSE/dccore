"""The advert went into every configured channel, in or not (audit M29, #631).

announce_worker walked irc.configured_channels() with no membership check.
A channel the bot had been kicked from - including one past REJOIN_ATTEMPTS,
which gets no more JOINs - and a channel that was +i and never answered its
JOIN each still received the advert PRIVMSG and the CTCP SLOTS line every
ANNOUNCE_INTERVAL. The server answers 404, nothing reads that, and each of
the two lines costs a MSG_DELAY slot on the shared pacer that the channels
the bot IS in were waiting for. With 14 channels and MSG_DELAY=5 that is
10 s of the outbound clock per cycle, for the life of the process, with no
operator-visible symptom beyond the one "gave up" line.

irc.py already knows the answer: config.kicked_channels holds exactly the
channels the bot is out of (a kick or an unanswered JOIN writes the entry,
the 366 of a successful join removes it). irc.channels_we_are_out_of()
reads it, and the worker skips what it names - the same division as the
rejoin: irc.py owns the rule, the worker only acts on it.
"""

import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import irc  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

KICKED = "#kickedchannel"
NEVER_LET_IN = "#neverjoined"
FINE = "#finechannel"


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class WhatTheBotIsOutOf(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="%s, %s, %s" % (KICKED, NEVER_LET_IN, FINE),
                        REJOIN_ATTEMPTS=3)

    def test_nothing_at_first(self):
        self.assertEqual(irc.channels_we_are_out_of(), set())

    def test_a_channel_that_kicked_us(self):
        irc.note_kicked_from(KICKED, "someop")

        self.assertEqual(irc.channels_we_are_out_of(), {KICKED})

    def test_a_channel_that_never_answered_its_join(self):
        irc.note_join_unconfirmed(NEVER_LET_IN)

        self.assertEqual(irc.channels_we_are_out_of(), {NEVER_LET_IN})

    def test_a_channel_we_gave_up_on_stays_out(self):
        """Past REJOIN_ATTEMPTS nothing will ever put the bot back in - which
        is exactly the channel that used to be advertised into for ever."""
        irc.note_kicked_from(KICKED, "someop")
        for _ in range(3):
            irc.note_join_refused(KICKED)

        self.assertEqual(irc.channels_to_rejoin(), [])
        self.assertIn(KICKED, irc.channels_we_are_out_of())

    def test_and_a_join_that_worked_puts_it_back(self):
        irc.note_kicked_from(KICKED, "someop")
        irc.note_joined(KICKED)

        self.assertEqual(irc.channels_we_are_out_of(), set())

    def test_lowercased_whatever_the_operator_typed(self):
        irc.note_kicked_from(KICKED.upper(), "someop")

        self.assertEqual(irc.channels_we_are_out_of(), {KICKED})


class TheWorkerSkipsThem(DCCoreTestCase):
    """One real cycle of the actual advert loop, the way
    test_rejoin_a_channel_that_kicked_us.py drives it: the worker on a
    thread, stopped through current_worker_id once its effect is seen."""

    def setUp(self):
        super().setUp()
        self._real_worker_id = announce.current_worker_id
        self.addCleanup(setattr, announce, "current_worker_id", self._real_worker_id)
        self._real_is_ready = announce.is_ready
        self.addCleanup(setattr, announce, "is_ready", self._real_is_ready)
        self._real_list_name = library.list_name_for_request
        self.addCleanup(setattr, library, "list_name_for_request", self._real_list_name)
        self._real_figures = list_mod.get_file_count_date_size_and_raw_bytes
        self.addCleanup(setattr, list_mod, "get_file_count_date_size_and_raw_bytes",
                        self._real_figures)
        # Every channel serves a list with real figures, so the only reason
        # for one to get no advert is the one under test.
        library.list_name_for_request = lambda channel=None: "somelist"
        list_mod.get_file_count_date_size_and_raw_bytes = (
            lambda name: (10, "2026-09-01", "1.0GB", 10 ** 9))
        self.set_config(ANNOUNCE_INTERVAL=0.01, REJOIN_ATTEMPTS=3,
                        CHANNEL="%s, %s, %s" % (KICKED, NEVER_LET_IN, FINE))
        irc.note_kicked_from(KICKED, "someop")
        irc.note_join_unconfirmed(NEVER_LET_IN)
        self.thread = None

    def tearDown(self):
        if self.thread is not None:
            announce.current_worker_id = object()
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive(), "the advert worker must be gone")
        super().tearDown()

    def start_worker(self):
        announce.is_ready = True
        self.thread = threading.Thread(target=announce.announce_worker, daemon=True)
        self.thread.start()

    def channel_lines(self):
        """(channel, line) for every PRIVMSG the worker queued - the advert
        and the CTCP SLOTS line both start that way."""
        out = []
        for _user, msg, _vip in list(self.oserve.queued):
            if msg.startswith("PRIVMSG "):
                out.append((msg.split()[1].lower(), msg))
        return out

    def test_the_channel_we_are_in_is_advertised_and_the_two_others_are_not(self):
        self.start_worker()
        # The advert and its CTCP line for FINE, and the loop has been round
        # every channel by the time both are there.
        self.assertTrue(
            wait_for(lambda: len([c for c, _m in self.channel_lines() if c == FINE]) >= 2),
            "no advert for %s: %r" % (FINE, self.oserve.queued))
        # A second full cycle, so a skip is not just "not yet".
        seen = len(self.channel_lines())
        self.assertTrue(wait_for(lambda: len(self.channel_lines()) >= seen + 2))

        channels = {c for c, _m in self.channel_lines()}
        self.assertEqual(channels, {FINE},
                         "adverts were queued for a channel the bot is not in: %r"
                         % sorted(channels))

    def test_the_rejoin_still_goes_out_for_the_kicked_channel(self):
        """Skipping the advert must not skip the asking-back-in."""
        self.start_worker()

        self.assertTrue(
            wait_for(lambda: any(msg.startswith("JOIN %s" % KICKED)
                                 for _u, msg, _v in self.oserve.queued)),
            "no JOIN for %s: %r" % (KICKED, self.oserve.queued))

    def test_once_back_in_the_channel_is_advertised_again(self):
        irc.note_joined(KICKED)
        self.start_worker()

        self.assertTrue(
            wait_for(lambda: any(c == KICKED for c, _m in self.channel_lines())),
            "no advert for %s after the rejoin: %r" % (KICKED, self.oserve.queued))


if __name__ == "__main__":
    unittest.main()
