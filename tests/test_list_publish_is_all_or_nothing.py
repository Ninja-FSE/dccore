"""A rebuild that failed partway published half of itself, and said it had not.

WHAT WENT WRONG

generate_master_list() published the master index, the album index and the
download artifact as three independent os.replace calls, then wrote the two
size side files and the base-name marker, then pruned. No rollback anywhere.

The ordinary way to reach it, on Windows: os.replace onto a file another
handle has open raises PermissionError, and dcc.py holds the published
artifact open for the whole duration of a DCC send. PAUSE_ON_UPDATE only
refuses NEW requests, so a transfer already in flight keeps that handle - and
somebody downloading the list when the scheduled rebuild lands is not an edge
case on a bot with several slots.

What that produced: the master index replaced, so @find, the advert count and
commands.count_from_master_list() all reported the NEW scan - while the
archive users actually received was the previous one, the size side files
still carried the previous numbers, the base-name marker was not updated, and
_prune_superseded_lists never ran.

Nothing recovered it. The artifact stayed stale until some later rebuild
happened to run with no transfer in progress. And the failure branch printed

    [LIST-GEN] The previous list was left untouched and is still in use.

which by then was false.

THE FIX

Each destination is moved ASIDE before its replacement lands, so a failure can
put back exactly what was there. That also makes the locked case fail at the
safest possible moment: renaming a file another process holds open fails on
Windows too, so the lock is discovered while moving the old file out of the
way - before anything observable has changed.

The download artifact goes first, because it is the one a DCC send holds open.
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

import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ThePublishIsAllOrNothing(DCCoreTestCase):

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

    def three_swaps(self):
        """A previous publish, and a fresh one waiting in temporaries."""
        old = [self.write(f"live{i}.txt", f"old-{i}") for i in range(3)]
        new = [self.write(f"tmp{i}.txt", f"new-{i}") for i in range(3)]
        return list(zip(new, old)), old

    def test_every_destination_is_replaced_on_success(self):
        swaps, live = self.three_swaps()

        update_list._publish_artifacts(swaps)

        for index, path in enumerate(live):
            self.assertEqual(self.read(path), f"new-{index}")

    def test_no_previous_files_are_left_behind(self):
        swaps, live = self.three_swaps()

        update_list._publish_artifacts(swaps)

        for path in live:
            self.assertFalse(os.path.exists(path + ".previous"))

    def test_a_failure_partway_leaves_every_destination_as_it_was(self):
        """The whole point. The second swap fails; the first must not stay
        published, because a published index with an unpublished archive is
        the bot advertising one scan and handing out another."""
        swaps, live = self.three_swaps()
        real = platform_compat.replace_with_retry
        calls = []

        def failing(src, dst):
            calls.append(dst)
            # Fail while moving the SECOND destination aside, which is what a
            # file held open by a DCC send actually does on Windows.
            if dst == live[1] + ".previous":
                raise PermissionError(13, "used by another process")
            return real(src, dst)

        platform_compat.replace_with_retry = failing
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

        with self.assertRaises(PermissionError):
            update_list._publish_artifacts(swaps)

        for index, path in enumerate(live):
            self.assertEqual(self.read(path), f"old-{index}",
                             f"destination {index} was left half published")

    def test_a_failure_leaves_no_previous_files_behind_either(self):
        swaps, live = self.three_swaps()
        real = platform_compat.replace_with_retry

        def failing(src, dst):
            if dst == live[2] + ".previous":
                raise PermissionError(13, "used by another process")
            return real(src, dst)

        platform_compat.replace_with_retry = failing
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

        with self.assertRaises(PermissionError):
            update_list._publish_artifacts(swaps)

        for path in live:
            self.assertFalse(os.path.exists(path + ".previous"),
                             "a rolled-back publish left its backup behind")

    def test_a_destination_that_did_not_exist_is_removed_on_failure(self):
        """A first run has nothing to move aside, so rollback means deleting
        what was published rather than restoring anything."""
        fresh = os.path.join(self.root, "brand-new.txt")
        existing = self.write("live.txt", "old")
        swaps = [(self.write("tmpA.txt", "newA"), fresh),
                 (self.write("tmpB.txt", "newB"), existing)]
        real = platform_compat.replace_with_retry

        def failing(src, dst):
            if dst == existing + ".previous":
                raise PermissionError(13, "used by another process")
            return real(src, dst)

        platform_compat.replace_with_retry = failing
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

        with self.assertRaises(PermissionError):
            update_list._publish_artifacts(swaps)

        self.assertFalse(os.path.exists(fresh),
                         "half a rebuild was left published")
        self.assertEqual(self.read(existing), "old")

    def test_a_first_publish_with_nothing_to_replace_works(self):
        swaps = [(self.write("tmpA.txt", "newA"),
                  os.path.join(self.root, "brand-new.txt"))]

        update_list._publish_artifacts(swaps)

        self.assertEqual(self.read(os.path.join(self.root, "brand-new.txt")),
                         "newA")


class TheDownloadArtifactIsSwappedFirst(unittest.TestCase):
    """It is the file a DCC send holds open, so it is the likeliest to fail -
    and the earlier it is attempted, the less there is to roll back."""

    def test_the_artifact_leads_the_swap_list(self):
        with io.open(os.path.join(REPO_ROOT, "update_list.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        # Comments stripped: the surrounding explanation names these paths.
        code = "\n".join(line.split("#", 1)[0]
                         for line in source.splitlines())

        built = code.split("swaps = [", 1)[1].split("]", 1)[0]

        self.assertIn("tmp_artifact_path", built)
        self.assertLess(built.index("tmp_artifact_path"),
                        built.index("tmp_txt_path"),
                        "the download artifact must be swapped before the "
                        "master index, so the likeliest failure happens with "
                        "nothing yet published")

    def test_the_publish_goes_through_the_helper(self):
        with io.open(os.path.join(REPO_ROOT, "update_list.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        code = "\n".join(line.split("#", 1)[0]
                         for line in source.splitlines())
        body = code.split("def generate_master_list(", 1)[1]

        self.assertIn("_publish_artifacts(swaps)", body)
        self.assertNotIn("replace_with_retry(tmp_txt_path", body,
                         "the master index is being swapped directly again, "
                         "outside the all-or-nothing publish")


if __name__ == "__main__":
    unittest.main()
