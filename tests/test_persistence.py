"""Regression tests for db.py - the atomic, crash-safe on-disk persistence layer.

Every file db.py owns is small and rewritten in full. The historical bugs all came
from the same shape: truncate the live file, then serialise into the open handle.
A crash, a full disk or a second writer between those two steps left a short or
empty file behind - and both loaders treat an unparseable file as "start empty",
so the queue or the statistics vanished silently on the next boot.

These tests pin down the fixed behaviour: write-to-temp-then-os.replace, a failed
write that leaves the previous file untouched, a damaged file preserved as
<name>.corrupt, and - the one that is not about disk at all - save_dcc_queue()
never touching dcc.queue_lock, because dcc.py calls it while already holding it.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from tests.support import DCCoreTestCase, queue_row

import defaults as config
import db
import platform_compat
import dcc


def _tmp_residue(directory):
    """Names of the ".tmp_*" scratch files _atomic_write leaves behind, if any."""
    return [name for name in os.listdir(directory) if name.startswith(".tmp_")]


class PersistenceTestCase(DCCoreTestCase):
    """Base case: db.py pointed at a throwaway directory, and kept quiet.

    db.py logs with bare print(); the module-global shadow below silences it for
    the duration of a test without touching the daemon source.
    """

    def setUp(self):
        super().setUp()
        self._dir = tempfile.mkdtemp(prefix="dccore-db-")
        self._saved = {
            "queue": db.DCC_QUEUE_FILE,
            "speed": db.SPEED_RECORD_FILE,
            "bans": getattr(config, "BANS_FILE", None),
            "stats": getattr(config, "STATS_FILE", None),
            "atomic": db._atomic_write,
        }
        self.queue_file = os.path.join(self._dir, "dcc_queue.txt")
        self.bans_file = os.path.join(self._dir, "bans.txt")
        self.stats_file = os.path.join(self._dir, "stats.txt")
        db.DCC_QUEUE_FILE = self.queue_file
        db.SPEED_RECORD_FILE = os.path.join(self._dir, "speed_record.txt")
        config.BANS_FILE = self.bans_file
        config.STATS_FILE = self.stats_file
        # Shadow the builtin inside db's namespace so its logging stays out of the
        # test output. Removed again in tearDown.
        db.print = lambda *a, **k: None

    def tearDown(self):
        db.DCC_QUEUE_FILE = self._saved["queue"]
        db.SPEED_RECORD_FILE = self._saved["speed"]
        db._atomic_write = self._saved["atomic"]
        if self._saved["bans"] is not None:
            config.BANS_FILE = self._saved["bans"]
        if self._saved["stats"] is not None:
            config.STATS_FILE = self._saved["stats"]
        db.__dict__.pop("print", None)
        shutil.rmtree(self._dir, ignore_errors=True)
        super().tearDown()

    # -- helpers ---------------------------------------------------------------

    def sample_queue(self):
        return {
            "dave": [queue_row(user="dave", filename="01 - Enter Sandman.flac"),
                     queue_row(user="dave", filename="02 - Sad But True.flac")],
            "erik": [queue_row(user="erik", filename="Album.zip", is_temporary_zip=True)],
        }

    def read_queue_file(self):
        with open(self.queue_file, "r", encoding="utf-8") as handle:
            return handle.read()


class TestQueueRoundTrip(PersistenceTestCase):

    def test_save_then_load_returns_the_same_queue(self):
        """Defect: save and load used two different path literals ("data/..." vs
        "./data/..."), so what was saved was not necessarily what was read back. The
        queue must survive a full round-trip through disk unchanged."""
        config.dcc_queue = self.sample_queue()
        expected = json.loads(json.dumps(config.dcc_queue))

        db.save_dcc_queue()
        config.dcc_queue = {"stale": [queue_row(user="stale")]}
        db.load_dcc_queue()

        self.assertEqual(config.dcc_queue, expected)
        self.assertEqual(sorted(config.dcc_queue), ["dave", "erik"])
        self.assertEqual(len(config.dcc_queue["dave"]), 2)
        self.assertEqual(config.dcc_queue["dave"][0]["file"], "01 - Enter Sandman.flac")
        self.assertIs(config.dcc_queue["erik"][0]["is_temporary_zip"], True)

    def test_save_drops_users_whose_list_is_empty(self):
        """Defect: users with an emptied list stayed in dcc_queue.txt and were reloaded at
        boot as phantom queue holders. save_dcc_queue() sanitises them away both in
        memory and on disk."""
        config.dcc_queue = self.sample_queue()
        config.dcc_queue["ghost"] = []

        db.save_dcc_queue()

        self.assertNotIn("ghost", config.dcc_queue)
        self.assertNotIn("ghost", json.loads(self.read_queue_file()))

    def test_save_leaves_no_tmp_residue(self):
        """The atomic write must clean up after itself: no ".tmp_*.swap" scratch file may
        be left in the data directory once the save has returned."""
        config.dcc_queue = self.sample_queue()

        db.save_dcc_queue()
        db.save_dcc_queue()

        self.assertEqual(_tmp_residue(self._dir), [])
        self.assertTrue(os.path.exists(self.queue_file))

    def test_load_of_missing_file_starts_empty(self):
        """A first boot with no dcc_queue.txt must simply start with an empty queue."""
        config.dcc_queue = {"leftover": [queue_row(user="leftover")]}
        self.assertFalse(os.path.exists(self.queue_file))

        db.load_dcc_queue()

        self.assertEqual(config.dcc_queue, {})


class TestFailedWriteKeepsPreviousFile(PersistenceTestCase):

    def test_failed_queue_save_leaves_previous_file_intact(self):
        """Defect: save_dcc_queue() truncated dcc_queue.txt and then serialised into the
        open handle, so a crash between the two steps lost the entire queue. With the
        atomic write a failing save must leave the previous file byte-for-byte intact
        and still valid JSON."""
        config.dcc_queue = self.sample_queue()
        db.save_dcc_queue()
        before = self.read_queue_file()
        self.assertEqual(json.loads(before), config.dcc_queue)

        def exploding_write(path, text):
            raise OSError(28, "No space left on device")

        db._atomic_write = exploding_write
        config.dcc_queue = {"nobody": [queue_row(user="nobody", filename="Lost.flac")]}
        db.save_dcc_queue()  # swallows the error, as the daemon must keep running

        after = self.read_queue_file()
        self.assertEqual(after, before)
        recovered = json.loads(after)  # still valid JSON, not truncated
        self.assertEqual(sorted(recovered), ["dave", "erik"])

        # And the old contents really come back on the next boot.
        db._atomic_write = self._saved["atomic"]
        db.load_dcc_queue()
        self.assertEqual(sorted(config.dcc_queue), ["dave", "erik"])
        self.assertFalse(os.path.exists(self.queue_file + ".corrupt"))

    def test_failed_stats_save_leaves_previous_row_intact(self):
        """Defect: save_advanced_stats() truncated stats.txt before writing, so a failure
        left a short row - which load_advanced_stats() discards, resetting every counter
        to zero. A failed save must keep the previous complete row."""
        db.save_advanced_stats([12, 3456, 2, 200, 7, 700, "2026-08-22"])
        with open(self.stats_file, "r") as handle:
            before = handle.read()

        def exploding_write(path, text):
            raise OSError("disk gone")

        db._atomic_write = exploding_write
        db.save_advanced_stats([99, 9999, 9, 999, 9, 999, "2026-08-23"])

        with open(self.stats_file, "r") as handle:
            self.assertEqual(handle.read(), before)
        db._atomic_write = self._saved["atomic"]
        self.assertEqual(db.load_advanced_stats(), [12, 3456, 2, 200, 7, 700, "2026-08-22"])

    def test_failed_bans_save_leaves_previous_file_intact(self):
        """Same shape for bans.txt: a failed rewrite must not cost the active bans."""
        config.banned_users = {"spammer!*@*": 1234.5}
        db.save_bans_to_file()
        with open(self.bans_file, "r") as handle:
            before = handle.read()

        def exploding_write(path, text):
            raise OSError("disk gone")

        db._atomic_write = exploding_write
        config.banned_users = {}
        db.save_bans_to_file()

        with open(self.bans_file, "r") as handle:
            self.assertEqual(handle.read(), before)
        db._atomic_write = self._saved["atomic"]
        db.load_bans_from_file()
        self.assertEqual(config.banned_users, {"spammer!*@*": 1234.5})

    def test_atomic_write_failure_removes_its_own_temp_file(self):
        """If the write itself blows up mid-way, _atomic_write must delete the scratch
        file and re-raise - leaving neither residue nor a damaged target file."""
        target = os.path.join(self._dir, "thing.txt")
        db._atomic_write(target, "original payload")

        with self.assertRaises(TypeError):
            db._atomic_write(target, object())  # f.write() rejects a non-string

        with open(target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original payload")
        self.assertEqual(_tmp_residue(self._dir), [])

    def test_atomic_write_creates_a_missing_directory(self):
        """The data directory may not exist on a fresh install; the write creates it
        instead of failing and losing the first save."""
        target = os.path.join(self._dir, "nested", "deeper", "queue.txt")

        db._atomic_write(target, "hello")

        with open(target, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")


class ReplaceWithRetryTests(unittest.TestCase):
    """platform_compat.replace_with_retry() in isolation - #162 finding #25's write-side
    half. The real failure mode (os.replace() raising PermissionError
    because another handle, e.g. security.check_user_status() reading
    hard_bans.txt, has the destination open) is Windows-only and cannot be
    triggered for real here, so os.replace() itself is stubbed to fail a
    controlled number of times before succeeding."""

    def setUp(self):
        self._real_replace = os.replace
        self._real_sleep = time.sleep
        self.sleeps = []
        time.sleep = lambda seconds: self.sleeps.append(seconds)

    def tearDown(self):
        os.replace = self._real_replace
        time.sleep = self._real_sleep

    def test_succeeds_immediately_when_the_first_attempt_works(self):
        calls = []
        os.replace = lambda src, dst: calls.append((src, dst))

        platform_compat.replace_with_retry("a", "b")

        self.assertEqual(calls, [("a", "b")])
        self.assertEqual(self.sleeps, [])

    def test_retries_after_permission_errors_and_then_succeeds(self):
        attempts = {"n": 0}

        def flaky_replace(src, dst):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError("[WinError 5] Access is denied")

        os.replace = flaky_replace

        platform_compat.replace_with_retry("a", "b")

        self.assertEqual(attempts["n"], 3)
        # Two failures before the third, successful attempt - two backoff sleeps.
        self.assertEqual(len(self.sleeps), 2)
        # Backoff, not a fixed delay.
        self.assertLess(self.sleeps[0], self.sleeps[1])

    def test_gives_up_after_the_bounded_number_of_attempts(self):
        def always_fails(src, dst):
            raise PermissionError("[WinError 5] Access is denied")

        os.replace = always_fails

        with self.assertRaises(PermissionError):
            platform_compat.replace_with_retry("a", "b", attempts=3, base_delay=0.001)

        self.assertEqual(len(self.sleeps), 2, "2 sleeps between 3 attempts")

    def test_a_non_permission_error_is_not_retried(self):
        """Only the collision this exists for (PermissionError) is retried -
        any other failure must surface immediately, exactly as a bare
        os.replace() would have."""
        calls = []

        def wrong_kind_of_failure(src, dst):
            calls.append(1)
            raise OSError("disk gone")

        os.replace = wrong_kind_of_failure

        with self.assertRaises(OSError):
            platform_compat.replace_with_retry("a", "b")

        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])


class TestCorruptQueueFile(PersistenceTestCase):

    def test_corrupt_file_is_preserved_as_corrupt_and_queue_starts_empty(self):
        """Defect: an unparseable dcc_queue.txt was left in place and silently overwritten
        by the next save, so the damaged queue vanished without trace. It must be moved
        aside to <name>.corrupt for manual rescue instead."""
        garbage = '{"dave": [{"file": "Broken.fl'  # a truncated write, the classic case
        with open(self.queue_file, "w", encoding="utf-8") as handle:
            handle.write(garbage)
        config.dcc_queue = {"stale": [queue_row(user="stale")]}

        db.load_dcc_queue()

        self.assertEqual(config.dcc_queue, {})
        backup = self.queue_file + ".corrupt"
        self.assertTrue(os.path.exists(backup), "the damaged file was not preserved")
        with open(backup, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), garbage)
        # Moved aside, not copied - the next save starts from a clean slate.
        self.assertFalse(os.path.exists(self.queue_file))

    def test_next_save_after_corruption_does_not_clobber_the_backup(self):
        """The rescued copy must survive the save that follows the failed boot."""
        with open(self.queue_file, "w", encoding="utf-8") as handle:
            handle.write("not json at all")
        db.load_dcc_queue()

        config.dcc_queue = self.sample_queue()
        db.save_dcc_queue()

        with open(self.queue_file + ".corrupt", "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not json at all")
        self.assertEqual(sorted(json.loads(self.read_queue_file())), ["dave", "erik"])

    def test_json_value_that_is_not_an_object_is_rejected(self):
        """Defect: load_dcc_queue() assigned whatever json.load() returned straight into
        config.dcc_queue. A list (or a bare string/number/null) then made every dict
        operation in dcc.py explode at runtime. Only a JSON object may be accepted."""
        for payload in ('["dave", "erik"]', '"just a string"', "42", "null"):
            with self.subTest(payload=payload):
                backup = self.queue_file + ".corrupt"
                if os.path.exists(backup):
                    os.remove(backup)
                with open(self.queue_file, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                config.dcc_queue = {"stale": [queue_row(user="stale")]}

                db.load_dcc_queue()

                self.assertIsInstance(config.dcc_queue, dict)
                self.assertEqual(config.dcc_queue, {})
                self.assertTrue(os.path.exists(backup))

    def test_valid_empty_object_is_accepted_not_treated_as_corrupt(self):
        """The opposite mistake: "{}" is a perfectly good empty queue and must not be
        quarantined as damaged."""
        with open(self.queue_file, "w", encoding="utf-8") as handle:
            handle.write("{}")

        db.load_dcc_queue()

        self.assertEqual(config.dcc_queue, {})
        self.assertFalse(os.path.exists(self.queue_file + ".corrupt"))
        self.assertTrue(os.path.exists(self.queue_file))


class TestQueueLockDeadlockGuard(PersistenceTestCase):
    """Held lock is dcc.queue_lock - dcc.py's own module-level lock, not
    config.queue_lock. These tests used to hold config.queue_lock (a
    separate object oserve.py allocated but dcc.py never actually touches -
    see dcc.py's and oserve.py's own comments on that mistake) while
    claiming to reproduce "exactly what dcc.py does", so a real reentrancy
    regression against the lock dcc.py genuinely holds would have gone
    uncaught here."""

    def test_save_dcc_queue_does_not_take_dcc_queue_lock(self):
        """Defect: save_dcc_queue() guarded itself with dcc.queue_lock, but dcc.py calls
        it from inside "with dcc.queue_lock:" and threading.Lock is not reentrant - the
        first save after a transfer deadlocked the whole daemon.

        Run in a daemon thread with a join timeout so a regression fails the test instead
        of hanging the suite forever."""
        config.dcc_queue = self.sample_queue()
        finished = threading.Event()
        errors = []

        def caller():
            try:
                with dcc.queue_lock:          # exactly what dcc.py does
                    db.save_dcc_queue()
            except Exception as err:             # pragma: no cover - defensive
                errors.append(err)
            finally:
                finished.set()

        worker = threading.Thread(target=caller, daemon=True)
        worker.start()
        completed = finished.wait(timeout=5.0)

        self.assertTrue(completed, "save_dcc_queue() deadlocked while queue_lock was held")
        self.assertEqual(errors, [])
        worker.join(timeout=5.0)
        self.assertEqual(sorted(json.loads(self.read_queue_file())), ["dave", "erik"])

    def test_bans_and_stats_saves_also_survive_a_held_queue_lock(self):
        """The same reentrancy trap applies to the other savers: the ban path and the
        transfer path can be holding queue_lock when they persist."""
        config.banned_users = {"spammer!*@*": time.time() + 600}
        finished = threading.Event()

        def caller():
            with dcc.queue_lock:
                db.save_bans_to_file()
                db.save_advanced_stats([1, 2, 3, 4, 5, 6, "2026-08-23"])
                db.save_speed_record(4242)
            finished.set()

        worker = threading.Thread(target=caller, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(timeout=5.0), "a db saver deadlocked on queue_lock")
        worker.join(timeout=5.0)
        self.assertTrue(os.path.exists(self.bans_file))
        self.assertTrue(os.path.exists(self.stats_file))
        self.assertTrue(os.path.exists(db.SPEED_RECORD_FILE))

    def test_disk_lock_is_a_separate_lock_object(self):
        """Structural guard for the same defect: db's own serialising lock must never be
        dcc.queue_lock, however the modules are imported or reloaded."""
        self.assertIsNot(db._disk_lock, dcc.queue_lock)
        self.assertIsNot(db._disk_lock, getattr(config, "debug_flood_lock", None))

    def test_save_does_not_leave_the_disk_lock_held_after_a_failure(self):
        """A save that raises must still release _disk_lock, or the very next save - and
        with it every future queue write - blocks forever."""
        def exploding_write(path, text):
            raise OSError("boom")

        db._atomic_write = exploding_write
        config.dcc_queue = self.sample_queue()
        db.save_dcc_queue()

        self.assertTrue(db._disk_lock.acquire(timeout=2.0),
                        "_disk_lock was still held after a failed save")
        db._disk_lock.release()


class TestConcurrentSaves(PersistenceTestCase):

    def test_many_threads_saving_at_once_leave_a_parseable_file(self):
        """Defect: concurrent saves interleaved inside one truncated file handle and left
        a mangled half-and-half file that the next boot threw away. Serialised atomic
        writes must always leave one complete, parseable JSON object behind."""
        workers, rounds = 6, 15
        errors = []
        barrier = threading.Barrier(workers)

        # The budget is measured, not guessed. This test performs
        # workers * rounds serialised atomic writes, and an atomic write is a
        # temp file, an fsync and an os.replace - about 3ms on a local SSD, and
        # hundreds of milliseconds on a CI runner whose virus scanner inspects
        # every file created in the temp directory. A flat 20 seconds was a bet
        # that one write would never exceed 220ms, and windows-latest/3.10
        # collected on it.
        #
        # What this test is for is in its own docstring: serialised atomic
        # writes leave one complete, parseable JSON object behind. The clock
        # was scaffolding, and scaffolding that fails on a slow machine reports
        # a defect that is not there. Same correction as #152.
        config.dcc_queue = {"probe": [queue_row(user="probe")]}
        probe_start = time.time()
        db.save_dcc_queue()
        one_write = max(time.time() - probe_start, 0.001)
        config.dcc_queue = {}

        # Four times the measured serial cost, because the writes contend, and
        # never below the old 20 seconds. Capped, so a genuine deadlock still
        # fails the run instead of hanging the suite.
        budget = min(300.0, max(20.0, one_write * workers * rounds * 4))

        def worker(index):
            try:
                user = "user%02d" % index
                barrier.wait(timeout=budget)
                for round_no in range(rounds):
                    config.dcc_queue[user] = [
                        queue_row(user=user, filename="track-%d-%d.flac" % (index, round_no))
                    ]
                    db.save_dcc_queue()
            except Exception as err:
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(workers)]
        for thread in threads:
            thread.start()
        deadline = time.time() + budget
        for thread in threads:
            # One shared deadline rather than one per thread: the threads run
            # at the same time, so six 20-second joins were never six chances
            # to be slow. They were one, and the arithmetic read otherwise.
            thread.join(timeout=max(0.0, deadline - time.time()))
            self.assertFalse(
                thread.is_alive(),
                f"a saving thread never finished within {budget:.1f}s "
                f"(one write measured {one_write * 1000:.0f}ms)")

        self.assertEqual(errors, [])
        loaded = json.loads(self.read_queue_file())
        self.assertIsInstance(loaded, dict)
        self.assertTrue(loaded, "the concurrently written file ended up empty")
        for user, rows in loaded.items():
            self.assertIsInstance(rows, list)
            self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertEqual(_tmp_residue(self._dir), [])

    def test_snapshot_survives_a_queue_mutated_during_the_save(self):
        """Defect: save_dcc_queue() serialised config.dcc_queue directly, so another thread
        adding a user mid-dump raised "dictionary changed size during iteration" and the
        save was abandoned. It must serialise from a snapshot instead."""
        config.dcc_queue = {"user%02d" % i: [queue_row(user="user%02d" % i)] for i in range(20)}
        stop = threading.Event()
        errors = []

        def churn():
            counter = 0
            while not stop.is_set():
                counter += 1
                key = "churn%d" % (counter % 40)
                config.dcc_queue[key] = [queue_row(user=key)]
                config.dcc_queue.pop("churn%d" % ((counter + 20) % 40), None)

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        try:
            for _ in range(12):
                try:
                    db.save_dcc_queue()
                except Exception as err:  # pragma: no cover - defensive
                    errors.append(err)
        finally:
            stop.set()
            churner.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertIsInstance(json.loads(self.read_queue_file()), dict)


class TestStatsAndBansRoundTrip(PersistenceTestCase):

    def test_advanced_stats_round_trip(self):
        """The seven-column stats row must come back with its integers as integers and the
        date as the trailing string - a short row is what resets every counter to zero."""
        stats = [1234, 987654321, 12, 345678, 7, 89012, "2026-08-23"]

        db.save_advanced_stats(stats)

        self.assertEqual(db.load_advanced_stats(), stats)
        with open(self.stats_file, "r") as handle:
            self.assertEqual(len(handle.read().strip().split()), 7)
        self.assertEqual(_tmp_residue(self._dir), [])

    def test_stats_load_falls_back_to_defaults_on_a_short_row(self):
        """A truncated legacy row must not raise; it degrades to the default counters."""
        with open(self.stats_file, "w") as handle:
            handle.write("1234 987")

        loaded = db.load_advanced_stats()

        self.assertEqual(loaded[:2], [1234, 987])
        self.assertEqual(loaded[2:6], [0, 0, 0, 0])
        self.assertIsInstance(loaded[6], str)

    def test_stats_load_without_a_file_returns_todays_defaults(self):
        """No stats.txt at all is a fresh install, not an error."""
        self.assertFalse(os.path.exists(self.stats_file))

        loaded = db.load_advanced_stats()

        self.assertEqual(loaded[:6], [0, 0, 0, 0, 0, 0])
        self.assertRegex(loaded[6], r"^\d{4}-\d{2}-\d{2}$")

    def test_bans_round_trip(self):
        """Bans must survive a restart: one "user expiry" line per ban, read back into
        config.banned_users with the expiry as a float."""
        expiry_a = time.time() + 3600.0
        expiry_b = time.time() + 7200.0
        config.banned_users = {"spammer!*@*": expiry_a, "leecher!*@host.example": expiry_b}

        db.save_bans_to_file()
        config.banned_users = {}
        db.load_bans_from_file()

        self.assertEqual(sorted(config.banned_users), ["leecher!*@host.example", "spammer!*@*"])
        self.assertAlmostEqual(config.banned_users["spammer!*@*"], expiry_a, places=6)
        self.assertAlmostEqual(config.banned_users["leecher!*@host.example"], expiry_b, places=6)
        self.assertEqual(_tmp_residue(self._dir), [])

    def test_bans_file_is_rewritten_in_full_not_appended(self):
        """An unban has to actually disappear from bans.txt. The file is rewritten from
        the in-memory snapshot, so a lifted ban must not come back after a restart."""
        config.banned_users = {"a!*@*": 1.0, "b!*@*": 2.0}
        db.save_bans_to_file()
        del config.banned_users["a!*@*"]

        db.save_bans_to_file()
        config.banned_users = {}
        db.load_bans_from_file()

        self.assertEqual(list(config.banned_users), ["b!*@*"])

    def test_empty_ban_list_writes_an_empty_file_not_a_stale_one(self):
        """Clearing every ban must empty the file rather than leave the old bans in place."""
        config.banned_users = {"a!*@*": 1.0}
        db.save_bans_to_file()
        config.banned_users = {}

        db.save_bans_to_file()
        db.load_bans_from_file()

        self.assertEqual(config.banned_users, {})
        with open(self.bans_file, "r") as handle:
            self.assertEqual(handle.read(), "")

    def test_a_malformed_line_only_costs_itself(self):
        """#162 finding #12: the whole per-line loop used to be inside one
        try/except, so one bad expiry timestamp aborted the load with
        earlier entries already committed - a silent partial load that the
        very next save() would then persist as the permanent truncated
        state, with no command anywhere able to restore what was skipped.
        Matches the posture _read_hard_bans_unlocked already has: skip the
        bad line, keep every other one."""
        with open(self.bans_file, "w") as handle:
            handle.write("good1!*@* 1111.0\n")
            handle.write("# a stray comment line, no space-split possible\n")
            handle.write("badexpiry!*@* not-a-float\n")
            handle.write("good2!*@* 2222.0\n")

        db.load_bans_from_file()

        self.assertEqual(sorted(config.banned_users), ["good1!*@*", "good2!*@*"])
        self.assertAlmostEqual(config.banned_users["good1!*@*"], 1111.0, places=6)
        self.assertAlmostEqual(config.banned_users["good2!*@*"], 2222.0, places=6)

    def test_a_non_ascii_nick_round_trips(self):
        """#226: load_bans_from_file() opened bans.txt with no encoding, so on
        Windows it used the locale ANSI code page while save (via
        _atomic_write) always writes utf-8. A banned nick containing a byte
        sequence invalid in that code page made the whole read raise, and the
        surrounding except left config.banned_users empty - every active
        timed ban lost at the next startup."""
        expiry = time.time() + 3600.0
        config.banned_users = {"Söker!*@värd.example": expiry}

        db.save_bans_to_file()
        config.banned_users = {}
        db.load_bans_from_file()

        self.assertIn("söker!*@värd.example", config.banned_users)
        self.assertAlmostEqual(config.banned_users["söker!*@värd.example"], expiry, places=6)

    def test_the_read_declares_utf8_explicitly(self):
        """The property #226 is actually about, checked in source rather than
        by round-trip: Python's default encoding when none is given is the
        LOCALE's, which is utf-8 on the Linux CI runners this suite runs on -
        so the test above alone would not fail even with the bug present.
        Only Windows, with a non-utf-8 ANSI code page active, was ever
        exposed."""
        import ast
        import io as _io

        with _io.open(db.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        sites = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                continue
            if not (len(node.args) >= 1 and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "BANS_FILE"):
                continue
            sites.append(node.lineno)
            has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
            self.assertTrue(has_encoding,
                            f"open(config.BANS_FILE, ...) at line {node.lineno} "
                            f"does not declare an explicit encoding")

        self.assertTrue(sites, "fixture invariant: no open(config.BANS_FILE) "
                               "call site found at all - the scan is broken, "
                               "not the code")

    def test_speed_record_round_trip(self):
        """save_speed_record() writes atomically to the shared module constant - both
        halves of the pair must agree on the path - and leaves no residue."""
        db.save_speed_record(1234567)

        with open(db.SPEED_RECORD_FILE, "r", encoding="utf-8") as handle:
            self.assertEqual(int(handle.read().strip()), 1234567)
        self.assertEqual(_tmp_residue(self._dir), [])


class TestFetchHistoryRoundTrip(DCCoreTestCase):
    """db.load_fetch_history()/save_fetch_history() - the same JSON-file
    round trip as db.load_known_bots()/db.load_fetched_bot_lists(), applied
    to a finished cross-bot fetch's own row (see dcc_fetch.py's
    _persist_fetch_history_locked() for the caller)."""

    def setUp(self):
        super().setUp()
        self._dir = tempfile.mkdtemp(prefix="dccore-fetch-history-")
        self._saved_file = db.FETCH_HISTORY_FILE
        db.FETCH_HISTORY_FILE = os.path.join(self._dir, "fetch_history.json")
        db.print = lambda *a, **k: None

    def tearDown(self):
        db.FETCH_HISTORY_FILE = self._saved_file
        db.__dict__.pop("print", None)
        shutil.rmtree(self._dir, ignore_errors=True)
        super().tearDown()

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(db.load_fetch_history(), {})

    def test_save_then_load_returns_the_same_rows(self):
        rows = {
            "r1": {"bot": "goodbot", "filename": "Song.flac", "state": "complete",
                   "stored_filename": "r1_Song.flac"},
            "r2": {"bot": "otherbot", "filename": "!rar Album", "state": "failed",
                   "reason": "no response"},
        }
        db.save_fetch_history(rows)
        self.assertEqual(db.load_fetch_history(), rows)

    def test_a_corrupt_file_loads_as_empty_rather_than_raising(self):
        with open(db.FETCH_HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertEqual(db.load_fetch_history(), {})

    def test_a_json_value_that_is_not_an_object_loads_as_empty(self):
        with open(db.FETCH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertEqual(db.load_fetch_history(), {})

    def test_save_leaves_no_tmp_residue(self):
        db.save_fetch_history({"r1": {"bot": "goodbot", "state": "complete"}})
        self.assertEqual(_tmp_residue(self._dir), [])


if __name__ == "__main__":
    unittest.main()
