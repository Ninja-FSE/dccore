"""Building one list from several folders (#164, step 3).

The first step where published output changes: every folder heading now leads
with its folder's label, so `D:\\MUSIC\\Artist\\Album\\` becomes
`D:\\MUSIC\\Flac\\Artist\\Album\\`.

Labelled for a SINGLE folder too, which is the decision worth restating here
because it looks like the more disruptive option and is not. Labelling only at
two or more would mean an operator who serves for weeks and then adds a second
folder changes every path anyone already saved - a second break, landing on
people who had no idea anything changed. Once, at the upgrade, is one.

What does not change: a file request carries a filename, not a path
(dcc.py:1132), so every saved file request and every AutoQ queue entry built
from a file row is untouched. Only `!rar` folder requests carry a path, and
resolution still accepts the unlabelled form - see
test_multi_folder_resolution.py.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ScanCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "folders.json")
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        LIBRARY_FOLDERS_FILE=self.store,
                        LIST_BASE_NAME="Bot", NICKNAME="Bot",
                        ORIGINAL_NICK="Bot", LIST_FORMAT="txt",
                        RAR_ENABLED=True)

    def add(self, root_name, relative, data=b"\x00" * 2048):
        path = os.path.join(self.tree.root, root_name, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as handle:
            handle.write(data)
        return path

    def configure(self, *names):
        for name in names:
            os.makedirs(os.path.join(self.tree.root, name), exist_ok=True)
        entries = [library.Folder(name, os.path.join(self.tree.root, name))
                   for name in names]
        library.save_folders(entries)
        self.set_config(FILE_DIRECTORY=entries[0].path)
        return entries

    def build(self):
        self.assertTrue(update_list.generate_master_list(), "the scan failed")
        with io.open(list_mod.find_latest_list(), encoding="utf-8") as handle:
            return handle.read()

    def rar_body(self):
        name = next(n for n in os.listdir(self.tree.lists) if "-RAR-" in n)
        with io.open(os.path.join(self.tree.lists, name), encoding="utf-8") as handle:
            return handle.read()


class EveryFolderContributes(ScanCase):

    def setUp(self):
        super().setUp()
        self.configure("Flac", "Mp3")
        self.add("Flac", "Pink Floyd/Animals/Dogs.flac")
        self.add("Mp3", "Pink Floyd/Animals/Dogs.mp3")
        self.add("Mp3", "Nirvana/Nevermind/Lithium.mp3")

    def test_files_from_both_folders_are_listed(self):
        body = self.build()

        self.assertIn("Dogs.flac", body)
        self.assertIn("Dogs.mp3", body)
        self.assertIn("Lithium.mp3", body)

    def test_the_same_album_in_two_folders_gets_two_headings(self):
        """The reason for labelling at all. Both folders hold
        Pink Floyd/Animals; unlabelled, the two headings would be identical
        text and nothing could tell them apart on the way back."""
        body = self.build()

        self.assertIn("D:\\MEDIA\\Flac\\Pink Floyd\\Animals\\", body)
        self.assertIn("D:\\MEDIA\\Mp3\\Pink Floyd\\Animals\\", body)

    def test_the_count_covers_every_folder(self):
        self.build()

        self.assertEqual(commands.count_from_master_list(), 3)

    def test_the_rar_list_labels_its_rows_too(self):
        self.build()
        rows = [l for l in self.rar_body().split("\n") if l.startswith("!")]

        self.assertIn("!Bot !rar D:\\MEDIA\\Flac\\Pink Floyd\\Animals\\", rows)
        self.assertIn("!Bot !rar D:\\MEDIA\\Mp3\\Pink Floyd\\Animals\\", rows)

    def test_every_written_row_resolves_back_to_a_real_folder(self):
        """The round trip, which is the property that actually matters: a row
        the writer produced has to be one resolution can turn back into the
        directory it came from. Asserted against the rows themselves rather
        than against strings retyped here."""
        self.build()
        rows = [l for l in self.rar_body().split("\n") if l.startswith("!")]
        self.assertTrue(rows, "no !rar rows were written")

        for row in rows:
            heading = row.split(" !rar ", 1)[1]
            path, folder = list_mod.resolve_list_folder_with_root(heading)

            self.assertIsNotNone(folder, f"{heading} resolved to no folder")
            self.assertTrue(os.path.isdir(path),
                            f"{heading} resolved to {path}, which is not there")


class OneFolderIsLabelledToo(ScanCase):
    """The decision that looks disruptive and is the lesser of two breaks."""

    def setUp(self):
        super().setUp()
        self.configure("Library")
        self.add("Library", "Artist/Album/track.flac")

    def test_the_single_folders_label_leads_the_path(self):
        body = self.build()

        self.assertIn("D:\\MEDIA\\Library\\Artist\\Album\\", body)

    def test_it_still_resolves(self):
        self.build()
        path, folder = list_mod.resolve_list_folder_with_root(
            "D:\\MUSIC\\Library\\Artist\\Album\\")

        self.assertEqual(folder.name, "Library")
        self.assertTrue(os.path.isdir(path))


class AMissingFolderCostsOnlyItself(ScanCase):
    """An unplugged drive or an unmounted share should cost its own contents,
    not take the bot off the air.

    Deliberately different from a subtree failing DURING a walk of a folder
    that was present: that keeps the previous index rather than publishing a
    truncated one, because it is a systemic failure of a library we are
    supposed to be reading.
    """

    def setUp(self):
        super().setUp()
        self.configure("Present", "Gone")
        self.add("Present", "Artist/Album/here.flac")
        os.rmdir(os.path.join(self.tree.root, "Gone"))

    def test_the_list_is_built_from_what_is_there(self):
        body = self.build()

        self.assertIn("here.flac", body)
        self.assertIn("D:\\MEDIA\\Present\\Artist\\Album\\", body)

    def test_the_missing_folder_is_named_in_the_log(self):
        """Silently shrinking is the failure mode to avoid: an operator whose
        drive dropped should be told which folder, not left to notice the file
        count fell."""
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            update_list.generate_master_list()
        output = buffer.getvalue()

        self.assertIn("Gone", output)
        self.assertIn("not available", output)

    def test_nothing_from_the_missing_folder_is_listed(self):
        body = self.build()

        self.assertNotIn("D:\\MEDIA\\Gone\\", body)


class TheOrderIsTheOperators(ScanCase):

    def test_folders_are_scanned_in_configured_order(self):
        """The order decides the build order and the tie-break, so it cannot
        be sorted. Configured Z-then-A, the log must say Z first."""
        import contextlib

        self.configure("Zed", "Alpha")
        self.add("Zed", "A/B/z.flac")
        self.add("Alpha", "A/B/a.flac")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            update_list.generate_master_list()
        line = next(l for l in buffer.getvalue().split("\n") if "Scanning" in l)

        self.assertLess(line.index("(Zed)"), line.index("(Alpha)"))


if __name__ == "__main__":
    unittest.main()
