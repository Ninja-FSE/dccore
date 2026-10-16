"""#1296: the search index's rebuild needed the index's size again in free
disk, and kept the bot off IRC while it re-indexed every held list.

Debian and Ubuntu build SQLite with secure delete on, so a schema rebuild's
DROP zero-filled every freed page through the WAL: an 8.8 GB WAL for an
8.8 GB index, and "database or disk is full" on a host with GB to spare.
The index is a cache of lists on disk, so its connections turn that off.
And the startup backfill, which re-indexes every held list after such a
rebuild, ran before the IRC connection - about ten minutes off IRC. It runs
on a thread of its own now, and the dashboard's filter waits for it.

Every bot and file name here is invented.
"""

import contextlib
import io
import os
import sqlite3
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list_index  # noqa: E402
import runtime  # noqa: E402

from tests.test_crosslist_search import IndexCase  # noqa: E402


class NothingIsScrubbed(IndexCase):
    """The pragma is per connection, so both of the index's are asked.

    Every connection here starts with secure delete ON, the way a Debian or
    Ubuntu build of SQLite opens one. Most Windows and macOS builds open it
    off, and there the fix would be invisible."""

    def setUp(self):
        super().setUp()
        debian = types.ModuleType("sqlite3")
        debian.__dict__.update(sqlite3.__dict__)

        def connect(*args, **kwargs):
            conn = sqlite3.connect(*args, **kwargs)
            conn.execute("PRAGMA secure_delete = ON")
            return conn
        debian.connect = connect
        real = list_index.sqlite3
        list_index.sqlite3 = debian
        self.addCleanup(setattr, list_index, "sqlite3", real)

    def test_the_writer_does_not_secure_delete(self):
        with runtime.list_index_lock:
            conn = list_index._connect()
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 0)

    def test_the_reader_does_not_either(self):
        self.assertTrue(list_index._open_reader())
        with runtime.list_index_read_lock:
            conn = list_index._reader()
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 0)

    def test_the_rebuild_drop_writes_almost_nothing(self):
        """The property itself: an index from before schema 3 is rebuilt
        on open, and its drop no longer copies the old rows into the WAL."""
        with runtime.list_index_lock:
            conn = list_index._connect()
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.executemany("INSERT INTO entries (bot, filename) VALUES (?, ?)",
                             (("somebot", "track_%06d_of_a_long_enough_name.flac" % i) for i in range(40_000)))
            conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        list_index.close()
        path = list_index._index_path()
        before = os.path.getsize(path)

        with runtime.list_index_lock, contextlib.redirect_stdout(io.StringIO()):
            conn = list_index._connect()
            wal = os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0
            self.assertEqual(conn.execute("SELECT count(*) FROM entries").fetchone()[0], 0,
                             "the schema-2 rows were rebuilt away")

        self.assertLess(wal, before / 10,
                        f"the drop wrote a {wal}-byte WAL for a {before}-byte index")


class AFullDisk(unittest.TestCase):

    def test_it_says_how_much_room_there_is(self):
        note = list_index._disk_note(os.path.abspath(__file__), Exception("database or disk is full"))
        self.assertIn("GB free", note)

    def test_any_other_failure_adds_nothing(self):
        self.assertEqual(list_index._disk_note(os.path.abspath(__file__), Exception("unable to open")), "")


class TheStartupBackfill(IndexCase):

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.seen = []
        real = list_index.backfill_missing

        def held_back(held, log=print):
            self.seen.append(held)
            self.release.wait(10)
        list_index.backfill_missing = held_back
        self.addCleanup(setattr, list_index, "backfill_missing", real)
        self.addCleanup(runtime.list_index_backfill_done.set)
        self.addCleanup(self.release.set)

    def test_it_runs_on_a_thread_of_its_own(self):
        held = {"somebot": {"bot": "somebot"}}
        thread = list_index.backfill_in_background(held, log=lambda _m: None)

        self.assertIsNot(thread, threading.current_thread())
        self.assertFalse(runtime.list_index_backfill_done.is_set(), "the caller is not kept waiting")
        self.release.set()
        thread.join(10)
        self.assertTrue(runtime.list_index_backfill_done.is_set())

    def test_it_reads_a_copy_of_the_held_lists(self):
        held = {"somebot": {"bot": "somebot"}}
        thread = list_index.backfill_in_background(held, log=lambda _m: None)
        held["anotherbot"] = {"bot": "anotherbot"}
        self.release.set()
        thread.join(10)

        self.assertEqual(list(self.seen[0]), ["somebot"])

    def test_the_filter_waits_for_it(self):
        thread = list_index.backfill_in_background({}, log=lambda _m: None)
        answered = threading.Event()
        reader = threading.Thread(target=lambda: (list_index._prepare_to_read(), answered.set()), daemon=True)
        reader.start()

        self.assertFalse(answered.wait(0.3), "the filter answered from a half-filled index")
        self.release.set()
        thread.join(10)
        self.assertTrue(answered.wait(10), "the filter never answered once the backfill was done")

    def test_a_failing_backfill_still_lets_the_filter_answer(self):
        said = []

        def broken(held, log=print):
            raise RuntimeError("no index here")
        list_index.backfill_missing = broken

        thread = list_index.backfill_in_background({}, log=said.append)
        thread.join(10)

        self.assertTrue(runtime.list_index_backfill_done.is_set())
        self.assertIn("no index here", said[0])


if __name__ == "__main__":
    unittest.main()
