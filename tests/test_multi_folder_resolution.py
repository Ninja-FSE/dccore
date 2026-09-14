"""Resolving a list heading when the library spans several folders (#164).

Step 2. The scan does not write labelled paths yet, so in production every
heading still looks exactly as it did - which is what makes this the safe
place to change resolution. The consumer learns to read labels before anything
produces them, rather than the other way round: a list whose paths did not
resolve would break every `!rar` request the moment it was published.

The security-relevant half is that dcc.py's traversal guard now runs against
the folder a heading resolved INTO, rather than against one global root.
is_safe_path() itself is untouched; what changed is which root it is asked
about. Those tests are at the bottom.
"""

import contextlib
import io
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import (DCCoreTestCase, RecordingSocket, no_disk_writes,  # noqa: E402
                           silence_debug)


class TwoFolderCase(DCCoreTestCase):
    """Two configured folders, both real, with a shared album name."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.flac = self.make_folder("FlacRoot", "Pink Floyd", "Animals")
        self.mp3 = self.make_folder("Mp3Root", "Pink Floyd", "Animals")
        self.make_folder("Mp3Root", "Nirvana", "Nevermind")

        self.store = os.path.join(self.tree.root, "folders.json")
        self.set_config(LIBRARY_FOLDERS_FILE=self.store,
                        FILE_DIRECTORY=os.path.join(self.tree.root, "FlacRoot"))
        library.save_folders([
            library.Folder("Flac", os.path.join(self.tree.root, "FlacRoot")),
            library.Folder("Mp3", os.path.join(self.tree.root, "Mp3Root"))])

    def make_folder(self, *parts):
        path = os.path.join(self.tree.root, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def heading(self, tail):
        return list_mod.LIST_FOLDER_PREFIX + tail


class ALabelledHeadingResolvesIntoItsOwnFolder(TwoFolderCase):

    def test_the_first_component_picks_the_folder(self):
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Flac\\Pink Floyd\\Animals\\"))

        self.assertEqual(folder.name, "Flac")
        self.assertEqual(os.path.normcase(path),
                         os.path.normcase(self.flac))

    def test_the_same_album_in_the_other_folder_is_a_different_path(self):
        """The whole point of labelling. Both folders hold Pink Floyd\\Animals,
        and unlabelled headings could not tell them apart."""
        flac, _ = list_mod.resolve_list_folder_with_root(
            self.heading("Flac\\Pink Floyd\\Animals\\"))
        mp3, _ = list_mod.resolve_list_folder_with_root(
            self.heading("Mp3\\Pink Floyd\\Animals\\"))

        self.assertNotEqual(os.path.normcase(flac), os.path.normcase(mp3))

    def test_a_label_matches_regardless_of_case(self):
        """It gets copied, pasted and retyped by people. A request differing
        only in case is the same request."""
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("fLaC\\Pink Floyd\\Animals\\"))

        self.assertEqual(folder.name, "Flac")
        self.assertEqual(os.path.normcase(path), os.path.normcase(self.flac))

    def test_a_bare_label_resolves_to_that_folder_itself(self):
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Mp3\\"))

        self.assertEqual(folder.name, "Mp3")
        self.assertEqual(os.path.normcase(path),
                         os.path.normcase(os.path.join(self.tree.root, "Mp3Root")))


class AnOldUnlabelledHeadingStillResolves(TwoFolderCase):
    """Anyone who saved a list before this shipped still has unlabelled paths,
    and AutoQ still holds them in its queue. They have to keep working."""

    def test_it_falls_through_to_the_folder_that_actually_has_it(self):
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Nirvana\\Nevermind\\"))

        self.assertEqual(folder.name, "Mp3")
        self.assertTrue(os.path.isdir(path))

    def test_the_operators_order_decides_when_both_have_it(self):
        """Pink Floyd\\Animals exists in both. Flac is configured first, so an
        unlabelled request for it gets Flac - the same rule the list's own
        build order uses."""
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Pink Floyd\\Animals\\"))

        self.assertEqual(folder.name, "Flac")
        self.assertEqual(os.path.normcase(path), os.path.normcase(self.flac))

    def test_existence_decides_when_a_label_shadows_a_real_folder(self):
        """A folder genuinely named "Mp3" inside the first root, while "Mp3"
        is also a label. The labelled reading is tried first; if it does not
        exist, the legacy one wins - so whichever is really there answers."""
        real = self.make_folder("FlacRoot", "Mp3", "Some Album")

        path, _folder = list_mod.resolve_list_folder_with_root(
            self.heading("Mp3\\Some Album\\"))

        self.assertEqual(os.path.normcase(path), os.path.normcase(real))


class WhenNothingResolves(TwoFolderCase):

    def test_a_path_in_no_folder_still_returns_the_first_root(self):
        """So the caller's own existence check fails as it always has, rather
        than this inventing a path that is real but wrong."""
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Nowhere\\At\\All\\"))

        self.assertIsNotNone(folder)
        self.assertFalse(os.path.isdir(path))

    def test_no_configured_folders_gives_no_root(self):
        """An install that has not chosen a music directory yet. Must not
        raise: os.path.join(None, ...) is a TypeError, and every caller here
        is on a request path."""
        self.set_config(LIBRARY_FOLDERS_FILE=os.path.join(self.tree.root, "gone.json"),
                        FILE_DIRECTORY=None)

        self.assertEqual(list_mod.resolve_list_folder_with_root("anything"),
                         ("", None))


class OneFolderBehavesExactlyAsBefore(DCCoreTestCase):
    """The equivalence the whole step rests on. Every install today has one
    folder and no folder file."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.album = os.path.join(self.tree.music, "Artist", "Album")
        os.makedirs(self.album, exist_ok=True)
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LIBRARY_FOLDERS_FILE=os.path.join(self.tree.root, "none.json"))

    def test_resolution_matches_a_plain_join(self):
        heading = list_mod.LIST_FOLDER_PREFIX + "Artist\\Album\\"

        self.assertEqual(
            os.path.normcase(list_mod.resolve_list_folder(heading)),
            os.path.normcase(self.album))

    def test_an_explicit_base_still_pins_to_that_directory(self):
        """Callers that mean one particular root - not "wherever this heading
        lives" - keep that meaning."""
        heading = list_mod.LIST_FOLDER_PREFIX + "Artist\\Album\\"

        self.assertEqual(
            list_mod.resolve_list_folder(heading, base=os.path.join("X:", "other")),
            os.path.join("X:", "other", "Artist", "Album"))

    def test_drive_specifiers_are_still_stripped_from_every_component(self):
        """os.path.join() treats "C:" as drive-relative and discards what came
        before it, so a heading naming another drive could otherwise return a
        path with no relation to the library at all."""
        parts = list_mod.list_heading_parts("D:\\MUSIC\\C:\\Windows\\System32")

        self.assertNotIn("C:", parts)
        self.assertEqual(parts, ["Windows", "System32"])


class TheTraversalGuardWithSeveralFolders(TwoFolderCase):
    """dcc.py's `!rar` guard, driven for real. It now runs against the folder
    the heading resolved INTO - which is what keeps it exactly as strong as it
    was, rather than widening to "inside any configured folder"."""

    def setUp(self):
        super().setUp()
        self.sock = RecordingSocket()
        no_disk_writes(db)
        self.debug = silence_debug(announce)

        self.notices = []
        self._saved = {}
        for name in ("send_pack_error_notice", "send_dcc_queue_notice",
                     "send_dcc_sending_notice", "send_dcc_error"):
            self._saved[name] = getattr(announce, name, None)
            setattr(announce, name,
                    lambda *a, _n=name, **k: self.notices.append(_n))
        self.addCleanup(self._restore)

        self._real_thread = threading.Thread
        dcc.threading.Thread = _Inline
        self.addCleanup(lambda: setattr(dcc.threading, "Thread", self._real_thread))

    def _restore(self):
        for name, original in self._saved.items():
            if original is not None:
                setattr(announce, name, original)

    def ask(self, spec):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            dcc.handle_download_request(self.sock, "someone", "!rar " + spec,
                                        "#dccore-test")
        return buffer.getvalue()

    def test_a_traversal_out_of_a_labelled_folder_is_refused(self):
        output = self.ask("D:\\MUSIC\\Flac\\..\\..\\secret\\")

        self.assertIn("SECURITY", output)
        self.assertIn("send_pack_error_notice", self.notices)

    def test_a_traversal_out_of_an_unlabelled_path_is_refused(self):
        """The legacy reading must not be a way around the guard."""
        output = self.ask("D:\\MUSIC\\..\\..\\secret\\")

        self.assertIn("SECURITY", output)

    # The two ACCEPT cases below assert the guard's own inputs rather than
    # driving handle_download_request(), because a request that passes every
    # check goes on to queue and pack the album for real - which needs a rar
    # binary and does not belong in a test about containment. What the handler
    # does with a legitimate path is exactly the pair asserted here.

    def test_an_album_in_the_second_folder_passes_the_guard(self):
        """The reason a single global root is wrong now: this path is
        legitimate and lives in the folder that is NOT first, so a check
        against FILE_DIRECTORY alone would refuse a real album."""
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Mp3\\Nirvana\\Nevermind\\"))

        self.assertEqual(folder.name, "Mp3")
        self.assertTrue(dcc.is_safe_path(folder.path, os.path.normpath(path)))

    def test_an_album_in_the_first_folder_still_passes(self):
        path, folder = list_mod.resolve_list_folder_with_root(
            self.heading("Flac\\Pink Floyd\\Animals\\"))

        self.assertEqual(folder.name, "Flac")
        self.assertTrue(dcc.is_safe_path(folder.path, os.path.normpath(path)))

    def test_the_guard_still_refuses_a_path_from_the_other_folder(self):
        """Control on the pair above: resolving to a root must not make that
        root accept anything. An Mp3 path checked against Flac is still out."""
        path, _folder = list_mod.resolve_list_folder_with_root(
            self.heading("Mp3\\Nirvana\\Nevermind\\"))
        flac = library.folder_for_label("Flac")

        self.assertFalse(dcc.is_safe_path(flac.path, os.path.normpath(path)))

    def test_a_folder_root_is_still_refused_as_an_artist_root(self):
        """The artist-root check is relative to the resolved folder too. A
        bare label names the whole folder, which is not a packable album."""
        output = self.ask("D:\\MUSIC\\Mp3\\Nirvana\\")

        self.assertIn("root folder", output.lower())
        self.assertNotIn("traversal", output.lower(),
                         "refused for the right reason: this is a legitimate "
                         "path inside a configured folder, just not an album")


class _Inline:
    """Runs the target on the calling thread, so a queued pack is observable."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None


class TheVirtualPrefix(unittest.TestCase):
    """The fixed part in front of every folder heading.

    It is not a real path on anybody's machine - QuickList made the written
    path an option, so lists in the wild carry full drive paths, stripped
    relative ones and bare folder names, and OmenServe consumed all of them.
    There is no canonical prefix, which is what makes this ours to choose.

    It became worth choosing again once the lists stopped being music-only: a
    heading reading "D:\\MUSIC\\TV\\Spider-Noir (2026)\\Season 01\\"
    contradicts itself, because the second component is the operator's own
    folder label.
    """

    def test_what_is_written_says_media(self):
        self.assertEqual(list_mod.LIST_FOLDER_PREFIX, "D:\\MEDIA\\")

    def test_the_old_prefix_is_still_understood(self):
        """EVERY LIST ALREADY IN SOMEBODY'S HANDS says the old one, and a row
        pasted back out of one has to keep working. One prefix is written; all
        of them are read."""
        self.assertIn("D:\\MUSIC\\", list_mod.LIST_FOLDER_PREFIXES)
        self.assertEqual(
            list_mod.list_heading_parts("D:\\MUSIC\\TV\\Show\\"),
            ["TV", "Show"])
        self.assertEqual(
            list_mod.list_heading_parts("D:\\MEDIA\\TV\\Show\\"),
            ["TV", "Show"])

    def test_the_current_prefix_is_the_first_one_tried(self):
        """Order matters only for cost, not correctness - but the one written
        today is the one nearly every heading will carry."""
        self.assertEqual(list_mod.LIST_FOLDER_PREFIXES[0],
                         list_mod.LIST_FOLDER_PREFIX)

    def test_a_heading_is_RECOGNISED_by_any_known_prefix(self):
        """Read out of dcc.py, and the reason this test exists: that file uses
        the prefix to decide whether a line IS a folder heading. Checking only
        the current one stopped it seeing the headings in every list already
        downloaded, so a bare request against one resolved nothing at all -
        which is what the resolution-counting test caught."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertIn("for p in list_mod.LIST_FOLDER_PREFIXES", code)
        self.assertNotIn("startswith(list_mod.LIST_FOLDER_PREFIX)", code)

    def test_the_builder_has_no_prefix_of_its_own(self):
        """Four writers each carried their own copy of the literal, so
        changing the constant alone would have changed nothing about what gets
        written. They go through it now."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"), encoding="utf-8") as handle:
            code = handle.read()

        self.assertNotIn("D:" + chr(92) + chr(92) + "MUSIC", code)
        self.assertGreaterEqual(code.count("list_mod.LIST_FOLDER_PREFIX"), 4)


if __name__ == "__main__":
    unittest.main()
