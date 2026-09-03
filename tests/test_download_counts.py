"""Counting what this bot has actually sent.

The Stats page's last empty table. Everything else on that page reads a number
the daemon already kept; this one is a new counter, so it is the only part with
its own failure modes:

  * two transfers finishing at once, each discarding the other's increment -
    the exact bug db.py's other counters were rewritten to close;
  * two different tracks with the same filename being credited to one row,
    which is #110's ambiguity wearing a different hat;
  * and a cosmetic counter taking a transfer down with it, which would trade
    something that matters for something that does not.
"""

import io
import json
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class CountsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "download_counts.json")
        previous = db.DOWNLOAD_COUNTS_FILE
        db.DOWNLOAD_COUNTS_FILE = self.path
        self.addCleanup(setattr, db, "DOWNLOAD_COUNTS_FILE", previous)

    def counts(self):
        with io.open(self.path, encoding="utf-8") as handle:
            return json.load(handle)


class CountingASend(CountsCase):

    def test_the_first_send_creates_the_row(self):
        db.record_download("Metal/Slayer/Reign.flac", "Reign.flac", "file")

        row = self.counts()["Metal/Slayer/Reign.flac"]
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["name"], "Reign.flac")
        self.assertEqual(row["kind"], "file")

    def test_sending_it_again_increments(self):
        for _ in range(3):
            db.record_download("Metal/Slayer/Reign.flac", "Reign.flac", "file")

        self.assertEqual(self.counts()["Metal/Slayer/Reign.flac"]["count"], 3)

    def test_the_counts_survive_a_restart(self):
        db.record_download("a/b.flac", "b.flac", "file")

        self.assertEqual(db.load_download_counts()["a/b.flac"]["count"], 1)

    def test_an_empty_key_is_ignored_rather_than_counted(self):
        db.record_download("", "b.flac", "file")

        self.assertFalse(os.path.exists(self.path))


class TwoTracksWithTheSameName(CountsCase):
    """#110 again, in a different place. Two albums can each hold a
    "01 - Intro.flac", and crediting one row with both would claim a track is
    popular when two different tracks are."""

    def test_they_are_counted_apart(self):
        db.record_download("Dolch/Nacht/01 - Intro.flac", "01 - Intro.flac", "file")
        db.record_download("EchO/Devoid/01 - Intro.flac", "01 - Intro.flac", "file")
        db.record_download("EchO/Devoid/01 - Intro.flac", "01 - Intro.flac", "file")

        counts = self.counts()
        self.assertEqual(counts["Dolch/Nacht/01 - Intro.flac"]["count"], 1)
        self.assertEqual(counts["EchO/Devoid/01 - Intro.flac"]["count"], 2)

    def test_but_a_person_still_reads_the_filename(self):
        """Keyed by path, displayed by basename - the table is for humans."""
        db.record_download("EchO/Devoid/01 - Intro.flac", "01 - Intro.flac", "file")

        self.assertEqual(db.top_downloads()[0]["name"], "01 - Intro.flac")


class FilesAndAlbumsAreKeptApart(CountsCase):
    """Counted together, reported apart. A 700 MB album and a 4 MB track are
    not comparable, so one merged table would rank by whichever kind this bot
    sends more of - a fact about the library, not about demand."""

    def setUp(self):
        super().setUp()
        for _ in range(2):
            db.record_download("a/Track.flac", "Track.flac", "file")
        for _ in range(5):
            db.record_download("DOLCH - Nacht.rar", "DOLCH - Nacht", "album")

    def test_files_only(self):
        rows = db.top_downloads(kind="file")

        self.assertEqual([row["name"] for row in rows], ["Track.flac"])

    def test_albums_only(self):
        rows = db.top_downloads(kind="album")

        self.assertEqual([row["name"] for row in rows], ["DOLCH - Nacht"])

    def test_the_busier_album_does_not_outrank_the_file_out_of_its_own_table(self):
        """The album has more sends. Asking for files must still not return
        it, however far ahead it is."""
        self.assertNotIn("DOLCH - Nacht",
                         [row["name"] for row in db.top_downloads(kind="file")])


class TheOrderIsStable(CountsCase):

    def test_highest_first(self):
        db.record_download("a", "A", "file")
        for _ in range(4):
            db.record_download("b", "B", "file")
        for _ in range(2):
            db.record_download("c", "C", "file")

        self.assertEqual([row["name"] for row in db.top_downloads()], ["B", "C", "A"])

    def test_equal_counts_are_ordered_by_name(self):
        """The Stats page polls, so equal rows swapping places every few
        seconds reads as the table changing when nothing has.

        The keys are deliberately in the opposite order to the names: the file
        is written with sort_keys=True, so dict order is key order, and an
        earlier version of this test used name == key - which meant it passed
        with no tie-break at all.
        """
        db.record_download("k1", "Zebra", "file")
        db.record_download("k2", "Mango", "file")
        db.record_download("k3", "Apple", "file")

        self.assertEqual([row["name"] for row in db.top_downloads()],
                         ["Apple", "Mango", "Zebra"])

    def test_a_zero_count_row_is_not_listed(self):
        """A hand-edited or half-written file can hold one. Zero sends is not
        a ranking, and showing it puts something in the table that never
        happened."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"a": {"name": "Never", "kind": "file", "count": 0},
                       "b": {"name": "Once", "kind": "file", "count": 1}}, handle)

        self.assertEqual([row["name"] for row in db.top_downloads()], ["Once"])

    def test_the_limit_is_honoured(self):
        for index in range(25):
            db.record_download("k%02d" % index, "n%02d" % index, "file")

        self.assertEqual(len(db.top_downloads(limit=10)), 10)


class TwoTransfersFinishingAtOnce(CountsCase):
    """MAX_DCC_SLOTS transfers complete concurrently. A load here, a mutate
    here and a separate save here let whichever thread saved second discard the
    other's increment - permanently, because nothing ever recomputes these.
    That is the bug db.py's other counters were rewritten to close, and this
    one is written the same way."""

    def test_no_increment_is_lost(self):
        threads = [threading.Thread(target=db.record_download,
                                    args=("shared/Track.flac", "Track.flac", "file"))
                   for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(self.counts()["shared/Track.flac"]["count"], 20)

    def test_concurrent_writes_to_different_keys_all_survive(self):
        threads = [threading.Thread(target=db.record_download,
                                    args=("k%02d" % index, "n%02d" % index, "file"))
                   for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(len(self.counts()), 20)


class ACosmeticCounterNeverCostsATransfer(CountsCase):
    """These numbers feed one table. Nothing else reads them, so every failure
    here has to be quieter than the thing it is counting."""

    def test_an_unreadable_file_starts_empty_rather_than_raising(self):
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(db.load_download_counts(), {})
        self.assertEqual(db.top_downloads(), [])

    def test_a_corrupt_row_is_skipped_not_fatal(self):
        with io.open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"good": {"name": "Good", "kind": "file", "count": 2},
                       "bad": "not a row",
                       "worse": {"name": "W", "kind": "file", "count": "many"}}, handle)

        self.assertEqual([row["name"] for row in db.top_downloads()], ["Good"])

    def test_counting_over_a_corrupt_file_still_works(self):
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        db.record_download("a/b.flac", "b.flac", "file")

        self.assertEqual(self.counts()["a/b.flac"]["count"], 1)

    def test_a_location_that_cannot_be_written_does_not_raise(self):
        """record_download() runs on the send path, immediately after a
        transfer completed successfully. Raising there would turn a cosmetic
        failure into a visible one.

        The unwritable location is a FILE standing where a directory has to
        be, not a chmod: chmod 0o500 does not stop a write on Windows, so a
        test built on it passes there without exercising anything and only
        really runs on the Linux half of CI. This shape fails the same way on
        both.
        """
        blocker = os.path.join(self.tree.root, "not-a-directory")
        with io.open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        db.DOWNLOAD_COUNTS_FILE = os.path.join(blocker, "counts.json")

        db.record_download("a/b.flac", "b.flac", "file")     # must not raise

        self.assertFalse(os.path.isdir(blocker), "the blocker stopped being a file")


class TheStatsPayloadCarriesBoth(CountsCase):

    def test_files_and_albums_arrive_separately(self):
        db.record_download("a/Track.flac", "Track.flac", "file")
        db.record_download("DOLCH - Nacht.rar", "DOLCH - Nacht", "album")

        top = webserver.build_stats_payload()["top"]

        self.assertEqual([row["name"] for row in top["files"]], ["Track.flac"])
        self.assertEqual([row["name"] for row in top["albums"]], ["DOLCH - Nacht"])

    def test_the_page_is_told_whether_albums_are_possible_at_all(self):
        """With RAR_ENABLED off no album can ever be sent, so the page says so
        rather than showing a table that would stay empty for ever."""
        self.set_config(RAR_ENABLED=False)

        self.assertIs(webserver.build_stats_payload()["top"]["albums_enabled"], False)

    def test_history_from_before_it_was_switched_off_is_still_returned(self):
        """Those sends happened. Deciding they did not would be the wrong kind
        of tidy, and the page can explain the state without hiding the past."""
        db.record_download("DOLCH - Nacht.rar", "DOLCH - Nacht", "album")
        self.set_config(RAR_ENABLED=False)

        top = webserver.build_stats_payload()["top"]

        self.assertEqual([row["name"] for row in top["albums"]], ["DOLCH - Nacht"])
        self.assertIs(top["albums_enabled"], False)

    def test_an_unreadable_counts_file_leaves_the_rest_of_the_page_intact(self):
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        payload = webserver.build_stats_payload()

        self.assertEqual(payload["top"]["files"], [])
        self.assertIn("transfer", payload)
        self.assertIn("library", payload)


class TheSendPathCountsWhatItSent(unittest.TestCase):
    """The counter is only worth anything if the transfer path calls it. This
    is the #119 shape: a correct function no live path reaches."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_a_completed_send_records_a_download(self):
        calls = [line.strip() for line in self.source().splitlines()
                 if "db.record_download(" in line
                 and not line.strip().startswith("#")
                 and '"""' not in line]

        self.assertTrue(calls,
                        "nothing in dcc.py counts a completed send, so the "
                        "Most downloaded table can only ever be empty")

    def test_it_is_counted_beside_the_other_completion_statistics(self):
        """Both run once, on the same successful completion. Drifting apart is
        how one of them ends up counting something the other does not."""
        lines = self.source().splitlines()
        stats = [i for i, line in enumerate(lines) if "update_stats_on_complete(" in line]
        counted = [i for i, line in enumerate(lines)
                   if "db.record_download(" in line and '"""' not in line]
        self.assertTrue(stats and counted)

        self.assertLess(min(abs(a - b) for a in stats for b in counted), 40,
                        "the download counter has drifted away from the "
                        "completion statistics it should run beside")

    def test_it_records_what_download_count_identity_decides(self):
        """The send path must not derive the key itself. There is one place
        that decides what a send counts as - WhatEachSendIsCountedAs tests it
        directly - and a second copy inside the transfer would be a rule to
        keep in step rather than one to call."""
        calls = [line.strip() for line in self.source().splitlines()
                 if "db.record_download(" in line
                 and not line.strip().startswith("#")
                 and '"""' not in line]

        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call):
                self.assertIn("download_count_identity(", call)


class WhatEachSendIsCountedAs(DCCoreTestCase):
    """dcc.download_count_identity() - the rules the two tables rest on.

    Split out of start_dcc_send() so it can be tested without a socket. It was
    buried in the middle of a transfer before, where the only way to reach it
    was to complete one.
    """

    def setUp(self):
        super().setUp()
        self.set_config(FILE_DIRECTORY=os.path.join("D:", os.sep, "MUSIC"),
                        TMP_ZIP_DIR=os.path.join("D:", os.sep, "tmp_zips"))

    def test_a_file_is_keyed_by_its_path_not_its_name(self):
        """The mutation that survived the first run. Two albums can each hold
        a "01 - Intro.flac" (#110), so a basename key would credit one track
        with the other's downloads."""
        first = dcc.download_count_identity(
            os.path.join(config.FILE_DIRECTORY, "Dolch", "Nacht", "01 - Intro.flac"),
            "01 - Intro.flac")
        second = dcc.download_count_identity(
            os.path.join(config.FILE_DIRECTORY, "EchO", "Devoid", "01 - Intro.flac"),
            "01 - Intro.flac")

        self.assertNotEqual(first[0], second[0], "two different tracks share one key")
        self.assertEqual(first[1], second[1], "both should still display as the filename")

    def test_the_key_is_relative_to_the_library(self):
        """So moving the library, or reading the file on another machine, does
        not orphan every count that came before."""
        key, _name, _kind = dcc.download_count_identity(
            os.path.join(config.FILE_DIRECTORY, "Dolch", "Nacht", "01 - Intro.flac"),
            "01 - Intro.flac")

        self.assertNotIn("MUSIC", key)
        self.assertIn("Dolch", key)

    def test_an_archive_from_the_temp_directory_is_an_album(self):
        key, name, kind = dcc.download_count_identity(
            os.path.join(config.TMP_ZIP_DIR, "DOLCH - Nacht.rar"), "DOLCH - Nacht.rar")

        self.assertEqual(kind, "album")
        self.assertEqual(key, "DOLCH - Nacht.rar")
        self.assertEqual(name, "DOLCH - Nacht", "the .rar is noise to a reader")

    def test_a_file_that_merely_ends_in_rar_is_not_an_album(self):
        """Somebody can share a .rar as an ordinary file. What makes a send an
        album is where the archive came from, not its extension."""
        _key, name, kind = dcc.download_count_identity(
            os.path.join(config.FILE_DIRECTORY, "Scene", "release.rar"), "release.rar")

        self.assertEqual(kind, "file")
        self.assertEqual(name, "release.rar", "a file keeps the name it has")

    def test_a_path_on_another_drive_still_gets_a_key(self):
        """os.path.relpath raises across drives on Windows rather than
        returning something wrong. A key is still needed, and the full path is
        a perfectly good one."""
        key, _name, kind = dcc.download_count_identity(
            os.path.join("Z:", os.sep, "Elsewhere", "Track.flac"), "Track.flac")

        self.assertTrue(key)
        self.assertEqual(kind, "file")


if __name__ == "__main__":
    unittest.main()
