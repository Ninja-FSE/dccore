"""A partial download can be resumed.

REPORTED FROM A BETA, from mIRC: a transfer sat at "Requesting resume" and
never moved. Not a failure, not an error - a client keeping its side of a
bargain this bot had never been able to answer. DCC RESUME was not
implemented at all.

The exchange is three lines. We offer:

    DCC SEND <name> <ip> <port> <size>

a receiver holding a partial file answers with what it already has:

    DCC RESUME <name> <port> <position>

and the sender MUST answer before anything else happens:

    DCC ACCEPT <name> <port> <position>

Only then does the receiver connect. Without the ACCEPT it waits.

MATCHED BY PORT, NEVER BY FILENAME. The port is ours, unique per offer, and
unambiguous. The offered name has already been through a space-to-underscore
pass and announce.fit_irc_filename() may have SHORTENED it to fit the IRC
line - so the name we sent is not always the name we hold, and matching on it
would fail on exactly the long-titled files most likely to need resuming.

THE ORDERING IS THE RACE. The offset is stored before the ACCEPT goes out,
because a receiver connects only once it has seen the ACCEPT. Sending first
and storing after would leave a window in which a prompt client connects and
is sent the file from byte zero, appended onto what it already had.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

USER = "someuser"
PORT = 5000
SIZE = 1_000_000
OFFERED = "Some_Long_Album_Name.zip"


class FakeIrcSocket:
    """Records what would have gone out on the wire."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def sendall(self, payload):
        # Delegated, not duplicated (#504) - see the note in
        # tests/test_no_socket_write_is_a_partial_write.py. A fake that
        # answered only send() would stop recording the moment production
        # was fixed, and the test would report it as the daemon having gone
        # quiet.
        self.send(payload)

    def send(self, payload):
        if self.fail:
            raise OSError("socket is gone")
        self.sent.append(payload.decode("utf-8", "replace"))
        return len(payload)


class ParsingAResumeLine(unittest.TestCase):

    def test_the_ordinary_shape(self):
        self.assertEqual(
            dcc.parse_resume_request("DCC RESUME Album.zip 5000 4096"),
            ("Album.zip", 5000, 4096))

    def test_a_quoted_name_with_spaces(self):
        """mIRC quotes a name containing spaces. Splitting from the right
        means the name needs no quote parsing a peer could get creative
        with - the last two fields are numbers and the rest is the name."""
        parsed = dcc.parse_resume_request(
            'DCC RESUME "Some Album Name.zip" 5000 4096')

        self.assertEqual(parsed, ("Some Album Name.zip", 5000, 4096))

    def test_the_ctcp_delimiters_are_tolerated(self):
        self.assertEqual(
            dcc.parse_resume_request("\x01DCC RESUME Album.zip 5000 1\x01"),
            ("Album.zip", 5000, 1))

    def test_a_lowercase_verb_is_accepted(self):
        self.assertIsNotNone(
            dcc.parse_resume_request("dcc resume Album.zip 5000 1"))

    def test_a_different_verb_is_not_a_resume(self):
        self.assertIsNone(
            dcc.parse_resume_request("DCC SEND Album.zip 1 5000 10"))

    def test_a_missing_field_is_refused(self):
        self.assertIsNone(dcc.parse_resume_request("DCC RESUME Album.zip 5000"))

    def test_a_non_numeric_position_is_refused(self):
        self.assertIsNone(
            dcc.parse_resume_request("DCC RESUME Album.zip 5000 soon"))

    def test_a_negative_position_is_refused(self):
        self.assertIsNone(
            dcc.parse_resume_request("DCC RESUME Album.zip 5000 -1"))

    def test_a_port_outside_the_range_is_refused(self):
        """It cannot be one we are listening on, and saying so here means the
        log names the reason rather than reporting a lookup miss."""
        self.assertIsNone(
            dcc.parse_resume_request("DCC RESUME Album.zip 70000 10"))
        self.assertIsNone(
            dcc.parse_resume_request("DCC RESUME Album.zip 0 10"))

    def test_nothing_at_all(self):
        self.assertIsNone(dcc.parse_resume_request(""))
        self.assertIsNone(dcc.parse_resume_request(None))


class ReadingBackTheNameWeActuallyOffered(unittest.TestCase):
    """The ACCEPT must echo the name the RECEIVER is matching on, which is the
    name that went out - not the one we hold, which may be longer."""

    def test_the_name_comes_out_of_the_handshake(self):
        handshake = (f"PRIVMSG {USER} :\x01DCC SEND {OFFERED} "
                     f"2130706433 {PORT} {SIZE}\x01\r\n")

        self.assertEqual(dcc.offered_name_from_handshake(handshake), OFFERED)

    def test_a_shortened_name_is_read_as_sent(self):
        """announce.fit_irc_filename() may trim the name to fit 512 bytes.
        Assuming the original would put a name in the ACCEPT that the
        receiver has never seen."""
        handshake = (f"PRIVMSG {USER} :\x01DCC SEND Trimmed_Na "
                     f"2130706433 {PORT} {SIZE}\x01\r\n")

        self.assertEqual(dcc.offered_name_from_handshake(handshake),
                         "Trimmed_Na")

    def test_a_line_that_is_not_an_offer_yields_nothing(self):
        self.assertEqual(dcc.offered_name_from_handshake("PRIVMSG x :hi"), "")
        self.assertEqual(dcc.offered_name_from_handshake(""), "")


class AnsweringAResume(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.sock = FakeIrcSocket()

    def offer(self, size=SIZE):
        dcc.register_send_offer(USER, PORT, OFFERED, size)

    def accept_line(self):
        return "".join(self.sock.sent)

    def test_an_accept_goes_back(self):
        """The whole report: without this the client waits forever."""
        self.offer()

        answered = dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertTrue(answered)
        self.assertIn(f"\x01DCC ACCEPT {OFFERED} {PORT} 4096\x01",
                      self.accept_line())

    def test_the_offset_is_stored_for_the_waiting_sender(self):
        """The sending thread is blocked in accept() and reads this once the
        receiver connects."""
        self.offer()

        dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertEqual(
            runtime.dcc_send_offers[(USER, PORT)]["position"], 4096)

    def test_the_offset_is_stored_before_the_accept_is_sent(self):
        """THE RACE. A receiver connects only once it has seen the ACCEPT, so
        if the send happened first a prompt client could connect and be sent
        the file from byte zero, appended onto what it already had."""
        seen = {}

        class WatchingSocket(FakeIrcSocket):
            # Overrides send(), and the base class's sendall() delegates to
            # it - so this still watches whichever one the daemon calls.
            def send(inner, payload):
                seen["position_at_send_time"] = (
                    runtime.dcc_send_offers[(USER, PORT)]["position"])
                return super().send(payload)

        self.offer()

        dcc.handle_resume_request(
            WatchingSocket(), USER, f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertEqual(seen["position_at_send_time"], 4096)

    def test_the_name_we_offered_is_echoed_not_the_one_they_sent(self):
        """No text off the wire is interpolated back into an outbound line,
        and the receiver is matching on what WE sent anyway."""
        self.offer()

        dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME TotallyDifferent.zip {PORT} 10")

        self.assertIn(f"DCC ACCEPT {OFFERED} {PORT} 10", self.accept_line())
        self.assertNotIn("TotallyDifferent", self.accept_line())

    def test_a_resume_for_a_port_we_are_not_offering_on_is_ignored(self):
        """Anyone on the network can send this line. The only thing that
        makes it ours is a port we are listening on for that exact nick."""
        self.offer()

        answered = dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} 9999 4096")

        self.assertFalse(answered)
        self.assertEqual(self.sock.sent, [])

    def test_a_resume_from_a_different_nick_is_ignored(self):
        self.offer()

        answered = dcc.handle_resume_request(
            self.sock, "someoneelse", f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertFalse(answered)
        self.assertEqual(self.sock.sent, [])

    def test_a_resume_with_no_offer_at_all_is_ignored(self):
        answered = dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertFalse(answered)

    def test_the_nick_is_matched_without_regard_to_case(self):
        """IRC nicks are case-insensitive, and the nick on the RESUME comes
        from the server's own prefix rather than from what we stored."""
        self.offer()

        answered = dcc.handle_resume_request(
            self.sock, USER.upper(), f"DCC RESUME {OFFERED} {PORT} 4096")

        self.assertTrue(answered)

    def test_a_position_past_the_end_is_clamped(self):
        """It decides where we seek in a file of ours and it arrives from the
        network. A receiver whose partial copy is longer than our file has a
        different file; answering "you have all of it" completes their
        transfer honestly instead of leaving them hanging, which is the
        failure this whole feature exists to end."""
        self.offer(size=1000)

        dcc.handle_resume_request(
            self.sock, USER, f"DCC RESUME {OFFERED} {PORT} 999999")

        self.assertIn(f"DCC ACCEPT {OFFERED} {PORT} 1000", self.accept_line())
        self.assertEqual(runtime.dcc_send_offers[(USER, PORT)]["position"], 1000)

    def test_a_dead_socket_does_not_raise(self):
        """This runs inline on the IRC read loop. An exception here would
        take the connection down over a resume request."""
        self.offer()

        answered = dcc.handle_resume_request(
            FakeIrcSocket(fail=True), USER, f"DCC RESUME {OFFERED} {PORT} 10")

        self.assertFalse(answered)


class TheOfferRegistry(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)

    def test_clearing_returns_the_offer(self):
        dcc.register_send_offer(USER, PORT, OFFERED, SIZE)

        cleared = dcc.clear_send_offer(USER, PORT)

        self.assertEqual(cleared["position"], 0)
        self.assertEqual(runtime.dcc_send_offers, {})

    def test_clearing_twice_is_not_an_error(self):
        """Every exit from a send runs through the finally that calls it,
        including the ones that never got as far as registering."""
        dcc.register_send_offer(USER, PORT, OFFERED, SIZE)
        dcc.clear_send_offer(USER, PORT)

        self.assertIsNone(dcc.clear_send_offer(USER, PORT))

    def test_clearing_a_port_that_was_never_assigned_is_not_an_error(self):
        """assigned_port is None until a port is actually claimed, and the
        finally still runs for a send that never got one."""
        self.assertIsNone(dcc.clear_send_offer(USER, None))

    def test_the_registry_lives_in_runtime_so_a_rehash_cannot_empty_it(self):
        """dcc.py is in commands.CORE_MODULES, and importlib.reload()
        re-executes a module body - a dict defined there would be replaced
        mid-transfer, stranding every offer in flight."""
        self.assertIs(dcc.runtime.dcc_send_offers, runtime.dcc_send_offers)


class TheSendPathUsesIt(unittest.TestCase):
    """start_dcc_send() needs a live socket and a real listener, so these
    read the source for the four things the wiring has to get right."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_offer_is_registered_before_the_handshake_is_sent(self):
        """A receiver answers the instant the offer lands, on a different
        thread. Registering afterwards would miss it."""
        source = self.source()
        register_at = source.index("register_send_offer(user, assigned_port")
        send_at = source.index("irc_sock.sendall(ctcp_handshake.encode(")

        self.assertLess(register_at, send_at)

    def test_the_file_is_seeked_to_the_agreed_offset(self):
        self.assertIn("f.seek(resume_offset)", self.source())

    def test_the_speed_record_excludes_the_skipped_bytes(self):
        """A resumed send that skipped 4 GB did not move 4 GB, and this
        number feeds the channel advert."""
        source = self.source()
        speed = source.split("acute_bytes = ", 1)[1].split("\n", 1)[0]

        self.assertIn("_skipped", speed)

    def test_the_totals_count_what_went_on_the_wire(self):
        self.assertIn("db.update_stats_on_complete(file_size - resume_offset)",
                      self.source())

    def test_every_exit_drops_the_offer(self):
        """A stale entry is not harmless: ports are reused, so the next offer
        on the same port to the same nick could read an offset agreed for a
        different file."""
        finally_block = self.source().rsplit("    finally:", 1)[1]

        self.assertIn("clear_send_offer(user, assigned_port)", finally_block)


class TheDispatchIsThrottled(unittest.TestCase):

    def source(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_a_resume_reaches_the_handler(self):
        source = self.source()

        self.assertIn('ctcp_cmd.startswith("DCC RESUME ")', source)
        self.assertIn("dcc.handle_resume_request(", source)

    def test_it_is_private_only(self):
        """Like DCC SEND and DCC CHAT above it: a DCC line addressed to a
        channel is meaningless, and acting on one would mean acting on a line
        every member of that channel can send."""
        branch = self.source().split('ctcp_cmd.startswith("DCC RESUME ")', 1)[1] \
                              .split("continue", 1)[0]

        self.assertIn("target_chan.lower() == config.NICKNAME.lower()", branch)

    def test_it_is_in_the_flood_checked_set(self):
        """#219's finding, applied to this verb: it answers with an outbound
        PRIVMSG, and an unthrottled responder is a standard way to make a bot
        flood ITSELF off the network."""
        expression = self.source().split("is_bot_command = (", 1)[1] \
                                  .split("\n                        )", 1)[0]

        self.assertIn('startswith("DCC RESUME ")', expression)


if __name__ == "__main__":
    unittest.main()
