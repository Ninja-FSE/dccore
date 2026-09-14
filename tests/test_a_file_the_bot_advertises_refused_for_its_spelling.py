"""#445 - the list lookup matches case-insensitively, then builds the path
from the one spelling known not to be the one on disk.

`list.find_duplicate_filenames()` says why the match is case-insensitive in
its own docstring: "a requester typing a name back cannot be expected to
reproduce its case". The path was then rebuilt as
`os.path.join(target_folder, requested_file)` - the REQUESTER's spelling,
not the list's.

On Linux that names a file that does not exist. The os.walk last resort is a
case-sensitive `if requested_file in files` and misses too, so the requester
is refused a file the bot is publicly advertising. On Windows it resolves,
and the file is then offered and received under the requester's casing rather
than the operator's - which is what goes out in the DCC SEND.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import dcc  # noqa: E402
import platform_compat  # noqa: E402

from tests.test_path_security import InlineThread, PathSecurityBase, quiet  # noqa: E402

ALBUM = "Alpha Album"
NAME = "Track 01.flac"
RETYPED = "track 01.FLAC"


class ARequestRetypedInAnotherCase(PathSecurityBase):
    """One album, one file, and a requester who typed the name back rather
    than copying it."""

    def setUp(self):
        super().setUp()
        directory = os.path.join(self.tree.music, ALBUM)
        os.makedirs(directory, exist_ok=True)
        self.served = os.path.join(directory, NAME)
        with io.open(self.served, "w", encoding="utf-8") as handle:
            handle.write(ALBUM)
        self.write_list()

    def write_list(self):
        """A master list in the shape update_list.py writes one: a folder
        heading between rule lines, then a row per file. The "D:\\MUSIC\\"
        prefix is the fixed heading the builder emits whatever the library's
        real path is."""
        rule = "=" * 53
        lines = ["List of files generated on Jan 1st\n", "\n",
                 "\n", rule + "\n",
                 "D:\\MUSIC\\%s\\\n" % ALBUM,
                 rule + "\n",
                 "!%s %s  ::INFO:: 1.0MB\n" % (config.NICKNAME, NAME)]
        path = os.path.join(self.tree.lists,
                            "%s-2026-01-01.txt" % config.LIST_BASE_NAME)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        return path

    def request(self, name):
        self.notices.clear()
        InlineThread.dispatched = []
        config.dcc_queue.clear()
        with quiet():
            dcc.handle_download_request(self.sock, "dave", name, "#dccore-test")
        return [kind for kind, _args in self.notices]

    def served_path(self):
        """The path the daemon settled on, whether it dispatched at once or
        queued the row - both carry the same resolved path."""
        for name, args in InlineThread.dispatched:
            if name == "start_dcc_send":
                return args[2]
        for rows in config.dcc_queue.values():
            for row in rows:
                if not row.get("is_temporary_zip"):
                    return row.get("path")
        return None

    # -- the behaviour ----------------------------------------------------

    def test_the_path_carries_the_list_spelling_not_the_requesters(self):
        """Runs everywhere, and on Windows it is the whole of the visible
        fault: the request resolves either way, but what goes out in the DCC
        SEND is the name the requester typed rather than the operator's."""
        self.request(RETYPED)

        settled = self.served_path()

        self.assertIsNotNone(settled, self.notices)
        self.assertEqual(os.path.basename(settled), NAME,
                         "the offer carries the requester's casing, not the "
                         "name the operator actually has on disk")

    def test_the_exact_name_is_unaffected(self):
        """The other half. Nearly every request is a copy-paste out of the
        list, and that path must not move."""
        self.request(NAME)

        self.assertEqual(os.path.basename(self.served_path() or ""), NAME)

    def test_a_request_carrying_a_size_hint_gets_the_same_treatment(self):
        """The other branch of the lookup. A request built from a search
        result carries its own "::INFO:: <size>" tail, and that branch settles
        on a different pair of variables - so it is a second place the
        requester's spelling could survive."""
        self.request(RETYPED + "  ::INFO:: 1.0MB")

        settled = self.served_path()

        self.assertIsNotNone(settled, self.notices)
        self.assertEqual(os.path.basename(settled), NAME)

    def test_a_name_that_is_in_no_list_is_still_refused(self):
        """Carrying the list's spelling forward must not turn the lookup into
        something that answers for names it never found."""
        kinds = self.request("Nothing Like This.flac")

        self.assertIn("error", kinds, kinds)


class OnAFilesystemThatTellsCaseApart(ARequestRetypedInAnotherCase):
    """The Linux half, driven on every platform.

    The refusal only happens where the filesystem is case-sensitive, and this
    machine may not be - Windows and macOS are not, and a test that skipped
    there would cover nothing on the box this is developed on. So the
    case-sensitivity is emulated rather than depended on: os.path.exists is
    replaced with one that checks each component against what the directory
    actually lists, which is what a case-sensitive filesystem does.

    Two things the emulation has to get right, and both were learned the hard
    way:

      * It must strip platform_compat.long_path()'s \\\\?\\ prefix before
        walking the path, or it answers about a path that does not parse -
        which is how a first attempt at this "passed" while testing nothing.
      * It must only case-check the components BELOW the fixture's own tree.
        Checking the whole path made it a question about the machine: a
        GitHub Windows runner's temp directory sits under an 8.3 short name
        (RUNNER~1) which no parent directory lists, so every path in the
        fixture "did not exist" and the class failed on CI while passing on
        a developer box. The library is what is being emulated, not the road
        to it.
    """

    def setUp(self):
        super().setUp()
        real_exists = os.path.exists
        self.addCleanup(setattr, os.path, "exists", real_exists)
        # Both spellings of the fixture's root. os.path.realpath() is NOT
        # applied to the path being TESTED: on Windows it resolves a path to
        # its real casing, which would hand back the very answer this is
        # trying to withhold.
        roots = [os.path.abspath(self.tree.root),
                 os.path.realpath(self.tree.root)]

        def without_long_prefix(text):
            for prefix in ("\\\\?\\UNC\\", "\\\\?\\"):
                if text.startswith(prefix):
                    return text[len(prefix):]
            return text

        def fixture_root_of(text):
            wanted = os.path.normcase(text)
            for base in roots:
                marked = os.path.normcase(base)
                if wanted == marked or wanted.startswith(marked + os.sep):
                    return base
            return None

        def case_sensitive_exists(path):
            text = os.path.abspath(without_long_prefix(str(path)))
            if not real_exists(text):
                return False
            base = fixture_root_of(text)
            if base is None:
                # Outside the library this is emulating - a module file, a
                # temp directory belonging to something else. Not its
                # business, and answering for it is what made the whole class
                # a question about the machine.
                return True
            head = base
            for part in os.path.relpath(text, base).split(os.sep):
                if part in ("", "."):
                    continue
                try:
                    if part not in os.listdir(head):
                        return False
                except OSError:
                    return False
                head = os.path.join(head, part)
            return True

        os.path.exists = case_sensitive_exists

    def test_the_emulation_actually_tells_the_two_names_apart(self):
        """Fixture invariant, and the discriminator for everything in this
        class: without it, every assertion below would pass on a filesystem
        that resolved the wrong-cased name by itself."""
        wrong = os.path.join(self.tree.music, ALBUM, RETYPED)

        self.assertTrue(os.path.exists(self.served))
        self.assertFalse(os.path.exists(wrong),
                         "this emulation is not emulating anything")
        self.assertTrue(os.path.exists(platform_compat.long_path(self.served)),
                        "the long_path prefix is not being stripped, so the "
                        "emulation answers about a path that does not parse")

    def test_a_parent_that_does_not_list_the_tree_changes_nothing(self):
        """The CI failure, reproduced portably.

        A GitHub Windows runner's temp directory sits under an 8.3 short name
        (RUNNER~1) which its own parent does not list. An emulation that
        case-checked every component up to the drive root therefore found
        every path in the fixture "missing", and this whole class failed there
        while passing on a developer box whose temp path has no short name.

        Making the tree's parent list nothing is the same condition, on any
        platform. The library is what is being emulated, not the road to it.
        """
        real_listdir = os.listdir
        self.addCleanup(setattr, os, "listdir", real_listdir)
        parent = os.path.normcase(os.path.dirname(os.path.abspath(self.tree.root)))

        def blind_above_the_tree(path):
            if os.path.normcase(os.path.abspath(path)) == parent:
                return []
            return real_listdir(path)

        os.listdir = blind_above_the_tree

        self.request(RETYPED)

        self.assertEqual(os.path.basename(self.served_path() or ""), NAME,
                         "the emulation is asking about directories above the "
                         "fixture, which is a question about the machine")

    def test_the_file_is_served_rather_than_refused(self):
        """The fault, on the platform that has it: the bot publicly lists the
        file and then tells the requester it does not exist."""
        kinds = self.request(RETYPED)

        self.assertNotIn("error", kinds,
                         "refused a file the bot is advertising, because the "
                         "requester did not reproduce its case")
        self.assertEqual(self.served_path(), self.served)

    def test_the_contents_that_go_out_are_the_real_file(self):
        """The path is not cosmetic - it is what gets read off disk."""
        self.request(RETYPED)

        with io.open(self.served_path(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), ALBUM)


class TheResolverSaysWhyItCarriesTheName(unittest.TestCase):

    def test_the_path_is_built_from_the_row_that_matched(self):
        """Read as the statement. The whole fault was one identifier - the
        requester's spelling where the list's belonged - so the assertion is
        about which name reaches os.path.join, not about a word appearing
        somewhere in dcc.py."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()
        code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())

        self.assertIn("test_path = os.path.join(target_folder, target_name)", code)
        self.assertNotIn("os.path.join(target_folder, requested_file)", code)
