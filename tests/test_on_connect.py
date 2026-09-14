"""Commands sent to the server once registered, before joining.

Every network wants something different done first - Undernet wants an X login
and `+x`, somewhere else it is NickServ or a usermode. The MOMENT is what these
tests mostly pin, because on Undernet `+x` replaces the host everyone in the
channel sees, and joining before it lands puts the real host in front of
everybody already sitting there.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import on_connect  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

X_LOGIN = "PRIVMSG X@channels.undernet.org :LOGIN someone hunter2"


class OnConnectCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "on_connect.json")
        self.set_config(ON_CONNECT_FILE=self.store)

    def write(self, payload):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


class WithNothingConfigured(OnConnectCase):

    def test_there_is_nothing_to_send(self):
        """Every install, by default. It must not be able to stop the bot
        connecting."""
        self.assertEqual(on_connect.load(), ([], on_connect.DEFAULT_DELAY_SECONDS))

    def test_an_unreadable_file_is_not_a_reason_to_fail_the_connect(self):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(on_connect.load()[0], [])

    def test_a_file_of_the_wrong_shape_is_not_either(self):
        self.write(["just", "a", "list"])

        self.assertEqual(on_connect.load()[0], [])


class WhatGetsStored(OnConnectCase):

    def test_the_commands_and_the_gap_come_back(self):
        on_connect.save([X_LOGIN, "MODE %nick% +x"], 3)

        commands, delay = on_connect.load()

        self.assertEqual(commands, [X_LOGIN, "MODE %nick% +x"])
        self.assertEqual(delay, 3)

    def test_a_newline_inside_one_entry_does_not_become_two_commands(self):
        """That is how one line would turn into two on the wire. An operator
        who pastes a block with a stray newline means one command, not one
        command and whatever the tail parses as."""
        on_connect.save(["MODE %nick% +x" + chr(13) + chr(10) + "QUIT"], 1)

        commands, _delay = on_connect.load()

        self.assertEqual(len(commands), 1)
        self.assertNotIn(chr(10), commands[0])

    def test_blank_lines_are_dropped_rather_than_sent(self):
        on_connect.save(["MODE %nick% +x", "   ", ""], 1)

        self.assertEqual(on_connect.load()[0], ["MODE %nick% +x"])

    def test_the_gap_is_clamped_to_something_sane(self):
        self.write({"commands": ["MODE %nick% +x"], "delay_seconds": 9999})

        self.assertEqual(on_connect.load()[1], on_connect.MAX_DELAY_SECONDS)

    def test_a_command_too_long_for_an_irc_line_is_refused(self):
        """512 bytes including CRLF. A longer one is not a command the server
        will ever see whole, so it is refused where it is written rather than
        truncated on the wire."""
        with self.assertRaises(ValueError) as raised:
            on_connect.save(["PRIVMSG #x :" + ("a" * 600)], 1)

        self.assertIn("IRC line limit", str(raised.exception))

    def test_every_fault_is_reported_at_once(self):
        found = on_connect.problems(["PRIVMSG #x :" + ("a" * 600), ""], 999)

        self.assertGreaterEqual(len(found), 2)

    def test_a_fault_names_the_position_not_the_text(self):
        """The text may be a password. "command 1" is enough to find it."""
        found = on_connect.problems(["PRIVMSG #x :" + ("a" * 600)], 1)

        self.assertIn("command 1", found[0])
        self.assertNotIn("aaaa", found[0])


class WhatIsSafeToShow(OnConnectCase):
    """An X login line has a password in it, and send_debug() writes to a
    CHANNEL."""

    def test_only_the_command_word_survives_redaction(self):
        shown = on_connect.redacted([X_LOGIN, "MODE %nick% +x"])

        self.assertEqual(shown, ["PRIVMSG ...", "MODE ..."])
        self.assertNotIn("hunter2", " ".join(shown))

    def test_the_connect_path_logs_only_that(self):
        """Read out of irc.py: the log line is built from redacted(), and the
        raw command never reaches print()."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        # "join_batches(" is where the JOIN begins (#510). The marker used to
        # be "JOIN {channels}" - one line sending every channel at once, which
        # the server truncated - so it moved with the mechanism. Worth keeping
        # in step rather than letting it rot: str.split() returns the WHOLE
        # body when it finds nothing, so a stale marker widens these windows
        # to the entire function and they go on passing.
        body = code.split("def delayed_join(", 1)[1].split("join_batches(", 1)[0]

        self.assertIn("on_connect.redacted(commands)", body)
        self.assertNotIn("print(f\"[CONNECT] Sending {commands}", body)


class WhenTheyAreSent(OnConnectCase):
    """The ordering, which is the whole feature."""

    def test_they_go_before_the_join(self):
        """On Undernet, X login takes +x and +x replaces the host every person
        in the channel sees. Joining first puts the real host in front of
        everybody already there, and no later mode change takes it back."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def delayed_join(", 1)[1]

        self.assertLess(body.index("on_connect.load()"),
                        body.index("join_batches("),
                        "the JOIN happens before the on-connect commands")

    def test_the_gap_is_taken_between_commands(self):
        """Sending an X login, a usermode and whatever else back to back is how
        a bot meets Excess Flood on its own first line. Read out of irc.py:
        driving a real connect needs a server.

        Asserted as a SEQUENCE - the sleep sits between the loop and the send -
        because a sleep before the loop and a sleep inside it look identical to
        a check for "is there a sleep"."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def delayed_join(", 1)[1].split("join_batches(", 1)[0]
        loop = body.split("for index, command in enumerate(commands):", 1)

        self.assertEqual(len(loop), 2, "the commands are not sent in a loop")
        self.assertIn("time.sleep(gap)", loop[1],
                      "every command is sent at once, with no gap between them")
        self.assertIn("if index:", loop[1],
                      "the gap is taken before the FIRST command too, which "
                      "delays the join for no reason")

    def test_a_failure_there_does_not_stop_the_join(self):
        """A bot that will not join because one optional line was refused is
        worse off than one that joined without its usermode."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def delayed_join(", 1)[1].split("join_batches(", 1)[0]

        self.assertIn("joining anyway", body)

    def test_the_nick_placeholder_uses_the_one_the_server_gave_us(self):
        """`MODE %nick% +x` needs the nick the SERVER settled on - a 433
        collision rebinds it, and writing the configured nick into that command
        would send a MODE for somebody else."""
        self.assertEqual(on_connect.expand("MODE %nick% +x", "DCCore_"),
                         "MODE DCCore_ +x")

    def test_a_command_with_no_placeholder_is_untouched(self):
        self.assertEqual(on_connect.expand(X_LOGIN, "DCCore_"), X_LOGIN)


class TheMsgShorthandBecomesPrivmsg(unittest.TestCase):
    """Reported live: the dashboard's own instructions say to write these
    "exactly as you would type it into a client" - and for the single most
    common line this file exists for, an X login or a NickServ IDENTIFY,
    that was only true for MODE and JOIN, never for MSG. mIRC and every
    other client translate `/msg <target> <text>` into `PRIVMSG <target>
    :<text>` before the line ever reaches the wire; this project sent the
    operator's line verbatim, so "msg X@channels.undernet.org LOGIN name
    pass" - typed exactly as the guidance says to - reached Undernet as a
    literal, nonexistent "MSG" command and came back `421 Unknown command`.
    """

    def test_msg_becomes_privmsg(self):
        self.assertEqual(
            on_connect.normalize("msg X@channels.undernet.org LOGIN name pass"),
            "PRIVMSG X@channels.undernet.org :LOGIN name pass")

    def test_it_is_case_insensitive(self):
        """Operators copy these from a dozen different guides, some of which
        write it in caps."""
        self.assertEqual(
            on_connect.normalize("MSG NickServ IDENTIFY hunter2"),
            "PRIVMSG NickServ :IDENTIFY hunter2")

    def test_a_raw_privmsg_line_is_untouched(self):
        """An operator who already wrote the wire form - the dashboard's own
        placeholder shows this exact line - must see it sent unchanged."""
        self.assertEqual(on_connect.normalize(X_LOGIN), X_LOGIN)

    def test_other_commands_are_untouched(self):
        """MODE, JOIN and NOTICE already match their own wire form - MSG is
        the one mismatch, not a general client-syntax interpreter."""
        for command in ("MODE %nick% +x", "JOIN #chan", "NOTICE AuthServ :hi"):
            with self.subTest(command=command):
                self.assertEqual(on_connect.normalize(command), command)

    def test_a_bare_msg_with_nothing_after_the_target_is_left_alone(self):
        """No message text to carry, so this is not the shape being fixed -
        left as the operator wrote it rather than guessed at."""
        self.assertEqual(on_connect.normalize("msg NickServ"), "msg NickServ")

    def test_something_that_only_starts_with_the_letters_is_not_mistaken_for_it(self):
        """The match is the whole first word, not a prefix - "msginfo" is
        somebody's actual (unlikely) command, not "msg" with a typo."""
        self.assertEqual(on_connect.normalize("msginfo foo bar"), "msginfo foo bar")

    def test_none_and_empty_are_handled(self):
        self.assertEqual(on_connect.normalize(""), "")
        self.assertEqual(on_connect.normalize(None), "")

    def test_the_byte_limit_is_checked_against_what_is_actually_sent(self):
        """normalize() can make a line LONGER - PRIVMSG plus the added colon
        is five bytes more than msg - so the 510-byte check has to catch
        growth introduced by the rewrite, not just the length of what the
        operator actually typed."""
        raw = "msg X " + ("a" * 502)
        self.assertLessEqual(len(raw.encode("utf-8")), on_connect.MAX_COMMAND_BYTES)
        self.assertGreater(
            len(on_connect.normalize(raw).encode("utf-8")),
            on_connect.MAX_COMMAND_BYTES)

        found = on_connect.problems([raw], 1)

        self.assertTrue(any("IRC line limit" in f for f in found), found)

    def test_the_connect_path_normalizes_before_expanding_and_sending(self):
        """Read out of irc.py: normalize() has to actually run in the send
        loop, not just exist as a function nothing calls."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def delayed_join(", 1)[1].split("join_batches(", 1)[0]

        self.assertIn("on_connect.normalize(command)", body)


class TheDashboardEndpoint(OnConnectCase):

    def test_a_block_of_text_is_split_into_commands(self):
        """The page sends a textarea, because that is what an operator
        pastes."""
        status, result = webserver.apply_on_connect_changes(
            {"commands": X_LOGIN + chr(10) + "MODE %nick% +x",
             "delay_seconds": 2})

        self.assertEqual(status, 200)
        self.assertEqual(result["commands"], [X_LOGIN, "MODE %nick% +x"])

    def test_the_commands_come_back_in_full(self):
        """A box an operator can overwrite but never read means never
        correcting a typo without retyping the lot - and anybody with the
        dashboard login can read the file off the disk anyway."""
        webserver.apply_on_connect_changes({"commands": [X_LOGIN],
                                            "delay_seconds": 1})

        self.assertEqual(webserver.build_on_connect_payload()["commands"],
                         [X_LOGIN])

    def test_saving_says_a_reconnect_is_what_applies_them(self):
        """Not a rehash. They are read fresh at every connect, and saying
        "rehash" would send an operator to do something that changes
        nothing."""
        _status, result = webserver.apply_on_connect_changes(
            {"commands": [X_LOGIN], "delay_seconds": 1})

        self.assertTrue(result["reconnect_required"])
        self.assertIn("reconnect", result["message"].lower())

    def test_clearing_it_is_allowed_and_says_so(self):
        webserver.apply_on_connect_changes({"commands": [X_LOGIN],
                                            "delay_seconds": 1})

        _status, result = webserver.apply_on_connect_changes({"commands": []})

        self.assertEqual(result["commands"], [])
        self.assertFalse(result["reconnect_required"])

    def test_a_bad_body_is_refused(self):
        for body in ("nope", {"commands": 5}, {"commands": [X_LOGIN],
                                               "delay_seconds": "soon"}):
            with self.subTest(body=body):
                status, _result = webserver.apply_on_connect_changes(body)
                self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
