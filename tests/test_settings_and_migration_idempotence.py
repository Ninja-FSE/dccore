"""Four ways a configuration change did not mean what it said.

All found by the full-program audit.

1. AN INDENTED LINE WAS SWALLOWED INTO THE SETTING ABOVE IT.

   configparser treats an indented line as a CONTINUATION, so

       NICKNAME = MyBot
           a stray indented line

   gave NICKNAME the value "MyBot\\na stray indented line" - newline and all -
   which then went out on the wire. Nothing reported it. settings.conf is one
   setting per line (save() refuses to write a value containing a line break
   for exactly that reason), so an indented line is always a mistake.

2. ADMIN_CHAT_MODE WAS A FIXED-CHOICE SETTING THAT CHOICES DID NOT COVER.

   adminchat tests for "listen" and "connect" and treats everything else as
   "auto", so a typo did not fail - it silently selected the default. The
   operator who wrote "lisen" got automatic mode and no indication their
   setting had not taken.

3. RENAMING A FOLDER LABEL SPLIT EVERY COUNTER AND GREW THE KEY.

   The counter migration decided a key was "already labelled" by testing its
   first component against the CURRENT labels. Rename Flac to Lossless and
   every existing key stops matching, so it is prefixed again:

       Flac/Artist/Song.flac  ->  Lossless/Flac/Artist/Song.flac

   one component longer each time, and the history split from what new
   downloads now write.

4. AN ABSOLUTE KEY RE-MIGRATED ON EVERY BOOT, FOR EVER.

   A file counted while it was under no configured folder keeps its absolute
   path as the key. Its first component ("C:") is not a label, so it was
   "migrated" every time - and os.path.join(label, absolute) returns the
   absolute path unchanged, so the file was rewritten and a migration logged
   on every single start while nothing changed.

3 and 4 have the same root: "has this already run?" was being inferred from
the shape of the data. It is recorded now, once, beside the counters.
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
import defaults as config  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AnIndentedLineIsAMistake(unittest.TestCase):

    def test_it_is_refused_rather_than_swallowed(self):
        with self.assertRaises(settings_file.SettingsError) as caught:
            settings_file.parse("NICKNAME = MyBot\n"
                                "    a stray indented line\n")

        self.assertIn("indented", str(caught.exception))

    def test_the_message_names_the_line(self):
        """An operator has to be able to find it."""
        with self.assertRaises(settings_file.SettingsError) as caught:
            settings_file.parse("NICKNAME = MyBot\n"
                                "CHANNEL = #one\n"
                                "\tindented with a tab\n")

        self.assertIn("line 3", str(caught.exception))
        self.assertIn("indented with a tab", str(caught.exception))

    def test_an_indented_setting_is_refused_too(self):
        """The other shape: it looks like a setting and is a continuation, so
        the daemon would never see it at all."""
        with self.assertRaises(settings_file.SettingsError):
            settings_file.parse("NICKNAME = MyBot\n"
                                "    MAX_DCC_SLOTS = 3\n")

    def test_an_indented_comment_is_still_fine(self):
        """A comment is a comment wherever it sits."""
        entries = settings_file.parse("NICKNAME = MyBot\n"
                                      "    # an indented note\n"
                                      "    ; and another\n"
                                      "CHANNEL = #one\n")

        self.assertEqual(entries["NICKNAME"], "MyBot")
        self.assertEqual(entries["CHANNEL"], "#one")

    def test_blank_and_whitespace_only_lines_are_fine(self):
        entries = settings_file.parse("NICKNAME = MyBot\n"
                                      "   \n"
                                      "\t\n"
                                      "CHANNEL = #one\n")

        self.assertEqual(len(entries), 2)

    def test_the_shipped_sample_still_parses(self):
        """The file every install starts from."""
        with io.open(os.path.join(REPO_ROOT, "settings.conf.sample"),
                     encoding="utf-8") as handle:
            settings_file.parse(handle.read())


class AFixedChoiceSettingIsChecked(unittest.TestCase):

    def test_admin_chat_mode_is_covered(self):
        self.assertIn("ADMIN_CHAT_MODE", settings_file.CHOICES)

    def test_a_typo_is_refused_rather_than_silently_defaulting(self):
        with self.assertRaises(ValueError):
            settings_file.coerce("ADMIN_CHAT_MODE", "lisen", "auto", str)

    def test_every_mode_adminchat_actually_tests_for_is_allowed(self):
        """Read out of adminchat, so the two cannot drift: a mode the daemon
        branches on but CHOICES refuses would be unreachable."""
        with io.open(os.path.join(REPO_ROOT, "adminchat.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        for mode in settings_file.CHOICES["ADMIN_CHAT_MODE"]:
            with self.subTest(mode=mode):
                self.assertIn(f'"{mode}"', source)

    def test_the_valid_modes_pass(self):
        for mode in ("auto", "listen", "connect", "AUTO", " listen "):
            with self.subTest(mode=mode):
                self.assertIn(
                    settings_file.coerce("ADMIN_CHAT_MODE", mode, "auto", str),
                    ("auto", "listen", "connect"))


class TheCounterMigrationRunsOnce(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root
        self.flac = os.path.join(self.root, "Flac")
        os.makedirs(os.path.join(self.flac, "Artist"), exist_ok=True)
        self.folders = os.path.join(self.root, "folders.json")
        self.counts = os.path.join(self.root, "counts.json")
        self.set_config(LIBRARY_FOLDERS_FILE=self.folders)
        self.original = db.DOWNLOAD_COUNTS_FILE
        db.DOWNLOAD_COUNTS_FILE = self.counts
        self.addCleanup(setattr, db, "DOWNLOAD_COUNTS_FILE", self.original)

    def label(self, name):
        with io.open(self.folders, "w", encoding="utf-8") as handle:
            json.dump([{"name": name, "path": self.flac}], handle)

    def counts_are(self, mapping):
        with io.open(self.counts, "w", encoding="utf-8") as handle:
            json.dump(mapping, handle)

    BARE = os.path.join("Artist", "Song.flac")
    ROW = {"name": "Song.flac", "kind": "file", "count": 5}

    def test_renaming_a_label_does_not_re_prefix(self):
        """The key would grow a component every rename, and split the history
        from what new downloads write."""
        self.label("Flac")
        self.counts_are({self.BARE: dict(self.ROW)})
        db.migrate_download_counts_to_labels()

        self.label("Lossless")
        db.migrate_download_counts_to_labels()

        keys = list(db.load_download_counts())
        self.assertEqual(keys, [os.path.join("Flac", "Artist", "Song.flac")])

    def test_an_absolute_key_is_left_alone(self):
        """It is not a relative path missing its label - it is a file that was
        under no configured folder when it was counted."""
        self.label("Flac")
        absolute = os.path.join(self.root, "elsewhere", "x.flac")
        self.counts_are({absolute: {"name": "x.flac", "kind": "file",
                                    "count": 2}})

        moved = db.migrate_download_counts_to_labels()

        self.assertIn(absolute, db.load_download_counts())
        # ZERO moves, not "the key survived". os.path.join(label, absolute)
        # returns the absolute path unchanged, so migrating it looks like a
        # no-op from the outside while rewriting the file and logging a
        # migration - which is what it did on every boot, for ever.
        self.assertEqual(moved, 0)

    def test_a_second_start_does_no_work_at_all(self):
        self.label("Flac")
        self.counts_are({self.BARE: dict(self.ROW)})

        first = db.migrate_download_counts_to_labels()
        second = db.migrate_download_counts_to_labels()
        third = db.migrate_download_counts_to_labels()

        self.assertEqual(first, 1)
        self.assertEqual((second, third), (0, 0))

    def test_the_marker_is_written_even_when_nothing_moved(self):
        """A fresh install has no counters. Without the marker it would try
        again on every start for ever."""
        self.label("Flac")

        db.migrate_download_counts_to_labels()

        self.assertTrue(os.path.exists(db._counter_migration_marker()))

    def test_the_marker_sits_beside_the_counters(self):
        """So a restored backup of the data directory carries its own answer
        with it, rather than being migrated a second time."""
        self.assertEqual(os.path.dirname(db._counter_migration_marker()),
                         os.path.dirname(os.path.abspath(self.counts)))

    def test_a_marker_that_cannot_be_written_does_not_stop_the_daemon(self):
        """It runs at startup. Refusing to boot over a marker would be the
        very thing the wrapper exists to prevent."""
        self.label("Flac")
        self.counts_are({self.BARE: dict(self.ROW)})
        original = db._write_counter_migration_marker

        def boom(_marker):
            raise OSError("read-only data directory")

        db._write_counter_migration_marker = boom
        self.addCleanup(setattr, db, "_write_counter_migration_marker",
                        original)

        self.assertEqual(db.migrate_download_counts_to_labels(), 0)

    def test_the_migration_still_does_its_job_on_a_real_upgrade(self):
        """Control. All of the above must not have turned it off."""
        self.label("Flac")
        self.counts_are({self.BARE: dict(self.ROW)})

        db.migrate_download_counts_to_labels()

        self.assertEqual(
            db.load_download_counts()[os.path.join("Flac", "Artist",
                                                   "Song.flac")]["count"], 5)


if __name__ == "__main__":
    unittest.main()
