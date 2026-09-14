"""A list that cannot produce an album does not get an album section.

Issue #383, and a follow-up to #288 which is otherwise done:

    "A video-only list still generates and delivers an always-empty -RAR-
    section ... It is guaranteed to be permanently empty ... it isn't 'usually
    empty,' it structurally cannot ever be anything else."

Verified rather than taken: `LIST_VIDEO_EXTENSIONS` and `RAR_EXTENSIONS` share
not one entry, so since #288 - which made a folder earn its !rar row by
holding a packable file - a video-only list can never hold a row, for any
library, under any configuration.

AND IT DID NOT SIT THERE INERTLY. The masthead makes the file non-empty, so
the `getsize > 0` test that decides what goes into the downloadable archive let
it through. Four dead lines and a heading with nothing behind it, inside every
copy every user downloads.

WHY THE DECISION IS MADE AFTER THE SCAN. #383 suggested asking whether the
list's configured extension set admits packable content. What actually matters
is whether this list PRODUCED a packable folder, and the scan has just finished
answering exactly that - where reasoning from the extension sets would be
predicting it, and would be wrong for a list whose folders merely hold no
albums today and might tomorrow.

AND WHY IT REUSES serve_albums RATHER THAN ADDING A FLAG. Every consequence is
already written: RAR_ENABLED being off skips the masthead, writes no rows,
fails the archive's size test, and removes the temp file instead of publishing
it. "This list has no albums" wants all four of those and nothing else.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import update_list  # noqa: E402

from tests.test_master_list_generation import MasterListCase  # noqa: E402


class AlbumListCase(MasterListCase):
    """MasterListCase plus a reader for the album list. rar_rows() lives on a
    different subclass in the file next door and is not inherited from here."""

    def album_lists(self):
        return [n for n in os.listdir(self.tree.lists) if "-RAR-" in n]

    def album_rows(self):
        found = self.album_lists()
        self.assertEqual(len(found), 1, "expected one album list, got %s" % found)
        with io.open(os.path.join(self.tree.lists, found[0]),
                     encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.startswith("!")]


class TheTwoExtensionSetsDoNotMeet(unittest.TestCase):
    """The premise the rest of this rests on. If it ever stops holding, a
    video list could hold an album row and none of the below is wanted."""

    def test_no_video_extension_is_packable(self):
        video = {x.lower() for x in update_list.video_extensions()}
        packable = {x.lower() for x in update_list.rar_extensions()}

        self.assertTrue(video, "no video extensions configured")
        self.assertTrue(packable, "no packable extensions configured")
        self.assertEqual(sorted(video & packable), [],
                         "a video file is now packable, which makes a "
                         "video-only list's album section reachable")


class AVideoOnlyLibrary(AlbumListCase):
    """The reported case: everything in the library is film or series."""

    def build(self):
        # From NOTHING. TempTree seeds two baseline .flac tracks, so a library
        # that merely adds films to it still holds two albums - the album list
        # is then correctly published and the test fails for the right reason.
        # It did, first time round.
        self.use_empty_library()
        self.set_config(SEPARATE_VIDEO_LIST=False)
        self.add("Films/Some Film (2019)/Some Film.mkv")
        self.add("Series/Some Series/S01E01.mp4")
        self.assertTrue(self.generate())

    def published(self, marker):
        return [n for n in os.listdir(self.tree.lists) if marker in n]

    def test_no_album_list_is_published(self):
        self.build()

        self.assertEqual(self.published("-RAR-"), [],
                         "published an album list that can never hold a row")

    def test_the_films_are_still_listed(self):
        """Guard on the guard. A build that produced nothing would satisfy the
        assertion above and be a far worse bug."""
        self.build()

        rows = [line for line in self.read_list().splitlines()
                if line.startswith("!")]

        self.assertTrue(rows, "the list itself is empty")
        self.assertTrue(any("Some Film" in row for row in rows))

    def test_and_the_download_carries_no_dead_section(self):
        """The half that reaches users. The archive is the master index and
        the album list concatenated, so an always-empty album list rode along
        in every copy."""
        self.build()

        archives = [n for n in os.listdir(self.tree.lists)
                    if n.endswith((".zip", ".rar", ".txt"))]

        self.assertTrue(archives, "nothing was published at all")
        self.assertFalse([n for n in archives if "-RAR-" in n])


class ALibraryWithAlbumsIsUntouched(AlbumListCase):
    """The change must cost nothing to the ordinary case, which is every
    install that serves music."""

    def test_an_album_list_is_still_published(self):
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertTrue([n for n in os.listdir(self.tree.lists)
                         if "-RAR-" in n],
                        "an album list disappeared from a library that has "
                        "albums")

    def test_and_it_still_has_the_row(self):
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertTrue(any("Album" in row for row in self.album_rows()))


class AMixedLibraryKeepsIts(AlbumListCase):
    """One album among the films is enough. The question is what the list
    produced, not what its folders mostly hold."""

    def test_one_packable_folder_keeps_the_album_list(self):
        self.use_empty_library()
        self.set_config(SEPARATE_VIDEO_LIST=False)
        self.add("Films/Some Film (2019)/Some Film.mkv")
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        rows = self.album_rows()
        self.assertTrue(any("Album" in row for row in rows))
        self.assertFalse(any("Some Film" in row for row in rows),
                         "a film folder earned an album row")


class TheSwitchStillMeansWhatItMeant(AlbumListCase):
    """RAR_ENABLED off is a different statement from "this list has no
    albums", and it already had this behaviour. Nothing here may change it."""

    def test_rar_enabled_off_still_publishes_nothing(self):
        self.set_config(RAR_ENABLED=False)
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertEqual([n for n in os.listdir(self.tree.lists)
                          if "-RAR-" in n], [])

    def test_and_the_reason_given_is_the_right_one(self):
        """Two paths reach the same outcome and an operator reading the log
        should be able to tell which - "packing is off" and "there is nothing
        to pack" call for different actions."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("RAR_ENABLED is off - skipping the album list entirely.",
                      source)
        self.assertIn("No folder in this list can be packed", source)


if __name__ == "__main__":
    unittest.main()
