"""library.py - which folders this bot serves, and in what order.

Step 1 of #164. Nothing here changes what the daemon does yet: with no folder
file on disk, which is every install today, folders() returns one entry built
from FILE_DIRECTORY and every caller sees exactly what it saw before.

So the tests that matter most are the ones pinning that equivalence, and the
validation rules - because those are what an operator meets when they first
configure two folders, and a rule that fires on the wrong thing is worse than
no rule.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import library  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class LibraryCase(DCCoreTestCase):
    """A scratch tree, and a folder file that does not exist yet."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "library_folders.json")
        self.set_config(LIBRARY_FOLDERS_FILE=self.store,
                        FILE_DIRECTORY=self.tree.music)

    def make_dir(self, *parts):
        path = os.path.join(self.tree.root, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def write_store(self, entries):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)


class WithNoFileNothingChanges(LibraryCase):
    """The property the whole step rests on. Every install today has no folder
    file, and must behave exactly as it did before this module existed."""

    def test_one_folder_taken_from_file_directory(self):
        found = library.folders()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].path, self.tree.music)

    def test_it_is_labelled_by_its_own_basename(self):
        found = library.folders()

        self.assertEqual(found[0].name, os.path.basename(self.tree.music))

    def test_an_unset_library_gives_no_folders(self):
        """FILE_DIRECTORY is deliberately allowed to be blank until the
        dashboard sets it, so "none configured" is a real state and must not
        raise or invent a path."""
        self.set_config(FILE_DIRECTORY=None)

        self.assertEqual(library.folders(), [])

    def test_folder_paths_agrees_with_folders(self):
        self.assertEqual(library.folder_paths(),
                         [entry.path for entry in library.folders()])


class WithAFileTheFileWins(LibraryCase):

    def test_the_stored_order_is_preserved(self):
        """Order is the operator's, and it decides both the build order and
        which folder wins a tie - so it cannot be sorted or set-ified."""
        first = self.make_dir("Zed")
        second = self.make_dir("Alpha")
        self.write_store([{"name": "Zed", "path": first},
                          {"name": "Alpha", "path": second}])

        found = library.folders()

        self.assertEqual([e.name for e in found], ["Zed", "Alpha"])

    def test_a_missing_label_is_filled_in_from_the_path(self):
        path = self.make_dir("Flac")
        self.write_store([{"path": path}])

        self.assertEqual(library.folders()[0].name, "Flac")

    def test_an_unreadable_file_falls_back_rather_than_serving_nothing(self):
        """None, not [], on any read problem. [] would read as "the operator
        configured no folders" and serve nothing; the truth is "we could not
        read the file", and the answer is the same single folder as a fresh
        install."""
        with io.open(self.store, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")

        self.assertIsNone(library.load_folders())
        self.assertEqual(library.folders()[0].path, self.tree.music)

    def test_a_json_object_instead_of_a_list_falls_back(self):
        self.write_store({"name": "Flac", "path": "D:/Flac"})

        self.assertIsNone(library.load_folders())

    def test_an_empty_list_falls_back(self):
        self.write_store([])

        self.assertIsNone(library.load_folders())


class TheNestingRule(LibraryCase):
    """No folder may sit inside another. Both would list every file under the
    inner one twice."""

    def test_the_separator_boundary_trap(self):
        """The reason is_inside() compares per separator rather than with a
        plain startswith: these two share a string prefix and neither contains
        the other. dcc.is_safe_path() documents the same trap."""
        self.assertFalse(library.is_inside("/srv/library", "/srv/library-backup"))
        self.assertTrue(library.is_inside("/srv/library", "/srv/library/sub"))
        self.assertTrue(library.is_inside("/srv/library", "/srv/library"))

    def test_a_child_added_after_its_parent_is_refused(self):
        parent = self.make_dir("Music")
        child = self.make_dir("Music", "Flac")
        found = library.problems([library.Folder("Music", parent),
                                  library.Folder("Flac", child)])

        self.assertTrue(found)
        self.assertIn("sits inside", " ".join(found))

    def test_a_parent_added_after_its_child_is_refused(self):
        """The other direction. A rule that only checked one would let the
        same overlap through depending on the order they were added."""
        parent = self.make_dir("Music")
        child = self.make_dir("Music", "Flac")
        found = library.problems([library.Folder("Flac", child),
                                  library.Folder("Music", parent)])

        self.assertTrue(found)
        self.assertIn("contains", " ".join(found))

    def test_siblings_are_fine(self):
        """Control: the rule must refuse overlap, not adjacency."""
        found = library.problems([
            library.Folder("Flac", self.make_dir("Flac")),
            library.Folder("Mp3", self.make_dir("Mp3"))])

        self.assertEqual(found, [])


class TheOtherRules(LibraryCase):

    def test_a_duplicate_path_is_refused(self):
        path = self.make_dir("Flac")
        found = library.problems([library.Folder("A", path),
                                  library.Folder("B", path)])

        self.assertTrue(any("already listed" in line for line in found))

    def test_two_folders_cannot_share_a_label(self):
        """The label is what tells them apart in the list. Two the same would
        make a path ambiguous in exactly the way labelling exists to prevent."""
        found = library.problems([
            library.Folder("Flac", self.make_dir("One")),
            library.Folder("flac", self.make_dir("Two"))])

        self.assertTrue(any("already used by" in line for line in found))

    def test_a_label_cannot_contain_a_separator(self):
        """It becomes a path component users copy back, so it has to be one.

        Both separators and the drive form are refused on EVERY platform, not
        by the host's own rules. On Linux a backslash is an ordinary filename
        character and "C:" an ordinary directory name, so a label made there
        would pass os.path.splitdrive() and then be read as a drive specifier
        by every Windows client that pastes the path back. That is what the
        first version of this got wrong, and Linux CI caught it.
        """
        for bad in ("a/b", "a\\b", "..", "C:", "D:Music"):
            with self.subTest(label=bad):
                found = library.problems(
                    [library.Folder(bad, self.make_dir("Real"))])

                self.assertTrue(any("single folder name" in line
                                    for line in found), f"{bad!r} was allowed")

    def test_a_blank_label_is_refused(self):
        found = library.problems([library.Folder("", self.make_dir("Real"))])

        self.assertTrue(any("no label" in line for line in found))

    def test_a_path_that_is_not_a_folder_is_refused(self):
        missing = os.path.join(self.tree.root, "not-there")
        found = library.problems([library.Folder("Gone", missing)])

        self.assertTrue(any("not a folder" in line for line in found))

    def test_a_good_set_has_no_problems(self):
        """Control. Every test above would pass on an implementation that
        called everything a problem."""
        found = library.problems([
            library.Folder("Flac", self.make_dir("Flac")),
            library.Folder("Mp3", self.make_dir("Mp3"))])

        self.assertEqual(found, [])

    def test_every_problem_is_reported_not_just_the_first(self):
        """An operator fixing three things should be told about three."""
        found = library.problems([
            library.Folder("", os.path.join(self.tree.root, "missing-one")),
            library.Folder("a/b", os.path.join(self.tree.root, "missing-two"))])

        self.assertGreaterEqual(len(found), 3)


class SavingTheFolderList(LibraryCase):

    def test_a_saved_list_reads_back_identically(self):
        entries = [library.Folder("Flac", self.make_dir("Flac")),
                   library.Folder("Mp3", self.make_dir("Mp3"))]

        library.save_folders(entries)

        self.assertEqual(library.load_folders(), entries)

    def test_saving_creates_the_directory(self):
        nested = os.path.join(self.tree.root, "deep", "folders.json")
        self.set_config(LIBRARY_FOLDERS_FILE=nested)

        library.save_folders([library.Folder("Flac", self.make_dir("Flac"))])

        self.assertTrue(os.path.exists(nested))

    def test_an_invalid_set_is_refused_and_nothing_is_written(self):
        parent = self.make_dir("Music")
        child = self.make_dir("Music", "Flac")

        with self.assertRaises(ValueError):
            library.save_folders([library.Folder("Music", parent),
                                  library.Folder("Flac", child)])

        self.assertFalse(os.path.exists(self.store),
                         "a refused save must leave no file behind")

    def test_the_refusal_names_every_problem(self):
        with self.assertRaises(ValueError) as caught:
            library.save_folders([
                library.Folder("", os.path.join(self.tree.root, "gone-one")),
                library.Folder("x/y", os.path.join(self.tree.root, "gone-two"))])

        self.assertGreaterEqual(len(str(caught.exception).splitlines()), 3)

    def test_no_temporary_file_is_left_behind(self):
        """The write goes via a temp file and a rename. A refused or failed
        save must not leave .library-*.tmp litter in the data directory."""
        library.save_folders([library.Folder("Flac", self.make_dir("Flac"))])

        leftovers = [n for n in os.listdir(os.path.dirname(self.store))
                     if n.startswith(".library-")]

        self.assertEqual(leftovers, [])


class LabelsAndLookup(LibraryCase):

    def test_default_label_is_the_basename(self):
        self.assertEqual(library.default_label(os.path.join("D:", "Flac")), "Flac")

    def test_default_label_ignores_a_trailing_separator(self):
        """Built with os.sep rather than written as a Windows literal.

        A backslash is a legal filename CHARACTER on POSIX, not a separator,
        so "D:\\Flac\\" is one folder genuinely called that on Linux and
        default_label() returning the whole string there is correct. The
        earlier version of this test hardcoded the Windows form and failed on
        the Linux half of CI - the assumption was the test's, not the code's.
        """
        path = os.path.join("somewhere", "Flac") + os.sep

        self.assertEqual(library.default_label(path), "Flac")

    def test_a_drive_root_still_gets_a_label(self):
        """An empty label would produce a doubled separator in every path
        built from it, so a path with no basename must not yield one."""
        self.assertTrue(library.default_label("D:\\"))
        self.assertTrue(library.default_label("/"))

    def test_folder_for_label_is_case_insensitive(self):
        """The label is written into paths users copy, paste and retype, so a
        request differing only in case is the same request."""
        path = self.make_dir("Flac")
        self.write_store([{"name": "Flac", "path": path}])

        self.assertIsNotNone(library.folder_for_label("flac"))
        self.assertEqual(library.folder_for_label("FLAC").path, path)

    def test_an_unknown_label_is_none(self):
        """What tells a labelled path from a legacy one at resolution time."""
        self.assertIsNone(library.folder_for_label("nosuchlabel"))
        self.assertIsNone(library.folder_for_label(""))

    def test_the_folders_file_follows_config(self):
        """Resolved per call, not baked in at import: !rehash reloads config,
        and a path captured at import would keep pointing at the old one."""
        self.set_config(LIBRARY_FOLDERS_FILE="somewhere/else.json")

        self.assertEqual(library.folders_file(), "somewhere/else.json")


if __name__ == "__main__":
    unittest.main()
