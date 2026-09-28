"""A remembered lookup does not hand out the wrong same-named copy (#962).

The lookup memories (#886) - the path a name last resolved to, and the
folders recent lookups landed in - were keyed on the NAME alone. The list
scan picks between same-named copies by the size a request pastes after
"::INFO::", but a request answered from memory skipped the scan: after one
user asked for album A's "Track 01.flac", another who pasted album B's row,
with B's size, was sent A's copy for the next five minutes. Names like
cover.jpg, folder.jpg and "01 - Intro.mp3" are in nearly every album.

Now the exact-name memory is keyed on the size hint too, and a folder
candidate is only taken when the file is the hinted size. A request with no
hint behaves as before.
"""

import io
import os
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402

import tests.test_download_resolution as resolution  # noqa: E402
import tests.test_path_security as path_security  # noqa: E402

# Imported as modules, not names: a TestCase class imported by name is
# collected and run again here.
ALBUMS, NAME = resolution.ALBUMS, resolution.NAME

SIZES = {1024: "1.0KB", 2048: "2.0KB"}


class TheSizeDecidesEvenFromMemory(path_security.PathSecurityBase):
    # The two-albums fixture's own helpers, borrowed rather than inherited so
    # its tests (which expect its tiny files) do not run against these.
    _album = resolution.TwoFoldersOneFilename._album
    _path = resolution.TwoFoldersOneFilename._path
    _walk_reaches_first = resolution.TwoFoldersOneFilename._walk_reaches_first
    _write_list = resolution.TwoFoldersOneFilename._write_list
    _request = resolution.TwoFoldersOneFilename._request
    _served_path = resolution.TwoFoldersOneFilename._served_path

    def setUp(self):
        super().setUp()
        for folder in ALBUMS:
            self._album(folder)
        self.walk_first = self._walk_reaches_first()
        self.list_first = next(f for f in ALBUMS if f != self.walk_first)
        dcc.forget_library_lookups()
        self.addCleanup(dcc.forget_library_lookups)
        # Real sizes the list's ::INFO:: values describe exactly.
        self.small, self.large = self.list_first, self.walk_first
        for folder, size in ((self.small, 1024), (self.large, 2048)):
            with io.open(self._path(folder), "wb") as handle:
                handle.write(b"x" * size)
        self._write_list([self.small, self.large],
                         sizes={self.small: SIZES[1024], self.large: SIZES[2048]})

    def served_for(self, hint):
        self._request(f"{NAME}  ::INFO:: {hint}" if hint else NAME)
        return self._served_path()

    def test_the_audit_s_case(self):
        """One user takes the small copy; the next asks for the large one."""
        self.assertEqual(self.served_for(SIZES[1024]), self._path(self.small))
        self.assertEqual(self.served_for(SIZES[2048]), self._path(self.large))

    def test_the_other_way_round(self):
        self.assertEqual(self.served_for(SIZES[2048]), self._path(self.large))
        self.assertEqual(self.served_for(SIZES[1024]), self._path(self.small))

    def test_the_same_hint_is_still_served_from_memory(self):
        """What #886 was for: the repeat costs no second scan."""
        self.served_for(SIZES[2048])
        walks = []
        real = dcc.os.walk
        dcc.os.walk = lambda *a, **k: walks.append(a) or real(*a, **k)
        self.addCleanup(setattr, dcc.os, "walk", real)
        self.assertEqual(self.served_for(SIZES[2048]), self._path(self.large))
        self.assertEqual(walks, [])

    def test_a_bare_request_s_memory_is_its_own(self):
        """No hint: the list's first copy, as the scan answers - and a hinted
        request in between neither takes that memory nor is given it."""
        self.assertEqual(self.served_for(""), self._path(self.small))
        self.assertEqual(self.served_for(SIZES[2048]), self._path(self.large))
        self.assertEqual(self.served_for(""), self._path(self.small))


class TheSizeCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="dccore-size-hint-")
        self.path = os.path.join(self.dir, "a.flac")
        with io.open(self.path, "wb") as handle:
            handle.write(b"x" * 1024)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_the_first_word_is_the_size(self):
        self.assertTrue(dcc._matches_size_hint(self.path, "1.0kb"))
        self.assertTrue(dcc._matches_size_hint(self.path, "1.0KB 4m31s 320/44.1/JS"),
                        "with audio info, the rest is length and quality")
        self.assertFalse(dcc._matches_size_hint(self.path, "2.0kb"))

    def test_no_hint_matches_and_a_missing_file_does_not(self):
        self.assertTrue(dcc._matches_size_hint(self.path, ""))
        self.assertFalse(dcc._matches_size_hint(os.path.join(self.dir, "gone.flac"), "1.0kb"))


if __name__ == "__main__":
    unittest.main()
