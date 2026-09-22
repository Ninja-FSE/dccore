"""#591: a nickname IRC would refuse was accepted, and the bot quietly ran as DCCore_.

The setup page refused only a space or a leading #&:digit; configure.py and the
settings file validated nothing. A nick with a Greek letter, "!", ".", "*" or a
leading "-" was written, the server answered 432 "Erroneous Nickname" (which the
handshake reads as 433, "in use"), and the bot switched to the shipped ALT_NICKNAME
"DCCore_" while reporting that the nick was taken; the admin nick could never
match a real one. Now every path that writes a nickname checks it against what the
protocol allows and tells the operator what is wrong.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import configure  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.test_set_it_up_in_the_browser import GOOD  # noqa: E402


GOOD_NICKS = ["MusicBot", "DCCore_", "Music-Bot", "[Bot]", "bot`", "_bot", "a", "Bot{1}", "x|y", "Bot^", "Music\\Bot",
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]
BAD_NICKS = {
    "a space": "Music Bot",
    "a Greek nick": "Βοτ",
    "an exclamation mark": "Bot!",
    "a dot": "Music.Bot",
    "a star": "Bot*",
    "a comma": "a,b",
    "a colon": "a:b",
    "a hash": "#Bot",
    "an at sign": "a@b",
    "a leading hyphen": "-Bot",
    "a leading digit": "1Bot",
    "a tab": "Bot\tone",
    "an accent": "Músic",
}


class TheRule(unittest.TestCase):

    def test_ordinary_nicks_pass(self):
        for nick in GOOD_NICKS:
            self.assertIsNone(settings_file.nick_problem(nick), nick)

    def test_each_bad_one_is_refused_with_a_reason(self):
        for label, nick in BAD_NICKS.items():
            problem = settings_file.nick_problem(nick)
            self.assertTrue(problem, label)
            self.assertIsInstance(problem, str)

    def test_the_reason_names_the_offending_character(self):
        self.assertIn("'!'", settings_file.nick_problem("Bot!"))
        self.assertIn("space", settings_file.nick_problem("Music Bot"))
        self.assertIn("not ASCII", settings_file.nick_problem("Βοτ"))
        self.assertIn("starts with '-'", settings_file.nick_problem("-Bot"))

    def test_a_line_break_is_named_as_one(self):
        """A value with a newline would be read as a second setting."""
        self.assertIn("line break", settings_file.nick_problem("DCCore\nMAX_DCC_SLOTS = 99"))
        self.assertIn("line break", settings_file.nick_problem("a\rb"))

    def test_empty_is_refused(self):
        self.assertTrue(settings_file.nick_problem(""))

    def test_a_list_is_each_nick_checked(self):
        self.assertIsNone(settings_file.nicks_problem("Op, Op2,Op3"))
        self.assertIn("'!'", settings_file.nicks_problem("Op, Bad!"))
        self.assertTrue(settings_file.nicks_problem(" , "))

    def test_a_trailing_comma_is_not_a_nick(self):
        self.assertIsNone(settings_file.nicks_problem("Op,"))


class ThroughTheSettingsFile(unittest.TestCase):
    """Every path into config - settings.conf, the dashboard's Settings page,
    apply_settings_changes - comes through coerce()."""

    def test_a_bad_nick_is_a_bad_setting_not_a_silent_fallback(self):
        for name in ("NICKNAME", "ALT_NICKNAME", "ADMIN_NICK"):
            with self.assertRaises(ValueError, msg=name) as caught:
                settings_file.coerce(name, "Music Bot", "x", str)
            self.assertIn("not a valid IRC nickname", str(caught.exception))

    def test_a_good_one_is_kept_as_typed(self):
        self.assertEqual(settings_file.coerce("NICKNAME", "MusicBot", None, str), "MusicBot")

    def test_admin_nick_may_be_a_list(self):
        self.assertEqual(settings_file.coerce("ADMIN_NICK", "Op, Op2", None, str), "Op, Op2")
        with self.assertRaises(ValueError):
            settings_file.coerce("ADMIN_NICK", "Op, Bad Nick", None, str)

    def test_blank_is_still_handled_as_before(self):
        """Blank NICKNAME means unset (REQUIRED reports it); not a nick error."""
        self.assertIsNone(settings_file.coerce("NICKNAME", "", None, str))

    def test_other_settings_are_not_affected(self):
        self.assertEqual(settings_file.coerce("SERVER", "irc.example.net", "x", str), "irc.example.net")
        self.assertEqual(settings_file.coerce("CHANNEL", "#a,#b", "x", str), "#a,#b")

    def test_a_bad_nick_in_settings_conf_is_reported_and_not_applied(self):
        namespace = {"NICKNAME": None, "ADMIN_NICK": None, "SERVER": "s"}
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.conf")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("NICKNAME = Music Bot\nADMIN_NICK = Op\n")
            logs = []
            with mock.patch.object(settings_file, "is_overridable", lambda key, value: True), \
                    mock.patch.object(settings_file, "declared_types", lambda ns: {"NICKNAME": str, "ADMIN_NICK": str}):
                report = settings_file.apply_to(namespace, path=path, log=logs.append)
        self.assertEqual([key for key, _ in report["bad"]], ["NICKNAME"])
        self.assertIsNone(namespace["NICKNAME"])
        self.assertEqual(namespace["ADMIN_NICK"], "Op")


class OnTheSetupPage(unittest.TestCase):

    def check(self, **overrides):
        form = dict(GOOD)
        form.update(overrides)
        return webserver.validate_setup_form(form)

    def test_each_bad_nickname_is_refused_and_named(self):
        for label, nick in BAD_NICKS.items():
            _, _, errors = self.check(NICKNAME=nick)
            names = [name for name, _ in errors]
            self.assertIn("NICKNAME", names, label)

    def test_a_good_nick_and_admin_pass(self):
        changes, _, errors = self.check(NICKNAME="Music-Bot", ADMIN_NICK="Op,Op2")
        self.assertEqual(errors, [])
        self.assertEqual(changes["NICKNAME"], "Music-Bot")

    def test_a_bad_admin_nick_is_refused(self):
        _, _, errors = self.check(ADMIN_NICK="Χρήστος")
        self.assertEqual([name for name, _ in errors], ["ADMIN_NICK"])
        self.assertIn("not ASCII", errors[0][1])

    def test_the_message_says_what_is_wrong(self):
        _, _, errors = self.check(NICKNAME="Bot!")
        self.assertIn("'!'", errors[0][1])


class InTheTerminal(unittest.TestCase):

    def ask(self, answers, **kwargs):
        with mock.patch("builtins.input", side_effect=answers) as prompt, mock.patch("builtins.print") as said:
            result = configure._ask("Nickname", check=kwargs.get("check"), default=kwargs.get("default"))
        return result, prompt.call_count, " ".join(str(c.args[0]) for c in said.call_args_list if c.args)

    def test_a_bad_answer_is_asked_again(self):
        result, asked, said = self.ask(["Music Bot", "Bot!", "MusicBot"], check=settings_file.nick_problem)
        self.assertEqual(result, "MusicBot")
        self.assertEqual(asked, 3)
        self.assertIn("That will not do", said)
        self.assertIn("space", said)

    def test_without_a_check_nothing_changes(self):
        result, asked, _ = self.ask(["Music Bot"])
        self.assertEqual((result, asked), ("Music Bot", 1))

    def test_a_bare_enter_takes_the_default_unchecked(self):
        result, asked, _ = self.ask([""], check=settings_file.nick_problem, default="DCCore")
        self.assertEqual((result, asked), ("DCCore", 1))

    def test_the_nickname_and_admin_prompts_are_checked(self):
        with open(os.path.join(REPO_ROOT, "configure.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('check=settings_file.nick_problem)', source)
        self.assertIn('check=settings_file.nicks_problem)', source)


if __name__ == "__main__":
    unittest.main()
