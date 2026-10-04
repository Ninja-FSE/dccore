"""Installing a fetched list streams its rows into the index (#1134).

WHAT WAS WRONG

The "one courtesy parse" at fetch time - _install_fetched_list(), every extra
list in an archive through _measure_extra_list(), and every backfilled list
through list_index.backfill_missing() - called find_matching_entries() with
no limit, which built a dict per row, then entries_to_filelist_rows(), which
built a second dict per row. Both lists stayed alive while len(rows) was
taken and while index_bot_list() wrote them, although index_bot_list()
already consumes its rows one at a time. Measured at 378k rows: +412 MB peak,
against +143 MB streamed.

THE FIX

list.iter_filelist_rows() yields the same rows, through the same scan and the
same dedup, one at a time, and list.CountedRows counts them as
index_bot_list() consumes them. Its total() drains whatever was not
consumed, which is the hole the audit's skeptic found: index_bot_list()
returns 0 WITHOUT iterating when the index is unavailable, and stops part-way
on an error, and a count taken from what it consumed would then be 0 or
partial, and _measure_extra_list()'s "no entries" check would throw away a
good list. A parse error is kept and raised again by total(), so a list that
cannot be read still fails where it failed before. index_bot_list() counts
the rows it inserts rather than taking len(rows), since a stream has no
length - and CountedRows has no __len__ on purpose: list() asks for one before
iterating, and would drain every row before handing over the first.

WHY THESE TESTS

The rows must be the same rows: compared with the materialised path on an
adversarial list. The counts must be right when the index takes nothing,
part, or all of them. And the install must not build the list: it is run
with the materialising functions made to fail.
"""

import contextlib
import io
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


_EDGE_LINES = [
    b"=====", b"= = =", b"=\x00=", b"\x00\x00", b"=\xc2\xa0=", b"====",
    b"!", b"D:\\MEDIA\\Some Folder\\", b"Other Folder",
    b"!SomeBot Love Song.flac  ::INFO:: 10MB",
    b"!SomeBot love song.FLAC  ::INFO:: 10MB",      # a duplicate once folded
    b"!SomeBot \xce\xa3\xce\xbf\xcf\x82.mp3 ::INFO:: 3MB",
    b"!SomeBot \xe2\x84\xaaelvin.flac\t::Info::\t1MB",
    b"!SomeBot \xc4\xb0stanbul.flac ::INFO:: 2MB",
    b"!SomeBot Bad \xff\xfe Bytes.mp3  ::INFO:: 4MB",
    b"!SomeBot vivaldi - winter.flac ---- 18.8Mb",
    b"!SomeBot !rar Some Folder",
    b"!SomeBot",
]


def write_adversarial_list(path, seed=1134, lines=3000):
    rng = random.Random(seed)
    data = bytearray()
    for n in range(lines):
        line = rng.choice(_EDGE_LINES)
        if rng.random() < 0.3:
            line = b"!SomeBot Track %d.flac  ::INFO:: %dMB" % (n % 700, n % 3)
        data += line + rng.choice([b"\n", b"\r\n", b"\r"])
    with io.open(path, "wb") as handle:
        handle.write(bytes(data))
    return path


def materialised_rows(path, source):
    """The path every caller took before #1134."""
    entries, _total = list_mod.find_matching_entries([], limit=None, list_path=path)
    return list_mod.entries_to_filelist_rows(entries, source)


class Case(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-streamed-rows-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.set_config(LIST_INDEX_FILE=os.path.join(self.tmp, "idx.db"))
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)
        self.path = write_adversarial_list(os.path.join(self.tmp, "SomeBot.txt"))

    def index_content(self, name):
        with contextlib.closing(sqlite3.connect(os.path.join(self.tmp, "idx.db"))) as conn:
            return conn.execute(
                "SELECT rowid, bot, filename, folder, size FROM entries "
                "WHERE bot = ? ORDER BY rowid", (name,)).fetchall()

    def quiet(self, call, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = call(*args)
        return result, out.getvalue()


class TheStreamedRowsAreTheSameRows(Case):

    def test_the_same_rows_in_the_same_order(self):
        expected = materialised_rows(self.path, "SomeBot")
        self.assertGreater(len(expected), 500)
        self.assertEqual(list(list_mod.iter_filelist_rows(self.path, "SomeBot")), expected)

    def test_a_missing_file_has_no_rows(self):
        missing = os.path.join(self.tmp, "nothing.txt")
        self.assertEqual(list(list_mod.iter_filelist_rows(missing, "SomeBot")),
                         materialised_rows(missing, "SomeBot"))

    def test_the_index_holds_what_it_held_before(self):
        list_index.index_bot_list("SomeBot", materialised_rows(self.path, "SomeBot"))
        before = self.index_content("somebot")
        list_index.reset_for_tests()
        os.remove(os.path.join(self.tmp, "idx.db"))

        rows = list_mod.CountedRows(list_mod.iter_filelist_rows(self.path, "SomeBot"))
        self.assertEqual(list_index.index_bot_list("SomeBot", rows), len(before))
        self.assertEqual(self.index_content("somebot"), before)


class TheCountIsEveryRow(Case):

    def expected(self):
        return len(materialised_rows(self.path, "SomeBot"))

    def counted(self):
        return list_mod.CountedRows(list_mod.iter_filelist_rows(self.path, "SomeBot"))

    def test_counted_after_a_full_pass(self):
        rows = self.counted()
        self.assertEqual(sum(1 for _row in rows), self.expected())
        self.assertEqual(rows.total(), self.expected())

    def test_counted_when_nothing_iterated(self):
        """index_bot_list() returns 0 without iterating when the index is
        unavailable. The count must still be the list's."""
        self.assertEqual(self.counted().total(), self.expected())

    def test_counted_when_the_index_is_unavailable(self):
        blocker = os.path.join(self.tmp, "not-a-directory")
        with open(blocker, "wb") as handle:
            handle.write(b"a file where the directory should be")
        self.set_config(LIST_INDEX_FILE=os.path.join(blocker, "idx.db"))
        list_index.reset_for_tests()
        rows = self.counted()
        indexed, _log = self.quiet(list_index.index_bot_list, "SomeBot", rows)
        self.assertEqual(indexed, 0)
        self.assertEqual(rows.total(), self.expected())

    def test_counted_when_the_write_stops_part_way(self):
        rows = self.counted()
        taken = []
        for row in rows:
            taken.append(row)
            if len(taken) == 7:
                break
        self.assertEqual(rows.total(), self.expected())

    def test_peeking_neither_loses_nor_doubles_a_row(self):
        rows = self.counted()
        self.assertTrue(rows.any_rows())
        self.assertTrue(rows.any_rows())
        self.assertEqual(list(rows), materialised_rows(self.path, "SomeBot"))
        self.assertEqual(rows.total(), self.expected())

    def test_an_empty_list_has_no_rows(self):
        empty = os.path.join(self.tmp, "empty.txt")
        with open(empty, "w") as handle:
            handle.write("List of 0 Files\n")
        rows = list_mod.CountedRows(list_mod.iter_filelist_rows(empty, "SomeBot"))
        self.assertFalse(rows.any_rows())
        self.assertEqual(rows.total(), 0)

    def test_an_error_met_while_peeking_is_kept_for_the_count(self):
        def broken():
            raise OSError("the disk went away")
            yield  # pragma: no cover

        rows = list_mod.CountedRows(broken())
        with self.assertRaises(OSError):
            rows.any_rows()
        with self.assertRaises(OSError):
            rows.total()

    def test_a_parse_error_is_raised_again_by_the_count(self):
        def broken():
            yield {"title": "One.flac", "folder": "", "size": ""}
            raise OSError("the disk went away")

        rows = list_mod.CountedRows(broken())
        _indexed, log = self.quiet(list_index.index_bot_list, "SomeBot", rows)
        self.assertIn("the disk went away", log)
        self.assertIsInstance(rows.error, OSError)
        with self.assertRaises(OSError):
            rows.total()
        # And the half-written list was rolled back, not left in place.
        self.assertEqual(self.index_content("somebot"), [])


class TheInstallDoesNotBuildTheList(Case):

    def setUp(self):
        super().setUp()
        self.set_config(FETCHED_FILES_DIR=self.tmp)

        def refuse(*_args, **_kwargs):
            raise AssertionError("the whole list was built in memory")

        real_find, real_rows = list_mod.find_matching_entries, list_mod.entries_to_filelist_rows
        self.addCleanup(setattr, list_mod, "find_matching_entries", real_find)
        self.addCleanup(setattr, list_mod, "entries_to_filelist_rows", real_rows)
        self.expected = materialised_rows(self.path, "SomeBot")
        list_mod.find_matching_entries = refuse
        list_mod.entries_to_filelist_rows = refuse

    def test_the_main_list(self):
        import zipfile
        zip_path = os.path.join(self.tmp, "incoming.zip")
        with open(self.path, "rb") as handle:
            data = handle.read()
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("SomeBot-2026-10-03.txt", data)

        (ok, reason), log = self.quiet(list_fetch.process_fetched_list_zip, "SomeBot", zip_path)

        self.assertTrue(ok, reason + log if reason else log)
        self.assertEqual(config.fetched_bot_lists["somebot"]["entry_count"],
                         len(self.expected))
        self.assertEqual(len(self.index_content("somebot")), len(self.expected))

    def test_the_main_list_is_counted_when_the_index_is_unavailable(self):
        """The skeptic's hole: index_bot_list() takes no row at all then,
        and the count stored for the list must still be the list's."""
        import zipfile
        blocker = os.path.join(self.tmp, "not-a-directory")
        with open(blocker, "wb") as handle:
            handle.write(b"a file where the directory should be")
        self.set_config(LIST_INDEX_FILE=os.path.join(blocker, "idx.db"))
        list_index.reset_for_tests()
        zip_path = os.path.join(self.tmp, "incoming.zip")
        with open(self.path, "rb") as handle:
            data = handle.read()
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("SomeBot-2026-10-03.txt", data)

        (ok, reason), log = self.quiet(list_fetch.process_fetched_list_zip, "SomeBot", zip_path)

        self.assertTrue(ok, reason)
        self.assertEqual(config.fetched_bot_lists["somebot"]["entry_count"],
                         len(self.expected))
        self.assertIn("reached the search index", log)

    def test_an_extra_list(self):
        info, _log = self.quiet(list_fetch._measure_extra_list, "SomeBot", "rar", self.path)
        self.assertEqual(info["entry_count"], len(self.expected))
        self.assertEqual(len(self.index_content("somebot/rar")), len(self.expected))

    def test_an_extra_list_with_no_entries_is_skipped_and_not_indexed(self):
        empty = os.path.join(self.tmp, "readme.txt")
        with open(empty, "w") as handle:
            handle.write("Nothing to see here\n")
        list_index.index_bot_list("SomeBot/rar", [{"title": "Old.flac"}])

        info, log = self.quiet(list_fetch._measure_extra_list, "SomeBot", "rar", empty)

        self.assertIsNone(info)
        self.assertIn("no entries", log)
        # Nothing was written: the index still holds what it held.
        self.assertEqual([row[2] for row in self.index_content("somebot/rar")], ["Old.flac"])

    def test_an_extra_list_that_cannot_be_read_is_skipped(self):
        def broken(*_args, **_kwargs):
            def rows():
                yield {"title": "One.flac", "folder": "", "size": ""}
                raise OSError("the disk went away")
            return rows()

        real = list_mod.iter_filelist_rows
        list_mod.iter_filelist_rows = broken
        self.addCleanup(setattr, list_mod, "iter_filelist_rows", real)

        info, log = self.quiet(list_fetch._measure_extra_list, "SomeBot", "rar", self.path)

        self.assertIsNone(info)
        self.assertIn("could not parse it", log)

    def test_a_held_list_is_backfilled(self):
        said = []
        held = {"somebot": {"bot": "SomeBot", "list_path": self.path}}
        self.assertEqual(list_index.backfill_missing(held, log=said.append), 1)
        self.assertEqual(len(self.index_content("somebot")), len(self.expected))

    def test_a_held_list_that_cannot_be_read_says_so(self):
        def broken(*_args, **_kwargs):
            def rows():
                raise OSError("the disk went away")
                yield  # pragma: no cover
            return rows()

        real = list_mod.iter_filelist_rows
        list_mod.iter_filelist_rows = broken
        self.addCleanup(setattr, list_mod, "iter_filelist_rows", real)
        said = []
        held = {"somebot": {"bot": "SomeBot", "list_path": self.path}}

        done, _log = self.quiet(list_index.backfill_missing, held, said.append)

        self.assertEqual(done, 0)
        self.assertTrue(any("Could not re-read" in line for line in said), said)


if __name__ == "__main__":
    unittest.main()
