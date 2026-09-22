"""#550, step 2: the feed as fields, over the admin DCC chat, for a client
that draws a window.

Prose is what a person reads; a script wants the nick, the size, the count.
Every feed emitter now calls announce.feed_event(kind, text, **fields),
which sends the prose through send_debug() exactly as before AND hands the
fields to event sinks. A console session that says `hello <client>
<version>` after logging in switches to structured mode and gets one line
per event:

    DCCORE <TYPE> <fixed fields...> <free text>

Space-separated positional tokens with the one free-text field last, so a
mIRC script reads it as $N-. A session that never says hello is byte-for-
byte the console it was before. An old bot answers hello with "Unknown
command", and the client stays in prose mode - the switch is opt-in for
exactly that reason.
"""

import contextlib
import io
import os
import socket
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_adminchat import (  # noqa: E402
    ADMIN_LINE, LOOPBACK_OK, NEEDS_LOOPBACK, PASSWORD, wait_for)


class TheLineFormat(DCCoreTestCase):
    """structured_line() - pure, and the whole contract with a client."""

    def test_request(self):
        self.assertEqual(adminchat.structured_line("REQUEST", {"nick": "dave", "channel": "#chan", "kind": "file", "name": "A B.flac"}),
                         "DCCORE REQUEST dave #chan file A B.flac")

    def test_queued(self):
        self.assertEqual(adminchat.structured_line("QUEUED", {"nick": "erin", "channel": "#chan", "pos": 2, "busy": 3, "slots": 3, "name": "X.rar"}),
                         "DCCORE QUEUED erin #chan 2 3 3 X.rar")

    def test_sending(self):
        self.assertEqual(adminchat.structured_line("SENDING", {"nick": "dave", "channel": "#chan", "slot": 2, "slots": 3, "bytes": 31200000, "name": "A.flac"}),
                         "DCCORE SENDING dave #chan 2 3 31200000 A.flac")

    def test_resumed(self):
        self.assertEqual(adminchat.structured_line("RESUMED", {"nick": "f", "channel": "#chan", "at_bytes": 10, "total_bytes": 20, "name": "S.mkv"}),
                         "DCCORE RESUMED f #chan 10 20 S.mkv")

    def test_sent(self):
        line = adminchat.structured_line("SENT", {"nick": "dave", "channel": "#chan", "bytes": 32712345, "seconds": 23.44, "bytes_per_s": 1398000.7, "name": "A.flac"})
        self.assertEqual(line, "DCCORE SENT dave #chan 32712345 23.4 1398000 A.flac")

    def test_fail_carries_the_reason_after_the_marker(self):
        line = adminchat.structured_line("FAIL", {"nick": "gary", "channel": "#chan", "acked": 1200000, "total": 2700000, "name": "T.flac", "reason": "stopped"})
        self.assertEqual(line, "DCCORE FAIL gary #chan 1200000 2700000 T.flac :: stopped")

    def test_search(self):
        self.assertEqual(adminchat.structured_line("SEARCH", {"nick": "dave", "channel": "#chan", "results": 12, "term": "iron maiden"}),
                         "DCCORE SEARCH dave #chan 12 iron maiden")

    def test_the_channel_is_one_token_and_dash_when_there_is_none(self):
        """It sits among the fixed fields, ahead of the free text, so it must
        always be exactly one token: "-" for a private-message request or a
        transfer that no longer knows where it was asked for, and for
        anything that is not a channel name (a nick is not a channel)."""
        for value in (None, "", "dave", "  "):
            with self.subTest(channel=value):
                self.assertEqual(adminchat.structured_line("SEARCH", {"nick": "d", "channel": value, "results": 1, "term": "x"}),
                                 "DCCORE SEARCH d - 1 x")
        self.assertEqual(adminchat.structured_line("SEARCH", {"nick": "d", "results": 1, "term": "x"}),
                         "DCCORE SEARCH d - 1 x")

    def test_every_channel_prefix_counts_and_odd_spellings_stay_one_token(self):
        for name in ("#a", "&b", "+c", "!d"):
            with self.subTest(channel=name):
                self.assertEqual(adminchat.structured_line("SEARCH", {"nick": "d", "channel": name, "results": 1, "term": "x"}),
                                 f"DCCORE SEARCH d {name} 1 x")
        line = adminchat.structured_line("SEARCH", {"nick": "d", "channel": "#a b\tc", "results": 1, "term": "x"})
        self.assertEqual(line.split(" ")[3], "#a_b_c")
        self.assertEqual(len(line.split(" ")), 6, "one token more than before, never more")

    def test_anything_else_is_a_log_line(self):
        """No category is lost by the typing."""
        self.assertEqual(adminchat.structured_line("LOG", {"category": "JOIN", "text": "helen returned"}),
                         "DCCORE LOG JOIN helen returned")
        self.assertEqual(adminchat.structured_line("WEIRD", {"text": "x"}), "DCCORE LOG WEIRD x")

    def test_the_free_text_is_always_last_and_may_hold_spaces(self):
        line = adminchat.structured_line("REQUEST", {"nick": "d", "channel": "#c", "kind": "file", "name": "Artist - Title (Live) [2020].flac"})
        self.assertTrue(line.endswith(" Artist - Title (Live) [2020].flac"))
        self.assertEqual(len(line.split(" ", 5)), 6)

    def test_numbers_are_raw_never_formatted(self):
        line = adminchat.structured_line("SENT", {"nick": "d", "bytes": 1288490188, "seconds": 0, "bytes_per_s": 0, "name": "x"})
        self.assertIn(" 1288490188 ", line)
        self.assertNotIn("GB", line)

    def test_missing_numbers_are_zero_not_none(self):
        self.assertEqual(adminchat.structured_line("SENT", {"nick": "d", "name": "x"}), "DCCORE SENT d - 0 0.0 0 x")

    def test_control_characters_become_spaces(self):
        """A tab, a newline or a colour code in a field would break the
        line or the client's display; reject_if_unsafe_for_irc_line()
        refuses them in requests, and this is the belt to that brace."""
        line = adminchat.structured_line("SEARCH", {"nick": "d", "results": 1, "term": "a\tb\r\nc\x0304d"})
        self.assertEqual(line, "DCCORE SEARCH d - 1 a b  c 04d")
        self.assertEqual(len(line.splitlines()), 1)

    def test_a_token_field_never_contains_a_space(self):
        line = adminchat.structured_line("REQUEST", {"nick": "two words", "kind": "a b", "name": "n"})
        self.assertEqual(line, "DCCORE REQUEST two_words - a_b n")

    def test_a_marker_inside_a_filename_cannot_split_a_fail_line(self):
        line = adminchat.structured_line("FAIL", {"nick": "g", "acked": 0, "total": 1, "name": "A :: B", "reason": "r"})
        self.assertEqual(line.count(" :: "), 1)

    def test_hello_names_the_protocol_the_bot_and_the_build(self):
        self.set_config(NICKNAME="MusicBot", SCRIPT_VERSION="DCCore v9.9")
        self.assertEqual(adminchat.hello_line(), "DCCORE HELLO 1.1 MusicBot DCCore v9.9")


class FeedEventTellsItTwice(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.prose, self.events = [], []
        self._real = announce.send_debug
        announce.send_debug = lambda text, category="INFO", notice=None: self.prose.append((category, text))
        self.addCleanup(setattr, announce, "send_debug", self._real)
        announce.add_event_sink(self.sink)
        self.addCleanup(announce.remove_event_sink, self.sink)
        self.set_config(CONSOLE_SHOW_SEARCHES=True)

    def sink(self, kind, fields, text):
        self.events.append((kind, fields, text))

    def test_prose_under_the_kind_and_fields_to_the_sink(self):
        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual(self.prose, [("SEARCH", "dave searched x")])
        self.assertEqual(self.events, [("SEARCH", {"nick": "dave", "results": 3, "term": "x"}, "dave searched x")])

    def test_a_field_may_be_called_kind(self):
        """REQUEST carries a field named `kind` ("file" / "folder"). The first
        feed_event() had a positional parameter of the same name, and the
        call was a TypeError - every REQUEST silently failed, which a
        path-security test noticed as "no dispatch". The positionals are
        underscored now; this pins it."""
        self.set_config(CONSOLE_SHOW_REQUESTS=True)
        announce.feed_event("REQUEST", "dave asked for the folder x", nick="dave", kind="folder", name="x")
        self.assertEqual(self.events[-1][1], {"nick": "dave", "kind": "folder", "name": "x"})

    def test_the_tickbox_gates_the_fields_too(self):
        self.set_config(CONSOLE_SHOW_SEARCHES=False)

        announce.feed_event("SEARCH", "dave searched x", nick="dave", results=3, term="x")

        self.assertEqual(self.events, [], "an unticked kind reached an event sink")

    def test_a_sink_that_raises_is_dropped_not_fatal(self):
        def bad(kind, fields, text):
            raise RuntimeError("boom")
        announce.add_event_sink(bad)
        self.addCleanup(announce.remove_event_sink, bad)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            announce.feed_event("SEARCH", "t", nick="d", results=0, term="t")
        self.assertEqual(len(self.events), 1, "the good sink still got it")

    def test_every_emitter_goes_through_it(self):
        """The feed kinds each have one site; each must reach the event sink
        with its fields, not send_debug() with prose only. DRIVEN, not read
        (#705, audit L41): the substring version was satisfied by a
        commented-out or `if False:`-wrapped call, and its `category="KIND"`
        guard named a string that exists nowhere. Each emitter is called the
        way the daemon calls it and the sink records the kind. REQUEST and
        SEARCH need a library and a list, and are driven in
        test_the_feed_says_which_channel.py (test_a_file_request,
        test_a_search_with_a_hit)."""
        import dcc
        import runtime
        from tests.support import RecordingSocket
        self.set_config(CONSOLE_SHOW_QUEUE=True, CONSOLE_SHOW_SENDS=True, CONSOLE_SHOW_FAILS=True,
                        ANNOUNCE_TRANSFERS=False, MAX_DCC_SLOTS=3)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)

        announce.send_dcc_sending_notice("dave", "A.flac", path=None, channel="#c")
        announce.send_dcc_queue_notice("dave", "A.flac", 2, channel="#c")
        announce.send_transfer_complete("#c", "dave", "A.flac", 1000, time.time() - 5, 200, duration=5.0)
        dcc._report_transfer_failure("dave", "A.flac", "socket died", acked=0, total=1000, channel="#c")
        dcc.register_send_offer("dave", 5000, "A.flac", 1000)
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_resume_request(RecordingSocket(), "dave", "DCC RESUME A.flac 5000 10")

        seen = [kind for kind, _fields, _text in self.events]
        self.assertEqual(seen, ["SENDING", "QUEUED", "SENT", "FAIL", "RESUMED"])

    def test_sent_is_finally_sent(self):
        """Sent: went out as INFO from the day it was written, so the [SENT]
        tag never fired for it and #528's sends tickbox never governed it."""
        self.set_config(ANNOUNCE_TRANSFERS=False, CONSOLE_SHOW_SENDS=True)
        announce.send_transfer_complete("#c", "dave", "A.flac", 1000, time.time() - 5, 200, duration=5.0)
        self.assertEqual(self.prose[-1][0], "SENT")
        kind, fields, _ = self.events[-1]
        self.assertEqual((kind, fields["nick"], fields["bytes"], fields["seconds"], fields["bytes_per_s"], fields["name"]),
                         ("SENT", "dave", 1000, 5.0, 200, "A.flac"))

    def test_sending_reports_the_size_when_it_knows_the_path(self):
        self.set_config(MAX_DCC_SLOTS=3, CONSOLE_SHOW_SENDS=True)
        config.active_transfers[:] = [{"user": "dave", "file": "A.flac", "bytes_sent": 0}]
        path = os.path.join(self.make_tree().root, "A.flac")
        with io.open(path, "wb") as handle:
            handle.write(b"\x00" * 4096)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_dcc_sending_notice("dave", "A.flac", path=path)
        kind, fields, _ = self.events[-1]
        self.assertEqual((kind, fields["slot"], fields["slots"], fields["bytes"]), ("SENDING", 1, 3, 4096))

    def test_sending_without_a_path_reports_zero_not_a_guess(self):
        self.set_config(MAX_DCC_SLOTS=3, CONSOLE_SHOW_SENDS=True)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_dcc_sending_notice("dave", "A.flac")
        self.assertEqual(self.events[-1][1]["bytes"], 0)


class ASessionThatSaysHello(DCCoreTestCase):
    def session(self, authenticated=True):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = authenticated
        return s

    def test_hello_switches_the_session_and_answers(self):
        self.set_config(NICKNAME="MusicBot")
        s = self.session()
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_command(s, "hello dccore.mrc 1.0")
        self.assertTrue(s.structured)
        self.assertEqual(s.client, "dccore.mrc")
        # Since step 3 a first STATUS burst follows the greeting; HELLO is
        # still the first structured line the client sees.
        first = [l for l in s._outbox if l.startswith("DCCORE ")][0]
        self.assertTrue(first.startswith("DCCORE HELLO 1.1 MusicBot "))

    def test_before_authentication_hello_is_just_a_wrong_password(self):
        """handle_command() is only reached once authenticated; a line on an
        unauthenticated socket is a password guess, whatever it says."""
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw", iterations=1000))
        s = self.session(authenticated=False)
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(s, "hello dccore.mrc 1.0")
        self.assertFalse(s.structured)
        self.assertEqual(s.attempts, 1)

    def test_a_plain_session_is_the_console_it_was(self):
        s = self.session()
        s.debug_sink("dave returned", "JOIN")
        s.event_sink("SENT", {"nick": "dave", "name": "x"}, "Sent: x")
        lines = list(s._outbox)
        self.assertEqual(lines, [adminchat.console_line("dave returned", "JOIN")])

    def test_a_structured_session_gets_fields_not_prose_for_feed_kinds(self):
        s = self.session(); s.structured = True
        s.debug_sink('Sent: "x" to dave [1k/s]', "SENT")            # the prose half
        s.event_sink("SENT", {"nick": "dave", "bytes": 1, "seconds": 1, "bytes_per_s": 1, "name": "x"}, "Sent")
        # (step 3 follows a SENT with a STATUS burst; that is not the prose)
        lines = [l for l in s._outbox if not l.startswith(("DCCORE STATUS ", "DCCORE SLOT ", "DCCORE QUEUE "))]
        self.assertEqual(lines, ["DCCORE SENT dave - 1 1.0 1 x"], "the prose must not arrive twice")

    def test_a_structured_session_gets_every_other_category_as_log(self):
        s = self.session(); s.structured = True
        s.debug_sink(f"{config.C_BOLD}helen{config.C_RESET} returned", "JOIN")
        self.assertEqual(list(s._outbox), ["DCCORE LOG JOIN helen returned"])

    def test_command_replies_are_wrapped_as_out(self):
        s = self.session(); s.structured = True
        s.send("Slots: 1/3 in use")
        s.send("")
        self.assertEqual(list(s._outbox), ["DCCORE OUT Slots: 1/3 in use", "DCCORE OUT "])

    def test_a_dccore_line_is_never_double_wrapped(self):
        s = self.session(); s.structured = True
        s.send("DCCORE HELLO 1 x y")
        self.assertEqual(list(s._outbox), ["DCCORE HELLO 1 x y"])

    def test_hello_is_in_the_command_table_with_help(self):
        self.assertIn("hello", adminchat.COMMANDS)
        self.assertIn("structured", adminchat.COMMANDS["hello"][1])


class ASlowClientIsToldWhatItLost(DCCoreTestCase):
    def test_dropped_lines_are_reported_on_the_next_line_through(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        s = adminchat.Session(a, "127.0.0.1", "SysOp", "h")
        s.authenticated = True; s.structured = True
        # Fill the outbox past its cap BEFORE the writer runs, so lines drop.
        for i in range(adminchat.OUTBOX_MAX + 7):
            s.send(f"DCCORE LOG INFO line {i}")
        self.assertEqual(s.dropped, 7)
        s.start_writer()
        b.settimeout(5.0)
        buffer = b""
        deadline = time.time() + 5.0
        while time.time() < deadline and b"DCCORE DROPPED 7\n" not in buffer:
            try:
                buffer += b.recv(65536)
            except socket.timeout:
                break
        s.close(None)
        self.assertIn(b"DCCORE DROPPED 7\n", buffer)
        first = buffer.split(b"\n", 1)[0]
        self.assertEqual(first, b"DCCORE DROPPED 7", "the report comes before the next line, not after")


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class OverARealChat(unittest.TestCase):
    """Login, hello, and a feed event, over a real loopback DCC chat - the
    path dccore.mrc will take."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)
        config.NICKNAME = "MusicBot"
        config.CONSOLE_SHOW_SEARCHES = True
        config.DEBUG_TO_CONSOLE = True
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)

    def read_until(self, needle, timeout=5.0):
        buffer = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            if needle in buffer:
                return buffer
        return buffer

    def test_hello_then_a_search_arrives_as_one_structured_line(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp", f"DCC CHAT chat 2130706433 {self.port}")
            self.listener.settimeout(5.0)
            self.client, _ = self.listener.accept()
            self.client.settimeout(5.0)
            self.addCleanup(self.client.close)
            self.read_until("Enter Your Password:")
            self.client.sendall((PASSWORD + "\n").encode())
            self.read_until("For help type")
            self.assertTrue(wait_for(lambda: adminchat.active_session() is not None))

            self.client.sendall(b"hello dccore.mrc 1.0\n")
            text = self.read_until("DCCORE HELLO")
            self.assertIn("DCCORE HELLO 1.1 MusicBot ", text)

            announce.feed_event("SEARCH", 'dave searched "x" - 3 results', nick="dave", results=3, term="x")
            text = self.read_until("DCCORE SEARCH")
        self.assertIn("DCCORE SEARCH dave - 3 x\n", text)
        self.assertNotIn('dave searched "x"', text, "the prose must not arrive as well")


if __name__ == "__main__":
    unittest.main()
