"""The on-connect commands are checked, and sent again when they did not take (#1066).

They go out once, before the JOIN. In a net split the X login among them can
go nowhere - X is on the other side - and the bot then sat in its channels
with its real host until the next reconnect, with nothing noticing.

+x alone is not the answer: on Undernet's ircd the +x flag is taken whenever
it is asked for, and the host is only hidden once the user is logged in too -
which the server says with numeric 396. So +x counts once a 396 has arrived.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import on_connect  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

REPO_ROOT = support.REPO_ROOT
NICK = "SomeBot"
X_LOGIN = "PRIVMSG X@channels.example.org :LOGIN someaccount s3cret-word"
COMMANDS = [X_LOGIN, "MODE %nick% +x"]


class FakeServer:
    """A socket that answers `MODE <nick>` with a 221, the way a server does,
    by handing the reply to the same reader the read loop calls."""

    def __init__(self, modes="+i", answer=True):
        self.modes = modes
        self.answer = answer
        self.sent = []
        self.lock = threading.Lock()
        self.got = threading.Condition(self.lock)

    def sendall(self, data):
        line = data.decode("utf-8").rstrip("\r\n")
        with self.lock:
            self.sent.append(line)
            self.got.notify_all()
        if self.answer and line == f"MODE {NICK}":
            on_connect.note_server_line(f":irc.example.org 221 {NICK} {self.modes}", NICK)

    def wait_for(self, count, timeout=10, keep=lambda line: True):
        """The lines `keep` accepts, once there are `count` of them."""
        with self.lock:
            self.got.wait_for(lambda: len([l for l in self.sent if keep(l)]) >= count,
                              timeout=timeout)
            return [l for l in self.sent if keep(l)]


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "on_connect.json")
        self.set_config(ON_CONNECT_FILE=self.store, NICKNAME=NICK)
        on_connect.reset_state()
        self.addCleanup(on_connect.reset_state)

    def save(self, commands, delay=0):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            json.dump({"commands": commands, "delay_seconds": delay}, handle)

    def check(self, server, commands=COMMANDS, wait=5):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = on_connect.check_once(server, NICK, commands, 0, wait=wait,
                                           sleep=lambda seconds: None)
        return result, out.getvalue()


class WhatTheCommandsAskFor(unittest.TestCase):
    def test_a_mode_on_the_nick_placeholder_or_the_nick(self):
        self.assertEqual(on_connect.wanted_modes(["MODE %nick% +x"], NICK), {"x"})
        self.assertEqual(on_connect.wanted_modes(["mode somebot +iw"], NICK), {"i", "w"})

    def test_not_a_channel_or_someone_else(self):
        self.assertEqual(on_connect.wanted_modes(
            ["MODE #somechan +m", "MODE OtherNick +x", X_LOGIN], NICK), set())

    def test_a_letter_taken_away_again_is_not_wanted(self):
        self.assertEqual(on_connect.wanted_modes(["MODE %nick% +xw", "MODE %nick% -w"], NICK), {"x"})

    def test_x_needs_the_hidden_host_too(self):
        self.assertEqual(on_connect.missing_modes({"x"}, {"x"}, hidden=False), {"x"})
        self.assertEqual(on_connect.missing_modes({"x"}, {"x"}, hidden=True), set())
        self.assertEqual(on_connect.missing_modes({"i"}, {"i"}, hidden=False), set())
        self.assertEqual(on_connect.missing_modes({"i", "x"}, set(), hidden=True), {"i", "x"})


class ReadingTheServer(Case):
    def test_221_lists_our_modes(self):
        on_connect.note_server_line(f":irc.example.org 221 {NICK} +ix", NICK)
        self.assertEqual(runtime.on_connect_state["modes"], {"i", "x"})
        self.assertEqual(runtime.on_connect_state["listings"], 1)

    def test_396_says_the_host_is_hidden(self):
        on_connect.note_server_line(
            f":irc.example.org 396 {NICK} someaccount.users.example.org :is now your hidden host", NICK)
        self.assertTrue(runtime.on_connect_state["hidden"])

    def test_a_mode_change_on_ourselves(self):
        on_connect.note_server_line(f":irc.example.org 221 {NICK} +i", NICK)
        on_connect.note_server_line(f":{NICK}!ident@host.example.org MODE {NICK} :+x", NICK)
        self.assertEqual(runtime.on_connect_state["modes"], {"i", "x"})

    def test_what_someone_types_is_not_the_server(self):
        on_connect.note_server_line(f":Someone!u@h PRIVMSG {NICK} :221 {NICK} +x", NICK)
        on_connect.note_server_line(f":Someone!u@h PRIVMSG #somechan :396 {NICK} x", NICK)
        on_connect.note_server_line(f":Someone!u@h MODE {NICK} :+x", NICK)
        on_connect.note_server_line(":irc.example.org 221 OtherNick +x", NICK)
        self.assertEqual(runtime.on_connect_state,
                         {"modes": None, "listings": 0, "hidden": False,
                          "resends": 0, "gave_up": False, "sent_epoch": None})

    def test_a_new_connection_starts_over(self):
        on_connect.note_server_line(f":irc.example.org 221 {NICK} +x", NICK)
        on_connect.note_server_line(f":irc.example.org 396 {NICK} h :is now your hidden host", NICK)
        runtime.on_connect_state["resends"] = 3
        on_connect.reset_state()
        self.assertEqual(runtime.on_connect_state,
                         {"modes": None, "listings": 0, "hidden": False,
                          "resends": 0, "gave_up": False, "sent_epoch": None})


class TheCheck(Case):
    def test_a_hidden_host_needs_nothing_more(self):
        on_connect.note_server_line(f":irc.example.org 396 {NICK} h :is now your hidden host", NICK)
        server = FakeServer("+ix")
        result, _log = self.check(server)
        self.assertEqual(result, "ok")
        self.assertEqual(server.sent, [f"MODE {NICK}"])

    def test_plus_x_without_a_hidden_host_sends_them_all_again(self):
        """The net split: the login went nowhere, +x was taken anyway."""
        server = FakeServer("+ix")
        result, log = self.check(server)
        self.assertEqual(result, "resent")
        self.assertEqual(server.sent, [f"MODE {NICK}", X_LOGIN, f"MODE {NICK} +x"])
        self.assertNotIn("s3cret-word", log)
        self.assertIn("host is not hidden", log)

    def test_no_plus_x_at_all_sends_them_again(self):
        server = FakeServer("+i")
        self.assertEqual(self.check(server)[0], "resent")
        self.assertEqual(runtime.on_connect_state["resends"], 1)

    def test_no_answer_is_not_a_reason_to_resend(self):
        server = FakeServer(answer=False)
        self.assertEqual(self.check(server, wait=0.05)[0], "no-answer")
        self.assertEqual(server.sent, [f"MODE {NICK}"])

    def test_nothing_to_check_asks_nothing(self):
        server = FakeServer()
        self.assertEqual(self.check(server, commands=[X_LOGIN])[0], "nothing")
        self.assertEqual(server.sent, [])

    def test_it_gives_up_after_the_cap_and_says_so_once(self):
        """A network that takes +x and never sends 396 must not be sent the
        login for ever."""
        runtime.on_connect_state["resends"] = on_connect.RESEND_MOST
        server = FakeServer("+ix")
        first, log1 = self.check(server)
        second, log2 = self.check(server)
        self.assertEqual((first, second), ("gave-up", "gave-up"))
        self.assertEqual(server.sent, [f"MODE {NICK}", f"MODE {NICK}"], "no login sent")
        self.assertIn("not sending them again", log1)
        self.assertEqual(log2, "")


class TheThread(Case):
    def test_it_ends_when_a_new_connection_claims_the_epoch(self):
        self.set_config(connection_epoch=7, ON_CONNECT_CHECK_MINUTES=5)
        self.save(COMMANDS)
        on_connect.note_server_line(f":irc.example.org 396 {NICK} h :is now your hidden host", NICK)
        server = FakeServer("+ix")
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                config.connection_epoch = 8

        with contextlib.redirect_stdout(io.StringIO()):
            on_connect.watch(server, 7, sleep=sleep)
        self.assertEqual(slept[:2], [on_connect.CHECK_FIRST_DELAY, 300])
        self.assertEqual(server.sent[0], f"MODE {NICK}", "it looked once, then stopped")

    def test_zero_minutes_never_looks(self):
        self.set_config(connection_epoch=7, ON_CONNECT_CHECK_MINUTES=0)
        self.save(COMMANDS)
        server = FakeServer("+i")
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                config.connection_epoch = 8

        on_connect.watch(server, 7, sleep=sleep)
        self.assertEqual(server.sent, [])


class TheResendButton(Case):
    def connected(self, server, commands_sent=True):
        """A registered link whose delayed_join() has sent the commands - and
        not joined to its channel: the button never asks that (#1085)."""
        import sys
        import types
        self.set_config(bot_joined_channel=False, connection_epoch=7)
        if commands_sent:
            on_connect.mark_sent(7)
        old = sys.modules.get("oserve")
        sys.modules["oserve"] = types.SimpleNamespace(irc_connection=server)
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", old) if old
                        else sys.modules.pop("oserve", None))

    def test_it_sends_the_saved_commands_and_lets_the_check_try_again(self):
        self.save(COMMANDS)
        server = FakeServer()
        self.connected(server)
        runtime.on_connect_state.update(resends=6, gave_up=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status, result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["sent"], 2)
        # Only what the resend sends. This fake is the bot's live socket for
        # the length of the test, and a debug line another test's thread is
        # still draining to the debug channel can land on it too - CI saw one.
        resent = server.wait_for(2, keep=lambda line: not line.startswith("PRIVMSG #"))
        self.assertEqual(resent, [X_LOGIN, f"MODE {NICK} +x"])
        self.assertEqual((runtime.on_connect_state["resends"], runtime.on_connect_state["gave_up"]),
                         (0, False))
        self.assertNotIn("s3cret-word", out.getvalue() + json.dumps(result))

    def test_not_connected_sends_nothing(self):
        self.save(COMMANDS)
        self.connected(None)
        status, result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 409)
        self.assertIn("not connected", result["error"])

    def test_refused_by_its_channel_it_still_sends_them(self):
        """#1085: a +r channel answers 477 when the X login did not take, so
        the channel is never joined - and sending the login again is the fix."""
        self.save(COMMANDS)
        server = FakeServer()
        self.connected(server)
        with contextlib.redirect_stdout(io.StringIO()):
            status, result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 200, result)
        resent = server.wait_for(2, keep=lambda line: not line.startswith("PRIVMSG #"))
        self.assertEqual(resent, [X_LOGIN, f"MODE {NICK} +x"])

    def test_a_link_still_registering_is_not_sent_to(self):
        """Its commands are about to go out from delayed_join() anyway."""
        self.save(COMMANDS)
        server = FakeServer()
        self.connected(server, commands_sent=False)
        status, result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 409)
        self.assertIn("still connecting", result["error"])
        self.assertEqual(server.sent, [])

    def test_the_last_link_having_sent_them_does_not_count(self):
        self.save(COMMANDS)
        server = FakeServer()
        self.connected(server)
        self.set_config(connection_epoch=8)    # dropped and reconnecting
        status, _result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 409)
        self.assertEqual(server.sent, [])

    def test_a_new_connection_forgets_the_mark(self):
        on_connect.mark_sent(7)
        on_connect.reset_state()
        self.assertFalse(on_connect.sent_on(7))

    def test_nothing_saved_is_said(self):
        self.connected(FakeServer())
        status, result = webserver.build_on_connect_resend_result()
        self.assertEqual(status, 400)


def read(path):
    with io.open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        return handle.read()


class TheReadLoopIsWired(unittest.TestCase):
    """Driving a real connect needs a server, so read out of irc.py."""

    def test_every_line_is_read_and_each_connection_starts_over(self):
        code = read("src/irc.py")
        self.assertIn("on_connect.note_server_line(line, config.NICKNAME)", code)
        start = code.index("my_epoch = config.connection_epoch")
        self.assertIn("on_connect.reset_state()", code[start:start + 1500])

    def test_the_check_starts_after_the_join_with_its_own_connection(self):
        """The epoch is bound when delayed_join() is defined: read later, a
        reconnect in its five-second sleep would hand the new epoch to a thread
        holding the old socket."""
        code = read("src/irc.py")
        self.assertIn("def delayed_join(socket_conn, channels, epoch=my_epoch):", code)
        body = code.split("def delayed_join(", 1)[1].split("threading.Thread(target=delayed_join", 1)[0]
        self.assertLess(body.index("join_batches("), body.index("target=on_connect.watch"))
        self.assertIn("args=(socket_conn, epoch),", body)

    def test_the_resend_opens_once_the_commands_are_out(self):
        """#1085: marked after the on-connect loop and before the JOIN - not on
        joining, which a +r channel refuses without the login."""
        code = read("src/irc.py")
        body = code.split("def delayed_join(", 1)[1].split("threading.Thread(target=delayed_join", 1)[0]
        mark = body.index("on_connect.mark_sent(epoch)")
        self.assertLess(body.index("socket_conn.sendall(" + chr(10)), mark)
        self.assertLess(mark, body.index("join_batches("))


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(signature) {
  const start = src.indexOf(signature);
  let depth = 0, i = src.indexOf("{", start);
  for (; i < src.length; i++) {
    if (src[i] === "{") { depth++; }
    else if (src[i] === "}") { depth--; if (depth === 0) { break; } }
  }
  return src.slice(start, i + 1);
}
const posted = [];
function run(boxValue) {
  const state = { onConnect: { commands: ["PRIVMSG X :LOGIN a b", "MODE %nick% +x"] }, onConnectNote: null };
  const el = { settingsFields: { querySelector: function () { return { value: boxValue }; } } };
  const resend = new Function("state", "el", "postJson", "renderSettingsCategory", "t",
    fn("function resendOnConnect(") + "\nreturn resendOnConnect;")(
    state, el,
    function (url, body) { posted.push(url); return { then: function () {} }; },
    function () {}, function (key) { return key; });
  resend();
  return state.onConnectNote;
}
const edited = run("PRIVMSG X :LOGIN a b\nMODE %nick% +xi");
const same = run("  PRIVMSG X :LOGIN a b \n\nMODE %nick% +x\n");
console.log(JSON.stringify({ edited: edited, same: same, posted: posted }));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class ThePage(unittest.TestCase):
    def test_unsaved_lines_are_not_sent_and_saved_ones_are(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        result = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual(result["edited"], {"ok": False, "text": "settings.resendSaveFirst"})
        self.assertIsNone(result["same"])
        self.assertEqual(result["posted"], ["/api/on-connect/resend"])


class ThePageSource(unittest.TestCase):
    def test_the_button_sits_beside_save_and_is_wired(self):
        js = read("web/app.js")
        section = js.split("function onConnectSectionHtml(", 1)[1].split("function attachOnConnectRows(", 1)[0]
        self.assertLess(section.index("on-connect-save"), section.index("on-connect-resend"))
        self.assertIn('if (evt.target.closest(".on-connect-resend")) {\n      resendOnConnect();', js)

    def test_every_language_has_the_words(self):
        for lang in ("en", "es", "fr"):
            strings = json.loads(read(f"web/lang/{lang}.json"))
            for key in ("settings.resendOnConnectCommands", "settings.resendOnConnectTitle",
                        "settings.resendSaveFirst", "settings.field.ON_CONNECT_CHECK_MINUTES"):
                self.assertIn(key, strings, (lang, key))


if __name__ == "__main__":
    unittest.main()
