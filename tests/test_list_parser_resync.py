"""One odd folder name made every file after it invisible.

WHAT WENT WRONG

find_matching_entries() walks the master list with a three-state machine,
because the format wraps each folder heading in a pair of rule lines:

    =====================
    D:\\MUSIC\\Some Folder\\
    =====================
    !bot Song.flac 10M

A rule line is "every character is =". So a folder whose NAME is also all "="
- "====" - reads as a second rule line. The state machine treats that as a
malformed doubled rule and keeps waiting for a heading, and the next line it
gets is a FILE line, which it takes as the heading instead.

That loses the file, and it shifts every heading after it by one, so the rest
of the list is mis-attributed or swallowed too. Measured on a three-folder
list with one bad name in the middle: searching found ONE of the three files.
The third was in a perfectly ordinary folder.

Nothing failed. The list on disk was complete, the files were still served
correctly when requested by name, and only search and the dashboard's File
Lists view were wrong.

HOW IT IS REACHED

Not through our own list: update_list.py writes every heading as
"D:\\MUSIC\\<folder>\\", which is neither all "=" nor starts with "!".

Through a FETCHED one. list_fetch.py runs this same parser over a list
another bot wrote and sent us, and that bot's folder names are not ours to
choose.

THE FIX

A line starting with "!" is a file line, whatever the state machine was
expecting. Checking that before consuming it as a heading means the parser
resynchronises instead of swallowing content, and a malformed heading costs
only its own folder attribution.
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

import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AMalformedHeadingCostsOnlyItsOwnFolder(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root

    def write_list(self, text):
        path = os.path.join(self.root, "fetched_list.txt")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    THE_REPORTED_SHAPE = (
        "=====\nNormal Folder\n=====\n"
        "!bot Song One.flac 10M\n"
        "=====\n====\n=====\n"          # a folder named exactly "===="
        "!bot Song Two.flac 20M\n"
        "=====\nAfter Folder\n=====\n"
        "!bot Song Three.flac 30M\n"
    )

    def test_every_file_is_still_found(self):
        """The whole defect: two of these three used to be invisible."""
        path = self.write_list(self.THE_REPORTED_SHAPE)

        _entries, total = list_mod.find_matching_entries([], list_path=path)

        self.assertEqual(total, 3)

    def test_a_file_after_the_bad_heading_is_searchable(self):
        path = self.write_list(self.THE_REPORTED_SHAPE)

        for word in ("one", "two", "three"):
            with self.subTest(word=word):
                _entries, total = list_mod.find_matching_entries(
                    [word], list_path=path)

                self.assertEqual(total, 1)

    def test_folder_attribution_recovers_afterwards(self):
        """Resynchronising is only worth anything if the NEXT heading is read
        correctly - otherwise every folder after the bad one is still wrong,
        just wrong in a different way."""
        path = self.write_list(self.THE_REPORTED_SHAPE)

        entries, _total = list_mod.find_matching_entries([], list_path=path)
        by_name = {e["filename"].split(".flac")[0]: e["folder"] for e in entries}

        self.assertEqual(by_name["Song One"], "Normal Folder")
        self.assertEqual(by_name["Song Three"], "After Folder")

    def test_a_file_line_is_never_consumed_as_a_heading(self):
        """The property, stated directly. A heading that happens to look like
        a file is the shape that started this."""
        path = self.write_list(
            "=====\n"
            "!bot Not A Folder.flac 1M\n"
            "=====\n"
            "!bot Real File.flac 2M\n")

        entries, total = list_mod.find_matching_entries([], list_path=path)
        folders = {e["folder"] for e in entries}

        self.assertEqual(total, 2)
        for folder in folders:
            self.assertFalse(str(folder).startswith("!"),
                             f"a file line was taken as a folder heading: "
                             f"{folder!r}")

    def test_consecutive_bad_headings(self):
        path = self.write_list(
            "=====\n====\n=====\n===\n=====\n"
            "!bot Survivor.flac 5M\n")

        _entries, total = list_mod.find_matching_entries([], list_path=path)

        self.assertEqual(total, 1)

    def test_a_well_formed_list_is_unchanged(self):
        """Control. The fix must only affect malformed input."""
        path = self.write_list(
            "=====\nFolder A\n=====\n"
            "!bot One.flac 1M\n"
            "!bot Two.flac 2M\n"
            "=====\nFolder B\n=====\n"
            "!bot Three.flac 3M\n")

        entries, total = list_mod.find_matching_entries([], list_path=path)
        folders = [e["folder"] for e in entries]

        self.assertEqual(total, 3)
        self.assertEqual(folders, ["Folder A", "Folder A", "Folder B"])

    def test_a_folder_named_with_rule_characters_does_not_hide_its_own_files(self):
        """Its heading is unreadable, so its files inherit the previous folder
        - but they are still listed, which is the part that matters."""
        path = self.write_list(
            "=====\nGood Folder\n=====\n"
            "!bot Before.flac 1M\n"
            "=====\n=======\n=====\n"
            "!bot Inside The Odd Folder.flac 2M\n")

        _entries, total = list_mod.find_matching_entries(
            ["inside"], list_path=path)

        self.assertEqual(total, 1)


class OurOwnListCannotProduceThis(unittest.TestCase):
    """Recorded so the fix is not mistaken for one that only matters to us.
    update_list.py's heading is always prefixed, which is why this had to come
    from a fetched list."""

    def test_the_heading_prefix_is_neither_a_rule_nor_a_file_line(self):
        prefix = list_mod.LIST_FOLDER_PREFIX

        self.assertNotEqual(set(prefix), {"="})
        self.assertFalse(prefix.startswith("!"))

    def test_the_fetched_path_uses_the_same_parser(self):
        """If list_fetch stopped sharing this parser, this whole file would be
        guarding something no remote input can reach."""
        with io.open(os.path.join(REPO_ROOT, "list_fetch.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("find_matching_entries", source,
                      "list_fetch.py no longer parses fetched lists with "
                      "list.find_matching_entries()")


if __name__ == "__main__":
    unittest.main()
