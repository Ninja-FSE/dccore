"""The album list follows RAR_ENABLED.

Raised on #140 and left as a follow-up there. `RAR_ENABLED = False` refuses
every `!rar` request, but the list builder went on producing the album list and
shipping it inside the zip every user downloads - a file whose every line is an
instruction to use a command this bot will refuse. Somebody pastes one, gets
"Folder packing (!rar) is disabled on this bot", and reasonably concludes the
bot is broken rather than that the feature is off.

The gate was never wrong. It just was not the only thing that had to know.
"""

import io
import os
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ListBuildCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest")

    def build(self, rar_enabled):
        self.set_config(RAR_ENABLED=rar_enabled)
        buffer = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buffer):
            built = update_list.generate_master_list()
        self.assertTrue(built, "the list build failed:\n" + buffer.getvalue())
        return buffer.getvalue()

    def lists_dir(self):
        return sorted(os.listdir(self.tree.lists))

    def album_lists(self):
        return [name for name in self.lists_dir()
                if name.startswith(config.LIST_BASE_NAME + "-RAR-")
                and name.endswith(".txt")]

    def zip_contents(self):
        zips = [name for name in self.lists_dir() if name.endswith(".zip")]
        self.assertEqual(len(zips), 1, "expected exactly one zip: %s" % self.lists_dir())
        with zipfile.ZipFile(os.path.join(self.tree.lists, zips[0])) as archive:
            return sorted(archive.namelist())


class WithFolderPackingOn(ListBuildCase):
    """Unchanged from before this fix - the whole point is that the default
    behaviour is the behaviour that already existed."""

    def test_the_album_list_is_built(self):
        self.build(rar_enabled=True)

        self.assertEqual(len(self.album_lists()), 1)

    def test_the_zip_carries_both_lists(self):
        self.build(rar_enabled=True)

        names = self.zip_contents()
        self.assertEqual(len(names), 2, names)
        self.assertTrue(any("-RAR-" in name for name in names), names)

    def test_the_album_list_tells_people_how_to_ask(self):
        self.build(rar_enabled=True)

        with io.open(os.path.join(self.tree.lists, self.album_lists()[0]),
                     encoding="utf-8") as handle:
            body = handle.read()

        self.assertIn("!rar", body)


class WithFolderPackingOff(ListBuildCase):

    def test_the_zip_a_user_downloads_carries_no_album_list(self):
        """The one that matters. This is the file people actually receive."""
        self.build(rar_enabled=False)

        names = self.zip_contents()
        self.assertEqual([name for name in names if "-RAR-" in name], [],
                         "the bot handed out album instructions it will refuse")
        self.assertEqual(len(names), 1, names)

    def test_no_album_list_is_left_in_the_lists_directory(self):
        """Not published and not left behind. An empty album list reads as
        "this bot offers no albums" to anything counting the file, which is a
        different claim from "it does not offer them at all"."""
        self.build(rar_enabled=False)

        self.assertEqual(self.album_lists(), [])

    def test_the_file_list_is_untouched(self):
        """Only the album half is affected. A bot with folder packing off is
        still a file server, and this is the half it serves."""
        self.build(rar_enabled=False)

        # By the list's own prefix: LOCAL_LIST_DIR also holds the two side
        # files (dccore.size.txt and dccore.rawbytes.txt), which are .txt too.
        text_lists = [name for name in self.lists_dir()
                      if name.startswith(config.LIST_BASE_NAME + "-")
                      and name.endswith(".txt") and "-RAR-" not in name]
        self.assertEqual(len(text_lists), 1, self.lists_dir())
        with io.open(os.path.join(self.tree.lists, text_lists[0]),
                     encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("To request a file", body)

    def test_no_half_written_temporary_is_left_behind_either(self):
        """The album list is still opened for writing - it is simply left
        empty - so the ".new" temporary exists whether or not anything went
        into it. _prune_superseded_lists() does not reach it: it only removes
        published names, not temporaries, so this has to be cleaned up where
        the decision not to publish is made."""
        self.build(rar_enabled=False)

        leftovers = [name for name in self.lists_dir() if name.endswith(".new")]

        self.assertEqual(leftovers, [], "a temporary list was left in lists/")

    def test_it_says_so_rather_than_skipping_silently(self):
        """An operator reading the build output should see why the album list
        is missing, not wonder whether the build half-failed."""
        output = self.build(rar_enabled=False)

        self.assertIn("RAR_ENABLED is off", output)


class SwitchingItOffLater(ListBuildCase):
    """The realistic sequence: a bot that has been serving albums, whose
    operator turns folder packing off and rebuilds."""

    def test_the_previous_album_list_does_not_survive_the_rebuild(self):
        self.build(rar_enabled=True)
        self.assertEqual(len(self.album_lists()), 1, "setup did not build one")

        self.build(rar_enabled=False)

        self.assertEqual(self.album_lists(), [],
                         "yesterday's album list is still being handed out")

    def test_and_switching_it_back_on_restores_it(self):
        self.build(rar_enabled=False)

        self.build(rar_enabled=True)

        self.assertEqual(len(self.album_lists()), 1)
        self.assertTrue(any("-RAR-" in name for name in self.zip_contents()))


if __name__ == "__main__":
    unittest.main()
