"""The bot's side of DCCore Chat's history limits (#1401).

dccore.mrc keeps the chat's history on the operator's PC and deletes whole
months by two limits that are the BOT's settings, CHAT_HISTORY_DAYS and
CHAT_HISTORY_MAX_MB, so the dashboard can set them. The bot sends them as
`DCCORE CHATHIST <days> <max_mb>`: after HELLO to a script that said 1.20 or
later, and again whenever either changes.

The script deletes files by these numbers, so the line must never carry a
value read halfway through a rehash's reload (a setting back at its shipped
default for a moment): an operator keeping everything (0) would be sent 100
days and lose every older month.
"""

import io
import json
import os
import socket
import threading
import time
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import serverschat  # noqa: E402
import settings_file  # noqa: E402
import web_settings  # noqa: E402

CHAN = "#examplechan"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def recv_until(sock, needle, seconds=5.0):
    sock.settimeout(0.2)
    buffer = b""
    deadline = time.time() + seconds
    while time.time() < deadline and needle not in buffer:
        try:
            buffer += sock.recv(65536)
        except socket.timeout:
            continue
    return buffer


class TheVersion(unittest.TestCase):
    def test_1_20_and_later_read_it(self):
        for version in ("1.20", "1.20.0", "1.21", "1.100", "2.0"):
            self.assertTrue(adminchat.script_reads_chathist(version), version)

    def test_older_or_unknown_do_not(self):
        # 1.9 is older than 1.20: the numbers are compared, not the text.
        for version in ("1.19", "1.19.9", "1.9", "1.2", "", "nonsense"):
            self.assertFalse(adminchat.script_reads_chathist(version), version)


class TheLine(support.DCCoreTestCase):
    def test_the_shipped_defaults(self):
        self.assertEqual(config.CHAT_HISTORY_DAYS, 100)
        self.assertEqual(config.CHAT_HISTORY_MAX_MB, 100)
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 100 100")

    def test_the_settings_in_order(self):
        self.set_config(CHAT_HISTORY_DAYS=0, CHAT_HISTORY_MAX_MB=250)
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 0 250")
        self.set_config(CHAT_HISTORY_DAYS=30, CHAT_HISTORY_MAX_MB=0)
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 30 0")

    def test_a_value_that_skipped_coerce_is_a_whole_number_of_0_or_more(self):
        """admin_config.py sets config directly; the script refuses anything
        but two whole numbers of 0 or more."""
        self.set_config(CHAT_HISTORY_DAYS=-5, CHAT_HISTORY_MAX_MB="lots")
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 0 0")
        self.set_config(CHAT_HISTORY_DAYS=45.7, CHAT_HISTORY_MAX_MB="20")
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 45 20")

    def test_nothing_while_a_rehash_holds_the_reload_lock(self):
        held, done = threading.Event(), threading.Event()

        def rehash():
            with runtime.config_reload_lock:
                held.set()
                done.wait(5)

        thread = threading.Thread(target=rehash, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(done.set)
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        self.assertIsNone(adminchat.chathist_line())
        self.assertLess(time.monotonic() - started, 1.0, "it must not wait for the reload")
        done.set()
        thread.join(5)
        self.assertEqual(adminchat.chathist_line(), "DCCORE CHATHIST 100 100")


class Hello(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="OurBot")
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

    def test_a_1_20_script_gets_it_after_hello_and_the_channels(self):
        self.set_config(CHAT_HISTORY_DAYS=60, CHAT_HISTORY_MAX_MB=40)
        session = self.hello("1.20.0")
        self.assertIn("DCCORE CHATHIST 60 40", session.sent)
        kinds = self.kinds(session)
        self.assertEqual(kinds.count("CHATHIST"), 1)
        self.assertLess(kinds.index("HELLO"), kinds.index("CHATHIST"))
        self.assertLess(kinds.index("CHANNELS"), kinds.index("CHATHIST"))

    def test_an_older_script_gets_none(self):
        session = self.hello("1.19.0")
        self.assertNotIn("CHATHIST", self.kinds(session))
        self.assertIn("CHANNELS", self.kinds(session), "the rest of hello still goes")

    def test_hello_again_sends_it_again(self):
        """A script that reconnects in the same session, or says hello twice,
        is told again - it may have lost dccore.ini in between."""
        session = self.hello("1.20")
        adminchat._cmd_hello(session, "dccore.mrc 1.20")
        self.assertEqual(self.kinds(session).count("CHATHIST"), 2)

    def test_hello_mid_rehash_sends_nothing_and_the_next_pass_sends_it(self):
        line = adminchat.chathist_line
        adminchat.chathist_line = lambda: None   # what it says while the lock is held
        self.addCleanup(setattr, adminchat, "chathist_line", line)
        session = self.hello("1.20")
        self.assertNotIn("CHATHIST", self.kinds(session))
        adminchat.chathist_line = line
        adminchat.send_chat_history_limits(session)
        self.assertIn("DCCORE CHATHIST 100 100", session.sent)


class AChange(support.DCCoreTestCase):
    """What the writer's half-second pass does: a line only when it changed."""

    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.session.reads_chathist = True
        adminchat.send_chat_history_limits(self.session)
        self.session.sent.clear()

    def test_nothing_when_nothing_changed(self):
        for _ in range(3):
            adminchat.send_chat_history_limits(self.session)
        self.assertEqual(self.session.sent, [])

    def test_a_change_goes_once(self):
        self.set_config(CHAT_HISTORY_DAYS=7)
        adminchat.send_chat_history_limits(self.session)
        adminchat.send_chat_history_limits(self.session)
        self.assertEqual(self.session.sent, ["DCCORE CHATHIST 7 100"])
        self.set_config(CHAT_HISTORY_MAX_MB=5)
        adminchat.send_chat_history_limits(self.session)
        self.assertEqual(self.session.sent[-1], "DCCORE CHATHIST 7 5")

    def test_never_to_a_script_that_does_not_read_it(self):
        self.session.reads_chathist = False
        self.set_config(CHAT_HISTORY_DAYS=7)
        adminchat.send_chat_history_limits(self.session)
        self.assertEqual(self.session.sent, [])


class TheWriter(support.DCCoreTestCase):
    """The real session writer sends a changed limit on its own."""

    def running_session(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "SomeOperator", "op.example")
        self.addCleanup(session.close, None)
        return session, far

    def test_a_new_session_reads_none_until_hello_says_so(self):
        session, _ = self.running_session()
        self.assertFalse(session.reads_chathist)
        self.assertIsNone(session._chathist_sent)

    def test_a_changed_setting_reaches_the_script_without_a_status_burst(self):
        session, far = self.running_session()
        session.authenticated = True
        session.structured = True
        session._status_sent_at = time.time() + 3600   # no timer burst in this test
        session.reads_chathist = True
        session._chathist_sent = adminchat.chathist_line()
        self.set_config(CHAT_HISTORY_DAYS=12)
        session.start_writer()
        buffer = recv_until(far, b"DCCORE CHATHIST 12 100\n")
        self.assertIn(b"DCCORE CHATHIST 12 100\n", buffer)


class TheSettings(support.DCCoreTestCase):
    def test_on_the_private_messages_page(self):
        pages = {key: names for key, _title, names in web_settings.SETTINGS_CATEGORIES}
        self.assertIn("CHAT_HISTORY_DAYS", pages["private-messages"])
        self.assertIn("CHAT_HISTORY_MAX_MB", pages["private-messages"])

    def test_a_label_in_every_language(self):
        for lang in ("en", "es", "fr"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", f"{lang}.json"), encoding="utf-8") as f:
                strings = json.load(f)
            for name in ("CHAT_HISTORY_DAYS", "CHAT_HISTORY_MAX_MB"):
                self.assertTrue(strings.get(f"settings.field.{name}"), (lang, name))

    def test_negative_is_refused_and_0_is_accepted(self):
        for name in ("CHAT_HISTORY_DAYS", "CHAT_HISTORY_MAX_MB"):
            with self.assertRaises(ValueError, msg=name):
                settings_file.coerce(name, "-1", 100, int)
            self.assertEqual(settings_file.coerce(name, "0", 100, int), 0)
            self.assertEqual(settings_file.coerce(name, "365", 100, int), 365)

    def test_the_feed_table_has_the_row(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as f:
            rows = [line for line in f if line.startswith("| `DCCORE CHATHIST <days> <max_mb>` |")]
        self.assertEqual(len(rows), 1)
        self.assertIn(f"said {adminchat.CHATHIST_SCRIPT_VERSION} or later", rows[0])


if __name__ == "__main__":
    unittest.main()
