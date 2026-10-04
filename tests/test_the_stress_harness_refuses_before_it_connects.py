"""scripts/stress_test.py, the release manager's stress harness (#1153).

It drives 40 simulated IRC clients at a running bot: it floods, advertises as
file servers and gets its own nicks muted and banned. That is only ever meant
for a test bot on a private test server, and the file says so in its own
docstring. These tests keep the parts that make it safe to carry in the
repository: the warning stays, a mistyped command line is refused before a
single connection is made, and the file imports on every platform the suite
runs on even though it only runs on Linux (it reads the bot from /proc).

Nothing here starts a client or opens a socket: socket.create_connection is
replaced with one that fails the test if it is ever called.
"""

import contextlib
import importlib.util
import io
import os
import sys
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO_ROOT, "scripts", "stress_test.py")


def load_harness():
    spec = importlib.util.spec_from_file_location("stress_test_under_test", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TheHarnessSaysWhereItMayRun(unittest.TestCase):

    def test_the_docstring_keeps_the_warning(self):
        harness = load_harness()

        self.assertIn("Never point this at a bot on a real network.",
                      " ".join(harness.__doc__.split()))

    def test_it_imports_where_it_cannot_run(self):
        """os.sysconf does not exist on Windows; a module-level call to it
        made the file fail to import there."""
        saved = getattr(os, "sysconf", None)
        if saved is not None:
            del os.sysconf
        try:
            harness = load_harness()
        finally:
            if saved is not None:
                os.sysconf = saved

        self.assertGreater(harness.TICK, 0)
        self.assertGreater(harness.PAGE, 0)


class AMistakeIsRefusedBeforeAnyConnection(unittest.TestCase):

    def run_main(self, *argv):
        harness = load_harness()

        def no_network(*_a, **_k):
            raise AssertionError("the harness opened a connection")

        stderr = io.StringIO()
        with mock.patch.object(harness.socket, "create_connection", no_network), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                harness.main(list(argv))
        return stopped.exception.code, stderr.getvalue()

    def test_an_unknown_phase(self):
        code, said = self.run_main("--pid", "1", "--channel", "#somechannel", "--test", "flod")

        self.assertEqual(code, 2)
        self.assertIn("unknown phase: flod", said)

    def test_a_server_without_a_port(self):
        code, said = self.run_main("--pid", "1", "--channel", "#somechannel",
                                   "--server", "127.0.0.1")

        self.assertEqual(code, 2)
        self.assertIn("--server must be host:port", said)

    def test_a_bot_that_is_not_there(self):
        """No /proc/<pid>: not Linux, or no such process. Refused rather than
        connecting 40 clients and then failing on the first sample."""
        code, said = self.run_main("--pid", "999999999", "--channel", "#somechannel")

        self.assertEqual(code, 2)
        self.assertIn("/proc/999999999", said)


class TheSimulatedNicks(unittest.TestCase):

    def test_are_letters_only_and_distinct(self):
        harness = load_harness()
        nicks = [harness.nick_for(i) for i in range(1000)]

        self.assertEqual(len(set(nicks)), 1000)
        for nick in nicks:
            self.assertTrue(nick.isalpha() and nick.isascii(), nick)


class TheStormCountsWhatTheBotWrote(unittest.TestCase):
    """The bot writes known_bots.json at most once per 30 s and only when an
    advert arrives, so read straight after the storm the file still lacked
    its last adverts and "known bots after" read low. The harness now sends
    one more advert from a bot already known before it reads the file."""

    def run_storm(self, clients):
        harness = load_harness()
        events = []

        class FakeClient(object):
            def __init__(self, nick, alive=True):
                self.nick, self.alive = nick, alive

            def send(self, line):
                events.append(("send", self.nick, line))

        class FakeBot(object):
            def known_bots(self):
                events.append(("read",))
                return 0

        def storm_then_settle(_bot, _host, _name, work, settle=5):
            work()
            events.append(("settled", settle))

        fakes = [FakeClient(nick, alive) for nick, alive in clients]
        args = harness.argparse.Namespace(channel="#somechannel")
        no_waiting = types.SimpleNamespace(sleep=lambda _s: None, time=time.time)
        with mock.patch.object(harness, "time", no_waiting), \
                mock.patch.object(harness, "phase", storm_then_settle), \
                contextlib.redirect_stdout(io.StringIO()):
            harness.run_storm(FakeBot(), args, "127.0.0.1", 6667, fakes)
        return events

    def test_one_more_advert_from_a_known_bot_comes_before_the_last_read(self):
        events = self.run_storm([("Alpha", True), ("Bravo", True)])

        self.assertEqual(events[-1], ("read",))
        self.assertEqual(events[-3][0], "settled", "the nudge comes after the storm has settled")
        self.assertGreater(events[-3][1], 30, "the settle outlasts the bot's 30 s flush interval")
        last_send = events[-2]
        self.assertEqual(last_send[0], "send")
        storm_nicks = {e[1] for e in events[1:-3]}
        self.assertIn(last_send[1], storm_nicks, "the nudge must come from a bot the storm made known")
        self.assertIn(f"Type: @{last_send[1]} For My List Of:", last_send[2])

    def test_a_dead_client_does_not_send_it(self):
        events = self.run_storm([("Alpha", False), ("Bravo", True)])

        self.assertEqual(events[-2][1], "Bravo")

    def test_with_no_client_left_it_still_reads(self):
        events = self.run_storm([("Alpha", False)])

        self.assertEqual(events[-2:], [("settled", events[-2][1]), ("read",)])


if __name__ == "__main__":
    unittest.main()
