"""#1124: the threaded walk takes each finished directory in constant time.

walk_with_sizes() submits every directory the moment it is found, so on a
real library thousands of them are outstanding at once. It used to collect
finished ones with concurrent.futures.wait(running, FIRST_COMPLETED), which on
EVERY call locks every outstanding future, installs a waiter on each and takes
it off again. That is O(outstanding) per completion, so the whole walk was
quadratic in the number of directories: 41 s against 3 s on 137k files, and 16
workers four times slower than one, because the workers could not hand back a
result while the caller held all those locks.

Finished directories now come back through a queue that each future feeds from
its done-callback. These tests count how often the walk touches the futures'
locks - a statement about the work done, not a timing that a loaded CI runner
could fail - and check that the walk still finds what the one-worker walk
finds and still stops cleanly when the caller stops early.
"""

import concurrent.futures._base as futures_base
import itertools
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

FOLDERS = 300


class CountingCondition:
    """A future's condition, counting every time anything takes its lock."""

    def __init__(self, inner, counter):
        self._inner = inner
        self._counter = counter

    def acquire(self, *args, **kwargs):
        next(self._counter)
        return self._inner.acquire(*args, **kwargs)

    def __enter__(self):
        next(self._counter)
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class WideTree(DCCoreTestCase):
    """One folder holding FOLDERS folders, each with a file or two - the shape
    that leaves hundreds of directories outstanding at once."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="dccore-wide-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        with open(os.path.join(self.root, "top.flac"), "wb") as handle:
            handle.write(b"t")
        for n in range(FOLDERS):
            folder = os.path.join(self.root, f"Folder {n:03d}")
            os.makedirs(folder)
            for track in range(1 + n % 2):
                with open(os.path.join(folder, f"{track:02d} Track.flac"), "wb") as handle:
                    handle.write(b"x" * (n + track))
        self.top = os.path.normcase(os.path.abspath(self.root))

    def gate_the_subfolders(self):
        """Each subfolder's listing waits for a pass, so the test decides when
        each one finishes: one at a time, with every other one outstanding."""
        real = os.scandir
        passes = threading.Semaphore(0)
        top = self.top

        def scandir(path="."):
            if not isinstance(path, int) and os.path.normcase(os.fspath(path)) != top:
                passes.acquire()
            return real(path)

        os.scandir = scandir
        self.addCleanup(setattr, os, "scandir", real)
        return passes

    def count_future_locks(self):
        """Every Future made from here on counts its lock acquisitions; returns
        (counter, futures made)."""
        counter = itertools.count()
        made = itertools.count()
        real_future = futures_base.Future

        class CountedFuture(real_future):
            def __init__(self):
                super().__init__()
                next(made)
                self._condition = CountingCondition(self._condition, counter)

        patcher = mock.patch.object(futures_base, "Future", CountedFuture)
        patcher.start()
        self.addCleanup(patcher.stop)
        return counter, made

    @staticmethod
    def taken(counter):
        """How many times the counter has been advanced."""
        return next(counter)

    def everything(self, walker, passes=None):
        found = []
        for root, files in walker:
            found.extend((os.path.relpath(root, self.root), name, size) for name, size in files)
            if passes is not None:
                passes.release()
        return sorted(found)


class EachFinishedFolderCostsTheSame(WideTree):
    def test_the_walk_does_not_rescan_the_outstanding_folders(self):
        """FOLDERS directories finish one at a time with all the others still
        outstanding. Taking each one must touch a constant number of locks.

        The old wait() locked every outstanding future twice per finished
        folder - tens of thousands of acquisitions here, growing with the
        square of FOLDERS. Each future's own life (submit, run, result,
        done-callback) takes four."""
        expected = self.everything(update_list.walk_with_sizes(self.root, workers=1))
        passes = self.gate_the_subfolders()
        counter, made = self.count_future_locks()

        found = self.everything(update_list.walk_with_sizes(self.root, workers=4), passes)

        self.assertEqual(found, expected)
        futures = self.taken(made)
        self.assertEqual(futures, FOLDERS + 1)
        locks = self.taken(counter)
        self.assertLessEqual(locks, 12 * futures,
                             f"{locks} lock acquisitions for {futures} folders")


class StoppingEarlyStillStops(WideTree):
    def test_stopping_with_folders_still_queued_leaves_no_worker_behind(self):
        """Closing the walk cancels the folders nobody has started; their
        done-callbacks still fire into a queue nobody reads, which must neither
        raise nor keep a worker alive."""
        passes = self.gate_the_subfolders()
        walker = update_list.walk_with_sizes(self.root, workers=4)
        root, files = next(walker)
        self.assertEqual(os.path.normcase(root), self.top)
        self.assertEqual(files, [("top.flac", 1)])
        # Let the four that are already listing finish, so shutdown has only
        # queued folders left to cancel and nothing to wait on forever.
        for _ in range(FOLDERS):
            passes.release()
        walker.close()
        alive = [t.name for t in threading.enumerate() if t.name.startswith("list-scan")]
        self.assertEqual(alive, [])

    def test_a_walk_after_a_stopped_one_finds_everything(self):
        first = update_list.walk_with_sizes(self.root, workers=4)
        next(first)
        first.close()
        expected = self.everything(update_list.walk_with_sizes(self.root, workers=1))
        self.assertEqual(self.everything(update_list.walk_with_sizes(self.root, workers=4)), expected)
        self.assertEqual(len(expected), 1 + sum(1 + n % 2 for n in range(FOLDERS)))


if __name__ == "__main__":
    unittest.main()
