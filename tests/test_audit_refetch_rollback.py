"""A rejected re-fetch must not destroy the list we already hold.

`_extract_and_locate_list_file()` wipes the extraction directory as its FIRST
action - and that directory is not scratch space. It is
`<FETCHED_FILES_DIR>/lists/<bot>/`, where the list currently being served
from lives.

Every validation happens after the wipe: the byte cap, `is_zipfile()`, the
member checks, the plausible-list sniff, the extracted-text line ceiling. So a
re-fetch that turned out to be a RAR archive, an oversized zip, or a peer's
error page destroyed a perfectly good list on its way to rejecting the
replacement.

`refetch_due_lists()` runs unattended on a timer, so nobody is watching when
it happens: the operator simply finds a list they had yesterday gone today,
with the only trace a rejection line in the log.

The fix is the shape this project already uses to publish its own list -
update_list.py writes `final + ".new"` and swaps - applied to the receiving
side: hold the existing copy aside, do the work, and put it back if the work
was rejected.
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
import list_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "PeerBot"


class ARejectedRefetchKeepsTheListWeHave(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FETCHED_FILES_DIR=os.path.join(self.tree.root, "fetched"),
                        fetched_bot_lists={})
        self.held_dir = list_fetch.list_extract_dir(BOT)
        os.makedirs(self.held_dir, exist_ok=True)
        self.held_file = os.path.join(self.held_dir, "PeerBot-2026-08-30.txt")
        with io.open(self.held_file, "w", encoding="utf-8") as handle:
            handle.write("List of files\n\n" + "=" * 40 + "\n"
                         "D:\\MEDIA\\Albums\\\n" + "=" * 40 + "\n"
                         "!PeerBot Held Track.mp3  ::INFO:: 4.00MB\n")

    def incoming(self, name, payload):
        path = os.path.join(self.tree.root, name)
        with io.open(path, "wb") as handle:
            handle.write(payload)
        return path

    def held_list_still_there(self):
        if not os.path.exists(self.held_file):
            return False
        with io.open(self.held_file, encoding="utf-8") as handle:
            return "Held Track.mp3" in handle.read()

    def test_a_rar_arriving_instead_of_a_zip_does_not_lose_it(self):
        """The reported shape: a peer switches LIST_FORMAT to rar, the hourly
        auto-refetch pulls it, and what arrives is not a zip at all."""
        rar = self.incoming("PeerBot.rar", b"Rar!\x1a\x07\x00" + b"\x00" * 400)

        ok, _reason = list_fetch.process_fetched_list_zip(BOT, rar)

        self.assertFalse(ok)
        self.assertTrue(self.held_list_still_there(),
                        "the list we were serving was destroyed while "
                        "rejecting its replacement")

    def test_an_oversized_archive_does_not_lose_it(self):
        self.set_config(MAX_FETCH_LIST_FILE_SIZE=64)
        big = self.incoming("PeerBot.zip", b"PK\x03\x04" + b"\x00" * 4096)

        ok, _reason = list_fetch.process_fetched_list_zip(BOT, big)

        self.assertFalse(ok)
        self.assertTrue(self.held_list_still_there())

    def test_a_zip_holding_no_list_does_not_lose_it(self):
        path = os.path.join(self.tree.root, "PeerBot.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("readme.md", "nothing useful in here")

        ok, _reason = list_fetch.process_fetched_list_zip(BOT, path)

        self.assertFalse(ok)
        self.assertTrue(self.held_list_still_there())

    def test_a_good_refetch_does_replace_it(self):
        """THE CONTROL. Every assertion above would also pass against a
        version that simply refused to ever install anything."""
        path = os.path.join(self.tree.root, "PeerBot-new.zip")
        fresh = ("List of files\n\n" + "=" * 40 + "\n"
                 "D:\\MEDIA\\Albums\\\n" + "=" * 40 + "\n"
                 "!PeerBot Brand New Track.mp3  ::INFO:: 5.00MB\n")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("PeerBot-2026-09-07.txt", fresh)

        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)

        self.assertTrue(ok, reason)
        listed = os.listdir(self.held_dir)
        self.assertTrue(any("2026-09-07" in name for name in listed), listed)

    def test_a_good_refetch_leaves_no_rollback_copy_behind(self):
        """The held copy is a whole second list on disk. Keeping it after a
        success would quietly double what every fetched list costs."""
        path = os.path.join(self.tree.root, "PeerBot-new.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("PeerBot-2026-09-07.txt",
                             "List of files\n\n" + "=" * 40 + "\n"
                             "D:\\MEDIA\\Albums\\\n" + "=" * 40 + "\n"
                             "!PeerBot Brand New Track.mp3  ::INFO:: 5.00MB\n")

        list_fetch.process_fetched_list_zip(BOT, path)

        self.assertFalse(os.path.exists(self.held_dir + ".previous"))

    def test_a_first_fetch_with_nothing_held_still_works(self):
        """Nothing to hold aside, and no rollback to attempt."""
        import shutil

        shutil.rmtree(self.held_dir, ignore_errors=True)
        path = os.path.join(self.tree.root, "PeerBot-first.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("PeerBot-2026-09-07.txt",
                             "List of files\n\n" + "=" * 40 + "\n"
                             "D:\\MEDIA\\Albums\\\n" + "=" * 40 + "\n"
                             "!PeerBot First Track.mp3  ::INFO:: 5.00MB\n")

        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)

        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
