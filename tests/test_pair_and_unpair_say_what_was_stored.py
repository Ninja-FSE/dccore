"""`unpair` said "Revoked" when nothing was revoked.

db.save_admin_tokens() swallowed every error, so when the token store could
not be written (a full disk, a read-only data folder, a file held open on
Windows) `unpair` still answered "Revoked <name>" while the token stayed on
disk and went on logging in; `pair` showed a token that was never stored.
And both loaded the store, changed it and saved it with no lock, so a `pair`
in one console session could write back the store it had loaded before an
`unpair` in another removed a token - the revoked token worked again, and
its revoker had been told otherwise.

save_admin_tokens() now says whether it wrote, both commands report that,
and the load-change-save runs under runtime.admin_tokens_lock.
"""

import contextlib
import io
import os
import socket
import sys
import threading

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def wait_for(condition, timeout):
    pause = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return True
        pause.wait(0.01)
    return condition()


class TokenStoreCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw", iterations=1000))
        self.out = io.StringIO()

    def session(self, nick="alfa"):
        s = adminchat.Session(socket.socket(), "127.0.0.1", nick, "alfa.host.example")
        self.addCleanup(s.close, None)
        s.authenticated = True
        s.unlocked = True        # logged in with the password
        return s

    def run_command(self, session, line):
        with contextlib.redirect_stdout(self.out):
            adminchat.handle_command(session, line)
        return list(session._outbox)

    def pair(self, name):
        lines = self.run_command(self.session(), f"pair {name} 1.0")
        return [line.strip() for line in lines if line.startswith("  ")][0]

    def fail_writes(self):
        real = db._atomic_write

        def refuse(path, text, mode=None):
            raise OSError(28, "No space left on device (test)")
        db._atomic_write = refuse
        self.addCleanup(setattr, db, "_atomic_write", real)


class AFailedWriteIsSaid(TokenStoreCase):

    def test_save_reports_whether_it_wrote(self):
        self.assertTrue(db.save_admin_tokens({}))
        self.fail_writes()
        with contextlib.redirect_stdout(self.out):
            self.assertFalse(db.save_admin_tokens({}))

    def test_unpair_that_could_not_write_says_the_token_still_works(self):
        token = self.pair("leaked.mrc")
        self.fail_writes()

        said = " ".join(self.run_command(self.session(), "unpair leaked.mrc"))

        self.assertNotIn("Revoked", said)
        self.assertIn("NOT revoked", said)
        self.assertIn("leaked.mrc", db.load_admin_tokens())
        self.assertEqual(adminchat._token_matches(token), "leaked.mrc",
                         "the message says the token still works; it must be true")

    def test_pair_that_could_not_write_shows_no_token(self):
        self.fail_writes()

        lines = self.run_command(self.session(), "pair fresh.mrc 1.0")

        self.assertFalse([line for line in lines if line.startswith("  ")],
                         "a token that was never stored was shown")
        self.assertIn("Could not pair fresh.mrc", " ".join(lines))
        self.assertEqual(db.load_admin_tokens(), {})

    def test_pair_that_could_not_write_sends_no_token_line_to_a_script(self):
        self.fail_writes()
        s = self.session()
        s.structured = True

        lines = self.run_command(s, "pair fresh.mrc 1.0")

        self.assertFalse([line for line in lines if "DCCORE TOKEN" in line])

    def test_a_working_unpair_still_revokes(self):
        token = self.pair("leaked.mrc")

        said = " ".join(self.run_command(self.session(), "unpair leaked.mrc"))

        self.assertIn("Revoked leaked.mrc", said)
        self.assertIsNone(adminchat._token_matches(token))


class PairAndUnpairDoNotOverlap(TokenStoreCase):

    def test_a_pair_in_flight_does_not_bring_a_revoked_token_back(self):
        """The audit's race: session B's `pair` has loaded the store and not
        yet saved it when session A's `unpair` revokes a token."""
        self.pair("leaked.mrc")
        real_load = db.load_admin_tokens
        b_loaded = threading.Event()
        b_may_save = threading.Event()

        def load_then_hold():
            tokens = real_load()
            if threading.current_thread().name == "session-b":
                b_loaded.set()
                b_may_save.wait(10)
            return tokens
        db.load_admin_tokens = load_then_hold
        self.addCleanup(setattr, db, "load_admin_tokens", real_load)

        a_said = []
        b = threading.Thread(target=self.run_command, args=(self.session("bravo"), "pair other.mrc 1.0"),
                             name="session-b", daemon=True)
        b.start()
        self.assertTrue(b_loaded.wait(10))

        def unpair():
            a_said.extend(self.run_command(self.session(), "unpair leaked.mrc"))
        a = threading.Thread(target=unpair, name="session-a", daemon=True)
        a.start()
        # Unlocked, A finishes here, inside B's load and save. Locked, it waits
        # for B, and this wait ends at its bound - which is the right outcome.
        wait_for(lambda: not a.is_alive(), timeout=0.5)
        b_may_save.set()
        b.join(10)
        a.join(10)

        self.assertIn("Revoked leaked.mrc", " ".join(a_said))
        self.assertEqual(sorted(real_load()), ["other.mrc"],
                         "a revoked token was written back by a pair that ran alongside")

    def test_the_lock_lives_in_runtime(self):
        """Taken while the store is read and written: a revoke waits for it."""
        self.pair("leaked.mrc")
        said = []
        with runtime.admin_tokens_lock:
            a = threading.Thread(target=lambda: said.extend(
                self.run_command(self.session(), "unpair leaked.mrc")), daemon=True)
            a.start()
            wait_for(lambda: not a.is_alive(), timeout=0.3)
            self.assertIn("leaked.mrc", db.load_admin_tokens(),
                          "unpair changed the store without runtime.admin_tokens_lock")
        a.join(10)
        self.assertIn("Revoked leaked.mrc", " ".join(said))


if __name__ == "__main__":
    import unittest
    unittest.main()
