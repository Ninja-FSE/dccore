"""Regression coverage for the config.banned_users race (#1248 review).

config.banned_users is written from the Flask (dashboard) and console
threads - security.ignore_user() and security.lift_ban() - while the IRC
read thread's own flood sweep (security._prune_flood_tracking()) reads and
prunes the same dict on a timer. Before this fix, neither side took any
lock: an ignore or lift landing in the middle of the sweep's own iteration
raised "RuntimeError: dictionary changed size during iteration", which
reaches the read loop's outer exception handler and reconnects the bot -
confirmed on review, forced below rather than left to the scheduler.

security.banned_users_lock (bound from runtime.py, the same pattern
dcc.queue_lock and announce.py's locks already use) is the fix: every
touch point takes it. These tests force the interleaving deterministically
with threading.Event, the same technique test_channel_users_lock.py already
uses for the equivalent config.channel_users race - a control test shows the
unlocked pattern really does raise, and a second test shows the real,
already-locked functions do not.
"""

import threading
import time
import unittest

from tests.support import DCCoreTestCase

import defaults as config
import runtime
import security


class BannedUsersLockIsShared(DCCoreTestCase):
    """security.banned_users_lock must be the one object runtime.py hands
    out - bound at import time, not constructed in security.py itself, so a
    !rehash reload of security.py (which re-executes the module body) picks
    the SAME live lock back up instead of silently starting a fresh one no
    other thread is holding."""

    def test_security_module_and_runtime_agree_on_one_lock(self):
        self.assertIs(security.banned_users_lock, runtime.banned_users_lock)


def _sweep_banned_users_unlocked(now):
    """Exactly the vulnerable shape security._prune_flood_tracking() used to
    have for banned_users, before this fix wrapped it in the lock - kept
    here only as the control, to show the window this fix closes is real."""
    expired = [nick for nick, until in config.banned_users.items()
              if now >= security._ban_expiry(until)]
    for nick in expired:
        del config.banned_users[nick]
    return expired


class ConcurrentIgnoreAndSweepDoNotCorruptState(DCCoreTestCase):
    """Hold the sweep's own iteration open, let a writer touch banned_users,
    resume - deterministic on purpose (same reasoning as
    test_channel_users_lock.py): the control used to churn an unlocked dict
    and hope the scheduler interleaved a writer mid-iteration, which a
    lightly loaded runner does not reliably do. Here the interleaving is
    forced with events."""

    def setUp(self):
        super().setUp()
        config.banned_users.clear()
        self.addCleanup(config.banned_users.clear)
        import db
        self._real_save = db.save_bans_to_file
        db.save_bans_to_file = lambda: None
        self.addCleanup(setattr, db, "save_bans_to_file", self._real_save)

    def _iterate_while_another_thread_ignores(self, locked):
        config.banned_users["alreadyexpired"] = time.time() - 1
        config.banned_users["stillrunning"] = time.time() + 999
        inside_the_loop = threading.Event()
        writer_done = threading.Event()
        errors = []
        real_ban_expiry = security._ban_expiry

        def patched_ban_expiry(value):
            result = real_ban_expiry(value)
            inside_the_loop.set()
            writer_done.wait(0.5 if locked else 5)
            return result

        def writer():
            inside_the_loop.wait(5)
            try:
                if locked:
                    security.ignore_user("newnick", 5)
                else:
                    config.banned_users["newnick"] = time.time() + 300
            finally:
                writer_done.set()

        def reader():
            try:
                orig = security._ban_expiry
                security._ban_expiry = patched_ban_expiry
                try:
                    if locked:
                        security._prune_flood_tracking(time.time())
                    else:
                        _sweep_banned_users_unlocked(time.time())
                finally:
                    security._ban_expiry = orig
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, daemon=True),
                   threading.Thread(target=reader, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads), "a thread never finished")
        return errors

    def test_without_the_lock_an_ignore_mid_sweep_raises(self):
        """Control: shows the crash this fix closes is real."""
        errors = self._iterate_while_another_thread_ignores(locked=False)

        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("changed size during iteration", str(errors[0]))

    def test_with_the_lock_the_writer_waits_for_the_sweep_to_finish(self):
        errors = self._iterate_while_another_thread_ignores(locked=True)

        self.assertEqual(errors, [])
        self.assertIn("newnick", config.banned_users, "the writer never got its turn after the sweep")
        self.assertNotIn("alreadyexpired", config.banned_users, "the sweep itself did not run to completion")


class LiftBanIsAtomicAgainstAConcurrentExpiry(DCCoreTestCase):
    """lift_ban() used to be check-then-delete - two separate statements, so
    the sweep expiring the SAME nick in the gap between them raised KeyError
    on the real del (a 500 on the dashboard for an ignore that had simply
    just ended on its own, a moment before the operator tried to lift it by
    hand). pop(key, None) collapses the two into one atomic dict operation."""

    def setUp(self):
        super().setUp()
        config.banned_users.clear()
        self.addCleanup(config.banned_users.clear)
        import db
        self._real_save = db.save_bans_to_file
        db.save_bans_to_file = lambda: None
        self.addCleanup(setattr, db, "save_bans_to_file", self._real_save)

    def test_the_old_check_then_delete_pattern_raises_when_raced(self):
        """Control: the exact shape lift_ban() used to have, forced
        deterministically rather than left to the scheduler - shows the
        window this fix closes is real, not just theoretical."""
        config.banned_users["racer"] = time.time() + 300
        checked = threading.Event()
        deleted_elsewhere = threading.Event()
        errors = []

        def victim():
            try:
                if "racer" not in config.banned_users:
                    return
                checked.set()
                deleted_elsewhere.wait(5)
                del config.banned_users["racer"]  # the bug: raises here
            except Exception as exc:
                errors.append(exc)

        def racer():
            checked.wait(5)
            del config.banned_users["racer"]
            deleted_elsewhere.set()

        threads = [threading.Thread(target=victim, daemon=True),
                   threading.Thread(target=racer, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(errors), 1, errors)
        self.assertIsInstance(errors[0], KeyError)

    def test_lift_ban_pops_atomically_instead(self):
        """The real fix: dict.pop(key, None) is one operation, not two - a
        concurrent sweep that already removed the entry by the time this
        runs is simply reported as "not ignored", the ordinary outcome,
        never a raise."""
        config.banned_users.pop("racer", None)
        ok, message = security.lift_ban("racer")
        self.assertFalse(ok)
        self.assertIn("not ignored", message)


if __name__ == "__main__":
    unittest.main()
