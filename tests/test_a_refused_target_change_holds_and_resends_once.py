"""Target change too fast: the pacer holds, and the dropped line goes again once (#1386).

ircu limits how fast a client may start messaging new targets, and answers a
line over the limit with

    :irc.example.net 439 SomeBot dave :Target change too fast. Please wait 8 seconds.

(707 on hybrid, ratbox and charybdis, with no time in it). The line was
DROPPED. There was no handler: the refusal went by without a log line, the
reply never reached the user, and the pacer carried on at MSG_DELAY straight
into the server's window, so the next lines to new targets were likely
dropped too.

Now the read loop answers it: every paced lane is held for the time the
server asked (10 s when it names none, never more than 120 s), and the line
that went to that target is put back at the front of the lane it came from,
marked, so that a second refusal of the same line drops it instead of
looping.

Driven for real throughout: the read loop's guard is read out of irc.py and
evaluated; the answer is called on real numerics and checked against each
lane; the queue worker and the debug drain run as the threads they are, and
the DCC ACCEPT and SEND handshakes go out through the real dcc.py.
"""

import contextlib
import io
import socket
import sys
import threading
import time
import unittest

import adminchat
import announce
import dcc
import dcc_fetch
import defaults as config
import irc
import queue_mgr
import runtime

from tests.support import DCCoreTestCase, RecordingSocket, silence_debug
from tests.test_membership_events import _evaluate, _guard_condition_for
from tests import test_a_receiver_that_never_connects_is_said_so as never_connects
from tests import test_a_refused_send_on_its_own as refused_send
from tests import test_a_shared_outbound_pace as shared_pace
from tests import test_the_reconnect_backs_off as reconnect


BOT = "SomeBot"


def refusal(target, text="Target change too fast. Please wait 8 seconds.",
            code="439", to=BOT):
    return f":irc.example.net {code} {to} {target} :{text}"


REFUSED_439 = refusal("dave")
REFUSED_707 = refusal("dave", "Targets changing too fast, message dropped", code="707")

# The marker for the read loop's call. Indented, so the `def` line - which
# holds the same name and argument after "def " - is not a second match.
ANSWER_CALL = "    _answer_target_change(line)"


def recorded():
    with runtime.recent_paced_lock:
        return [dict(entry) for entry in runtime.recent_paced_lines]


def answer(line):
    """irc._answer_target_change(line), and what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        seconds = irc._answer_target_change(line)
    return seconds, out.getvalue()


def wait_until(condition, timeout=10.0):
    """Poll `condition` until it holds or the time is up; whether it held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


class TheHold(unittest.TestCase):
    """runtime.OutboundPacer.hold_for(): every lane waits on this one clock."""

    def test_the_next_slot_moves_to_the_end_of_the_hold(self):
        pacer = runtime.OutboundPacer()
        before = time.monotonic()

        pacer.hold_for(8)

        self.assertGreaterEqual(pacer._next_allowed, before + 8)

    def test_a_shorter_hold_after_it_does_not_bring_the_slot_forward(self):
        pacer = runtime.OutboundPacer()
        pacer.hold_for(8)
        held_until = pacer._next_allowed

        pacer.hold_for(1)

        self.assertEqual(pacer._next_allowed, held_until)

    def test_a_short_hold_does_not_shorten_a_slot_already_booked(self):
        """A hold never brings a send forward: a slot booked further out
        than the hold stays where it was."""
        pacer = runtime.OutboundPacer()
        pacer.wait_for_slot(30)
        booked = pacer._next_allowed

        pacer.hold_for(1)

        self.assertEqual(pacer._next_allowed, booked)

    def test_a_waiter_already_blocked_does_not_get_its_slot_before_the_hold_ends(self):
        """The case that matters: a lane is asleep towards the slot it was
        promised when the refusal arrives. It must wake to the new time, not
        send at the old one."""
        pacer = runtime.OutboundPacer()
        pacer.wait_for_slot(1.0)          # booked: about one second from now
        got = {}

        def lane():
            pacer.wait_for_slot(0)
            got["at"] = time.monotonic()

        thread = threading.Thread(target=lane, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        def queued():
            with pacer._cond:
                return pacer._next_ticket == 2 and pacer._serving == 1
        self.assertTrue(wait_until(queued, 5), "the waiter never took its ticket")

        pacer.hold_for(1.2)
        with pacer._cond:
            held_until = pacer._next_allowed

        thread.join(5)
        self.assertFalse(thread.is_alive(), "the waiter never got its slot")
        self.assertIn("at", got)
        self.assertGreaterEqual(got["at"], held_until,
                                "a waiter blocked before the hold sent inside it")


class TheReadLoopGuard(unittest.TestCase):
    """The guard in front of _answer_target_change(), read out of irc.py
    and evaluated (docs/CONVENTIONS.md rule 5)."""

    def setUp(self):
        self.addCleanup(setattr, config, "NICKNAME", config.NICKNAME)
        config.NICKNAME = BOT

    def condition(self):
        return _guard_condition_for(ANSWER_CALL)

    def test_a_real_439_and_a_real_707_each_pass_it(self):
        for line in (REFUSED_439, REFUSED_707):
            with self.subTest(line=line):
                self.assertTrue(_evaluate(self.condition(), line), self.condition())

    def test_channel_text_quoting_the_numeric_does_not(self):
        """Matched by position (rule 4): anyone can type the numeric."""
        for forged in (f":x!u@h PRIVMSG #chan :439 {BOT} SomeUser :Please wait 8 seconds.",
                       f":x!u@h NOTICE {BOT} :707 {BOT} dave :message dropped",
                       ":x!u@h PART #chan :439 707",
                       f":irc.example.net 372 {BOT} :- the motd says 439 {BOT} dave"):
            with self.subTest(line=forged):
                self.assertFalse(_evaluate(self.condition(), forged), self.condition())


class WhatCountsAsARefusal(unittest.TestCase):
    """irc._parse_target_change(): (target, seconds), or None."""

    def setUp(self):
        self.addCleanup(setattr, config, "NICKNAME", config.NICKNAME)
        config.NICKNAME = BOT
        self.addCleanup(setattr, runtime, "server_casemapping", runtime.server_casemapping)
        runtime.server_casemapping = "rfc1459"

    def test_the_time_ircu_names_is_the_time_held(self):
        self.assertEqual(irc._parse_target_change(REFUSED_439), ("dave", 8.0))

    def test_a_707_or_a_439_without_a_number_gets_the_default(self):
        for line in (REFUSED_707,
                     refusal("dave", "Target change too fast."),
                     refusal("dave", "Target change too fast. Please wait 30 seconds.",
                             code="707")):
            with self.subTest(line=line):
                self.assertEqual(irc._parse_target_change(line),
                                 ("dave", irc.TARGET_CHANGE_HOLD_DEFAULT))
        self.assertEqual(irc.TARGET_CHANGE_HOLD_DEFAULT, 10.0)

    def test_a_huge_time_is_capped(self):
        """A malformed or hostile number must not silence the bot for long."""
        line = refusal("dave", "Target change too fast. Please wait 99999 seconds.")

        self.assertEqual(irc._parse_target_change(line), ("dave", irc.TARGET_CHANGE_HOLD_MAX))
        self.assertEqual(irc.TARGET_CHANGE_HOLD_MAX, 120.0)

    def test_the_line_ending_does_not_matter(self):
        self.assertEqual(irc._parse_target_change(REFUSED_439 + "\r\n"), ("dave", 8.0))

    def test_our_nick_is_matched_as_the_server_folds_it(self):
        config.NICKNAME = "Some[Bot]"
        for to in ("Some[Bot]", "SOME[BOT]", "some{bot}"):
            with self.subTest(to=to):
                self.assertEqual(irc._parse_target_change(refusal("dave", to=to)), ("dave", 8.0))

    def test_a_refusal_for_another_nick_is_not_ours(self):
        self.assertIsNone(irc._parse_target_change(refusal("dave", to="OtherBot")))

    def test_a_refusal_without_a_target_is_not_one(self):
        for line in (f":irc.example.net 439 {BOT} :Target change too fast. Please wait 8 seconds.",
                     f":irc.example.net 707 {BOT}"):
            with self.subTest(line=line):
                self.assertIsNone(irc._parse_target_change(line))

    def test_channel_text_is_not_one(self):
        self.assertIsNone(irc._parse_target_change(
            f":x!u@h PRIVMSG #chan :439 {BOT} SomeUser :Please wait 8 seconds."))


class _RefusalCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME=BOT)
        silence_debug(announce)


class TheHoldIsApplied(_RefusalCase):

    def test_a_439_holds_the_shared_pacer_for_the_time_asked(self):
        before = time.monotonic()

        seconds, out = answer(REFUSED_439)

        self.assertEqual(seconds, 8.0)
        self.assertGreaterEqual(runtime.outbound_pacer._next_allowed, before + 8)
        self.assertIn("[QUEUE] The server refused a message to dave: target changes too fast. "
                      "Holding paced output for 8 s, nothing to resend.", out)

    def test_a_huge_time_holds_for_the_cap_and_no_longer(self):
        seconds, _out = answer(refusal("dave", "Please wait 86400 seconds."))
        after = time.monotonic()

        self.assertEqual(seconds, 120.0)
        self.assertLessEqual(runtime.outbound_pacer._next_allowed, after + 120)

    def test_what_is_not_a_refusal_for_us_does_nothing_at_all(self):
        """Lines that pass the guard (a 439 to another nick, a 439 with no
        target) or would pass a looser one (channel text): no hold, the
        record untouched, nothing printed."""
        runtime.note_paced_line("NOTICE dave :your reply\r\n", "standard", owner="dave")
        for line in (refusal("dave", to="OtherBot"),
                     f":irc.example.net 439 {BOT} :Target change too fast. Please wait 8 seconds.",
                     f":x!u@h PRIVMSG #chan :439 {BOT} dave :Please wait 8 seconds."):
            with self.subTest(line=line):
                seconds, out = answer(line)
                self.assertIsNone(seconds)
                self.assertEqual(out, "")
                self.assertEqual(runtime.outbound_pacer._next_allowed, 0.0)
                self.assertEqual(len(recorded()), 1)
        self.assertEqual(config.send_queue, {})


class TheDroppedLineGoesBackToItsLane(_RefusalCase):

    def test_the_standard_lane_the_front_of_that_users_own_queue(self):
        line = "NOTICE dave :Your request is number 2 in the queue.\r\n"
        config.send_queue["dave"] = ["NOTICE dave :the next line\r\n"]
        config.send_queue["erin"] = ["NOTICE erin :not hers\r\n"]
        runtime.note_paced_line(line, "standard", owner="dave")

        _seconds, out = answer(REFUSED_439)

        self.assertEqual(config.send_queue["dave"], [line, "NOTICE dave :the next line\r\n"])
        self.assertIsInstance(config.send_queue["dave"][0], runtime.ResentLine)
        self.assertEqual(config.send_queue["erin"], ["NOTICE erin :not hers\r\n"])
        self.assertIn("then sending it again.", out)

    def test_the_standard_lane_of_a_user_whose_queue_has_emptied(self):
        line = "NOTICE dave :last line\r\n"
        runtime.note_paced_line(line, "standard", owner="dave")

        answer(REFUSED_439)

        self.assertEqual(config.send_queue["dave"], [line])

    def test_the_vip_lane(self):
        line = "PRIVMSG #somechannel :a reply in the express lane\r\n"
        config.vip_queue[:] = ["PRIVMSG #somechannel :an advert\r\n"]
        runtime.note_paced_line(line, "vip")

        answer(refusal("#somechannel"))

        self.assertEqual(config.vip_queue, [line, "PRIVMSG #somechannel :an advert\r\n"])
        self.assertIsInstance(config.vip_queue[0], runtime.ResentLine)

    def test_the_debug_drain(self):
        line = "PRIVMSG #somechannel-debug :a debug line\r\n"
        announce._debug_queue.append("PRIVMSG #somechannel-debug :the next one\r\n")
        runtime.note_paced_line(line, "debug")

        answer(refusal("#somechannel-debug"))

        self.assertEqual(list(announce._debug_queue),
                         [line, "PRIVMSG #somechannel-debug :the next one\r\n"])
        self.assertIsInstance(announce._debug_queue[0], runtime.ResentLine)


class TheFetchLaneOnlyForARowStillWaiting(_RefusalCase):
    """A request to another bot goes again only while its row is "offered"
    with that very line: a row cancelled, failed or asked again since has
    had its line taken back (#1028), and sending it now would have the
    other bot send a file nobody waits for."""

    LINE = "PRIVMSG #somechannel :!PeerBot Some Album.flac\r\n"

    def refuse(self):
        runtime.note_paced_line(self.LINE, "fetch")
        return answer(refusal("#somechannel"))

    def test_an_offered_row_with_that_line_has_it_put_back(self):
        config.fetch_queue["r1"] = {"state": "offered", "request_line": self.LINE}
        config.fetch_request_queue[:] = ["PRIVMSG #otherchannel :@OtherPeer\r\n"]

        _seconds, out = self.refuse()

        self.assertEqual(config.fetch_request_queue,
                         [self.LINE, "PRIVMSG #otherchannel :@OtherPeer\r\n"])
        self.assertIsInstance(config.fetch_request_queue[0], runtime.ResentLine)
        self.assertIn("then sending it again.", out)

    def test_a_row_no_longer_offered_does_not(self):
        for state in ("pending", "failed", "cancelled", "receiving"):
            with self.subTest(state=state):
                config.fetch_queue.clear()
                config.fetch_request_queue[:] = []
                config.fetch_queue["r1"] = {"state": state, "request_line": self.LINE}

                _seconds, out = self.refuse()

                self.assertEqual(config.fetch_request_queue, [])
                self.assertIn("nothing to resend.", out)

    def test_a_row_that_asked_again_with_another_line_does_not(self):
        config.fetch_queue["r1"] = {"state": "offered",
                                    "request_line": "PRIVMSG #otherchannel :!PeerBot Some Album.flac\r\n"}

        _seconds, out = self.refuse()

        self.assertEqual(config.fetch_request_queue, [])
        self.assertIn("nothing to resend.", out)

    def test_no_row_at_all_does_not(self):
        _seconds, out = self.refuse()

        self.assertEqual(config.fetch_request_queue, [])
        self.assertIn("nothing to resend.", out)

    def test_a_line_already_waiting_again_is_not_added_twice(self):
        config.fetch_queue["r1"] = {"state": "offered", "request_line": self.LINE}
        config.fetch_request_queue[:] = [self.LINE]

        self.refuse()

        self.assertEqual(config.fetch_request_queue, [self.LINE])

    def test_the_helper_says_whether_it_put_it_back(self):
        self.assertFalse(dcc_fetch._put_back_refused_request(self.LINE))
        config.fetch_queue["r1"] = {"state": "offered", "request_line": self.LINE}
        self.assertTrue(dcc_fetch._put_back_refused_request(self.LINE))


class OnceOnly(_RefusalCase):

    def test_a_line_refused_again_after_its_resend_is_dropped(self):
        line = "NOTICE dave :your reply\r\n"
        runtime.note_paced_line(line, "standard", owner="dave")
        answer(REFUSED_439)

        # What the queue worker does with it: pop it from the lane, send it,
        # record it - the line it records is the marked one.
        user, resent = queue_mgr.next_standard_line(config.send_queue, None)
        self.assertEqual((user, resent), ("dave", line))
        runtime.note_paced_line(resent, "standard", owner=user)
        self.assertTrue(recorded()[-1]["resent"])

        _seconds, out = answer(REFUSED_439)

        self.assertEqual(config.send_queue.get("dave", []), [])
        self.assertIn("it was already sent again once, so it is dropped.", out)

    def test_a_plain_line_is_recorded_as_not_resent(self):
        runtime.note_paced_line("NOTICE dave :x\r\n", "standard", owner="dave")

        self.assertFalse(recorded()[-1]["resent"])

    def test_a_refusal_answers_a_record_once(self):
        """Taken off the record: a second refusal for the same target, with
        no newer line sent there, finds nothing."""
        runtime.note_paced_line("NOTICE dave :x\r\n", "standard", owner="dave")
        answer(REFUSED_439)
        config.send_queue.clear()

        _seconds, out = answer(REFUSED_439)

        self.assertEqual(config.send_queue, {})
        self.assertIn("nothing to resend.", out)


class WhatTheRecordHolds(_RefusalCase):

    def test_only_privmsg_and_notice_are_recorded(self):
        for line in ("PING :irc.example.net\r\n", "JOIN #somechannel\r\n",
                     "MODE SomeBot +x\r\n", "PRIVMSG\r\n", "", None):
            runtime.note_paced_line(line, "vip")
        self.assertEqual(recorded(), [])

        runtime.note_paced_line("privmsg dave :lower case is still a message\r\n", "vip")
        runtime.note_paced_line("NOTICE dave :x\r\n", "vip")
        self.assertEqual([entry["target"] for entry in recorded()], ["dave", "dave"])

    def test_what_a_record_holds(self):
        line = "NOTICE dave :x\r\n"
        runtime.note_paced_line(line, "standard", owner="dave")

        entry = recorded()[-1]
        self.assertEqual({k: entry[k] for k in ("target", "line", "lane", "owner", "resent")},
                         {"target": "dave", "line": line, "lane": "standard",
                          "owner": "dave", "resent": False})

    def test_a_line_older_than_the_window_is_not_taken(self):
        runtime.note_paced_line("NOTICE dave :long ago\r\n", "standard", owner="dave")
        with runtime.recent_paced_lock:
            runtime.recent_paced_lines[-1]["at"] -= runtime.PACED_LINE_WINDOW + 1

        self.assertIsNone(runtime.take_paced_line("dave"))

    def test_a_line_inside_the_window_is(self):
        runtime.note_paced_line("NOTICE dave :not so long ago\r\n", "standard", owner="dave")
        with runtime.recent_paced_lock:
            runtime.recent_paced_lines[-1]["at"] -= runtime.PACED_LINE_WINDOW - 1

        self.assertEqual(runtime.take_paced_line("dave")["line"], "NOTICE dave :not so long ago\r\n")

    def test_the_most_recent_line_to_the_target_is_the_one_taken(self):
        runtime.note_paced_line("NOTICE dave :first\r\n", "standard", owner="dave")
        runtime.note_paced_line("NOTICE erin :between\r\n", "standard", owner="erin")
        runtime.note_paced_line("NOTICE dave :second\r\n", "standard", owner="dave")

        self.assertEqual(runtime.take_paced_line("dave")["line"], "NOTICE dave :second\r\n")
        self.assertEqual([e["line"] for e in recorded()],
                         ["NOTICE dave :first\r\n", "NOTICE erin :between\r\n"])

    def test_the_target_is_matched_as_the_server_folds_it(self):
        self.addCleanup(setattr, runtime, "server_casemapping", runtime.server_casemapping)
        runtime.server_casemapping = "rfc1459"
        line = "PRIVMSG #Some[Channel] :x\r\n"
        runtime.note_paced_line(line, "vip")

        answer(refusal("#some{channel}"))

        self.assertEqual(config.vip_queue, [line])

    def test_the_record_is_bounded(self):
        for n in range(runtime.PACED_LINES_KEPT + 10):
            runtime.note_paced_line(f"NOTICE user{n} :x\r\n", "vip")
        self.assertEqual(len(recorded()), runtime.PACED_LINES_KEPT)

    def test_a_line_no_lane_can_take_back_masks_an_older_one_to_that_nick(self):
        """A DCC ACCEPT or SEND handshake is recorded with no lane. Its
        refusal must not be taken for the reply sent to the same user just
        before it, which would then go out twice."""
        reply = "NOTICE dave :Sending you Some Album.flac\r\n"
        runtime.note_paced_line(reply, "standard", owner="dave")
        runtime.note_paced_line("PRIVMSG dave :\x01DCC ACCEPT Some_Album.flac 51000 4096\x01\r\n", None)

        _seconds, out = answer(REFUSED_439)

        self.assertEqual(config.send_queue, {})
        self.assertIn("nothing to resend.", out)


class TheDccHandshakesAreRecordedWithNoLane(_RefusalCase):
    """dcc.py's two direct sends, through the real code."""

    def test_a_resume_accept_masks_the_reply_before_it(self):
        self.set_config(MSG_DELAY=0)
        key = ("dave", 51000)
        runtime.dcc_send_offers[key] = {"filename": "Some_Album.flac", "size": 10000,
                                        "position": 4096}
        self.addCleanup(runtime.dcc_send_offers.pop, key, None)
        runtime.note_paced_line("NOTICE dave :an older reply\r\n", "standard", owner="dave")
        sock = RecordingSocket()

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(dcc._send_resume_accept(sock, "dave", key))

        self.assertIn("DCC ACCEPT", sock.text())
        entry = recorded()[-1]
        self.assertEqual((entry["target"], entry["lane"]), ("dave", None))
        self.assertIn("DCC ACCEPT Some_Album.flac 51000 4096", entry["line"])

        _seconds, out = answer(REFUSED_439)

        self.assertEqual(config.send_queue, {})
        self.assertIn("nothing to resend.", out)


class TheDccSendHandshake(never_connects.NobodyConnects):
    """The whole send path, with nobody connecting: what matters here is
    only that the offer it made went on the record with no lane."""

    def test_the_handshake_masks_the_reply_before_it(self):
        self.set_config(NICKNAME=BOT)
        user = never_connects.USER
        runtime.note_paced_line(f"NOTICE {user} :an older reply\r\n", "standard", owner=user)

        with contextlib.redirect_stdout(io.StringIO()):
            sock = self.run_the_send()

        self.assertTrue(sock.handshake_seen.is_set(), "the offer never went out")
        offers = [e for e in recorded() if "DCC SEND" in e["line"]]
        self.assertEqual(len(offers), 1, recorded())
        self.assertEqual((offers[0]["target"], offers[0]["lane"]), (user, None))

        config.send_queue.clear()
        _seconds, out = answer(refusal(user))

        self.assertEqual(config.send_queue, {})
        self.assertIn("nothing to resend.", out)


for _name in [n for n in dir(never_connects.NobodyConnects) if n.startswith("test")]:
    setattr(TheDccSendHandshake, _name, None)


class TheRealSendersRecordTheirLanes(DCCoreTestCase):
    """queue_mgr.queue_worker() and announce's debug drain, as the threads
    they are (the fixture of test_a_shared_outbound_pace)."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME=BOT, MSG_DELAY=0.01, DEBUG_MSG_DELAY=0.0)
        silence_debug(announce)
        config.bot_joined_channel = True
        self.oserve.bot_joined_channel = True
        config.activation_triggered = True
        self.sock = shared_pace.TimestampedSocket()
        self.oserve.irc_connection = self.sock
        self.threads = []
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        sys.stdout = io.StringIO()

    def run_worker(self, module, target):
        shim = shared_pace._SleepShim()
        real_time = module.time
        module.time = shim

        def run():
            try:
                target()
            except SystemExit:
                pass
        thread = threading.Thread(target=run, daemon=True)

        def stop():
            shim.stopped.set()
            thread.join(5)
            module.time = real_time
        self.addCleanup(stop)
        thread.start()

    def sent(self):
        return [payload.decode("utf-8") for _at, payload in list(self.sock.sent)]

    def test_each_lane_of_the_queue_worker_records_its_lane(self):
        fetch_line = "PRIVMSG #otherchannel :!PeerBot Some Album.flac\r\n"
        vip_line = "PRIVMSG #somechannel :an advert\r\n"
        standard_line = "NOTICE dave :your reply\r\n"
        config.fetch_request_queue[:] = [fetch_line]
        config.vip_queue[:] = [vip_line]
        config.send_queue["dave"] = [standard_line]

        self.run_worker(queue_mgr, queue_mgr.queue_worker)

        self.assertTrue(wait_until(lambda: len(recorded()) == 3), recorded())
        lanes = {e["line"]: (e["lane"], e["owner"]) for e in recorded()}
        self.assertEqual(lanes, {fetch_line: ("fetch", None),
                                 vip_line: ("vip", None),
                                 standard_line: ("standard", "dave")})

    def test_a_refused_reply_goes_out_twice_and_no_more(self):
        line = "NOTICE dave :your reply\r\n"
        config.send_queue["dave"] = [line]
        self.run_worker(queue_mgr, queue_mgr.queue_worker)
        self.assertTrue(wait_until(lambda: len(recorded()) == 1), "the reply never went out")

        # "wait 0 seconds": the hold is real, and over at once.
        refused = refusal("dave", "Target change too fast. Please wait 0 seconds.")
        irc._answer_target_change(refused)
        self.assertTrue(wait_until(lambda: self.sent().count(line) == 2
                                   and len(recorded()) == 1 and recorded()[0]["resent"]),
                        (self.sent(), recorded()))

        irc._answer_target_change(refused)

        self.assertEqual(config.send_queue.get("dave", []), [])
        self.assertEqual(recorded(), [])
        self.assertIn("so it is dropped.", sys.stdout.getvalue())

    def test_the_debug_drain_records_its_lane_and_resends_once(self):
        line = "PRIVMSG #somechannel-debug :a debug line\r\n"
        announce._debug_queue.append(line)
        announce._debug_drain_id = "refusal-test-drain"
        self.run_worker(announce, lambda: announce._debug_drain_worker("refusal-test-drain"))
        self.assertTrue(wait_until(lambda: len(recorded()) == 1), "the debug line never went out")
        self.assertEqual(recorded()[0]["lane"], "debug")

        irc._answer_target_change(refusal("#somechannel-debug",
                                          "Target change too fast. Please wait 0 seconds."))

        self.assertTrue(wait_until(lambda: self.sent().count(line) == 2
                                   and len(recorded()) == 1 and recorded()[0]["resent"]),
                        (self.sent(), recorded()))


class TheReadLoopAnswersAndTheConnectionTakesTheRecordWithIt(reconnect.ASeriesOfConnections):
    """irc.irc_loop() against a scripted server (#663's harness)."""

    def test_a_real_439_on_the_wire_puts_the_line_back(self):
        line = "NOTICE dave :your reply\r\n"
        runtime.note_paced_line(line, "standard", owner="dave")

        _waits, out = self.run_connections([reconnect.WELCOME, REFUSED_439])

        self.assertIn("[QUEUE] The server refused a message to dave: target changes too fast. "
                      "Holding paced output for 8 s, then sending it again.", out)
        self.assertEqual(config.send_queue.get("dave"), [line])
        self.assertIsInstance(config.send_queue["dave"][0], runtime.ResentLine)

    def test_channel_text_quoting_one_on_the_wire_does_nothing(self):
        runtime.note_paced_line("NOTICE erin :your reply\r\n", "standard", owner="erin")

        _waits, out = self.run_connections([
            reconnect.WELCOME,
            f":erin!u@host.example PRIVMSG #somechannel :439 {BOT} erin :Please wait 8 seconds."])

        self.assertNotIn("[QUEUE] The server refused", out)
        self.assertEqual(config.send_queue.get("erin", []), [])

    def test_the_disconnect_clears_the_record(self):
        """A refusal on the next connection is never about a line sent on
        this one."""
        runtime.note_paced_line("NOTICE dave :sent on the old link\r\n", "standard", owner="dave")
        self.assertEqual(len(recorded()), 1)

        self.run_connections([reconnect.WELCOME])

        self.assertEqual(recorded(), [])


for _name in [n for n in dir(reconnect.ASeriesOfConnections) if n.startswith("test")]:
    setattr(TheReadLoopAnswersAndTheConnectionTakesTheRecordWithIt, _name, None)


class TheResentLineGoesBeforeTheOneThatWaited(unittest.TestCase):
    """queue_mgr._resent_line_first(), on its own."""

    def test_a_resent_line_at_the_front_swaps_with_the_held_one(self):
        lines = [runtime.ResentLine("line 1"), "line 3"]

        self.assertEqual(queue_mgr._resent_line_first(lines, "line 2"), "line 1")
        self.assertEqual(lines, ["line 2", "line 3"])

    def test_a_plain_line_at_the_front_is_left_where_it_is(self):
        lines = ["line 3"]

        self.assertEqual(queue_mgr._resent_line_first(lines, "line 2"), "line 2")
        self.assertEqual(lines, ["line 3"])

    def test_an_empty_or_missing_list_sends_the_held_line(self):
        self.assertEqual(queue_mgr._resent_line_first([], "line 2"), "line 2")
        self.assertEqual(queue_mgr._resent_line_first(None, "line 2"), "line 2")


class _HoldsAfterALine:
    """The IRC socket, which holds the shared pacer for a minute the moment
    `hold_after` goes out: the worker then pops the next line and waits for
    its slot, which is the window a refusal can land in."""

    def __init__(self, hold_after):
        self.hold_after = hold_after
        self.held = False
        self.lock = threading.Lock()
        self.lines = []

    def sendall(self, payload):
        text = payload.decode("utf-8")
        with self.lock:
            self.lines.append(text)
        if text == self.hold_after and not self.held:
            self.held = True
            runtime.outbound_pacer.hold_for(60)

    def sent(self):
        with self.lock:
            return list(self.lines)

    def close(self):
        pass


class ARefusalDuringTheWaitKeepsTheOrder(DCCoreTestCase):
    """The real queue worker. Line 1 goes out; the worker pops line 2 and
    waits for its slot; the refusal of line 1 puts it back at the front.
    Line 2 must not overtake it, and neither goes out twice."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME=BOT, MSG_DELAY=0.01)
        silence_debug(announce)
        config.bot_joined_channel = True
        self.oserve.bot_joined_channel = True
        config.activation_triggered = True
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        sys.stdout = io.StringIO()

    def release_the_pacer(self):
        pacer = runtime.outbound_pacer
        with pacer._cond:
            pacer._next_allowed = 0.0
            pacer._cond.notify_all()

    def start_worker(self):
        shim = shared_pace._SleepShim()
        real_time = queue_mgr.time
        queue_mgr.time = shim

        def run():
            try:
                queue_mgr.queue_worker()
            except SystemExit:
                pass
        thread = threading.Thread(target=run, daemon=True)

        def stop():
            shim.stopped.set()
            self.release_the_pacer()
            thread.join(5)
            queue_mgr.time = real_time
        self.addCleanup(stop)
        thread.start()

    def waiting_for_a_slot(self):
        pacer = runtime.outbound_pacer
        with pacer._cond:
            return pacer._next_ticket - pacer._serving == 1

    def run_the_refusal(self, lane, line1, line2, target):
        sock = _HoldsAfterALine(line1)
        self.oserve.irc_connection = sock
        self.start_worker()

        self.assertTrue(wait_until(lambda: [e["line"] for e in recorded()] == [line1]
                                   and lane() == [] and self.waiting_for_a_slot()),
                        ("line 2 was never popped and held", sock.sent(), lane()))
        irc._answer_target_change(refusal(target, "Target change too fast. Please wait 0 seconds."))
        self.assertEqual(lane(), [line1])
        self.release_the_pacer()

        self.assertTrue(wait_until(lambda: len(sock.sent()) >= 3 and lane() == []),
                        (sock.sent(), lane()))
        self.assertEqual(sock.sent(), [line1, line1, line2])
        self.assertEqual([(e["line"], e["resent"]) for e in recorded()],
                         [(line1, True), (line2, False)])

    def test_in_the_standard_lane(self):
        line1, line2 = "NOTICE dave :line 1\r\n", "NOTICE dave :line 2\r\n"
        config.send_queue["dave"] = [line1, line2]

        self.run_the_refusal(lambda: list(config.send_queue.get("dave") or []),
                             line1, line2, "dave")

    def test_in_the_vip_lane(self):
        line1, line2 = "PRIVMSG #somechannel :line 1\r\n", "PRIVMSG #somechannel :line 2\r\n"
        config.vip_queue[:] = [line1, line2]

        self.run_the_refusal(lambda: list(config.vip_queue), line1, line2, "#somechannel")


class TheRefusalNoticesAreRecordedWithNoLane(refused_send.AnUnsendableFile):
    """dcc.refuse_an_unsendable_file()'s NOTICE, through the real helper
    (the harness of test_a_refused_send_on_its_own)."""

    def test_the_no_address_notice_masks_the_reply_before_it(self):
        self.set_config(NICKNAME=BOT)
        user = refused_send.USER
        runtime.note_paced_line(f"NOTICE {user} :an older reply\r\n", "standard", owner=user)

        self.refuse(ip_long=0)

        entry = recorded()[-1]
        self.assertEqual((entry["target"], entry["lane"]), (user, None))
        self.assertIn("no usable public address", entry["line"])
        _seconds, out = answer(refusal(user))
        self.assertFalse(any(isinstance(line, runtime.ResentLine)
                             for line in config.send_queue.get(user, [])))
        self.assertIn("nothing to resend.", out)


for _name in [n for n in dir(refused_send.AnUnsendableFile) if n.startswith("test")]:
    setattr(TheRefusalNoticesAreRecordedWithNoLane, _name, None)


class TheNoPortNoticesAreRecordedWithNoLane(refused_send.NoPort):
    """dcc.refuse_without_a_port()'s NOTICE, in both its wordings."""

    def test_both_wordings_mask_the_reply_before_them(self):
        self.set_config(NICKNAME=BOT)
        user = refused_send.USER
        for error, wording in ((None, "No available DCC ports."),
                               (OSError("too many open files"), "Your file stays queued.")):
            with self.subTest(wording=wording):
                with runtime.recent_paced_lock:
                    runtime.recent_paced_lines.clear()
                runtime.note_paced_line(f"NOTICE {user} :an older reply\r\n", "standard", owner=user)

                self.refuse(listener_error=error)

                entry = recorded()[-1]
                self.assertEqual((entry["target"], entry["lane"]), (user, None))
                self.assertIn(wording, entry["line"])
                _seconds, out = answer(refusal(user))
                self.assertFalse(any(isinstance(line, runtime.ResentLine)
                                     for line in config.send_queue.get(user, [])))
                self.assertIn("nothing to resend.", out)


for _name in [n for n in dir(refused_send.NoPort) if n.startswith("test")]:
    setattr(TheNoPortNoticesAreRecordedWithNoLane, _name, None)


class _NobodyDials:
    """A console listener nobody connects to: accept() times out at once."""

    def settimeout(self, _value):
        pass

    def accept(self):
        raise socket.timeout("timed out")

    def close(self):
        pass


class TheConsoleOfferIsRecordedWithNoLane(_RefusalCase):
    """adminchat._listen_and_serve_locked()'s DCC CHAT offer, with the
    listener faked: no socket is bound."""

    def setUp(self):
        super().setUp()
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self.addCleanup(setattr, dcc, "get_public_ip_long", dcc.get_public_ip_long)
        dcc.get_public_ip_long = lambda: 134744072          # 8.8.8.8
        self.addCleanup(setattr, adminchat, "_open_chat_listener", adminchat._open_chat_listener)
        adminchat._open_chat_listener = lambda: (_NobodyDials(), 55555)

    def test_the_offer_masks_the_reply_before_it(self):
        runtime.note_paced_line("NOTICE operator :an older reply\r\n", "standard", owner="operator")
        sock = RecordingSocket()

        with contextlib.redirect_stdout(io.StringIO()) as log:
            adminchat._listen_and_serve_locked(sock, "operator", "op!u@host.example", "TOKEN1", None)

        self.assertIn("DCC CHAT chat", sock.text())
        self.assertIn("did not accept the DCC CHAT offer", log.getvalue())
        entry = recorded()[-1]
        self.assertEqual((entry["target"], entry["lane"]), ("operator", None))
        self.assertIn("DCC CHAT chat 134744072 55555 TOKEN1", entry["line"])
        _seconds, out = answer(refusal("operator"))
        self.assertEqual(config.send_queue, {})
        self.assertIn("nothing to resend.", out)


class AMutedOrBannedUserGetsNothingBack(_RefusalCase):
    """A mute or ban drops the user's pending replies (security.is_flooding()),
    so a refused one must not come back through the put-back."""

    LINE = "NOTICE Dave :your reply\r\n"
    SILENCED = "nothing to resend: its user is muted or banned now."

    def refuse(self, lane="standard", owner="Dave", line=None, target="Dave"):
        runtime.note_paced_line(line or self.LINE, lane, owner=owner)
        return answer(refusal(target))

    def test_a_muted_user(self):
        config.muted_until["dave"] = time.time() + 60

        _seconds, out = self.refuse()

        self.assertEqual(config.send_queue.get("Dave", []), [])
        self.assertTrue(out.rstrip().endswith(self.SILENCED), out)

    def test_a_banned_user(self):
        config.banned_users["dave"] = time.time() + 600

        _seconds, out = self.refuse()

        self.assertEqual(config.send_queue.get("Dave", []), [])
        self.assertTrue(out.rstrip().endswith(self.SILENCED), out)

    def test_a_mute_that_has_run_out_does_not_count(self):
        config.muted_until["dave"] = time.time() - 1

        _seconds, out = self.refuse()

        self.assertEqual(config.send_queue.get("Dave"), [self.LINE])
        self.assertIn("then sending it again.", out)

    def test_a_ban_that_has_run_out_does_not_count(self):
        config.banned_users["dave"] = time.time() - 1

        self.refuse()

        self.assertEqual(config.send_queue.get("Dave"), [self.LINE])

    def test_the_hold_still_applies(self):
        config.muted_until["dave"] = time.time() + 60
        before = time.monotonic()

        self.refuse()

        self.assertGreaterEqual(runtime.outbound_pacer._next_allowed, before + 8)

    def test_only_the_standard_lane_is_affected(self):
        config.muted_until["dave"] = time.time() + 60
        config.banned_users["dave"] = time.time() + 600
        channel_line = "PRIVMSG #somechannel :an advert\r\n"
        debug_line = "PRIVMSG #somechannel-debug :a debug line\r\n"

        self.refuse(lane="vip", owner=None, line=channel_line, target="#somechannel")
        self.refuse(lane="debug", owner=None, line=debug_line, target="#somechannel-debug")
        _seconds, out = self.refuse(lane="vip", owner=None, target="Dave")

        self.assertEqual(config.vip_queue, [self.LINE, channel_line])
        self.assertEqual(list(announce._debug_queue), [debug_line])
        self.assertIn("then sending it again.", out)

    def test_someone_else_being_muted_does_not_count(self):
        config.muted_until["erin"] = time.time() + 60

        self.refuse()

        self.assertEqual(config.send_queue.get("Dave"), [self.LINE])


class TheHarnessForgetsTheRecord(DCCoreTestCase):
    """tests/support.reset_config() clears it, as it renews the pacer: a
    line one test sent is not the next test's to resend."""

    def test_reset_config_empties_it(self):
        runtime.note_paced_line("NOTICE dave :left behind\r\n", "standard", owner="dave")
        from tests import support
        support.reset_config()
        self.assertEqual(recorded(), [])


if __name__ == "__main__":
    unittest.main()
