"""The advert's Yesterday and Today figures, on a bot that has been idle.

stats.txt carries seven columns and a date. The day is only rotated on disk
when a transfer COMPLETES - db.check_and_rotate_day() is the writer, and
nothing else calls it - so a bot that has sent nothing since midnight still
has yesterday's numbers sitting in the Today columns and the day before's in
the Yesterday columns.

The advert read that row raw, so it announced both under the wrong label.
Every five minutes, in every channel, on any bot quiet enough to have sent
nothing yet - which is every bot, every morning.

db.load_advanced_stats_rolled() has existed since #144 for exactly this, and
the web dashboard has used it since. Its docstring names this failure: "wrong,
and wrong in the direction that flatters the bot". The advert is where the
flattery is public.
"""

import re
import io
import os
import tempfile
import time
import unittest

from tests.support import DCCoreTestCase, silence_debug

import announce
import db

# What a bot looks like that sent 583 files yesterday and has not started
# today: total, total_bytes, yesterday_files, yesterday_bytes, today_files,
# today_bytes, last_date.
IDLE_SINCE_MIDNIGHT = "3945009 4100000000000 120 900000000 583 700000000 {date}"


class DayFigureCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        stats_dir = tempfile.mkdtemp(prefix="dccore-stats-")
        self.addCleanup(_rmtree, stats_dir)
        self.set_config(STATS_FILE=os.path.join(stats_dir, "stats.txt"))

    def write_stats(self, days_ago):
        """A stats row last touched `days_ago` days back."""
        stamp = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() - days_ago * 86400))
        with io.open(self.config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write(IDLE_SINCE_MIDNIGHT.format(date=stamp))
        db._advanced_stats_cache = None
        return stamp


# Colour codes are about to become configurable (the theme write-up on #69),
# so these tests read the words and not the palette.
MIRC_CODES = re.compile("[" + chr(2) + chr(15) + chr(22) + chr(31) + "]|"
                        + chr(3) + r"\d{0,2}(,\d{1,2})?")


def plain(line):
    """The line with every mIRC formatting code removed."""
    return MIRC_CODES.sub("", line)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class TheAdvertsOwnFigures(DayFigureCase):
    """announce.get_formatted_stats_strings() - what the five-minute channel
    advertisement prints."""

    def test_a_bot_idle_since_midnight_reports_nothing_sent_today(self):
        """The headline defect. 583 files were sent yesterday; the advert
        announced them as today's."""
        self.write_stats(days_ago=1)

        _total, yesterday, today = announce.get_formatted_stats_strings()

        self.assertTrue(today.startswith("0 Files"), today)

    def test_and_moves_them_into_yesterday_where_they_belong(self):
        self.write_stats(days_ago=1)

        _total, yesterday, _today = announce.get_formatted_stats_strings()

        self.assertEqual(yesterday, "583 Files")

    def test_a_bot_that_has_sent_something_today_is_unchanged(self):
        """The control. Rolling must not move figures that are current - this
        is the ordinary case, and it is the one that already worked."""
        self.write_stats(days_ago=0)

        _total, yesterday, today = announce.get_formatted_stats_strings()

        self.assertEqual(yesterday, "120 Files")
        self.assertTrue(today.startswith("583 Files"), today)

    def test_a_bot_idle_for_a_week_reports_zero_for_both(self):
        """Rotation is not a one-day shift. Anything older than yesterday is
        gone, and claiming last Tuesday's traffic as yesterday's would be the
        same lie one step further out."""
        self.write_stats(days_ago=7)

        _total, yesterday, today = announce.get_formatted_stats_strings()

        self.assertEqual(yesterday, "0 Files")
        self.assertTrue(today.startswith("0 Files"), today)

    def test_the_lifetime_total_is_never_touched(self):
        """Rotation moves the day buckets and nothing else. A total that
        changed with the calendar would be a far worse bug than the one being
        fixed."""
        self.write_stats(days_ago=1)

        total, _yesterday, _today = announce.get_formatted_stats_strings()

        self.assertIn("3,945,009", total)


class ItStillDoesNotWriteToDisk(DayFigureCase):
    """get_formatted_stats_strings() is called from the advert loop, which
    runs whether or not anything is happening. Rotating on disk from there
    would move the write out of the one place that owns it, and a bot with a
    read-only or full data directory would start logging failures on a timer.
    """

    def test_reading_the_advert_figures_leaves_the_file_alone(self):
        stamp = self.write_stats(days_ago=1)
        before = io.open(self.config.STATS_FILE, encoding="utf-8").read()

        announce.get_formatted_stats_strings()

        after = io.open(self.config.STATS_FILE, encoding="utf-8").read()
        self.assertEqual(before, after)
        self.assertIn(stamp, after, "the stored date moved")

    def test_and_so_does_the_transfer_notice(self):
        self.addCleanup(setattr, announce, "send_debug", announce.send_debug)
        silence_debug(announce)
        self.write_stats(days_ago=1)
        before = io.open(self.config.STATS_FILE, encoding="utf-8").read()

        announce.send_transfer_complete("#dccore-test", "dave", "track.flac",
                                        4096, time.time() - 2, 512000)

        self.assertEqual(io.open(self.config.STATS_FILE, encoding="utf-8").read(), before)


class TheTransferNoticeCarriesTheSameFigures(DayFigureCase):
    """send_transfer_complete() prints Yesterday and Today too, from its own
    read of the same row. Two readers of one file that label it differently is
    the second-list problem this codebase keeps getting bitten by - and here
    both lines land in the same channel, minutes apart."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, announce, "send_debug", announce.send_debug)
        silence_debug(announce)

    def announced(self):
        self.assertEqual(len(self.oserve.queued), 1, "expected exactly one announcement")
        return self.oserve.queued[0][1]

    def test_it_does_not_announce_yesterdays_traffic_as_todays(self):
        self.write_stats(days_ago=1)

        announce.send_transfer_complete("#dccore-test", "dave", "track.flac",
                                        4096, time.time() - 2, 512000)

        line = self.announced()

        self.assertIn("Today: 0 Files", plain(line))
        self.assertIn("Yesterday: 583 Files", plain(line))

    def test_the_two_lines_agree(self):
        """Whatever the numbers are, the advert and the completion notice must
        not disagree about which day they belong to."""
        self.write_stats(days_ago=1)

        _total, yesterday, today = announce.get_formatted_stats_strings()
        announce.send_transfer_complete("#dccore-test", "dave", "track.flac",
                                        4096, time.time() - 2, 512000)
        line = self.announced()
        self.assertIn(yesterday, plain(line), 
                      "the completion notice disagrees about Yesterday")
        self.assertIn(today.split(" [")[0], plain(line),
                      "the completion notice disagrees about Today")


if __name__ == "__main__":
    unittest.main()
