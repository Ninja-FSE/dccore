"""A nick paused while its own !rar folder is being packed stops that pack (#1468).

DAILY_SEND_FAILS_LIMIT pauses a nick and drops its queue (#1389, #1460). A
folder row among the dropped ones could be the one rar was packing right
then - the limit reached at the last failed send, the dispatcher dropping
the queue of a nick already paused (the limit lowered during the day), or a
rename that carried a pause onto the owner. The row went, but rar ran on to
the end, holding the one pack interlock for every other nick, and the packer
then logged that the nick had removed the folder, which it had not.

_pause_for_send_fails() now stops a pack of any row it drops, once
queue_lock is released, with cancel_pack(row=..., reason=...): the user's
own remove (#1268) with a reason the packer logs instead. The two other
places the packer finds the row gone - before rar began, and after it
finished but before the hand-off - say the same when the owner is paused.

rar is the slow stand-in of the running-pack tests: a real process that
writes until it is told to stop, so a stop is a real terminate on every CI
platform. The pause is the real one, through note_failed_send(), the real
dispatcher and the real NICK line. Every nick and folder here is invented.
"""

import contextlib
import datetime
import io
import os
import threading
import unittest
from unittest import mock

import commands
import dcc
import defaults as config
import irc
import runtime
import transfer_log

from tests import test_a_running_pack_can_be_seen_and_cancelled as packing
from tests.support import RecordingSocket

USER = packing.USER
OTHER = packing.OTHER
RENAMED = USER + "_"
ALBUM = "Metallica/Black Album (1991)"
LEAF = "Black Album (1991)"
OTHER_ALBUM = "Pink Floyd/The Wall (1979)"
CHANNEL = "#somechannel"
TODAY = datetime.date(2026, 3, 4)
LIMIT = 3
FALLBACK = "system_next_trigger_fallback"
PAUSED = "paused until midnight"
ONE_REMOVED = "the 1 file left in your queue was removed"
# rar writes until it is stopped: any wait shorter than the cleanup's tells
# a stopped rar from one that runs on. Generous for a slow CI runner.
STOPPED_WITHIN = 8


def stopped_line(owner):
    return (f"[LINEAR RAR] The pack of {LEAF} for {owner} is stopped: "
            f"{owner} is paused for failed sends today.")


class _PauseCase(packing.RunningPackTests):
    """The running-pack fixture, with the limit at 3, today fixed, and the
    record, the operator-cancel path and the log watched."""

    def setUp(self):
        super().setUp()
        self.set_config(DAILY_SEND_FAILS_LIMIT=LIMIT)
        self.patch(dcc, "_send_fails_today", lambda: TODAY)

        self.recorded = []

        def record_unfinished(direction, status, kind, size, reached, nick=None,
                              item_key=None, name=None):
            self.recorded.append((status, nick))
            return True

        self.patch(dcc.transfer_log, "record_unfinished", record_unfinished)

        self.released = []
        real_release = dcc.release_queue_entry

        def release_queue_entry(user, next_file, *args, **kwargs):
            self.released.append((user, kwargs.get("reason")))
            return real_release(user, next_file, *args, **kwargs)

        self.patch(dcc, "release_queue_entry", release_queue_entry)
        self.log = io.StringIO()

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- ways the owner of the running pack becomes paused ------------------

    def fail_sends(self, nick, times=LIMIT):
        for _ in range(times):
            dcc.note_failed_send(nick)

    def paused_by_its_last_failed_send(self):
        """The failure that reaches the limit, while the pack runs."""
        self.fail_sends(USER)
        return USER

    def paused_by_the_dispatcher(self):
        """3 failures under a limit of 5 (before the pack began, see
        before_the_pack()); lowered to 3 while it runs; the next dispatch
        drops the queue of the nick that is now paused (#1460)."""
        self.set_config(DAILY_SEND_FAILS_LIMIT=LIMIT)
        dcc.check_queue_and_send(RecordingSocket(), FALLBACK)
        return USER

    def paused_by_a_rename(self):
        """dave_ was paused earlier today; dave becomes dave_ while rar runs,
        and the next dispatch finds the pause on dave's queue."""
        irc.answer_nick_change(f":{USER}!d@host.example NICK :{RENAMED}")
        dcc.check_queue_and_send(RecordingSocket(), FALLBACK)
        return RENAMED

    def before_the_pack(self, way):
        with contextlib.redirect_stdout(io.StringIO()):
            if way == self.paused_by_the_dispatcher:
                self.set_config(DAILY_SEND_FAILS_LIMIT=5)
                self.fail_sends(USER)
                self.assertEqual(dcc.send_fails_paused(USER), 0)
            elif way == self.paused_by_a_rename:
                self.fail_sends(RENAMED)
                self.assertEqual(dcc.send_fails_paused(RENAMED), LIMIT)

    # --- driving it -----------------------------------------------------------

    def start_the_pack(self, way, someone_waits=False):
        self.before_the_pack(way)
        self.start_a_pack()
        self.process = self.rars[0]
        self.job = runtime.pack_job
        self.archive = self.job["archive"]
        self.packer = runtime.packer_thread
        self.assertIsNotNone(self.packer)
        if someone_waits:
            self.request(OTHER, OTHER_ALBUM)
            self.assertEqual(len(self.rars), 1, "the second pack must wait for the interlock")
        self.oserve.queued.clear()
        self.recorded.clear()

    def pause_while_it_runs(self, way, someone_waits=False):
        """Start dave's pack, pause its owner `way` while rar runs, and wait
        for rar and the packer to end. Returns the owner's nick now."""
        self.start_the_pack(way, someone_waits)
        with contextlib.redirect_stdout(self.log):
            owner = way()
            self.wait_for(lambda: self.process.poll() is not None,
                          "rar to be stopped", seconds=STOPPED_WITHIN)
            self.packer.join(10)
        self.assertFalse(self.packer.is_alive(), "the packer did not finish")
        return owner

    def notices_to(self, nick):
        return [m for who, m, _vip in self.oserve.queued if who == nick]


class _WhatThePauseDoesToThePack:
    """The same checks for each way the owner becomes paused. `way` names
    the _PauseCase method that pauses it."""

    way = None

    def pause(self, someone_waits=False):
        return self.pause_while_it_runs(getattr(self, self.way), someone_waits)

    def test_rar_is_stopped_and_its_partial_archive_removed(self):
        self.pause()

        self.assertIsNotNone(self.process.poll())
        self.assertFalse(os.path.exists(self.archive), "the partial archive was left behind")
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])

    def test_the_pack_is_recorded_as_cancelled_once(self):
        owner = self.pause()

        self.assertEqual(self.recorded, [(transfer_log.STATUS_CANCELLED, owner)])

    def test_the_owner_is_told_once_and_only_of_the_pause(self):
        owner = self.pause()

        told = self.notices_to(owner)
        self.assertEqual(len(told), 1, self.oserve.queued)
        self.assertIn(PAUSED, told[0])
        self.assertIn(ONE_REMOVED, told[0])
        everything = " ".join(m for _who, m, _vip in self.oserve.queued)
        self.assertNotIn("cancelled", everything)
        if owner != USER:
            self.assertEqual(self.notices_to(USER), [])

    def test_it_is_not_settled_as_an_operator_cancel(self):
        self.pause()

        self.assertEqual(self.released, [])

    def test_the_interlocks_are_released_and_no_job_is_left(self):
        owner = self.pause()

        self.assertFalse(config.rar_inprogress, "packing stays blocked for everyone")
        self.assertNotIn(owner.lower(), config.user_processing_lock)
        self.assertNotIn(USER, config.user_processing_lock)
        self.assertIsNone(runtime.pack_job)
        self.assertNotIn(owner.lower(), config.dcc_queue)
        self.assertNotIn(USER, config.dcc_queue)

    def test_the_next_waiting_pack_starts(self):
        self.pause(someone_waits=True)

        self.wait_for(lambda: len(self.rars) == 2, "the waiting pack to start")
        self.wait_for(lambda: (dcc.pack_status() or {}).get("user") == OTHER, "its status")
        self.assertEqual(dcc.pack_status()["name"], "The Wall (1979)")

    def test_the_log_says_the_pause_stopped_it_not_a_remove(self):
        owner = self.pause()

        log = self.log.getvalue()
        self.assertIn(stopped_line(owner), log)
        self.assertNotIn(f"removed {LEAF}", log)
        self.assertNotIn(f"{owner} removed", log)


class PausedByItsLastFailedSend(_WhatThePauseDoesToThePack, _PauseCase):
    way = "paused_by_its_last_failed_send"


class PausedByTheDispatcher(_WhatThePauseDoesToThePack, _PauseCase):
    way = "paused_by_the_dispatcher"


class PausedByARename(_WhatThePauseDoesToThePack, _PauseCase):
    way = "paused_by_a_rename"


class WhatAPauseLeavesAlone(_PauseCase):

    def test_another_nicks_pause_does_not_stop_the_pack(self):
        self.start_the_pack(self.paused_by_its_last_failed_send, someone_waits=True)
        self.assertTrue(config.dcc_queue.get(OTHER), "dan's own folder must be queued")

        with contextlib.redirect_stdout(self.log):
            self.fail_sends(OTHER)

        self.assertNotIn(OTHER, config.dcc_queue, "dan's queue was not dropped")
        self.assertIs(runtime.pack_job, self.job)
        self.assertFalse(self.job["cancelled"])
        self.assertIsNone(self.process.poll(), "dave's rar was stopped")
        self.assertEqual(self.notices_to(USER), [])
        self.assertEqual(self.released, [])

    def test_stop_pack_false_leaves_the_running_pack_alone(self):
        self.start_the_pack(self.paused_by_its_last_failed_send)

        with contextlib.redirect_stdout(self.log):
            removed = dcc._pause_for_send_fails(USER, USER, LIMIT, stop_pack=False)

        self.assertEqual(removed, 1)
        self.assertIs(runtime.pack_job, self.job)
        self.assertFalse(self.job["cancelled"])
        self.assertNotIn("stop_reason", self.job)
        self.assertIsNone(self.process.poll())

    def test_a_pause_of_the_nick_itself_does_stop_it(self):
        """The control for the two above: the same call, stop_pack left as
        it is."""
        self.start_the_pack(self.paused_by_its_last_failed_send)

        with contextlib.redirect_stdout(self.log):
            dcc._pause_for_send_fails(USER, USER, LIMIT)

        self.assertTrue(self.job["cancelled"])
        self.assertTrue(self.job.get("withdrawn"), "stopped as an operator cancel")
        self.assertEqual(self.job.get("stop_reason"), f"{USER} is paused for failed sends today")


class AUsersOwnRemoveIsUnchanged(_PauseCase):
    """#1268: the user's own remove stops the pack, and the log says they
    removed it - no reason, as before."""

    def test_the_log_still_says_the_user_removed_it(self):
        self.start_the_pack(self.paused_by_its_last_failed_send)

        with contextlib.redirect_stdout(self.log):
            commands.handle_queue_remove(RecordingSocket(), USER, CHANNEL)
            self.wait_for(lambda: self.process.poll() is not None,
                          "rar to be stopped", seconds=STOPPED_WITHIN)
            self.packer.join(10)

        log = self.log.getvalue()
        self.assertIn(f"[LINEAR RAR] {USER} removed {LEAF} while it was being packed; "
                      f"the pack is stopped.", log)
        self.assertNotIn("is stopped:", log)
        self.assertNotIn("paused", log)
        self.assertEqual(self.recorded, [(transfer_log.STATUS_CANCELLED, USER)])
        self.assertNotIn("stop_reason", self.job)


class PackerSeesTheRowGone(_PauseCase):
    """The row goes outside the run of rar: before the pack began, or after
    rar finished and before the hand-off. Nothing is running to stop; the
    log says why the row went - the pause, or else the user's remove."""

    def pause_dave(self):
        self.fail_sends(USER)

    def dave_removes_it(self):
        commands.handle_queue_remove(RecordingSocket(), USER, CHANNEL)

    def run_the_pack_with(self, hook_name, step, rar_finishes=False):
        """Ask for dave's folder; `step` runs once, in the packer, at
        `hook_name` (the library check before the queued check, or
        _end_pack_job() after rar)."""
        if rar_finishes:
            with io.open(self.release, "w"):
                pass
        real = getattr(dcc, hook_name)
        ran = []

        def hooked(*args, **kwargs):
            result = real(*args, **kwargs)
            if not ran and threading.current_thread() is runtime.packer_thread:
                ran.append(threading.current_thread())
                step()
            return result

        self.patch(dcc, hook_name, hooked)
        with contextlib.redirect_stdout(self.log):
            dcc.handle_download_request(RecordingSocket(), USER, "!rar " + ALBUM, CHANNEL)
            self.wait_for(lambda: ran, "the packer to reach " + hook_name)
            ran[0].join(10)
            self.assertFalse(ran[0].is_alive(), "the packer did not finish")
        self.assertEqual(self.recorded, [(transfer_log.STATUS_CANCELLED, USER)])
        self.assertEqual(config.active_transfers, [], "a slot was taken")
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn(USER, config.dcc_queue)
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])
        self.assertEqual(self.released, [])
        return self.log.getvalue()

    def test_paused_before_the_pack_began_it_is_not_packed(self):
        log = self.run_the_pack_with("path_is_in_our_library", self.pause_dave)

        self.assertEqual(self.rars, [], "rar was started")
        self.assertIn(f"[LINEAR RAR] {LEAF} is not packed: {USER} is paused for failed "
                      f"sends today.", log)
        self.assertNotIn(f"{USER} removed", log)
        told = self.notices_to(USER)
        self.assertEqual(len([m for m in told if PAUSED in m]), 1, told)

    def test_removed_before_the_pack_began_says_so_as_before(self):
        log = self.run_the_pack_with("path_is_in_our_library", self.dave_removes_it)

        self.assertEqual(self.rars, [])
        self.assertIn(f"[LINEAR RAR] {USER} removed {LEAF} before it was packed.", log)
        self.assertNotIn("is not packed", log)

    def test_paused_after_rar_finished_the_archive_is_not_sent(self):
        log = self.run_the_pack_with("_end_pack_job", self.pause_dave, rar_finishes=True)

        self.assertEqual(len(self.rars), 1)
        self.assertIn(f"[LINEAR RAR] The packed {LEAF} is not sent: {USER} is paused for "
                      f"failed sends today.", log)
        self.assertNotIn(f"{USER} removed", log)
        told = self.notices_to(USER)
        self.assertEqual(len([m for m in told if PAUSED in m]), 1, told)

    def test_removed_after_rar_finished_says_so_as_before(self):
        log = self.run_the_pack_with("_end_pack_job", self.dave_removes_it, rar_finishes=True)

        self.assertIn(f"[LINEAR RAR] {USER} removed {LEAF} while it was being packed; "
                      f"it is not sent.", log)
        self.assertNotIn("is not sent:", log)


# ---------------------------------------------------------------------------
# Lock order (#1452): _begin_pack_job() takes pack_lock inside queue_lock, so
# queue_lock must never be taken while pack_lock is held. The pause goes
# further and never holds them together: it takes pack_lock (in cancel_pack())
# only once queue_lock is released - and not while its caller holds
# failed_transfers_lock.
# ---------------------------------------------------------------------------

class _Held(threading.local):
    def __init__(self):
        self.names = set()
        self.pausing = 0


class _WatchedLock:
    """The real lock, which notes on every acquire what this thread already
    holds and whether it is inside _pause_for_send_fails()."""

    def __init__(self, name, real, held, log):
        self.name, self.real, self.held, self.log = name, real, held, log

    def acquire(self, blocking=True, timeout=-1):
        self.log.append((self.name, frozenset(self.held.names), self.held.pausing > 0))
        got = self.real.acquire(blocking, timeout)
        if got:
            self.held.names.add(self.name)
        return got

    def release(self):
        self.held.names.discard(self.name)
        self.real.release()

    def locked(self):
        return self.real.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
        return False


class TheLockOrder(_PauseCase):

    def watch_the_locks(self):
        held, log = _Held(), []
        queue = _WatchedLock("queue_lock", runtime.queue_lock, held, log)
        self.patch(dcc, "queue_lock", queue)
        self.patch(runtime, "queue_lock", queue)
        self.patch(runtime, "pack_lock",
                   _WatchedLock("pack_lock", runtime.pack_lock, held, log))
        self.patch(runtime, "failed_transfers_lock",
                   _WatchedLock("failed_transfers_lock", runtime.failed_transfers_lock, held, log))
        real_pause = dcc._pause_for_send_fails

        def pausing(*args, **kwargs):
            held.pausing += 1
            try:
                return real_pause(*args, **kwargs)
            finally:
                held.pausing -= 1

        self.patch(dcc, "_pause_for_send_fails", pausing)
        return log

    def assertPackLockTakenAlone(self, log):
        on_the_pause = [(name, held) for name, held, pausing in log if pausing]
        self.assertIn("pack_lock", [name for name, _held in on_the_pause],
                      "the pause never reached cancel_pack(), so this proves nothing")
        self.assertIn("queue_lock", [name for name, _held in on_the_pause])
        inside = [(name, sorted(held)) for name, held in on_the_pause
                  if name == "pack_lock" and held & {"queue_lock", "failed_transfers_lock"}]
        self.assertEqual(inside, [], "pack_lock taken while another lock was held")

    def test_by_the_last_failed_send(self):
        self.start_the_pack(self.paused_by_its_last_failed_send)
        log = self.watch_the_locks()

        with contextlib.redirect_stdout(self.log):
            self.paused_by_its_last_failed_send()
            self.wait_for(lambda: self.process.poll() is not None,
                          "rar to be stopped", seconds=STOPPED_WITHIN)
            self.packer.join(10)

        self.assertIn("failed_transfers_lock", [name for name, _h, pausing in log if not pausing])
        self.assertPackLockTakenAlone(log)

    def test_by_the_dispatcher(self):
        self.start_the_pack(self.paused_by_the_dispatcher)
        log = self.watch_the_locks()

        with contextlib.redirect_stdout(self.log):
            self.paused_by_the_dispatcher()
            self.wait_for(lambda: self.process.poll() is not None,
                          "rar to be stopped", seconds=STOPPED_WITHIN)
            self.packer.join(10)

        self.assertPackLockTakenAlone(log)


class TheRowIsOutBeforeThePackStops(_PauseCase):
    """A packer that ends the moment it is stopped, and a dispatch that
    runs right then, find dave's row already gone: they neither pack it
    again nor tell dave a second time."""

    def test_a_dispatch_in_that_moment_finds_nothing_to_drop(self):
        self.start_the_pack(self.paused_by_its_last_failed_send)
        real_cancel = dcc.cancel_pack
        packer = self.packer
        once = []

        def cancel_then_the_packer_ends_and_a_dispatch_runs(*args, **kwargs):
            status = real_cancel(*args, **kwargs)
            if once:
                return status
            once.append(True)
            packer.join(10)
            if packer.is_alive():
                raise AssertionError("the packer could not end while the pause ran")
            dcc.check_queue_and_send(RecordingSocket(), FALLBACK)
            return status

        self.patch(dcc, "cancel_pack", cancel_then_the_packer_ends_and_a_dispatch_runs)

        with contextlib.redirect_stdout(self.log):
            self.paused_by_its_last_failed_send()

        self.assertFalse(packer.is_alive())
        told = self.notices_to(USER)
        self.assertEqual(len(told), 1, told)
        self.assertIn(ONE_REMOVED, told[0])
        self.assertEqual(len(self.rars), 1, "the dropped row was packed again")
        self.assertEqual(self.recorded, [(transfer_log.STATUS_CANCELLED, USER)])


for _name in [n for n in dir(packing.RunningPackTests) if n.startswith("test")]:
    setattr(_PauseCase, _name, None)


if __name__ == "__main__":
    unittest.main()
