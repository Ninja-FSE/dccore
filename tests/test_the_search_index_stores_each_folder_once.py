"""The search index stores each folder heading once per list (#1135).

Every FTS5 row carried its full heading, which repeats about nine times per
folder at the median, so the index was larger than the list text it indexed.
A list's headings now live once in a `folders` table and each row holds an
id. That is only a saving if the table does not grow without end: a refetch
under a new set of headings and a purge must take the old headings with them,
which is what the audit's skeptic asked to be held. What search() shows is
held exactly as before by
test_the_rebuilt_search_index_answers_every_query_as_before.
"""

import contextlib
import os
import sqlite3

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase

import list as list_mod  # noqa: E402
import list_index  # noqa: E402


class FolderCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-index-folders-")
        self.path = os.path.join(self.dir, "idx.db")
        self.set_config(LIST_INDEX_FILE=self.path)
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def index(self, bot, layout):
        """layout: {heading: number of files under it}."""
        entries = [{"filename": f"Track {n:02d} of folder {k}.flac",
                    "folder": heading, "size": "4.00MB"}
                   for k, (heading, count) in enumerate(layout.items())
                   for n in range(count)]
        return list_index.index_bot_list(
            bot, list_mod.entries_to_filelist_rows(entries, bot))

    def query(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql, args).fetchall()

    def folders(self, bot):
        return sorted(r[0] for r in self.query(
            "SELECT folder FROM folders WHERE bot = ?", bot))


class EachHeadingIsStoredOnce(FolderCase):

    def test_once_per_list_however_many_rows_it_has(self):
        self.index("SomeBot", {"D:\\A\\": 12, "D:\\a\\": 5, "": 3})

        self.assertEqual(self.folders("somebot"), ["", "D:\\A\\", "D:\\a\\"])

    def test_the_rows_hold_ids_and_not_the_heading(self):
        self.index("SomeBot", {"D:\\MUSIC\\A Long Album Title (2001) [FLAC]\\": 9})

        kinds = self.query("SELECT DISTINCT typeof(folder) FROM entries")
        self.assertEqual(kinds, [("integer",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM entries WHERE folder LIKE 'D:%'"),
                         [(0,)])

    def test_two_lists_keep_their_own_headings(self):
        self.index("SomeBot", {"D:\\Shared\\": 2})
        self.index("OtherBot", {"D:\\Shared\\": 3})

        ids = self.query("SELECT bot, id FROM folders ORDER BY bot")
        self.assertEqual(len({i for _b, i in ids}), 2, ids)
        rows = list_index.search(["track"], limit=2000)
        self.assertEqual({(r["bot"], r["folder"]) for r in rows},
                         {("somebot", "D:\\Shared\\"), ("otherbot", "D:\\Shared\\")})


class OldHeadingsDoNotPileUp(FolderCase):

    def test_a_refetch_replaces_the_lists_headings(self):
        self.index("SomeBot", {"D:\\Old One\\": 4, "D:\\Old Two\\": 2})
        self.index("OtherBot", {"D:\\Theirs\\": 1})

        self.index("SomeBot", {"D:\\New\\": 3})

        self.assertEqual(self.folders("somebot"), ["D:\\New\\"])
        self.assertEqual(self.folders("otherbot"), ["D:\\Theirs\\"])
        self.assertEqual({r["folder"] for r in list_index.search(["track"], limit=2000)},
                         {"D:\\New\\", "D:\\Theirs\\"})

    def test_dropping_a_list_drops_its_headings(self):
        self.index("SomeBot", {"D:\\Mine\\": 4})
        self.index("OtherBot", {"D:\\Theirs\\": 1})

        self.assertTrue(list_index.drop_bot("SomeBot"))

        self.assertEqual(self.folders("somebot"), [])
        self.assertEqual(self.folders("otherbot"), ["D:\\Theirs\\"])
        self.assertEqual([r["folder"] for r in list_index.search(["track"])],
                         ["D:\\Theirs\\"])

    def test_a_refetch_whose_ids_are_reused_shows_the_new_headings(self):
        """The list that held the highest ids is refetched, so its new
        headings may take the same numbers. Each row must show its own."""
        self.index("OtherBot", {"D:\\Theirs\\": 1})
        self.index("SomeBot", {"D:\\Old\\": 2})
        self.index("SomeBot", {"D:\\New A\\": 1, "D:\\New B\\": 1})

        shown = {(r["bot"], r["filename"], r["folder"])
                 for r in list_index.search(["track"], limit=2000)}
        self.assertEqual(shown, {
            ("otherbot", "Track 00 of folder 0.flac", "D:\\Theirs\\"),
            ("somebot", "Track 00 of folder 0.flac", "D:\\New A\\"),
            ("somebot", "Track 00 of folder 1.flac", "D:\\New B\\")})
