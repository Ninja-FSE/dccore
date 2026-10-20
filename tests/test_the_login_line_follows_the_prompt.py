"""dccore.mrc: the login line counts only once the password prompt was answered.

The bot always sends `Enter Your Password:`, and sends `Entering DCC Chat
Admin Interface` only once that prompt has been answered. A chat's state is
`auth` only then: the prompt branch sent the token after the peer check, or
a password was typed at the prompt (dccore.typedpass, on every way a line
can be typed: @DCCore, @DCCore-console, /dccore raw and the menu's console
command). `password` means the prompt was shown and nothing has answered
it. The script accepts the login line only in `auth`: in any other state it
says so in the window and leaves the chat as it leaves one whose peer check
said no (dccore.abandon), and the chat never reaches state `in` - so nothing
is sent, and no `DCCORE` line is read after it. A chat already logged in is
left alone by a second login line.

The peer check before the password prompt is `$dccore.peerok(password)`:
mIRC passes an identifier parameters only in parentheses, so the check has
to be written that way for a failed one to stop the prompt branch.

mIRC cannot run here. These tests RUN the script's own statements - on
^*:CHAT, dccore.line, dccore.peerok, dccore.abandon, dccore.set,
dccore.timers.off, dccore.typedpass, both on *:INPUT handlers, /dccore and
dccore.askraw - with the evaluator of
tests/test_the_start_page_says_when_the_bot_is_offline.py, against fake
tables and a fake chat. Identifiers it does not know are refused loudly.
"""

import fnmatch
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_the_start_page_says_when_the_bot_is_offline import (  # noqa: E402
    BOT, Mirc, _Return, body_of, roll_changelog, split_words)
from test_the_mirc_settings_window import collapse, mirc_enc  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Placeholders, nobody's real host, token or password.
HOST = "*!*@bot.example.invalid"
ELSEWHERE = "*!*@elsewhere.example.invalid"
TOKEN = "tok-example-0000"
PASSWORD = "example-password"
LOGIN = "Entering DCC Chat Admin Interface"
PROMPT = "Enter Your Password:"
BANNER = "Welcome to the example console"
FEED = "DCCORE STATUS x"
IGNORED = "Ignored a login line that came before the password prompt was answered."

# Every state a chat can be in where the prompt has not been answered.
NOT_ANSWERED = (None, "opening", "waiting", "banner", "password", "closed", "taken")

RUN = ("dccore.line", "dccore.peerok", "dccore.abandon", "dccore.set", "dccore.timers.off",
       "dccore.typedpass", "dccore.sendpass")

# A password #1273 is about: runs of spaces, and one at either end.
SPACED = "  example  pass word "


class Chat(Mirc):
    """The evaluator, with what a chat event needs: $nick, $address, a
    halt that stops the event, and the `iswm` operator."""

    def __init__(self, host=HOST, token=TOKEN, bothost=HOST, **live):
        super().__init__(real=RUN, stubs={
            "strip": lambda text: text,
            "address": lambda nick, kind: self.host if nick.lower() == BOT.lower() else "",
            "dccore.ver": lambda: "1.20.0",
            "dccore.client": lambda: "dccore.mrc-0000",
            "dccore.prompt": lambda: ">",
            "left": lambda text, n: text[:int(n)],
            "input": self.input,
            "editbox": lambda window: self.editboxes.get(window, ""),
            "len": lambda text: str(len(text)),
            "dccore.sw.enc": mirc_enc,
        })
        self.host = host
        self.nick = BOT
        self.answer = ""
        self.asked = []
        self.editboxes = {}
        self.table("dccore").update({"bot": BOT, "wantopen": "1"})
        if token:
            self.table("dccore")["token"] = token
        if bothost:
            self.table("dccore")["bothost"] = bothost
        self.chats.add(BOT)
        # what dccore.connect leaves behind for a dial (byhand=1: the operator's own)
        defaults = dict(state="opening", byhand=0, tokentried=0, typed=0, vetted=0)
        defaults.update(live)
        self.live(**{k: v for k, v in defaults.items() if v is not None})
        if defaults.get("state") is None:
            self.table("dccore.live").pop("state", None)

    def input(self, prompt, flags, title):
        self.asked.append(flags)
        return self.answer

    def identifier(self, name, raw, frame):
        if name == "nick" and raw is None:
            return self.nick
        return super().identifier(name, raw, frame)

    def command(self, text, frame):
        if text.split(" ", 1)[0] == "halt":
            self.calls.append(("halt", ""))
            raise _Return("")
        super().command(text, frame)

    def part(self, text, frame):
        inner = text.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        words = split_words(inner)
        if "iswm" in words[1:]:
            k = words.index("iswm")
            pattern = self.value(" ".join(words[:k]), frame)
            subject = self.value(" ".join(words[k + 1:]), frame)
            return fnmatch.fnmatchcase(subject.lower(), pattern.lower())
        return super().part(text, frame)

    def event(self, header, line):
        try:
            self.run(body_of(header), {"args": line.split(" "), "vars": {}})
        except _Return:
            pass

    def hear(self, *lines):
        """Each line as the bot's chat delivers it."""
        for line in lines:
            self.event("on ^*:CHAT:*: {", line)

    def typed_in(self, window, line):
        """`line` typed in `window`: its editbox holds it as typed, the
        event's parameters hold it closed up."""
        self.editboxes = {window: line}
        self.event("on *:INPUT:%s: {" % window, collapse(line))
        self.editboxes = {}

    def type(self, line):
        """A line typed in @DCCore."""
        self.typed_in("@DCCore", line)

    def type_console(self, line):
        """A line typed in @DCCore-console."""
        self.typed_in("@DCCore-console", line)

    def raw(self, line):
        """/dccore raw <line>, typed in @DCCore: the editbox holds the whole
        command, the alias gets its parameters closed up."""
        self.editboxes = {"@DCCore": "/dccore raw " + line}
        self.call("dccore", ["raw"] + collapse(line).split(" "))
        self.editboxes = {}

    def menu(self, line):
        """The menu's console command, answered with `line` (a /var holds
        the answer closed up)."""
        self.answer = collapse(line)
        self.call("dccore.askraw", [])

    @property
    def state(self):
        return self.table("dccore.live").get("state", "")

    def sent(self):
        return [args for head, args in self.calls if head in ("dccore.send", "msg")]

    def said(self):
        return [args for head, args in self.calls if head == "dccore.sys"]

    def structured(self):
        return [args for head, args in self.calls if head == "dccore.structured"]


class ALoginLineBeforeThePromptIsAnsweredIsIgnored(unittest.TestCase):

    def assertLeft(self, chat):
        """Left as a chat whose peer check said no: closed, not redialled."""
        self.assertEqual(chat.state, "closed")
        self.assertIn(IGNORED, " ".join(chat.said()))
        self.assertIn(("window", "-c =" + BOT), chat.calls)
        self.assertEqual(chat.table("dccore")["wantopen"], "0")
        self.assertIn(("timer", "timerdccoreRetry off"), chat.calls)
        self.assertNotIn("Logged in", " ".join(chat.said()))

    def test_as_the_first_line_of_a_chat(self):
        for byhand in (0, 1):
            for token in (TOKEN, None):
                with self.subTest(byhand=byhand, token=token):
                    chat = Chat(token=token, byhand=byhand)
                    chat.hear(LOGIN)
                    self.assertLeft(chat)
                    self.assertEqual(chat.sent(), [])

    def test_after_a_banner_with_no_prompt(self):
        chat = Chat()
        chat.hear(BANNER, "", LOGIN)
        self.assertLeft(chat)
        self.assertEqual(chat.sent(), [])

    def test_right_after_the_prompt_with_nothing_typed(self):
        """No token in use: the prompt asks for the password (in a chat
        opened by hand with no peer check), and nothing answers it."""
        for byhand, host, bothost in ((1, "", None), (1, ELSEWHERE, HOST), (0, HOST, HOST)):
            with self.subTest(byhand=byhand, host=host):
                chat = Chat(token=None, byhand=byhand, host=host, bothost=bothost)
                chat.hear(BANNER, PROMPT)
                self.assertEqual(chat.state, "password")
                chat.hear(LOGIN)
                self.assertLeft(chat)
                chat.hear(FEED)
                self.assertNotEqual(chat.state, "in")
                self.assertIn(IGNORED, " ".join(chat.said()))
                self.assertEqual(chat.table("dccore")["wantopen"], "0")
                self.assertEqual(chat.sent(), [])
                self.assertEqual(chat.structured(), [])

    def test_no_dccore_line_is_read_after_it(self):
        chat = Chat()
        chat.hear(LOGIN, FEED, "DCCORE STATUS y")
        self.assertNotEqual(chat.state, "in")
        self.assertEqual(chat.structured(), [])
        self.assertEqual(chat.sent(), [])

    def test_in_every_state_but_auth_and_in(self):
        for state in NOT_ANSWERED:
            with self.subTest(state=state):
                chat = Chat(state=state)
                chat.call("dccore.line", LOGIN.split(" "))
                self.assertLeft(chat)
                self.assertEqual(chat.sent(), [])

    def test_after_a_peer_check_that_said_no_to_the_token(self):
        """The token is withheld and the chat left; a login line in it after
        that still does not log in."""
        chat = Chat(host=ELSEWHERE)
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "closed")
        self.assertEqual(chat.sent(), [])
        chat.hear(LOGIN)
        self.assertEqual(chat.state, "closed")
        self.assertIn(IGNORED, " ".join(chat.said()))
        chat.hear(FEED)
        self.assertNotEqual(chat.state, "in")
        self.assertEqual(chat.sent(), [])
        self.assertEqual(chat.structured(), [])

    def test_the_line_is_not_shown_as_the_bots(self):
        chat = Chat()
        chat.hear(LOGIN)
        self.assertNotIn(("dccore.echo", LOGIN), chat.calls)


class ThePeerCheckBeforeThePasswordPrompt(unittest.TestCase):
    """No token, a chat the script dialled by itself, the nick at another
    host than the bot's: the prompt is not passed on, and the chat is left."""

    def test_a_failed_check_stops_the_prompt_branch(self):
        chat = Chat(token=None, host=ELSEWHERE, bothost=HOST, byhand=0)
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "closed")
        said = " ".join(chat.said())
        self.assertIn("NOT asking for the password", said)
        self.assertNotIn("Type the admin password", said)
        self.assertNotEqual(chat.table("dccore.live").get("vetted"), "1")
        self.assertEqual(chat.table("dccore")["wantopen"], "0")
        chat.hear(LOGIN, FEED)
        self.assertNotEqual(chat.state, "in")
        self.assertEqual(chat.structured(), [])
        self.assertEqual(chat.sent(), [])

    def test_an_unknown_host_also_stops_it(self):
        chat = Chat(token=None, host="", bothost=None, byhand=0)
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "closed")
        self.assertIn("Not asking for the password yet", " ".join(chat.said()))
        self.assertNotIn("Type the admin password", " ".join(chat.said()))

    def test_the_check_is_told_what_it_withholds(self):
        """peerok's $1 reaches it: the window names the password, not the
        token."""
        chat = Chat(token=None, host=ELSEWHERE, bothost=HOST, byhand=0)
        chat.hear(BANNER, PROMPT)
        self.assertNotIn("sending the token", " ".join(chat.said()))


class ALoginAfterThePromptWasAnsweredLogsIn(unittest.TestCase):

    def assertLoggedIn(self, chat):
        self.assertEqual(chat.state, "in")
        self.assertIn("hello dccore.mrc 1.20.0", chat.sent())
        self.assertNotIn(IGNORED, " ".join(chat.said()))
        self.assertEqual(chat.table("dccore")["wantopen"], "1")

    def test_the_stored_token_after_the_peer_check(self):
        chat = Chat()
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "auth")
        self.assertEqual(chat.sent(), [TOKEN])
        chat.hear("", LOGIN, 'For help type "help"', FEED)
        self.assertLoggedIn(chat)
        self.assertEqual(chat.structured(), ["STATUS x"])

    def test_a_password_typed_in_a_chat_opened_by_hand(self):
        """/dccore connect with no token: the operator's own chat asks with
        no peer check, and the typed password logs in."""
        chat = Chat(token=None, bothost=None, host="", byhand=1)
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "password")
        self.assertEqual(chat.sent(), [])
        chat.type(PASSWORD)
        self.assertEqual(chat.state, "auth")
        self.assertEqual(chat.sent(), [PASSWORD])
        chat.hear(LOGIN)
        self.assertLoggedIn(chat)

    def test_a_password_typed_in_a_chat_the_script_dialled(self):
        chat = Chat(token=None)
        chat.hear(BANNER, PROMPT)
        self.assertEqual(chat.state, "password")
        self.assertEqual(chat.table("dccore.live")["vetted"], "1")
        chat.type(PASSWORD)
        chat.hear(LOGIN)
        self.assertLoggedIn(chat)

    def test_a_refused_token_then_a_typed_password(self):
        chat = Chat()
        chat.hear(BANNER, PROMPT, "Incorrect Password.")
        self.assertEqual(chat.state, "password")
        chat.hear(PROMPT)
        self.assertEqual(chat.sent(), [TOKEN])
        chat.type(PASSWORD)
        chat.hear(LOGIN)
        self.assertLoggedIn(chat)

    def test_a_pairing_by_hand_sends_its_pair_after_the_login(self):
        chat = Chat(token=None, byhand=1, pairing=1)
        chat.hear(BANNER, PROMPT)
        chat.type(PASSWORD)
        chat.hear(LOGIN)
        self.assertLoggedIn(chat)
        self.assertIn("pair dccore.mrc-0000 1.20.0", chat.sent())

    def test_only_state_auth_logs_in(self):
        chat = Chat(state="auth")
        chat.call("dccore.line", LOGIN.split(" "))
        self.assertLoggedIn(chat)
        chat = Chat(state="password")
        chat.call("dccore.line", LOGIN.split(" "))
        self.assertEqual(chat.state, "closed")
        self.assertIn(IGNORED, " ".join(chat.said()))


class EveryWayOfTypingThePasswordAnswersThePrompt(unittest.TestCase):
    """@DCCore, @DCCore-console, /dccore raw and the menu's console command:
    a line sent at the prompt moves the chat to auth, typed by hand, goes
    through dccore.sendpass, and is never shown."""

    ROUTES = ("type", "type_console", "raw", "menu")

    def test_each_route_then_the_login(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                chat = Chat(token=None, bothost=None, host="", byhand=1)
                chat.hear(BANNER, PROMPT)
                getattr(chat, route)(PASSWORD)
                self.assertEqual(chat.state, "auth")
                self.assertEqual(chat.table("dccore.live")["typed"], "1")
                self.assertEqual(chat.sent(), [PASSWORD])
                window = {"type": "@DCCore", "type_console": "@DCCore-console"}.get(route, "-")
                self.assertIn(("dccore.sendpass", window + " " + PASSWORD), chat.calls)
                shown = [args for head, args in chat.calls if head in ("echo", "dccore.echo", "dccore.sys")]
                self.assertFalse([s for s in shown if PASSWORD in s], shown)
                chat.hear(LOGIN)
                self.assertEqual(chat.state, "in")

    def test_the_console_window_shows_it_masked(self):
        chat = Chat(token=None, bothost=None, host="", byhand=1)
        chat.hear(BANNER, PROMPT)
        chat.type_console(PASSWORD)
        self.assertIn(("echo", "-ti2 @DCCore-console > ********"), chat.calls)
        self.assertEqual(chat.calls[-1], ("halt", ""))

    def test_once_logged_in_each_route_sends_the_line_as_it_is(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                chat = Chat(state="in")
                getattr(chat, route)("status")
                self.assertEqual(chat.state, "in")
                self.assertEqual(chat.sent(), ["status"])
                self.assertNotIn("dccore.typedpass", [head for head, _args in chat.calls])

    def test_the_console_window_still_shows_a_command_and_masks_unlock(self):
        chat = Chat(state="in")
        chat.type_console("status")
        self.assertIn(("echo", "-ti2 @DCCore-console > status"), chat.calls)
        chat.type_console("unlock " + PASSWORD)
        self.assertIn(("echo", "-ti2 @DCCore-console > unlock ********"), chat.calls)


class APasswordKeepsItsSpacesWhereTheEditboxHasThem(unittest.TestCase):
    """#1273 on each route: the window it was typed in is the editbox
    dccore.sendpass reads."""

    def spaced(self, route):
        chat = Chat(token=None, bothost=None, host="", byhand=1)
        chat.hear(BANNER, PROMPT)
        getattr(chat, route)(SPACED)
        self.assertEqual(chat.state, "auth")
        return chat.sent()

    def test_typed_in_either_window_it_goes_encoded_as_typed(self):
        for route in ("type", "type_console"):
            with self.subTest(route=route):
                self.assertEqual(self.spaced(route), ["DCCORE PASSWORD " + mirc_enc(SPACED)])

    def test_the_console_window_reads_its_own_editbox(self):
        """Not @DCCore's: with @DCCore's editbox holding something else, the
        console window's password still keeps its spaces."""
        chat = Chat(token=None, bothost=None, host="", byhand=1)
        chat.hear(BANNER, PROMPT)
        chat.editboxes = {"@DCCore": "something else", "@DCCore-console": SPACED}
        chat.event("on *:INPUT:@DCCore-console: {", collapse(SPACED))
        self.assertEqual(chat.sent(), ["DCCORE PASSWORD " + mirc_enc(SPACED)])

    def test_raw_and_the_menu_send_it_closed_up(self):
        """No editbox holds the password alone there: the line goes as mIRC
        gives it, as ADMIN-CONSOLE.md says."""
        for route in ("raw", "menu"):
            with self.subTest(route=route):
                self.assertEqual(self.spaced(route), [collapse(SPACED)])

    def test_a_plain_password_goes_as_it_is_on_every_route(self):
        for route in EveryWayOfTypingThePasswordAnswersThePrompt.ROUTES:
            with self.subTest(route=route):
                chat = Chat(token=None, bothost=None, host="", byhand=1)
                chat.hear(BANNER, PROMPT)
                getattr(chat, route)(PASSWORD)
                self.assertEqual(chat.sent(), [PASSWORD])


class TheMenuHidesAPasswordAsItIsTyped(unittest.TestCase):

    def asked(self, state):
        chat = Chat(state=state)
        chat.menu("status")
        return chat.asked

    def test_at_the_password_prompt_the_box_is_a_password_box(self):
        self.assertEqual(self.asked("password"), ["eop"])

    def test_any_other_time_it_is_the_plain_box(self):
        for state in ("in", "auth", "banner", "closed", None):
            with self.subTest(state=state):
                self.assertEqual(self.asked(state), ["eo"])


class ALoggedInChatIsLeftAlone(unittest.TestCase):

    def test_a_second_login_line_changes_nothing(self):
        chat = Chat()
        chat.hear(BANNER, PROMPT, LOGIN)
        before = (dict(chat.table("dccore.live")), dict(chat.table("dccore")), len(chat.sent()))
        calls = len(chat.calls)
        chat.call("dccore.line", LOGIN.split(" "))
        self.assertEqual(chat.state, "in")
        self.assertEqual((dict(chat.table("dccore.live")), dict(chat.table("dccore")), len(chat.sent())), before)
        self.assertEqual(chat.calls[calls:], [])


class TheStructuredFeedNeedsTheLogin(unittest.TestCase):

    def test_only_state_in_reads_a_dccore_line(self):
        for state in NOT_ANSWERED + ("auth", "in"):
            with self.subTest(state=state):
                chat = Chat(state=state)
                chat.call("dccore.line", FEED.split(" "))
                self.assertEqual(chat.structured(), ["STATUS x"] if state == "in" else [])


class TheEvaluatorReadsTheseLines(unittest.TestCase):
    """What the scenarios above lean on."""

    def test_iswm_and_halt(self):
        chat = Chat()
        frame = {"args": [], "vars": {"%t": "Session taken over from 192.0.2.1"}}
        self.assertTrue(chat.cond("(Session taken over from * iswm %t)", frame))
        self.assertFalse(chat.cond("(Unknown command: hello* iswm %t)", frame))
        chat.live(state="password")
        chat.type(PASSWORD)
        self.assertEqual(chat.calls[-1], ("halt", ""))
        self.assertEqual(chat.sent(), [PASSWORD])

    def test_an_identifier_gets_parameters_only_in_parentheses(self):
        """As in mIRC: `$alias word` runs the alias with no parameters and
        leaves `word` beside its value, so `(!$alias word)` is false
        whatever the alias returns."""
        frame = {"args": [], "vars": {}}
        for written, failed in (("(!$dccore.peerok(password))", True), ("(!$dccore.peerok password)", False)):
            with self.subTest(written=written):
                chat = Chat(token=None, host=ELSEWHERE, bothost=HOST)
                self.assertEqual(chat.cond(written, frame), failed)
                said = " ".join(chat.said())
                self.assertIn("asking for the password" if failed else "sending the token", said)

    def test_a_chat_from_another_nick_is_not_read(self):
        chat = Chat(state="auth")
        chat.nick = "SomeoneElseZ"
        chat.hear(LOGIN)
        self.assertEqual(chat.state, "auth")
        self.assertEqual(chat.calls, [])


class TheFragments(unittest.TestCase):

    def test_they_roll(self):
        _fragments, problems = roll_changelog.load(REPO_ROOT)
        self.assertEqual([p for p in problems if p.startswith("585-login-after-prompt")], [])


if __name__ == "__main__":
    unittest.main()
