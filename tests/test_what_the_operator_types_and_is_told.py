"""Two things the operator is told, and one of them is what they type.

  * #486 `on_connect.normalize()` rewrote `msg` into PRIVMSG, which is what
    #474 asked for - but in a client you type `/msg`, and the slash is
    consumed by the client rather than sent. Every on-connect line had the
    same problem: `/mode`, `/join` and `/nick` all reached the server with
    the slash attached and all came back `421 Unknown command`.
  * #465 two runtime messages told the operator to set MY_IP_OR_DOCK "in
    admin_config.py or settings.conf". The second route does not work on a
    fresh install - nothing declares the name, so settings_file.apply_to()
    discards it - and it must not be made to work: the value is the address
    DETECTED at startup, and one written into the file would freeze one
    session's answer into every session after it.
"""

import io
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402
import on_connect  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

X_LOGIN = "PRIVMSG X@channels.undernet.org :LOGIN someone hunter2"


def code_only(name):
    """`name`'s source with comments and docstrings taken out.

    A message this file is about is quoted in the comment above it and in the
    changelog entry for it, so a test that searched the raw source would pass
    on the prose alone.
    """
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        text = handle.read()
    text = re.sub(chr(35) + "[^" + chr(10) + "]*", "", text)
    return re.sub(r'"""..*?"""', "", text, flags=re.S)


class TheLeadingSlashComesOff(unittest.TestCase):
    """#486."""

    def test_the_slash_a_client_user_types_is_not_sent(self):
        """The instruction an operator follows for an X login is written with
        the slash, because that is how you type it into a client."""
        self.assertEqual(
            on_connect.normalize("/msg X@channels.undernet.org LOGIN someone hunter2"),
            X_LOGIN)

    def test_it_is_not_a_msg_problem(self):
        """#474 fixed the shorthand; this is every other line. All of them
        were sent with the slash attached and all of them were rejected.

        The case is left exactly as typed, deliberately: RFC 1459 makes the
        command word case-insensitive on the wire, so `mode` is a MODE, and
        rewriting it would be a second, unasked-for change to a line the
        operator is entitled to see sent as they wrote it."""
        for typed, wire in (("/mode %nick% +x", "mode %nick% +x"),
                            ("/join #chan", "join #chan"),
                            ("/NICK Somebody", "NICK Somebody"),
                            ("/notice AuthServ :hi", "notice AuthServ :hi")):
            with self.subTest(typed=typed):
                self.assertEqual(on_connect.normalize(typed), wire)

    def test_exactly_one_slash_comes_off(self):
        """`//` is an escaped literal slash in most clients, so it keeps one
        rather than losing both. Decided deliberately - see the issue."""
        self.assertEqual(on_connect.normalize("//raw thing"), "/raw thing")

    def test_a_line_with_no_slash_is_untouched(self):
        """The other half. An operator who already wrote the wire form - the
        dashboard's own placeholder shows exactly that - must see it sent
        unchanged."""
        for command in (X_LOGIN, "MODE %nick% +x", "JOIN #chan"):
            with self.subTest(command=command):
                self.assertEqual(on_connect.normalize(command), command)

    def test_a_slash_after_leading_space_still_comes_off(self):
        """These arrive as a pasted block, and a pasted block carries
        whatever indentation it was copied with."""
        self.assertEqual(on_connect.normalize("  /msg NickServ IDENTIFY hunter2"),
                         "PRIVMSG NickServ :IDENTIFY hunter2")

    def test_a_slash_inside_the_line_is_left_alone(self):
        """Only the leading one is the client's. A slash in a password or a
        channel key is data."""
        self.assertEqual(on_connect.normalize("JOIN #chan pass/word"),
                         "JOIN #chan pass/word")

    def test_a_slash_and_nothing_else_is_refused_rather_than_sent(self):
        """New with the stripping: "/" is not blank to _clean_command(), but
        once the slash comes off there is no command left and what reaches
        the server is a bare CRLF it discards - which looks exactly like a
        command that ran."""
        found = on_connect.problems(["/"], 1)

        self.assertTrue(any("slash" in line for line in found), found)

    def test_the_byte_limit_is_measured_after_the_slash_comes_off(self):
        """The 510 bytes are what goes on the wire. The slash is not on the
        wire and PRIVMSG is longer than msg, so neither the typed length nor
        the stored one is the number that matters."""
        raw = "/msg X " + ("a" * 502)
        self.assertLessEqual(len(raw.encode("utf-8")), on_connect.MAX_COMMAND_BYTES)

        found = on_connect.problems([raw], 1)

        self.assertTrue(any("IRC line limit" in line for line in found), found)


class TheAddressIsNotAFileSetting(DCCoreTestCase):
    """#465."""

    def test_the_lookup_failure_names_admin_config_only(self):
        """Driven, not read: the message is what an operator sees when ipify
        cannot be reached, which is the moment they go and set this."""
        self.set_config(MY_IP_OR_DOCK="")
        said = []

        def unreachable():
            raise OSError("no network")

        found = irc.resolve_dcc_address(lookup=unreachable, log=said.append)

        self.assertEqual(found, "", "a failed lookup must not invent an address")
        text = " ".join(said)
        self.assertIn("admin_config.py", text)
        self.assertNotIn("admin_config.py or settings.conf", text,
                         "the message still offers a route that does not work")

    def test_the_refused_send_names_admin_config_only(self):
        """The other message. Read out of dcc.py with comments stripped: it
        is printed from inside the send path, several hundred lines past the
        point a test can reach without a real transfer."""
        code = code_only("dcc.py")
        abort = code.split("No usable public address", 1)

        self.assertEqual(len(abort), 2, "the abort message has moved")
        message = abort[1][:600]
        self.assertIn("admin_config.py", message)
        self.assertNotIn("admin_config.py or settings.conf", message)

    def test_the_file_itself_says_what_to_do_instead(self):
        """An operator who puts it in settings.conf anyway was told it was a
        spelling mistake. It is spelled perfectly - it is simply not set from
        there, which is a different thing to go and check."""
        said = []
        namespace = {"MSG_DELAY": 5.0}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.conf")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("MY_IP_OR_DOCK = 203.0.113.9\n")
            report = settings_file.apply_to(namespace, path, log=said.append)

        self.assertEqual(report["unknown"], ["MY_IP_OR_DOCK"])
        self.assertNotIn("MY_IP_OR_DOCK", namespace,
                         "declaring it is the fix the issue rules out - a "
                         "value in the file freezes one session's detected "
                         "address into every session after it")
        text = " ".join(said)
        self.assertIn("admin_config.py", text)
        self.assertNotIn("Check the spelling", text)

    def test_a_real_typo_still_gets_the_spelling_advice(self):
        """The other half: the new message must not swallow the case the old
        one was right about."""
        said = []
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.conf")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("MSG_DELY = 3\n")
            settings_file.apply_to({"MSG_DELAY": 5.0}, path, log=said.append)

        self.assertIn("Check the spelling", " ".join(said))
