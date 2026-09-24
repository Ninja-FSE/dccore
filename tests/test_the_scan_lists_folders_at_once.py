"""#922: the rebuild lists several folders at once.

On a network mount every directory listing and every file's size is a round
trip; one folder at a time, none of them overlap. walk_with_sizes() now lists
LIST_SCAN_THREADS folders at once. What must not change is what it finds:
the same folders, the same files, the same sizes, the same errors - only the
order folders come back in, which the rebuild sorts away (#443).
"""

import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TreeCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="dccore-scan-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for artist in range(6):
            for album in range(4):
                folder = os.path.join(self.root, f"Artist {artist}", f"Album {album}")
                os.makedirs(folder)
                for track in range(3):
                    with open(os.path.join(folder, f"{track:02d} Track.flac"), "wb") as handle:
                        handle.write(b"x" * (artist * 100 + album * 10 + track))
        os.makedirs(os.path.join(self.root, "Empty", "Deeper"))

    def walk(self, workers, onerror=None):
        """Everything the walk found, in an order that does not depend on
        which thread listed what."""
        found = []
        for root, files in update_list.walk_with_sizes(self.root, onerror=onerror, workers=workers):
            found.extend((os.path.relpath(root, self.root), name, size) for name, size in files)
        return sorted(found)

    def replace_scandir(self, fake):
        """os.scandir replaced for this test. A file descriptor goes straight
        to the real one: the fake is still in place during tearDown (cleanups
        run after it), where shutil.rmtree on Linux and macOS lists by fd."""
        real = os.scandir

        def guarded(path="."):
            if isinstance(path, int):
                return real(path)
            return fake(path)

        os.scandir = guarded
        self.addCleanup(setattr, os, "scandir", real)
        return real


class WhatItFindsDoesNotChange(TreeCase):
    def test_every_worker_count_finds_exactly_what_one_does(self):
        one = self.walk(1)
        self.assertEqual(len(one), 6 * 4 * 3)
        for workers in (2, 8, 16):
            self.assertEqual(self.walk(workers), one, workers)

    def test_an_unreadable_folder_is_reported_once_and_the_rest_still_found(self):
        real = os.scandir
        bad = os.path.join(self.root, "Artist 2")

        def scandir(path="."):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(bad):
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        self.replace_scandir(scandir)
        for workers in (1, 8):
            errors = []
            found = self.walk(workers, onerror=errors.append)
            self.assertEqual(len(errors), 1, workers)
            self.assertEqual(len(found), 5 * 4 * 3, workers)
            self.assertFalse(any(root.startswith("Artist 2") for root, _n, _s in found))


class ItReallyListsSeveralAtOnce(TreeCase):
    def test_the_listings_overlap(self):
        """Six artist folders that can only be listed if six listings are in
        flight together: a barrier of six, which one-at-a-time never passes."""
        real = os.scandir
        barrier = threading.Barrier(6, timeout=10)
        artists = {os.path.normcase(os.path.join(self.root, f"Artist {n}")) for n in range(6)}

        def scandir(path="."):
            if os.path.normcase(os.fspath(path)) in artists:
                barrier.wait()
            return real(path)

        self.replace_scandir(scandir)
        self.assertEqual(len(self.walk(8)), 6 * 4 * 3)

    def test_errors_are_reported_on_the_callers_thread(self):
        real = os.scandir
        bad = os.path.join(self.root, "Artist 0")

        def scandir(path="."):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(bad):
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        self.replace_scandir(scandir)
        threads = []
        self.walk(8, onerror=lambda err: threads.append(threading.current_thread()))
        self.assertEqual(threads, [threading.current_thread()])

    def test_one_worker_is_the_old_walk_and_starts_no_thread(self):
        real = os.scandir
        seen = set()

        def scandir(path="."):
            seen.add(threading.current_thread().name)
            return real(path)

        self.replace_scandir(scandir)
        self.walk(1)
        self.assertEqual(seen, {threading.current_thread().name})

    def test_a_caller_that_stops_early_leaves_no_worker_behind(self):
        walker = update_list.walk_with_sizes(self.root, workers=8)
        next(walker)
        walker.close()
        alive = [t.name for t in threading.enumerate() if t.name.startswith("list-scan")]
        self.assertEqual(alive, [])


class TheSetting(DCCoreTestCase):
    def test_it_is_held_to_1_to_64(self):
        for value, expected in ((0, 1), (1, 1), (16, 16), (500, 64), (None, 1)):
            self.set_config(LIST_SCAN_THREADS=value)
            self.assertEqual(update_list.scan_workers(), expected, value)

    def test_it_ships_at_16(self):
        import defaults
        self.assertEqual(defaults.LIST_SCAN_THREADS, 16)

    def test_the_rebuild_says_how_many_at_a_time(self):
        tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", ORIGINAL_NICK="DCCoreTest",
                        RAR_ENABLED=False, LIST_FORMAT="txt", LIST_SCAN_THREADS=4)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertTrue(update_list.generate_master_list(), buffer.getvalue())
        self.assertIn(", 4 folder(s) at a time...", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
