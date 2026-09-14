"""The download counters assumed there was only one library folder.

WHAT WENT WRONG

#164's own cost table listed this as one of the places that assumed a single
root, and steps 1-4 never came back to it. The key was:

    key = os.path.relpath(file_path, config.FILE_DIRECTORY)

With several folders configured that goes wrong two ways, neither of them
loudly:

  * FILE_DIRECTORY UNSET. Ordinary now that the dashboard writes
    data/library_folders.json and need never touch FILE_DIRECTORY at all.
    os.path.relpath(path, None) measures from the CURRENT WORKING DIRECTORY,
    so the key described where the daemon was started from rather than where
    the library is.

  * FILE_DIRECTORY SET TO THE FIRST FOLDER. A file in the second keys as
    "..\\Second\\Artist\\Album\\track.flac" - and on a different drive relpath
    raises ValueError outright and the code fell back to the ABSOLUTE path,
    which is precisely what #151 made these keys relative to avoid: every
    counter breaks the moment the library moves.

THE FIX, AND WHY THE LABEL

The key is now "<label>/<path beneath that folder>". Keyed on the LABEL and
not the folder's path, so an operator who moves D:\\Flac to E:\\Flac and
updates the folder list keeps their history - the label did not change.

AND WHY A MIGRATION

Changing the key silently resets every operator's counters: the old rows sit
under keys nothing will increment again while the table starts from nothing.
#164 settled the principle for this exact shape of change - one break at the
moment the operator upgrades beats a quiet second one weeks later - and a
migration means there is no break at all. Every install with counters today is
single-folder, because multi-folder was unreachable until the dashboard could
write the list, so the old bare key is unambiguously a file in the first
configured folder.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import db  # noqa: E402
import dcc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class CounterTestCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root
        self.flac = os.path.join(self.root, "Flac")
        self.mp3 = os.path.join(self.root, "Mp3")
        for base in (self.flac, self.mp3):
            os.makedirs(os.path.join(base, "Artist", "Album"), exist_ok=True)
        self.in_flac = os.path.join(self.flac, "Artist", "Album", "Song.flac")
        self.in_mp3 = os.path.join(self.mp3, "Artist", "Album", "Song.mp3")
        self.folders_file = os.path.join(self.root, "library_folders.json")
        self.counts_file = os.path.join(self.root, "download_counts.json")
        self.set_config(LIBRARY_FOLDERS_FILE=self.folders_file,
                        FILE_DIRECTORY=None)
        db.DOWNLOAD_COUNTS_FILE = self.counts_file
        self.addCleanup(setattr, db, "DOWNLOAD_COUNTS_FILE",
                        db.DOWNLOAD_COUNTS_FILE)

    def set_folders(self, *pairs):
        with io.open(self.folders_file, "w", encoding="utf-8") as handle:
            json.dump([{"name": name, "path": path} for name, path in pairs],
                      handle)

    def write_counts(self, mapping):
        with io.open(self.counts_file, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle)

    def key_for(self, path):
        key, _name, _kind = dcc.download_count_identity(
            path, os.path.basename(path))
        return key


class TheKeyNamesItsFolder(CounterTestCase):

    def test_each_folder_gets_its_own_key(self):
        self.set_folders(("Flac", self.flac), ("Mp3", self.mp3))

        self.assertEqual(self.key_for(self.in_flac),
                         os.path.join("Flac", "Artist", "Album", "Song.flac"))
        self.assertEqual(self.key_for(self.in_mp3),
                         os.path.join("Mp3", "Artist", "Album", "Song.mp3"))

    def test_two_folders_holding_the_same_relative_path_stay_apart(self):
        """The ambiguity #164 named: both roots can hold
        Artist/Album/track. Before this, one credited the other."""
        same_in_flac = os.path.join(self.flac, "Artist", "Album", "Same.flac")
        same_in_mp3 = os.path.join(self.mp3, "Artist", "Album", "Same.flac")
        self.set_folders(("Flac", self.flac), ("Mp3", self.mp3))

        self.assertNotEqual(self.key_for(same_in_flac),
                            self.key_for(same_in_mp3))

    def test_the_key_survives_the_library_moving(self):
        """The property #151 made these keys relative for, restated for
        several folders: the LABEL is the stable part, not the path."""
        self.set_folders(("Flac", self.flac))
        before = self.key_for(self.in_flac)

        moved = os.path.join(self.root, "MovedElsewhere")
        os.makedirs(os.path.join(moved, "Artist", "Album"), exist_ok=True)
        self.set_folders(("Flac", moved))

        after = self.key_for(os.path.join(moved, "Artist", "Album", "Song.flac"))
        self.assertEqual(after, before)

    def test_a_file_under_no_configured_folder_keeps_its_full_path(self):
        """A temp archive, or a folder removed from the list mid-session. Not
        a reason to lose the row."""
        self.set_folders(("Flac", self.flac))
        outside = os.path.join(self.root, "elsewhere", "x.flac")
        os.makedirs(os.path.dirname(outside), exist_ok=True)

        self.assertEqual(self.key_for(outside), outside)

    def test_no_folders_configured_at_all(self):
        """A fresh install serving nothing yet must not raise on the one code
        path that runs after a transfer."""
        self.set_folders()

        self.assertEqual(self.key_for(self.in_flac), self.in_flac)

    def test_a_sibling_folder_sharing_a_prefix_is_not_confused(self):
        """library.is_inside() compares on a separator boundary, and this is
        the case a plain startswith gets wrong."""
        backup = self.flac + "-backup"
        os.makedirs(os.path.join(backup, "Artist"), exist_ok=True)
        self.set_folders(("Flac", self.flac))

        stray = os.path.join(backup, "Artist", "Song.flac")

        self.assertEqual(self.key_for(stray), stray)

    def test_an_album_is_still_keyed_by_its_archive_name(self):
        """Untouched: a packed album's archive name already identifies it, and
        it does not live under a served folder at all."""
        self.set_folders(("Flac", self.flac))
        archive = os.path.join(self.config.TMP_ZIP_DIR, "Artist - Album.rar")

        key, name, kind = dcc.download_count_identity(archive,
                                                      "Artist - Album.rar")

        self.assertEqual(kind, "album")
        self.assertEqual(key, "Artist - Album.rar")
        self.assertEqual(name, "Artist - Album")


class MigratingExistingCounters(CounterTestCase):

    LEGACY = {
        os.path.join("Artist", "Album", "Song.flac"):
            {"name": "Song.flac", "kind": "file", "count": 7},
        os.path.join("Other", "Track.mp3"):
            {"name": "Track.mp3", "kind": "file", "count": 3},
        "SomeAlbum.rar":
            {"name": "SomeAlbum", "kind": "album", "count": 12},
    }

    def test_existing_rows_move_onto_the_label(self):
        self.set_folders(("Flac", self.flac))
        self.write_counts(self.LEGACY)

        moved = db.migrate_download_counts_to_labels()
        counts = db.load_download_counts()

        self.assertEqual(moved, 2)
        self.assertEqual(
            counts[os.path.join("Flac", "Artist", "Album", "Song.flac")]["count"], 7)
        self.assertEqual(counts[os.path.join("Flac", "Other", "Track.mp3")]["count"], 3)

    def test_the_history_actually_continues(self):
        """The point of migrating at all: the key a live download produces must
        be the key the old row now sits under."""
        self.set_folders(("Flac", self.flac))
        self.write_counts(self.LEGACY)
        db.migrate_download_counts_to_labels()

        counts = db.load_download_counts()

        self.assertIn(self.key_for(self.in_flac), counts)

    def test_albums_are_left_alone(self):
        """An album's key is its archive name and never had a root in it."""
        self.set_folders(("Flac", self.flac))
        self.write_counts(self.LEGACY)

        db.migrate_download_counts_to_labels()

        self.assertEqual(db.load_download_counts()["SomeAlbum.rar"]["count"], 12)

    def test_it_is_idempotent(self):
        self.set_folders(("Flac", self.flac))
        self.write_counts(self.LEGACY)

        first = db.migrate_download_counts_to_labels()
        second = db.migrate_download_counts_to_labels()

        self.assertEqual(first, 2)
        self.assertEqual(second, 0)

    def test_a_second_run_does_not_double_prefix(self):
        self.set_folders(("Flac", self.flac))
        self.write_counts(self.LEGACY)

        db.migrate_download_counts_to_labels()
        db.migrate_download_counts_to_labels()

        for key in db.load_download_counts():
            self.assertNotIn(os.path.join("Flac", "Flac"), key)

    def test_both_spellings_present_are_added_not_discarded(self):
        """Possible if an operator ran a build from before the migration and
        one from after. Either row winning would throw away real downloads."""
        self.set_folders(("Flac", self.flac))
        bare = os.path.join("Artist", "Song.flac")
        labelled = os.path.join("Flac", "Artist", "Song.flac")
        self.write_counts({
            bare: {"name": "Song.flac", "kind": "file", "count": 4},
            labelled: {"name": "Song.flac", "kind": "file", "count": 6},
        })

        db.migrate_download_counts_to_labels()

        self.assertEqual(db.load_download_counts()[labelled]["count"], 10)

    def test_nothing_happens_with_no_folders_configured(self):
        self.set_folders()
        self.write_counts(self.LEGACY)

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)

    def test_no_counters_is_not_an_error(self):
        self.set_folders(("Flac", self.flac))

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)

    def test_a_corrupt_counts_file_does_not_stop_the_daemon(self):
        """It runs at startup, so it must have the same posture as every other
        loader in db.py: a cosmetic file losing its contents is not a reason
        to refuse to boot."""
        self.set_folders(("Flac", self.flac))
        with io.open(self.counts_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)

    def test_rows_that_are_not_dicts_are_skipped(self):
        self.set_folders(("Flac", self.flac))
        self.write_counts({"a/b.flac": "not a row", "c/d.flac": None})

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)


class TheMigrationRunsAtStartup(unittest.TestCase):
    """A migration nothing calls is not a migration."""

    def test_startup_calls_it(self):
        with io.open(os.path.join(REPO_ROOT, "oserve.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("db.migrate_download_counts_to_labels()", source,
                      "oserve.startup() no longer runs the counter migration - "
                      "every existing operator's history would stay under keys "
                      "nothing increments again")


class TheMigrationCannotStopTheDaemonBooting(CounterTestCase):
    """It runs from oserve.startup(), so anything it raises is a bot that will
    not start - over a file whose own loader is written to shrug off
    corruption. load_download_counts() states the posture: losing these
    counters "is a cosmetic failure - which is exactly why it must not be a
    loud one".

    Found by audit. The merge branch read counts[new_key].get(...) after only
    checking `new_key in counts`, which is an AttributeError the moment the
    value sitting there is not a dict.
    """

    def test_a_non_dict_row_under_the_target_key_does_not_raise(self):
        """The exact defect: a legacy row migrates ONTO a key whose existing
        value is a string."""
        self.set_folders(("Flac", self.flac))
        bare = os.path.join("Artist", "Song.flac")
        labelled = os.path.join("Flac", "Artist", "Song.flac")
        self.write_counts({
            bare: {"name": "Song.flac", "kind": "file", "count": 4},
            labelled: "corrupted-not-a-dict",
        })

        moved = db.migrate_download_counts_to_labels()

        self.assertEqual(moved, 1)

    def test_the_real_row_survives_the_corrupt_one(self):
        """`row` came through the isinstance check and is real data; whatever
        was sitting under the target key did not."""
        self.set_folders(("Flac", self.flac))
        bare = os.path.join("Artist", "Song.flac")
        labelled = os.path.join("Flac", "Artist", "Song.flac")
        self.write_counts({
            bare: {"name": "Song.flac", "kind": "file", "count": 4},
            labelled: "corrupted-not-a-dict",
        })

        db.migrate_download_counts_to_labels()

        self.assertEqual(db.load_download_counts()[labelled]["count"], 4)

    def test_every_shape_of_junk_under_the_target_key(self):
        for junk in ("string", 12, None, [], [1, 2], True):
            with self.subTest(junk=junk):
                self.set_folders(("Flac", self.flac))
                bare = os.path.join("Artist", "Song.flac")
                labelled = os.path.join("Flac", "Artist", "Song.flac")
                self.write_counts({
                    bare: {"name": "Song.flac", "kind": "file", "count": 4},
                    labelled: junk,
                })

                db.migrate_download_counts_to_labels()

    def test_a_count_that_is_not_a_number_does_not_lose_the_row(self):
        self.set_folders(("Flac", self.flac))
        bare = os.path.join("Artist", "Song.flac")
        labelled = os.path.join("Flac", "Artist", "Song.flac")
        self.write_counts({
            bare: {"name": "Song.flac", "kind": "file", "count": 9},
            labelled: {"name": "Song.flac", "kind": "file", "count": "lots"},
        })

        db.migrate_download_counts_to_labels()

        # The legacy row's real count wins over the unusable one, rather than
        # the row merely surviving with "lots" still in it.
        self.assertEqual(db.load_download_counts()[labelled]["count"], 9)

    def test_anything_that_still_escapes_is_caught_and_reported(self):
        """The backstop. Whatever a future edit gets wrong in here, the daemon
        still boots and the counters are left as they were."""
        original = db._migrate_download_counts_to_labels
        db._migrate_download_counts_to_labels = lambda: 1 / 0
        self.addCleanup(setattr, db, "_migrate_download_counts_to_labels",
                        original)

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)


if __name__ == "__main__":
    unittest.main()
