"""With both configured nicks held by ghosts, the bot went quiet until the
server closed the link (audit M31, #633).

The 433 handler only reacted while the bot was still asking for its main
nick: a 433 for the ALTERNATE did nothing, and 437 (ERR_UNAVAILRESOURCE, the
nick delay Hybrid, ratbox and Solanum apply after a split or a kill) was not
matched anywhere. So on a split storm - the ghost holds NICKNAME, the bot
goes to ALT_NICKNAME, the link drops again, now ghosts hold both - every
reconnect was NICK main, 433, NICK alt, 433, silence, the server's
registration timeout, ten seconds, and the same again for as long as the
ghosts lived. On an EFnet-style server the same happened with 437 for the
whole nick-delay window even when the alternate was free.

Now every refusal during registration moves on to the next name: the
alternate first, then the alternate with a digit. Once registered the ladder
is not climbed - a refused NICK means the bot keeps the name it has (#635) -
because a server that answers 433 to a NICK for the name we already hold
must not walk us down the ladder.

The registration itself is driven for real below: irc.irc_loop() against a
scripted socket, no network, stopped on the reconnect path.
"""

import contextlib
import io
import os
import re
import socket
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


def server(numeric, refused, target="*"):
    return ":irc.example.net %s %s %s :refused" % (numeric, target, refused)


class WhatCountsAsARefusedNick(unittest.TestCase):

    def test_433_before_registration(self):
        """The server addresses an unregistered client as `*`; the refused
        name is the parameter after that."""
        self.assertEqual(irc.parse_nick_refusal(
            ":irc.example.net 433 * SomeBot :Nickname is already in use"), "SomeBot")

    def test_433_after_registration(self):
        self.assertEqual(irc.parse_nick_refusal(
            ":irc.example.net 433 SomeBot_ SomeBot :Nickname is already in use"), "SomeBot")

    def test_432_and_437(self):
        for numeric in ("432", "437"):
            with self.subTest(numeric=numeric):
                self.assertEqual(irc.parse_nick_refusal(server(numeric, "SomeBot")), "SomeBot")

    def test_437_for_a_channel_is_not_a_nick_refusal(self):
        """The same numeric says a CHANNEL is temporarily unavailable, with
        the channel where the nick would be. Not ours to act on here."""
        for channel in ("#somechannel", "&local", "+modeless", "!XYZ12safe"):
            with self.subTest(channel=channel):
                self.assertIsNone(irc.parse_nick_refusal(server("437", channel, "SomeBot")))

    def test_a_forged_line_in_a_channel_is_not_one(self):
        self.assertIsNone(irc.parse_nick_refusal(
            ":someone!user@host PRIVMSG #somechannel ::x 433 * SomeBot :got you"))

    def test_other_numerics_are_not(self):
        for numeric in ("001", "431", "436", "474"):
            with self.subTest(numeric=numeric):
                self.assertIsNone(irc.parse_nick_refusal(server(numeric, "SomeBot")))


class TheLadderOfNames(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", ALT_NICKNAME="SomeBot_")

    def test_the_alternate_comes_first_as_always(self):
        self.assertEqual(irc.fallback_nick("SomeBot", 1), "SomeBot_")

    def test_then_the_alternate_with_a_digit(self):
        self.assertEqual([irc.fallback_nick("SomeBot", n) for n in range(2, 11)],
                         ["SomeBot_%d" % d for d in range(1, 10)])

    def test_and_then_nothing_more_this_connection(self):
        self.assertIsNone(irc.fallback_nick("SomeBot", 11))
        self.assertIsNone(irc.fallback_nick("SomeBot", 50))

    def test_a_long_alternate_keeps_its_length(self):
        """Appending would push past a NICKLEN the server may well have;
        the alternate's own length is one it accepted, so the last character
        gives way instead."""
        self.set_config(ALT_NICKNAME="SomeLongBot_")

        self.assertEqual(irc.fallback_nick("SomeLongBot", 2), "SomeLongBot1")
        self.assertEqual(len(irc.fallback_nick("SomeLongBot", 9)), len("SomeLongBot_"))

    def test_never_a_name_that_was_already_refused(self):
        """Main "Bot1", alternate "Bot": the first digit would be the main
        nick again, so it is skipped."""
        self.set_config(NICKNAME="Bot1", ALT_NICKNAME="Bot")

        self.assertEqual(irc.fallback_nick("Bot1", 2), "Bot2")

    def test_an_empty_alternate_still_gets_a_ladder(self):
        """resolve_alt_nick()'s own fallback - the alternate can be saved
        empty by old dashboards."""
        self.set_config(ALT_NICKNAME="")

        self.assertEqual(irc.fallback_nick("SomeBot", 1), "SomeBot`")
        self.assertEqual(irc.fallback_nick("SomeBot", 2), "SomeBot`1")


class _StopTheLoop(BaseException):
    """Raised from the reconnect path's sleep - BaseException so nothing in
    irc_loop()'s own handlers swallows it."""


class _ScriptedSocket:
    """Hands the loop the scripted server lines, one chunk per recv(), and
    breaks the link when they run out. Remembers every line sent."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.lock = threading.Lock()

    def settimeout(self, *_a): pass
    def setsockopt(self, *_a): pass
    def connect(self, *_a): pass
    def close(self): pass

    def sendall(self, payload):
        with self.lock:
            self.sent.append(payload.decode("utf-8", "replace"))

    def send(self, payload):
        self.sendall(payload)
        return len(payload)

    def recv(self, _n):
        if self.chunks:
            return self.chunks.pop(0)
        raise socket.error("scripted end of link")

    def lines_sent(self):
        with self.lock:
            return [line.strip() for line in self.sent]


class DrivesOneRegistration(DCCoreTestCase):
    """One real registration attempt of irc.irc_loop() against a scripted
    server, stopped on the reconnect path. No tests of its own - the classes
    below (and test_nick_and_user_go_out_back_to_back.py) run it."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", ALT_NICKNAME="SomeBot_",
                        SERVER="irc.example.invalid", PORT=6667,
                        CHANNEL="#somechannel", DEBUG_CHANNEL="",
                        MY_IP_OR_DOCK="203.0.113.5")
        config.ORIGINAL_NICK = "SomeBot"
        self.addCleanup(setattr, config, "ORIGINAL_NICK", "DCCore")
        silence_debug(announce)
        self._real_socket = irc.socket.socket
        self._real_sleep = irc.time.sleep
        self.addCleanup(setattr, irc.socket, "socket", self._real_socket)
        self.addCleanup(setattr, irc.time, "sleep", self._real_sleep)

    def run_registration(self, *server_lines):
        """Every line the bot sent, one server line per recv() chunk. A bytes
        entry is handed over as one chunk verbatim - for a chunk holding two
        lines, or half of one."""
        chunks = [line if isinstance(line, bytes) else (line + "\r\n").encode("utf-8")
                  for line in server_lines]
        sock = _ScriptedSocket(chunks)
        irc.socket.socket = lambda *a, **k: sock

        def sleep_stops_the_loop(_seconds):
            raise _StopTheLoop()
        irc.time.sleep = sleep_stops_the_loop

        outcome = {}

        def run():
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    irc.irc_loop()
                except _StopTheLoop:
                    outcome["stopped"] = True
                except BaseException as err:  # noqa: BLE001 - reported below
                    outcome["error"] = repr(err)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "irc_loop() did not reach the reconnect path")
        self.assertEqual(outcome, {"stopped": True})
        return sock.lines_sent()

    def nicks_asked_for(self, *server_lines):
        return [line for line in self.run_registration(*server_lines) if line.startswith("NICK ")]


class RegistrationWalksTheLadder(DrivesOneRegistration):
    """A scripted server that refuses every name."""

    def test_both_names_taken_a_third_is_asked_for(self):
        """The audit's own trace, inverted: main refused, alternate refused,
        and then - instead of silence - the next name."""
        nicks = self.nicks_asked_for(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            server("433", "SomeBot"),
            server("433", "SomeBot_"),
            server("433", "SomeBot_1"))

        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_", "NICK SomeBot_1", "NICK SomeBot_2"])

    def test_a_nick_delay_is_a_refusal_too(self):
        nicks = self.nicks_asked_for(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            server("437", "SomeBot"))

        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_"])

    def test_a_refusal_before_the_servers_first_notice_counts_as_well(self):
        """Some servers answer the NICK before they say anything else. One
        count, wherever the refusal lands relative to the server's first
        NOTICE (there used to be a separate handshake reader before it -
        #634 - and this proved the two shared the count)."""
        nicks = self.nicks_asked_for(
            server("433", "SomeBot"),
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            server("433", "SomeBot_"))

        self.assertEqual(nicks, ["NICK SomeBot", "NICK SomeBot_", "NICK SomeBot_1"])

    def test_the_ladder_has_an_end(self):
        refusals = [server("433", "SomeBot"), server("433", "SomeBot_")]
        refusals += [server("433", "SomeBot_%d" % d) for d in range(1, 10)]
        nicks = self.nicks_asked_for(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname", *refusals)

        self.assertEqual(len(nicks), 11, nicks)
        self.assertEqual(nicks[-1], "NICK SomeBot_9")

    def test_a_437_for_a_channel_asks_for_nothing(self):
        nicks = self.nicks_asked_for(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            ":irc.example.net 437 SomeBot #somechannel :Nick/channel is temporarily unavailable")

        self.assertEqual(nicks, ["NICK SomeBot"])


class OnceRegisteredTheOldRuleStands(unittest.TestCase):
    """A registered bot that is refused a NICK keeps the name it has - the
    ladder is for registration only. (Until #635 the refused branch re-sent
    the alternate; it now sends nothing, since the server still calls the
    bot by the name it had.) The branch lives in irc_loop(), so this reads
    the text."""

    def handler(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        return code.split("refused_nick = parse_nick_refusal(line)", 1)[1][:3000]

    def test_the_ladder_is_taken_only_while_registering(self):
        block = self.handler()
        ladder = block.split("if not joined:", 1)[1].split("elif ", 1)[0]

        self.assertIn("fallback_nick(main_nick, nick_refusals)", ladder)

    def test_and_a_registered_bot_does_not_climb_it(self):
        block = self.handler()
        after_ladder = block.index("[NICK LADDER] The server refused")
        start = block.index(" " * 24 + "else:", after_ladder)
        # Up to the first line back at the handler's own indent (20 spaces).
        dedent = re.search(r"\n {20}\S", block[start:])
        registered = block[start:start + dedent.start()]
        self.assertIn("keeps {config.NICKNAME}", registered, "not the branch this expects")

        self.assertNotIn("fallback_nick(", registered)
        self.assertNotIn("sendall(", registered, "a refused NICK must not be answered with another NICK")


if __name__ == "__main__":
    unittest.main()
