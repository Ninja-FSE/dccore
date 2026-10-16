"""A name no published list places is not found, and the disk is not walked for it (#1376).

Found on a live bot whose one folder is a network share. A request for a name
its list did not have read every published list and then, as a last resort
from OmenServe, os.walk()ed every configured folder for it - with no time
limit. Over the share that ran for many minutes, holding one of the two scan
slots and the nick's turn, so each of their further requests was answered
"Busy looking up other files"; a minute after the walk ended, the next request
for the same name walked again.

The walk could only find a file that no list names, and nobody can have that
name from this bot: its list, its search and its adverts all come from the
lists. So the list is what the bot serves: a name it does not place is "File
not found" at once. And a file type the list leaves out (LIST_IGNORED_EXTENSIONS,
or a list's own exclusions, #1295) is not served by the two shortcuts that find
a file without reading the list either - the first folder's root and the folder
memory (#886) - since the walk was not the only way past the list.
"""

import contextlib
import inspect
import io
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import dcc  # noqa: E402
import library  # noqa: E402

from tests.test_an_unknown_filename_does_not_scan_the_library_unbounded import LookupBase  # noqa: E402


def no_walk(*args, **kwargs):
    raise AssertionError("the request walked the disk")


class NotInTheListIsNotFound(LookupBase):

    def setUp(self):
        super().setUp()
        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        self.out = io.StringIO()

    def ask(self, name, user="dave"):
        with mock.patch.object(dcc.os, "walk", no_walk), contextlib.redirect_stdout(self.out):
            dcc.handle_download_request(self.sock, user, name, "#dccore-test")

    def put(self, folder, name):
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(b"\x00" * 2048)
        return path

    def test_a_name_in_no_list_is_not_found_without_a_walk(self):
        self.ask("Nothing By This Name.flac")
        self.assertEqual(self.errors(), ["file_not_found"])
        self.assertEqual(len(self.scans), 1, "the lists were read")

    def test_a_file_on_disk_that_no_list_names_is_not_found(self):
        self.put(self.tree.album, "Added Since The Rebuild.flac")
        self.ask("Added Since The Rebuild.flac")
        self.assertEqual(self.errors(), ["file_not_found"])

    def test_a_listed_file_is_still_found(self):
        self.ask(os.path.basename(self.tree.tracks[0]))
        self.assertEqual(self.errors(), [])
        self.assertIn("sending", [kind for kind, _args in self.notices])

    def test_the_lookup_holds_no_walk(self):
        """The statement, not a comment that names it."""
        code = [line for line in inspect.getsource(dcc.handle_download_request).splitlines()
                if not line.strip().startswith("#")]
        self.assertFalse([line for line in code if "walk(" in line])


class WhatTheListLeavesOutIsNotServed(NotInTheListIsNotFound):

    def test_in_the_first_folders_root(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=[".db"])
        self.put(self.tree.music, "notes.db")
        self.ask("notes.db")
        self.assertEqual(self.errors(), ["file_not_found"])
        self.assertIn("refused: the list leaves its file type out", self.out.getvalue())

    def test_beside_a_file_a_recent_lookup_found(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=["DB"])     # as an operator may type it
        self.put(self.tree.album, "folder.db")
        self.ask(os.path.basename(self.tree.tracks[0]))
        self.assertEqual(self.errors(), [])
        self.ask("folder.db")
        self.assertEqual(self.errors(), ["file_not_found"])
        self.assertEqual(len(self.scans), 1, "found by the folder memory, not the lists")

    def test_a_lists_own_exclusions_count(self):
        self.put(self.tree.music, "release.nfo")
        with mock.patch.object(library, "list_exclusions", lambda name=None: (".nfo",)):
            self.ask("release.nfo")
        self.assertEqual(self.errors(), ["file_not_found"])

    def test_a_type_nothing_leaves_out_is_served_from_the_root(self):
        """The control: the same request, nothing excluded."""
        self.put(self.tree.music, "notes.db")
        self.ask("notes.db")
        self.assertEqual(self.errors(), [])

    def test_the_list_archive_itself_is_served_whatever_is_left_out(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=[".zip", ".txt"])
        name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        with open(os.path.join(self.tree.lists, name), "wb") as handle:
            handle.write(b"not a real zip, just bytes")
        self.ask(name)
        self.assertEqual(self.errors(), [])
        self.assertIn("sending", [kind for kind, _args in self.notices])


class TheDocsSayIt(unittest.TestCase):

    def test_the_install_guide_says_the_list_is_what_is_served(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "INSTALL.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue("**The list is what the bot serves** (#1376)" in text)


if __name__ == "__main__":
    unittest.main()
