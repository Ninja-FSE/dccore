"""A nick paused for failed sends is sent nothing from its queue (#1460).

DAILY_SEND_FAILS_LIMIT (#1389) pauses a nick whose sends keep failing, and
the pause removes what that nick has queued. Only the request gate asked
send_fails_paused(), though: the dispatcher sent whatever was queued, so
rows still queued under a paused nick went out. Two ways they were there:

  * a rename merged a pause onto a nick that had a queue of its own - or,
    on a tie after the limit was changed during the day, the merged entry
    was paused for a queue that was never removed (#1437);
  * a NICK landed between note_failed_send() counting the failure and
    _pause_for_send_fails() removing the queue: the queue and the pause
    moved to the new nick, and the removal looked under the old one.

check_queue_and_send() now drops the queue of every paused nick first
(drop_queues_of_paused_nicks()), and each place that hands out a slot
passes a paused nick over: section A, claim_next_in_line() (section B) and
the hand-off of a finished !rar archive in pack_folder_for_request().

Driven through the real NICK line (irc.answer_nick_change()), the real
failure count (dcc.note_failed_send()) and the real dispatcher, with dcc's
threads recorded instead of started. The rar binary is a stand-in.

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
import platform_compat  # noqa: E402
import runtime  # noqa: E402
import transfer_log  # noqa: E402

from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402
from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

TODAY = datetime.date(2026, 3, 4)
PAUSED = "paused until midnight"
OTHER = feed.OTHER
FALLBACK = "system_next_trigger_fallback"
FILES = ("A.flac", "B.flac", "C.flac", "D.flac")


def entry(fails, paused, day=TODAY):
    return {"day": day, "fails": fails, "paused": paused}


# ---------------------------------------------------------------------------
# Locks that say who holds them, to show the two are never held together.
# ---------------------------------------------------------------------------

class _TrackedLock:
    """A real lock that notes, on every acquire, which of the other tracked
    locks the acquiring thread already holds."""

    def __init__(self, name, log):
        self.real = threading.Lock()
        self.name = name
        self.log = log
        self.holder = None
        self.others = ()

    def held_here(self):
        return self.holder == threading.get_ident()

    def acquire(self, blocking=True, timeout=-1):
        self.log.append((self.name, tuple(o.name for o in self.others if o.held_here())))
        got = self.real.acquire(blocking, timeout)
        if got:
            self.holder = threading.get_ident()
        return got

    def release(self):
        self.holder = None
        self.real.release()

    def locked(self):
        return self.real.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


def track_the_two_locks(test):
    """Swap queue_lock and failed_transfers_lock for tracked ones until the
    test ends. Returns the log: (lock acquired, locks already held)."""
    log = []
    queue = _TrackedLock("queue_lock", log)
    fails = _TrackedLock("failed_transfers_lock", log)
    queue.others = (fails,)
    fails.others = (queue,)
    for target, name, value in ((dcc, "queue_lock", queue),
                                (runtime, "queue_lock", queue),
                                (runtime, "failed_transfers_lock", fails)):
        patch = mock.patch.object(target, name, value)
        patch.start()
        test.addCleanup(patch.stop)
    return log


class _LockOrder:
    """Mixin: the assertion over a tracked-lock log."""

    def assertNeverHeldTogether(self, log):
        taken = {name for name, _held in log}
        self.assertEqual(taken, {"queue_lock", "failed_transfers_lock"},
                         "the dispatch did not take both locks, so it proves nothing: %r" % log)
        together = [(name, held) for name, held in log if held]
        self.assertEqual(together, [], "one lock taken while the other was held")


# ---------------------------------------------------------------------------
# The fixture: a served channel, files in the library, the limit at 3.
# ---------------------------------------------------------------------------

class Case(feed.ServesARealRequest, _LockOrder):
    """dcc's threads are recorded, not started: a start_dcc_send() in the
    record is a send the dispatcher gave a slot to."""

    LIMIT = 3

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=self.LIMIT, MAX_DCC_SLOTS=1,
                        transfers_paused=False, rar_inprogress=False)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        self.debug_lines = silence_debug(announce)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        self.addCleanup(runtime.queue_waiting_since.clear)
        config.channel_users[OTHER] = set()
        for name in FILES:
            with io.open(os.path.join(self.tree.music, name), "wb") as handle:
                handle.write(b"\x00" * 4096)

    # --- building the state -------------------------------------------------

    def row(self, nick, name):
        return {"file": name, "path": os.path.join(self.tree.music, name), "channel": OTHER,
                "user_raw": nick, "is_temporary_zip": False}

    def folder_row(self, nick, name="Some Album"):
        folder = os.path.join(self.tree.music, name)
        return {"file": name, "path": folder, "channel": OTHER, "user_raw": nick,
                "is_unpacked_rar_folder": True, "is_temporary_zip": True}

    def waiting(self, nick, since, *rows):
        config.dcc_queue[nick] = list(rows) or [self.row(nick, FILES[0])]
        runtime.queue_waiting_since[nick] = since

    def present(self, *nicks):
        config.channel_users[OTHER].update(nicks)

    def count_failures(self, nick, times=1):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(times):
                dcc.note_failed_send(nick)

    def nick_line(self, old, new):
        with contextlib.redirect_stdout(io.StringIO()):
            irc.answer_nick_change(":%s!u@host.example NICK :%s" % (old, new))

    def a_nick_line_after_the_drop(self, old, new):
        """The NICK lands after drop_queues_of_paused_nicks() and before a
        claim: the dispatcher's drop has run, and the rows it brings sit
        under a paused nick when the claim looks."""
        real = dcc.drop_queues_of_paused_nicks
        done = []

        def drop_then_the_nick_line():
            paused = real()
            if not done:
                done.append(True)
                self.nick_line(old, new)
            return paused

        patch = mock.patch.object(dcc, "drop_queues_of_paused_nicks", drop_then_the_nick_line)
        patch.start()
        self.addCleanup(patch.stop)
        return done

    def dispatch(self, completed=FALLBACK):
        self.quietly(dcc.check_queue_and_send, completed)

    # --- reading what happened ----------------------------------------------

    def started(self):
        return [args[1] for name, args in feed.InlineThread.dispatched if name == "start_dcc_send"]

    def woken(self):
        return [args[1] for name, args in feed.InlineThread.dispatched if name == "check_queue_and_send"]

    def claimed(self):
        return [tx["user"] for tx in config.active_transfers]

    def queued(self, nick):
        return [r["file"] for r in config.dcc_queue.get(nick, [])]

    def paused_notices(self, nick):
        return [m for who, m, *_ in self.oserve.queued if who == nick and PAUSED in m]

    def assertNothingFor(self, nick):
        self.assertNotIn(nick, self.started(), "a send was started for the paused nick")
        self.assertNotIn(nick, [u.lower() for u in self.claimed()], "the paused nick was given a slot")
        self.assertNotIn(nick, config.user_processing_lock)


# ---------------------------------------------------------------------------
# A rename merges a pause onto a nick that has a queue.
# ---------------------------------------------------------------------------

class ARenameMergesAPauseOntoAQueue(Case):
    """alfa is paused at the limit (its queue removed then), and renames to
    bravo, a nick that still has two files queued."""

    def setUp(self):
        super().setUp()
        self.present("alfa")
        self.waiting("bravo", 100.0, self.row("bravo", "A.flac"), self.row("bravo", "B.flac"))

    def pause_alfa_and_rename(self):
        self.count_failures("alfa", 3)
        self.assertEqual(dcc.send_fails_paused("alfa"), 3, "the fixture pauses alfa")
        self.nick_line("alfa", "bravo")
        self.assertEqual(dcc.send_fails_paused("bravo"), 3, "the pause did not follow the nick")
        self.assertEqual(self.queued("bravo"), ["A.flac", "B.flac"], "the fixture keeps bravo's queue")
        self.oserve.queued.clear()
        del self.debug_lines[:]

    def assertDropped(self):
        self.assertNothingFor("bravo")
        self.assertEqual(self.claimed(), [])
        self.assertNotIn("bravo", config.dcc_queue)
        told = self.paused_notices("bravo")
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn("the 2 files left in your queue were removed", told[0])

    def test_the_sweep_sends_nothing_and_drops_the_queue(self):
        self.pause_alfa_and_rename()

        self.dispatch(FALLBACK)
        self.assertDropped()

        self.dispatch("bravo")
        self.assertDropped()

    def test_a_dispatch_for_the_nick_itself_sends_nothing(self):
        self.pause_alfa_and_rename()

        self.dispatch("bravo")
        self.assertDropped()

        self.dispatch(FALLBACK)
        self.assertDropped()

    def test_the_operator_is_told_once(self):
        self.pause_alfa_and_rename()

        self.dispatch(FALLBACK)
        self.dispatch("bravo")

        said = [text for _cat, text in self.debug_lines if PAUSED in text]
        self.assertEqual(len(said), 1, self.debug_lines)
        self.assertIn("2 queued file(s) removed", said[0])

    def test_the_tie_after_the_limit_was_lowered_during_the_day(self):
        """alfa paused at 3; the limit raised to 5, under which bravo fails 3
        times and keeps its queue; the limit back at 3. On the rename the
        counts tie and the merged entry keeps alfa's pause - for bravo's
        queue, which no pause ever removed."""
        self.count_failures("alfa", 3)
        self.set_config(DAILY_SEND_FAILS_LIMIT=5)
        self.count_failures("bravo", 3)
        self.assertEqual(self.queued("bravo"), ["A.flac", "B.flac"], "bravo was not paused at 3 of 5")
        self.set_config(DAILY_SEND_FAILS_LIMIT=3)
        self.nick_line("alfa", "bravo")
        self.assertEqual(runtime.failed_transfers["bravo"], entry(3, True), "the tie keeps the pause")
        self.oserve.queued.clear()

        self.dispatch(FALLBACK)
        self.dispatch("bravo")

        self.assertDropped()

    def test_another_nick_waiting_gets_the_slot(self):
        self.present("charlie")
        self.waiting("charlie", 200.0, self.row("charlie", "C.flac"))
        self.pause_alfa_and_rename()

        self.dispatch(FALLBACK)

        self.assertEqual(self.started(), ["charlie"])
        self.assertNotIn("bravo", config.dcc_queue)

    def test_neither_lock_is_taken_inside_the_other(self):
        self.pause_alfa_and_rename()
        log = track_the_two_locks(self)

        self.dispatch(FALLBACK)

        self.assertNotIn("bravo", config.dcc_queue, "the dispatch did not reach the drop")
        self.assertNeverHeldTogether(log)


# ---------------------------------------------------------------------------
# A NICK between counting the failure and removing the queue.
# ---------------------------------------------------------------------------

class ANickBetweenTheCountAndTheRemoval(Case):
    """kilo has two files queued and two failures; the third is counted, and
    kilo becomes kilo_away before _pause_for_send_fails() takes the queue."""

    def setUp(self):
        super().setUp()
        self.present("kilo")
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"), self.row("kilo", "B.flac"))
        self.count_failures("kilo", 2)
        real = dcc._pause_for_send_fails
        raced = []

        def nick_line_first(*args, **kwargs):
            if not raced:
                raced.append(args)
                self.nick_line("kilo", "kilo_away")
            return real(*args, **kwargs)

        patch = mock.patch.object(dcc, "_pause_for_send_fails", nick_line_first)
        patch.start()
        self.addCleanup(patch.stop)
        self.count_failures("kilo")
        self.assertEqual(raced[0][:3], ("kilo", "kilo", 3), "the race was not reproduced")

    def test_the_new_nick_holds_the_queue_and_the_pause(self):
        """What the race leaves: the removal looked under the old nick."""
        self.assertEqual(self.queued("kilo_away"), ["A.flac", "B.flac"])
        self.assertEqual(dcc.send_fails_paused("kilo_away"), 3)
        self.assertNotIn("kilo", config.dcc_queue)

    def test_the_next_dispatch_drops_it_and_sends_nothing(self):
        self.oserve.queued.clear()

        self.dispatch(FALLBACK)

        self.assertNothingFor("kilo_away")
        self.assertEqual(self.claimed(), [])
        self.assertNotIn("kilo_away", config.dcc_queue)
        told = self.paused_notices("kilo_away")
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn("the 2 files left in your queue were removed", told[0])

    def test_a_dispatch_for_the_new_nick_sends_nothing_either(self):
        self.dispatch("kilo_away")

        self.assertNothingFor("kilo_away")
        self.assertNotIn("kilo_away", config.dcc_queue)


# ---------------------------------------------------------------------------
# Rows under a paused nick when the claim looks: passed over.
# ---------------------------------------------------------------------------

class RowsThatArriveAfterTheDrop(Case):
    """kilo is paused with nothing queued. lima, who has waited longest,
    becomes kilo after the drop and before the claim; mike waits too. One
    slot."""

    def setUp(self):
        super().setUp()
        self.count_failures("kilo", 3)
        self.oserve.queued.clear()
        self.present("lima", "mike")
        self.waiting("mike", 200.0, self.row("mike", "C.flac"))

    def test_a_paused_plain_file_first_in_line_does_not_hold_up_the_next(self):
        self.waiting("lima", 100.0, self.row("lima", "A.flac"))
        renamed = self.a_nick_line_after_the_drop("lima", "kilo")

        self.dispatch(FALLBACK)

        self.assertTrue(renamed, "the NICK did not land")
        self.assertEqual(self.started(), ["mike"], "the slot did not go on to mike")
        self.assertNothingFor("kilo")

    def test_a_paused_folder_pack_first_in_line_keeps_no_slot(self):
        """The skip comes before the slot kept for a pack whose turn has come
        (#1034): with one slot, a pack woken for kilo would have kept it."""
        self.waiting("lima", 100.0, self.folder_row("lima"))
        renamed = self.a_nick_line_after_the_drop("lima", "kilo")

        self.dispatch(FALLBACK)

        self.assertTrue(renamed)
        self.assertEqual(self.started(), ["mike"])
        self.assertEqual(self.woken(), [], "a pack was woken for the paused nick")
        self.assertNothingFor("kilo")

    def test_the_next_dispatch_drops_what_was_passed_over(self):
        self.waiting("lima", 100.0, self.row("lima", "A.flac"))
        self.a_nick_line_after_the_drop("lima", "kilo")
        self.dispatch(FALLBACK)
        self.assertEqual(self.queued("kilo"), ["A.flac"], "passed over, kept until the next drop")

        self.dispatch(FALLBACK)

        self.assertNotIn("kilo", config.dcc_queue)
        self.assertEqual(len(self.paused_notices("kilo")), 1)
        self.assertEqual(self.started(), ["mike"])

    def test_section_a_does_not_pick_a_paused_finishing_nick(self):
        """A dispatch for kilo itself, who has waited longest: section A
        would hand kilo's head the slot. It goes through section B to mike."""
        self.waiting("lima", 100.0, self.row("lima", "A.flac"))
        renamed = self.a_nick_line_after_the_drop("lima", "kilo")

        self.dispatch("kilo")

        self.assertTrue(renamed)
        self.assertNothingFor("kilo")
        self.assertEqual(self.started(), ["mike"])
        self.assertEqual(self.claimed(), ["mike"])


class TheSweepOnItsOwn(Case):
    """claim_next_in_line() called directly: it claims and returns."""

    def setUp(self):
        super().setUp()
        self.present("kilo", "mike")
        self.waiting("mike", 200.0, self.row("mike", "C.flac"))

    def test_a_paused_nick_first_in_line_is_passed_over(self):
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"))

        promoted, key, pack, absent = dcc.claim_next_in_line(paused={"kilo": 3})

        self.assertEqual((key, pack, absent), ("mike", None, []))
        self.assertEqual(self.claimed(), ["mike"])

    def test_a_paused_pack_first_in_line_is_passed_over_before_its_slot_is_kept(self):
        self.waiting("kilo", 100.0, self.folder_row("kilo"))

        promoted, key, pack, absent = dcc.claim_next_in_line(paused={"kilo": 3})

        self.assertEqual((key, pack), ("mike", None))

    def test_with_two_slots_the_pack_is_not_woken_either(self):
        self.set_config(MAX_DCC_SLOTS=2)
        self.waiting("kilo", 100.0, self.folder_row("kilo"))

        _promoted, key, pack, _absent = dcc.claim_next_in_line(paused={"kilo": 3})

        self.assertEqual((key, pack), ("mike", None))

    def test_a_paused_nick_that_is_absent_is_not_frozen(self):
        """Passed over before the presence test, so not returned for a freeze."""
        config.channel_users[OTHER].discard("kilo")
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"))

        _promoted, key, _pack, absent = dcc.claim_next_in_line(paused={"kilo": 3})

        self.assertEqual((key, absent), ("mike", []))

    def test_without_paused_given_it_reads_the_paused_set_itself(self):
        self.count_failures("kilo", 3)
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"))

        _promoted, key, _pack, _absent = dcc.claim_next_in_line()

        self.assertEqual(key, "mike")

    def test_reading_it_itself_takes_neither_lock_inside_the_other(self):
        self.count_failures("kilo", 3)
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"))
        log = track_the_two_locks(self)

        _promoted, key, _pack, _absent = dcc.claim_next_in_line()

        self.assertEqual(key, "mike")
        self.assertNeverHeldTogether(log)

    def test_control_a_nick_that_is_not_paused_is_claimed_first(self):
        self.waiting("kilo", 100.0, self.row("kilo", "A.flac"))

        _promoted, key, _pack, _absent = dcc.claim_next_in_line(paused={})

        self.assertEqual(key, "kilo")


# ---------------------------------------------------------------------------
# Nothing is skipped or dropped when nobody is paused.
# ---------------------------------------------------------------------------

class WhenItShouldNotAct(Case):

    def setUp(self):
        super().setUp()
        self.present("bravo")
        self.waiting("bravo", 100.0, self.row("bravo", "A.flac"), self.row("bravo", "B.flac"))

    def assertSent(self):
        self.assertEqual(self.started(), ["bravo"])
        self.assertEqual(self.queued("bravo"), ["A.flac", "B.flac"], "a queue was dropped")
        self.assertEqual(self.paused_notices("bravo"), [])

    def test_with_the_limit_off_a_count_left_from_when_it_was_on_holds_nothing_back(self):
        runtime.failed_transfers["bravo"] = entry(9, True)
        self.set_config(DAILY_SEND_FAILS_LIMIT=0)

        self.assertEqual(dcc.drop_queues_of_paused_nicks(), {})
        self.dispatch(FALLBACK)

        self.assertSent()

    def test_an_admin_is_sent_whatever_the_count_says(self):
        self.set_config(ADMIN_NICK="Bravo")
        runtime.failed_transfers["bravo"] = entry(9, True)

        self.assertEqual(dcc.drop_queues_of_paused_nicks(), {})
        self.dispatch("bravo")

        self.assertSent()

    def test_a_nick_under_the_limit_is_sent(self):
        self.count_failures("bravo", 2)
        self.oserve.queued.clear()

        self.dispatch(FALLBACK)

        self.assertSent()

    def test_another_nicks_pause_leaves_this_queue_alone(self):
        self.count_failures("kilo", 3)
        self.oserve.queued.clear()

        self.dispatch("bravo")

        self.assertSent()


# ---------------------------------------------------------------------------
# drop_queues_of_paused_nicks() and _pause_for_send_fails() on their own.
# ---------------------------------------------------------------------------

class UnitCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=3)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        self.saves = []
        patch = mock.patch.object(db, "save_dcc_queue", lambda: self.saves.append(1))
        patch.start()
        self.addCleanup(patch.stop)
        self.debug_lines = silence_debug(announce)
        self.out = io.StringIO()

    def notices(self, nick=None):
        return [m for who, m, *_ in self.oserve.queued if nick is None or who == nick]


class TheDropOnItsOwn(UnitCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        self.archive = os.path.join(config.TMP_ZIP_DIR, "Some_Album.rar")
        with io.open(self.archive, "wb") as handle:
            handle.write(b"Rar!")
        self.packed = {"file": "Some_Album.rar", "path": self.archive, "is_temporary_zip": True,
                       "user_raw": "Bravo"}
        config.dcc_queue["bravo"] = [self.packed,
                                     {"file": "A.flac", "path": "a", "is_temporary_zip": False,
                                      "user_raw": "Bravo"}]
        config.dcc_queue["lima"] = [{"file": "Other.flac", "path": "x", "is_temporary_zip": False,
                                     "user_raw": "lima"}]
        runtime.failed_transfers.update({"bravo": entry(4, True), "kilo": entry(3, True),
                                         "lima": entry(2, False)})

    def drop(self):
        with contextlib.redirect_stdout(self.out):
            return dcc.drop_queues_of_paused_nicks()

    def test_it_returns_every_paused_nick_with_its_count(self):
        self.assertEqual(self.drop(), {"bravo": 4, "kilo": 3})

    def test_it_drops_a_paused_nicks_queue_and_leaves_the_others(self):
        self.drop()

        self.assertNotIn("bravo", config.dcc_queue)
        self.assertEqual(len(config.dcc_queue["lima"]), 1)
        self.assertTrue(self.saves, "the queue was not saved")

    def test_the_notice_goes_to_the_nick_as_the_rows_name_it(self):
        self.drop()

        told = self.notices()
        self.assertEqual([who for who, *_ in self.oserve.queued], ["Bravo"], told)
        self.assertIn("failed 4 times today", told[0])
        self.assertIn("the 2 files left in your queue were removed", told[0])

    def test_a_paused_nick_with_nothing_queued_is_told_nothing(self):
        self.drop()

        self.assertEqual(self.notices("kilo"), [])
        self.assertEqual([t for _c, t in self.debug_lines if "kilo" in t], [])

    def test_its_archive_goes_with_it(self):
        self.drop()

        self.assertFalse(os.path.exists(self.archive))

    def test_an_archive_another_row_names_is_kept(self):
        config.dcc_queue["lima"].append(dict(self.packed, user_raw="lima"))

        self.drop()

        self.assertTrue(os.path.exists(self.archive))

    def test_a_second_drop_finds_nothing_and_says_nothing(self):
        self.drop()
        self.oserve.queued.clear()
        del self.debug_lines[:]

        self.assertEqual(self.drop(), {"bravo": 4, "kilo": 3})

        self.assertEqual(self.oserve.queued, [])
        self.assertEqual(self.debug_lines, [])

    def test_with_nobody_paused_it_touches_nothing(self):
        runtime.failed_transfers.clear()

        self.assertEqual(self.drop(), {})

        self.assertEqual(set(config.dcc_queue), {"bravo", "lima"})
        self.assertEqual(self.oserve.queued, [])


class QueuedOnly(UnitCase):
    """_pause_for_send_fails(..., queued_only=True): the dispatcher's drop of
    a nick already paused, and told so when it was."""

    def setUp(self):
        super().setUp()
        config.frozen_queues["kilo"] = 12345.0

    def pause(self, queued_only):
        with contextlib.redirect_stdout(self.out):
            return dcc._pause_for_send_fails("kilo", "kilo", 3, queued_only=queued_only)

    def test_with_nothing_queued_it_is_silent_and_changes_nothing(self):
        self.assertEqual(self.pause(queued_only=True), 0)

        self.assertEqual(self.oserve.queued, [], "a notice")
        self.assertEqual(self.debug_lines, [], "a debug line")
        self.assertEqual(config.frozen_queues, {"kilo": 12345.0}, "the freeze was lifted")
        self.assertEqual(self.saves, [])
        self.assertNotIn("[DCC PAUSE]", self.out.getvalue())

    def test_with_rows_queued_it_drops_them_and_says_so(self):
        config.dcc_queue["kilo"] = [{"file": "A.flac", "path": "a", "is_temporary_zip": False},
                                    {"file": "B.flac", "path": "b", "is_temporary_zip": False}]

        self.assertEqual(self.pause(queued_only=True), 2)

        self.assertNotIn("kilo", config.dcc_queue)
        self.assertNotIn("kilo", config.frozen_queues)
        self.assertEqual(len(self.notices("kilo")), 1)
        self.assertEqual(len([t for _c, t in self.debug_lines if PAUSED in t]), 1)
        self.assertTrue(self.saves)

    def test_the_pause_itself_still_tells_a_nick_with_nothing_queued(self):
        """Unchanged (#1389): the pause beginning is said, rows or not."""
        self.assertEqual(self.pause(queued_only=False), 0)

        self.assertEqual(len(self.notices("kilo")), 1)
        self.assertNotIn("kilo", config.frozen_queues)

    def test_the_pause_itself_returns_how_many_went(self):
        config.dcc_queue["kilo"] = [{"file": "A.flac", "path": "a", "is_temporary_zip": False}]

        self.assertEqual(self.pause(queued_only=False), 1)


# ---------------------------------------------------------------------------
# A finished !rar archive is not handed to a paused owner.
# ---------------------------------------------------------------------------

class APackFinishedForAPausedOwner(security.PathSecurityBase, _LockOrder):
    """dave asks for a folder; rar runs (a stand-in that writes the archive);
    while it runs, dave becomes paused. Two slots, both free."""

    def setUp(self):
        super().setUp()
        config.channel_users = {"#dccore-test": {"dave"}}
        self.set_config(DAILY_SEND_FAILS_LIMIT=3, MAX_DCC_SLOTS=2, rar_inprogress=False,
                        transfers_paused=False, bot_joined_channel=True)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        patch = mock.patch.object(dcc, "_send_fails_today", lambda: TODAY)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(dcc.time, "sleep", lambda *_a: None)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(platform_compat, "rar_command",
                                  lambda configured=None: os.path.join(self.tree.root, "rar"))
        patch.start()
        self.addCleanup(patch.stop)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        self.addCleanup(setattr, runtime, "pack_owner", None)
        self.addCleanup(setattr, runtime, "pack_job", None)

        self.album = os.path.join(self.tree.music, "Some Artist", "Some Album")
        os.makedirs(self.album, exist_ok=True)
        with io.open(os.path.join(self.album, "01.flac"), "wb") as handle:
            handle.write(b"\x00" * 1024)
        self.archive = os.path.normpath(os.path.join(config.TMP_ZIP_DIR,
                                                     dcc._rar_archive_disk_name(self.album)))
        self.row = {"file": "Some_Album.rar", "path": self.album, "channel": "#dccore-test",
                    "user_raw": "dave", "is_unpacked_rar_folder": True, "is_temporary_zip": True}

        self.while_rar_runs = []

        class Packed:
            returncode = 0
            stdout = stderr = ""

        def rar_that_writes(cmd, *a, **kw):
            with io.open([c for c in cmd if c.endswith(".rar")][0], "wb") as handle:
                handle.write(b"Rar!")
            for step in self.while_rar_runs:
                step()
            return Packed()

        support.fake_rar_runs(self, dcc.subprocess, rar_that_writes)

        self.recorded = []

        def record_unfinished(direction, status, kind, size, reached, nick=None, item_key=None, name=None):
            self.recorded.append((status, kind, nick, item_key))
            return True

        patch = mock.patch.object(dcc.transfer_log, "record_unfinished", record_unfinished)
        patch.start()
        self.addCleanup(patch.stop)

    # --- ways the owner becomes paused while rar runs -----------------------

    def renamed_onto_a_paused_nick(self):
        """dave_ was paused earlier today; dave becomes dave_ while rar runs."""
        with security.quiet():
            for _ in range(3):
                dcc.note_failed_send("dave_")
        self.oserve.queued.clear()

        def nick_line():
            with security.quiet():
                irc.answer_nick_change(":dave!d@host.example NICK :dave_")
        self.while_rar_runs.append(nick_line)
        return "dave_"

    def the_limit_lowered_to_its_count(self):
        """dave failed 3 times under a limit of 5; it is lowered to 3 while
        rar runs."""
        self.set_config(DAILY_SEND_FAILS_LIMIT=5)
        with security.quiet():
            for _ in range(3):
                dcc.note_failed_send("dave")
        self.while_rar_runs.append(lambda: self.set_config(DAILY_SEND_FAILS_LIMIT=3))
        return "dave"

    # --- driving it ---------------------------------------------------------

    def dispatch(self):
        config.dcc_queue["dave"] = [self.row]
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

    def pack(self):
        config.dcc_queue["dave"] = [self.row]
        with security.quiet():
            return dcc.pack_folder_for_request(self.sock, self.row, lambda: "dave", "#dccore-test", None)

    def started(self):
        return [args[1] for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]

    def assertNotSentAndDropped(self, owner, removed_text="the 1 file left in your queue was removed"):
        self.assertEqual(self.started(), [], "the archive was handed to a send")
        self.assertEqual([n for n in self.notices if n[0] == "sending"], [])
        self.assertEqual(config.active_transfers, [], "a slot was taken")
        self.assertNotIn(owner, config.dcc_queue)
        self.assertNotIn("dave", config.dcc_queue)
        told = [m for who, m, *_ in self.oserve.queued if who == owner and PAUSED in m]
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn(removed_text, told[0])

    def assertCancelledOnce(self, owner):
        album_key = dcc._pack_identity(self.album)[0]
        self.assertEqual(self.recorded, [(transfer_log.STATUS_CANCELLED, transfer_log.KIND_ALBUM,
                                          owner, album_key)])

    # --- the tests ------------------------------------------------------------

    def test_renamed_onto_a_paused_nick_the_archive_is_not_sent(self):
        owner = self.renamed_onto_a_paused_nick()

        self.dispatch()

        self.assertNotSentAndDropped(owner)
        self.assertFalse(os.path.exists(self.archive), "the archive was left on disk")
        self.assertCancelledOnce(owner)

    def test_the_interlocks_are_released(self):
        owner = self.renamed_onto_a_paused_nick()

        self.dispatch()

        self.assertFalse(config.rar_inprogress, "packing stays blocked for everyone")
        self.assertNotIn(owner, config.user_processing_lock)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertIsNone(runtime.pack_owner)

    def test_the_packer_says_false_so_its_wrapper_releases_them(self):
        self.the_limit_lowered_to_its_count()

        self.assertIs(self.pack(), False)

        self.assertNotSentAndDropped("dave")
        self.assertCancelledOnce("dave")

    def test_paused_by_the_limit_lowered_while_rar_ran(self):
        self.the_limit_lowered_to_its_count()

        self.dispatch()

        self.assertNotSentAndDropped("dave")
        self.assertFalse(os.path.exists(self.archive))
        self.assertFalse(config.rar_inprogress)

    def test_the_rest_of_the_queue_goes_with_it(self):
        owner = self.renamed_onto_a_paused_nick()
        behind = {"file": "Song.flac", "path": os.path.join(self.tree.music, "Song.flac"),
                  "channel": "#dccore-test", "user_raw": "dave", "is_temporary_zip": False}
        config.dcc_queue["dave"] = [self.row, behind]
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertNotSentAndDropped(owner, "the 2 files left in your queue were removed")

    def test_an_archive_another_row_names_is_kept(self):
        owner = self.renamed_onto_a_paused_nick()
        theirs = {"file": "Some_Album.rar", "path": self.archive, "channel": "#dccore-test",
                  "user_raw": "erin", "is_temporary_zip": True, "is_unpacked_rar_folder": False}

        def erin_queues_the_same_album():
            with dcc.queue_lock:
                config.dcc_queue["erin"] = [theirs]
        self.while_rar_runs.append(erin_queues_the_same_album)

        self.dispatch()

        self.assertNotSentAndDropped(owner)
        self.assertTrue(os.path.exists(self.archive), "erin's archive was deleted")
        self.assertEqual(config.dcc_queue["erin"], [theirs])

    def test_neither_lock_is_taken_inside_the_other(self):
        self.renamed_onto_a_paused_nick()
        log = track_the_two_locks(self)

        self.dispatch()

        self.assertEqual(self.started(), [])
        self.assertNeverHeldTogether(log)

    def test_control_an_owner_who_is_not_paused_is_sent_the_archive(self):
        def nick_line():
            with security.quiet():
                irc.answer_nick_change(":dave!d@host.example NICK :dave_")
        self.while_rar_runs.append(nick_line)

        self.dispatch()

        self.assertEqual(self.started(), ["dave_"])
        self.assertEqual(self.recorded, [])
        self.assertTrue(os.path.exists(self.archive))


if __name__ == "__main__":
    unittest.main()
