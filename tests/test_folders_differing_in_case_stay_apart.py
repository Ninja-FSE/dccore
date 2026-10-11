"""Two folders whose names differ only in case are two blocks in the list (#1270).

On a case-sensitive filesystem - every Linux install - "Band Alfa/Live" and
"band alfa/Live" are two folders. The rows were sorted by the lower-cased
folder and then the lower-cased filename, so the two folders' files
interleaved by name. The writer starts a heading whenever the folder changes,
so the list carried six headings for two folders, alternating, each with the
whole folder's "3 files" summary above a single row - and the List Browser and
every reader saw each folder split into runs.

The order is still case-insensitive; the exact folder now breaks the tie.

A stand-in for the walk runs everywhere; a real pair of folders runs where the
filesystem can hold one (Linux CI), and is skipped where it cannot.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

UPPER = ("Band Alfa", "Live")
LOWER = ("band alfa", "Live")


class Case(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.library = os.path.join(self.tree.root, "library")
        os.makedirs(self.library)
        self.set_config(FILE_DIRECTORY=self.library, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="alfa", NICKNAME="alfa", ORIGINAL_NICK="alfa",
                        RAR_ENABLED=True)

    def build(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(), out.getvalue())
        with io.open(list_mod.find_latest_list(), encoding="utf-8") as handle:
            return handle.read().splitlines()

    def blocks(self, lines):
        """[(heading, summary line, [rows])] in list order."""
        blocks = []
        for index, line in enumerate(lines):
            if line.startswith("D:\\MEDIA\\"):
                blocks.append((line, lines[index + 2], []))
            elif line.startswith("!alfa ") and blocks:
                blocks[-1][2].append(line)
        return blocks

    def assert_two_blocks(self, lines):
        blocks = self.blocks(lines)
        self.assertEqual(len(blocks), 2, [heading for heading, _s, _r in blocks])
        self.assertNotEqual(blocks[0][0], blocks[1][0])
        for heading, summary, rows in blocks:
            with self.subTest(heading=heading):
                self.assertTrue(summary.strip().startswith("3 files"), summary)
                self.assertEqual(len(rows), 3)
                # Each folder's own files, and only them.
                tag = "a" if "Band Alfa" in heading else "b"
                self.assertTrue(all(row.split()[2] == f"{tag}.flac" for row in rows), rows)


class AStandInWalk(Case):
    """The two folders handed over by a stand-in for walk_with_sizes(), so
    this runs on a filesystem that could never hold them."""

    def test_each_folder_is_one_block(self):
        sep = os.sep

        def walk(top, onerror=None, workers=None):
            yield top + sep + sep.join(UPPER), [(f"0{i} a.flac", 100) for i in range(1, 4)]
            yield top + sep + sep.join(LOWER), [(f"0{i} b.flac", 100) for i in range(1, 4)]

        self.addCleanup(setattr, update_list, "walk_with_sizes", update_list.walk_with_sizes)
        update_list.walk_with_sizes = walk

        self.assert_two_blocks(self.build())

    def test_the_order_is_still_case_insensitive(self):
        rows = [("lib/b", "x", 1), ("lib/A", "y", 1), ("lib/a", "z", 1), ("lib/B", "w", 1)]

        ordered = sorted(rows, key=update_list._row_order)

        self.assertEqual([folder for folder, _n, _s in ordered],
                         ["lib/A", "lib/a", "lib/B", "lib/b"])


class RealFolders(Case):

    def test_each_folder_is_one_block(self):
        upper = os.path.join(self.library, *UPPER)
        lower = os.path.join(self.library, *LOWER)
        os.makedirs(upper)
        try:
            os.makedirs(lower)
        except FileExistsError:
            self.skipTest("this filesystem does not tell names apart by case")
        if len(os.listdir(self.library)) != 2:
            self.skipTest("this filesystem does not tell names apart by case")
        for folder, tag in ((upper, "a"), (lower, "b")):
            for index in range(1, 4):
                with open(os.path.join(folder, f"0{index} {tag}.flac"), "wb") as handle:
                    handle.write(b"\0" * 100)

        self.assert_two_blocks(self.build())


if __name__ == "__main__":
    unittest.main()
