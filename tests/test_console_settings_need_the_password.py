"""Settings changes need the admin password, not only a paired token (#1264).

A paired token is kept in clear text in the script's dccore.ini. Before the
settings commands it opened a console and nothing else of weight; with them
it could serve any directory on the machine, read the on-connect commands
(an X login holds a password), send raw IRC as the bot, point the token
store at settings.conf, and widen ADMIN_HOSTMASKS - everything the dashboard
asks the password for. So a session that logged in with a token reads, and
changes nothing until `unlock <password>` succeeds once in it. A password
login is unlocked from the start.

Also here: a revert made while the save's rehash has not reloaded config
yet is saved, not judged "Nothing changed", and the window is told with
`DCCORE SETAPPLIED` when the rehash has run.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import on_connect  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_console_settings_commands import (  # noqa: E402
    Recorder, _real_apply, logged, make_session, read_bytes, run)

PASSWORD = "correct horse "          # a trailing space is part of it, as at the login
LOGIN = "PRIVMSG X@channels.example :LOGIN alfa sekritpw"


class LockCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(PASSWORD, iterations=1000))
        self.addCleanup(setattr, adminchat, "WRONG_PASSWORD_DELAY", adminchat.WRONG_PASSWORD_DELAY)
        adminchat.WRONG_PASSWORD_DELAY = 0.0
        self.apply = Recorder()
        self.addCleanup(setattr, webserver, "apply_settings_changes", webserver.apply_settings_changes)
        webserver.apply_settings_changes = self.apply
        self.addCleanup(setattr, commands, "handle_rehash_request", commands.handle_rehash_request)
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(adminchat.reset_state_for_tests)
        on_connect.save([LOGIN], 2)
        self.session = self.token_session()

    def token_session(self):
        session = make_session(self)
        session.unlocked = False                 # what a token login leaves it
        session.paired_as = "dccore.mrc-0a1b2c3d"
        return session

    def locked(self, reply, what):
        self.assertEqual(len(reply), 1, reply)
        self.assertTrue(reply[0].startswith(f"DCCORE LOCKED {what} "), reply)


class ALoginSaysWhichItWas(LockCase):

    def login(self, supplied):
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", db.ADMIN_TOKENS_FILE)
        db.ADMIN_TOKENS_FILE = os.path.join(self.make_temp_dir(), "tokens.json")
        db.save_admin_tokens({"dccore.mrc-0a1b2c3d": {"hash": adminchat.make_password_hash("tok-en", 1000)}})
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example")
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(session, supplied)
        self.assertTrue(session.authenticated)
        return session

    def test_a_password_login_is_never_locked(self):
        session = self.login(PASSWORD)
        self.assertTrue(session.unlocked)
        self.assertIsNone(session.paired_as)

    def test_a_token_login_is_locked(self):
        session = self.login("tok-en")
        self.assertFalse(session.unlocked)
        self.assertEqual(session.paired_as, "dccore.mrc-0a1b2c3d")

    def test_a_session_is_locked_until_a_login_says_otherwise(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.assertFalse(adminchat.Session(near, "192.0.2.1", "alfa", "alfa.example").unlocked)


class ATokenSessionChangesNothing(LockCase):

    def test_a_commit_is_refused_and_the_transaction_kept(self):
        run(self.session, "setbegin")
        self.assertEqual(run(self.session, "set MAX_DCC_SLOTS 5"), [])
        self.locked(run(self.session, "setcommit"), "setcommit")
        self.locked(run(self.session, "setcommit confirm"), "setcommit")
        self.assertEqual(self.apply.calls, [])
        self.assertEqual(self.session.settings_txn.changes, {"MAX_DCC_SLOTS": "5"})

    def test_a_lone_set_is_refused(self):
        self.locked(run(self.session, "set MAX_DCC_SLOTS 5"), "set")
        self.assertEqual(self.apply.calls, [])

    def test_served_folders_and_onconnect_commits_are_refused(self):
        before = (read_bytes(on_connect.on_connect_file()),)
        for begin, row, commit in (("served begin", "served list 1 1 Main", "served commit"),
                                   ("folders begin", "folders row 1 Music /music", "folders commit"),
                                   ("onconnect begin", "onconnect line 1 MODE %nick% +x", "onconnect commit")):
            run(self.session, begin)
            run(self.session, row)
            with self.subTest(commit=commit):
                self.locked(run(self.session, commit), commit)
        self.assertFalse(os.path.exists(library.lists_file()))
        self.assertFalse(os.path.exists(library.folders_file()))
        self.assertEqual((read_bytes(on_connect.on_connect_file()),), before)

    def test_the_on_connect_commands_are_not_shown_nor_resent(self):
        for line in ("onconnect", "onconnect resend"):
            reply = run(self.session, line)
            with self.subTest(line=line):
                self.locked(reply, line)
                self.assertNotIn("sekritpw", " ".join(reply))

    def test_no_token_is_minted_and_none_but_its_own_revoked(self):
        self.locked(run(self.session, "pair other 1.0"), "pair")
        self.locked(run(self.session, "unpair"), "unpair")
        self.locked(run(self.session, "unpair someone-else"), "unpair")
        self.addCleanup(setattr, db, "ADMIN_TOKENS_FILE", db.ADMIN_TOKENS_FILE)
        db.ADMIN_TOKENS_FILE = os.path.join(self.make_temp_dir(), "tokens.json")
        db.save_admin_tokens({"dccore.mrc-0a1b2c3d": {"hash": "x"}})
        reply = run(self.session, "unpair dccore.mrc-0a1b2c3d")      # giving access up is allowed
        self.assertIn("Revoked", " ".join(reply))
        self.assertEqual(db.load_admin_tokens(), {})

    def test_the_reads_stay_open(self):
        for line in ("settings", "served", "folders", "banlist", "setpreview", "consolecaps"):
            reply = run(self.session, line)
            with self.subTest(line=line):
                self.assertTrue(reply)
                self.assertFalse(any(r.startswith("DCCORE LOCKED") for r in reply), reply)

    def test_a_person_is_told_in_a_sentence(self):
        self.session.structured = False
        self.assertEqual(run(self.session, "set MAX_DCC_SLOTS 5"),
                         ["Type unlock <password> first - a paired token alone cannot change settings."])


class Unlocking(LockCase):

    def test_the_password_unlocks_and_the_kept_transaction_commits(self):
        run(self.session, "setbegin")
        run(self.session, "set MAX_DCC_SLOTS 5")
        self.locked(run(self.session, "setcommit"), "setcommit")
        self.assertEqual(run(self.session, "unlock " + PASSWORD), ["DCCORE UNLOCKED"])
        reply = run(self.session, "setcommit")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assertEqual(self.apply.calls, [{"MAX_DCC_SLOTS": "5"}])
        self.assertTrue(run(self.session, "onconnect")[0].startswith("DCCORE OCBEGIN "))

    def test_a_wrong_password_leaves_it_locked(self):
        reply = run(self.session, "unlock wrong")
        self.assertEqual(reply, ["DCCORE UNLOCK error Wrong password (1 of 3)."])
        self.assertFalse(self.session.unlocked)
        self.locked(run(self.session, "set MAX_DCC_SLOTS 5"), "set")

    def test_the_password_without_its_trailing_space_is_wrong(self):
        run(self.session, "unlock " + PASSWORD.rstrip(" "))
        self.assertFalse(self.session.unlocked)

    def test_three_wrong_close_the_session(self):
        closed = []
        self.session.close = lambda announce_text="Session closed.": closed.append(announce_text)
        run(self.session, "unlock one")
        run(self.session, "unlock two")
        self.assertEqual(closed, [])
        run(self.session, "unlock three")
        self.assertEqual(closed, ["Incorrect Password."])
        self.assertFalse(self.session.unlocked)

    def test_the_password_is_never_logged_nor_echoed(self):
        for line in ("unlock " + PASSWORD, "unlock sekritpw", "UNLOCK sekritpw", "unlok sekritpw",
                     "unlocksekritpw"):
            out = logged(self.session, line)
            with self.subTest(line=line):
                self.assertNotIn("sekrit", out)
                self.assertNotIn(PASSWORD.strip(), out)
        self.assertFalse(any("correct horse" in line for line in self.session.sent))

    def test_a_password_session_is_unlocked_already(self):
        session = make_session(self)
        self.assertEqual(run(session, "unlock anything"), ["DCCORE UNLOCKED"])
        self.assertTrue(run(session, "onconnect")[0].startswith("DCCORE OCBEGIN "))

    def test_the_dashboards_console_counts_as_the_password(self):
        with contextlib.redirect_stdout(io.StringIO()):
            status, result = webserver.build_console_command_result("onconnect")
        self.assertEqual(status, 200)
        self.assertIn(LOGIN, "\n".join(result["lines"]))


def hold_the_rehash(case, release):
    """Stand in for the rehash a save starts: it reloads nothing and returns
    when `release` is set. Its threads are released and joined when the test
    ends, so none outlives it."""
    threads = []

    def rehash(*_args, **_kwargs):
        threads.append(threading.current_thread())
        release.wait(10)

    def finish():
        release.set()
        for thread in threads:
            thread.join(10)
    case.addCleanup(setattr, commands, "handle_rehash_request", commands.handle_rehash_request)
    commands.handle_rehash_request = rehash
    case.addCleanup(finish)


class ARevertBeforeTheRehashIsSaved(DCCoreTestCase):
    """The save's rehash waits up to REHASH_TRANSFER_WAIT for transfers
    before it reloads config. A setting put back in that window was judged
    against the old in-memory value - "Nothing changed" - and the file kept
    the new one, which the rehash then applied."""

    def setUp(self):
        super().setUp()
        self.session = make_session(self)
        self.addCleanup(setattr, webserver, "apply_settings_changes", webserver.apply_settings_changes)
        webserver.apply_settings_changes = _real_apply
        hold_the_rehash(self, threading.Event())    # never reloads: config keeps the old value

    def stored(self):
        return settings_file.parse(read_bytes(settings_file.settings_path()).decode("utf-8")).get("MAX_DCC_SLOTS")

    def test_the_revert_is_saved(self):
        self.set_config(MAX_DCC_SLOTS=2)
        self.assertTrue(run(self.session, "set MAX_DCC_SLOTS 5")[0].startswith("DCCORE SETDONE ok 1 0 "))
        self.assertEqual((self.stored(), config.MAX_DCC_SLOTS), ("5", 2))
        reply = run(self.session, "set MAX_DCC_SLOTS 2")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assertEqual(self.stored(), "2")

    def test_a_value_the_file_already_holds_is_unchanged(self):
        self.set_config(MAX_DCC_SLOTS=2)
        run(self.session, "set MAX_DCC_SLOTS 5")
        self.assertEqual(run(self.session, "set MAX_DCC_SLOTS 5"),
                         ["DCCORE SETDONE ok 0 1 - Nothing changed; nothing was saved."])


class TheWindowHearsWhenTheRehashHasRun(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)
        self.addCleanup(setattr, webserver, "apply_settings_changes", webserver.apply_settings_changes)
        webserver.apply_settings_changes = _real_apply
        self.release = threading.Event()
        hold_the_rehash(self, self.release)

    def wait_for(self, line, limit=10.0):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and line not in self.session.sent:
            time.sleep(0.01)
        return line in self.session.sent

    def test_setapplied_comes_once_the_rehash_has_finished_and_after_setdone(self):
        self.set_config(MAX_DCC_SLOTS=2)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assertNotIn("DCCORE SETAPPLIED", self.session.sent)        # the rehash is still running
        self.release.set()
        self.assertTrue(self.wait_for("DCCORE SETAPPLIED"))
        self.assertLess(self.session.sent.index(reply[0]), self.session.sent.index("DCCORE SETAPPLIED"))

    def test_a_fast_rehash_still_reports_after_setdone(self):
        self.release.set()
        self.set_config(MAX_DCC_SLOTS=2)
        run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertTrue(self.wait_for("DCCORE SETAPPLIED"))
        done = [i for i, line in enumerate(self.session.sent) if line.startswith("DCCORE SETDONE ok")]
        self.assertLess(done[0], self.session.sent.index("DCCORE SETAPPLIED"))

    def test_a_save_that_failed_reports_no_setapplied(self):
        self.release.set()
        run(self.session, "set NICKNAME")        # a required setting: refused, nothing started
        self.assertNotIn("DCCORE SETAPPLIED", self.session.sent)


if __name__ == "__main__":
    unittest.main()
