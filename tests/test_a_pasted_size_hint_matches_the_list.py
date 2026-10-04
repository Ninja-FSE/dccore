"""A row pasted from the list matches the file it names (#1121).

The list writes every row's size with two decimals ("!Bot 01 - Track.flac
::INFO:: 7.30MB"), but the check behind the #886 folder memory formatted the
file's size with update_list.format_size_human(), which gives one ("7.3MB"),
and compared the two as text. They never matched, so the folder memory never
answered a pasted row: each one went to a full list scan - about three
seconds a row on a million-row list, for every track of an album pasted in
one go. The hint is now read as a number, at its own unit and precision.
"""

import io
import os

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_master_list_generation as generation  # noqa: E402

# Sizes that land on every unit and on rounding edges at two decimals.
SIZES = {
    "01 - Small.flac": 999,
    "02 - Kilo.flac": 1024,
    "03 - Just Under.flac": 1023,
    "04 - Two Kilo.flac": 2048,
    "05 - Mega.flac": 7_654_321,
    "06 - Mega Edge.flac": 5 * 1024 * 1024 + 1,
    "07 - Round Mega.flac": 1_500_000,
}


def rows_of(path):
    """{file name: size hint} for every file row of a list, as it was written."""
    rows = {}
    with io.open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("!") and "::INFO::" in line:
                head, hint = line.split("::INFO::", 1)
                rows[head.split(" ", 1)[1].strip()] = hint.strip()
    return rows


class RowsTheListWrote(generation.MasterListCase):
    def setUp(self):
        super().setUp()
        self.use_empty_library()
        self.paths = {name: self.add(os.path.join("Artist", "Album", name), b"\x00" * size)
                      for name, size in SIZES.items()}
        self.assertTrue(self.generate())
        self.rows = rows_of(self.list_path())

    def test_every_row_s_hint_matches_its_own_file(self):
        self.assertEqual(sorted(self.rows), sorted(SIZES), self.rows)
        for name, hint in self.rows.items():
            with self.subTest(name=name, hint=hint):
                self.assertTrue(dcc._matches_size_hint(self.paths[name], hint))

    def test_it_is_the_list_s_two_decimals(self):
        """Pinned so this test cannot pass on a list that writes one decimal."""
        self.assertEqual(self.rows["05 - Mega.flac"], "7.30MB")

    def test_another_size_is_still_turned_away(self):
        other = self.add(os.path.join("Artist", "Other", "05 - Mega.flac"), b"\x00" * (7_654_321 + 20_000))
        self.assertFalse(dcc._matches_size_hint(other, self.rows["05 - Mega.flac"]))

    def test_the_folder_memory_answers_a_pasted_row(self):
        """The #886 path itself: a sibling's folder is remembered, and a row
        pasted exactly as the list wrote it is found there without a scan."""
        folder = os.path.dirname(self.paths["05 - Mega.flac"])
        list_name = "DCCore"
        self.addCleanup(dcc.forget_library_lookups)
        with dcc._lookup_misses_lock:
            dcc._lookup_folders[list_name] = [folder]
        found = dcc._in_a_recent_folder(list_name, "05 - Mega.flac", self.rows["05 - Mega.flac"])
        self.assertEqual(found, os.path.join(folder, "05 - Mega.flac"))


class OtherWaysASizeIsWritten(support.DCCoreTestCase):
    """Lists from older versions and from other bots: each hint is taken at the
    precision it was written with, in any case."""

    def file_of(self, size):
        tree = self.make_tree()
        path = os.path.join(tree.root, "track.flac")
        with io.open(path, "wb") as handle:
            handle.write(b"\x00" * size)
        return path

    def test_kilobytes(self):
        path = self.file_of(1024)
        for hint in ("1.0KB", "1.00KB", "1KB", "1.0kb", "1.00Kb", "1024B", "1.0KB 4m31s 320/44.1/JS"):
            with self.subTest(hint=hint):
                self.assertTrue(dcc._matches_size_hint(path, hint))
        for hint in ("1.1KB", "2.0KB", "1.01KB", "1023B"):
            with self.subTest(hint=hint):
                self.assertFalse(dcc._matches_size_hint(path, hint))

    def test_megabytes(self):
        path = self.file_of(7_654_321)   # 7.2997... MB
        for hint in ("7.30MB", "7.3MB", "7MB", "7.30Mb", "7.300MB", "7474.9KB"):
            with self.subTest(hint=hint):
                self.assertTrue(dcc._matches_size_hint(path, hint))
        for hint in ("7.31MB", "7.29MB", "7.4MB", "8MB", "7.30GB"):
            with self.subTest(hint=hint):
                self.assertFalse(dcc._matches_size_hint(path, hint))

    def test_a_word_that_is_not_a_size_matches_nothing(self):
        path = self.file_of(7_654_321)
        self.assertFalse(dcc._matches_size_hint(path, "7.3"))       # no unit
        self.assertFalse(dcc._matches_size_hint(path, "big"))
        self.assertFalse(dcc._matches_size_hint(path, "7.3XB"))
        self.assertTrue(dcc._matches_size_hint(path, ""), "no hint matches anything, as before")

    def test_a_file_that_cannot_be_read_matches_nothing(self):
        self.assertFalse(dcc._matches_size_hint(os.path.join(self.make_tree().root, "gone.flac"), "7.30MB"))
