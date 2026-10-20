"""A nick paused for failed sends stays paused under a new nick (#1437).

DAILY_SEND_FAILS_LIMIT (#1389) keeps each nick's failed sends today, and the
pause they made, in runtime.failed_transfers, keyed like dcc_queue by the
lowercased nick; the request gate (dcc.send_fails_paused(), from
handle_download_request() and the list request) reads only the asking
nick's key. irc.note_nick_change() carried the queue, the slot lock, the
freeze and the transfer rows to the new nick, but not that entry, so a nick
paused at its last failed send typed /nick and was served again at once,
its count back at 0.

The entry now follows the nick: one from an earlier day is dropped, not
carried; when the new key holds today's entry too the larger count wins with
its own "paused" flag, never the sum; equal counts keep a pause either had.

Driven three ways: note_nick_change() on its own for the merge rules, the
real NICK line (irc.answer_nick_change()) in front of the real request gate
(dcc.handle_download_request() and list.send_file_list()), and the lock the
move is made under.

Every nick, channel and file name here is invented.
"""

import contextlib
import datetime
import io
import os
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

TODAY = datetime.date(2026, 3, 4)
YESTERDAY = TODAY - datetime.timedelta(days=1)
PAUSED = "paused until midnight"
OLD = "kilo"
NEW = "kilo_away"


def entry(fails, paused, day=TODAY):
    return {"day": day, "fails": fails, "paused": paused}


class RenameCase(DCCoreTestCase):
    """The limit on at 3, today fixed, the queue file and debug channel stubbed."""

    LIMIT = 3

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=self.LIMIT, MAX_SEND_FAILS=3)
        self.today = TODAY
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: self.today)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(db, "save_dcc_queue", lambda: None)
        patch.start()
        self.addCleanup(patch.stop)
        silence_debug(announce)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)

    def fail_sends(self, nick, times=1):
        result = 0
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(times):
                result = dcc.note_failed_send(nick)
        return result

    def rename(self, old=OLD, new=NEW):
        with contextlib.redirect_stdout(io.StringIO()):
            return irc.note_nick_change(old, new)

    def paused_notices(self, nick):
        return [m for who, m, *_ in self.oserve.queued if who == nick and PAUSED in m]


# ---------------------------------------------------------------------------
# The count and the pause go with the nick.
# ---------------------------------------------------------------------------

class TheCountFollowsTheNick(RenameCase):

    def test_a_nick_paused_at_the_limit_is_still_paused_under_its_new_one(self):
        self.assertEqual(self.fail_sends(OLD, 3), 3)
        self.assertEqual(dcc.send_fails_paused(OLD), 3, "the fixture pauses it")

        self.rename()

        self.assertEqual(dcc.send_fails_paused(NEW), 3,
                         "the new nick is served at once, its count back at 0")
        self.assertEqual(dcc.send_fails_paused(OLD), 0)
        self.assertNotIn(OLD, runtime.failed_transfers)
        self.assertEqual(dcc.paused_for_send_fails(), {NEW: 3})

    def test_one_short_of_the_limit_the_next_failure_under_the_new_nick_pauses_it(self):
        self.fail_sends(OLD, 2)
        self.rename()
        self.oserve.queued.clear()

        self.assertEqual(self.fail_sends(NEW), 3, "the count started again from one")

        self.assertEqual(dcc.send_fails_paused(NEW), 3)
        self.assertEqual(len(self.paused_notices(NEW)), 1, self.oserve.queued)

    def test_a_paused_nick_renamed_is_not_told_a_second_time(self):
        """The pause made under the old nick travels with its flag, so the
        next failure under the new one is not a new pause."""
        self.fail_sends(OLD, 3)
        self.rename()
        self.oserve.queued.clear()

        self.assertEqual(self.fail_sends(NEW), 4)

        self.assertEqual(self.paused_notices(NEW), [])

    def test_the_old_name_is_free_for_whoever_takes_it_next(self):
        self.fail_sends(OLD, 3)
        self.rename()

        self.assertEqual(self.fail_sends(OLD), 1, "someone else on the old name starts from one")
        self.assertEqual(dcc.send_fails_paused(OLD), 0)

    def test_unpause_under_the_new_nick_lifts_it(self):
        self.fail_sends(OLD, 3)
        self.rename()

        self.assertEqual(dcc.unpause_send_fails(NEW), 3)
        self.assertEqual(dcc.send_fails_paused(NEW), 0)
        self.assertEqual(runtime.failed_transfers, {})

    def test_the_names_are_matched_whatever_their_case(self):
        self.fail_sends("Kilo", 3)

        self.rename("KILO", "Kilo_Away")

        self.assertEqual(set(runtime.failed_transfers), {"kilo_away"})
        self.assertEqual(dcc.send_fails_paused("KILO_AWAY"), 3)

    def test_it_says_the_count_moved(self):
        self.fail_sends(OLD)
        self.assertIn("failed_transfers", self.rename())

    def test_a_second_rename_carries_it_again(self):
        self.fail_sends(OLD, 3)
        self.rename(OLD, NEW)
        self.rename(NEW, "kilo_back")

        self.assertEqual(set(runtime.failed_transfers), {"kilo_back"})
        self.assertEqual(dcc.send_fails_paused("kilo_back"), 3)


class NothingToCarry(RenameCase):

    def test_a_nick_with_no_count_leaves_the_counts_alone(self):
        self.fail_sends("lima", 2)
        runtime.failed_transfers[NEW] = entry(1, False)
        before = {key: dict(value) for key, value in runtime.failed_transfers.items()}

        moved = self.rename()

        self.assertEqual(runtime.failed_transfers, before)
        self.assertNotIn("failed_transfers", moved)

    def test_with_nothing_at_all_nothing_is_reported(self):
        self.assertEqual(self.rename(), [])
        self.assertEqual(runtime.failed_transfers, {})

    def test_a_rename_that_only_changes_case_moves_nothing(self):
        self.fail_sends(OLD, 3)

        self.assertEqual(self.rename(OLD, "KiLo"), [])

        self.assertEqual(runtime.failed_transfers, {OLD: entry(3, True)})
        self.assertEqual(dcc.send_fails_paused("KiLo"), 3)


# ---------------------------------------------------------------------------
# A count from an earlier day is worth nothing, on either side.
# ---------------------------------------------------------------------------

class YesterdaysCounts(RenameCase):

    def test_the_old_nicks_count_from_yesterday_is_dropped_not_carried(self):
        self.today = YESTERDAY
        self.fail_sends(OLD, 3)
        self.today = TODAY

        moved = self.rename()

        self.assertEqual(runtime.failed_transfers, {},
                         "yesterday's count moved to the new nick, or stayed on the old")
        self.assertNotIn("failed_transfers", moved)

    def test_it_leaves_the_new_nicks_entry_as_it_was(self):
        runtime.failed_transfers[OLD] = entry(5, True, YESTERDAY)
        runtime.failed_transfers[NEW] = entry(1, False)

        self.rename()

        self.assertEqual(runtime.failed_transfers, {NEW: entry(1, False)})

    def test_the_new_nicks_count_from_yesterday_is_replaced(self):
        runtime.failed_transfers[NEW] = entry(9, True, YESTERDAY)
        self.fail_sends(OLD, 3)

        self.rename()

        self.assertEqual(runtime.failed_transfers, {NEW: entry(3, True)})
        self.assertEqual(dcc.send_fails_paused(NEW), 3)

    def test_even_by_a_smaller_count_today(self):
        runtime.failed_transfers[NEW] = entry(9, True, YESTERDAY)
        runtime.failed_transfers[OLD] = entry(1, False)

        self.rename()

        self.assertEqual(runtime.failed_transfers, {NEW: entry(1, False)})


# ---------------------------------------------------------------------------
# Both nicks counted today: the larger count wins, whole; never the sum.
# ---------------------------------------------------------------------------

class BothCountedToday(RenameCase):

    def merge(self, old, new):
        runtime.failed_transfers[OLD] = old
        runtime.failed_transfers[NEW] = new
        moved = self.rename()
        self.assertNotIn(OLD, runtime.failed_transfers, "the old key is left holding a count")
        self.assertEqual(set(runtime.failed_transfers), {NEW})
        self.assertIn("failed_transfers", moved)
        return runtime.failed_transfers[NEW]

    def test_the_old_count_larger_wins_with_its_pause(self):
        self.assertEqual(self.merge(entry(3, True), entry(1, False)), entry(3, True))
        self.assertEqual(dcc.send_fails_paused(NEW), 3)

    def test_the_new_count_larger_is_kept_with_its_own_flag(self):
        self.assertEqual(self.merge(entry(1, False), entry(3, True)), entry(3, True))

    def test_the_new_count_larger_and_unpaused_stays_unpaused(self):
        self.set_config(DAILY_SEND_FAILS_LIMIT=5)
        self.assertEqual(self.merge(entry(1, False), entry(2, False)), entry(2, False))
        self.assertEqual(dcc.send_fails_paused(NEW), 0)

    def test_equal_counts_keep_the_old_nicks_pause(self):
        self.assertEqual(self.merge(entry(3, True), entry(3, False)), entry(3, True))

    def test_equal_counts_keep_the_new_nicks_pause(self):
        self.assertEqual(self.merge(entry(3, False), entry(3, True)), entry(3, True))

    def test_equal_counts_with_no_pause_stay_unpaused(self):
        self.assertEqual(self.merge(entry(2, False), entry(2, False)), entry(2, False))

    def test_never_the_sum(self):
        """Two people's failures are not one person's: 2 and 2 under a limit
        of 3 pause nobody, and 2 and 1 stay at 2."""
        self.assertEqual(self.merge(entry(2, False), entry(2, False))["fails"], 2)
        self.assertEqual(dcc.send_fails_paused(NEW), 0)
        runtime.failed_transfers.clear()
        self.assertEqual(self.merge(entry(2, False), entry(1, False))["fails"], 2)
        self.assertEqual(dcc.send_fails_paused(NEW), 0)


class UnderItsOwnLock(RenameCase):
    """runtime.failed_transfers is changed under failed_transfers_lock and not
    inside queue_lock: dcc.py never takes one inside the other."""

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

    def test_the_move_holds_the_counts_lock_and_not_the_queues(self):
        fails_lock = self._Recording(threading.Lock())
        queue_lock = self._Recording(threading.Lock())
        seen = []

        class Watched(dict):
            def _note(self):
                seen.append((fails_lock.held_here(), queue_lock.held_here()))

            def pop(self, *args):
                self._note()
                return dict.pop(self, *args)

            def __setitem__(self, key, value):
                self._note()
                dict.__setitem__(self, key, value)

        store = Watched({OLD: entry(3, True)})
        for target, name, value in ((runtime, "failed_transfers_lock", fails_lock),
                                    (runtime, "failed_transfers", store),
                                    (dcc, "queue_lock", queue_lock)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.rename()

        self.assertEqual(dict(store), {NEW: entry(3, True)})
        self.assertTrue(seen, "nothing was popped or set")
        self.assertEqual(set(seen), {(True, False)},
                         "(counts lock held, queue lock held) for each change")


# ---------------------------------------------------------------------------
# Through the real NICK line, in front of the real request gate.
# ---------------------------------------------------------------------------

class ARenamedNickAtTheGate(feed.ServesARealRequest):
    """dave fails twice against a limit of 2, changes nick on the wire, and
    asks again. The slots are full, so a request that is taken queues."""

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=2)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        self.fill_the_slots()

    def fail_sends(self, nick, times=1):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(times):
                dcc.note_failed_send(nick)
        self.oserve.queued.clear()

    def nick_line(self, old="dave", new="dave_"):
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_nick_change(":%s!d@host.example NICK :%s" % (old, new))

    def ask(self, user, what="Song.flac"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, user, what, feed.OTHER)
        return out.getvalue()

    def rows(self, user):
        return [row["file"] for row in config.dcc_queue.get(user, [])]

    def refusals(self, user):
        return [m for who, m, *_ in self.oserve.queued if who == user and PAUSED in m]

    def test_the_nick_line_moves_the_pause(self):
        self.fail_sends("dave", 2)
        self.nick_line()

        self.assertEqual(dcc.send_fails_paused("dave_"), 2)
        self.assertEqual(dcc.send_fails_paused("dave"), 0)
        self.assertIn("dave_", config.channel_users[feed.OTHER], "presence moved too")

    def test_a_paused_nick_renamed_is_refused_and_told_why(self):
        self.fail_sends("dave", 2)
        self.nick_line()

        out = self.ask("dave_")

        self.assertEqual(self.rows("dave_"), [], "the renamed nick was served")
        told = self.refusals("dave_")
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn("failed 2 times today", told[0])
        self.assertIn("Refused a request from dave_", out)

    def test_one_failure_before_and_one_after_the_rename_pause_it(self):
        self.fail_sends("dave")
        self.nick_line()
        self.fail_sends("dave_")

        self.ask("dave_")

        self.assertEqual(self.rows("dave_"), [])
        self.assertEqual(len(self.refusals("dave_")), 1)

    def test_one_failure_and_a_rename_is_still_served(self):
        """Control: the count follows, it does not grow by moving."""
        self.fail_sends("dave")
        self.nick_line()

        self.ask("dave_")

        self.assertEqual(self.rows("dave_"), ["Song.flac"])
        self.assertEqual(self.refusals("dave_"), [])

    def test_someone_who_takes_the_old_name_is_served(self):
        self.fail_sends("dave", 2)
        self.nick_line()
        config.channel_users[feed.OTHER].add("dave")

        self.ask("dave")

        self.assertEqual(self.rows("dave"), ["Song.flac"])
        self.assertEqual(self.refusals("dave"), [])

    def test_the_list_request_is_refused_too(self):
        """list.send_file_list() asks the same gate before "Preparing"."""
        patch = mock.patch.object(list_mod, "oserve", self.oserve)
        patch.start()
        self.addCleanup(patch.stop)
        downloads = []
        patch = mock.patch.object(dcc, "handle_download_request",
                                  lambda *args: downloads.append(args))
        patch.start()
        self.addCleanup(patch.stop)
        with io.open(os.path.join(self.tree.lists, "DCCoreTest-2026-03-04.zip"), "wb") as handle:
            handle.write(b"PK\x05\x06" + b"\x00" * 18)
        self.fail_sends("dave", 2)
        self.nick_line()

        with contextlib.redirect_stdout(io.StringIO()):
            list_mod.send_file_list(self.sock, "dave_", feed.OTHER)

        told = [m for who, m, *_ in self.oserve.queued if who == "dave_"]
        self.assertEqual(len(told), 1, told)
        self.assertIn(PAUSED, told[0])
        self.assertEqual(downloads, [])


if __name__ == "__main__":
    unittest.main()
