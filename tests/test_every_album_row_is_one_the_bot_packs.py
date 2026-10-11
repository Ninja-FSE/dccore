"""Every "!<nick> !rar <folder>" row the list publishes is one dcc.py packs (#1270).

The album list exists to offer those rows: a user pastes one, AutoQ copies
one verbatim. Two kinds were published that dcc.py refused every time:

  * A MULTI-DISC ALBUM. Album/CD1 and Album/CD2 are written as one row naming
    Album - the box-word truncation. dcc.py's RAR_EXTENSIONS gate then looked
    only at Album's own top level, found two folders and no track, and
    refused with "holds nothing in RAR_EXTENSIONS". No per-disc row was
    written either, so the album could not be packed at all.
  * A LOOSE TRACK in the scan folder itself or directly in an artist folder.
    That marked the folder packable and published a row for it, and dcc.py
    refuses to pack the library root or an artist root outright.

The row TEXT for a multi-disc album is unchanged - the gate now looks through
the disc folders the way the builder truncates them - so a row already in
somebody's AutoQ queue keeps working. The dead rows are simply not written.

Every row is fed back here to the real dcc.handle_download_request(), which
is the only honest answer to "would the bot pack this".
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug  # noqa: E402

BACKSLASH = chr(92)


class RowCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        # Start from nothing: TempTree's own album is not part of the scenario.
        self.music = os.path.join(self.tree.root, "library")
        os.makedirs(self.music)
        self.set_config(FILE_DIRECTORY=self.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="alfa", NICKNAME="alfa", ORIGINAL_NICK="alfa",
                        CHANNEL="#alfa-test", RAR_ENABLED=True, MAX_RAR_FOLDER_SIZE=0)
        no_disk_writes(db)
        silence_debug(announce)
        self.notices = []
        for name in ("send_pack_error_notice", "send_dcc_queue_notice",
                     "send_dcc_error", "send_dcc_already_queued_notice"):
            real = getattr(announce, name)
            setattr(announce, name,
                    lambda *a, _name=name, **k: self.notices.append(_name))
            self.addCleanup(setattr, announce, name, real)
        # Queued is the answer wanted; the pack itself is not run.
        real_check = dcc.check_queue_and_send
        dcc.check_queue_and_send = lambda *a, **k: None
        self.addCleanup(setattr, dcc, "check_queue_and_send", real_check)
        self.addCleanup(config.dcc_queue.clear)

    def add(self, *parts):
        path = os.path.join(self.music, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * 100)

    def build(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(), out.getvalue())
        names = [n for n in os.listdir(self.tree.lists) if "-RAR-" in n]
        if not names:
            return []
        with io.open(os.path.join(self.tree.lists, names[0]), encoding="utf-8") as handle:
            return [line.rstrip("\n") for line in handle if line.startswith("!alfa !rar ")]

    def answer(self, row):
        """What the real request handler does with a pasted row."""
        self.notices.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(RecordingSocket(), "bravo",
                                        row[len("!alfa "):], "#alfa-test")
        return list(self.notices)

    def row_for(self, *parts):
        return ("!alfa !rar D:" + BACKSLASH + "MEDIA" + BACKSLASH + "library"
                + BACKSLASH + BACKSLASH.join(parts) + BACKSLASH)


class AMultiDiscAlbum(RowCase):

    def setUp(self):
        super().setUp()
        self.add("Artist A", "Double Album", "CD1", "01 - Opening.flac")
        self.add("Artist A", "Double Album", "CD2", "01 - Closing.flac")

    def test_it_has_one_row_naming_the_album(self):
        """Unchanged text: AutoQ copies this line, and rows already queued
        there must still match what the bot sends back."""
        self.assertEqual(self.build(), [self.row_for("Artist A", "Double Album")])

    def test_and_the_bot_packs_it(self):
        rows = self.build()

        self.assertEqual([self.answer(row) for row in rows],
                         [["send_dcc_queue_notice"]])

    def test_discs_nested_two_deep_are_followed_too(self):
        self.add("Artist C", "Box Set", "Disc 1", "CD1", "01.flac")

        rows = self.build()

        self.assertIn(self.row_for("Artist C", "Box Set"), rows)
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(self.answer(row), ["send_dcc_queue_notice"])


class FoldersTheBotRefuses(RowCase):

    def setUp(self):
        super().setUp()
        self.add("Loose Root Track.mp3")
        self.add("Artist B", "Loose Single.flac")
        self.add("Artist C", "Normal Album", "01.flac")

    def test_no_row_for_the_library_root_or_an_artist_root(self):
        self.assertEqual(self.build(), [self.row_for("Artist C", "Normal Album")])

    def test_every_row_written_is_packed(self):
        for row in self.build():
            with self.subTest(row=row):
                self.assertEqual(self.answer(row), ["send_dcc_queue_notice"])

    def test_the_refused_rows_really_are_refused(self):
        """Guard on the guard: the two rows the list no longer carries are the
        ones the bot turns away, so leaving them out loses nothing."""
        self.build()
        for row in (self.row_for("Artist B"),
                    "!alfa !rar D:" + BACKSLASH + "MEDIA" + BACKSLASH + "library" + BACKSLASH):
            with self.subTest(row=row):
                self.assertEqual(self.answer(row), ["send_pack_error_notice"])

    def test_loose_tracks_alone_ship_no_album_list(self):
        """An album list whose every row was dropped would be a masthead with
        nothing under it, in the archive every user downloads."""
        os.remove(os.path.join(self.music, "Artist C", "Normal Album", "01.flac"))

        self.assertEqual(self.build(), [])
        self.assertEqual([n for n in os.listdir(self.tree.lists) if "-RAR-" in n], [])

    def test_the_loose_tracks_are_still_listed_by_name(self):
        self.build()
        # The master list by its name - "alfa-<date>.txt" - not the first
        # .txt the folder happens to list: the size side file is a .txt too,
        # and macOS lists it first (a CI failure read "300" as the list).
        names = [n for n in os.listdir(self.tree.lists)
                 if n.startswith("alfa-") and n.endswith(".txt") and "-RAR-" not in n]
        self.assertEqual(len(names), 1, names)
        with io.open(os.path.join(self.tree.lists, names[0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("!alfa Loose Root Track.mp3", text)
        self.assertIn("!alfa Loose Single.flac", text)


class TheTwoHalvesOfTheRule(DCCoreTestCase):
    """rar_row_folder() writes the rows; folder_holds_packable() honours them."""

    def test_the_row_folder(self):
        cases = {
            os.path.join("lib", "Artist", "Album"): "lib/Artist/Album",
            os.path.join("lib", "Artist", "Album", "CD1"): "lib/Artist/Album",
            os.path.join("lib", "Artist", "Album", "Disc 2", "CD1"): "lib/Artist/Album",
            # The disc folder is not the last segment: no cut.
            os.path.join("lib", "Artist", "Album", "CD1", "Bonus"): "lib/Artist/Album/CD1/Bonus",
            # Cutting would leave an artist root: the folder itself.
            os.path.join("lib", "Artist", "CD1"): "lib/Artist/CD1",
            # A substring is not a box word.
            os.path.join("lib", "Artist", "Discography"): "lib/Artist/Discography",
            os.path.join("lib", "Artist"): None,
            "lib": None,
        }
        for folder, expected in cases.items():
            with self.subTest(folder=folder):
                self.assertEqual(update_list.rar_row_folder(folder), expected)

    def make(self, root, *parts):
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0")

    def test_the_gate(self):
        root = self.make_tree().root
        packable = (".flac",)
        self.make(root, "one", "01.flac")
        self.make(root, "discs", "CD1", "01.flac")
        self.make(root, "deep", "Disc 1", "CD2", "01.flac")
        self.make(root, "bonus", "Extras", "01.flac")
        self.make(root, "notes", "CD1", "notes.txt")
        cases = {"one": True, "discs": True, "deep": True,
                 "bonus": False, "notes": False}
        for folder, expected in cases.items():
            with self.subTest(folder=folder):
                self.assertEqual(update_list.folder_holds_packable(
                    os.path.join(root, folder), packable), expected)

    def test_an_unreadable_folder_raises_as_the_scandir_did(self):
        root = self.make_tree().root
        with self.assertRaises(OSError):
            update_list.folder_holds_packable(os.path.join(root, "missing"), (".flac",))


if __name__ == "__main__":
    unittest.main()
