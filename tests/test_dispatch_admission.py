"""Regression tests for dcc.check_queue_and_send - admission control.

check_queue_and_send has two dispatch paths and both of them have lost files in
production:

  Section A  the "next file for the user who just finished" path. It reads the
             queue head under queue_lock, then does the channel-membership check
             and the dispatch OUTSIDE that lock, so two overlapping triggers (the
             3s delayed fallback after every send, a JOIN/353 thaw, !rehash) could
             both dispatch the same head to the same user - issue #27.

  Section B  the global promotion path for the next waiting user. It was dead code
             (issue #4: it tested isinstance(user_files, dict) on the LIST of a
             user's files instead of on user_files[0], so every waiting user was
             skipped). Once woken up it turned out to have no admission control at
             all: two triggers promoted the same queue head, the file went out
             twice, and both finally blocks popped position 0 - the second one
             removing the NEXT, never-sent file.

Everything here drives the real dcc.check_queue_and_send; only the leaf effects
(the socket-level start_dcc_send, disk writes, debug output) are stubbed.
"""

import io
import sys
import threading
import time
import unittest

from tests.support import (DCCoreTestCase, silence_debug, no_disk_writes,
                           queue_row, RecordingSocket)

import announce
import defaults as config
import db
import dcc


class DispatchAdmissionTests(DCCoreTestCase):
    """Base wiring shared by every case below.

    NOTE on the harness: CapturedDispatch patches dcc.threading.Thread, but
    check_queue_and_send re-imports threading as a FUNCTION-LOCAL name, so the
    module attribute is never consulted there and CapturedDispatch sees nothing
    from this function. We therefore intercept at two places we can actually
    reach:

      * dcc.start_dcc_send - resolved from dcc's globals when the thread is built,
        so replacing it neutralises the real socket work; it is recorded from
        inside the spawned thread, hence not synchronous.
      * announce.send_dcc_sending_notice - called synchronously, exactly once per
        dispatch, on both the section A and section B paths, immediately before
        the thread is started. This is the deterministic seam every assertion
        below uses; self.dispatches is the corroborating record.
    """

    def setUp(self):
        super().setUp()
        self.sock = RecordingSocket()
        self.debug = silence_debug(announce)
        no_disk_writes(db)

        # Deterministic, synchronous record of "a dispatch happened".
        self.notices = []
        self._record_lock = threading.Lock()

        def fake_notice(user, file_name):
            with self._record_lock:
                self.notices.append((user, file_name))

        self.addCleanup(setattr, announce, "send_dcc_sending_notice",
                        announce.send_dcc_sending_notice)
        announce.send_dcc_sending_notice = fake_notice

        # Neutralise the actual transfer thread.
        self.dispatches = []

        def fake_start_dcc_send(irc_sock, user, file_path, file_name, channel, next_file):
            with self._record_lock:
                self.dispatches.append({"user": user, "path": file_path,
                                        "file": file_name, "entry": next_file})

        fake_start_dcc_send.__name__ = "start_dcc_send"
        self.addCleanup(setattr, dcc, "start_dcc_send", dcc.start_dcc_send)
        dcc.start_dcc_send = fake_start_dcc_send

        # MAX_DCC_SLOTS is a static config constant; pin it and put it back.
        self.addCleanup(setattr, config, "MAX_DCC_SLOTS", config.MAX_DCC_SLOTS)
        config.MAX_DCC_SLOTS = 3

        # The daemon narrates itself on stdout; keep the test output readable.
        self.addCleanup(setattr, sys, "stdout", sys.stdout)
        sys.stdout = io.StringIO()

    # -- helpers ------------------------------------------------------------

    def in_channel(self, *users, **kwargs):
        """Put users in the bot's live channel list (the 353/JOIN mirror)."""
        channel = kwargs.pop("channel", "#dccore-test")
        config.channel_users[channel] = set(users)

    def busy_slot(self, user, file_name="Busy.flac"):
        """An in-flight transfer, shaped as check_queue_and_send records it."""
        return {"user": user, "file": file_name, "bytes_sent": 0,
                "next_file_obj": file_name}

    def settle(self, seconds=0.15):
        """Let any real dispatch threads finish before asserting 'exactly once'."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(0.01)

    def run_concurrently(self, targets):
        """Run check_queue_and_send for each completed_user, started together."""
        barrier = threading.Barrier(len(targets))
        errors = []

        def trigger(completed_user):
            try:
                barrier.wait(timeout=5)
                dcc.check_queue_and_send(self.sock, completed_user)
            except Exception as exc:  # noqa: BLE001 - surfaced as a failure below
                errors.append(exc)

        threads = [threading.Thread(target=trigger, args=(name,), daemon=True)
                   for name in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "trigger thread hung")
        self.assertEqual(errors, [], "check_queue_and_send raised: %r" % (errors,))
        self.settle()

    # -- SECTION B: global promotion ----------------------------------------

    def test_concurrent_promotion_for_different_users_promotes_once(self):
        """Section B promoted the same queue head twice when two triggers overlapped.

        Guards section B's admission control (the user_processing_lock claim plus
        the already_sending scan of active_transfers). Historically neither
        existed: two calls for two DIFFERENT completed users both promoted waiting
        user 'dave', he was offered the same file twice, two slots were burned,
        and the second transfer's finally popped position 0 - deleting the NEXT
        file, which had never been sent.
        """
        self.in_channel("dave")
        first = queue_row(user="dave", filename="01_First.flac")
        second = queue_row(user="dave", filename="02_Second.flac")
        config.dcc_queue["dave"] = [first, second]

        # Neither trigger user has a queue of their own, so both calls fall
        # straight through section A into the global promotion in section B.
        self.run_concurrently(["carl", "erin"])

        self.assertEqual(len(self.notices), 1,
                         "dave was promoted %d times, expected exactly once: %r"
                         % (len(self.notices), self.notices))
        self.assertEqual(self.notices[0][1], "01_First.flac")
        self.assertEqual(len(self.dispatches), 1,
                         "start_dcc_send was spawned %d times: %r"
                         % (len(self.dispatches), self.dispatches))
        # Exactly one slot consumed, and the second file is still queued, unsent.
        self.assertEqual(len(config.active_transfers), 1)
        self.assertEqual(config.dcc_queue["dave"], [first, second])

    def test_promotion_claims_the_user_in_user_processing_lock(self):
        """Section B never claimed the promoted user, so the next trigger re-promoted him.

        The claim must be taken with the dcc_queue KEY (lowercased), not with the
        display name from user_raw - the old code tested the guards with one key
        and claimed with another, so guard and claim could disagree.
        """
        self.in_channel("Dave")
        config.dcc_queue["dave"] = [queue_row(user="Dave", filename="Claimed.flac")]

        dcc.check_queue_and_send(self.sock, "carl")
        self.settle()

        self.assertEqual(len(self.notices), 1)
        self.assertIn("dave", config.user_processing_lock,
                      "promotion did not claim the user in config.user_processing_lock")
        self.assertEqual(len(config.active_transfers), 1)
        self.assertEqual(config.active_transfers[0]["user"], "Dave",
                         "active_transfers should carry the display name from user_raw")

        # And the claim is what stops the very next trigger from doubling up.
        dcc.check_queue_and_send(self.sock, "erin")
        self.settle()
        self.assertEqual(len(self.notices), 1,
                         "a claimed user was promoted a second time: %r" % (self.notices,))

    def test_user_already_in_active_transfers_is_not_promoted_again(self):
        """A user with a live transfer must not be promoted for a second file.

        The already_sending scan of config.active_transfers is the second half of
        section B's admission control; it catches the case where the transfer was
        started by the other dispatch path and the processing-lock entry has
        already been discarded.
        """
        self.in_channel("dave")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Second.flac")]
        # Busy, but nowhere near MAX_DCC_SLOTS - only the per-user check can stop this.
        config.active_transfers.append(self.busy_slot("Dave", "First.flac"))

        dcc.check_queue_and_send(self.sock, "carl")
        self.settle()

        self.assertEqual(self.notices, [],
                         "user with a transfer in flight was promoted again")
        self.assertEqual(self.dispatches, [])
        self.assertEqual(len(config.active_transfers), 1)

    def test_max_dcc_slots_is_respected(self):
        """Promotion must stop dead once MAX_DCC_SLOTS transfers are in flight.

        The slot count is re-checked INSIDE queue_lock as well, because the outer
        test is already stale by the time the lock is acquired and two concurrent
        callers could otherwise both pass it and overshoot the limit.
        """
        self.in_channel("dave", "amy", "bob", "cid")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Waiting.flac")]
        for name in ("amy", "bob", "cid"):
            config.active_transfers.append(self.busy_slot(name))
        self.assertEqual(len(config.active_transfers), config.MAX_DCC_SLOTS)

        dcc.check_queue_and_send(self.sock, "carl")
        self.settle()

        self.assertEqual(self.notices, [], "promoted a user with every slot busy")
        self.assertEqual(len(config.active_transfers), config.MAX_DCC_SLOTS,
                         "active_transfers overshot MAX_DCC_SLOTS")
        self.assertNotIn("dave", config.user_processing_lock)

    def test_pending_rar_folder_does_not_starve_the_users_behind_it(self):
        """A queued !rar folder used to break the scan and starve everyone behind it.

        One user waiting on a folder pack blocked every other waiting user for as
        long as the pack took. That row must be skipped with continue so the scan
        reaches the next waiting user.
        """
        self.in_channel("alice", "bob")
        # Insertion order is the scan order: alice's folder pack comes first.
        config.dcc_queue["alice"] = [queue_row(user="alice", filename="Album.rar",
                                               is_unpacked_rar_folder=True,
                                               is_temporary_zip=True)]
        config.dcc_queue["bob"] = [queue_row(user="bob", filename="Track.flac")]
        self.assertEqual(list(config.dcc_queue), ["alice", "bob"])

        dcc.check_queue_and_send(self.sock, "carl")
        self.settle()

        self.assertEqual([user for user, _ in self.notices], ["bob"],
                         "the user behind a pending folder pack was starved: %r"
                         % (self.notices,))
        self.assertEqual(self.notices[0][1], "Track.flac")
        self.assertIn("bob", config.user_processing_lock)
        self.assertNotIn("alice", config.user_processing_lock,
                         "the folder-pack row must not be claimed by the promotion scan")

    def test_legacy_string_row_is_skipped_without_raising(self):
        """Pre-dict queue rows survive in dcc_queue.txt and must not crash the scan.

        Section B reads g_next.get(...) on the row, so a legacy plain-string entry
        left over from an older dcc_queue.txt would raise AttributeError inside
        queue_lock and take the whole promotion path down with it.
        """
        self.in_channel("dave", "erin")
        config.dcc_queue["dave"] = ["/srv/library/Artist/Album/Legacy.mp3"]
        config.dcc_queue["erin"] = [queue_row(user="erin", filename="Modern.flac")]

        dcc.check_queue_and_send(self.sock, "carl")  # must not raise
        self.settle()

        self.assertEqual([user for user, _ in self.notices], ["erin"],
                         "legacy string row was not skipped cleanly: %r" % (self.notices,))
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(config.dcc_queue["dave"],
                         ["/srv/library/Artist/Album/Legacy.mp3"],
                         "the legacy row must be left untouched, not silently dropped")

    def test_waiting_user_absent_from_channel_users_is_not_promoted(self):
        """Section B must only promote users verified live in the 353/JOIN mirror.

        The queue row's own channel is the one checked, lowercased, against
        config.channel_users.
        """
        self.in_channel("eve")  # dave is queued but no longer in the channel
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Ghost.flac")]

        dcc.check_queue_and_send(self.sock, "carl")
        self.settle()

        self.assertEqual(self.notices, [], "promoted a user who is not in any channel")
        self.assertEqual(config.active_transfers, [])
        self.assertNotIn("dave", config.user_processing_lock)

    # -- SECTION A: next file for the user who just finished ----------------

    def test_concurrent_calls_for_the_same_user_dispatch_once(self):
        """Issue #27: section A's plain-file branch had no admission control.

        The queue head is read under queue_lock, but the membership check and the
        dispatch that follow are not, so two overlapping triggers for the SAME
        user - the 3s post-transfer fallback, a JOIN/353 thaw, !rehash - both read
        the same head and both started an independent start_dcc_send for the
        identical file: one DCC handshake completed, the other arrived as 0 bytes
        on the leech side.
        """
        self.in_channel("dave")
        head = queue_row(user="dave", filename="Head.flac")
        tail = queue_row(user="dave", filename="Tail.flac")
        config.dcc_queue["dave"] = [head, tail]

        self.run_concurrently(["dave", "dave"])

        self.assertEqual(len(self.notices), 1,
                         "the same file was dispatched %d times to dave: %r"
                         % (len(self.notices), self.notices))
        self.assertEqual(self.notices[0], ("dave", "Head.flac"))
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(len(config.active_transfers), 1,
                         "a duplicate dispatch burned a second DCC slot")
        self.assertIn("dave", config.user_processing_lock,
                      "section A must claim the user before releasing queue_lock")
        self.assertEqual(config.dcc_queue["dave"], [head, tail])

    def test_section_a_claims_the_user_and_blocks_the_next_trigger(self):
        """Same defect as above (issue #27), exercised serially.

        With the file still at position 0 - start_dcc_send settles the row only
        when the transfer ends - nothing but the user_processing_lock claim stops
        a second trigger from re-sending it.
        """
        self.in_channel("dave")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Once.flac")]

        dcc.check_queue_and_send(self.sock, "dave")
        self.settle()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("dave", config.user_processing_lock)

        dcc.check_queue_and_send(self.sock, "dave")
        self.settle()
        self.assertEqual(len(self.notices), 1,
                         "a second trigger re-sent a file already in flight: %r"
                         % (self.notices,))
        self.assertEqual(len(config.active_transfers), 1)

    def test_section_a_blocked_by_an_existing_active_transfer(self):
        """Issue #27, other half: the claim can already be gone while the transfer lives.

        start_dcc_send discards the user_processing_lock entry on every exit path,
        so active_transfers is the authority that must also be consulted before
        section A dispatches the next file.
        """
        self.in_channel("dave")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Next.flac")]
        config.active_transfers.append(self.busy_slot("Dave", "InFlight.flac"))
        config.user_processing_lock.clear()

        dcc.check_queue_and_send(self.sock, "dave")
        self.settle()

        self.assertEqual(self.notices, [],
                         "dispatched a second file to a user already transferring")
        self.assertEqual(len(config.active_transfers), 1)

    def test_no_dispatch_and_no_freeze_while_the_bot_is_not_channel_synced(self):
        """Never dispatch to - or freeze the queue of - a user we cannot see yet.

        During a reconnect/netsplit config.channel_users is empty or half-synced.
        The old code read that as "the user has left", froze the queue and started
        the 5-minute erase timer against users who had never gone anywhere.
        """
        config.channel_users = {}  # nothing synced yet
        row = queue_row(user="dave", filename="Unseen.flac")
        config.dcc_queue["dave"] = [row]

        dcc.check_queue_and_send(self.sock, "dave")
        self.settle()

        self.assertEqual(self.notices, [], "dispatched to an unverified user")
        self.assertEqual(config.frozen_queues, {},
                         "froze a queue while the bot was not channel-synced")
        self.assertEqual(config.dcc_queue["dave"], [row], "queue must be left untouched")
        self.assertNotIn("dave", config.user_processing_lock)

    def test_a_folder_pack_also_respects_max_dcc_slots(self):
        """#162 finding #23: the folder-pack (is_unpacked_rar_folder) half of
        section A had NO capacity check at all, unlike its sibling plain-file
        branch a few lines below, which already re-checks capacity inside
        the same lock. rar_inprogress bounds concurrent PACKS to one, but a
        pack's own send afterwards is an ordinary DCC slot like any other -
        with every slot already busy, starting one more pack (which will
        itself need a slot once packing finishes) must still be refused,
        exactly like test_max_dcc_slots_is_respected already proves for the
        plain-file case.
        """
        self.in_channel("dave", "amy", "bob", "cid")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Album.rar",
                                              is_unpacked_rar_folder=True,
                                              is_temporary_zip=True)]
        for name in ("amy", "bob", "cid"):
            config.active_transfers.append(self.busy_slot(name))
        self.assertEqual(len(config.active_transfers), config.MAX_DCC_SLOTS)

        dcc.check_queue_and_send(self.sock, "dave")
        self.settle()

        self.assertEqual(self.notices, [],
                         "started packing a folder with every slot already busy")
        self.assertFalse(getattr(config, "rar_inprogress", False),
                         "must never claim the pack interlock if it is about "
                         "to be refused for capacity anyway")
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertEqual(len(config.active_transfers), config.MAX_DCC_SLOTS,
                         "active_transfers overshot MAX_DCC_SLOTS")

    def test_a_folder_pack_still_starts_with_a_free_slot(self):
        """Control for the test above: the new capacity check must not
        refuse a folder pack that genuinely has room.

        The packer thread itself is prevented from actually running: its
        real target would fail against queue_row()'s fake
        "/srv/library/..." path (not really inside FILE_DIRECTORY) and
        release the interlocks again moments later, in a background
        thread racing this assertion - not a hypothetical, CI itself hit
        this the first time this test was written, on whichever run
        happened to schedule that thread early. What is under test here
        is only the synchronous capacity-check-and-claim inside
        queue_lock, before any thread is even started - so threading.Thread
        itself is patched to record the target without running it,
        exactly as this file's own CapturedDispatch/fake_start_dcc_send
        pattern already does for the send side.
        """
        import threading as real_threading
        real_thread_cls = real_threading.Thread
        scheduled = []

        class NoOpThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                scheduled.append(target)

            def start(self):
                pass

        real_threading.Thread = NoOpThread
        self.addCleanup(setattr, real_threading, "Thread", real_thread_cls)

        self.in_channel("dave")
        config.dcc_queue["dave"] = [queue_row(user="dave", filename="Album.rar",
                                              is_unpacked_rar_folder=True,
                                              is_temporary_zip=True)]

        dcc.check_queue_and_send(self.sock, "dave")

        self.assertTrue(getattr(config, "rar_inprogress", False),
                        "a folder pack with a free slot must still be able to start")
        self.assertIn("dave", config.user_processing_lock)
        self.assertEqual(len(scheduled), 1,
                         "the packer thread should have been scheduled exactly once")


if __name__ == "__main__":
    unittest.main()
