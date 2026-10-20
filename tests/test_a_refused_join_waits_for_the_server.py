"""#1415: a JOIN refused with "target change too fast" waits for the server.

ircu answers a JOIN with 439 when the client has no free target, and it
remembers how few targets a client had across a reconnect. A bot restarted
after hours of advertising in many channels came back with almost none: two
of its JOINs were refused, the advert worker asked again 25 s later - inside
the server's wait, so again refused - and the last channel got in on the
advert after that, about six minutes after the restart.

Checked here on the real functions, with the clock handed in:

  * a 439 for a channel the bot should be in and is not is a refused JOIN,
    remembered until the server's wait (plus a margin) is over; a 439 for a
    nick, or for a channel the bot is in, is not;
  * a second channel refused in the same burst waits for the first's turn
    and a wait of its own;
  * channels_to_rejoin() leaves a channel out until its wait is over;
  * ask_again_after_join_waits() asks for it as soon as the wait is over,
    once - never for a channel the bot was kicked from, which keeps the
    advert's rhythm, nor one it is already in;
  * the rest of a JOIN burst waits behind a refusal instead of being sent;
  * a new connection forgets the last one's waits;
  * the 439 handler itself feeds all this.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402

NOW = 1_000_000.0


def refused(target, seconds=105):
    return f":irc.example.net 439 SomeBot {target} :Target change too fast. Please wait {seconds} seconds."


class JoinWaitCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#alpha,#beta,#gamma", DEBUG_CHANNEL="#thedebug", NICKNAME="SomeBot")
        config.kicked_channels.clear()
        with runtime.channel_users_lock():
            config.channel_users.clear()
            config.channel_users["#alpha"] = {"somebot"}
        self.out = io.StringIO()
        self._quiet = contextlib.redirect_stdout(self.out)
        self._quiet.__enter__()
        self.addCleanup(self._quiet.__exit__, None, None, None)

    def note(self, channel, seconds=105, now=NOW):
        return irc.note_join_target_wait(channel, seconds, now=now)


class WhatIsARefusedJoin(JoinWaitCase):

    def test_a_channel_we_should_be_in_and_are_not(self):
        self.assertEqual(self.note("#beta"), NOW + 105 + irc.JOIN_WAIT_MARGIN)
        self.assertIn("#beta", runtime.join_waits)

    def test_the_case_of_the_name_does_not_matter(self):
        self.note("#BETA")
        self.assertIn("#beta", runtime.join_waits)

    def test_not_a_channel_the_bot_is_in(self):
        """A message to it was what the server refused; only the pacer holds."""
        self.assertIsNone(self.note("#alpha"))
        self.assertEqual(runtime.join_waits, {})

    def test_not_a_nick_nor_a_channel_nobody_configured(self):
        self.assertIsNone(self.note("somenick"))
        self.assertIsNone(self.note("#elsewhere"))
        self.assertEqual(runtime.join_waits, {})

    def test_a_second_refusal_waits_for_the_first_ones_turn(self):
        first = self.note("#beta", 105)
        second = self.note("#gamma", 107, now=NOW + 0.5)
        self.assertEqual(second, first + 107 + irc.JOIN_WAIT_MARGIN)

    def test_refused_again_it_waits_from_now(self):
        self.note("#beta", 105)
        again = self.note("#beta", 110, now=NOW + 200)
        self.assertEqual(again, NOW + 200 + 110 + irc.JOIN_WAIT_MARGIN)


class TheAdvertsRejoinWaitsToo(JoinWaitCase):

    def setUp(self):
        super().setUp()
        irc.note_join_unconfirmed("#beta")

    def test_left_out_until_the_wait_is_over(self):
        due = self.note("#beta")
        self.assertNotIn("#beta", irc.channels_to_rejoin(now=due - 1))
        self.assertIn("#beta", irc.channels_to_rejoin(now=due))

    def test_with_no_wait_it_is_asked_for_as_before(self):
        self.assertIn("#beta", irc.channels_to_rejoin(now=NOW))


class AskedForAgainWhenTheWaitIsOver(JoinWaitCase):

    def ask(self, now):
        sent = []
        asked = irc.ask_again_after_join_waits(now=now, send=sent.append)
        return asked, sent

    def test_not_before_the_wait_is_over(self):
        due = self.note("#beta")
        self.assertEqual(self.ask(due - 1), ([], []))

    def test_not_at_the_very_second_the_server_named(self):
        """A margin past the server's own figure: its clock and ours are not
        the same, and asked a moment early the wait starts over."""
        self.note("#beta", 105)
        self.assertEqual(self.ask(NOW + 105), ([], []))
        self.assertEqual(self.ask(NOW + 105 + 1), ([], []))

    def test_once_it_is_over_one_join_once(self):
        irc.note_join_unconfirmed("#beta")
        due = self.note("#beta")
        self.assertEqual(self.ask(due), (["#beta"], ["JOIN #beta\r\n"]))
        self.assertEqual(self.ask(due + 60), ([], []))

    def test_in_the_order_their_turns_come(self):
        first = self.note("#beta", 105)
        second = self.note("#gamma", 107)
        self.assertEqual(self.ask(first)[0], ["#beta"])
        self.assertEqual(self.ask(second)[0], ["#gamma"])

    def test_never_for_a_channel_we_were_kicked_from(self):
        """A kick keeps the advert's slower rhythm: an instant rejoin reads as a fight."""
        due = self.note("#beta")
        with runtime.kicked_channels_lock:
            config.kicked_channels["#beta"] = {"refusals": 0, "kicked_at": NOW, "by": "SomeOp", "reason": "kicked"}
        self.assertEqual(self.ask(due), ([], []))

    def test_never_for_a_channel_we_are_in_by_then(self):
        due = self.note("#beta")
        with runtime.channel_users_lock():
            config.channel_users["#beta"] = {"somebot"}
        self.assertEqual(self.ask(due), ([], []))

    def test_a_confirmed_join_clears_the_wait(self):
        self.note("#beta")
        irc.note_joined("#beta")
        self.assertEqual(runtime.join_waits, {})


class TheRestOfTheBurstWaits(JoinWaitCase):

    def test_nothing_waits_when_nothing_was_refused(self):
        self.assertEqual(irc.defer_joins_after_a_refusal(["#gamma", "#thedebug"], now=NOW), [])
        self.assertEqual(runtime.join_waits, {})

    def test_after_a_refusal_the_rest_queue_behind_it(self):
        first = self.note("#beta", 105)
        deferred = irc.defer_joins_after_a_refusal(["#gamma", "#thedebug"], now=NOW + 2)
        self.assertEqual(deferred, ["#gamma", "#thedebug"])
        self.assertEqual(runtime.join_waits["#gamma"], first + 105 + irc.JOIN_WAIT_MARGIN)
        self.assertGreater(runtime.join_waits["#thedebug"], runtime.join_waits["#gamma"])

    def test_a_new_connection_forgets_the_waits(self):
        self.note("#beta")
        irc.forget_join_waits()
        self.assertEqual(runtime.join_waits, {})
        self.assertIsNone(runtime.join_wait_seconds)


class TheJoinBurstStopsAtARefusal(JoinWaitCase):
    """delayed_join() for real: its sleeps and the two threads it starts are
    stood in for, and the server's 439 for the first batch arrives while the
    burst waits JOIN_BATCH_GAP, as it does on the wire."""

    CHANNELS = ["#c1", "#c2", "#c3", "#c4", "#c5", "#c6"]

    def run_the_burst(self, refuse=None):
        import types
        import time as real_time
        import on_connect
        from unittest import mock

        self.set_config(CHANNEL=",".join(self.CHANNELS), DEBUG_CHANNEL="")
        with runtime.channel_users_lock():
            config.channel_users.clear()
        quick = types.SimpleNamespace(sleep=lambda seconds: None, time=real_time.time,
                                      strftime=real_time.strftime, localtime=real_time.localtime)

        class Server:
            def __init__(self):
                self.lines = []

            def sendall(self, payload):
                self.lines.append(payload.decode("utf-8"))
                if refuse and len(self.lines) == 1:
                    irc.note_join_target_wait(refuse, 105)

        server = Server()
        conn = irc.Connection(server, config.connection_epoch)
        with mock.patch.object(irc, "time", quick), \
                mock.patch.object(irc, "activation_watchdog", lambda conn: None), \
                mock.patch.object(on_connect, "load", lambda: ([], 0)), \
                mock.patch.object(on_connect, "watch", lambda *args: None):
            irc.delayed_join(conn, list(self.CHANNELS))
        return [line for line in server.lines if line.startswith("JOIN ")]

    def test_with_no_refusal_every_batch_goes(self):
        joins = self.run_the_burst()
        self.assertEqual(joins, ["JOIN #c1,#c2,#c3,#c4\r\n", "JOIN #c5,#c6\r\n"])
        self.assertEqual(runtime.join_waits, {})

    def test_after_a_refusal_the_rest_wait_their_turn(self):
        joins = self.run_the_burst(refuse="#c4")
        self.assertEqual(joins, ["JOIN #c1,#c2,#c3,#c4\r\n"])
        self.assertEqual(sorted(runtime.join_waits), ["#c4", "#c5", "#c6"])
        self.assertLess(runtime.join_waits["#c4"], runtime.join_waits["#c5"])
        self.assertLess(runtime.join_waits["#c5"], runtime.join_waits["#c6"])


class TheHandlerFeedsIt(JoinWaitCase):

    def test_a_439_for_a_join_is_remembered(self):
        held = []
        self.patch_pacer(held)
        self.assertEqual(irc._answer_target_change(refused("#beta", 105)), 105)
        self.assertIn("#beta", runtime.join_waits)
        self.assertEqual(held, [105])

    def test_a_439_for_a_message_to_a_nick_is_not(self):
        held = []
        self.patch_pacer(held)
        irc._answer_target_change(refused("somenick", 30))
        self.assertEqual(runtime.join_waits, {})

    def patch_pacer(self, held):
        original = runtime.outbound_pacer.hold_for
        runtime.outbound_pacer.hold_for = held.append
        self.addCleanup(setattr, runtime.outbound_pacer, "hold_for", original)


if __name__ == "__main__":
    unittest.main()
