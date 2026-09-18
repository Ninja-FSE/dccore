"""#69: a size on each album. Wanted from the start, and deferred twice - the
first time because a !rar row must stay verbatim (AutoQ copies it), the
second because "put it on the folder heading" turned out to be wrong too:
the heading is not decoration. list.py's reader and dcc.py's request
resolver both take the whole heading line as the folder path, and so does
every older DCCore that fetches this list.

So it is its own line, under the closing rule:

    ==========================
    D:\\MEDIA\\Artist\\Album\\
    ==========================
        14 files, 1.20GB
    !Bot 01 - Track.flac  ::INFO:: 87.61MB

A line that is neither a rule nor a "!" row is dropped by every reader in
its resting state, which is what makes this safe to add to a format other
software already parses. The tests below check that claim against each
reader rather than assert it.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.test_master_list_generation import MasterListCase  # noqa: E402


class TheLine(unittest.TestCase):
    def test_count_and_size(self):
        line = update_list.folder_summary_line(14, 1288490188, lambda b: "1.20GB")
        self.assertEqual(line, "    14 files, 1.20GB")

    def test_one_file_is_singular(self):
        self.assertEqual(update_list.folder_summary_line(1, 10, lambda b: "10B"),
                         "    1 file, 10B")

    def test_thousands_are_grouped(self):
        self.assertTrue(update_list.folder_summary_line(12345, 0, lambda b: "0B")
                        .startswith("    12,345 files"))

    def test_it_is_indented_and_not_a_rule_and_not_a_request(self):
        """The three shapes every reader keys on; this must be none of them."""
        line = update_list.folder_summary_line(3, 9, lambda b: "9B")
        self.assertTrue(line.startswith(" "))
        self.assertNotEqual(set(line.strip()), {"="})
        self.assertFalse(line.strip().startswith("!"))
        for prefix in list_mod.LIST_FOLDER_PREFIXES:
            self.assertFalse(line.strip().upper().startswith(prefix))

    def test_folder_totals(self):
        rows = [("A", "x", 10), ("A", "y", 5), ("B", "z", 1), ("A", "w", None)]
        self.assertEqual(update_list.folder_totals(rows), {"A": (3, 15), "B": (1, 1)})


class ItIsWrittenUnderEveryHeading(MasterListCase):
    def test_the_music_list(self):
        self.use_empty_library()
        self.add("Artist/Album/01.flac", b"\x00" * 3000)
        self.add("Artist/Album/02.flac", b"\x00" * 1000)

        self.assertTrue(self.generate())

        lines = self.read_list().split("\n")
        heading = next(i for i, l in enumerate(lines) if "Artist\\Album\\" in l)
        self.assertEqual(set(lines[heading + 1]), {"="}, "closing rule expected")
        self.assertEqual(lines[heading + 2].strip(), "2 files, 3.91KB")
        self.assertTrue(lines[heading + 3].startswith("!"), "then the rows")

    def test_the_film_list(self):
        self.use_empty_library()
        self.add("Films/Feature/feature.mkv", b"\x00" * 2048)

        self.assertTrue(self.generate())

        lines = self.read_video_list().split("\n")
        heading = next(i for i, l in enumerate(lines) if "Feature\\" in l)
        self.assertEqual(lines[heading + 2].strip(), "1 file, 2.00KB")

    def test_the_album_list_stays_one_line_per_album(self):
        """The constraint that deferred this twice: a !rar row is copied
        verbatim by AutoQ, so that file gains nothing."""
        self.use_empty_library()
        self.add("Artist/Album/01.mp3")
        self.set_config(RAR_ENABLED=True)

        self.assertTrue(self.generate())

        with open(self.rar_path(), encoding="utf-8") as handle:
            body = handle.read().split("=" * 90, 1)[1]
        rows = [l for l in body.split("\n") if l.strip()]
        self.assertEqual(len(rows), 1, rows)
        self.assertNotIn("file", rows[0].lower().split("!rar")[1])


class EveryReaderIgnoresIt(MasterListCase):
    """The claim the whole design rests on, checked reader by reader."""

    def setUp(self):
        super().setUp()
        self.use_empty_library()
        self.add("Artist/Album/Track One.flac", b"\x00" * 4096)
        self.add("Artist/Album/Track Two.flac", b"\x00" * 4096)
        self.assertTrue(self.generate())

    def test_search_attributes_rows_to_the_right_folder(self):
        """list.py's state machine: the summary must neither become the
        folder nor swallow the row after it."""
        entries, total = list_mod.find_matching_entries(["track"], limit=None)

        self.assertEqual(total, 2)
        for entry in entries:
            self.assertTrue(entry["folder"].endswith("Artist\\Album\\"), entry["folder"])

    def test_the_folder_heading_resolves_to_the_real_folder(self):
        """dcc.py's request path goes through resolve_list_folder() on the
        heading line - which must still be the heading, not the summary."""
        with open(self.list_path(), encoding="utf-8") as handle:
            lines = [l.strip() for l in handle if l.strip()]
        heading = next(l for l in lines if "Artist\\Album\\" in l)
        summary = next(l for l in lines if "2 files" in l)

        resolved = list_mod.resolve_list_folder(heading)
        self.assertIsNotNone(resolved)
        self.assertTrue(os.path.isdir(resolved))
        self.assertNotIn(summary, heading)

    def test_the_request_count_is_unchanged(self):
        """count_request_lines() counts "!" rows - the advert's file count."""
        self.assertEqual(list_mod.count_request_lines([self.list_path()]), 2)

    def test_a_fetched_list_from_a_newer_bot_reads_the_same(self):
        """The other direction: our parser on a list another DCCore wrote
        with this line in it. Same parser, same answer - which is what an
        OLDER peer without this change would get too, since the parser's
        resting-state rule predates it."""
        entries, _ = list_mod.find_matching_entries(
            ["track"], limit=None, list_path=self.list_path())
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
