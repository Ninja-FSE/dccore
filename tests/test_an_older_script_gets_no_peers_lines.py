"""An older dccore.mrc is not sent PEERS or CONSOLEFEED lines (#1045).

The 1.5 script that v1.13.1 shipped has no branch for either and prints a
line type it does not know as text. MIN_SCRIPT_VERSION still accepts it, so
an operator who upgraded the bot but kept that script saw "[CONSOLEFEED] on"
and a "[PEERS] ..." line per channel on every connect, and another on every
WHO round and every peer that joined or left. FETCHING, REBUILD and the
Downloads rows were already held back from a script too old to draw them;
these two now are as well.
"""

import socket
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import serverschat  # noqa: E402

CHAN = "#examplechan"


class FakeSession:
    def __init__(self):
        self.authenticated = True
        self.structured = False
        self.closed = False
        self.nick = "SomeOperator"
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def send_status(self):
        pass


class TheVersion(unittest.TestCase):
    def test_1_7_and_later_read_them(self):
        for version in ("1.7", "1.10", "1.10.5", "2.0"):
            self.assertTrue(adminchat.script_reads_peers(version), version)

    def test_older_or_unknown_do_not(self):
        for version in ("1.5", "1.6", "", "nonsense"):
            self.assertFalse(adminchat.script_reads_peers(version), version)


class Hello(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="OurBot", DEBUG_TO_CONSOLE=True)
        config.channel_users[CHAN] = {"ourbot", "someoperator"}
        channels = serverschat.channels
        serverschat.channels = lambda: [CHAN]
        self.addCleanup(setattr, serverschat, "channels", channels)

    def hello(self, version):
        session = FakeSession()
        adminchat._cmd_hello(session, f"dccore.mrc {version}")
        return session

    def kinds(self, session):
        return [line.split(" ", 2)[1] for line in session.sent if line.startswith("DCCORE ")]

    def test_a_1_5_script_gets_neither(self):
        kinds = self.kinds(self.hello("1.5"))
        self.assertNotIn("PEERS", kinds)
        self.assertNotIn("CONSOLEFEED", kinds)
        self.assertIn("CHANNELS", kinds, "the rest of hello still goes")

    def test_a_1_7_script_gets_both(self):
        kinds = self.kinds(self.hello("1.7"))
        self.assertIn("PEERS", kinds)
        self.assertIn("CONSOLEFEED", kinds)

    def test_an_older_script_still_hears_the_feed_is_off_in_prose(self):
        self.set_config(DEBUG_TO_CONSOLE=False)
        session = self.hello("1.5")
        self.assertTrue(any("console feed is off" in line.lower() for line in session.sent), session.sent)


class LaterPeersLines(support.DCCoreTestCase):
    """A WHO round, a peer that joins or leaves: all go out by _deliver_peers()."""

    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.session.structured = True
        active = adminchat.active_session
        adminchat.active_session = lambda: self.session
        self.addCleanup(setattr, adminchat, "active_session", active)

    def test_an_older_script_is_not_sent_one(self):
        self.session.reads_peers = False
        serverschat._deliver_peers(CHAN)
        self.assertEqual(self.session.sent, [])

    def test_a_script_that_reads_them_is(self):
        self.session.reads_peers = True
        serverschat._deliver_peers(CHAN)
        self.assertEqual(len(self.session.sent), 1)
        self.assertTrue(self.session.sent[0].startswith(f"DCCORE PEERS {CHAN}"))

    def test_a_real_session_reads_none_until_hello_says_so(self):
        sock = socket.socket()
        self.addCleanup(sock.close)
        self.assertFalse(adminchat.Session(sock, "127.0.0.1", "SomeOperator", "h").reads_peers)


if __name__ == "__main__":
    unittest.main()
