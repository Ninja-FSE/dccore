"""The scan treats a Windows directory junction like a symlink: listed past, not entered (#1270).

walk_with_sizes() has never descended a symlinked directory. A junction
(mklink /J) is not a symlink to Python - is_symlink() answers False for one -
so it WAS descended. One pointing at an ancestor was walked 63 levels deep,
every file under it listed 64 times, until Windows gave up with OSError 22.
That is not a PermissionError, so it counted as a part of the library that
could not be read, and every rebuild after it kept the previous index - with
nothing ever recovering. A junction that does not loop still listed its
target's files a second time under another path.

Two halves, because a junction can only be made on Windows:

  * fakes of a directory entry and of os.scandir, which run everywhere and
    cover both ways the answer is reached - DirEntry.is_junction() on 3.12
    and later, the reparse tag before it;
  * a real junction loop, on Windows, through the real walk and a real
    rebuild.
"""

import contextlib
import io
import os
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

MOUNT_POINT = 0xA0000003
# A OneDrive placeholder folder is a reparse point too, and IS a folder to
# walk into: only the junction tag may stop the walk.
CLOUD_FILES = 0x9000001A


class FakeEntry:
    """Just what walk_with_sizes() and is_link_dir() ask of an os.DirEntry."""

    def __init__(self, path, is_dir, symlink=False, junction=None, tag=0, size=10):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self._is_dir = is_dir
        self._symlink = symlink
        self._tag = tag
        self._size = size
        if junction is not None:
            # Python 3.12 and later: the entry answers for itself.
            self.is_junction = lambda: junction

    def is_dir(self, follow_symlinks=True):
        return self._is_dir

    def is_symlink(self):
        return self._symlink

    def stat(self, follow_symlinks=True):
        return types.SimpleNamespace(st_size=self._size, st_reparse_tag=self._tag)


class ThePredicate(unittest.TestCase):

    def test_each_kind_of_entry(self):
        cases = [
            ("a plain folder", FakeEntry("a", True), False),
            ("a symlink", FakeEntry("a", True, symlink=True), True),
            ("a junction, asked of the entry", FakeEntry("a", True, junction=True), True),
            ("not a junction, asked of the entry", FakeEntry("a", True, junction=False), False),
            ("a junction, by its reparse tag", FakeEntry("a", True, tag=MOUNT_POINT), True),
            ("a cloud placeholder folder", FakeEntry("a", True, tag=CLOUD_FILES), False),
        ]
        for label, entry, expected in cases:
            with self.subTest(label):
                self.assertEqual(update_list.is_link_dir(entry), expected)


class AFakeJunctionLoop(unittest.TestCase):
    """lib/a/loop -> lib, on any platform, through a stand-in for os.scandir."""

    def scandir_with_a_loop(self, **junction):
        listings = {
            "lib": lambda: [FakeEntry("lib/a", True)],
            "lib/a": lambda: [FakeEntry("lib/a/track.flac", False),
                              FakeEntry("lib/a/loop", True, **junction)],
        }
        calls = []

        class Listing:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *exc):
                return False

        def scandir(path):
            calls.append(path)
            if len(calls) > 50:
                raise AssertionError("the walk went round the junction loop")
            # Entering the junction lists its target again, under the
            # junction's own path - which is what makes it a loop.
            target = path
            while "/a/loop" in target:
                target = target.replace("/a/loop", "", 1)
            if target not in listings:
                raise FileNotFoundError(2, "no such folder", path)
            entries = []
            for entry in listings[target]():
                entry.path = path + entry.path[len(target):]
                entries.append(entry)
            return Listing(entries)

        return scandir

    def walk(self, workers, **junction):
        errors = []
        with mock.patch.object(os, "scandir", self.scandir_with_a_loop(**junction)):
            walked = list(update_list.walk_with_sizes("lib", onerror=errors.append,
                                                      workers=workers))
        return walked, errors

    def test_the_loop_is_not_entered(self):
        for workers in (1, 4):
            for label, junction in (("is_junction()", {"junction": True}),
                                    ("reparse tag", {"tag": MOUNT_POINT})):
                with self.subTest(workers=workers, how=label):
                    walked, errors = self.walk(workers, **junction)
                    self.assertEqual(errors, [])
                    self.assertEqual(sorted(root for root, _files in walked), ["lib", "lib/a"])
                    self.assertEqual([files for root, files in walked if root == "lib/a"],
                                     [[("track.flac", 10)]])

    def test_a_folder_that_only_looks_like_one_is_still_entered(self):
        """Guard on the guard: the fake does loop when nothing stops it, so
        the test above is not passing for want of a loop."""
        with self.assertRaises(AssertionError):
            self.walk(1, junction=False)


@unittest.skipUnless(sys.platform == "win32", "a directory junction is a Windows thing")
class ARealJunctionLoop(DCCoreTestCase):
    """The fakes above, against NTFS. They run everywhere; this is the proof
    that they describe what Windows actually hands the walk."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.library = os.path.join(self.tree.root, "library")
        album = os.path.join(self.library, "Artist", "Album")
        os.makedirs(album)
        with open(os.path.join(album, "01 - Track.flac"), "wb") as handle:
            handle.write(b"\0" * 100)
        self.loop = os.path.join(album, "loop")
        try:
            import _winapi
            _winapi.CreateJunction(self.library, self.loop)
        except (ImportError, AttributeError, OSError) as err:
            self.skipTest(f"no junction could be made here: {err}")

    def tearDown(self):
        # The junction goes first, on its own, so removing the tree never
        # has to decide whether to walk through it.
        if os.path.lexists(self.loop):
            os.rmdir(self.loop)
        super().tearDown()

    def test_the_walk_lists_the_track_once(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                errors = []
                files = [name for _root, names in update_list.walk_with_sizes(
                    self.library, onerror=errors.append, workers=workers)
                    for name, _size in names]
                self.assertEqual(errors, [])
                self.assertEqual(files, ["01 - Track.flac"])

    def test_the_reparse_tag_answers_alone(self):
        """The path Pythons before 3.12 take, against a real junction: the
        entry's is_junction() hidden, so only the tag can answer."""

        class WithoutIsJunction:
            def __init__(self, entry):
                self._entry = entry

            def __getattr__(self, name):
                if name == "is_junction":
                    raise AttributeError(name)
                return getattr(self._entry, name)

        with os.scandir(os.path.dirname(self.loop)) as listing:
            entry = next(e for e in listing if e.name == "loop")

        self.assertTrue(update_list.is_link_dir(WithoutIsJunction(entry)))

    def test_a_rebuild_succeeds(self):
        self.set_config(FILE_DIRECTORY=self.library, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="alfa", NICKNAME="alfa")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(), out.getvalue())
        with io.open(list_mod.find_latest_list(), encoding="utf-8") as handle:
            rows = [line for line in handle if line.startswith("!")]
        self.assertEqual(len(rows), 1, rows)


if __name__ == "__main__":
    unittest.main()
