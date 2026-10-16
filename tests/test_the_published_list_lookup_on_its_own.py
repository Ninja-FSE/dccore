"""#1298: dcc.path_from_published_lists(), step 1 of splitting dcc.py.

The part of handle_download_request() that turns a requested name into a
path on disk by streaming the published lists: the folder heading above a
matching row places it, the match ignores case, the pasted ::INFO:: size
picks between same-named copies, and the list's own spelling is the one
returned (#445). It was reachable only by driving the whole request; here
it is called directly. The request-level tests (the #445 file,
test_path_security.py and others) still cover the wiring.

Every nick, album and file here is invented.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

RULE = "=" * 53


class TheLookup(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(NICKNAME="alfa", LIST_BASE_NAME="alfa")
        self.paths = []

    def album(self, name, files):
        directory = os.path.join(self.tree.music, name)
        os.makedirs(directory, exist_ok=True)
        for file_name in files:
            with io.open(os.path.join(directory, file_name), "w", encoding="utf-8") as handle:
                handle.write(name)
        return directory

    def write_list(self, *blocks, name="alfa-2026-01-01.txt"):
        """Blocks of (album heading or None, [(file, size), ...])."""
        lines = ["List of files\n", "\n"]
        for heading, rows in blocks:
            if heading is not None:
                lines += [RULE + "\n", "D:\\MUSIC\\%s\\\n" % heading, RULE + "\n"]
            lines += ["!%s %s  ::INFO:: %s\n" % (config.NICKNAME, f, size) for f, size in rows]
        path = os.path.join(self.tree.lists, name)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        self.paths.append(path)
        return path

    def find(self, requested, hint=""):
        return dcc.path_from_published_lists(self.paths, requested, hint, None)

    def test_the_heading_above_the_row_places_it(self):
        directory = self.album("Alpha Album", ["Track 01.flac"])
        self.write_list(("Alpha Album", [("Track 01.flac", "1.0MB")]))
        self.assertEqual(self.find("Track 01.flac"), os.path.join(directory, "Track 01.flac"))

    def test_case_is_ignored_and_the_lists_spelling_returned(self):
        directory = self.album("Alpha Album", ["Track 01.flac"])
        self.write_list(("Alpha Album", [("Track 01.flac", "1.0MB")]))
        self.assertEqual(self.find("track 01.FLAC"), os.path.join(directory, "Track 01.flac"))

    def test_without_a_hint_the_first_copy_wins(self):
        first = self.album("First", ["Intro.mp3"])
        self.album("Second", ["Intro.mp3"])
        self.write_list(("First", [("Intro.mp3", "1.0MB")]), ("Second", [("Intro.mp3", "2.0MB")]))
        self.assertEqual(self.find("Intro.mp3"), os.path.join(first, "Intro.mp3"))

    def test_a_hint_picks_the_copy_of_that_size(self):
        self.album("First", ["Intro.mp3"])
        second = self.album("Second", ["Intro.mp3"])
        self.write_list(("First", [("Intro.mp3", "1.0MB")]), ("Second", [("Intro.mp3", "2.0MB")]))
        self.assertEqual(self.find("Intro.mp3", "2.0mb"), os.path.join(second, "Intro.mp3"))

    def test_a_hint_matching_nothing_falls_back_to_the_first(self):
        first = self.album("First", ["Intro.mp3"])
        self.write_list(("First", [("Intro.mp3", "1.0MB")]))
        self.assertEqual(self.find("Intro.mp3", "9.9mb"), os.path.join(first, "Intro.mp3"))

    def test_the_heading_carries_across_list_files(self):
        """The music list then the film list, read as one stream."""
        directory = self.album("Alpha Album", ["Track 01.flac", "Clip.mkv"])
        self.write_list(("Alpha Album", [("Track 01.flac", "1.0MB")]))
        self.write_list((None, [("Clip.mkv", "5.0MB")]), name="alfa-VIDEO-2026-01-01.txt")
        self.assertEqual(self.find("Clip.mkv"), os.path.join(directory, "Clip.mkv"))

    def test_a_row_before_any_heading_is_not_placed(self):
        # Even with the file at the top of the library, where a guessed
        # heading would find it: a row with no folder above it says nowhere.
        with io.open(os.path.join(self.tree.music, "Loose.flac"), "w", encoding="utf-8") as handle:
            handle.write("loose")
        self.write_list((None, [("Loose.flac", "1.0MB")]))
        self.assertIsNone(self.find("Loose.flac"))

    def test_another_bots_rows_are_not_ours(self):
        self.album("Alpha Album", ["Track 01.flac"])
        path = self.write_list(("Alpha Album", []))
        with io.open(path, "a", encoding="utf-8") as handle:
            handle.write("!otherbot Track 01.flac  ::INFO:: 1.0MB\n")
        self.assertIsNone(self.find("Track 01.flac"))

    def test_a_row_the_disk_does_not_have_is_none(self):
        self.album("Alpha Album", [])
        self.write_list(("Alpha Album", [("Gone.flac", "1.0MB")]))
        self.assertIsNone(self.find("Gone.flac"))

    def test_a_name_no_list_has_is_none(self):
        self.album("Alpha Album", ["Track 01.flac"])
        self.write_list(("Alpha Album", [("Track 01.flac", "1.0MB")]))
        self.assertIsNone(self.find("Not There.flac"))


if __name__ == "__main__":
    unittest.main()
