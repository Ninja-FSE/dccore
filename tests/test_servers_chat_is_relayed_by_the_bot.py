"""#371: DCCore Chat, relayed by the bot (serverschat.py).

The bot reads a channel message whose first word is [ServersChat], from a
nick whose realname says it is another DCCore bot (found with WHO), in one of
its channels and hands it to the operator's console; `chat #chan text` and
`chat * text` say one, as a PRIVMSG - a channel NOTICE is what channel bots
kick for. What arrives is other people's text on the read loop, so it is held
to the capture rules: first word only, a known peer, the bot's channels only,
stripped, capped, limited per nick, bounded, in memory only, never sent
anywhere else and never answered.
"""

import io
import os
import re
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import serverschat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CHAN = "#examplechan"
T0 = 1_000_000.0


class FakeSession:
    def __init__(self, structured=True):
        self.authenticated = True
        self.structured = structured
        self.reads_peers = structured
        self.closed = False
        self.nick = "SomeOperator"
        self.client = "dccore.mrc"
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def send_status(self):
        self.sent.append("(status)")


class Case(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="OurBot")
        config.channel_users[CHAN] = {"ourbot", "someoperator"}
        self.session = FakeSession()
        real = adminchat.active_session
        adminchat.active_session = lambda: self.session
        self.addCleanup(setattr, adminchat, "active_session", real)
        self.debug = []
        real_debug = announce.send_debug
        announce.send_debug = lambda *a, **k: self.debug.append(a)
        self.addCleanup(setattr, announce, "send_debug", real_debug)
        # No daemon here: what would go out lands in config.send_queue.
        self.oserve = sys.modules.pop("oserve", None)
        self.addCleanup(lambda: self.oserve and sys.modules.__setitem__("oserve", self.oserve))

    def arrive(self, text, nick="OtherOperator", target=CHAN, now=T0, peer=True):
        if peer:
            runtime.chat_peers.setdefault(nick.lower(), {})[CHAN] = now
        return serverschat.capture(nick, target, text, now=now)

    def see_peer(self, nick, chan=CHAN, now=T0, real="DCCore/sc SomeBot"):
        """What the server says in answer to WHO: a 352 line."""
        return serverschat.note_who_reply(
            f":irc.example.net 352 OurBot {chan} ~ident host.example irc.example.net "
            f"{nick} H :0 {real}", now=now)

    def chat_lines(self):
        return [line for line in self.session.sent if line.startswith("DCCORE CHAT ")]

    def queued(self):
        return {key: list(lines) for key, lines in (config.send_queue or {}).items()}

    def spoken(self):
        """What went on the express lane: what an operator typed."""
        return list(config.vip_queue)


class WhatArrives(Case):
    def test_a_tagged_message_in_our_channel_reaches_the_console(self):
        line = self.arrive("[ServersChat] hello all")
        self.assertIsNotNone(line)
        self.assertEqual(len(self.chat_lines()), 1)
        fields = self.chat_lines()[0].split(" ", 5)
        self.assertEqual(fields[3:], [CHAN, "OtherOperator", "hello all"])
        self.assertTrue(fields[2].isdigit())

    def test_the_tag_has_to_be_the_first_word(self):
        self.assertIsNone(self.arrive("hello [ServersChat] all"))
        self.assertIsNone(self.arrive("[ServersChatX] hello"))
        self.assertIsNone(self.arrive("plain message"))
        self.assertEqual(self.chat_lines(), [])

    def test_only_from_a_nick_whose_realname_says_it_is_a_dccore_bot(self):
        self.assertIsNone(self.arrive("[ServersChat] hi", nick="Stranger", peer=False))
        self.assertEqual(self.chat_lines(), [])
        self.see_peer("Stranger")
        self.assertIsNotNone(self.arrive("[ServersChat] hi", nick="Stranger", peer=False))

    def test_a_channel_not_our_own_is_nothing(self):
        self.assertIsNone(self.arrive("[ServersChat] hi", target="#elsewhere"))

    def test_a_private_line_to_us_is_chat_too(self):
        """#371 follow-up: a tagged private message from a known peer is
        chat the same way a channel one is - see PrivateMessages below."""
        line = self.arrive("[ServersChat] hi", target="OurBot")
        self.assertIsNotNone(line)
        self.assertEqual(line["chan"], "@OtherOperator")

    def test_never_our_own_nick(self):
        self.assertIsNone(self.arrive("[ServersChat] echo", nick="OurBot"))

    def test_colours_and_control_codes_are_stripped(self):
        line = self.arrive("[ServersChat] \x0304red\x03 and \x02bold\x02\x07")
        self.assertEqual(line["text"], "red and bold")

    def test_the_text_is_capped(self):
        line = self.arrive("[ServersChat] " + "x" * 1000)
        self.assertEqual(len(line["text"]), serverschat.MAX_TEXT)

    def test_nothing_left_after_stripping_is_not_a_line(self):
        self.assertIsNone(self.arrive("[ServersChat] \x02\x02"))

    def test_it_never_goes_to_the_debug_channel(self):
        self.arrive("[ServersChat] private enough")
        self.assertEqual(self.debug, [])

    def test_a_plain_console_gets_it_as_words(self):
        self.session.structured = False
        self.arrive("[ServersChat] hi")
        self.assertEqual(self.session.sent, [f"[CHAT] {CHAN} <OtherOperator> hi"])

    def test_ids_only_go_up(self):
        first = self.arrive("[ServersChat] one", now=T0)
        second = self.arrive("[ServersChat] two", now=T0)
        third = self.arrive("[ServersChat] three", now=T0 - 5)
        self.assertLess(first["id"], second["id"])
        self.assertLess(second["id"], third["id"])


class TheReviewOf958(Case):
    """The four points from the review of #958."""

    def test_a_banned_nick_s_chat_is_not_relayed(self):
        config.banned_users["banneduser"] = 9_999_999_999
        for nick in ("banneduser", "otheroperator"):
            runtime.chat_peers[nick] = {CHAN: T0}
        irc._capture_chat_message("BannedUser", CHAN, "[ServersChat] let me in", "b@host.example")
        self.assertEqual(self.chat_lines(), [])
        irc._capture_chat_message("OtherOperator", CHAN, "[ServersChat] fine", "o@host.example")
        self.assertEqual(len(self.chat_lines()), 1)

    def test_an_ordinary_message_costs_no_ban_lookup(self):
        import security
        looked = []
        real = security.check_user_status
        security.check_user_status = lambda *a, **k: looked.append(a) or True
        self.addCleanup(setattr, security, "check_user_status", real)
        irc._capture_chat_message("OtherOperator", CHAN, "just a message", "o@host.example")
        self.assertEqual(looked, [])

    def test_everyone_together_is_capped_and_said_once(self):
        for i in range(serverschat.INBOUND_ALL_MAX):
            self.assertIsNotNone(self.arrive(f"[ServersChat] {i}", nick=f"Operator{i}", now=T0))
        for i in range(20):
            self.assertIsNone(self.arrive("[ServersChat] one more", nick=f"Late{i}", now=T0 + 1))
        remarks = [line for line in self.chat_lines() if " * " in line]
        self.assertEqual(len(remarks), 1)
        self.assertIn("from everyone together", remarks[0])
        self.assertIsNotNone(self.arrive("[ServersChat] later", nick="Later", now=T0 + serverschat.INBOUND_PER + 1))

    def test_many_nicks_in_one_window_cannot_grow_the_table(self):
        real = serverschat.INBOUND_ALL_MAX
        serverschat.INBOUND_ALL_MAX = 10 ** 6
        self.addCleanup(setattr, serverschat, "INBOUND_ALL_MAX", real)
        for i in range(serverschat._TRACK_MAX * 5):
            self.arrive(f"[ServersChat] {i}", nick=f"Operator{i}", now=T0)
        self.assertLessEqual(len(runtime.chat_rate), serverschat._TRACK_MAX + 2)

    def test_bidi_controls_are_stripped_both_ways(self):
        line = self.arrive("[ServersChat] abc\u202edef\u2066g\u200f")
        self.assertEqual(line["text"], "abcdefg")
        serverschat.say("SomeOperator", CHAN, "x\u202ey", now=T0)
        self.assertNotIn("\u202e", self.spoken()[0])

    def test_a_changed_channel_list_is_sent_again_with_the_status(self):
        session = adminchat.Session.__new__(adminchat.Session)
        session.sent = []
        session.send = session.sent.append
        session._chat_channels = serverschat.channels_line()
        session._send_chat_channels_if_changed()
        self.assertEqual(session.sent, [], "unchanged: nothing")
        config.channel_users["#another"] = {"ourbot"}
        session._send_chat_channels_if_changed()
        self.assertEqual(session.sent, [f"DCCORE CHANNELS #another {CHAN}"])

    def test_only_a_session_that_had_the_channels_gets_updates(self):
        session = adminchat.Session.__new__(adminchat.Session)
        session.sent = []
        session.send = session.sent.append
        session._chat_channels = None
        session._send_chat_channels_if_changed()
        self.assertEqual(session.sent, [])

    def test_the_status_burst_asks_for_it(self):
        with io.open(os.path.join(REPO_ROOT, "src", "adminchat.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index("    def send_status(self):")
        body = code[at:code.index("    def request_status", at)]
        self.assertIn("self._send_chat_channels_if_changed()", body)


class TheFollowUpTo958(Case):
    def test_every_channel_line_counts_against_the_cap(self):
        """`chat *` to three channels is three lines: two such lines and the
        cap of six is used up."""
        real = serverschat.cover
        serverschat.cover = lambda now=None: ["#one", "#two", "#three"]
        self.addCleanup(setattr, serverschat, "cover", real)
        self.assertTrue(serverschat.say("SomeOperator", "*", "a", now=T0)[0])
        self.assertTrue(serverschat.say("SomeOperator", "*", "b", now=T0 + 1)[0])
        ok, message = serverschat.say("SomeOperator", "*", "c", now=T0 + 2)
        self.assertFalse(ok)
        self.assertIn("counting once for each", message)

    def test_one_fan_out_bigger_than_the_cap_is_refused_whole(self):
        real = (serverschat.cover, serverschat.OUTBOUND_MAX)
        serverschat.cover = lambda now=None: ["#one", "#two", "#three"]
        serverschat.OUTBOUND_MAX = 2
        self.addCleanup(lambda: (setattr(serverschat, "cover", real[0]),
                                 setattr(serverschat, "OUTBOUND_MAX", real[1])))
        self.assertFalse(serverschat.say("SomeOperator", "*", "too wide", now=T0)[0])
        self.assertEqual(self.queued(), {})
        self.assertEqual(list(config.vip_queue), [])

    def test_whois_status_is_bounded(self):
        config.whois_status.clear()
        real = irc.WHOIS_STATUS_MAX
        irc.WHOIS_STATUS_MAX = 3
        self.addCleanup(setattr, irc, "WHOIS_STATUS_MAX", real)
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index('if is_server_numeric(line, "352"):')
        self.assertIn("while len(config.whois_status) > WHOIS_STATUS_MAX:", code[at:at + 1200])
        self.assertIn("config.whois_status.pop(next(iter(config.whois_status)))", code[at:at + 1200])


class NothingAnswers(Case):
    def test_capture_sends_nothing(self):
        for i in range(10):
            self.arrive(f"[ServersChat] line {i}", nick=f"Operator{i}")
        self.assertEqual(self.queued(), {})
        self.assertEqual(self.spoken(), [])

    def test_the_capture_path_has_no_send_in_it(self):
        with io.open(os.path.join(REPO_ROOT, "src", "serverschat.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code[code.index("def capture("):code.index("def _enqueue(")]
        # Statements only: the docstring is prose, which is not a send (a guard that reads prose passes or fails on words).
        body = re.sub(r'"""[\s\S]*?"""', "", body)
        body = "\n".join(line for line in body.split("\n") if not line.strip().startswith("#"))
        for word in ("_enqueue(", "queue_message", "send_queue", '"NOTICE', "f\"NOTICE",
                     '"PRIVMSG', "f\"PRIVMSG", "session.sock"):
            self.assertNotIn(word, body)
        self.assertIn("_deliver(line)", body, "the body was really read")


class OneSenderCannotFloodIt(Case):
    def test_five_in_ten_seconds_then_hidden_for_sixty(self):
        for i in range(serverschat.INBOUND_MAX):
            self.assertIsNotNone(self.arrive(f"[ServersChat] {i}", now=T0 + i))
        self.assertIsNone(self.arrive("[ServersChat] too many", now=T0 + 5))
        notices = [line for line in self.chat_lines() if " * " in line]
        self.assertEqual(len(notices), 1, "said once")
        self.assertIsNone(self.arrive("[ServersChat] still", now=T0 + 30))
        self.assertEqual(len([line for line in self.chat_lines() if " * " in line]), 1)
        self.assertIsNotNone(self.arrive("[ServersChat] back", now=T0 + 5 + serverschat.INBOUND_HIDE + 1))

    def test_another_nick_is_not_held_back(self):
        for i in range(serverschat.INBOUND_MAX + 1):
            self.arrive(f"[ServersChat] {i}", now=T0)
        self.assertIsNotNone(self.arrive("[ServersChat] me", nick="ThirdOperator", now=T0))


class MemoryOnlyAndBounded(Case):
    def test_the_recent_lines_are_bounded(self):
        for i in range(serverschat.RECENT_MAX + 20):
            self.arrive(f"[ServersChat] {i}", nick=f"Operator{i}", now=T0 + i)
        self.assertEqual(len(runtime.chat_recent), serverschat.RECENT_MAX)
        self.assertEqual(runtime.chat_recent[-1]["text"], str(serverschat.RECENT_MAX + 19))

    def test_the_limit_table_is_bounded(self):
        for i in range(300):
            self.arrive(f"[ServersChat] {i}", nick=f"Operator{i}", now=T0)
        self.arrive("[ServersChat] later", nick="LateOperator", now=T0 + serverschat.INBOUND_PER + 1)
        self.assertLess(len(runtime.chat_rate), 50)

    def test_nothing_is_written_to_disk(self):
        with io.open(os.path.join(REPO_ROOT, "src", "serverschat.py"), encoding="utf-8") as handle:
            code = handle.read()
        for word in ("import db", "db.", "open(", "json"):
            self.assertNotIn(word, code)


class WhatIsSent(Case):
    def test_a_tagged_privmsg_through_the_paced_queue(self):
        ok, _message = serverschat.say("SomeOperator", CHAN, "hello there", now=T0)
        self.assertTrue(ok)
        self.assertEqual(self.spoken(), [f"PRIVMSG {CHAN} :[ServersChat] hello there\r\n"])
        self.assertEqual(self.queued(), {}, "the express lane, not behind every other channel")

    def test_the_own_line_comes_back_as_the_bot(self):
        serverschat.say("SomeOperator", CHAN, "hello there", now=T0)
        self.assertEqual(self.chat_lines()[0].split(" ", 5)[3:], [CHAN, "OurBot", "hello there"])

    def test_a_line_break_cannot_smuggle_a_second_command(self):
        serverschat.say("SomeOperator", CHAN, "hi\r\nQUIT :bye\x0304red", now=T0)
        line = self.spoken()[0]
        self.assertEqual(line.count("\r\n"), 1)
        self.assertTrue(line.endswith("\r\n"))
        self.assertNotIn("\x03", line)

    def test_the_line_fits_irc(self):
        serverschat.say("SomeOperator", CHAN, "é" * 500, now=T0)
        self.assertLessEqual(len(self.spoken()[0].encode("utf-8")), 512)

    def test_only_the_bot_s_channels(self):
        ok, message = serverschat.say("SomeOperator", "#elsewhere", "hi", now=T0)
        self.assertFalse(ok)
        self.assertIn("Not in #elsewhere", message)
        self.assertFalse(serverschat.say("SomeOperator", "nochannel", "hi", now=T0)[0])
        self.assertEqual(self.spoken(), [])

    def test_nothing_to_send(self):
        self.assertFalse(serverschat.say("SomeOperator", CHAN, " \x02 ", now=T0)[0])

    def test_a_cap_of_its_own(self):
        for i in range(serverschat.OUTBOUND_MAX):
            self.assertTrue(serverschat.say("SomeOperator", CHAN, str(i), now=T0 + i)[0])
        ok, message = serverschat.say("SomeOperator", CHAN, "one more", now=T0 + 10)
        self.assertFalse(ok)
        self.assertIn("Slow down", message)
        self.assertTrue(serverschat.say("SomeOperator", CHAN, "later", now=T0 + serverschat.OUTBOUND_PER + 1)[0])


class WhoIsADccoreBot(Case):
    def test_a_352_with_the_mark_first_in_the_realname_is_a_peer(self):
        self.assertEqual(self.see_peer("SomeBot"), "SomeBot")
        self.assertEqual(serverschat.peer_channels(T0), {CHAN: {"somebot"}})

    def test_the_mark_has_to_be_the_first_word(self):
        self.assertIsNone(self.see_peer("Faker", real="I am DCCore/sc"))
        self.assertIsNone(self.see_peer("Faker", real="DCCore/scx name"))
        self.assertIsNone(self.see_peer("Faker", real="just a name"))
        self.assertEqual(serverschat.peer_channels(T0), {})

    def test_not_ourselves_and_not_a_who_about_a_nick(self):
        self.assertIsNone(self.see_peer("OurBot"))
        self.assertIsNone(self.see_peer("SomeBot", chan="*"))

    def test_a_line_that_is_not_a_352_is_nothing(self):
        for line in ("", ":srv 353 OurBot = #c :a b", ":srv 352 short", "PING :x"):
            self.assertIsNone(serverschat.note_who_reply(line, now=T0))

    def test_only_channels_the_bot_is_in_count(self):
        self.see_peer("SomeBot", chan="#elsewhere")
        self.assertEqual(serverschat.peer_channels(T0), {})

    def test_a_sighting_goes_stale(self):
        self.see_peer("SomeBot")
        self.assertEqual(serverschat.peer_nicks(T0 + serverschat.PEER_FRESH - 1), ["somebot"])
        self.assertEqual(serverschat.peer_nicks(T0 + serverschat.PEER_FRESH + 1), [])

    def test_leaving_a_channel_or_the_network(self):
        self.see_peer("SomeBot")
        serverschat.note_gone("SomeBot", CHAN)
        self.assertEqual(serverschat.peer_nicks(T0), [])
        self.see_peer("SomeBot")
        serverschat.note_gone("somebot")
        self.assertEqual(serverschat.peer_nicks(T0), [])

    def test_the_table_is_bounded(self):
        for i in range(serverschat.PEER_MAX + 20):
            self.see_peer(f"Bot{i}")
        self.assertEqual(len(runtime.chat_peers), serverschat.PEER_MAX)

    def test_the_real_line_from_the_read_loop(self):
        irc._note_chat_peers(f":irc.example.net 352 OurBot {CHAN} ~i h s Other H :0 DCCore/sc Other")
        self.assertIn("other", runtime.chat_peers)

    def test_the_registration_carries_the_mark(self):
        self.set_config(NICKNAME="jlnbln", ORIGINAL_NICK="jlnbln")
        ident, real = irc.registration_names()
        self.assertEqual(ident, "jlnbln")
        self.assertEqual(real, f"{serverschat.REALNAME_MARK} jlnbln")
        self.assertLessEqual(len(real), 50)


class AJoinAsksWhoForAStranger(Case):
    """A plain JOIN carries no realname (no extended-join capability), so a
    reconnecting DCCore bot would otherwise sit out of the sidebar for up to
    WHO_EVERY. note_join() asks WHO for just that one nick instead - unless
    it is already a known peer, so ordinary join/part churn from regulars
    does not turn into one extra WHO per join."""

    def test_a_stranger_joining_is_asked_who(self):
        serverschat.note_join("SomeBot", CHAN)
        self.assertEqual(self.queued(), {serverschat.JOIN_WHO_QUEUE: ["WHO SomeBot\r\n"]})

    def test_never_on_the_channels_own_queue(self):
        """Where the dashboard's @find for that channel waits: a WHO ahead of
        it there held the search past its window."""
        for n in range(3):
            serverschat.note_join(f"Stranger{n}", CHAN)
        self.assertNotIn(CHAN.lower(), self.queued())

    def test_the_same_nick_is_asked_once_a_round(self):
        """Joining and parting in a loop is one WHO, not one a join."""
        for _ in range(10):
            serverschat.note_join("Looper", CHAN)
        self.assertEqual(self.queued(), {serverschat.JOIN_WHO_QUEUE: ["WHO Looper\r\n"]})

    def test_a_crowd_of_strangers_is_capped(self):
        """A netjoin, or clones: past the cap the regular round finds them."""
        for n in range(serverschat.JOIN_WHO_MOST + 20):
            serverschat.note_join(f"Clone{n}", CHAN)
        self.assertEqual(len(self.queued()[serverschat.JOIN_WHO_QUEUE]), serverschat.JOIN_WHO_MOST)

    def test_the_cap_outlives_the_inbound_window_when_the_table_is_full(self):
        """The full-table pass drops windows older than INBOUND_PER (10 s); the
        cap's runs JOIN_WHO_PER (60 s) and must not go with them."""
        for n in range(serverschat.JOIN_WHO_MOST):
            serverschat.note_join(f"Clone{n}", CHAN)
        runtime.chat_rate[serverschat._JOIN_WHO_ALL][0] -= serverschat.INBOUND_PER + 5
        for n in range(serverschat._TRACK_MAX + 10):
            runtime.chat_rate[f"filler{n}"] = [0.0, 1]
        serverschat.note_join("OneMore", CHAN)
        self.assertNotIn("WHO OneMore\r\n", self.queued()[serverschat.JOIN_WHO_QUEUE])

    def test_the_cap_is_not_forgotten_when_the_table_is_full(self):
        """Pruning a full table drops the oldest windows; the cap's own is kept,
        as the all-senders one is, or clones could reset it by filling it."""
        for n in range(serverschat.JOIN_WHO_MOST):
            serverschat.note_join(f"Clone{n}", CHAN)
        # Live windows, newer than the cap's own: the oldest one left is the cap.
        for n in range(serverschat._TRACK_MAX + 10):
            runtime.chat_rate[f"filler{n}"] = [time.time() + 1, 1]
        serverschat.note_join("OneMore", CHAN)
        self.assertNotIn("WHO OneMore\r\n", self.queued()[serverschat.JOIN_WHO_QUEUE])

    def test_an_already_known_peer_is_left_alone(self):
        self.see_peer("SomeBot")
        serverschat.note_join("SomeBot", CHAN)
        self.assertEqual(self.queued(), {})

    def test_the_who_reply_it_provoked_still_makes_the_bot_a_peer(self):
        serverschat.note_join("SomeBot", CHAN)
        self.see_peer("SomeBot")
        self.assertEqual(serverschat.peer_channels(T0), {CHAN: {"somebot"}})


class ThereIsNothingToSayItToUnlessAPeerIsThere(Case):
    def test_the_fewest_channels_that_reach_everybody(self):
        for chan in ("#one", "#two", "#three"):
            config.channel_users[chan] = {"ourbot"}
        self.see_peer("A", "#one")
        self.see_peer("A", "#two")
        self.see_peer("B", "#two")
        self.see_peer("C", "#three")
        self.assertEqual(serverschat.cover(T0), ["#two", "#three"])

    def test_at_most_a_few_channels(self):
        for i in range(serverschat.SEND_CHANNELS_MAX + 4):
            config.channel_users[f"#c{i}"] = {"ourbot"}
            self.see_peer(f"Bot{i}", f"#c{i}")
        self.assertEqual(len(serverschat.cover(T0)), serverschat.SEND_CHANNELS_MAX)

    def test_star_says_it_once_in_each_covering_channel(self):
        config.channel_users["#two"] = {"ourbot"}
        self.see_peer("A", CHAN)
        self.see_peer("B", "#two")
        ok, message = serverschat.say("SomeOperator", "*", "hello all", now=T0)
        self.assertTrue(ok, message)
        self.assertEqual(sorted(self.spoken()), [
            f"PRIVMSG {CHAN} :[ServersChat] hello all\r\n",
            "PRIVMSG #two :[ServersChat] hello all\r\n"])
        self.assertEqual(len(self.chat_lines()), 1, "one line comes back to the window, not two")

    def test_star_with_nobody_seen_says_nothing(self):
        ok, message = serverschat.say("SomeOperator", "*", "hello", now=T0)
        self.assertFalse(ok)
        self.assertIn("nobody to say it to", message)
        self.assertEqual(self.spoken(), [])

    def test_a_named_channel_needs_no_peer(self):
        self.assertTrue(serverschat.say("SomeOperator", CHAN, "hello", now=T0)[0])


class ChatDoesNotWaitBehindWho(Case):
    def test_who_is_standard_and_what_was_typed_is_express(self):
        config.channel_users["#two"] = {"ourbot"}
        serverschat.refresh_peers(now=T0)
        serverschat.say("SomeOperator", CHAN, "now", now=T0)
        self.assertEqual(len(self.queued()), 2, "WHO stays on the standard lane")
        self.assertEqual(self.spoken(), [f"PRIVMSG {CHAN} :[ServersChat] now\r\n"])

    def test_the_real_queue_takes_it_as_a_vip_line(self):
        import oserve
        sys.modules["oserve"] = oserve
        serverschat.say("SomeOperator", CHAN, "via oserve", now=T0)
        self.assertEqual(self.spoken(), [f"PRIVMSG {CHAN} :[ServersChat] via oserve\r\n"])


class AskingWho(Case):
    def test_every_channel_once_per_interval(self):
        config.channel_users["#two"] = {"ourbot"}
        self.assertEqual(serverschat.refresh_peers(now=T0), 2)
        self.assertEqual(self.queued(), {CHAN: [f"WHO {CHAN}\r\n"], "#two": ["WHO #two\r\n"]})
        self.assertEqual(serverschat.refresh_peers(now=T0 + serverschat.WHO_EVERY - 1), 0)
        self.assertEqual(serverschat.refresh_peers(now=T0 + serverschat.WHO_EVERY + 1), 2)

    def test_the_console_can_ask_at_once_and_list_who_answered(self):
        serverschat.refresh_peers(now=T0)
        self.session.sent = []
        adminchat._cmd_chat(self.session, "who")
        self.assertTrue(any("Asked WHO in 1 channel" in s for s in self.session.sent))
        self.see_peer("SomeBot", now=time.time())
        self.session.sent = []
        adminchat._cmd_chat(self.session, "peers")
        self.assertIn("somebot", self.session.sent[0].lower())

    def test_it_runs_from_the_servers_ping(self):
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index('if line.startswith("PING"):')
        self.assertIn("_refresh_chat_peers()", code[at:at + 900])

    def test_it_also_runs_from_the_bots_own_keepalive(self):
        """#982 audit finding 2: ircu pings a client only after ~90s of
        silence FROM it, and the keepalive below fires at 45s - so the
        server always hears from the bot first and never sends its own
        PING. Without this, refresh_peers() never ran on its own at all."""
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index('s.sendall(b"PING :lagcheck\\r\\n")')
        self.assertIn("_refresh_chat_peers()", code[at:at + 650])

    def test_it_also_runs_the_moment_every_channel_is_joined(self):
        """NAMES (353/366), which is all a JOIN itself ever gets, carries
        nicknames only - never a realname - so this is the earliest a WHO
        round can find a peer at all. Without it the first one waited for
        the keepalive or the advert thread, both minutes away."""
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index("if target_channels.issubset(channels_confirmed):")
        self.assertIn("_refresh_chat_peers()", code[at:at + 1050])


class WhoRoundReconciliation(Case):
    """The server's own "End of /WHO list" (315) is the one point a peer
    that quietly vanished - a QUIT or PART the read loop missed, a net
    split - is ever caught: note_who_reply() only ever ADDS a sighting."""

    def test_a_round_starts_empty_for_every_channel_asked(self):
        serverschat.refresh_peers(now=T0)
        self.assertEqual(runtime.chat_who_round, {CHAN: set()})

    def test_a_reconfirmed_peer_is_recorded_into_the_open_round(self):
        serverschat.refresh_peers(now=T0)
        self.see_peer("SomeBot", now=T0)
        self.assertEqual(runtime.chat_who_round[CHAN], {"somebot"})

    def test_a_peer_not_reconfirmed_is_removed_when_the_round_ends(self):
        self.see_peer("SomeBot", CHAN, now=T0)
        serverschat.refresh_peers(now=T0, force=True)
        # SomeBot is not seen again this round - it quietly vanished.
        self.session.sent = []
        serverschat.finish_who_round(CHAN, now=T0)
        self.assertEqual(serverschat.peer_nicks(T0), [])
        self.assertEqual(self.session.sent, [f"DCCORE PEERS {CHAN}"])

    def test_a_reconfirmed_peer_survives_the_round_ending(self):
        serverschat.refresh_peers(now=T0, force=True)
        self.see_peer("SomeBot", now=T0)
        serverschat.finish_who_round(CHAN, now=T0)
        self.assertEqual(serverschat.peer_nicks(T0), ["somebot"])

    def test_a_peer_in_a_different_channel_is_untouched(self):
        config.channel_users["#two"] = {"ourbot"}
        self.see_peer("SomeBot", CHAN, now=T0)
        self.see_peer("OtherBot", "#two", now=T0)
        serverschat.refresh_peers(now=T0, force=True)
        self.see_peer("OtherBot", "#two", now=T0)
        # SomeBot is not reconfirmed for CHAN; #two's own round is untouched.
        serverschat.finish_who_round(CHAN, now=T0)
        self.assertEqual(serverschat.peer_channels(T0), {"#two": {"otherbot"}})

    def test_no_round_open_is_a_silent_no_op(self):
        """A WHO the operator's own client happened to ask for outside
        refresh_peers() must not read as a round this owns."""
        self.see_peer("SomeBot", CHAN, now=T0)
        self.session.sent = []
        serverschat.finish_who_round(CHAN, now=T0)
        self.assertEqual(serverschat.peer_nicks(T0), ["somebot"])
        self.assertEqual(self.session.sent, [])

    def test_the_real_line_from_the_read_loop(self):
        irc._finish_chat_who_round(f":irc.example.net 315 OurBot {CHAN} :End of /WHO list.")
        # Ran without raising is the wiring test; behaviour is covered above.


class TheConsoleCommand(Case):
    def test_chat_alone_lists_the_channels(self):
        adminchat._cmd_chat(self.session, "")
        self.assertEqual(self.session.sent, [f"DCCORE CHANNELS {CHAN}"])
        self.session.structured = False
        self.session.sent = []
        adminchat._cmd_chat(self.session, "")
        self.assertIn(CHAN, self.session.sent[0])
        self.assertIn("public", self.session.sent[0])

    def test_chat_sends_and_a_window_just_sees_its_line(self):
        adminchat._cmd_chat(self.session, f"{CHAN} hello")
        self.assertEqual(len(self.chat_lines()), 1)
        self.assertEqual([s for s in self.session.sent if not s.startswith("DCCORE CHAT")], [])

    def test_a_person_at_a_plain_console_is_told(self):
        self.session.structured = False
        adminchat._cmd_chat(self.session, f"{CHAN} hello")
        self.assertIn(f"Sent to {CHAN}.", self.session.sent)

    def test_a_refusal_is_said(self):
        adminchat._cmd_chat(self.session, "#elsewhere hello")
        self.assertTrue(any("Not in #elsewhere" in s for s in self.session.sent))

    def test_it_is_a_console_command(self):
        self.assertIn("chat", adminchat.COMMANDS)

    def test_hello_brings_the_channels_and_what_was_missed(self):
        self.arrive("[ServersChat] while you were away", now=T0)
        self.session.sent = []
        adminchat._cmd_hello(self.session, "dccore.mrc 1.6")
        self.assertIn(f"DCCORE CHANNELS {CHAN}", self.session.sent)
        replay = [s for s in self.session.sent if s.startswith("DCCORE CHAT ")]
        self.assertEqual(len(replay), 1)
        self.assertTrue(replay[0].endswith("while you were away"))


class TheReadLoop(Case):
    def test_the_privmsg_branch_hands_it_on(self):
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index("privmsg_parsed = parse_privmsg(line)")
        self.assertIn("_capture_chat_message(user, target_chan, msg, user_host)", code[at:at + 3500])

    def test_the_who_reply_and_the_departures_are_watched(self):
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn("_note_chat_peers(line)", code)
        self.assertIn("_forget_chat_peer(p_user, p_chan)", code)
        self.assertIn("_forget_chat_peer(q_user)", code)

    def test_a_join_is_watched_too(self):
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn("_note_chat_join(joined_user, joined_chan)", code)

    def test_a_notice_is_not_chat(self):
        with io.open(os.path.join(REPO_ROOT, "src", "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index("notice_parsed = parse_notice(line)")
        self.assertNotIn("_capture_chat", code[at:at + 1500])

    def test_a_capture_that_raises_cannot_break_the_connection(self):
        real = serverschat.capture

        def explode(*_a, **_k):
            raise RuntimeError("bad line")

        serverschat.capture = explode
        self.addCleanup(setattr, serverschat, "capture", real)
        irc._capture_chat_message("OtherOperator", CHAN, "[ServersChat] x")

    def test_the_real_capture_runs_from_it(self):
        runtime.chat_peers["otheroperator"] = {CHAN: T0}
        irc._capture_chat_message("OtherOperator", CHAN, "[ServersChat] via irc")
        self.assertEqual(len(self.chat_lines()), 1)


if __name__ == "__main__":
    unittest.main()
