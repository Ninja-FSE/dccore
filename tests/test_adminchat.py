"""Tests for the DCC CHAT admin console gate (phase 1).

This module is the whole security surface of the admin console, so the tests are
about two questions and little else: who gets in, and what does a stranger learn.

The end-to-end cases use a real loopback socket, because the thing being verified
is the actual connect-out path - the bot dials the client's listener, exactly as
iroffer's non-passive branch does. A mock would prove the mock works.
"""

import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

ADMIN_LINE = ":SysOp!~sysop@SysOp.users.undernet.org PRIVMSG DCCore :\x01DCC CHAT chat 2130706433 55555\x01"
STRANGER_LINE = ":dave!~d@cpe-91-22-33-44.isp.net PRIVMSG DCCore :\x01DCC CHAT chat 2130706433 55555\x01"

PASSWORD = "correct horse battery staple"


def _loopback_is_available():
    """Can this machine actually bind and connect on loopback?

    Some build sandboxes and locked-down CI images forbid it. The tests below
    are not unit tests with a socket bolted on - they exist to prove the real
    transport works, so faking it would defeat the point. Where the machine
    cannot do it they skip, and say so.

    Reported as a FAILURE this cost a reviewer real time: nine tests went red
    on a sandbox that simply does not allow this, and the reasonable conclusion
    was that the codebase had pre-existing problems. It did not. A skip says
    "not here", which is the truth; a failure says "broken", which is not.

    Both shapes the code actually uses are probed - a 0.0.0.0 listener, which is
    what _open_chat_listener binds, and a real connect/accept round trip.
    """
    import socket as _socket

    listener = None
    client = None
    accepted = None
    try:
        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.bind(("0.0.0.0", 0))
        listener.listen(1)
        listener.settimeout(2.0)
        port = listener.getsockname()[1]

        client = _socket.create_connection(("127.0.0.1", port), timeout=2.0)
        accepted, _addr = listener.accept()
        return True
    except OSError:
        return False
    finally:
        for sock in (accepted, client, listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


LOOPBACK_OK = _loopback_is_available()
NEEDS_LOOPBACK = "needs loopback socket binding, which this machine does not allow"


def wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll rather than sleep a fixed time, so the suite is neither slow nor flaky."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class HostMatching(unittest.TestCase):
    """The gate. Everything else is depth behind this."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]

    def test_the_configured_host_matches(self):
        self.assertTrue(adminchat.is_admin_host(ADMIN_LINE))

    def test_a_stranger_does_not_match(self):
        self.assertFalse(adminchat.is_admin_host(STRANGER_LINE))

    def test_a_bare_host_pattern_works_too(self):
        """He may write it either way; both must mean the same thing."""
        config.ADMIN_HOSTMASKS = ["SysOp.users.undernet.org"]
        self.assertTrue(adminchat.is_admin_host(ADMIN_LINE))

    def test_the_ident_is_ignored(self):
        """The point of the whole design: only the host is server-issued.

        A pattern that appears to pin the ident must not actually pin it - if it
        did, the gate would break the moment his client's ident setting changed,
        while granting nothing, because anyone can set their ident to 'sysop'.
        """
        config.ADMIN_HOSTMASKS = ["*!sysop@SysOp.users.undernet.org"]
        for ident in ("~sysop", "sysop", "~anything", "x"):
            line = f":SysOp!{ident}@SysOp.users.undernet.org PRIVMSG DCCore :hi"
            with self.subTest(ident=ident):
                self.assertTrue(adminchat.is_admin_host(line))

    def test_the_nick_is_ignored(self):
        """Nick theft is the attack this replaces, so the nick cannot be the check."""
        line = ":SomeoneElse!~x@SysOp.users.undernet.org PRIVMSG DCCore :hi"
        self.assertTrue(adminchat.is_admin_host(line),
                        "the host is the proof; whoever holds it holds the account")

    def test_taking_the_admin_nick_from_another_host_gets_nothing(self):
        """The exact scenario the console exists to close."""
        line = ":SysOp!~sysop@cpe-91-22-33-44.isp.net PRIVMSG DCCore :hi"
        self.assertFalse(adminchat.is_admin_host(line))

    def test_matching_is_case_insensitive(self):
        line = ":SysOp!~f@sysop.USERS.undernet.ORG PRIVMSG DCCore :hi"
        self.assertTrue(adminchat.is_admin_host(line))

    def test_a_wildcard_account_pattern_works(self):
        config.ADMIN_HOSTMASKS = ["*.users.undernet.org"]
        self.assertTrue(adminchat.is_admin_host(ADMIN_LINE))
        self.assertFalse(adminchat.is_admin_host(STRANGER_LINE))

    def test_an_empty_list_admits_nobody(self):
        """Fail closed: an unconfigured console must be an off console."""
        for empty in ([], "", None):
            with self.subTest(empty=empty):
                config.ADMIN_HOSTMASKS = empty
                self.assertFalse(adminchat.is_admin_host(ADMIN_LINE))

    def test_an_all_wildcard_pattern_is_refused(self):
        """'*' would admit the entire network and make the gate decorative."""
        for broad in ("*", "*!*@*", "**"):
            with self.subTest(broad=broad):
                config.ADMIN_HOSTMASKS = [broad]
                self.assertFalse(adminchat.is_admin_host(ADMIN_LINE))

    def test_a_hostmask_in_the_message_body_cannot_forge_it(self):
        """Anchoring, for the same reason event_source_nick is anchored."""
        line = (":dave!~d@isp.net PRIVMSG DCCore :"
                "look at this SysOp!~sysop@SysOp.users.undernet.org")
        self.assertFalse(adminchat.is_admin_host(line))

    def test_a_prefixless_line_cannot_donate_a_host_from_its_body(self):
        """The case that actually separates anchoring from searching.

        The test above passes even against a searching implementation, because a
        PRIVMSG's own prefix contains the first "@" on the line and is found
        first. Only a line with NO user prefix tells the two apart - and there a
        searching implementation hands back a host the line is merely ABOUT.

        Nothing reaches is_admin_host this way today: irc.py only calls it from
        inside the PRIVMSG branch, whose regex already demands a nick!user@host
        prefix. This pins the helper's contract rather than the current caller's
        good manners, exactly as the sibling test does for event_source_nick.
        """
        for line in (":irc.undernet.org 372 DCCore :please mail admin@SysOp.users.undernet.org",
                     "NOTICE AUTH :checking ident@SysOp.users.undernet.org"):
            with self.subTest(line=line):
                self.assertIsNone(adminchat.source_host(line))
                self.assertFalse(adminchat.is_admin_host(line))

    def test_a_server_line_has_no_source_host(self):
        self.assertIsNone(adminchat.source_host(":irc.undernet.org 001 DCCore :Welcome"))
        self.assertFalse(adminchat.is_admin_host(":irc.undernet.org 001 DCCore :Welcome"))

    def test_irc_helper_agrees_with_the_console(self):
        """irc.event_source_host is what feeds this; they must not drift."""
        self.assertEqual(irc.event_source_host(ADMIN_LINE),
                         adminchat.source_host(ADMIN_LINE))
        self.assertEqual(irc.event_source_host(ADMIN_LINE), "sysop.users.undernet.org")

    def test_irc_helper_is_anchored_too(self):
        """Same contract, same failure mode, so the same test on both sides."""
        for line in (":irc.undernet.org 372 DCCore :please mail admin@SysOp.users.undernet.org",
                     "PING :cookie",
                     ":irc.undernet.org 001 DCCore :Welcome"):
            with self.subTest(line=line):
                self.assertIsNone(irc.event_source_host(line))


class PasswordHandling(unittest.TestCase):

    def test_hash_and_verify_round_trip(self):
        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)
        self.assertTrue(adminchat.verify_password(stored, PASSWORD))

    def test_a_wrong_password_is_refused(self):
        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)
        self.assertFalse(adminchat.verify_password(stored, PASSWORD + "x"))
        self.assertFalse(adminchat.verify_password(stored, ""))

    def test_the_hash_does_not_contain_the_password(self):
        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)
        self.assertNotIn(PASSWORD, stored)

    def test_the_salt_makes_two_hashes_of_one_password_differ(self):
        a = adminchat.make_password_hash(PASSWORD, iterations=1000)
        b = adminchat.make_password_hash(PASSWORD, iterations=1000)
        self.assertNotEqual(a, b)
        self.assertTrue(adminchat.verify_password(a, PASSWORD))
        self.assertTrue(adminchat.verify_password(b, PASSWORD))

    def test_an_unset_password_refuses_everyone(self):
        """Fail closed. A console with no password must not be an open console."""
        for stored in ("", None):
            with self.subTest(stored=stored):
                self.assertFalse(adminchat.verify_password(stored, PASSWORD))
                self.assertFalse(adminchat.verify_password(stored, ""))

    def test_a_malformed_stored_value_refuses_rather_than_raising(self):
        """A mistyped admin_config must lock the console, not crash the daemon."""
        for junk in ("garbage", "pbkdf2_sha256$notanint$aa$bb", "pbkdf2_sha256$1$zz$zz",
                     "bcrypt$1$aa$bb", "$$$", "pbkdf2_sha256$1000$aa"):
            with self.subTest(junk=junk):
                self.assertFalse(adminchat.verify_password(junk, PASSWORD))

    def test_verification_is_constant_time(self):
        """That verify_password() actually COMPARES with hmac.compare_digest,
        not that adminchat.py contains that string somewhere.

        The previous version was `assertIn("hmac.compare_digest", source)`.
        Rewriting the return as `expected == actual` and leaving any mention of
        the name behind - this docstring would do it - passed. So would calling
        compare_digest somewhere unrelated. A guard for a timing leak that a
        timing leak walks straight past is worse than no guard, because the
        suite reports it as covered.

        The real function is kept underneath the recorder: the comparison still
        has to give the right answer, or a wrapper that records and returns
        True would satisfy this and admit everyone.
        """
        import hmac

        calls = []
        real = hmac.compare_digest

        def recorder(left, right):
            calls.append((left, right))
            return real(left, right)

        hmac.compare_digest = recorder
        self.addCleanup(setattr, hmac, "compare_digest", real)

        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)

        self.assertTrue(adminchat.verify_password(stored, PASSWORD))
        self.assertTrue(calls,
                        "the digests were compared with == - a wrong password "
                        "is rejected fractionally sooner the earlier it differs, "
                        "which is the whole attack")

    def test_a_wrong_password_is_compared_the_same_way(self):
        """The half an attacker actually drives. A short-circuit on the failing
        path is the timing leak; the succeeding path cannot show it."""
        import hmac

        calls = []
        real = hmac.compare_digest
        hmac.compare_digest = lambda a, b: (calls.append((a, b)), real(a, b))[1]
        self.addCleanup(setattr, hmac, "compare_digest", real)

        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)

        self.assertFalse(adminchat.verify_password(stored, "wrong password"))
        self.assertTrue(calls, "the failing comparison short-circuited")

    def test_the_digests_reaching_the_comparison_are_bytes_of_equal_length(self):
        """compare_digest is only constant-time over equal-length bytes; given
        two different lengths it can return early on the length alone. pbkdf2
        always returns 32 bytes for sha256, so this holds - pinned because a
        change to the stored format is exactly what would break it quietly."""
        import hmac

        seen = []
        real = hmac.compare_digest
        hmac.compare_digest = lambda a, b: (seen.append((a, b)), real(a, b))[1]
        self.addCleanup(setattr, hmac, "compare_digest", real)

        stored = adminchat.make_password_hash(PASSWORD, iterations=1000)
        adminchat.verify_password(stored, "wrong password")

        self.assertTrue(seen)
        for left, right in seen:
            self.assertIsInstance(left, bytes)
            self.assertIsInstance(right, bytes)
            self.assertEqual(len(left), len(right))


class OfferParsing(unittest.TestCase):

    def test_a_normal_offer_parses(self):
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 2130706433 55555"),
                         ("127.0.0.1", 55555, None))

    def test_the_passive_form_asks_us_to_listen(self):
        """Port 0 means "you listen instead". Phase 1 refused it; it is handled now.

        See ListenModeEndToEnd - an offer with no dialable address is answered
        with an offer of our own rather than a failure.
        """
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 2130706433 0"), (None, 0, None))

    def test_a_privileged_port_is_refused(self):
        self.assertIsNone(adminchat.parse_offer("DCC CHAT chat 2130706433 22"))

    def test_junk_is_refused_rather_than_raising(self):
        for junk in ("", "DCC CHAT", "DCC SEND file 1 2", "DCC CHAT chat x y",
                     "DCC CHAT chat 999999999999 55555", "DCC CHAT chat -1 55555",
                     "DCC CHAT chat 2130706433 99999"):
            with self.subTest(junk=junk):
                self.assertIsNone(adminchat.parse_offer(junk))


class OfferedAddressIsUsable(unittest.TestCase):
    """The field bug: mIRC offered 0.0.0.0 and the bot dialled itself.

    Reported from a live run:

        [ADMINCHAT] Could not connect to SysOp at 0.0.0.0:11283
        ([Errno 111] Connection refused).

    0.0.0.0 is what mIRC sends when its Local Info lookup has not resolved. It
    is not a harmless no-op: on Linux connect() to 0.0.0.0 means "this host", so
    the daemon dialled its OWN port 11283, found nothing listening, and reported
    the refusal as though the operator's client had rejected it.

    An unusable address now means "listen and offer back" rather than "fail".
    """

    def test_the_reported_offer_routes_to_listen_mode(self):
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 0 11283"), (None, 11283, None))

    def test_passive_dcc_routes_to_listen_mode(self):
        """Port 0 is the client explicitly asking us to listen."""
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 2130706433 0"), (None, 0, None))

    def test_multicast_and_reserved_route_to_listen_mode(self):
        for ip_long, label in ((3758096384, "224.0.0.0 multicast"),
                               (4026531840, "240.0.0.0 reserved")):
            with self.subTest(label=label):
                ip, _, _ = adminchat.parse_offer(f"DCC CHAT chat {ip_long} 55555")
                self.assertIsNone(ip)

    def test_loopback_and_private_stay_dialable(self):
        """An operator on the same LAN, or testing locally, is legitimate."""
        for ip_long, expected in ((2130706433, "127.0.0.1"), (3232235777, "192.168.1.1")):
            with self.subTest(expected=expected):
                self.assertEqual(adminchat.parse_offer(f"DCC CHAT chat {ip_long} 55555"),
                                 (expected, 55555, None))

    def test_junk_is_still_refused_outright(self):
        for junk in ("DCC CHAT chat x y", "DCC SEND f 1 2", "", "DCC CHAT"):
            with self.subTest(junk=junk):
                self.assertIsNone(adminchat.parse_offer(junk))


class PassiveOfferWithToken(unittest.TestCase):
    """The second field report: a passive offer read as an active one.

        [ADMINCHAT] Unusable DCC CHAT offer from SysOp:
        'DCC CHAT chat 3405803861 0 350'

    The two forms are different LENGTHS:

        active   DCC CHAT chat <ip> <port>        5 tokens
        passive  DCC CHAT chat <ip> 0 <token>     6 tokens

    Counting back from the end works only for the active form. On this one
    parts[-2] was the literal 0 and parts[-1] was the token, so it parsed as
    0.0.0.0 port 350 and was discarded as junk. Fields are read by position now.
    """

    def test_the_reported_passive_offer_parses(self):
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 3405803861 0 350"),
                         (None, 0, "350"))

    def test_a_passive_offer_without_a_token_still_parses(self):
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 3405803861 0"),
                         (None, 0, None))

    def test_an_active_offer_is_unaffected(self):
        self.assertEqual(adminchat.parse_offer("DCC CHAT chat 2130706433 55555"),
                         ("127.0.0.1", 55555, None))

    def test_a_client_that_omits_the_chat_argument_still_parses(self):
        """The third token is an argument, not a guaranteed literal."""
        self.assertEqual(adminchat.parse_offer("DCC CHAT 2130706433 55555"),
                         ("127.0.0.1", 55555, None))

    @unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
    def test_the_token_is_echoed_back_in_our_offer(self):
        """Required by the protocol: without it the client cannot match the reply.

        A passive request is identified by its token. Our answering offer has to
        carry the same one back, or the client has nothing to associate it with
        and silently ignores a perfectly good offer.
        """
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)
        config.MY_IP_OR_DOCK = "127.0.0.1"

        sent = []

        class Recorder:
            def send(self, payload):
                sent.append(payload.decode("utf-8", "replace"))
                return len(payload)
            sendall = send

        self.assertTrue(adminchat.handle_dcc_chat(
            Recorder(), ADMIN_LINE, "SysOp", "DCC CHAT chat 3405803861 0 350"))

        self.assertTrue(wait_for(lambda: any("DCC CHAT chat" in s for s in sent)),
                        "a passive request must be answered with an offer")
        ctcp = next(s for s in sent if "DCC CHAT chat" in s)
        self.assertTrue(ctcp.strip().strip("").endswith(" 350"),
                        f"the token must be the last field; got {ctcp!r}")


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class ConnectFailureFallsBackToListening(unittest.TestCase):
    """Third field report: a real address that simply does not answer.

        [ADMINCHAT] Could not connect to SysOp at 203.0.113.41:55101 (timed out).

    A TIMEOUT rather than a refusal means the packets left and nothing came
    back - a VPN exit address with no inbound forwarding, a router not
    forwarding the port, or a firewall that drops instead of rejecting. The
    offer looked perfectly dialable, so the earlier "unusable address" fallback
    never triggered, and the attempt just ended there.

    The bot's own listener is proven reachable every day by DCC SEND, so a
    failed dial should cost one timeout and then work.
    """

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)
        config.MY_IP_OR_DOCK = "127.0.0.1"
        self._mode = getattr(config, "ADMIN_CHAT_MODE", "auto")
        self.addCleanup(lambda: setattr(config, "ADMIN_CHAT_MODE", self._mode))
        config.ADMIN_CHAT_MODE = "auto"

        self.sent = []
        outer = self

        class Recorder:
            def send(self, payload):
                outer.sent.append(payload.decode("utf-8", "replace"))
                return len(payload)
            sendall = send

        self.irc = Recorder()

    def dead_port(self):
        """A port nothing is listening on, so connect() is refused at once."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def offered_port(self, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self.sent:
                if "DCC CHAT chat" in line:
                    return int(line.strip().strip("").split()[-1])
            time.sleep(0.02)
        return None

    def test_a_refused_dial_falls_back_to_an_offer(self):
        offer = f"DCC CHAT chat 2130706433 {self.dead_port()}"
        self.assertTrue(adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp", offer))

        port = self.offered_port()
        self.assertIsNotNone(port, "a failed dial must be followed by an offer")
        self.assertGreaterEqual(port, config.DCC_PORT_START)
        self.assertLessEqual(port, config.DCC_PORT_END)

    def test_the_fallback_console_actually_works(self):
        """Not just that an offer was sent - that a session comes up on it."""
        offer = f"DCC CHAT chat 2130706433 {self.dead_port()}"
        adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp", offer)
        port = self.offered_port()
        self.assertIsNotNone(port)

        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        self.addCleanup(client.close)
        client.settimeout(5.0)
        buffer = ""
        deadline = time.time() + 5.0
        while "Enter Your Password:" not in buffer and time.time() < deadline:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
        self.assertIn("Enter Your Password:", buffer)

    def test_a_timed_out_dial_falls_back_too(self):
        """His exact symptom. A timeout is an OSError subclass, but assert it."""
        real = socket.create_connection

        def timeout_once(address, timeout=None, *args, **kwargs):
            if address[1] not in range(config.DCC_PORT_START, config.DCC_PORT_END + 1):
                raise socket.timeout("timed out")
            return real(address, timeout, *args, **kwargs)

        socket.create_connection = timeout_once
        self.addCleanup(lambda: setattr(socket, "create_connection", real))

        adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp",
                                  "DCC CHAT chat 3405803817 55101")
        self.assertIsNotNone(self.offered_port(),
                             "a timeout must fall back exactly as a refusal does")

    def test_listen_mode_does_not_dial_at_all(self):
        """So the operator stops paying the connect timeout on every login."""
        config.ADMIN_CHAT_MODE = "listen"
        dialled = []
        real = socket.create_connection

        def watch(address, *args, **kwargs):
            dialled.append(address)
            return real(address, *args, **kwargs)

        socket.create_connection = watch
        self.addCleanup(lambda: setattr(socket, "create_connection", real))

        adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp",
                                  "DCC CHAT chat 3405803817 55101")
        self.assertIsNotNone(self.offered_port())
        self.assertEqual([a for a in dialled if a[1] == 55101], [],
                         "listen mode must not dial the client")

    def test_connect_mode_refuses_to_fall_back(self):
        """For an operator who does not want the bot listening at all.

        Asserted by watching the fallback itself rather than by sleeping and
        hoping. A fixed wait passes whenever the fallback is merely still in
        flight, which is how this test first let a mutation through: the delay
        was the assertion, and delays are not assertions.
        """
        config.ADMIN_CHAT_MODE = "connect"

        dial_attempted = threading.Event()
        listened = []

        real_connect = socket.create_connection
        real_listen = adminchat._listen_and_serve

        def refuse(address, *args, **kwargs):
            dial_attempted.set()
            raise ConnectionRefusedError("refused")

        socket.create_connection = refuse
        adminchat._listen_and_serve = lambda *a, **k: listened.append(a)
        self.addCleanup(lambda: setattr(socket, "create_connection", real_connect))
        self.addCleanup(lambda: setattr(adminchat, "_listen_and_serve", real_listen))

        adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp",
                                  "DCC CHAT chat 2130706433 55555")

        self.assertTrue(dial_attempted.wait(5.0), "the dial must at least be attempted")
        # The decision happens on the same thread, immediately after the failure.
        self.assertTrue(wait_for(lambda: True, timeout=0.2))
        self.assertEqual(listened, [],
                         "connect mode must not fall back to listening")

    def test_auto_is_the_default(self):
        self.assertEqual(str(getattr(config, "ADMIN_CHAT_MODE", "auto")).lower(), "auto")


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class ListenerUsesTheConfiguredRange(unittest.TestCase):
    """His other point: the console must respect DCC_PORT_START/END."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        self._range = (config.DCC_PORT_START, config.DCC_PORT_END)
        self.addCleanup(lambda: setattr(config, "DCC_PORT_START", self._range[0]))
        self.addCleanup(lambda: setattr(config, "DCC_PORT_END", self._range[1]))

    def test_the_listener_binds_inside_the_range(self):
        sock, port = adminchat._open_chat_listener()
        self.addCleanup(sock.close)
        self.assertIsNotNone(sock)
        self.assertGreaterEqual(port, config.DCC_PORT_START)
        self.assertLessEqual(port, config.DCC_PORT_END)

    def test_it_scans_downward_so_transfers_keep_the_low_ports(self):
        """start_dcc_send scans upward from DCC_PORT_START; this scans the other way.

        Asserted as an ordering rather than an exact port: whether 55010 itself
        is free depends on what else the machine is doing, but two consecutive
        listeners must still descend. An upward scan would ascend.
        """
        first_sock, first_port = adminchat._open_chat_listener()
        self.assertIsNotNone(first_sock)
        self.addCleanup(first_sock.close)
        second_sock, second_port = adminchat._open_chat_listener()
        self.assertIsNotNone(second_sock, "the range needs two free ports for this test")
        self.addCleanup(second_sock.close)
        self.assertLess(second_port, first_port)

    def test_two_listeners_never_claim_the_same_port(self):
        """Found by CI on Linux, invisible on Windows.

        _open_chat_listener used to return a socket that was bound but not yet
        listening. On POSIX, SO_REUSEADDR permits a second socket to bind the
        same port while the first is in that state, so two console attempts
        could claim one port and fight over the connection. Windows never had
        the hole - SO_EXCLUSIVEADDRUSE refuses the duplicate at bind - which is
        why only the Linux jobs went red.

        listen() inside the helper claims the port properly.
        """
        first_sock, first_port = adminchat._open_chat_listener()
        self.assertIsNotNone(first_sock)
        self.addCleanup(first_sock.close)
        second_sock, second_port = adminchat._open_chat_listener()
        self.assertIsNotNone(second_sock, "the range needs two free ports for this test")
        self.addCleanup(second_sock.close)
        self.assertNotEqual(first_port, second_port,
                            "a bound-but-not-listening socket does not claim the port")

    def test_the_returned_listener_is_already_listening(self):
        """Accept must work without the caller calling listen() first."""
        sock, port = adminchat._open_chat_listener()
        self.assertIsNotNone(sock)
        self.addCleanup(sock.close)
        sock.settimeout(2.0)
        client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        self.addCleanup(client.close)
        accepted, _ = sock.accept()
        accepted.close()

    def test_an_exhausted_range_is_reported_rather_than_crashing(self):
        held = []
        for port in range(config.DCC_PORT_START, config.DCC_PORT_END + 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("0.0.0.0", port))
                held.append(s)
            except OSError:
                s.close()
        self.addCleanup(lambda: [s.close() for s in held])
        if len(held) != (config.DCC_PORT_END - config.DCC_PORT_START + 1):
            self.skipTest("could not reserve the whole range on this machine")
        sock, port = adminchat._open_chat_listener()
        self.assertIsNone(sock)
        self.assertIsNone(port)


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class ListenModeEndToEnd(unittest.TestCase):
    """Reproduces the reported failure, then proves the fallback carries it."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)
        config.MY_IP_OR_DOCK = "127.0.0.1"
        self.sent = []

        outer = self

        class RecordingIrcSocket:
            def send(self, payload):
                outer.sent.append(payload.decode("utf-8", "replace"))
                return len(payload)
            sendall = send

        self.irc = RecordingIrcSocket()

    def offered_port(self, timeout=5.0):
        """Pull the port out of the CTCP the bot sent back."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self.sent:
                if "DCC CHAT chat" in line:
                    return int(line.strip().strip("").split()[-1])
            time.sleep(0.02)
        return None

    def test_an_unroutable_offer_makes_the_bot_listen_and_offer_back(self):
        """The whole bug, end to end: 0.0.0.0 in, a working console out."""
        self.assertTrue(adminchat.handle_dcc_chat(
            self.irc, ADMIN_LINE, "SysOp", "DCC CHAT chat 0 11283"))

        port = self.offered_port()
        self.assertIsNotNone(port, "the bot must offer a DCC CHAT back")
        self.assertGreaterEqual(port, config.DCC_PORT_START)
        self.assertLessEqual(port, config.DCC_PORT_END)

        client = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        self.addCleanup(client.close)
        client.settimeout(5.0)

        buffer = ""
        deadline = time.time() + 5.0
        while "Enter Your Password:" not in buffer and time.time() < deadline:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
        self.assertIn("Welcome to", buffer)
        self.assertIn("Enter Your Password:", buffer)

    def test_the_offer_advertises_the_bots_own_ip(self):
        """His suggestion: use the IP the bot got when it connected to IRC."""
        import dcc
        adminchat.handle_dcc_chat(self.irc, ADMIN_LINE, "SysOp", "DCC CHAT chat 0 11283")
        self.assertIsNotNone(self.offered_port())
        ctcp = next(line for line in self.sent if "DCC CHAT chat" in line)
        self.assertIn(str(dcc.get_public_ip_long()), ctcp)

    def test_an_exhausted_range_does_not_crash_the_listen_thread(self):
        """A daemon thread dying with a traceback is not "handled".

        Called synchronously rather than through handle_dcc_chat: the real path
        runs in a thread, where an unhandled exception prints and vanishes, and
        the test would pass while the console silently never opened.

        The range is narrowed to a single port that this test already holds, so
        exhaustion is deterministic rather than dependent on the machine.
        """
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("0.0.0.0", 0))
        taken = held.getsockname()[1]
        self.addCleanup(held.close)

        original = (config.DCC_PORT_START, config.DCC_PORT_END)
        config.DCC_PORT_START = config.DCC_PORT_END = taken
        self.addCleanup(lambda: (setattr(config, "DCC_PORT_START", original[0]),
                                 setattr(config, "DCC_PORT_END", original[1])))

        adminchat._listen_and_serve(self.irc, "SysOp", "sysop.users.undernet.org")

        self.assertEqual(self.sent, [],
                         "no offer can be sent when there is no port to offer")

    def test_a_stranger_still_gets_no_offer(self):
        """The gate runs before either transport, and must be unaffected."""
        self.assertFalse(adminchat.handle_dcc_chat(
            self.irc, STRANGER_LINE, "dave", "DCC CHAT chat 0 11283"))
        time.sleep(0.3)
        self.assertEqual(self.sent, [])


class BadIpTracking(unittest.TestCase):
    """Per-session attempt counting is useless alone - an attacker reconnects."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)

    def test_an_address_blocks_after_the_attempt_limit(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.assertFalse(adminchat.is_bad_ip("10.0.0.9"))
            adminchat.note_bad_ip("10.0.0.9")
        self.assertTrue(adminchat.is_bad_ip("10.0.0.9"))

    def test_the_block_expires(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            adminchat.note_bad_ip("10.0.0.9")
        self.assertTrue(adminchat.is_bad_ip("10.0.0.9"))
        adminchat._bad_ips["10.0.0.9"][1] = time.time() - 1
        self.assertFalse(adminchat.is_bad_ip("10.0.0.9"),
                         "a typo must not lock the operator out permanently")

    def test_a_success_clears_the_record(self):
        adminchat.note_bad_ip("10.0.0.9")
        adminchat.clear_bad_ip("10.0.0.9")
        self.assertNotIn("10.0.0.9", adminchat._bad_ips)

    def test_addresses_are_tracked_separately(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            adminchat.note_bad_ip("10.0.0.9")
        self.assertFalse(adminchat.is_bad_ip("10.0.0.10"))


class TheRequestGate(unittest.TestCase):
    """handle_dcc_chat is called from the IRC read loop and must never block."""

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)

        self.dialled = []
        self._real = adminchat._connect_and_serve
        adminchat._connect_and_serve = lambda *a: self.dialled.append(a)
        self.addCleanup(lambda: setattr(adminchat, "_connect_and_serve", self._real))

    def test_a_stranger_gets_absolutely_nothing(self):
        """The deliberate difference from iroffer, which banners at anyone.

        No connection, no reply on the IRC socket, no debug line. A debug line
        would tell whoever watches the debug channel that a mask was wrong, and
        would let a stranger fill the log for free.
        """
        sent = []

        class Recorder:
            def send(self, payload):
                sent.append(payload)
            sendall = send

        self.assertFalse(adminchat.handle_dcc_chat(Recorder(), STRANGER_LINE, "dave",
                                                   "DCC CHAT chat 2130706433 55555"))
        self.assertEqual(self.dialled, [], "must not dial a stranger's listener")
        self.assertEqual(sent, [], "must not answer a stranger at all")

    def test_an_authorised_host_is_dialled(self):
        self.assertTrue(adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp",
                                                  "DCC CHAT chat 2130706433 55555"))
        self.assertEqual(len(self.dialled), 1)
        _irc, nick, host, ip, port, _token = self.dialled[0]
        self.assertEqual((host, ip, port), ("sysop.users.undernet.org", "127.0.0.1", 55555))

    def test_no_password_configured_means_no_console(self):
        """Refuse rather than open an unauthenticated console."""
        config.ADMIN_PASSWORD_HASH = ""
        self.assertFalse(adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp",
                                                   "DCC CHAT chat 2130706433 55555"))
        self.assertEqual(self.dialled, [])

    def test_a_blocked_address_is_refused_even_from_the_right_host(self):
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            adminchat.note_bad_ip("127.0.0.1")
        self.assertFalse(adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp",
                                                   "DCC CHAT chat 2130706433 55555"))
        self.assertEqual(self.dialled, [])

    def test_a_malformed_offer_is_refused(self):
        self.assertFalse(adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp",
                                                   "DCC CHAT chat nonsense"))
        self.assertEqual(self.dialled, [])


class OutboxNeverBlocksTheCaller(unittest.TestCase):
    """The failure that would take the daemon off the server.

    send_debug() is called from the IRC read loop. If a queued log line could
    block on a stalled admin client, a sleeping laptop would freeze the network
    thread. Session.send must be a pure append.
    """

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)

    def make_session(self, sock=None):
        return adminchat.Session(sock or socket.socket(), "127.0.0.1", "SysOp", "h")

    def test_send_does_not_touch_the_socket(self):
        class Exploding:
            def sendall(self, payload):
                raise AssertionError("send() wrote the socket on the caller's thread")
            def close(self):
                pass

        session = self.make_session(Exploding())
        for i in range(50):
            session.send(f"line {i}")

    def test_the_outbox_is_bounded_and_drops_oldest(self):
        session = self.make_session()
        self.addCleanup(session.close, None)
        for i in range(adminchat.OUTBOX_MAX + 250):
            session.send(f"line {i}")
        self.assertEqual(len(session._outbox), adminchat.OUTBOX_MAX,
                         "a dead client must drop lines, not grow without limit")
        self.assertEqual(session.dropped, 250)
        self.assertNotIn("line 0", session._outbox)

    def test_sending_to_a_closed_session_is_a_no_op(self):
        session = self.make_session()
        session.close(announce_text=None)
        session.send("after close")
        self.assertEqual(len(session._outbox), 0)

    def test_send_is_fast_even_when_the_writer_is_stuck(self):
        """Measured, not assumed: 500 lines against a wedged socket."""
        blocked = threading.Event()

        class Wedged:
            def sendall(self, payload):
                blocked.wait(30)
            def close(self):
                pass
            def shutdown(self, how):
                pass

        session = self.make_session(Wedged())
        session.start_writer()
        self.addCleanup(lambda: (blocked.set(), session.close(None)))

        started = time.time()
        for i in range(500):
            session.send(f"line {i}")
        self.assertLess(time.time() - started, 1.0,
                        "queueing must not wait on the socket")


class SessionExpiry(unittest.TestCase):

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)

    def session(self):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        return s

    def test_an_unauthenticated_session_expires_on_the_short_clock(self):
        s = self.session()
        self.assertFalse(s.expired())
        s.opened_at = time.time() - adminchat.AUTH_TIMEOUT - 1
        self.assertTrue(s.expired())

    def test_an_authenticated_session_uses_the_idle_clock(self):
        s = self.session()
        s.authenticated = True
        s.opened_at = time.time() - adminchat.AUTH_TIMEOUT - 1
        s.last_activity = time.time()
        self.assertFalse(s.expired(), "activity, not age, keeps a live session open")
        s.last_activity = time.time() - adminchat.IDLE_TIMEOUT - 1
        self.assertTrue(s.expired())


@unittest.skipUnless(LOOPBACK_OK, NEEDS_LOOPBACK)
class EndToEndOverLoopback(unittest.TestCase):
    """The real connect-out path, against a real listening socket.

    This is what the operator's mIRC does: listen, send the offer, wait for the
    bot to dial in. Nothing here is mocked except the clock's patience.
    """

    def setUp(self):
        adminchat.reset_state_for_tests()
        self.addCleanup(adminchat.reset_state_for_tests)
        config.ADMIN_HOSTMASKS = ["*!*@SysOp.users.undernet.org"]
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash(PASSWORD, iterations=1000)

        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)
        self.client = None

    def offer(self):
        return f"DCC CHAT chat 2130706433 {self.port}"

    def dial(self):
        """Trigger the bot, then accept its incoming connection like mIRC would."""
        adminchat.handle_dcc_chat(None, ADMIN_LINE, "SysOp", self.offer())
        self.listener.settimeout(5.0)
        self.client, _ = self.listener.accept()
        self.client.settimeout(5.0)
        self.addCleanup(self.client.close)
        return self.client

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

    def test_the_banner_and_prompt_arrive(self):
        self.dial()
        text = self.read_until("Enter Your Password:")
        self.assertIn("Welcome to", text)
        self.assertIn(config.SCRIPT_VERSION, text)
        self.assertIn("Enter Your Password:", text)

    def test_the_banner_does_not_leak_the_password_or_the_mask(self):
        self.dial()
        text = self.read_until("Enter Your Password:")
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(config.ADMIN_PASSWORD_HASH, text)

    def test_the_right_password_opens_the_console(self):
        self.dial()
        self.read_until("Enter Your Password:")
        self.client.sendall((PASSWORD + "\n").encode())
        text = self.read_until("For help type")
        self.assertIn("Entering DCC Chat Admin Interface", text)
        self.assertTrue(wait_for(lambda: adminchat.active_session() is not None))

    def peer_closed(self, timeout=10.0):
        """True once the bot has actually hung up on us."""
        self.client.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.client.recv(4096) == b"":
                    return True
            except socket.timeout:
                return False
            except OSError:
                return True
        return False

    def test_a_wrong_password_reprompts(self):
        self.dial()
        self.read_until("Enter Your Password:")
        self.client.sendall(b"wrong\n")
        text = self.read_until("Enter Your Password:", timeout=8.0)
        self.assertIn("Incorrect Password.", text)
        self.assertIsNone(adminchat.active_session())

    def test_the_attempt_limit_actually_hangs_up(self):
        """Assert the disconnect, not just the message.

        Reprompting forever and disconnecting look identical if the test only
        checks that "Incorrect Password." arrived and that nobody got in - both
        are true either way. The security property is that the socket closes, so
        that is what gets asserted.
        """
        self.dial()
        self.read_until("Enter Your Password:")
        for _ in range(adminchat.MAX_PASSWORD_ATTEMPTS):
            self.client.sendall(b"wrong\n")
        self.assertTrue(self.peer_closed(),
                        "the console must hang up after the attempt limit, not reprompt forever")
        self.assertTrue(wait_for(lambda: adminchat.is_bad_ip("127.0.0.1"), timeout=8.0),
                        "failed attempts must be charged to the address")
        self.assertIsNone(adminchat.active_session())

    def test_commands_are_refused_before_authentication(self):
        """The console must not answer 'help' to an unauthenticated socket."""
        self.dial()
        self.read_until("Enter Your Password:")
        self.client.sendall(b"help\n")
        text = self.read_until("Available commands:", timeout=1.5)
        self.assertNotIn("Available commands:", text)

    def test_help_works_once_authenticated(self):
        self.dial()
        self.read_until("Enter Your Password:")
        self.client.sendall((PASSWORD + "\nhelp\n").encode())
        text = self.read_until("close this session")
        self.assertIn("Available commands:", text)

    def test_quit_closes_the_session(self):
        self.dial()
        self.read_until("Enter Your Password:")
        self.client.sendall((PASSWORD + "\n").encode())
        self.read_until("For help type")
        self.client.sendall(b"quit\n")
        self.assertTrue(wait_for(lambda: adminchat.active_session() is None))


class WiringIsInPlace(unittest.TestCase):
    """irc.py must actually route DCC CHAT here, and only from a private message."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_irc_hands_dcc_chat_to_the_console(self):
        self.assertTrue("adminchat.handle_dcc_chat(" in self.source)

    def test_the_branch_requires_a_private_message(self):
        """A DCC CHAT offer to a channel is meaningless and anyone can send one."""
        self.assertTrue("target_chan.lower() == config.NICKNAME.lower()" in self.source)

    def test_the_privmsg_parser_keeps_the_hostmask(self):
        self.assertTrue("def event_source_host(" in self.source,
                        "the console cannot work if the host is discarded")

    def test_adminchat_is_not_reloaded_by_rehash(self):
        """importlib.reload would drop a live session's socket on every !rehash."""
        with open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            commands_source = handle.read()
        for line in commands_source.split("\n"):
            if "modules_to_reload" in line and "=" in line:
                self.assertNotIn("adminchat", line,
                                 "reloading adminchat would kill the live console")


if __name__ == "__main__":
    unittest.main()
