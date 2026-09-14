"""A web-native alternative to the DCC CHAT admin console and the debug
channel, for an operator who wants neither.

Two halves, tested separately because they ARE separate at runtime:

  * the LOG (webserver._console_debug_sink / build_console_log_payload) is
    the ambient stream every announce.send_debug() call already produces,
    reached by registering as another consumer of the same fan-out
    adminchat.py's own DCC sessions use.
  * a COMMAND (webserver.build_console_command_result) is request/response:
    adminchat.COMMANDS is dispatched directly, not re-implemented, so these
    tests are mostly about the SEAM (what a caller gets back) rather than
    about any one command's own behaviour, which already has its own tests
    in tests/test_adminchat.py.

Route wiring (does the HTTP layer reach these functions with what the
caller sent) lives in tests/test_dashboard_routes.py, matching where every
other route's wiring already lives.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ConsoleCase(DCCoreTestCase):
    """Isolates announce._debug_sinks and webserver's own console state the
    same way tests/test_debug_routing.py isolates the sinks list: snapshot,
    clear, restore - so a sink this file registers cannot leak into an
    unrelated test that runs afterward in the same process, and so an
    unrelated sink some earlier test left behind cannot end up in this
    file's own log assertions.
    """

    def setUp(self):
        super().setUp()
        self._sinks = announce._debug_sinks[:]
        announce._debug_sinks.clear()
        self.addCleanup(lambda: (announce._debug_sinks.clear(),
                                 announce._debug_sinks.extend(self._sinks)))

        webserver._console_log.clear()
        self._next_id = webserver._console_next_id
        self.addCleanup(setattr, webserver, "_console_next_id", self._next_id)

        self._registered = webserver._console_sink_registered
        webserver._console_sink_registered = False
        self.addCleanup(setattr, webserver, "_console_sink_registered",
                        self._registered)

        for name in ("DEBUG_TO_CHANNEL", "DEBUG_TO_CONSOLE"):
            original = getattr(config, name, True)
            self.addCleanup(lambda n=name, v=original: setattr(config, n, v))


class TheSinkReceivesWhatSendDebugSends(ConsoleCase):
    """The wiring claim itself: registering _console_debug_sink is enough for
    real announce.send_debug() output - not a synthetic call to the sink
    function - to reach the polled log."""

    def test_a_debug_line_reaches_the_log(self):
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()

        announce.send_debug("a console test line", "INFO")

        payload = webserver.build_console_log_payload(0)
        texts = [line["text"] for line in payload["lines"]]
        self.assertIn("a console test line", texts)

    def test_categories_are_carried_through(self):
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()

        announce.send_debug("a security line", "HARDBAN")

        payload = webserver.build_console_log_payload(0)
        matches = [line for line in payload["lines"] if line["text"] == "a security line"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["category"], "HARDBAN")

    def test_debug_to_channel_off_does_not_take_the_console_with_it(self):
        """The whole point: DEBUG_TO_CHANNEL and DEBUG_TO_CONSOLE are
        independent switches (see announce.send_debug's own ROUTING
        comment), so an operator with no debug channel at all still gets
        this."""
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()

        announce.send_debug("still arrives", "INFO")

        texts = [line["text"] for line in webserver.build_console_log_payload(0)["lines"]]
        self.assertIn("still arrives", texts)

    def test_irc_formatting_is_stripped(self):
        """A console is read as a log, not rendered by an IRC client - the
        same reason adminchat.Session.debug_sink() strips it."""
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()

        announce.send_debug(f"{config.C_RED}coloured{config.C_RESET} text", "INFO")

        texts = [line["text"] for line in webserver.build_console_log_payload(0)["lines"]]
        self.assertIn("coloured text", texts)


class TheSinkRegistersAtMostOnce(ConsoleCase):

    def test_calling_ensure_twice_does_not_duplicate_the_sink(self):
        webserver._ensure_console_sink()
        webserver._ensure_console_sink()

        self.assertEqual(announce._debug_sinks.count(webserver._console_debug_sink), 1)

    def test_a_line_is_not_logged_twice(self):
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()
        webserver._ensure_console_sink()

        announce.send_debug("counted once", "INFO")

        texts = [line["text"] for line in webserver.build_console_log_payload(0)["lines"]]
        self.assertEqual(texts.count("counted once"), 1)

    def test_the_log_route_itself_registers_the_sink(self):
        """build_console_log_payload() is what a freshly loaded page calls
        first - it must not depend on start() (never run in a test) having
        already registered anything."""
        self.assertNotIn(webserver._console_debug_sink, announce._debug_sinks)

        webserver.build_console_log_payload(0)

        self.assertIn(webserver._console_debug_sink, announce._debug_sinks)


class TheLogIsPolledByCursorNotOffset(ConsoleCase):
    """since=<cursor>, not offset/limit - a live stream where an offset would
    either replay lines already shown or skip ones that arrived between two
    polls."""

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_TO_CHANNEL=False, DEBUG_TO_CONSOLE=True)
        webserver._ensure_console_sink()

    def test_the_first_poll_returns_the_existing_backlog(self):
        announce.send_debug("before anyone polled", "INFO")

        payload = webserver.build_console_log_payload(0)

        texts = [line["text"] for line in payload["lines"]]
        self.assertIn("before anyone polled", texts)

    def test_polling_again_with_the_returned_cursor_gets_nothing_new(self):
        announce.send_debug("first", "INFO")
        first = webserver.build_console_log_payload(0)

        second = webserver.build_console_log_payload(first["cursor"])

        self.assertEqual(second["lines"], [])

    def test_a_line_that_arrives_after_the_cursor_is_returned_next_time(self):
        announce.send_debug("first", "INFO")
        first = webserver.build_console_log_payload(0)

        announce.send_debug("second", "INFO")
        second = webserver.build_console_log_payload(first["cursor"])

        texts = [line["text"] for line in second["lines"]]
        self.assertEqual(texts, ["second"])

    def test_a_missing_since_is_treated_as_zero(self):
        """The route passes request.args.get("since") straight through, which
        is None on a first request with no query string at all."""
        announce.send_debug("present", "INFO")

        payload = webserver.build_console_log_payload(None)

        texts = [line["text"] for line in payload["lines"]]
        self.assertIn("present", texts)

    def test_a_non_numeric_since_is_treated_as_zero_not_an_error(self):
        announce.send_debug("present", "INFO")

        payload = webserver.build_console_log_payload("not-a-number")

        texts = [line["text"] for line in payload["lines"]]
        self.assertIn("present", texts)

    def test_the_buffer_is_bounded(self):
        """A flood of debug lines must not grow the log without limit - the
        same shape as _debug_queue's own maxlen in announce.py."""
        for i in range(webserver._console_log.maxlen + 50):
            announce.send_debug(f"line {i}", "INFO")

        self.assertEqual(len(webserver._console_log), webserver._console_log.maxlen)


class CommandDispatchReusesAdminchat(ConsoleCase):
    """adminchat.COMMANDS is dispatched directly, not re-implemented - these
    tests are about the seam (what build_console_command_result returns),
    not about any one command's own behaviour."""

    def test_a_known_command_produces_the_same_lines_adminchat_would(self):
        """Not a hand-picked expected string: whatever _cmd_uptime actually
        writes into a session is what this must return, so a change to that
        command's own output cannot silently stop matching this test."""
        real_session = adminchat.Session(sock=None, peer_ip="1.2.3.4",
                                         nick="probe", host="probe.host")
        adminchat._cmd_uptime(real_session, "")
        expected = list(real_session._outbox)

        status, result = webserver.build_console_command_result("uptime")

        self.assertEqual(status, 200)
        self.assertEqual(result["lines"], expected)

    def test_help_lists_every_command_including_quit(self):
        """'help' still lists 'quit' - COMMANDS itself is untouched, only the
        web layer intercepts it before dispatch. Misleading to hide a real
        command from its own help text."""
        status, result = webserver.build_console_command_result("help")

        self.assertEqual(status, 200)
        self.assertTrue(any("quit" in line for line in result["lines"]))

    def test_an_unrecognised_command_is_a_normal_reply_not_an_http_error(self):
        """The same as what a real DCC CHAT session sees for a typo - see
        adminchat.handle_command()'s own 'Unknown command' branch."""
        status, result = webserver.build_console_command_result("not-a-real-command")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["lines"]), 1)
        self.assertIn("Unknown command", result["lines"][0])

    def test_an_empty_command_is_refused_before_dispatch(self):
        status, result = webserver.build_console_command_result("")

        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_a_command_that_is_only_whitespace_is_also_refused(self):
        status, result = webserver.build_console_command_result("   ")

        self.assertEqual(status, 400)

    def test_command_name_matching_is_case_insensitive(self):
        """adminchat.handle_command() lowercases before the COMMANDS lookup;
        this must not shadow that with its own, different rule."""
        status, result = webserver.build_console_command_result("UPTIME")

        self.assertEqual(status, 200)
        self.assertFalse(any("Unknown command" in line for line in result["lines"]))

    def test_the_session_nick_identifies_the_web_console(self):
        """Reaches commands.py's own audit logging (ban/unban pass
        session.nick straight through as the requester) - a web-issued
        action should not read back as though an IRC nick did it."""
        captured = {}

        class RecordingSession(webserver._WebConsoleSession):
            def send(self, text=""):
                captured["nick"] = self.nick
                super().send(text)

        original = webserver._WebConsoleSession
        webserver._WebConsoleSession = RecordingSession
        self.addCleanup(setattr, webserver, "_WebConsoleSession", original)

        webserver.build_console_command_result("uptime", remote_addr="203.0.113.9")

        self.assertIn("203.0.113.9", captured["nick"])


class TheQuitCommandIsInterceptedBeforeDispatch(ConsoleCase):
    """adminchat._cmd_quit calls session.close() - meaningful for a DCC CHAT
    socket, meaningless (and, on _WebConsoleSession, an AttributeError) for a
    single HTTP request. The web layer must intercept it before
    handle_command() ever runs, not rely on the try/except inside
    handle_command() to paper over the crash.
    """

    def test_quit_does_not_raise(self):
        status, result = webserver.build_console_command_result("quit")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["lines"]), 1)

    def test_quit_never_reaches_handle_command(self):
        """Mutation check on the interception itself: if _CONSOLE_UNSUPPORTED_
        COMMANDS were empty, 'quit' would fall through to handle_command(),
        which calls session.close() - AttributeError on _WebConsoleSession,
        caught by handle_command()'s own try/except and turned into a
        'Command failed: ...' line instead of the friendly message asserted
        here. Verified by reverting rather than assumed."""
        original = webserver._CONSOLE_UNSUPPORTED_COMMANDS
        webserver._CONSOLE_UNSUPPORTED_COMMANDS = frozenset()
        self.addCleanup(setattr, webserver, "_CONSOLE_UNSUPPORTED_COMMANDS", original)

        status, result = webserver.build_console_command_result("quit")

        self.assertEqual(status, 200)
        self.assertIn("Command failed", result["lines"][0])

    def test_the_friendly_message_is_restored_with_the_guard_in_place(self):
        status, result = webserver.build_console_command_result("quit")

        self.assertNotIn("Command failed", result["lines"][0])
        self.assertIn("close this tab", result["lines"][0].lower())


class AsyncCommandsAcknowledgeRatherThanWaitForTheResult(ConsoleCase):
    """ban, unban, clearqueue, rehash and update each run the real work on a
    background thread (adminchat._run_detached) and reply immediately with
    only an acknowledgement line. The eventual result is not returned here -
    it reaches announce.send_debug() when the background thread finishes,
    which is covered by TheSinkReceivesWhatSendDebugSends above, not by this
    class.
    """

    def test_ban_returns_only_the_acknowledgement(self):
        """Sandboxes HARD_BANS_FILE first: unmocked, the background thread
        this dispatches to really does write the pattern to disk (see
        commands.handle_hard_ban_request) - config.py's own default,
        './data/hard_bans.txt', is relative to the real repository, the same
        hazard tests/test_bans_and_flood.py's setUp already redirects for
        every test in that file."""
        ban_dir = tempfile.mkdtemp(prefix="dccore-console-ban-test-")
        self.addCleanup(shutil.rmtree, ban_dir, True)
        self.set_config(HARD_BANS_FILE=os.path.join(ban_dir, "hard_bans.txt"))

        status, result = webserver.build_console_command_result(
            "ban *!*@spammer.example")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["lines"]), 1)
        self.assertIn("Banning", result["lines"][0])

    def test_rehash_returns_only_the_acknowledgement(self):
        """rehash is exercised here for the acknowledgement-only property
        alone, not for what a real reload does - so the real reload must not
        run at all. Unmocked, adminchat._cmd_rehash's background thread calls
        the REAL commands.handle_rehash_request(), which reloads config/dcc/
        announce/security/db/stats_mgr for real on a thread this test does
        not wait for, racing whatever test runs next in this process - the
        exact hazard tests/test_webserver.py's own SettingsPayloadTests.setUp
        already guards against the same way."""
        import commands
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

        status, result = webserver.build_console_command_result("rehash")

        self.assertEqual(status, 200)
        self.assertEqual(len(result["lines"]), 1)
        self.assertIn("Rehashing", result["lines"][0])


if __name__ == "__main__":
    unittest.main()
