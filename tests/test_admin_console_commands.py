"""Phase 2: the console's command surface, and what it is allowed to do.

Phase 1 proved who may open a session. This is what a session can then do, and
the piece that matters most is the authority question: a console has proved the
operator's Undernet services login and a password, which is a stronger claim
than the nick comparison the five admin handlers make internally. Without a way
to say "already authorised" those handlers would refuse a perfectly legitimate
console whose current nick simply is not in ADMIN_NICK.
"""

import importlib
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
import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import stats_mgr  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class FakeSession:
    """A Session that records instead of writing a socket."""

    def __init__(self, nick="SysOp"):
        self.nick = nick
        self.host = "sysop.users.undernet.org"
        self.peer_ip = "127.0.0.1"
        self.authenticated = True
        self.closed = False
        self.last_activity = time.time()
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))

    def close(self, announce_text="Session closed."):
        self.closed = True
        if announce_text:
            self.lines.append(announce_text)

    def text(self):
        return "\n".join(self.lines)


class AuthorisedBypassesTheNickCheck(DCCoreTestCase):
    """The console's authority comes from its own gate, not from ADMIN_NICK."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.HARD_BANS_FILE = os.path.join(self.tree.root, "hard_bans.txt")
        config.ADMIN_NICK = "SomeoneElse"
        silence_debug(announce)

    def test_a_console_may_ban_though_its_nick_is_not_admin_nick(self):
        """The trap this phase had to avoid.

        The operator authenticated with their +x host and a password. If the
        handler still insisted their NICK be in ADMIN_NICK, every console
        command would fail for exactly the people the console exists to serve.
        """
        self.assertFalse(commands.is_admin("SysOp"))
        commands.handle_hard_ban_request("SysOp", "DCC-CONSOLE", "!ban *!*@spam.net",
                                         authorised=True)
        self.assertEqual(db.load_hard_bans(), ["*!*@spam.net"])

    def test_a_console_may_unban(self):
        db.add_hard_ban("*!*@spam.net")
        commands.handle_hard_unban_request("SysOp", "DCC-CONSOLE", "!unban *!*@spam.net",
                                           authorised=True)
        self.assertEqual(db.load_hard_bans(), [])

    def test_the_channel_path_still_checks_the_nick(self):
        """Default is authorised=False, so nothing changes for channel callers."""
        commands.handle_hard_ban_request("SysOp", "#dccore-test", "!ban *!*@spam.net")
        self.assertEqual(db.load_hard_bans(), [],
                         "a non-admin nick in channel must still be refused")

    def test_every_admin_handler_accepts_the_flag(self):
        """All five, so none is left behind and silently unreachable."""
        import inspect
        for name in ("handle_admin_clear_queue", "handle_rehash_request",
                     "handle_hard_ban_request", "handle_hard_unban_request",
                     "handle_list_update_request"):
            with self.subTest(handler=name):
                params = inspect.signature(getattr(commands, name)).parameters
                self.assertIn("authorised", params)
                self.assertIs(params["authorised"].default, False,
                              "the flag must default off, so channel callers are unaffected")


class UptimeSurvivesRehash(unittest.TestCase):
    """stats_mgr.get_uptime_seconds() was dead code, and also wrong."""

    def test_start_time_is_not_reset_by_a_reload(self):
        """!rehash reloads stats_mgr, and reload re-executes the module body.

        A bare `start_time = time.time()` therefore reset the uptime to zero on
        every rehash. Nothing read it, so nothing noticed - but the console's
        banner and `uptime` command read it now.
        """
        before = stats_mgr.start_time
        time.sleep(0.05)
        importlib.reload(stats_mgr)
        self.assertEqual(stats_mgr.start_time, before,
                         "a rehash must not make the daemon look freshly started")

    def test_uptime_is_a_sane_number(self):
        self.assertGreaterEqual(stats_mgr.get_uptime_seconds(), 0)

    def test_format_uptime_reads_like_the_banner(self):
        cases = [
            (0, "0 Min"),
            (60, "1 Min"),
            (3600, "1 Hr and 0 Min"),
            (7320, "2 Hrs and 2 Min"),
            (90000, "1 Day, 1 Hr and 0 Min"),
            (283740, "3 Days, 6 Hrs and 49 Min"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(adminchat.format_uptime(seconds), expected)

    def test_a_negative_uptime_does_not_produce_nonsense(self):
        self.assertEqual(adminchat.format_uptime(-5), "0 Min")


class ReadOnlyCommands(DCCoreTestCase):
    """These build their own output, so they answer the session directly."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.HARD_BANS_FILE = os.path.join(self.tree.root, "hard_bans.txt")
        self.session = FakeSession()
        silence_debug(announce)

    def run_command(self, text):
        adminchat.handle_command(self.session, text)
        return self.session.text()

    def test_status_reports_the_things_an_operator_asks_for(self):
        config.dcc_queue = {"dave": [{"file": "a.flac"}], "erin": [{"file": "b.flac"}]}
        config.active_transfers = [{"user": "dave", "file": "a.flac", "bytes_sent": 10}]
        out = self.run_command("status")
        for expected in ("Slots", "Queued", "Frozen", "Hard bans", "MasterList", "running"):
            with self.subTest(expected=expected):
                self.assertIn(expected, out)

    def test_queue_lists_every_user(self):
        config.dcc_queue = {"dave": [{"file": "a.flac"}, {"file": "b.flac"}],
                            "erin": [{"file": "c.flac"}]}
        out = self.run_command("queue")
        self.assertIn("dave", out)
        self.assertIn("erin", out)
        self.assertIn("3 file(s) queued", out)

    def test_queue_can_name_one_user_and_shows_their_files(self):
        config.dcc_queue = {"dave": [{"file": "Enter Sandman.flac"}]}
        out = self.run_command("queue dave")
        self.assertIn("Enter Sandman.flac", out)

    def test_queue_marks_a_frozen_user(self):
        config.dcc_queue = {"dave": [{"file": "a.flac"}]}
        config.frozen_queues = {"dave": time.time()}
        self.assertIn("FROZEN", self.run_command("queue"))

    def test_queue_is_honest_about_being_empty(self):
        self.assertIn("empty", self.run_command("queue").lower())

    def test_slots_shows_what_is_sending(self):
        config.active_transfers = [{"user": "dave", "file": "a.flac", "bytes_sent": 4096}]
        out = self.run_command("slots")
        self.assertIn("dave", out)
        self.assertIn("4,096", out)

    def test_slots_says_so_when_idle(self):
        self.assertIn("nothing sending", self.run_command("slots"))

    def test_bans_lists_both_kinds(self):
        db.add_hard_ban("*!*@spam.net")
        config.banned_users = {"dave": time.time() + 600}
        out = self.run_command("bans")
        self.assertIn("*!*@spam.net", out)
        self.assertIn("dave", out)

    def test_version_names_the_build_and_platform(self):
        out = self.run_command("version")
        self.assertIn(config.SCRIPT_VERSION, out)
        self.assertIn("platform=", out)

    def test_uptime_answers(self):
        self.assertIn("Running", self.run_command("uptime"))


class DispatchBehaviour(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        silence_debug(announce)

    def test_an_unknown_command_is_reported_not_ignored(self):
        adminchat.handle_command(self.session, "frobnicate")
        self.assertIn("Unknown command", self.session.text())

    def test_help_lists_every_command(self):
        adminchat.handle_command(self.session, "help")
        out = self.session.text()
        for name in adminchat.COMMANDS:
            with self.subTest(name=name):
                self.assertIn(name, out)

    def test_commands_are_case_insensitive(self):
        adminchat.handle_command(self.session, "VERSION")
        self.assertIn(config.SCRIPT_VERSION, self.session.text())

    def test_a_blank_line_does_nothing(self):
        adminchat.handle_command(self.session, "   ")
        self.assertEqual(self.session.lines, [])

    def test_quit_closes_the_session(self):
        adminchat.handle_command(self.session, "quit")
        self.assertTrue(self.session.closed)

    def test_an_action_without_its_argument_shows_usage(self):
        for command in ("ban", "unban", "clearqueue"):
            with self.subTest(command=command):
                session = FakeSession()
                adminchat.handle_command(session, command)
                self.assertIn("Usage:", session.text())

    def test_a_command_that_raises_does_not_kill_the_session(self):
        """One bad command must cost that command, not the console."""
        original = adminchat.COMMANDS["status"]
        def explode(session, args):
            raise RuntimeError("boom")
        adminchat.COMMANDS["status"] = (explode, "x", "status")
        self.addCleanup(lambda: adminchat.COMMANDS.__setitem__("status", original))

        adminchat.handle_command(self.session, "status")
        self.assertIn("Command failed", self.session.text())
        self.assertFalse(self.session.closed)

    def test_running_a_command_counts_as_activity(self):
        """Otherwise a busy operator gets timed out mid-session."""
        self.session.last_activity = time.time() - 600
        adminchat.handle_command(self.session, "version")
        self.assertGreater(self.session.last_activity, time.time() - 5)


class DebugOutputReachesTheConsole(DCCoreTestCase):
    """Action commands report through send_debug, so the console subscribes."""

    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.addCleanup(lambda: announce.remove_debug_sink(self.session.debug_sink)
                        if hasattr(self.session, "debug_sink") else None)

    def test_a_registered_sink_receives_debug_lines(self):
        received = []
        announce.add_debug_sink(lambda text, category: received.append((category, text)))
        self.addCleanup(lambda: announce._debug_sinks.clear())
        announce.send_debug("a thing happened", category="BAN")
        self.assertEqual(received, [("BAN", "a thing happened")])

    def test_a_sink_that_raises_is_dropped_not_propagated(self):
        """send_debug is called from the IRC read loop; it must not throw there."""
        def bad(text, category):
            raise RuntimeError("sink is broken")
        announce.add_debug_sink(bad)
        self.addCleanup(lambda: announce._debug_sinks.clear())
        announce.send_debug("still fine")  # must not raise

    def test_mirc_formatting_is_stripped_for_the_console(self):
        session = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(session.close, None)
        session.authenticated = True
        session.debug_sink(f"{config.C_BOLD}dave{config.C_RESET} returned", "JOIN")
        line = session._outbox[-1]
        self.assertIn("dave returned", line)
        self.assertNotIn(config.C_BOLD, line)
        self.assertIn("[JOIN]", line)

    def test_an_unauthenticated_session_receives_nothing(self):
        session = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(session.close, None)
        session.authenticated = False
        session.debug_sink("secret operational detail", "INFO")
        self.assertEqual(len(session._outbox), 0)

    def test_the_sink_never_touches_the_socket(self):
        """It runs on the caller's thread, which may be the IRC read loop."""
        class Exploding:
            def sendall(self, payload):
                raise AssertionError("the sink wrote the socket on the caller's thread")
            def close(self):
                pass
            def shutdown(self, how):
                pass

        session = adminchat.Session(Exploding(), "127.0.0.1", "SysOp", "h")
        session.authenticated = True
        for i in range(200):
            session.debug_sink(f"line {i}", "INFO")

    def test_closing_a_session_unregisters_its_sink(self):
        session = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        session.authenticated = True
        announce.add_debug_sink(session.debug_sink)
        self.assertIn(session.debug_sink, announce._debug_sinks)
        session.close(announce_text=None)
        self.assertNotIn(session.debug_sink, announce._debug_sinks,
                         "a dead session must stop receiving log lines")


class ChannelCommandsCanBeRetired(unittest.TestCase):
    """ADMIN_CHANNEL_COMMANDS, so the console can eventually be the only way in."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_the_flag_defaults_to_on(self):
        """Locking the operator out on day one would be a poor introduction."""
        self.assertIs(getattr(config, "ADMIN_CHANNEL_COMMANDS", None), True)

    @staticmethod
    def _gate_condition():
        """The real elif condition, read out of irc.py.

        Spans several lines, so it is joined from the opening `elif` through the
        line that closes it.
        """
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        start = next(i for i, l in enumerate(lines)
                     if l.strip().startswith("elif (getattr(config, 'ADMIN_CHANNEL_COMMANDS'"))
        end = start
        while not lines[end].rstrip().endswith("):"):
            end += 1
        joined = " ".join(l.strip() for l in lines[start:end + 1])
        return joined[len("elif "):-1].strip()

    @staticmethod
    def _evaluate(condition, msg):
        namespace = {"config": config, "msg": msg, "msg_lower": msg.lower()}
        return bool(eval(condition, namespace))  # noqa: S307 - our own source

    def test_the_gate_actually_gates(self):
        """Evaluated, not grepped.

        A source search cannot tell that the condition was short-circuited to
        `True or ...`, which is precisely the mutation that survived the first
        pass of this suite.
        """
        condition = self._gate_condition()
        original = config.ADMIN_CHANNEL_COMMANDS
        self.addCleanup(lambda: setattr(config, "ADMIN_CHANNEL_COMMANDS", original))

        for command in ("!rehash", "!update", "!ban *!*@x", "!unban *!*@x",
                        "!clearqueue dave"):
            with self.subTest(command=command):
                config.ADMIN_CHANNEL_COMMANDS = True
                self.assertTrue(self._evaluate(condition, command),
                                f"{command} must work in channel while the flag is on")
                config.ADMIN_CHANNEL_COMMANDS = False
                self.assertFalse(self._evaluate(condition, command),
                                 f"{command} must be refused in channel once retired")

    def test_the_gate_never_swallows_a_user_command(self):
        """Turning admin commands off must not touch what ordinary users type."""
        condition = self._gate_condition()
        original = config.ADMIN_CHANNEL_COMMANDS
        self.addCleanup(lambda: setattr(config, "ADMIN_CHANNEL_COMMANDS", original))

        for flag in (True, False):
            config.ADMIN_CHANNEL_COMMANDS = flag
            for command in ("!list", "!ping", "!debugnames", "@find QUIT PLAYING GAMES",
                            "@DCCore", "!DCCore Enter Sandman.flac"):
                with self.subTest(flag=flag, command=command):
                    self.assertFalse(self._evaluate(condition, command),
                                     "a user command must never match the admin gate")

    def test_the_admin_branches_are_gated(self):
        self.assertIn("ADMIN_CHANNEL_COMMANDS", self.source)

    def test_the_user_commands_are_not_gated(self):
        """!list, !ping and the queue triggers must keep working regardless."""
        gated = self.source.split("ADMIN_CHANNEL_COMMANDS", 1)[1]
        end = gated.find("elif any(msg_lower.startswith")
        block = gated[:end]
        for user_command in ('"!list"', '"!ping"', '"!debugnames"', '@find '):
            with self.subTest(user_command=user_command):
                self.assertNotIn(user_command, block,
                                 "a user command must not sit behind the admin flag")

    def test_all_five_admin_commands_are_inside_the_gate(self):
        gated = self.source.split("ADMIN_CHANNEL_COMMANDS", 1)[1]
        end = gated.find("elif any(msg_lower.startswith")
        block = gated[:end]
        for handler in ("handle_rehash_request", "handle_hard_ban_request",
                        "handle_hard_unban_request", "handle_list_update_request",
                        "handle_admin_clear_queue"):
            with self.subTest(handler=handler):
                self.assertIn(handler, block)


if __name__ == "__main__":
    unittest.main()
