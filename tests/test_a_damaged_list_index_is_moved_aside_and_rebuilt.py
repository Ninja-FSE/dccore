"""#628: a corrupt list_index.db never self-healed, and the log said it would.

_connect() failed on a damaged file ("file is not a database", "database
disk image is malformed") and printed that the cross-list filter was "off
until the next fetch". But the fetch opens the file through the same
_connect(), and so does the startup backfill, so every path failed the same
way and nothing ever repaired it: the List Browser filter answered nothing for
the rest of the install's life, with a log line promising the opposite on
every keystroke, and the only recovery - deleting the file by hand - was said
nowhere the operator would see it.

The index is a cache of lists still on disk. So a damaged file is moved aside
as `<file>.corrupt-<timestamp>` (kept, not deleted), a fresh one is started in
its place, and the held lists are re-indexed from disk on the next filter
query - before the sidebar can report them all as empty.
"""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import list_index  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def _aside_files(index_dir):
    return sorted(name for name in os.listdir(index_dir)
                  if ".corrupt-" in name and not name.endswith(("-wal", "-shm")))


class DamagedIndexCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.index_dir = tempfile.mkdtemp(prefix="dccore-damaged-index-")
        self.path = os.path.join(self.index_dir, "idx.db")
        self.set_config(LIST_INDEX_FILE=self.path)
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def damage(self, content=b"this is not a database, it is a text file\n" * 90):
        with open(self.path, "wb") as handle:
            handle.write(content)

    def held_list(self, bot, *filenames):
        """A list file on disk and an entry pointing at it - what survives a
        restart, and what the rebuild reads."""
        path = os.path.join(self.index_dir, f"{bot}-list.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(f"List of {len(filenames)} Files\n\n")
            handle.write("=" * 20 + "\n")
            handle.write("D:\\MUSIC\\Some Folder\\\n")
            handle.write("=" * 20 + "\n")
            for name in filenames:
                handle.write(f"!{bot} {name}  ::INFO:: 4.00MB\n")
        return {"bot": bot, "fetched_at": 1, "entry_count": len(filenames),
                "list_path": path}

    def quiet(self, call, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = call(*args, **kwargs)
        return result, out.getvalue()


class ADamagedFileIsMovedAsideAndAFreshOneStarted(DamagedIndexCase):

    def test_the_next_fetch_does_index_its_list(self):
        """The audit's own reproduction: two index_bot_list() calls both
        returned 0 and left the file byte-for-byte as it was."""
        self.damage()
        import list as list_mod
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": "Enter Sandman.flac", "folder": "D:\\MUSIC\\",
              "size": "4.00MB"}], "BoomBox")

        indexed, _log = self.quiet(list_index.index_bot_list, "BoomBox", rows)

        self.assertEqual(indexed, 1)
        self.assertEqual([r["filename"] for r in list_index.search(["sandman"])],
                         ["Enter Sandman.flac"])

    def test_the_damaged_file_is_kept_beside_the_new_one(self):
        self.damage()

        _rows, log = self.quiet(list_index.search, ["anything"])

        aside = _aside_files(self.index_dir)
        self.assertEqual(len(aside), 1, log)
        self.assertTrue(aside[0].startswith("idx.db.corrupt-"), aside)
        with open(os.path.join(self.index_dir, aside[0]), "rb") as handle:
            self.assertTrue(handle.read().startswith(b"this is not a database"),
                            "the damaged file was not kept as it was")
        # And what now sits at the path is a real, empty index.
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    def test_the_log_says_what_happened_and_where_the_file_went(self):
        self.damage()

        _rows, log = self.quiet(list_index.search, ["anything"])

        aside = _aside_files(self.index_dir)[0]
        self.assertIn(aside, log)
        self.assertIn("file is not a database", log)
        self.assertNotIn("until the next fetch", log,
                         "the old promise, which no fetch could keep")

    def test_it_happens_once_and_not_on_every_keystroke(self):
        self.damage()

        _rows, first = self.quiet(list_index.search, ["anything"])
        _rows, second = self.quiet(list_index.search, ["anything"])
        _rows, third = self.quiet(list_index.bots_with_a_match, ["x"], ["BoomBox"])

        self.assertEqual(len(_aside_files(self.index_dir)), 1)
        self.assertIn("moved it to", first)
        self.assertEqual(second, "")
        self.assertEqual(third, "")

    def test_no_stale_wal_sidecar_is_left_beside_the_fresh_file(self):
        """A WAL left beside a fresh database is replayed into it on open.
        sqlite3 deletes the sidecars itself as the failed handle closes; if
        it could not, they are moved aside with the file. Either way, what
        sits beside the new index is not the old log."""
        self.damage()
        for suffix in ("-wal", "-shm"):
            with open(self.path + suffix, "wb") as handle:
                handle.write(b"stale sidecar")

        self.quiet(list_index.search, ["anything"])

        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                with open(self.path + suffix, "rb") as handle:
                    self.assertNotEqual(handle.read(), b"stale sidecar", suffix)

    def test_a_sidecar_that_survived_the_close_goes_with_the_file(self):
        """The branch sqlite3's own clean-up normally makes unreachable."""
        self.damage()
        real = list_index.os.rename
        planted = []

        def rename_then_plant(src, dst):
            real(src, dst)
            for suffix in ("-wal", "-shm"):
                with open(src + suffix, "wb") as handle:
                    handle.write(b"stale sidecar")
                planted.append(src + suffix)

        list_index.os.rename = rename_then_plant
        self.addCleanup(setattr, list_index.os, "rename", real)

        self.quiet(list_index.search, ["anything"])

        aside = os.path.join(self.index_dir, _aside_files(self.index_dir)[0])
        self.assertEqual(len(planted), 2)
        for suffix in ("-wal", "-shm"):
            self.assertTrue(os.path.exists(aside + suffix), suffix)
            if os.path.exists(self.path + suffix):
                with open(self.path + suffix, "rb") as handle:
                    self.assertNotEqual(handle.read(), b"stale sidecar", suffix)


class TheHeldListsAreIndexedAgainBeforeTheSidebarIsAnswered(DamagedIndexCase):
    """The fresh index is EMPTY, and an empty index answers "no list holds a
    match" - positively, greying out every list in the sidebar. That is the
    false claim the "no index" branch already refuses to make, so the rebuild
    has to run before the readers answer, not at the next restart."""

    def test_bots_with_a_match_finds_the_held_list(self):
        self.damage()
        self.set_config(fetched_bot_lists={
            "boombox": self.held_list("BoomBox", "Enter Sandman.flac")})

        (matched, empty), _log = self.quiet(
            list_index.bots_with_a_match, ["sandman"], ["BoomBox"])

        self.assertEqual((matched, empty), ({"boombox"}, set()))

    def test_search_finds_the_held_list(self):
        self.damage()
        self.set_config(fetched_bot_lists={
            "boombox": self.held_list("BoomBox", "Enter Sandman.flac")})

        # The very first query, not the second: the reader opens the index
        # (which repairs it) and runs the rebuild before it asks.
        rows, _log = self.quiet(list_index.search, ["sandman"], bots=["BoomBox"])

        self.assertEqual([r["filename"] for r in rows], ["Enter Sandman.flac"])

    def test_the_rebuild_runs_once(self):
        self.damage()
        entry = self.held_list("BoomBox", "Enter Sandman.flac")
        self.set_config(fetched_bot_lists={"boombox": entry})
        reads = []
        real = list_index.backfill_missing

        def counted(held, log=print):
            reads.append(dict(held))
            return real(held, log=log)

        list_index.backfill_missing = counted
        self.addCleanup(setattr, list_index, "backfill_missing", real)

        for _ in range(3):
            self.quiet(list_index.bots_with_a_match, ["sandman"], ["BoomBox"])

        self.assertEqual(len(reads), 1, reads)
        self.assertEqual(reads[0], {"boombox": entry})


class AHealthyFileThatCannotBeOpenedIsLeftAlone(DamagedIndexCase):
    """Only the file's own content is a reason to move it. A locked file, a
    full disk or a missing FTS5 build are sqlite3.OperationalError - a
    SUBCLASS of DatabaseError - and moving a fine index aside for one of
    those would throw away what it held."""

    def test_an_environmental_error_moves_nothing(self):
        import list as list_mod
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": "Enter Sandman.flac", "folder": "D:\\MUSIC\\",
              "size": "4.00MB"}], "BoomBox")
        self.assertEqual(list_index.index_bot_list("BoomBox", rows), 1)
        list_index.reset_for_tests()
        real = list_index.sqlite3.connect

        def locked(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        list_index.sqlite3.connect = locked
        self.addCleanup(setattr, list_index.sqlite3, "connect", real)

        rows_found, log = self.quiet(list_index.search, ["sandman"])

        self.assertEqual(rows_found, [])
        self.assertEqual(_aside_files(self.index_dir), [])
        self.assertIn("database is locked", log)
        self.assertNotIn("until the next fetch", log)
        list_index.sqlite3.connect = real
        self.assertEqual(len(list_index.search(["sandman"])), 1,
                         "the healthy index was lost")

    def test_a_missing_parent_that_cannot_be_made_moves_nothing(self):
        blocker = os.path.join(self.index_dir, "not-a-directory")
        with open(blocker, "wb") as handle:
            handle.write(b"a file where the directory should be")
        self.set_config(LIST_INDEX_FILE=os.path.join(blocker, "idx.db"))
        list_index.reset_for_tests()

        rows, log = self.quiet(list_index.search, ["anything"])

        self.assertEqual(rows, [])
        self.assertIn("Unavailable", log)
        self.assertEqual(_aside_files(self.index_dir), [])


class WhenTheDamagedFileCannotBeMoved(DamagedIndexCase):

    def test_the_operator_is_told_to_delete_it_by_hand(self):
        self.damage()
        real = list_index.os.rename

        def refused(_src, _dst):
            raise PermissionError(13, "Permission denied")

        list_index.os.rename = refused
        self.addCleanup(setattr, list_index.os, "rename", real)

        rows, log = self.quiet(list_index.search, ["anything"])

        self.assertEqual(rows, [])
        self.assertIn("deleted by hand", log)
        self.assertNotIn("until the next fetch", log)
        self.assertTrue(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
