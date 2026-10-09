"""A timed ignore (#1206): the operator drops one nick's requests for N minutes.

It is a timed ban the operator sets by hand - the same entry in
config.banned_users and the same bans.txt - so what is checked here is the
part that is new (the setting, the refusals, lifting it) and that the old
machinery really does carry it: dropped while it runs, free after, kept
across a restart, listed with the bans.
"""

import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402
import security  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_dccore_downloads_window import make_session  # noqa: E402


def read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class Base(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        config.banned_users.clear()
        self.addCleanup(config.banned_users.clear)
        self.set_config(NICKNAME="TheBot")


class IgnoringANick(Base):

    def test_the_nick_is_dropped_while_it_runs(self):
        ok, _ = security.ignore_user("Dave", 30)
        self.assertTrue(ok)
        self.assertFalse(security.check_user_status("dave"))
        self.assertFalse(security.check_user_status("DAVE"))

    def test_it_runs_for_the_minutes_asked(self):
        before = time.time()
        security.ignore_user("dave", 30)
        self.assertAlmostEqual(config.banned_users["dave"] - before, 1800, delta=5)
        self.assertAlmostEqual(security.ban_seconds_left("dave"), 1800, delta=5)

    def test_the_nick_is_let_through_when_it_ends(self):
        security.ignore_user("dave", 1)
        config.banned_users["dave"] = time.time() - 1
        self.assertTrue(security.check_user_status("dave"))
        self.assertNotIn("dave", config.banned_users)
        self.assertEqual(security.ban_seconds_left("dave"), 0)

    def test_it_is_kept_across_a_restart(self):
        security.ignore_user("dave", 30)
        until = config.banned_users["dave"]
        config.banned_users.clear()
        db.load_bans_from_file()
        self.assertAlmostEqual(config.banned_users["dave"], until, delta=1)
        self.assertFalse(security.check_user_status("dave"))

    def test_one_that_ended_while_the_bot_was_down_is_free_after_the_restart(self):
        security.ignore_user("dave", 1)
        config.banned_users["dave"] = time.time() - 5
        db.save_bans_to_file()
        config.banned_users.clear()
        db.load_bans_from_file()
        self.assertTrue(security.check_user_status("dave"))

    def test_other_nicks_are_not_touched(self):
        security.ignore_user("dave", 30)
        self.assertTrue(security.check_user_status("erin"))

    def test_the_pending_replies_are_dropped(self):
        config.send_queue["dave"] = ["one", "two"]
        config.send_queue["erin"] = ["kept"]
        self.addCleanup(config.send_queue.clear)
        security.ignore_user("dave", 30)
        self.assertNotIn("dave", config.send_queue)
        self.assertEqual(config.send_queue["erin"], ["kept"])

    def test_the_queued_files_are_left_alone(self):
        config.dcc_queue["dave"] = [{"file": "a.mp3"}]
        self.addCleanup(config.dcc_queue.clear)
        security.ignore_user("dave", 30)
        self.assertEqual(len(config.dcc_queue["dave"]), 1)

    def test_a_second_ignore_replaces_the_first_longer_or_shorter(self):
        security.ignore_user("dave", 60)
        security.ignore_user("dave", 5)
        self.assertAlmostEqual(security.ban_seconds_left("dave"), 300, delta=5)

    def test_what_it_says_names_the_length(self):
        ok, message = security.ignore_user("dave", 90)
        self.assertTrue(ok)
        self.assertIn("dave", message.lower())
        self.assertIn("1.5 hours", message)


class WhatIsRefused(Base):

    def refused(self, nick, minutes):
        ok, message = security.ignore_user(nick, minutes)
        self.assertFalse(ok, (nick, minutes))
        self.assertEqual(config.banned_users, {}, (nick, minutes))
        return message

    def test_a_pattern_is_not_a_nick(self):
        for text in ("*!*@host", "da ve", "", "dave!x@y", "#chan", "9dave"):
            self.assertIn("not a nick", self.refused(text, 5), text)

    def test_the_bots_own_nick(self):
        self.assertIn("own nick", self.refused("thebot", 5))

    def test_the_length_is_whole_minutes_in_range(self):
        for minutes in (0, -3, "", "soon", "1.5", None, security.IGNORE_MAX_MINUTES + 1):
            self.assertIn("minutes", self.refused("dave", minutes), minutes)

    def test_the_longest_is_allowed(self):
        ok, _ = security.ignore_user("dave", security.IGNORE_MAX_MINUTES)
        self.assertTrue(ok)

    def test_the_length_may_come_as_text(self):
        ok, _ = security.ignore_user("dave", " 15 ")
        self.assertTrue(ok)


class LiftingIt(Base):

    def test_it_ends_at_once_and_the_file_forgets_it(self):
        security.ignore_user("dave", 30)
        ok, _ = security.lift_ban("Dave")
        self.assertTrue(ok)
        self.assertTrue(security.check_user_status("dave"))
        config.banned_users.clear()
        db.load_bans_from_file()
        self.assertNotIn("dave", config.banned_users)

    def test_a_nick_with_nothing_to_lift_says_so(self):
        ok, message = security.lift_ban("dave")
        self.assertFalse(ok)
        self.assertIn("not ignored", message)

    def test_a_flood_ban_is_lifted_the_same_way(self):
        config.banned_users["dave"] = time.time() + 3000
        ok, _ = security.lift_ban("dave")
        self.assertTrue(ok)
        self.assertTrue(security.check_user_status("dave"))


class FromTheConsole(Base):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def test_ignore_and_unignore(self):
        adminchat._cmd_ignore(self.session, "dave 20")
        self.assertIn("dave", config.banned_users)
        self.assertIn("20 minutes", self.session.sent[-1])
        adminchat._cmd_unignore(self.session, "dave")
        self.assertNotIn("dave", config.banned_users)

    def test_a_wrong_line_gets_the_usage(self):
        for args in ("", "dave", "dave 5 extra"):
            adminchat._cmd_ignore(self.session, args)
            self.assertIn("Usage: ignore", self.session.sent[-1], args)
        adminchat._cmd_unignore(self.session, "")
        self.assertIn("Usage: unignore", self.session.sent[-1])
        self.assertEqual(config.banned_users, {})

    def test_a_refusal_is_said_not_swallowed(self):
        adminchat._cmd_ignore(self.session, "thebot 5")
        self.assertIn("own nick", self.session.sent[-1])

    def test_bans_lists_it_with_the_time_left(self):
        adminchat._cmd_ignore(self.session, "dave 20")
        adminchat._cmd_bans(self.session, "")
        listed = [line for line in self.session.sent if "dave" in line and "left" in line]
        self.assertTrue(listed, self.session.sent)
        self.assertRegex(listed[-1], r"1\d minutes left|20 minutes left")

    def test_both_commands_are_registered_and_documented(self):
        self.assertIn("ignore", adminchat.COMMANDS)
        self.assertIn("unignore", adminchat.COMMANDS)
        text = read("docs", "ADMIN-CONSOLE.md")
        self.assertIn("`ignore <nick> <minutes>`", text)
        self.assertIn("`unignore <nick>`", text)


class ClearAndIgnoreOnlyClearsIfTheIgnoreTook(Base):
    """#1247: dccore.mrc's "Clear the queue of ... and ignore for..." used
    to send `ignore` and `clearqueue` as two separate, unconditional
    commands - a nick the ignore refused (the bot's own nick, or one
    outside the pattern `ignore` accepts) still had its queue cleared
    regardless, as if the ignore had worked. `clearandignore` is one
    console command, so the clear only ever runs once the ignore itself
    has actually succeeded."""

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def test_a_refused_ignore_never_touches_the_queue(self):
        with mock.patch.object(adminchat, "_run_detached") as detached:
            adminchat._cmd_clearandignore(self.session, "thebot 5")
        self.assertIn("own nick", self.session.sent[-1])
        detached.assert_not_called()

    def test_a_successful_ignore_goes_on_to_clear(self):
        with mock.patch.object(adminchat, "_run_detached") as detached:
            adminchat._cmd_clearandignore(self.session, "dave 20")
        self.assertIn("dave", config.banned_users)
        self.assertIn("20 minutes", self.session.sent[-2])
        self.assertIn("Clearing the queue for dave", self.session.sent[-1])
        detached.assert_called_once()
        self.assertEqual(detached.call_args[0][1], "clearqueue")

    def test_a_wrong_line_gets_the_usage(self):
        for args in ("", "dave", "dave 5 extra"):
            adminchat._cmd_clearandignore(self.session, args)
            self.assertIn("Usage: clearandignore", self.session.sent[-1], args)
        self.assertEqual(config.banned_users, {})

    def test_it_is_registered_and_documented(self):
        self.assertIn("clearandignore", adminchat.COMMANDS)
        text = read("docs", "ADMIN-CONSOLE.md")
        self.assertIn("`clearandignore <nick> <minutes>`", text)

    def test_dccore_mrc_sends_the_combined_command(self):
        """The mIRC side of the same fix: one send, not two."""
        script = read("scripts", "mirc", "dccore.mrc")
        start = script.index("alias dccore.clearignore {")
        body = script[start:script.index("\n}", start)]
        self.assertIn("dccore.send clearandignore $1 %m", body)


class FromTheDashboard(Base):

    def test_ignore_answers_with_how_long(self):
        status, result = webserver.build_ignore_result({"nick": "Dave", "minutes": 10})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"], "dave")
        self.assertAlmostEqual(result["seconds_left"], 600, delta=5)

    def test_a_refusal_is_a_400_with_the_reason(self):
        status, result = webserver.build_ignore_result({"nick": "*!*@x", "minutes": 10})
        self.assertEqual(status, 400)
        self.assertIn("not a nick", result["error"])
        status, result = webserver.build_ignore_result({"nick": "dave"})
        self.assertEqual(status, 400)

    def test_unignore_is_a_404_for_a_nick_with_nothing(self):
        status, _ = webserver.build_unignore_result({"nick": "dave"})
        self.assertEqual(status, 404)
        webserver.build_ignore_result({"nick": "dave", "minutes": 10})
        status, _ = webserver.build_unignore_result({"nick": "dave"})
        self.assertEqual(status, 200)
        self.assertNotIn("dave", config.banned_users)

    def test_the_queue_row_says_how_long_is_left(self):
        config.dcc_queue["dave"] = [{"file": "a.mp3"}]
        config.dcc_queue["erin"] = [{"file": "b.mp3"}]
        self.addCleanup(config.dcc_queue.clear)
        webserver.build_ignore_result({"nick": "dave", "minutes": 10})
        rows = {row["user"]: row for row in webserver.build_queue_payload()}
        self.assertAlmostEqual(rows["dave"]["ignored_seconds"], 600, delta=5)
        self.assertEqual(rows["erin"]["ignored_seconds"], 0)
        single = webserver.build_queue_payload(user="dave")
        self.assertGreater(single["ignored_seconds"], 0)

    def test_the_routes_exist(self):
        text = read("src", "webserver.py")
        self.assertIn('"/api/ignore", methods=["POST"]', text)
        self.assertIn('"/api/unignore", methods=["POST"]', text)

    def test_the_page_draws_the_buttons_and_says_it_in_every_language(self):
        import json
        js = read("web", "app.js")
        for needle in ("ignore-btn", "lift-btn", "/api/ignore", "/api/unignore", "ignored_seconds"):
            self.assertIn(needle, js, needle)
        for lang in ("en", "es", "fr"):
            strings = json.loads(read("web", "lang", lang + ".json"))
            for key in ("queue.actions", "queue.ignore", "queue.lift", "queue.ignorePrompt",
                        "queue.ignoredLeft", "queue.ignoreFailed"):
                self.assertTrue(strings.get(key), (lang, key))


if __name__ == "__main__":
    unittest.main()
