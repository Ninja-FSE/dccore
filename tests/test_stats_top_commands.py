"""@<nick>-stats and @<nick>-top - the numbers, on demand and in private.

The advert carries most of what -stats reports, but it fires on a timer into a
busy channel: somebody who missed it, or who is in a PM, had no way to ask.
-top is not published anywhere except the web dashboard, which only the
operator can see.

What can actually go wrong here is not the arithmetic:

  * -top prints names straight off the operator's disk, five to a line. A
    library of Bjork and Sigur Ros costs two bytes a letter and a Japanese one
    three, so a clamp measured in CHARACTERS puts the line past 512 and the
    server cuts it mid-colour-code;
  * -stats reading the day row raw would answer a different number from the
    same bot's dashboard, and from its own advert;
  * offering a menu of albums on a bot that refuses !rar - the mistake #153
    took out of the album list.
"""

import io
import json
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

# 583 files sent on the stored date, 120 the day before.
STATS_ROW = "3945009 4100000000000 120 900000000 583 700000000 {date}"


class CommandCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        RAR_ENABLED=True, MAX_DCC_SLOTS=5,
                        STATS_FILE=os.path.join(self.tree.root, "stats.txt"),
                        LOCAL_LIST_DIR=self.tree.lists)
        for name in ("DOWNLOAD_COUNTS_FILE", "SPEED_RECORD_FILE"):
            self.addCleanup(setattr, db, name, getattr(db, name))
        db.DOWNLOAD_COUNTS_FILE = os.path.join(self.tree.root, "counts.json")
        db.SPEED_RECORD_FILE = os.path.join(self.tree.root, "speed.txt")
        self.write_stats(days_ago=0)

    def write_stats(self, days_ago):
        stamp = time.strftime("%Y-%m-%d",
                              time.localtime(time.time() - days_ago * 86400))
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write(STATS_ROW.format(date=stamp))
        db._advanced_stats_cache = None

    def write_counts(self, name="Metallica - Enter Sandman.flac", rows=5, kinds=("file", "album")):
        counts = {}
        for kind in kinds:
            for index in range(rows):
                counts[f"{kind}{index}"] = {"name": name, "kind": kind,
                                            "count": 4123000 - index}
        with io.open(db.DOWNLOAD_COUNTS_FILE, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(counts))

    def ask(self, handler, user="dave", target="#dccore-test"):
        self.oserve.queued.clear()
        handler(None, user, target)
        return [message for _user, message, _vip in self.oserve.queued]

    def stats(self, **kwargs):
        return self.ask(commands.handle_stats_request, **kwargs)

    def top(self, **kwargs):
        return self.ask(commands.handle_top_request, **kwargs)

    def plain(self, lines):
        """The words, with the colour codes and the envelope taken off."""
        import re
        codes = re.compile("[" + chr(2) + chr(15) + "]|" + chr(3) + r"\d{0,2}(,\d{1,2})?")
        return " ".join(codes.sub("", line.split(":", 1)[1]).strip() for line in lines)


class ItAnswersThePersonNotTheChannel(CommandCase):

    def test_stats_goes_only_to_the_asker(self):
        for line in self.stats(user="dave", target="#dccore-test"):
            with self.subTest(line=line[:40]):
                self.assertTrue(line.startswith("NOTICE dave :"), line)
                self.assertNotIn("#dccore-test", line)

    def test_top_goes_only_to_the_asker(self):
        self.write_counts()

        for line in self.top(user="dave", target="#dccore-test"):
            with self.subTest(line=line[:40]):
                self.assertTrue(line.startswith("NOTICE dave :"), line)
                self.assertNotIn("#dccore-test", line)

    def test_both_go_out_on_the_vip_lane(self):
        """Same lane as -help and -que: an answer to a direct question should
        not queue behind a channel advert."""
        self.write_counts()
        for handler in (commands.handle_stats_request, commands.handle_top_request):
            with self.subTest(handler=handler.__name__):
                self.ask(handler)

                self.assertTrue(all(vip for _u, _m, vip in self.oserve.queued))


class WhatStatsReports(CommandCase):

    def test_it_carries_the_lifetime_total(self):
        self.assertIn("3,945,009", self.plain(self.stats()))

    def test_it_carries_the_day_figures(self):
        body = self.plain(self.stats())

        self.assertIn("120 yesterday", body)
        self.assertIn("583 today", body)

    def test_a_bot_idle_since_midnight_does_not_report_yesterday_as_today(self):
        """The row is only rotated on disk when a transfer completes, so a bot
        that has sent nothing since midnight still has yesterday's numbers in
        the Today columns. Reading it raw would make this bot answer one number
        here and a different one on its own dashboard."""
        self.write_stats(days_ago=1)

        body = self.plain(self.stats())

        self.assertIn("583 yesterday", body)
        self.assertIn("0 today", body)

    def test_it_carries_the_speed_record(self):
        with io.open(db.SPEED_RECORD_FILE, "w", encoding="utf-8") as handle:
            handle.write("1258291")

        self.assertIn("1.20MB/s", self.plain(self.stats()))

    def test_it_carries_the_free_slots(self):
        self.assertIn("5/5 free", self.plain(self.stats()))

    def test_a_bot_with_no_list_says_so_rather_than_printing_a_non_date(self):
        """get_file_count_date_size_and_raw_bytes() answers "No List" as the
        DATE, which read as a date in the sentence it was going into. This is
        the state every fresh install is in and the first thing somebody would
        ask about."""
        body = self.plain(self.stats())

        self.assertNotIn("built No List", body)
        self.assertIn("No list has been built yet", body)


class WhatTopReports(CommandCase):

    def test_it_lists_the_most_requested_files(self):
        self.write_counts(name="Metallica - Enter Sandman.flac")

        body = self.plain(self.top())

        self.assertIn("Most requested files", body)
        self.assertIn("Enter Sandman", body)

    def test_it_lists_the_albums_separately(self):
        """A 700MB album and a 4MB track are not comparable, which is why
        db.top_downloads() counts them together and reports them apart."""
        self.write_counts()

        body = self.plain(self.top())

        self.assertIn("Most requested albums", body)

    def test_it_offers_no_albums_when_folder_packing_is_off(self):
        """#153's rule. A menu of albums on a bot that answers every !rar with
        "Folder packing is disabled" is the same defect the album list had."""
        self.write_counts()
        self.set_config(RAR_ENABLED=False)

        body = self.plain(self.top())

        self.assertNotIn("albums", body)
        self.assertIn("Most requested files", body, "the file half went too")

    def test_nothing_recorded_yet_reads_as_a_sentence(self):
        """Not an empty label. A bot that has sent nothing is a new bot, and
        "Most requested files:" followed by nothing reads as a fault."""
        body = self.plain(self.top())

        self.assertNotIn("Most requested", body)
        self.assertIn("Nothing has been requested yet", body)

    def test_it_tells_a_newcomer_what_to_do_instead(self):
        self.assertIn(f"@{config.NICKNAME}", self.plain(self.top()))


class ItFitsOnTheWire(CommandCase):
    """announce.IRC_LINE_BUDGET is 420 for the pessimistic hostmask. -top
    prints names straight off the operator's disk, five to a line, so it is
    the command most able to blow past it."""

    def measured(self, lines):
        return [len(line.encode("utf-8")) for line in lines]

    def test_stats_fits(self):
        self.assertTrue(all(size < announce.IRC_LINE_BUDGET
                            for size in self.measured(self.stats(user="B" * 15))))

    def test_top_fits_with_long_ascii_names(self):
        self.write_counts(name="Ludwig van Beethoven - Symphony No. 9 in D minor, "
                               "Op. 125 'Choral' - IV. Finale - Presto (Karajan 1963 DG).flac")

        for size in self.measured(self.top(user="B" * 15)):
            with self.subTest(size=size):
                self.assertLess(size, announce.IRC_LINE_BUDGET)

    def test_top_fits_with_two_byte_names(self):
        """The clamp is in bytes, not characters. Clamped to 44 CHARACTERS,
        five of these came to 596 bytes against a 420 budget."""
        self.write_counts(name="Ä" * 80)

        for size in self.measured(self.top(user="B" * 15)):
            with self.subTest(size=size):
                self.assertLess(size, announce.IRC_LINE_BUDGET)

    def test_top_fits_with_three_byte_names(self):
        self.write_counts(name="アイ" * 40)

        for size in self.measured(self.top(user="B" * 15)):
            with self.subTest(size=size):
                self.assertLess(size, announce.IRC_LINE_BUDGET)

    def test_a_non_ascii_library_still_gets_the_whole_list(self):
        """What the byte clamp actually buys.

        The budget-aware assembly keeps the LINE safe whichever way the clamp
        counts - it just stops adding entries. So a clamp measured in
        characters does not overflow anything; it silently hands somebody with
        an umlaut-heavy library three of their five most-requested files, on
        the same 362-byte line that would have carried all five.
        """
        self.write_counts(name="Ä" * 80, rows=5, kinds=("file",))

        import re
        listed = re.findall(r"\d+\. ", self.plain(self.top(user="B" * 15)))

        self.assertEqual(len(listed), 5, "entries were dropped to make the names fit")

    def test_a_clamped_name_is_not_cut_through_a_character(self):
        """Half a character is not a shorter name, it is a box in everybody's
        client. The cut lands mid-character whenever the byte limit does not
        fall on a boundary, which for three-byte characters is most of the
        time."""
        clamped = commands._clamp_name("ア" * 40)

        self.assertTrue(clamped.endswith("..."), clamped)
        clamped.encode("utf-8").decode("utf-8")  # raises if a character was split
        self.assertNotIn("�", clamped)

    def test_a_short_name_is_left_alone(self):
        self.assertEqual(commands._clamp_name("Song.flac"), "Song.flac")

    def test_a_section_that_fits_nothing_is_not_sent_as_a_bare_label(self):
        """One name so long that not even the first entry fits. The label
        alone would read as "this bot has no popular files", which is a
        different claim from "the name would not fit"."""
        self.write_counts(name="x" * 4000, kinds=("file",))
        commands.TOP_NAME_BYTES, previous = 100000, commands.TOP_NAME_BYTES
        self.addCleanup(setattr, commands, "TOP_NAME_BYTES", previous)

        body = self.plain(self.top(user="B" * 15))

        self.assertNotIn("Most requested files:", body)


class ItIsReachable(unittest.TestCase):
    """A handler nothing dispatches answers nobody - the #119 shape."""

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_channel_dispatch_calls_both(self):
        source = self.source("irc.py")

        for handler in ("handle_stats_request", "handle_top_request"):
            with self.subTest(handler=handler):
                calls = [line for line in source.splitlines()
                         if handler in line and not line.strip().startswith("#")]
                self.assertTrue(calls, f"nothing routes {handler}, so asking does nothing")

    def test_both_are_metered_like_the_other_user_commands(self):
        """Sent by strangers, so they go in the anti-flood budget beside -que
        and -help. Left out, they would be the user commands anybody could
        repeat without limit - and -top reads a file on every call."""
        source = self.source("irc.py")
        block = source[source.index("is_bot_command = ("):]
        block = block[:block.index(")\n")]

        self.assertIn("-stats", block)
        self.assertIn("-top", block)
        self.assertIn("-que", block, "the block being checked is the wrong one")

    def test_the_help_notice_mentions_them(self):
        """A command nobody is told about is a command nobody types. -help is
        the one place a stranger finds out what this bot answers."""
        source = self.source("commands.py")
        helper = source[source.index("def handle_help_request"):]
        helper = helper[:helper.index("\ndef ")]

        self.assertIn("-stats", helper)
        self.assertIn("-top", helper)


if __name__ == "__main__":
    unittest.main()
