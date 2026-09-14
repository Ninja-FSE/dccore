"""#510 - fourteen channels asked for, eleven joined, nothing said.

Reported live. The connect path sent every configured channel as ONE JOIN
line and then a second, separate command for the debug channel with no gap at
all. The server took the head of the line and dropped the tail, so the
channels lost were the last ones in configured order - and the debug channel,
which was last of all.

Three things then kept it quiet:

  * a refusal numeric arriving at connect time is deliberately not counted
    (note_join_refused() only counts for a channel already being retried), so
    nothing was recorded;
  * the retry machinery is seeded by the KICK handler alone, so a channel the
    bot never got INTO could not be retried whatever the server said;
  * the one warning that did fire - activation_watchdog(), which had already
    computed exactly the right set - went to stdout and to the DEBUG CHANNEL,
    which is routinely one of the casualties, and never to the dashboard.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# One IRC line is 512 bytes including the trailing CRLF.
IRC_LINE_BYTES = 512


def join_body():
    """delayed_join()'s source, with comments stripped.

    Reaching this code for real needs a registered connection, so it is read -
    and comments are stripped because the paragraph above it describes the old
    behaviour it replaced, in the same words a test would search for.
    """
    with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
        code = handle.read()
    body = code.split("def delayed_join(", 1)[1].split("activation_watchdog", 1)[0]
    return re.sub(chr(35) + "[^" + chr(10) + "]*", "", body)


class WhereTheBotBelongs(DCCoreTestCase):
    """One definition of the question, which is the whole of #193's lesson."""

    def test_the_debug_channel_is_one_of_them(self):
        self.set_config(CHANNEL="#alpha,#beta", DEBUG_CHANNEL="#thedebug")

        self.assertEqual(irc.channels_we_should_be_in(),
                         ["#alpha", "#beta", "#thedebug"])

    def test_a_blank_debug_channel_is_not_a_channel_named_nothing(self):
        self.set_config(CHANNEL="#alpha", DEBUG_CHANNEL="")

        self.assertEqual(irc.channels_we_should_be_in(), ["#alpha"])

    def test_a_debug_channel_already_in_channel_is_not_listed_twice(self):
        """An operator who points DEBUG_CHANNEL at a channel they also serve.
        Joining it twice is harmless; PARTing it once because it appears twice
        is not."""
        self.set_config(CHANNEL="#alpha,#beta", DEBUG_CHANNEL="#Beta")

        self.assertEqual(irc.channels_we_should_be_in(), ["#alpha", "#beta"])

    def test_the_rehash_asks_the_same_question(self):
        """Asserted as AGREEMENT rather than by looking for an import. Two
        answers to "where does this bot belong" is how one of them ends up
        deciding to PART a channel the other still wants."""
        self.set_config(CHANNEL="#alpha,#Beta", DEBUG_CHANNEL="#TheDebug")

        self.assertEqual(commands._channels_to_sync(config),
                         [name.lower() for name in irc.channels_we_should_be_in()])


class TheJoinGoesOutInBatches(unittest.TestCase):

    def names(self, count):
        return ["#chan%02d" % n for n in range(count)]

    def test_fourteen_channels_are_not_one_line(self):
        """The reported case. One line is what the server truncated."""
        batches = irc.join_batches(self.names(14))

        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(payload.split(",")) <= irc.JOIN_BATCH_SIZE
                            for payload in batches), batches)

    def test_nothing_is_lost_or_reordered(self):
        """The other half of batching, and the one that matters: a channel
        dropped by the batcher is indistinguishable from one dropped by the
        server."""
        wanted = self.names(14)

        rejoined = []
        for payload in irc.join_batches(wanted):
            rejoined.extend(payload.split(","))

        self.assertEqual(rejoined, wanted)

    def test_a_short_list_is_still_one_line(self):
        """Batching must not cost a gap to an install with three channels."""
        self.assertEqual(irc.join_batches(["#one", "#two"]), ["#one,#two"])

    def test_nothing_configured_sends_nothing(self):
        """`JOIN \\r\\n` is a malformed line, and joining some channel nobody
        configured is worse than joining none at all."""
        self.assertEqual(irc.join_batches([]), [])

    def test_a_line_is_never_over_the_budget(self):
        """The count is what a server's join handling limits; this is the
        ordinary line budget underneath it, and only bites for names far
        longer than RFC 2812 allows."""
        wanted = ["#" + ("x" * 200) for _ in range(6)]

        for payload in irc.join_batches(wanted):
            with self.subTest(payload=payload[:40]):
                self.assertLessEqual(len("JOIN %s\r\n" % payload), IRC_LINE_BYTES)

    def test_one_channel_too_long_to_batch_still_goes_out(self):
        """Refusing to ask is worse than asking and being refused - only one
        of those leaves the operator something to read."""
        monster = "#" + ("x" * 900)

        self.assertEqual(irc.join_batches([monster]), [monster])


class TheConnectPathUsesIt(unittest.TestCase):
    """Read out of irc.py: reaching delayed_join() for real needs a registered
    connection to a server."""

    def test_the_batches_are_what_is_sent(self):
        """delayed_join() joins what it was HANDED. Which list that is gets
        decided by the caller, and tests/test_channel_list.py owns that half -
        so this asserts the body uses its parameter rather than asking a
        second time, which is how the two would drift apart."""
        body = join_body()

        self.assertIn("join_batches(", body)
        self.assertIn("wanted = channels", body,
                      "the body fetches its own channel list instead of "
                      "joining the one it was given")

    def test_there_is_a_gap_between_them(self):
        """Asserted as a SEQUENCE - the sleep inside the loop, skipped on the
        first pass - because a sleep before the loop and a sleep in it look
        identical to a check for "is there a sleep"."""
        body = join_body()
        loop = body.split("for index, payload in enumerate(batches):", 1)

        self.assertEqual(len(loop), 2, "the batches are not sent in a loop")
        self.assertIn("time.sleep(JOIN_BATCH_GAP)", loop[1],
                      "every batch goes out at once, which is the burst this "
                      "exists to break up")
        self.assertIn("if index:", loop[1],
                      "the gap is taken before the FIRST batch too, which "
                      "delays every channel for no reason")

    def test_the_debug_channel_is_no_longer_a_lone_trailing_command(self):
        """It was the most exposed line in the burst, and the one whose loss
        costs the operator the message saying anything was lost."""
        body = join_body()

        self.assertNotIn("JOIN {debug_chan}", body)


class AChannelThatNeverLetUsIn(DCCoreTestCase):
    """#510's second half: the retry machinery was seeded by KICK alone."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#alpha,#beta", DEBUG_CHANNEL="#thedebug",
                        REJOIN_ATTEMPTS=3)
        config.kicked_channels.clear()

    def test_an_unconfirmed_channel_is_tracked_and_retried(self):
        self.assertTrue(irc.note_join_unconfirmed("#beta"))

        self.assertIn("#beta", irc.channels_to_rejoin())

    def test_the_debug_channel_is_retried_too(self):
        """It could not be, before: every gate here read CHANNEL alone, so a
        debug channel that failed to join was invisible to the machinery even
        once something tracked it."""
        self.assertTrue(irc.note_join_unconfirmed("#thedebug"))

        self.assertIn("#thedebug", irc.channels_to_rejoin())

    def test_a_channel_nobody_configured_is_not_tracked(self):
        """The bot deciding where it belongs is the thing this must not do."""
        self.assertFalse(irc.note_join_unconfirmed("#somewhere-else"))
        self.assertEqual(irc.channels_to_rejoin(), [])

    def test_a_kick_is_not_overwritten_by_an_unconfirmed_join(self):
        """A kick is the more specific answer, and it carries who did it."""
        irc.note_kicked_from("#beta", "SomeOp")

        self.assertFalse(irc.note_join_unconfirmed("#beta"))
        self.assertEqual(config.kicked_channels["#beta"]["by"], "SomeOp")
        self.assertEqual(config.kicked_channels["#beta"]["reason"], "kicked")

    def test_the_reason_is_recorded_because_the_wording_differs(self):
        """"Gave up after 3 attempts" reads very differently for a channel
        that threw us out and one that never let us in."""
        irc.note_join_unconfirmed("#beta")

        self.assertEqual(config.kicked_channels["#beta"]["reason"],
                         "never confirmed")

    def test_it_still_gives_up_after_the_configured_attempts(self):
        """The retry is bounded, the way a kick's is. A server that will not
        have us must not be asked forever."""
        irc.note_join_unconfirmed("#beta")
        for _ in range(3):
            irc.note_join_refused("#beta")

        self.assertEqual(irc.channels_to_rejoin(), [])
        self.assertIn("#beta", irc.gave_up_on())


class TheWatchdogSaysSoWhereItIsRead(unittest.TestCase):

    def watchdog_body(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def activation_watchdog(", 1)[1].split("while True:", 1)[0]
        return re.sub(chr(35) + "[^" + chr(10) + "]*", "", body)

    def test_what_never_confirmed_is_retried_rather_than_only_printed(self):
        """The set was already exactly right and was only ever printed."""
        self.assertIn("note_join_unconfirmed(", self.watchdog_body())

    def test_it_reaches_the_dashboard_and_not_only_the_debug_channel(self):
        """send_debug() writes to the debug channel, which when a truncated
        JOIN is the cause is routinely one of the channels that went missing.
        The message about losing channels cannot be delivered only to one of
        them."""
        body = self.watchdog_body()
        call = body.split("never confirmed via NAMES", 1)

        self.assertEqual(len(call), 2, "the watchdog message has moved")
        self.assertIn('notice="error"', call[1][:400],
                      "the only operator-facing copy of this still goes to a "
                      "channel the fault may have removed us from")

    def test_the_missing_set_includes_the_debug_channel(self):
        """target_channels is CHANNEL only, and deliberately so - activation
        must not wait on the debug channel. But "what never confirmed" is a
        different question from "may we start advertising"."""
        body = self.watchdog_body()

        self.assertIn("missing = channels_we_should_be_in_set() - channels_confirmed",
                      body)

    def test_activation_still_does_not_wait_on_the_debug_channel(self):
        """The other half. One broken debug channel must not silence every
        advert - which is what gating activation on the wider set would do."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = re.sub(chr(35) + "[^" + chr(10) + "]*", "", handle.read())

        self.assertIn("if target_channels.issubset(channels_confirmed):", code)


class WhatTheServerSaidIsNoLongerDiscarded(unittest.TestCase):

    def test_too_many_channels_is_a_refusal(self):
        """405 is the answer to "you are configured for more channels than
        this server allows", and it fell straight through."""
        self.assertIn("405", irc.JOIN_REFUSED_NUMERICS)
        self.assertEqual(
            irc.parse_join_refusal(
                ":irc.example.invalid 405 SomeBot #beta :You have joined too many channels"),
            ("#beta", "405"))

    def test_it_gets_its_own_wording(self):
        """"Gave up after 3 attempts" would send the operator looking for a
        fault on the channel's side. The fault is their channel count."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = re.sub(chr(35) + "[^" + chr(10) + "]*", "", handle.read())

        self.assertIn("if numeric == JOIN_REFUSED_AT_THE_LIMIT:", code)

    def test_an_uncounted_refusal_is_still_said_out_loud(self):
        """note_join_refused() counts only for a channel already being
        retried, deliberately - so a refusal at CONNECT time was discarded in
        silence, which is most of why serving eleven of fourteen channels
        looked like nothing had happened."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        block = code.split("count = note_join_refused(refused_chan)", 1)[1][:2000]
        block = re.sub(chr(35) + "[^" + chr(10) + "]*", "", block)

        self.assertIn("else:", block)
        self.assertIn("refused us ({numeric}).", block)
