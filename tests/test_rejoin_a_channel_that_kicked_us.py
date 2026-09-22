"""Being thrown out is a thing that happens, and nothing noticed.

Reported from a live channel:

    "a slight bug there, dccore doesn't appear to rejoin a chan if kicked or
    banned, maybe add an option that it can try to rejoin when the advert
    timer triggers"

IT IS WORSE THAN NOT REJOINING. `KICK` was not parsed anywhere, so DCCore did
not know it had left. It went on advertising into a channel it was not in -
the server answers those with 404 and nothing reads it - and never asked to
come back. `474 ERR_BANNEDFROMCHAN` was not parsed either, so a refused join
was equally invisible.

THE RETRY RIDES ON THE ADVERT TIMER, as suggested, and that is the right
cadence for a reason beyond convenience: an instant rejoin reads as a fight
with whoever kicked us, and is how a kick becomes a ban. The advert interval
is already the bot's rhythm, and it is the moment it was about to speak there
anyway.

GIVING UP IS THE POINT, not retrying. A channel answering "you are banned"
will answer that way for as long as the ban stands, and a bot that keeps
asking earns a longer one. After REJOIN_ATTEMPTS refusals DCCore stops and
says so.

WHY THE RULE LIVES IN irc.py AND THE SEND IN announce.py. The read thread must
not block on a socket write, and the advert worker must not hold a lock or
know what a kick is. So irc.py counts and decides; the worker asks it what to
send.
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

from tests.support import DCCoreTestCase  # noqa: E402


def wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll rather than sleep a fixed time, so the suite is neither slow nor
    flaky - same pattern tests/test_adminchat.py uses for the same reason."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False

OURS = "#somechannel"
ALSO_OURS = "#otherchannel"
NOT_OURS = "#somewhere-else"


class ParsingAKick(unittest.TestCase):

    def test_a_well_formed_kick(self):
        parsed = irc.parse_kick(
            ":someop!user@host KICK #somechannel SomeBot :go away")

        self.assertEqual(parsed, ("someop", "#somechannel", "SomeBot"))

    def test_a_channel_with_an_ampersand_in_its_name(self):
        """Legal per RFC 2812, and the thing six parsers here used to drop."""
        parsed = irc.parse_kick(
            ":someop!user@host KICK #alpha&beta SomeBot :bye")

        self.assertEqual(parsed[1], "#alpha&beta")

    def test_a_kick_typed_into_a_channel_is_not_a_kick(self):
        """Anchored on the server prefix. Unanchored, anyone could say this in
        a channel and have the bot act on it - the same forgery the PRIVMSG
        and user-event parsers were fixed for."""
        forged = (":someone!user@host PRIVMSG #somechannel :"
                  ":x!y@z KICK #somechannel SomeBot :got you")

        self.assertIsNone(irc.parse_kick(forged))

    def test_nonsense_is_not_a_kick(self):
        for line in ("", "KICK", ":only a prefix", ":a!b@c KICK #chan"):
            with self.subTest(line=line):
                self.assertIsNone(irc.parse_kick(line))


class ParsingARefusal(unittest.TestCase):

    def test_banned_from_channel(self):
        parsed = irc.parse_join_refusal(
            ":irc.example.net 474 SomeBot #somechannel :Cannot join channel (+b)")

        self.assertEqual(parsed, ("#somechannel", "474"))

    def test_the_other_three_a_join_can_be_refused_with(self):
        for numeric in ("471", "473", "475"):
            with self.subTest(numeric=numeric):
                line = ":irc.example.net %s SomeBot #somechannel :no" % numeric
                self.assertEqual(irc.parse_join_refusal(line)[1], numeric)

    def test_a_numeric_that_is_not_a_refusal_is_ignored(self):
        """366 is a join that WORKED. Counting it as a refusal would give up
        on every channel the bot successfully joined."""
        self.assertIsNone(irc.parse_join_refusal(
            ":irc.example.net 366 SomeBot #somechannel :End of /NAMES list."))

    def test_the_set_is_what_it_claims(self):
        """Guard on the guard: an empty set would make every assertion above
        about parsing pass and the counting never happen.

        405 joined the four in #510. It belongs with them rather than with a
        throttle - "you have joined too many channels" keeps being true until
        the operator serves fewer, which is the same shape as a ban and the
        same reason a bounded retry is right. It is answered in different
        WORDS, because "gave up after 3 attempts" would send somebody looking
        for a fault on the channel's side.

        476, 477 and 479 joined in #632: a bad channel name and Undernet's
        +r (needs a services login) are refusals that keep being refusals,
        and were being answered in silence and retried for ever."""
        self.assertEqual(irc.JOIN_REFUSED_NUMERICS,
                         {"405", "471", "473", "474", "475", "476", "477", "479"})


class WhatWeStartTrackingAndWhy(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="%s, %s" % (OURS, ALSO_OURS),
                        REJOIN_ATTEMPTS=3)

    def test_a_kick_from_one_of_ours_is_recorded(self):
        self.assertTrue(irc.note_kicked_from(OURS, "someop"))
        self.assertIn(OURS, irc.channels_to_rejoin())

    def test_a_kick_from_somewhere_we_never_asked_to_be_is_not(self):
        """Somebody invited the bot, or the channel has been taken out of
        CHANNEL. Rejoining would be the bot deciding where it belongs."""
        self.assertFalse(irc.note_kicked_from(NOT_OURS, "someop"))
        self.assertEqual(irc.channels_to_rejoin(), [])

    def test_the_case_of_the_channel_does_not_matter(self):
        """IRC channel names are case-insensitive, and an operator types
        CHANNEL by hand."""
        self.assertTrue(irc.note_kicked_from(OURS.upper(), "someop"))
        self.assertIn(OURS, irc.channels_to_rejoin())

    def test_who_did_it_is_kept(self):
        irc.note_kicked_from(OURS, "someop")

        self.assertEqual(self.config.kicked_channels[OURS]["by"], "someop")


class TheCountIsWhatStopsIt(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=OURS, REJOIN_ATTEMPTS=3)
        irc.note_kicked_from(OURS, "someop")

    def test_it_is_tried_while_it_has_attempts_left(self):
        for expected in (1, 2):
            self.assertEqual(irc.note_join_refused(OURS), expected)
            self.assertIn(OURS, irc.channels_to_rejoin(),
                          "gave up with attempts remaining")

    def test_and_left_alone_once_they_are_used(self):
        for _ in range(3):
            irc.note_join_refused(OURS)

        self.assertEqual(irc.channels_to_rejoin(), [])
        self.assertEqual(irc.gave_up_on(), [OURS])

    def test_a_refusal_for_a_channel_we_are_not_chasing_counts_nothing(self):
        """Otherwise an ordinary failed JOIN - somewhere the bot was never in
        - would start it knocking on a door nobody asked about."""
        self.assertEqual(irc.note_join_refused(NOT_OURS), 0)
        self.assertNotIn(NOT_OURS, irc.gave_up_on())

    def test_a_join_that_works_clears_the_record_entirely(self):
        irc.note_join_refused(OURS)

        self.assertIsNotNone(irc.note_joined(OURS))
        self.assertEqual(irc.channels_to_rejoin(), [])
        self.assertEqual(irc.gave_up_on(), [])

    def test_and_a_later_kick_starts_from_zero_again(self):
        """The count is CONSECUTIVE refusals. A channel that let us back in
        and threw us out again months later is not two-thirds of the way to
        being abandoned."""
        for _ in range(2):
            irc.note_join_refused(OURS)
        irc.note_joined(OURS)

        irc.note_kicked_from(OURS, "someop")

        self.assertEqual(self.config.kicked_channels[OURS]["refusals"], 0)
        self.assertIn(OURS, irc.channels_to_rejoin())

    def test_zero_attempts_means_never(self):
        """An operator who does not want the bot chasing channels at all."""
        self.set_config(REJOIN_ATTEMPTS=0)

        self.assertEqual(irc.channels_to_rejoin(), [])

    def test_and_zero_attempts_is_not_the_same_as_having_given_up(self):
        """The asymmetry a mutation run exposed. `refusals >= 0` is true for
        every entry, so without its own guard gave_up_on() would report a
        channel the bot had never tried - and the operator would be told
        DCCore had abandoned somewhere it was told not to chase."""
        self.set_config(REJOIN_ATTEMPTS=0)

        self.assertEqual(irc.gave_up_on(), [],
                         "reported giving up on a channel it never tried")

    def test_a_channel_removed_from_CHANNEL_stops_being_chased(self):
        """The operator's answer to a ban is often to stop serving there. The
        retry must not outlive the configuration that wanted it."""
        self.set_config(CHANNEL=ALSO_OURS)

        self.assertEqual(irc.channels_to_rejoin(), [])


class TheOrderOfResponsibilities(unittest.TestCase):
    """The read thread must not block on a socket write, and the advert worker
    must not hold a lock or know what a kick is."""

    def source(self, name):
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_read_loop_only_records(self):
        body = self.source("irc.py")
        watch = body.split("kick_parsed = parse_kick(line)", 1)[1][:900]

        self.assertNotIn("JOIN ", watch,
                         "the read thread is sending a JOIN itself")

    def test_the_worker_sends_what_irc_py_decides(self):
        worker = self.source("announce.py")

        self.assertIn("irc_mod.channels_to_rejoin()", worker)
        self.assertIn('f"JOIN {waiting}', worker)

    def test_the_worker_holds_no_rule_of_its_own(self):
        """No count, no limit, no comparison - if the rule were duplicated
        here the two could disagree about when to stop."""
        worker = self.source("announce.py")
        block = worker.split("for waiting in irc_mod.channels_to_rejoin():", 1)[1][:400]

        self.assertNotIn("REJOIN_ATTEMPTS", block)
        self.assertNotIn("refusals", block)

    def test_a_failure_in_the_rejoin_cannot_stop_the_advert(self):
        """It runs inside the advert loop. An exception there would take the
        thing the bot exists to do with it."""
        worker = self.source("announce.py")
        block = worker.split("for waiting in irc_mod.channels_to_rejoin():", 1)[0]

        self.assertIn("try:", block.rsplit("import irc as irc_mod", 1)[0][-200:])


class TheRejoinBlockCanReadOserveOnItsFirstCycle(DCCoreTestCase):
    """#435: oserve is a plain local in announce_worker() - no `global`, no
    module-level binding in announce.py - and was first ASSIGNED some 80
    lines into the per-channel loop, while the rejoin block that READS it
    sits a few lines above that loop. So it raised UnboundLocalError on
    every worker's FIRST cycle (every reconnect starts a new worker), caught
    by the block's own broad except and printed as one line - and forever on
    an install where every channel hits the loop's OWN `continue` (no list
    bound, or before the first !update) before ever reaching the old
    assignment.

    Source-reading cannot tell "assigned before this read" apart from
    "assigned after" - both look like a binding exists somewhere in the
    function, which is why TheOrderOfResponsibilities above (checking what
    the worker calls and holds no rule of its own) never caught this. This
    drives one real cycle of the actual `while True:` loop instead, the same
    way the audit that found the bug did, using current_worker_id to stop it
    once the cycle's effect is observed - the loop has no other exit.
    """

    def setUp(self):
        super().setUp()
        self._real_worker_id = announce.current_worker_id
        self.addCleanup(setattr, announce, "current_worker_id", self._real_worker_id)
        self._real_is_ready = announce.is_ready
        self.addCleanup(setattr, announce, "is_ready", self._real_is_ready)
        self._real_list_name_for_request = library.list_name_for_request
        self.addCleanup(setattr, library, "list_name_for_request",
                        self._real_list_name_for_request)
        # Real-time seconds, not IRC time: the loop must cycle back to its
        # current_worker_id check quickly once this test is done observing
        # it, rather than sleeping out a production-sized ANNOUNCE_INTERVAL.
        self.set_config(ANNOUNCE_INTERVAL=0.01, CHANNEL=OURS)
        irc.note_kicked_from(OURS, "someop")

    def start_worker(self):
        announce.is_ready = True
        thread = threading.Thread(target=announce.announce_worker, daemon=True)
        thread.start()
        return thread

    def stop_worker(self, thread):
        # Anything other than the value the thread bound to itself at
        # start - the loop's own guard then breaks out on its next pass,
        # which ANNOUNCE_INTERVAL=0.01 (set in setUp) makes prompt.
        announce.current_worker_id = object()
        thread.join(5)

    def test_the_rejoin_is_queued_even_when_no_list_is_bound(self):
        """The critical case: every channel hits the "no list bound"
        continue before the loop ever reaches the old assignment site, so
        the rejoin block was UnboundLocalError FOREVER on an install like
        this - not just on the first cycle."""
        library.list_name_for_request = lambda channel=None: None

        thread = self.start_worker()
        try:
            self.assertTrue(
                wait_for(lambda: any(msg.startswith(f"JOIN {OURS}")
                                     for _user, msg, _vip in self.oserve.queued)),
                f"no JOIN for {OURS} was ever queued - self.oserve.queued: "
                f"{self.oserve.queued!r}")
        finally:
            self.stop_worker(thread)

    def test_no_rejoin_error_is_printed_when_no_list_is_bound(self):
        """The other half of the same evidence: not just "a JOIN appeared
        eventually by some other path", but "the rejoin block itself did not
        crash". UnboundLocalError, caught by the rejoin's own except, prints
        exactly this line."""
        library.list_name_for_request = lambda channel=None: None
        import contextlib as _contextlib
        import io as _io

        sink = _io.StringIO()
        thread = self.start_worker()
        try:
            wait_for(lambda: any(msg.startswith(f"JOIN {OURS}")
                                 for _user, msg, _vip in self.oserve.queued))
            with _contextlib.redirect_stdout(sink):
                time.sleep(0.1)
        finally:
            self.stop_worker(thread)

        self.assertNotIn("REJOIN ERROR", sink.getvalue())
        self.assertNotIn("not associated with a value", sink.getvalue())


class TheStateIsLiveStateAndFollowsThoseRules(unittest.TestCase):
    """runtime.py containers have four separate contracts and this is the
    fourth thing that has had to learn them."""

    def test_it_lives_in_runtime(self):
        import runtime

        self.assertIsInstance(runtime.kicked_channels, dict)

    def test_it_survives_a_rehash(self):
        """Losing it restarts the count at zero on every rehash, so a channel
        that has refused three times gets three more tries each time the
        operator saves a setting."""
        import commands

        self.assertIn("kicked_channels", commands.PRESERVE_RUNTIME)

    def test_and_the_harness_resets_it_between_tests(self):
        from tests import support

        self.assertIn("kicked_channels", support.RUNTIME_CONTAINERS)


if __name__ == "__main__":
    unittest.main()
