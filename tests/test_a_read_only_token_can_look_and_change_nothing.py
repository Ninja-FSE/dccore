"""A read-only console token (#1374): it can look and change nothing.

A paired token opened the whole console: the lock (#1264) kept settings
behind the password, but `ban`, `clearqueue`, `rehash`, `update`, `chat` and
`shutdown now` ran on a token alone, and a token sits in clear text wherever
its script keeps it. `pair <client> [version] readonly` mints one that may
run READ_ONLY_COMMANDS - an allow-list, so a command added later is refused
until someone decides it reads - and is refused everything else, `unlock`
included, with the LOCKED line a script already shows. Its hash is kept as
`readonly_hash`, so a bot from before this refuses it after a downgrade
instead of reading it as a full token. And a read-only login never takes the
console from a full session: a stats script logging in every few minutes
would otherwise close the operator's console each time.
"""

import contextlib
import io
import os
import socket
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


SENTENCE = "This console logged in with a read-only token"


class ReadOnlyCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw", iterations=1000))
        adminchat.WRONG_PASSWORD_DELAY, real = 0.0, adminchat.WRONG_PASSWORD_DELAY
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", real)
        self.addCleanup(setattr, adminchat, "_session", adminchat._session)
        self.addCleanup(setattr, adminchat, "_pending", adminchat._pending)
        adminchat._session = None
        adminchat._pending = None
        self.out = io.StringIO()

    def session(self, authenticated=True, structured=False):
        s = adminchat.Session(socket.socket(), "127.0.0.1", "SysOp", "h")
        self.addCleanup(s.close, None)
        s.authenticated = authenticated
        s.unlocked = authenticated      # as a password login is
        s.structured = structured
        return s

    def run_command(self, session, line):
        with contextlib.redirect_stdout(self.out):
            adminchat.handle_command(session, line)
        return list(session._outbox)

    def pair(self, line):
        lines = self.run_command(self.session(), "pair " + line)
        return [line.strip() for line in lines if line.startswith("  ")][0]

    def login(self, token, structured=False):
        s = self.session(authenticated=False, structured=structured)
        with contextlib.redirect_stdout(self.out):
            adminchat._check_password(s, token)
        return s

    def read_only_session(self, structured=True):
        s = self.login(self.pair("widget 1.0 readonly"), structured=structured)
        self.assertTrue(s.authenticated)
        self.assertTrue(s.read_only)
        s._outbox.clear()
        return s


class PairingReadOnly(ReadOnlyCase):

    def test_the_word_makes_a_read_only_token_kept_under_its_own_key(self):
        self.pair("widget 1.0 readonly")
        entry = db.load_admin_tokens()["widget"]
        self.assertTrue(entry["readonly_hash"].startswith("pbkdf2"))
        # A bot from before read-only tokens reads "hash" alone: with no
        # "hash" it refuses this token instead of letting it in as a full one.
        self.assertNotIn("hash", entry)

    def test_a_token_paired_without_it_is_full_as_before(self):
        self.pair("desk 1.0")
        entry = db.load_admin_tokens()["desk"]
        self.assertIn("hash", entry)
        self.assertNotIn("readonly_hash", entry)

    def test_the_word_counts_in_any_case_and_without_a_version(self):
        self.pair("widget READONLY")
        self.assertIn("readonly_hash", db.load_admin_tokens()["widget"])

    def test_readonly_is_not_taken_for_a_client_name(self):
        lines = self.run_command(self.session(), "pair readonly")
        self.assertEqual(db.load_admin_tokens(), {})
        self.assertIn("Name the client first", " ".join(lines))

    def test_the_reply_says_read_only(self):
        lines = self.run_command(self.session(), "pair widget 1.0 readonly")
        self.assertIn("Paired widget, read-only.", " ".join(lines))

    def test_unpair_lists_which_tokens_are_read_only(self):
        self.pair("widget 1.0 readonly")
        self.pair("desk 1.0")
        lines = self.run_command(self.session(), "unpair")
        widget = [line for line in lines if line.strip().startswith("widget ")][0]
        desk = [line for line in lines if line.strip().startswith("desk ")][0]
        self.assertTrue(widget.endswith(", read-only"), widget)
        self.assertNotIn("read-only", desk)

    def test_pairing_the_name_again_without_the_word_makes_it_full(self):
        first = self.pair("widget 1.0 readonly")
        second = self.pair("widget 1.0")
        self.assertEqual(adminchat._paired_token(second), ("widget", False))
        self.assertEqual(adminchat._paired_token(first), (None, False))


class LoggingIn(ReadOnlyCase):

    def test_a_read_only_token_logs_in_read_only_and_locked(self):
        s = self.login(self.pair("widget 1.0 readonly"))
        self.assertTrue(s.authenticated)
        self.assertTrue(s.read_only)
        self.assertEqual(s.paired_as, "widget")
        self.assertFalse(s.unlocked)
        self.assertIn("logged in with the read-only token paired as 'widget'", self.out.getvalue())

    def test_a_full_token_and_the_password_are_not_read_only(self):
        full = self.login(self.pair("desk 1.0"))
        self.assertTrue(full.authenticated)
        self.assertFalse(full.read_only)
        adminchat._session = None
        password = self.login("pw")
        self.assertTrue(password.authenticated)
        self.assertFalse(password.read_only)

    def test_token_matches_still_names_the_client(self):
        token = self.pair("widget 1.0 readonly")
        self.assertEqual(adminchat._token_matches(token), "widget")
        self.assertIsNone(adminchat._token_matches("not-a-token"))


class OneConsoleStillButNotTakenByAReader(ReadOnlyCase):

    def full_session_holding_the_console(self):
        full = self.session()
        adminchat._session = full
        return full

    def test_a_read_only_login_does_not_take_the_console_from_a_full_one(self):
        token = self.pair("widget 1.0 readonly")
        full = self.full_session_holding_the_console()
        reader = self.login(token)
        self.assertIs(adminchat._session, full)
        self.assertFalse(full.closed)
        self.assertFalse(reader.authenticated)
        self.assertTrue(reader.closed)
        self.assertIn("refused, that session stays", self.out.getvalue())

    def test_that_refusal_is_not_a_wrong_password(self):
        token = self.pair("widget 1.0 readonly")
        self.full_session_holding_the_console()
        reader = self.login(token)
        self.assertEqual(reader.attempts, 0)
        self.assertFalse(adminchat.is_bad_ip(reader.peer_ip))
        self.assertNotIn("Failed password", self.out.getvalue())

    def test_a_closed_full_session_does_not_hold_it(self):
        token = self.pair("widget 1.0 readonly")
        full = self.full_session_holding_the_console()
        full.close(None)
        reader = self.login(token)
        self.assertTrue(reader.authenticated)
        self.assertIs(adminchat._session, reader)

    def test_a_read_only_login_replaces_another_read_only_one(self):
        token = self.pair("widget 1.0 readonly")
        first = self.login(token)
        second = self.login(token)
        self.assertTrue(second.authenticated)
        self.assertIs(adminchat._session, second)
        self.assertTrue(first.closed)

    def test_a_full_login_replaces_a_read_only_one(self):
        reader = self.login(self.pair("widget 1.0 readonly"))
        full = self.login("pw")
        self.assertTrue(full.authenticated)
        self.assertIs(adminchat._session, full)
        self.assertTrue(reader.closed)


class WhatAReadOnlySessionMayRun(ReadOnlyCase):

    def recorders(self):
        """Every command replaced by one that records it ran."""
        ran = []
        table = {name: ((lambda name: lambda session, args: ran.append(name))(name), summary, usage)
                 for name, (_fn, summary, usage) in adminchat.COMMANDS.items()}
        patcher = mock.patch.dict(adminchat.COMMANDS, table)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ran

    def test_the_allow_list_is_the_one_decided(self):
        """Adding a command here is a decision that it only reads - make it
        in this test too."""
        self.assertEqual(adminchat.READ_ONLY_COMMANDS, {
            "status", "queue", "slots", "packing", "library", "bans", "uptime", "version",
            "lists", "dlqueue", "downloads", "hello", "consolecaps", "help", "quit"})
        self.assertEqual(adminchat.READ_ONLY_REPORTS, {"consolefeed", "checkupdates"})
        self.assertLessEqual(adminchat.READ_ONLY_COMMANDS | adminchat.READ_ONLY_REPORTS,
                             set(adminchat.COMMANDS))

    def test_every_other_command_is_refused_with_a_locked_line(self):
        s = self.read_only_session(structured=True)
        ran = self.recorders()
        refused = sorted(set(adminchat.COMMANDS) - adminchat.READ_ONLY_COMMANDS)
        self.assertIn("shutdown", refused)
        for name in refused:
            s._outbox.clear()
            self.run_command(s, f"{name} x")
            self.assertEqual(ran, [], name)
            self.assertEqual(len(s._outbox), 1, name)
            self.assertTrue(s._outbox[0].startswith(f"DCCORE LOCKED {name} {SENTENCE}"), s._outbox[0])

    def test_the_allowed_ones_run(self):
        s = self.read_only_session(structured=True)
        ran = self.recorders()
        for name in sorted(adminchat.READ_ONLY_COMMANDS):
            self.run_command(s, f"{name} x")
        self.assertEqual(ran, sorted(adminchat.READ_ONLY_COMMANDS))
        self.assertNotIn(SENTENCE, " ".join(s._outbox))

    def test_the_reports_run_bare_and_are_refused_with_an_argument(self):
        s = self.read_only_session()
        ran = self.recorders()
        for name in sorted(adminchat.READ_ONLY_REPORTS):
            self.run_command(s, name)
            self.run_command(s, name + "  ")
            self.run_command(s, name + " off")
        self.assertEqual(ran, ["checkupdates", "checkupdates", "consolefeed", "consolefeed"])
        self.assertEqual(sum(SENTENCE in line for line in s._outbox), 2)

    def test_it_may_unpair_its_own_token_only(self):
        s = self.read_only_session()
        ran = self.recorders()
        self.run_command(s, "unpair")
        self.run_command(s, "unpair desk")
        self.assertEqual(ran, [])
        self.run_command(s, "unpair widget")
        self.assertEqual(ran, ["unpair"])

    def test_unlock_is_refused_and_the_password_never_checked(self):
        s = self.read_only_session()
        with mock.patch.object(adminchat, "reserve_password_attempt") as reserve:
            self.run_command(s, "unlock pw")
        reserve.assert_not_called()
        self.assertFalse(s.unlocked)
        self.assertEqual(s.unlock_failures, 0)
        self.assertTrue(s._outbox[-1].startswith(f"DCCORE LOCKED unlock {SENTENCE}"))
        self.assertNotIn("unlock pw", self.out.getvalue())

    def test_a_person_gets_the_sentence(self):
        s = self.read_only_session(structured=False)
        lines = self.run_command(s, "ban *!*@*")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(SENTENCE), lines[0])

    def test_a_ban_really_is_not_made(self):
        s = self.read_only_session()
        with mock.patch.object(adminchat, "_cmd_ban") as ban:
            self.run_command(s, "ban *!*@*")
        ban.assert_not_called()

    def test_help_lists_only_what_it_may_run(self):
        s = self.read_only_session(structured=False)
        lines = self.run_command(s, "help")
        listed = {line.split()[0] for line in lines if line.startswith("  ")}
        self.assertEqual(listed, adminchat.READ_ONLY_COMMANDS | adminchat.READ_ONLY_REPORTS)
        self.assertIn(SENTENCE, " ".join(lines))

    def test_a_full_session_is_untouched(self):
        s = self.session()
        ran = self.recorders()
        self.run_command(s, "ban x")
        self.run_command(s, "unlock pw")
        self.assertEqual(ran, ["ban", "unlock"])
        lines = self.run_command(self.session(structured=False), "help")
        self.assertNotIn(SENTENCE, " ".join(lines))

    def test_a_stand_in_without_the_flag_is_not_read_only(self):
        """The dashboard's Console makes a stand-in per request, behind its
        password login, with no read_only attribute."""
        class StandIn:
            nick = "dashboard"
            structured = False
            settings_txn = None

            def __init__(self):
                self.lines = []

            def send(self, text=""):
                self.lines.append(text)
        stand_in = StandIn()
        self.assertFalse(adminchat._refused_read_only(stand_in, "ban", "x"))
        self.assertEqual(stand_in.lines, [])


class TheDocsSayIt(unittest.TestCase):

    def test_admin_console_names_the_word_and_the_rule(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            text = handle.read()
        section = text.split("### Read-only tokens", 1)[1].split("\n### ", 1)[0]
        self.assertTrue("pair <client> <version> [readonly]" in text)
        self.assertTrue("readonly_hash" in section)
        for name in sorted(adminchat.READ_ONLY_COMMANDS | adminchat.READ_ONLY_REPORTS):
            self.assertTrue(f"`{name}`" in section, name)


if __name__ == "__main__":
    unittest.main()
