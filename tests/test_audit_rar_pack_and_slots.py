"""The folder packer's two blind spots, which are the same blind spot.

`config.active_transfers` is the SEND side. A folder pack has no row there for
as long as it runs: check_queue_and_send() claims `rar_inprogress`, runs `rar`
for up to RAR_TIMEOUT (half an hour by default), and only appends to
active_transfers once the archive exists.

Two things read active_transfers and drew the wrong conclusion from that.

THE QUIESCE SAW AN IDLE BOT

wait_for_transfers_to_finish() polls active_transfers, so it returned True
immediately while a pack was mid-flight. The reload then re-executed
defaults.py - whose body contains `rar_inprogress = False` - and commands.py
cleared `user_processing_lock` outright, both while the packer was still
running. The next `!rar` read both interlocks as free and started a SECOND rar
process. Two packs of the same album target the same archive path, and the
second one removes the file the first is still writing.

THE CAPACITY CHECK WAS MINUTES STALE

The RAR branch checks MAX_DCC_SLOTS before starting rar, and appends its slot
after. For a large album those are minutes apart - long enough for every slot
to fill with plain file sends, each of which re-checked correctly on its own
way through. This was the one append that did not.
"""

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
import platform_compat  # noqa: E402
import runtime  # noqa: E402

from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402


class ARunningPackCountsAsBusy(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(active_transfers=[], rar_inprogress=False,
                        REHASH_TRANSFER_WAIT=60)
        self.addCleanup(lambda: setattr(config, "transfers_paused", False))

    def wait(self, sleep):
        return dcc.wait_for_transfers_to_finish(timeout=60, sleep=sleep,
                                                log=lambda _m: None)

    def test_the_wait_does_not_return_while_a_pack_runs(self):
        config.rar_inprogress = True
        ticks = []

        def tick(_seconds):
            ticks.append(1)
            if len(ticks) >= 3:
                config.rar_inprogress = False   # the pack finishes

        went_quiet = self.wait(tick)

        self.assertTrue(went_quiet)
        self.assertGreaterEqual(len(ticks), 3,
                                "the wait returned while a pack was running")

    def test_an_idle_bot_still_returns_at_once(self):
        """Control: the wait must not start blocking on a flag nobody set."""
        ticks = []

        went_quiet = self.wait(lambda _s: ticks.append(1))

        self.assertTrue(went_quiet)
        self.assertEqual(ticks, [])

    def test_a_pack_and_a_send_together_are_both_waited_for(self):
        config.rar_inprogress = True
        config.active_transfers.append({"user": "alice", "file": "a.flac"})
        ticks = []

        def tick(_seconds):
            ticks.append(1)
            if len(ticks) == 2:
                config.active_transfers.clear()
            if len(ticks) == 4:
                config.rar_inprogress = False

        self.assertTrue(self.wait(tick))
        self.assertGreaterEqual(len(ticks), 4)

    def test_the_pause_is_still_set_before_the_wait_begins(self):
        """Unchanged, and the thing the whole quiesce rests on: a send started
        during the wait is one the reload lands in the middle of."""
        config.rar_inprogress = True
        seen = []

        def tick(_seconds):
            seen.append(dcc.transfers_are_paused())
            config.rar_inprogress = False

        self.wait(tick)

        self.assertEqual(seen, [True])


class _APackWithRarStoodIn(security.PathSecurityBase):
    """The real dispatcher and packer, with a stand-in for the rar binary
    that writes the archive and lets slots fill while it runs."""

    def setUp(self):
        super().setUp()
        config.channel_users = {"#dccore-test": {"dave"}}
        self.set_config(MAX_DCC_SLOTS=2, rar_inprogress=False, transfers_paused=False,
                        bot_joined_channel=True)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        patch = mock.patch.object(dcc.time, "sleep", lambda *_a: None)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(platform_compat, "rar_command",
                                  lambda configured=None: os.path.join(self.tree.root, "rar"))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(setattr, runtime, "pack_owner", None)
        self.addCleanup(setattr, runtime, "pack_job", None)
        album = os.path.join(self.tree.music, "Some Artist", "Some Album")
        os.makedirs(album, exist_ok=True)
        with io.open(os.path.join(album, "01.flac"), "wb") as handle:
            handle.write(b"\x00" * 1024)
        self.row = {"file": "Some_Album.rar", "path": album, "channel": "#dccore-test",
                    "user_raw": "dave", "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        self.slots_taken_while_rar_runs = 0
        self.rar_runs = 0
        self.while_rar_runs = lambda: None

        class Packed:
            returncode = 0
            stdout = stderr = ""

        def rar_that_writes(cmd, *a, **kw):
            self.rar_runs += 1
            with io.open([c for c in cmd if c.endswith(".rar")][0], "wb") as handle:
                handle.write(b"Rar!")
            self.while_rar_runs()
            for n in range(self.slots_taken_while_rar_runs):
                config.active_transfers.append({"user": "other%d" % n, "file": "X.flac", "bytes_sent": 0})
            return Packed()

        support.fake_rar_runs(self, dcc.subprocess, rar_that_writes)

    def dispatch(self):
        config.dcc_queue["dave"] = [self.row]
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

    def sends(self):
        return [args[1] for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]


class ThePackRechecksItsSlotWhenRarIsDone(_APackWithRarStoodIn):
    """The capacity test between the pack finishing and the slot being
    taken, evaluated, with every slot filling while rar runs. (Was a search
    of the source for the test's text, #1460.)"""

    def test_slots_filled_while_rar_ran_are_not_overshot(self):
        self.slots_taken_while_rar_runs = 2

        self.dispatch()

        self.assertEqual(len(config.active_transfers), 2, "the pack took a slot past MAX_DCC_SLOTS")
        self.assertEqual(self.sends(), [])
        self.assertEqual([r["file"] for r in config.dcc_queue["dave"]], ["Some_Album.rar"],
                         "the archive no longer waits in the queue")
        self.assertFalse(self.row["is_unpacked_rar_folder"], "the row does not name the archive")
        self.assertFalse(config.rar_inprogress, "the interlock was not released")

    def test_control_with_a_slot_left_the_archive_is_sent(self):
        self.slots_taken_while_rar_runs = 1

        self.dispatch()

        self.assertEqual(self.sends(), ["dave"])
        self.assertEqual(len(config.active_transfers), 2)


class _HoldsCounted:
    """dcc.queue_lock, still the real lock, numbering each hold of it, so a
    test can tell whether two things happened inside one hold."""

    def __init__(self, real):
        self.real = real
        self.holds = 0
        self.owner = None

    def acquire(self, *args, **kwargs):
        got = self.real.acquire(*args, **kwargs)
        if got:
            self.holds += 1
            self.owner = threading.get_ident()
        return got

    def release(self):
        self.owner = None
        self.real.release()

    def locked(self):
        return self.real.locked()

    __enter__ = acquire

    def __exit__(self, *_exc):
        self.release()

    def hold_of_this_thread(self):
        return self.holds if self.owner == threading.get_ident() else None


class _CountedTransfers(list):
    """active_transfers that notes the hold of queue_lock each count and
    each append happened in, once rar has run."""

    def __init__(self, lock):
        super().__init__()
        self.lock = lock
        self.watching = False
        self.events = []

    def __len__(self):
        if self.watching:
            self.events.append(("count", None, self.lock.hold_of_this_thread()))
        return super().__len__()

    def append(self, row):
        if self.watching:
            self.events.append(("append", row.get("file"), self.lock.hold_of_this_thread()))
        super().append(row)


class ThePackRechecksItsSlotBeforeTakingOne(_APackWithRarStoodIn):
    """The rest of the pack's slot handling, driven through the dispatcher
    and the packer rather than read out of dcc.py (#1469): the count and the
    append are one hold of queue_lock, a refused pack lets go of the
    interlocks and wakes the next pack, and the checks before a dispatch
    still refuse a full house. Whether the interlocks are released ONCE is
    tests/test_the_pack_interlocks_are_released_once.py."""

    def test_it_is_taken_under_the_queue_lock(self):
        """Two dispatch threads reaching this together would otherwise both
        see room for the last slot: the count that finds room and the append
        that takes it must be one hold of queue_lock, with nothing between."""
        lock = _HoldsCounted(dcc.queue_lock)
        patch = mock.patch.object(dcc, "queue_lock", lock)
        patch.start()
        self.addCleanup(patch.stop)
        transfers = _CountedTransfers(lock)
        self.set_config(active_transfers=transfers)

        def from_now_on():
            transfers.watching = True
        self.while_rar_runs = from_now_on
        self.slots_taken_while_rar_runs = 1

        self.dispatch()

        self.assertEqual(self.sends(), ["dave"], "the control: the archive was not sent")
        appends = [i for i, event in enumerate(transfers.events)
                   if event[:2] == ("append", "Some_Album.rar")]
        self.assertEqual(len(appends), 1, transfers.events)
        _kind, _file, hold = transfers.events[appends[0]]
        self.assertIsNotNone(hold, "the slot was taken outside queue_lock")
        counts = [event for event in transfers.events[:appends[0]] if event[0] == "count"]
        self.assertTrue(counts, "the slot was taken without counting the slots")
        self.assertEqual(counts[-1][2], hold,
                         "queue_lock was let go between the count that found room and "
                         "the append that took it")

    def test_a_refused_pack_releases_the_interlocks_it_holds(self):
        """Returning without clearing rar_inprogress would stop every future
        pack for every user until the daemon restarted - the exact failure
        the RAR_TIMEOUT comment warns about. Proved by the next pack going
        through once a slot is free."""
        self.slots_taken_while_rar_runs = 2

        self.dispatch()

        self.assertEqual(self.sends(), [])
        self.assertFalse(config.rar_inprogress, "rar_inprogress stayed latched")
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertIsNone(runtime.pack_owner)

        config.active_transfers.clear()
        self.slots_taken_while_rar_runs = 0
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.sends(), ["dave"], "the next trigger could not send the archive")

    def test_a_refused_pack_lets_a_waiting_one_start(self):
        """redispatch_waiting_pack() is the only thing that revisits a user
        turned away at [RAR-HOLD]; without it they wait for a trigger that
        never comes. Called once, for the nick the claim is held under now,
        which a rename while rar ran has changed (#1268)."""
        import irc

        woken = []
        real = dcc.redispatch_waiting_pack

        def watched(irc_sock, just_finished=None):
            woken.append((just_finished, config.rar_inprogress))
            return real(irc_sock, just_finished=just_finished)
        patch = mock.patch.object(dcc, "redispatch_waiting_pack", watched)
        patch.start()
        self.addCleanup(patch.stop)
        self.while_rar_runs = lambda: irc.note_nick_change("dave", "dave_away")
        self.slots_taken_while_rar_runs = 2

        self.dispatch()

        self.assertEqual(self.sends(), [])
        self.assertEqual(woken, [("dave_away", False)],
                         "the next pack was not woken once, for the renamed holder, "
                         "with the packer free")
        self.assertNotIn("dave_away", config.user_processing_lock)

    def test_a_full_house_is_refused_before_rar_starts(self):
        """The RAR branch's own check, before packing: no slot, no rar run,
        no interlock claimed, and the folder stays queued."""
        config.active_transfers.extend(
            {"user": "other%d" % n, "file": "X.flac", "bytes_sent": 0} for n in range(2))

        self.dispatch()

        self.assertEqual(self.rar_runs, 0, "rar ran with every slot taken")
        self.assertEqual(self.sends(), [])
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(config.dcc_queue["dave"], [self.row])

    def queue_a_plain_file(self):
        track = self.tree.tracks[0]
        row = support.queue_row("dave", os.path.basename(track), path=track)
        config.dcc_queue["dave"] = [row]
        return row

    def test_a_full_house_is_refused_for_a_plain_file(self):
        """The plain-file path's check: the file is not sent and keeps its place."""
        row = self.queue_a_plain_file()
        config.active_transfers.extend(
            {"user": "other%d" % n, "file": "X.flac", "bytes_sent": 0} for n in range(2))

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.sends(), [])
        self.assertEqual(len(config.active_transfers), 2)
        self.assertEqual(config.dcc_queue["dave"], [row])

    def test_control_a_plain_file_with_a_slot_left_is_sent(self):
        self.queue_a_plain_file()
        config.active_transfers.append({"user": "other0", "file": "X.flac", "bytes_sent": 0})

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.sends(), ["dave"])

    def test_a_full_house_is_refused_by_the_sweep(self):
        """Section B's check, claim_next_in_line(): somebody waits, nobody
        is claimed."""
        self.queue_a_plain_file()
        config.active_transfers.extend(
            {"user": "other%d" % n, "file": "X.flac", "bytes_sent": 0} for n in range(2))

        with security.quiet():
            promoted, promoted_key, _wake, _absent = dcc.claim_next_in_line()

        self.assertEqual((promoted, promoted_key), (None, None))
        self.assertEqual(len(config.active_transfers), 2)
        self.assertNotIn("dave", config.user_processing_lock)

    def test_control_the_sweep_claims_with_a_slot_left(self):
        self.queue_a_plain_file()
        config.active_transfers.append({"user": "other0", "file": "X.flac", "bytes_sent": 0})

        with security.quiet():
            _promoted, promoted_key, _wake, _absent = dcc.claim_next_in_line()

        self.assertEqual(promoted_key, "dave")


if __name__ == "__main__":
    unittest.main()
