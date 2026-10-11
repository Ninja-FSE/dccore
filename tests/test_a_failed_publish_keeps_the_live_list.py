"""A publish that failed on the staged file's rename lost the live list (#1270).

_publish_artifacts() moves each live file aside to "<name>.previous" and then
renames the staged file into its place, rolling back every pair it recorded
when anything fails. A pair was recorded only once BOTH renames had worked.
When the second one failed - an AV scanner or an indexer still holding the
freshly written staged file past every retry - the live file had already been
moved aside and nothing put it back.

What that left: the live master list renamed to "alfa-<date>.txt.previous",
which no "<base>-*.txt" glob matches. find_latest_list() answered None, so
@find said there was no list, the advert was skipped and the counts read 0 -
while the rebuild's own log line said the previous list was untouched and
still in use.

The existing tests in test_list_publish_is_all_or_nothing.py inject their
failure on the move-aside step only, which is why this one was never seen.
"""

import contextlib
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheStagedRenameFailing(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root

    def write(self, name, body):
        path = os.path.join(self.root, name)
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        return path

    def read(self, path):
        with io.open(path, encoding="utf-8") as handle:
            return handle.read()

    def fail_renaming(self, source):
        """The staged file `source` cannot be renamed; everything else can."""
        real = platform_compat.replace_with_retry

        def failing(src, dst, **kwargs):
            if src == source:
                raise PermissionError(13, "used by another process")
            return real(src, dst, **kwargs)

        platform_compat.replace_with_retry = failing
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

    def test_the_pair_that_failed_is_put_back_too(self):
        live = [self.write(f"live{i}.txt", f"old-{i}") for i in range(3)]
        staged = [self.write(f"tmp{i}.txt", f"new-{i}") for i in range(3)]
        self.fail_renaming(staged[1])

        with self.assertRaises(PermissionError):
            update_list._publish_artifacts(list(zip(staged, live)))

        for index, path in enumerate(live):
            self.assertEqual(self.read(path), f"old-{index}",
                             f"live{index}.txt was not restored")
            self.assertFalse(os.path.exists(path + ".previous"))

    def test_the_first_pair_failing_is_put_back(self):
        live = self.write("live.txt", "old")
        staged = self.write("tmp.txt", "new")
        self.fail_renaming(staged)

        with self.assertRaises(PermissionError):
            update_list._publish_artifacts([(staged, live)])

        self.assertEqual(self.read(live), "old")
        self.assertFalse(os.path.exists(live + ".previous"))

    def test_a_new_destination_whose_rename_failed_says_nothing_about_rollback(self):
        """Nothing was there before and nothing landed: the rollback has
        nothing to remove, and must not report that as a failure of its own."""
        fresh = os.path.join(self.root, "brand-new.txt")
        staged = self.write("tmp.txt", "new")
        self.fail_renaming(staged)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(PermissionError):
            update_list._publish_artifacts([(staged, fresh)])

        self.assertFalse(os.path.exists(fresh))
        self.assertNotIn("Could not roll", out.getvalue())


class AWholeRebuild(DCCoreTestCase):
    """The same failure through generate_master_list(), as the bot meets it."""

    def test_the_previous_list_is_still_found_afterwards(self):
        tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=tree.music, LOCAL_LIST_DIR=tree.lists,
                        LIST_BASE_NAME="alfa", NICKNAME="alfa")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(update_list.generate_master_list())
        before = sorted(os.listdir(tree.lists))
        latest = list_mod.find_latest_list()
        self.assertIsNotNone(latest)
        with io.open(latest, encoding="utf-8") as handle:
            published = handle.read()

        real = platform_compat.replace_with_retry

        def failing(src, dst, **kwargs):
            # The master index's staged file is held.
            if re.match(r"alfa-\d{4}-\d{2}-\d{2}\.txt\.new$", os.path.basename(str(src))):
                raise PermissionError(13, "used by another process")
            return real(src, dst, **kwargs)

        platform_compat.replace_with_retry = failing
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(update_list.generate_master_list())

        self.assertEqual(sorted(os.listdir(tree.lists)), before)
        self.assertEqual(list_mod.find_latest_list(), latest)
        with io.open(latest, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), published)


if __name__ == "__main__":
    unittest.main()
