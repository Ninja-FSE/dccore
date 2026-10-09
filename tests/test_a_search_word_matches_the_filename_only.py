"""A search word is matched against the filename, not the whole row (#1199).

Every row of a list is "!<nick> <filename>  ::INFO:: <size> ...". Matching a
word against all of it made `info` and `nfo` find every file, and the bot's
own nick find the whole list - in a channel `@find info` answered with the
first rows of the list, up to the limit. The cross-list search index already
held only the filename, so the filter bar and @find disagreed on one word.
Now both ask the filename alone; the size, length and quality in the tail are
not searched, as in the index.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

ROWS = [
    ("Artist - Album - 01 - Track.mp3", "10.3MB 4m31s 320/44.1/JS"),
    ("Artist - Album - 02 - Other.flac", "31MB"),
    ("Band - Info.mp3", "4MB"),
    ("NFO file for the album.txt", "1KB"),
    ("Somebot Tribute - Song.mp3", "5MB"),
]


class WhatAWordMatches(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.path = write_master_list(self.tree.lists, "SomeBot", [("D:\\MUSIC\\Album\\", ROWS)])

    def matched(self, term):
        entries, total = list_mod.find_matching_entries(
            list_mod.split_search_term(term), list_path=self.path)
        self.assertEqual(total, len(entries))
        return sorted(entry["filename"] for entry in entries)

    def test_the_marker_word_finds_only_a_file_with_it_in_the_name(self):
        self.assertEqual(self.matched("info"), ["Band - Info.mp3"])

    def test_nfo_finds_only_the_files_with_it_in_the_name(self):
        self.assertEqual(self.matched("nfo"), ["Band - Info.mp3", "NFO file for the album.txt"])

    def test_the_bots_own_nick_finds_only_a_file_with_it_in_the_name(self):
        self.assertEqual(self.matched("somebot"), ["Somebot Tribute - Song.mp3"])

    def test_the_marker_itself_is_never_searched_for(self):
        self.assertEqual(self.matched("::info::"), [])

    def test_a_word_in_the_filename_still_matches(self):
        self.assertEqual(self.matched("album"), sorted(
            ["Artist - Album - 01 - Track.mp3", "Artist - Album - 02 - Other.flac",
             "NFO file for the album.txt"]))

    def test_the_extension_is_part_of_the_filename(self):
        self.assertEqual(self.matched("flac"), ["Artist - Album - 02 - Other.flac"])

    def test_a_quoted_phrase_still_matches_across_the_filename(self):
        self.assertEqual(self.matched('"album 01"'), ["Artist - Album - 01 - Track.mp3"])

    def test_a_phrase_cannot_run_over_into_the_tail(self):
        self.assertEqual(self.matched('"track mp3 info"'), [])

    def test_the_size_length_and_quality_are_not_searched(self):
        for term in ("10.3MB", "4m31s", "320", "KB"):
            with self.subTest(term=term):
                self.assertEqual(self.matched(term), [])

    def test_an_empty_search_still_matches_every_row(self):
        self.assertEqual(len(self.matched("")), len(ROWS))


class ARowWithoutTheMarker(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.path = os.path.join(self.tree.lists, "Other-2026-08-25.txt")
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            f.write("=" * 53 + "\nD:\\MUSIC\\Folder\\\n" + "=" * 53 + "\n")
            f.write("!OtherBot Plain Name.mp3\n")
            f.write("!OtherBot Dash Style - Song.mp3 ---- 18.8Mb\n")
            f.write("!OtherBot Upper Case Marker.mp3 ::Info:: 4MB\n")

    def matched(self, term):
        entries, _ = list_mod.find_matching_entries(
            list_mod.split_search_term(term), list_path=self.path)
        return sorted(entry["line"] for entry in entries)

    def test_it_matches_on_its_whole_name(self):
        self.assertEqual(self.matched("plain name"), ["!OtherBot Plain Name.mp3"])

    def test_the_nick_is_not_matched_there_either(self):
        self.assertEqual(self.matched("otherbot"), [])

    def test_a_dash_size_tail_is_not_searched(self):
        self.assertEqual(self.matched("18.8Mb"), [])
        self.assertEqual(self.matched("dash style"), ["!OtherBot Dash Style - Song.mp3 ---- 18.8Mb"])

    def test_the_marker_is_found_whatever_its_case(self):
        self.assertEqual(self.matched("info"), [])
        self.assertEqual(self.matched("upper case"), ["!OtherBot Upper Case Marker.mp3 ::Info:: 4MB"])
