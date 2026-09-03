"""A DCC offer carrying an address nobody can dial is worse than a refusal.

Found by the pre-publication audit, reproduced end to end. irc.py's connect
fetched the public address from api.ipify.org and, on any failure, fell back to
"127.0.0.1". The only guard on the send path was `if ip_long == 0` - and
127.0.0.1 converts to 2130706433, which is not 0. So nothing caught it.

The bot then connected, joined, advertised normally, accepted every request,
sent each user "Active Transfer Started", and held a DCC slot per request,
while every leecher's client dialled its own loopback and received nothing.
A single boot warning was the only clue; the queue drained into failed
transfers and the retry budget.

THE SHAPE OF THE FIX MATTERS, and the first attempt got it wrong. Folding the
"is this address any use?" question into get_public_ip_long() broke sixteen
tests, all in the admin console and the passive-fetch paths - because those use
the SAME value and a loopback address is entirely correct for them: an operator
opening a DCC CHAT from the same machine, or a LAN. So the converter stays a
converter, and the stricter rule is a separate predicate used only where offers
go to strangers. The classes below pin both halves, including that regression.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import irc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402

# 8.8.8.8 rather than the usual 203.0.113.x: TEST-NET is classed as private by
# the stdlib, which is correct and is exactly what this predicate refuses.
ROUTABLE = "8.8.8.8"

UNDIALABLE = [
    ("127.0.0.1", "loopback - what the failed lookup used to fall back to"),
    ("192.168.1.50", "private"),
    ("10.0.0.4", "private"),
    ("169.254.7.7", "link-local"),
    ("224.0.0.1", "multicast"),
    ("0.0.0.0", "unspecified"),
    ("203.0.113.7", "documentation range"),
    ("", "blank - the lookup failed and invented nothing"),
    ("not-an-ip", "malformed"),
]


class TheConverterStaysAConverter(DCCoreTestCase):
    """get_public_ip_long() is shared with adminchat.py's DCC CHAT listen-back,
    where loopback and LAN addresses are legitimate. It must not editorialise."""

    def test_loopback_still_converts(self):
        self.set_config(MY_IP_OR_DOCK="127.0.0.1")

        self.assertEqual(dcc.get_public_ip_long(), 2130706433)

    def test_a_private_address_still_converts(self):
        self.set_config(MY_IP_OR_DOCK="192.168.1.50")

        self.assertEqual(dcc.get_public_ip_long(), 3232235826)

    def test_only_blank_and_malformed_give_zero(self):
        for value in ("", "   ", "not-an-ip", "1.2.3"):
            with self.subTest(value=value):
                self.set_config(MY_IP_OR_DOCK=value)

                self.assertEqual(dcc.get_public_ip_long(), 0)


class OnlyADialableAddressMayBeOffered(DCCoreTestCase):

    def test_every_undialable_form_is_refused(self):
        for value, why in UNDIALABLE:
            with self.subTest(address=value or "(blank)", why=why):
                self.set_config(MY_IP_OR_DOCK=value)

                self.assertFalse(dcc.is_offerable_to_strangers())

    def test_a_routable_address_is_accepted(self):
        """The control. A predicate that refused everything would pass every
        test above and stop the bot serving anyone."""
        self.set_config(MY_IP_OR_DOCK=ROUTABLE)

        self.assertTrue(dcc.is_offerable_to_strangers())

    def test_it_can_be_asked_about_an_address_directly(self):
        self.assertTrue(dcc.is_offerable_to_strangers(ROUTABLE))
        self.assertFalse(dcc.is_offerable_to_strangers("127.0.0.1"))


class TheSendRefusesRatherThanOfferingLoopback(DCCoreTestCase):
    """Driven through the real start_dcc_send(). Its address check sits well
    before any socket is bound, so this touches no network."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.track = os.path.join(self.tree.root, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)

    def send(self, address):
        self.set_config(MY_IP_OR_DOCK=address)
        sock = RecordingSocket()
        dcc.start_dcc_send(sock, "dave", self.track, "Song.flac", "#chan",
                           {"file": "Song.flac", "path": self.track})
        return sock.text()

    def test_a_loopback_address_produces_a_refusal_not_an_offer(self):
        out = self.send("127.0.0.1")

        self.assertNotIn("DCC SEND", out,
                         "an offer was made carrying an address nobody can dial")

    def test_the_refusal_says_it_is_the_address(self):
        """The old message was "File access issue or empty payload. Please try
        again." - wrong on every count here: the file is fine, retrying cannot
        help, and only the operator can fix it."""
        out = self.send("127.0.0.1").lower()

        self.assertIn("address", out)
        self.assertNotIn("try again", out)

    def test_a_routable_address_gets_past_the_address_check(self):
        """The control: too strict a predicate would refuse everyone.

        Proven by making the NEXT step fail instead: this test holds the only
        port in the configured range open itself, so a routable address reaches
        "No available DCC ports" and stops there, while an undialable one never
        gets that far. Letting it proceed to a real handshake proves the same
        thing but then blocks on a 30-second accept() in every suite run, and a
        privileged port number is not reliably unbindable (port 1 binds fine
        here), so the port is occupied deliberately rather than assumed busy."""
        import socket
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        self.addCleanup(held.close)
        busy = held.getsockname()[1]
        self.set_config(DCC_PORT_START=busy, DCC_PORT_END=busy)
        out = self.send(ROUTABLE).lower()

        self.assertIn("no available dcc ports", out,
                      "the address check refused a perfectly dialable address")
        self.assertNotIn("no usable public address", out)

    def test_an_empty_file_still_reports_the_file_problem(self):
        """The other branch of the same guard must keep its own message."""
        empty = os.path.join(self.tree.root, "Empty.flac")
        io.open(empty, "w").close()
        self.set_config(MY_IP_OR_DOCK=ROUTABLE)
        sock = RecordingSocket()

        dcc.start_dcc_send(sock, "dave", empty, "Empty.flac", "#chan",
                           {"file": "Empty.flac", "path": empty})

        self.assertIn("try again", sock.text().lower())


class TheConnectNeverInventsAnAddress(unittest.TestCase):
    """irc.py's half. The fallback is the origin of the whole defect."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_no_loopback_literal_is_assigned_to_the_dcc_address(self):
        """Parsed rather than grepped: the string "127.0.0.1" legitimately
        appears in irc.py's comments describing this very bug, so a substring
        search would either miss the assignment or trip on the explanation."""
        tree = ast.parse(self.source())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr == "MY_IP_OR_DOCK"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value):
                    offenders.append(f"irc.py:{node.lineno} -> {node.value.value!r}")

        self.assertEqual(offenders, [],
                         "the connect assigns a literal address on failure: "
                         + "; ".join(offenders))

    def test_the_operator_is_told_what_to_set(self):
        self.assertIn("MY_IP_OR_DOCK", self.source())


class ResolvingTheAddressIsDrivenNotGrepped(DCCoreTestCase):
    """irc.resolve_dcc_address() was lifted out of irc_loop() for this: while it
    was inline the only available assertion was "the word 'pinned' appears in
    irc.py", which passes against `if False:` with the word still in the file.
    A mutation run caught exactly that, so both branches are exercised here."""

    def test_a_pinned_address_is_returned_untouched(self):
        """docs/WINDOWS.md documents pinning MY_IP_OR_DOCK for a static address
        or a forwarded router. The lookup used to run unconditionally and
        overwrite it, so the documented setting silently did nothing."""
        self.set_config(MY_IP_OR_DOCK="198.51.100.9")
        called = []

        found = irc.resolve_dcc_address(lookup=lambda: called.append(1) or "1.1.1.1",
                                        log=lambda *_: None)

        self.assertEqual(found, "198.51.100.9")
        self.assertEqual(called, [], "the lookup ran despite a pinned address")

    def test_an_unpinned_address_is_looked_up(self):
        """The control: respecting a pinned value must not disable detection
        for the operators who rely on it."""
        self.set_config(MY_IP_OR_DOCK="")

        found = irc.resolve_dcc_address(lookup=lambda: "8.8.8.8", log=lambda *_: None)

        self.assertEqual(found, "8.8.8.8")

    def test_a_failed_lookup_returns_blank_not_loopback(self):
        """The origin of the whole defect: it used to return "127.0.0.1"."""
        self.set_config(MY_IP_OR_DOCK="")

        def boom():
            raise OSError("no route to host")

        found = irc.resolve_dcc_address(lookup=boom, log=lambda *_: None)

        self.assertEqual(found, "")
        self.assertFalse(dcc.is_offerable_to_strangers(found))

    def test_a_failed_lookup_says_which_setting_fixes_it(self):
        self.set_config(MY_IP_OR_DOCK="")
        lines = []

        def boom():
            raise OSError("no route to host")

        irc.resolve_dcc_address(lookup=boom, log=lines.append)

        self.assertIn("MY_IP_OR_DOCK", " ".join(lines))

    def test_whitespace_only_is_not_treated_as_pinned(self):
        self.set_config(MY_IP_OR_DOCK="   ")

        found = irc.resolve_dcc_address(lookup=lambda: "8.8.8.8", log=lambda *_: None)

        self.assertEqual(found, "8.8.8.8")


if __name__ == "__main__":
    unittest.main()
