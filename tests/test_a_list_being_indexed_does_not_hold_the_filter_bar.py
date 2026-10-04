"""A list being indexed no longer holds every filter-bar keystroke (#1129).

WHAT WAS WRONG

index_bot_list() holds runtime.list_index_lock for the whole delete, insert,
commit and checkpoint of a list, so that no reader could see a bot's list
emptied and not yet refilled. Every dashboard read took the same lock and the
same connection, so every keystroke in the List Browser's filter bar during a
fetch install, an auto-refetch or a backfill waited for the write to finish:
about nine seconds for a list of realistic size, 46 seconds for the largest,
with Flask threads piling up behind it.

THE FIX

The readers - search(), bots_with_a_match(), indexed_bots() - have their own
connection to the same WAL database under their own lock,
runtime.list_index_read_lock. WAL gives each of their queries a snapshot of
the last committed state, which is the guarantee the lock used to give: the
old list until the write commits, the new one after, never the gap.

WHAT THESE TESTS HOLD IT TO

- A query made while a write is open, its delete done and not committed, is
  answered at once, and from the old list.
- The lock order the second lock brings: the write lock, then the read lock.
  Every entry point is run with both locks watched, and no thread ever asks
  for the write lock while it holds the read lock.
- The read connection is closed by close(), reset_for_tests() and a moved
  LIST_INDEX_FILE, and it is closed BEFORE a damaged file is renamed aside:
  on Windows the rename fails with a sharing violation while any handle is
  open. That is checked by watching the rename, so it holds on every system,
  and then end to end.
- It is opened only once the writer has made the schema, so it never creates
  an empty database file of its own.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import list_index  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


# Long enough that a loaded CI runner answers well inside it, and only ever
# waited out in full when the fix is gone.
ANSWER_WITHIN = 15.0


class ReadIndexCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.index_dir = tempfile.mkdtemp(prefix="dccore-read-index-")
        self.addCleanup(shutil.rmtree, self.index_dir, True)
        self.path = os.path.join(self.index_dir, "idx.db")
        self.set_config(LIST_INDEX_FILE=self.path)
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def index(self, bot, *filenames):
        import list as list_mod
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": name, "size": "4.00MB", "folder": "D:\\MEDIA\\Some Folder\\"}
             for name in filenames], bot)
        return list_index.index_bot_list(bot, rows)

    def in_another_thread(self, call):
        """Run `call` on a thread and wait for it to finish, or give up."""
        result = {}

        def run():
            try:
                result["value"] = call()
            except BaseException as err:  # pragma: no cover - reported below
                result["error"] = err

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(ANSWER_WITHIN)
        return worker, result


class AKeystrokeDuringAWriteIsAnsweredFromTheOldList(ReadIndexCase):

    def test_a_query_is_answered_while_the_write_lock_is_held(self):
        self.index("SomeBot", "Blue Monday.mp3")
        self.assertEqual(len(list_index.search(["blue"], bots=["SomeBot"])), 1)

        with runtime.list_index_lock:
            # The write, half done: the bot's rows deleted, not committed.
            writer = list_index._connection
            writer.execute("DELETE FROM entries WHERE bot = ?", ("somebot",))
            try:
                worker, result = self.in_another_thread(lambda: (
                    list_index.search(["blue"], bots=["SomeBot"]),
                    list_index.bots_with_a_match(["blue"], ["SomeBot"]),
                    list_index.indexed_bots()))

                self.assertFalse(worker.is_alive(),
                                 "the filter waited for the write to finish")
                self.assertNotIn("error", result)
                rows, (matched, empty), bots = result["value"]
                # The snapshot from before the write: the old list, whole.
                self.assertEqual([r["filename"] for r in rows], ["Blue Monday.mp3"])
                self.assertEqual((matched, empty), ({"somebot"}, set()))
                self.assertEqual(bots, {"somebot"})

                writer.execute(
                    "INSERT INTO entries (bot, filename, folder, size) "
                    "VALUES (?, ?, ?, ?)", ("somebot", "Temptation.mp3", "", ""))
                writer.commit()
            except BaseException:
                writer.rollback()
                raise

        # And after the commit, the new list and only the new list.
        self.assertEqual([r["filename"] for r in list_index.search(["temptation"])],
                         ["Temptation.mp3"])
        self.assertEqual(list_index.search(["blue"]), [])

    def test_a_real_write_and_a_query_do_not_wait_on_each_other(self):
        """The same through the real index_bot_list(), stopped part-way by a
        row source that waits until a query has been answered."""
        self.index("SomeBot", "Blue Monday.mp3")
        list_index.search(["blue"])
        answered = threading.Event()
        seen = {}

        def rows():
            yield {"title": "Temptation.mp3", "folder": "", "size": ""}
            # The delete has run, the insert is under way, nothing committed.
            # Asked from a third thread, so a query that does wait for the
            # write gives up here instead of deadlocking the writer.
            query, result = self.in_another_thread(
                lambda: list_index.search(["blue"], bots=["SomeBot"]))
            if not query.is_alive():
                seen["mid"] = result.get("value")
                answered.set()
            yield {"title": "Ceremony.mp3", "folder": "", "size": ""}

        class Rows(object):
            def __iter__(self):
                return rows()

            def __len__(self):
                return 2

        worker, result = self.in_another_thread(
            lambda: list_index.index_bot_list("SomeBot", Rows()))

        worker.join(ANSWER_WITHIN)
        self.assertFalse(worker.is_alive(), "the write never finished")
        self.assertTrue(answered.is_set(), "the query waited for the write")
        self.assertEqual(result.get("value"), 2)
        self.assertEqual([r["filename"] for r in seen["mid"]], ["Blue Monday.mp3"])
        self.assertEqual(sorted(r["filename"] for r in list_index.search(["mp3"])),
                         ["Ceremony.mp3", "Temptation.mp3"])


class _WatchedLock(object):
    """A lock that records, per thread, what else that thread held."""

    def __init__(self, name, real, held, violations, forbidden_while):
        self.name = name
        self.real = real
        self.held = held
        self.violations = violations
        self.forbidden_while = forbidden_while

    def _stack(self):
        stack = getattr(self.held, "stack", None)
        if stack is None:
            stack = self.held.stack = []
        return stack

    def __enter__(self):
        stack = self._stack()
        if self.forbidden_while in stack:
            self.violations.append((self.name, list(stack)))
        self.real.acquire()
        stack.append(self.name)
        return self

    def __exit__(self, *exc):
        self._stack().remove(self.name)
        self.real.release()
        return False


class TheLockOrderIsWriteThenRead(ReadIndexCase):

    def setUp(self):
        super().setUp()
        held = threading.local()
        self.violations = []
        self.order = []
        write = _WatchedLock("write", list_index._conn_lock, held, self.violations, "read")
        read = _WatchedLock("read", list_index._read_lock, held, self.violations, None)
        real_write, real_read = list_index._conn_lock, list_index._read_lock
        list_index._conn_lock, list_index._read_lock = write, read
        self.addCleanup(setattr, list_index, "_conn_lock", real_write)
        self.addCleanup(setattr, list_index, "_read_lock", real_read)

    def test_no_entry_point_asks_for_the_write_lock_while_holding_the_read_lock(self):
        self.index("SomeBot", "Blue Monday.mp3")
        list_index.search(["blue"], bots=["SomeBot"])
        list_index.bots_with_a_match(["blue"], ["SomeBot"])
        list_index.indexed_bots()
        self.index("OtherBot", "Temptation.mp3")
        list_index.drop_bot("OtherBot")
        moved = os.path.join(self.index_dir, "moved.db")
        self.set_config(LIST_INDEX_FILE=moved)
        list_index.search(["blue"])
        list_index.close()
        list_index.search(["blue"])
        list_index.reset_for_tests()

        damaged = os.path.join(self.index_dir, "damaged.db")
        with open(damaged, "wb") as handle:
            handle.write(b"not a database\n" * 50)
        self.set_config(LIST_INDEX_FILE=damaged,
                        fetched_bot_lists={"somebot": {"bot": "SomeBot"}})
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            list_index.bots_with_a_match(["blue"], ["SomeBot"])

        self.assertEqual(self.violations, [])

    def test_the_watch_itself_sees_a_violation(self):
        """Control: the property above is only worth something if this
        harness can fail it."""
        with list_index._read_lock:
            with list_index._conn_lock:
                pass
        self.assertEqual(len(self.violations), 1)
        self.violations.clear()


class TheReadConnectionIsClosedWithTheWriter(ReadIndexCase):

    def open_reader(self):
        self.index("SomeBot", "Blue Monday.mp3")
        list_index.search(["blue"])
        reader = list_index._read_connection
        self.assertIsNotNone(reader)
        self.assertIsNot(reader, list_index._connection)
        return reader

    def assertClosed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_close_closes_it(self):
        reader = self.open_reader()
        list_index.close()
        self.assertClosed(reader)
        self.assertIsNone(list_index._read_connection)

    def test_reset_for_tests_closes_it(self):
        reader = self.open_reader()
        list_index.reset_for_tests()
        self.assertClosed(reader)
        self.assertIsNone(list_index._read_connection)

    def test_a_moved_index_file_closes_it_and_reads_the_new_one(self):
        reader = self.open_reader()
        moved = os.path.join(self.index_dir, "moved.db")
        self.set_config(LIST_INDEX_FILE=moved)
        # A write to the new file is what moves the writer; the reader must
        # not stay behind on the old one.
        self.index("SomeBot", "Temptation.mp3")
        self.assertClosed(reader)
        self.assertEqual([r["filename"] for r in list_index.search(["temptation"])],
                         ["Temptation.mp3"])
        self.assertEqual(list_index.search(["blue"]), [])
        self.assertEqual(list_index._read_connection_path, moved)

    def test_a_moved_index_file_is_read_before_anything_writes_to_it(self):
        """The reader checks the path itself, the way _connect() does: a
        rehash that repoints the file is followed at the next keystroke."""
        reader = self.open_reader()
        moved = os.path.join(self.index_dir, "moved.db")
        self.set_config(LIST_INDEX_FILE=moved)
        self.assertEqual(list_index.search(["blue"]), [])
        self.assertClosed(reader)
        self.assertEqual(list_index._read_connection_path, moved)

    def test_a_query_reopens_it_after_a_close(self):
        self.open_reader()
        list_index.close()
        self.assertEqual(len(list_index.search(["blue"])), 1)
        self.assertIsNotNone(list_index._read_connection)

    def test_it_is_closed_before_a_damaged_file_is_moved_aside(self):
        """Watched at the rename itself, so this fails on every system and
        not only on Windows, where the rename is what would fail."""
        self.open_reader()
        open_at_rename = []
        real = list_index._move_aside

        def watched(path):
            open_at_rename.append(list_index._read_connection)
            return real(path)

        list_index._move_aside = watched
        self.addCleanup(setattr, list_index, "_move_aside", real)

        damaged = os.path.join(self.index_dir, "damaged.db")
        with open(damaged, "wb") as handle:
            handle.write(b"not a database\n" * 50)
        self.set_config(LIST_INDEX_FILE=damaged)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            list_index.search(["blue"])

        self.assertEqual(open_at_rename, [None])

    def test_the_file_a_reader_had_open_can_be_repaired(self):
        """End to end: the reader had the file open, the file is damaged,
        and the repair must still be able to rename it."""
        self.open_reader()
        other = os.path.join(self.index_dir, "other.db")
        self.set_config(LIST_INDEX_FILE=other)
        self.index("SomeBot", "Temptation.mp3")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)
        with open(self.path, "wb") as handle:
            handle.write(b"not a database\n" * 50)
        self.set_config(LIST_INDEX_FILE=self.path)
        import contextlib
        import io
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertEqual(self.index("SomeBot", "Ceremony.mp3"), 1)

        self.assertIn("moved it to", log.getvalue())
        self.assertEqual([r["filename"] for r in list_index.search(["ceremony"])],
                         ["Ceremony.mp3"])


class TheReadConnectionNeverMakesTheFile(ReadIndexCase):

    def test_it_is_opened_only_once_the_schema_is_there(self):
        real = list_index.sqlite3.connect
        schema_at_open = []

        def watched(path, *args, **kwargs):
            has_schema = False
            if os.path.exists(path):
                with real(path) as probe:
                    has_schema = probe.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'entries'"
                    ).fetchone()[0] == 1
                probe.close()
            schema_at_open.append(has_schema)
            return real(path, *args, **kwargs)

        list_index.sqlite3.connect = watched
        self.addCleanup(setattr, list_index.sqlite3, "connect", real)

        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(list_index.search(["anything"]), [])

        # The writer made the file; the reader found it already made.
        self.assertEqual(schema_at_open, [False, True])

    def test_an_unopenable_index_opens_no_reader(self):
        blocker = os.path.join(self.index_dir, "not-a-directory")
        with open(blocker, "wb") as handle:
            handle.write(b"a file where the directory should be")
        self.set_config(LIST_INDEX_FILE=os.path.join(blocker, "idx.db"))
        list_index.reset_for_tests()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(list_index.search(["anything"]), [])
            self.assertEqual(list_index.bots_with_a_match(["x"], ["SomeBot"]),
                             (set(), set()))
            self.assertEqual(list_index.indexed_bots(), set())
        self.assertIsNone(list_index._read_connection)


class TheReadConnectionCannotWrite(ReadIndexCase):

    def test_it_is_query_only(self):
        self.index("SomeBot", "Blue Monday.mp3")
        list_index.search(["blue"])
        with self.assertRaises(sqlite3.OperationalError):
            list_index._read_connection.execute("DELETE FROM entries")


if __name__ == "__main__":
    unittest.main()
