"""@find matches a name whichever Unicode form it was written in (#1270).

A library copied from a Mac names an accented letter as a base letter plus a
combining accent (NFD); an IRC client sends the precomposed letter (NFC). The
two look identical and share no substring, so "@find Renée" answered 0 for a
file that was in the list - nothing anywhere normalised either side.

Only the COMPARISON is normalised. The list keeps the bytes the filesystem
gave it, because a pasted request has to match the file on disk.
"""

import contextlib
import io
import os
import sys
import unicodedata
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

NAME = "Renée Fictive - Été Nocturne.flac"
NFC_NAME = unicodedata.normalize("NFC", NAME)
NFD_NAME = unicodedata.normalize("NFD", NAME)


def search(term, path):
    return list_mod.find_matching_entries(list_mod.split_search_term(term),
                                          list_path=path)[1]


class AListWrittenInEitherForm(DCCoreTestCase):
    """The list file written directly, so no filesystem's own handling of
    names comes into it: this runs the same everywhere."""

    def write_list(self, name):
        root = self.make_tree().root
        path = os.path.join(root, "alfa-2026-10-01.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("List of 1 Files\n\n" + "=" * 20 + "\nD:\\MEDIA\\Pop\\\n"
                         + "=" * 20 + "\n")
            handle.write(f"!alfa {name}  ::INFO:: 1.0MB\n")
        return path

    def test_the_forms_differ_to_begin_with(self):
        """Guard on the guard: otherwise every test here passes vacuously."""
        self.assertNotEqual(NFC_NAME, NFD_NAME)
        self.assertNotIn("renée", NFD_NAME.lower())

    def test_every_form_of_the_term_finds_every_form_of_the_row(self):
        for row_form in ("NFC", "NFD"):
            path = self.write_list(unicodedata.normalize(row_form, NAME))
            for term_form in ("NFC", "NFD"):
                for term in ("Renée", "été nocturne", '"Renée Fictive"'):
                    with self.subTest(row=row_form, term=term_form, words=term):
                        self.assertEqual(
                            search(unicodedata.normalize(term_form, term), path), 1)

    def test_the_row_handed_back_is_the_list_s_own_bytes(self):
        path = self.write_list(NFD_NAME)

        entries, _total = list_mod.find_matching_entries(
            list_mod.split_search_term(unicodedata.normalize("NFC", "Renée")),
            list_path=path)

        self.assertEqual(entries[0]["filename"], NFD_NAME)

    def test_a_word_not_in_the_name_still_misses(self):
        path = self.write_list(NFD_NAME)
        self.assertEqual(search("Renee", path), 0)


class AListBuiltFromAnNfdNamedFile(DCCoreTestCase):
    """The reported case end to end, where the filesystem keeps NFD names
    (NTFS and ext4 do; a filesystem that normalises on write is skipped, and
    the class above covers it)."""

    def test_it_is_found_and_listed_unchanged(self):
        tree = self.make_tree()
        album = os.path.join(tree.music, "Pop", "Album")
        os.makedirs(album)
        with open(os.path.join(album, NFD_NAME), "wb") as handle:
            handle.write(b"\0" * 100)
        if NFD_NAME not in os.listdir(album):
            self.skipTest("this filesystem normalises names on write")
        self.set_config(FILE_DIRECTORY=tree.music, LOCAL_LIST_DIR=tree.lists,
                        LIST_BASE_NAME="alfa", NICKNAME="alfa")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(update_list.generate_master_list())
        path = list_mod.find_latest_list()

        self.assertEqual(search(unicodedata.normalize("NFC", "Renée"), path), 1)
        with io.open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(NFD_NAME, text)
        self.assertNotIn(NFC_NAME, text)


if __name__ == "__main__":
    unittest.main()
