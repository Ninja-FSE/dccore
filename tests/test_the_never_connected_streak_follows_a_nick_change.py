"""The never-connected streak follows a nick change (#1437).

DAILY_SEND_FAILS_LIMIT (#1389) stops counting sends whose receiver never
connected once a run of them, with no connect in between, reaches
NEVER_CONNECTED_STREAK sends to NEVER_CONNECTED_STREAK_NICKS different nicks
(dcc.never_connected_counts()): several downloaders failing the same way at
once is the bot's own address. The nicks of that run are kept in
runtime.never_connected_streak_nicks, keyed like dcc_queue.

irc.note_nick_change() carried the daily count (dcc.carry_send_fails()) but
not the streak's entry, so one user renamed between two unanswered offers was
two nicks to the rule. It now moves the old key to the new one through
dcc.carry_never_connected_streak(), under send_connected_lock and outside
queue_lock; the run length is left alone.

Driven three ways: the real NICK line (irc.answer_nick_change()) between real
sends that never connect - through start_dcc_send() with a listener nobody
dials, and through finish_send() as start_dcc_send() hands it over - in front
of the real count and pause; the move on its own for its edges; and the locks
it is made under, with recording locks.

Every nick, channel and file name here is invented.
"""

import contextlib
import io
import os
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

from tests import test_a_nick_whose_sends_keep_failing_is_paused as paused  # noqa: E402

OLD = "kilo"
NEW = "kilo_away"
THIRD = "kilo_back"
OTHER = "lima"
FILE = paused.FILE
CHAN = paused.CHAN
CONTENT = paused.CONTENT
SKIPPED = "not counted toward DAILY_SEND_FAILS_LIMIT"


class StreakCase(paused.RealSendCase):
    """The limit at 3, some send already connected, the streak empty; the
    listener and the connection are fakes, so every runner runs all of it."""

    LIMIT = 3

    def setUp(self):
        super().setUp()
        # Sends connect, so a never-connected one may count; no run yet.
        runtime.send_connected_since_start = True
        runtime.never_connected_streak = 0
        runtime.never_connected_streak_nicks.clear()
        runtime.never_connected_streak_logged = False
        self.assertEqual((dcc.NEVER_CONNECTED_STREAK, dcc.NEVER_CONNECTED_STREAK_NICKS), (3, 2),
                         "these tests are written for a rule of 3 sends over 2 nicks")
        # The sent row moves with the queue on a rename; keep it there for
        # every attempt so each real send has a row to settle.
        self.set_config(MAX_SEND_FAILS=10)

    # --- the wire -------------------------------------------------------

    def nick_line(self, old=OLD, new=NEW):
        with contextlib.redirect_stdout(self.out):
            irc.answer_nick_change(":%s!u@host.example NICK :%s" % (old, new))

    def rename(self, old=OLD, new=NEW):
        with contextlib.redirect_stdout(self.out):
            return irc.note_nick_change(old, new)

    # --- sends that never connect ----------------------------------------

    def real_unanswered_send(self, nick):
        """The REAL start_dcc_send() to `nick`, whose receiver never connects."""
        paused._Listener.behaviour = "timeout"
        config.active_transfers[:] = [{"user": nick, "file": FILE, "bytes_sent": 0,
                                       "next_file_obj": FILE}]
        sock = paused._Irc()
        self.oserve.irc_connection = sock
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            dcc.start_dcc_send(sock, nick, self.path, FILE, CHAN, self.row)
        self.assertTrue(sock.sent, "the offer went out")

    def unanswered(self, *nicks):
        """finish_send() of a send to each nick in turn whose receiver never
        connected, as start_dcc_send() hands it over."""
        with contextlib.redirect_stdout(self.out), mock.patch("time.sleep", lambda *_a, **_k: None):
            for nick in nicks:
                row = {"user": nick, "file": FILE, "bytes_sent": 0}
                config.active_transfers[:] = [row]
                dcc.finish_send(dcc.SentFile(
                    row, nick, {"file": FILE, "path": self.path}, self.path, FILE, len(CONTENT),
                    0, 0, False, False, 0.0, 0.0, None, channel=CHAN, irc_sock=paused._Irc(),
                    outcome_identity=("", FILE, "file"), outcome_status="failed",
                    never_connected=True, receiver_failed=True))

    # --- what came of it --------------------------------------------------

    def counts(self):
        return {nick: entry["fails"] for nick, entry in runtime.failed_transfers.items()}

    def skips_logged(self):
        return self.out.getvalue().count(SKIPPED)


# ---------------------------------------------------------------------------
# One user, renamed between unanswered offers, is one nick to the rule.
# ---------------------------------------------------------------------------

class OneUserUnderTwoNicks(StreakCase):

    def test_through_real_sends_the_renamed_nick_is_paused_at_the_limit(self):
        self.real_unanswered_send(OLD)
        self.assertEqual(self.counts(), {OLD: 1}, "the first unanswered offer counts")
        self.assertEqual(runtime.never_connected_streak_nicks, {OLD})

        self.nick_line(OLD, NEW)

        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})
        self.assertIn(self.row, config.dcc_queue.get(NEW, []), "the queue moved too")
        self.real_unanswered_send(NEW)
        self.real_unanswered_send(NEW)

        self.assertEqual(self.counts(), {NEW: 3},
                         "the third offer was taken for several downloaders failing at once")
        self.assertEqual(dcc.send_fails_paused(NEW), 3)
        self.assertEqual(len(self.paused_notices(NEW)), 1, self.oserve.queued)
        self.assertNotIn(NEW, config.dcc_queue, "the pause removes what is left of the queue")
        self.assertEqual(self.skips_logged(), 0, self.out.getvalue())
        self.assertEqual(runtime.never_connected_streak, 3)
        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})

    def test_through_finished_sends_the_renamed_nick_is_paused_at_the_limit(self):
        self.unanswered(OLD)
        self.nick_line(OLD, NEW)
        self.unanswered(NEW, NEW)

        self.assertEqual(self.counts(), {NEW: 3})
        self.assertEqual(dcc.send_fails_paused(NEW), 3)
        self.assertEqual(len(self.paused_notices(NEW)), 1, self.oserve.queued)
        self.assertEqual(self.skips_logged(), 0, self.out.getvalue())

    def test_one_short_of_the_limit_the_renamed_nick_is_still_served(self):
        """Control: the move makes it one nick, it does not add a failure."""
        self.unanswered(OLD)
        self.nick_line(OLD, NEW)
        self.unanswered(NEW)

        self.assertEqual(self.counts(), {NEW: 2})
        self.assertEqual(dcc.send_fails_paused(NEW), 0)
        self.assertEqual(self.paused_notices(NEW), [])

    def test_a_mixed_case_nick_line_moves_the_folded_key(self):
        self.unanswered("Kilo")
        self.assertEqual(runtime.never_connected_streak_nicks, {OLD})

        self.nick_line("KILO", "Kilo_Away")

        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})
        self.unanswered("KILO_AWAY", "kilo_AWAY")
        self.assertEqual(self.counts(), {NEW: 3})
        self.assertEqual(dcc.send_fails_paused("Kilo_Away"), 3)
        self.assertEqual(self.skips_logged(), 0)

    def test_two_renames_are_still_one_nick(self):
        self.unanswered(OLD)
        self.nick_line(OLD, NEW)
        self.unanswered(NEW)
        self.nick_line(NEW, THIRD)

        self.assertEqual(runtime.never_connected_streak_nicks, {THIRD})
        self.unanswered(THIRD)

        self.assertEqual(self.counts(), {THIRD: 3})
        self.assertEqual(dcc.send_fails_paused(THIRD), 3)
        self.assertEqual(self.skips_logged(), 0)

    def test_a_case_only_rename_moves_nothing_and_stays_one_nick(self):
        self.unanswered(OLD)

        self.assertEqual(self.rename(OLD, "KiLo"), [])

        self.assertEqual(runtime.never_connected_streak_nicks, {OLD})
        self.unanswered("KiLo", "KILO")
        self.assertEqual(self.counts(), {OLD: 3})
        self.assertEqual(self.skips_logged(), 0)


class TwoUsersStillTripTheRule(StreakCase):
    """The move keeps one user one nick; it does not hide a second user."""

    def test_a_renamed_user_and_another_user_are_two_nicks(self):
        self.unanswered(OLD)
        self.nick_line(OLD, NEW)
        self.unanswered(OTHER)
        self.assertEqual(self.counts(), {NEW: 1, OTHER: 1})

        self.unanswered(NEW)

        self.assertEqual(self.counts(), {NEW: 1, OTHER: 1},
                         "the third offer, to the second of two nicks, counted")
        self.assertEqual(self.skips_logged(), 1)
        self.assertIn("3 offers to 2 different nicks have gone unanswered", self.out.getvalue())
        self.assertEqual(runtime.never_connected_streak_nicks, {NEW, OTHER})

    def test_renaming_onto_another_nick_in_the_streak_leaves_one_entry(self):
        self.unanswered(OLD, OTHER)

        moved = self.rename(OLD, OTHER)

        self.assertIn("never_connected_streak", moved)
        self.assertEqual(runtime.never_connected_streak_nicks, {OTHER})
        self.assertEqual(runtime.never_connected_streak, 2)


# ---------------------------------------------------------------------------
# The move on its own.
# ---------------------------------------------------------------------------

class TheMove(StreakCase):
    """Nothing else of the user's to carry, so what a rename reports is the
    streak's alone."""

    def setUp(self):
        super().setUp()
        config.dcc_queue.clear()
        config.active_transfers[:] = []

    def test_it_moves_the_old_key_to_the_new_and_says_so(self):
        runtime.never_connected_streak_nicks.add(OLD)

        self.assertIs(dcc.carry_never_connected_streak(OLD, NEW), True)

        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})

    def test_a_new_key_already_there_is_kept_once_and_the_old_one_goes(self):
        runtime.never_connected_streak_nicks.update({OLD, NEW})

        self.assertIs(dcc.carry_never_connected_streak(OLD, NEW), True)

        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})

    def test_an_old_key_not_in_the_streak_moves_nothing(self):
        runtime.never_connected_streak_nicks.add(OTHER)

        self.assertIs(dcc.carry_never_connected_streak(OLD, NEW), False)

        self.assertEqual(runtime.never_connected_streak_nicks, {OTHER})

    def test_an_empty_streak_stays_empty(self):
        self.assertIs(dcc.carry_never_connected_streak(OLD, NEW), False)

        self.assertEqual(runtime.never_connected_streak_nicks, set())

    def test_the_run_length_and_its_log_flag_are_left_alone(self):
        runtime.never_connected_streak = 2
        runtime.never_connected_streak_logged = True
        runtime.never_connected_streak_nicks.update({OLD, OTHER})

        dcc.carry_never_connected_streak(OLD, NEW)

        self.assertEqual(runtime.never_connected_streak, 2)
        self.assertTrue(runtime.never_connected_streak_logged)
        self.assertTrue(runtime.send_connected_since_start)
        self.assertEqual(runtime.never_connected_streak_nicks, {NEW, OTHER})

    def test_the_rename_reports_it_among_what_moved(self):
        runtime.never_connected_streak_nicks.add(OLD)

        moved = self.rename(OLD, NEW)

        self.assertEqual(moved, ["never_connected_streak"])
        self.assertIn("carried over: never_connected_streak", self.out.getvalue())

    def test_the_rename_does_not_report_it_when_the_old_nick_is_not_in_it(self):
        runtime.never_connected_streak_nicks.add(OTHER)

        moved = self.rename(OLD, NEW)

        self.assertNotIn("never_connected_streak", moved)
        self.assertEqual(moved, [])
        self.assertEqual(runtime.never_connected_streak_nicks, {OTHER})

    def test_the_rename_with_an_empty_streak_reports_nothing(self):
        self.assertEqual(self.rename(OLD, NEW), [])
        self.assertEqual(runtime.never_connected_streak_nicks, set())

    def test_a_connect_after_a_move_still_clears_everything(self):
        self.unanswered(OLD)
        runtime.never_connected_streak_logged = True
        self.nick_line(OLD, NEW)
        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})

        dcc.note_send_connected()

        self.assertEqual(runtime.never_connected_streak, 0)
        self.assertEqual(runtime.never_connected_streak_nicks, set())
        self.assertFalse(runtime.never_connected_streak_logged)
        self.assertTrue(runtime.send_connected_since_start)
        # And a fresh run starts from the new nick alone.
        self.unanswered(NEW)
        self.assertEqual(runtime.never_connected_streak_nicks, {NEW})
        self.assertEqual(runtime.never_connected_streak, 1)


# ---------------------------------------------------------------------------
# Under send_connected_lock, and not inside queue_lock.
# ---------------------------------------------------------------------------

class _Recording:
    """A lock that knows which thread holds it, delegating to a real one."""

    def __init__(self, real):
        self.real = real
        self.holder = None

    def __enter__(self):
        self.real.acquire()
        self.holder = threading.get_ident()
        return self

    def __exit__(self, *exc_info):
        self.holder = None
        self.real.release()
        return False

    def acquire(self, *args, **kwargs):
        got = self.real.acquire(*args, **kwargs)
        if got:
            self.holder = threading.get_ident()
        return got

    def release(self):
        self.holder = None
        self.real.release()

    def held_here(self):
        return self.holder == threading.get_ident()


class UnderItsOwnLock(StreakCase):
    """The streak is read and changed under send_connected_lock, and not
    inside queue_lock: dcc.py never takes one inside the other."""

    def watch(self, members):
        send_lock = _Recording(threading.Lock())
        queue_lock = _Recording(threading.Lock())
        seen = []

        def note(what):
            seen.append((what, send_lock.held_here(), queue_lock.held_here()))

        class Watched(set):
            def __contains__(self, item):
                note("in")
                return set.__contains__(self, item)

            def discard(self, item):
                note("discard")
                set.discard(self, item)

            def add(self, item):
                note("add")
                set.add(self, item)

        store = Watched(members)
        for target, name, value in ((runtime, "send_connected_lock", send_lock),
                                    (runtime, "never_connected_streak_nicks", store),
                                    (dcc, "queue_lock", queue_lock)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return store, seen

    def test_the_rename_moves_it_under_the_streaks_lock_and_not_the_queues(self):
        store, seen = self.watch({OLD})

        moved = self.rename(OLD, NEW)

        self.assertIn("never_connected_streak", moved)
        self.assertEqual(set(store), {NEW})
        self.assertEqual({what for what, *_ in seen}, {"in", "discard", "add"}, seen)
        self.assertEqual({(s, q) for _what, s, q in seen}, {(True, False)},
                         "(streak lock held, queue lock held) for each look and change")

    def test_the_move_on_its_own_holds_the_streaks_lock(self):
        store, seen = self.watch({OLD})

        self.assertTrue(dcc.carry_never_connected_streak(OLD, NEW))

        self.assertEqual(set(store), {NEW})
        self.assertTrue(seen)
        self.assertEqual({(s, q) for _what, s, q in seen}, {(True, False)}, seen)

    def test_the_lock_is_released_afterwards(self):
        self.watch({OLD})
        self.rename(OLD, NEW)
        self.assertTrue(runtime.send_connected_lock.acquire(blocking=False),
                        "send_connected_lock is still held after the rename")
        runtime.send_connected_lock.release()


if __name__ == "__main__":
    unittest.main()
