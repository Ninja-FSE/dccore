"""The Stats view's payload.

Every tile on the page reads a number the daemon already kept and never showed
anywhere except the channel advert. So the risks here are not arithmetic:

  * a figure the page renders differently from the advert, so the operator is
    told two things about one number;
  * a counter read raw when it has gone stale, which for the Today column means
    yesterday's total presented as today's;
  * one unreadable file taking the whole page down, when it should cost one
    tile;
  * and the usual one for this module - reaching for the daemon at import time
    and breaking the guarantee that the routes are testable without one.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import stats_mgr  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class StatsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(STATS_FILE=os.path.join(self.tree.root, "stats.txt"),
                        SPEED_RECORD_FILE=os.path.join(self.tree.root, "speed.txt"),
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreWin",
                        MAX_DCC_SLOTS=3)
        for name in ("STATS_FILE", "SPEED_RECORD_FILE"):
            previous = getattr(db, name, None)
            setattr(db, name, getattr(config, name))
            self.addCleanup(setattr, db, name, previous)

    def write_stats(self, row):
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write(" ".join(str(part) for part in row))

    def write_rar_list(self, folders, name="DCCoreWin-RAR-2026-08-27.txt"):
        with io.open(os.path.join(self.tree.lists, name), "w", encoding="utf-8") as handle:
            handle.write("List of Entire Album Folders (!rar) for !DCCoreWin\n")
            handle.write("To request an entire album, copy/paste the line...\n")
            handle.write("=" * 40 + "\n\n")
            for index in range(folders):
                handle.write("!DCCoreWin !rar D:\\MUSIC\\Album %d\\\n" % index)


class ThePageAndTheAdvertAgree(StatsCase):
    """Both render the same underlying number, so both must render it the same
    way. Two renderings of one figure is how an operator ends up believing the
    dashboard and the channel are reporting different things."""

    def test_speeds_use_the_daemons_own_formatter(self):
        payload = webserver.build_stats_payload()

        self.assertEqual(payload["transfer"]["speed_now_text"],
                         stats_mgr.format_speed(payload["transfer"]["speed_now"]))
        self.assertEqual(payload["transfer"]["record_text"],
                         stats_mgr.format_speed(payload["transfer"]["record"]))

    def test_byte_totals_use_the_daemons_own_formatter(self):
        self.write_stats([44, 901326172, 21, 393945884, 13, 27724720,
                          db.datetime.datetime.now().strftime("%Y-%m-%d")])

        sent = webserver.build_stats_payload()["sent"]

        for name in ("total", "today", "yesterday"):
            with self.subTest(column=name):
                self.assertEqual(sent[name + "_text"],
                                 stats_mgr.format_size_human(sent[name + "_bytes"]))

    def test_uptime_uses_the_admin_consoles_own_formatter(self):
        payload = webserver.build_stats_payload()

        self.assertEqual(payload["transfer"]["uptime_text"],
                         adminchat.format_uptime(payload["transfer"]["uptime_seconds"]))

    def test_the_raw_number_is_there_as_well(self):
        """A script or a future CTCP reply should not have to parse "1.21TB"
        back into an integer."""
        payload = webserver.build_stats_payload()

        self.assertIsInstance(payload["transfer"]["speed_now"], int)
        self.assertIsInstance(payload["transfer"]["record"], int)
        self.assertIsInstance(payload["library"]["raw_bytes"], int)


class TodayMeansToday(StatsCase):
    """The daemon rotates the day when a transfer completes. A bot that has
    sent nothing since midnight therefore still has yesterday's figures sitting
    in the Today columns, and a page reading the row raw would label them
    Today - wrong, and wrong in the direction that flatters the bot."""

    def yesterdays_date(self):
        return (db.datetime.datetime.now()
                - db.datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    def test_a_stale_row_does_not_report_yesterday_as_today(self):
        self.write_stats([44, 901326172, 2, 2000, 21, 393945884,
                          self.yesterdays_date()])

        sent = webserver.build_stats_payload()["sent"]

        self.assertEqual(sent["today_files"], 0)
        self.assertEqual(sent["today_bytes"], 0)

    def test_the_stale_today_becomes_yesterday(self):
        """It is not discarded - it really was yesterday's traffic."""
        self.write_stats([44, 901326172, 2, 2000, 21, 393945884,
                          self.yesterdays_date()])

        sent = webserver.build_stats_payload()["sent"]

        self.assertEqual(sent["yesterday_files"], 21)
        self.assertEqual(sent["yesterday_bytes"], 393945884)

    def test_a_current_row_is_reported_as_it_stands(self):
        today = db.datetime.datetime.now().strftime("%Y-%m-%d")
        self.write_stats([44, 901326172, 21, 393945884, 13, 27724720, today])

        sent = webserver.build_stats_payload()["sent"]

        self.assertEqual(sent["today_files"], 13)
        self.assertEqual(sent["today_bytes"], 27724720)
        self.assertEqual(sent["total_files"], 44)

    def test_reading_the_page_does_not_write_to_disk(self):
        """check_and_rotate_day() is the writer and the daemon calls it when a
        transfer completes. A dashboard answering a GET must not be a second
        writer - two of them racing is how the counters get lost."""
        self.write_stats([44, 901326172, 2, 2000, 21, 393945884,
                          self.yesterdays_date()])
        with io.open(config.STATS_FILE, encoding="utf-8") as handle:
            before = handle.read()

        webserver.build_stats_payload()

        with io.open(config.STATS_FILE, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before,
                             "answering /api/stats rewrote stats.txt")


class CountingAlbumFolders(StatsCase):

    def test_only_the_request_lines_are_counted(self):
        """The file opens with three lines of explanation. Counting the file's
        length would report three albums that do not exist."""
        self.write_rar_list(4107)

        self.assertEqual(webserver.build_stats_payload()["library"]["rar_folders"], 4107)

    def test_the_newest_list_is_the_one_counted(self):
        """os.listdir gives no ordering guarantee, so the newest is chosen by
        sorting the names. This stubs listdir to hand them back oldest-last -
        without the sort the test would pass on any filesystem that happens to
        return them in order, which is most of them, and that is exactly how a
        missing sort survives into production."""
        self.write_rar_list(10, "DCCoreWin-RAR-2026-08-20.txt")
        self.write_rar_list(4107, "DCCoreWin-RAR-2026-08-27.txt")

        real_listdir = os.listdir
        self.addCleanup(setattr, os, "listdir", real_listdir)
        os.listdir = lambda path: list(reversed(sorted(real_listdir(path))))

        self.assertEqual(webserver.build_stats_payload()["library"]["rar_folders"], 4107)

    def test_no_list_at_all_is_unknown_rather_than_zero(self):
        """A bot whose first list has not been built has an unknown album
        count. Zero is a different claim, and the page would show it as one."""
        self.assertIsNone(webserver.build_stats_payload()["library"]["rar_folders"])


class OneBadFileCostsOneTile(StatsCase):
    """This is a read-only status page. A missing stats file, an unbuilt list
    or a permissions error must cost the tile that needs it and nothing else -
    a dashboard that 500s because one counter is unreadable is worse than one
    showing a gap."""

    def test_a_missing_stats_file(self):
        payload = webserver.build_stats_payload()

        self.assertEqual(payload["sent"]["total_files"], 0)
        self.assertEqual(payload["transfer"]["slots"], 3)

    def test_a_corrupt_stats_file(self):
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write("this is not a stats row")

        payload = webserver.build_stats_payload()

        self.assertEqual(payload["sent"]["total_files"], 0)
        self.assertIn("version", payload)

    def test_a_short_stats_row(self):
        """Fewer than seven columns - what a torn write leaves behind."""
        self.write_stats([44, 901326172])

        payload = webserver.build_stats_payload()

        self.assertEqual(payload["sent"]["yesterday_files"], 0)

    def test_a_lists_directory_that_cannot_be_listed(self):
        self.set_config(LOCAL_LIST_DIR=os.path.join(self.tree.root, "not-there"))

        payload = webserver.build_stats_payload()

        self.assertIsNone(payload["library"]["rar_folders"])
        self.assertIn("transfer", payload)

    def test_every_key_the_page_reads_is_always_present(self):
        """The page indexes these without guarding each one, so a key that
        disappears when a file is missing is a blank tile at best."""
        payload = webserver.build_stats_payload()

        for group, keys in (
                ("transfer", ("speed_now", "speed_now_text", "record", "record_text",
                              "sending", "slots", "queued_files", "queued_users",
                              "uptime_seconds", "uptime_text")),
                ("sent", ("total_files", "total_bytes", "total_text",
                          "today_files", "today_bytes", "today_text",
                          "yesterday_files", "yesterday_bytes", "yesterday_text")),
                ("library", ("files", "size", "raw_bytes", "list_date", "rar_folders"))):
            for key in keys:
                with self.subTest(group=group, key=key):
                    self.assertIn(key, payload[group])


class TheLiveCountersComeFromTheLiveContainers(StatsCase):

    def test_sending_and_queued_are_read_from_the_daemons_own_state(self):
        config.active_transfers.append({"user": "dave", "file": "a.flac"})
        config.dcc_queue["dave"] = ["b.flac", "c.flac"]
        config.dcc_queue["sam"] = ["d.flac"]

        transfer = webserver.build_stats_payload()["transfer"]

        self.assertEqual(transfer["sending"], 1)
        self.assertEqual(transfer["queued_files"], 3)
        self.assertEqual(transfer["queued_users"], 2)

    def test_slots_come_from_config(self):
        self.set_config(MAX_DCC_SLOTS=9)

        self.assertEqual(webserver.build_stats_payload()["transfer"]["slots"], 9)


class ItStillDoesNotImportTheDaemon(unittest.TestCase):
    """tests/test_import_graph.py pins this for webserver.py as a whole. This
    is the same rule aimed at the one function most likely to break it: the
    Stats payload needs db, list and stats_mgr, and all three are named in that
    test as things importing webserver must not pull in."""

    def test_the_daemon_imports_are_inside_the_function(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        top_level = [line for line in lines
                     if line.startswith("import ") or line.startswith("from ")]

        for module in ("db", "list", "stats_mgr"):
            with self.subTest(module=module):
                self.assertNotIn("import " + module, top_level,
                                 "webserver.py imports %s at module scope, which "
                                 "breaks testing the routes without a daemon" % module)

    def test_the_payload_builder_imports_them_itself(self):
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()

        body = source[source.index("def build_stats_payload("):]
        body = body[:body.index("\ndef ", 1)]

        for needle in ("import db", "import list as list_mod", "import stats_mgr"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)


if __name__ == "__main__":
    unittest.main()
