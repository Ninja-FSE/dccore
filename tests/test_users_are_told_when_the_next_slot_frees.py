"""Users are told when the next slot is likely to free up (#1207).

The question a busy channel asks most is "when do I get a slot?", and DCCore
had no answer to it: -que and -stats gave counts and no time, and the CTCP
SLOTS line's "next" field is a literal NOW or 0.

THE ESTIMATE is stats_mgr.next_slot_estimate(): "now" while a slot is free;
otherwise the minimum over the active sends of (size - sent) / speed, rounded
up to whole minutes - when the first busy slot frees. Unknown (None) while no
send has a usable speed. The speed is stats_mgr.send_speed(), the per-send
figure the DCC console's SLOT line already showed, moved out of adminchat so
there is one measure rather than two.

IT IS THE NEXT FREE SLOT, NOT THIS USER'S TURN, and every text says "next
free slot": who gets that slot is the queue's business.

SHOWN IN @nick-que (both replies), @nick-stats next to the free slots, and
the dashboard's Live Transfers page.

THE CTCP SLOTS LINE IS LEFT ALONE, byte for byte. Other scripts parse it, and
nothing says its "next" field accepts minutes - so a test below drives one
real advert cycle with every slot busy and a known estimate, and pins the
whole line.
"""

import io
import os
import re
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import stats_mgr  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

NOW = 10_000.0
CHANNEL = "#somechannel"


def send(size, sent, seconds_ago, resume=0, now=NOW, user="someuser"):
    """An active_transfers row as dcc.start_dcc_send() leaves it."""
    row = {"user": user, "file": "Artist - Song.flac", "bytes_sent": sent,
           "size": size, "started_at": now - seconds_ago}
    if resume:
        row["resume_offset"] = resume
    return row


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def plain(text):
    """The words, with the colour codes taken off."""
    codes = re.compile("[" + chr(2) + chr(15) + "]|" + chr(3) + r"\d{0,2}(,\d{1,2})?")
    return codes.sub("", text)


class TheEstimate(unittest.TestCase):

    def estimate(self, transfers, slots=2):
        return stats_mgr.next_slot_estimate(now=NOW, transfers=transfers, slots=slots)

    def test_a_free_slot_is_now(self):
        self.assertEqual(self.estimate([send(10_000_000, 1, 100)]), stats_mgr.NEXT_SLOT_NOW)
        self.assertEqual(stats_mgr.NEXT_SLOT_NOW, "now")

    def test_a_free_slot_is_now_even_with_no_speed_known(self):
        self.assertEqual(self.estimate([{"user": "a", "bytes_sent": 0}]), "now")
        self.assertEqual(self.estimate([]), "now")

    def test_minutes_from_the_size_left_and_the_speed(self):
        """40 kB/s with 6 MB left is 150 seconds: three minutes, rounded up."""
        self.assertEqual(self.estimate([send(10_000_000, 4_000_000, 100)], slots=1), 3)

    def test_an_exact_number_of_minutes_is_not_rounded_up_further(self):
        """40 kB/s with 4.8 MB left is exactly 120 seconds."""
        self.assertEqual(self.estimate([send(8_800_000, 4_000_000, 100)], slots=1), 2)

    def test_it_is_the_send_nearest_its_end_whichever_comes_first(self):
        slow = send(100_000_000, 1_000_000, 100)      # 10 kB/s, 9900 s left
        quick = send(10_000_000, 4_000_000, 100)      # 40 kB/s, 150 s left
        self.assertEqual(self.estimate([slow, quick]), 3)
        self.assertEqual(self.estimate([quick, slow]), 3)

    def test_a_resumed_send_is_timed_by_what_this_connection_moved(self):
        """Resumed at 50 MB, 6 MB moved in ten minutes: 10 kB/s, so 44 MB
        left is 4400 s - 74 minutes. Counting the resumed part as moved would
        say 8."""
        row = send(100_000_000, 56_000_000, 600, resume=50_000_000)
        self.assertEqual(self.estimate([row], slots=1), 74)

    def test_a_busy_slot_is_never_less_than_a_minute(self):
        """A send a few bytes from its end is not a free slot."""
        self.assertEqual(self.estimate([send(4_000_010, 4_000_000, 100)], slots=1), 1)
        self.assertEqual(self.estimate([send(4_000_000, 4_000_000, 100)], slots=1), 1)

    def test_unknown_while_no_send_has_a_speed(self):
        for row in ({"user": "a", "file": "x", "bytes_sent": 0},          # not stamped yet
                    send(10_000_000, 0, 100),                           # nothing moved
                    send(10_000_000, 500, 0.2),                         # half a second is not a rate
                    {"user": "a", "file": "x", "bytes_sent": 5000,       # no size known
                     "started_at": NOW - 100}):
            with self.subTest(row=row):
                self.assertIsNone(self.estimate([row], slots=1))

    def test_a_send_with_no_speed_does_not_hide_one_that_has(self):
        fresh = {"user": "a", "file": "x", "bytes_sent": 0}
        self.assertEqual(self.estimate([fresh, send(10_000_000, 4_000_000, 100)]), 3)

    def test_a_row_that_is_not_a_send_holds_a_slot_and_nothing_else(self):
        """The console's STATUS burst skips such a row too; here it still
        counts as busy, and must not take the -que reply down with it."""
        self.assertEqual(self.estimate(["junk", send(10_000_000, 4_000_000, 100)]), 3)

    def test_a_malformed_send_is_skipped_not_raised(self):
        bad = {"user": "a", "file": "x", "bytes_sent": 5, "size": 10, "started_at": "abc"}
        self.assertIsNone(self.estimate([bad], slots=1))
        self.assertEqual(self.estimate([bad, send(10_000_000, 4_000_000, 100)]), 3)

    def test_by_default_it_reads_the_live_transfers_and_the_slot_count(self):
        saved = (list(config.active_transfers), config.MAX_DCC_SLOTS)
        self.addCleanup(setattr, config, "MAX_DCC_SLOTS", saved[1])
        self.addCleanup(config.active_transfers.__setitem__, slice(None), saved[0])
        config.active_transfers[:] = [send(10_000_000, 4_000_000, 100)]

        config.MAX_DCC_SLOTS = 1
        self.assertEqual(stats_mgr.next_slot_estimate(now=NOW), 3)
        config.MAX_DCC_SLOTS = 2
        self.assertEqual(stats_mgr.next_slot_estimate(now=NOW), "now")


class TheWords(unittest.TestCase):

    def test_each_kind_of_answer(self):
        cases = {"now": "now", None: "not known yet", 1: "~1 min", 4: "~4 min",
                 59: "~59 min", 60: "~1h 0m", 130: "~2h 10m", 1439: "~23h 59m",
                 1440: "~1d 0h", 1620: "~1d 3h"}
        for estimate, words in cases.items():
            with self.subTest(estimate=estimate):
                self.assertEqual(stats_mgr.format_next_slot(estimate), words)


class OneSpeedMeasureNotTwo(DCCoreTestCase):
    """The console's SLOT line and the estimate read the same per-send speed."""

    def test_the_console_slot_line_reads_send_speed(self):
        config.active_transfers[:] = [send(10_000, 5_000, 2, now=1000.0, user="erin")]
        real = stats_mgr.send_speed
        self.addCleanup(setattr, stats_mgr, "send_speed", real)
        stats_mgr.send_speed = lambda tx, now=None: 12345

        line = adminchat.status_lines(now=1000.0)[1]

        self.assertEqual(line, "DCCORE SLOT erin 5000 10000 12345 Artist - Song.flac")

    def test_send_speed_is_what_this_connection_moved_per_second(self):
        self.assertEqual(stats_mgr.send_speed(send(10_000, 5_000, 2), NOW), 2500)
        self.assertEqual(stats_mgr.send_speed(send(10_000, 900, 10, resume=800), NOW), 10)
        self.assertEqual(stats_mgr.send_speed(send(10_000, 100, 10, resume=5000), NOW), 0)
        self.assertEqual(stats_mgr.send_speed({"bytes_sent": 5}, NOW), 0)

    def test_a_malformed_row_still_fails_the_console_burst_as_before(self):
        """#681 pins that the STATUS burst reports such a row as a failure;
        moving the arithmetic must not quietly turn it into a 0."""
        with self.assertRaises(ValueError):
            stats_mgr.send_speed({"bytes_sent": 5, "started_at": "abc"}, NOW)


class UserNoticeCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", MAX_DCC_SLOTS=2)
        self.patch(list_mod, "get_file_count_date_size_and_raw_bytes",
                   lambda *a, **k: (1234, "2026-01-01", "1.0GB", 1073741824))

    def patch(self, owner, name, value):
        self.addCleanup(setattr, owner, name, getattr(owner, name))
        setattr(owner, name, value)

    def busy(self, *rows):
        """Every row a running send; active_downloads follows, as dcc.py keeps it."""
        config.active_transfers[:] = list(rows)
        self.oserve.active_downloads = len(rows)

    def both_slots_busy_three_minutes_out(self):
        now = time.time()
        self.busy(send(10_000_000, 4_000_000, 100, now=now, user="a"),
                  send(100_000_000, 1_000_000, 100, now=now, user="b"))

    def answers(self, handler, user="dave"):
        self.oserve.queued.clear()
        handler(None, user, CHANNEL)
        return [message for _user, message, _vip in self.oserve.queued]


class TheQueueCommand(UserNoticeCase):

    def que(self):
        lines = self.answers(commands.handle_queue_check)
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_with_files_queued_it_says_when_the_next_slot_frees(self):
        config.dcc_queue["dave"] = [{"file": "a.flac"}, {"file": "b.flac"}]
        self.both_slots_busy_three_minutes_out()

        line = self.que()

        self.assertTrue(line.startswith("NOTICE dave :"), line)
        self.assertIn("You have 2 files in queue. Next free slot: ~3 min. To remove your entire queue",
                      plain(line))
        self.assertIn(f"Next free slot: {config.C_BOLD}{config.C_GREEN}~3 min{config.C_RESET}. ", line)

    def test_with_nothing_queued_it_says_so_too(self):
        self.both_slots_busy_three_minutes_out()

        text = plain(self.que())

        self.assertIn("You have 0 files in queue.", text)
        self.assertIn("Slots 0/2. Next free slot: ~3 min. Queue ", text)

    def test_a_free_slot_is_now(self):
        self.busy(send(10_000_000, 4_000_000, 100, now=time.time()))

        self.assertIn("Slots 1/2. Next free slot: now. Queue ", plain(self.que()))

    def test_no_speed_yet_is_said_plainly(self):
        config.dcc_queue["dave"] = [{"file": "a.flac"}]
        self.busy({"user": "a", "file": "x", "bytes_sent": 0},
                  {"user": "b", "file": "y", "bytes_sent": 0})

        self.assertIn("files in queue. Next free slot: not known yet. To remove", plain(self.que()))

    def test_it_never_promises_the_asker_a_turn(self):
        config.dcc_queue["dave"] = [{"file": "a.flac"}]
        self.both_slots_busy_three_minutes_out()

        text = plain(self.que()).lower()

        self.assertNotIn("your turn", text)
        self.assertNotIn("your slot", text)


class TheStatsCommand(UserNoticeCase):

    def setUp(self):
        super().setUp()
        self.patch(stats_mgr, "get_total_sent", lambda: 3)
        self.patch(stats_mgr, "get_total_sent_bytes", lambda: 3_000_000)
        self.patch(stats_mgr, "live_speed", lambda now=None: 0)
        self.patch(db, "get_speed_record", lambda: 0)
        self.patch(db, "load_advanced_stats_rolled",
                   lambda: [3, 3_000_000, 1, 1_000_000, 2, 2_000_000, "2026-01-01"])

    def slots_line(self):
        lines = [plain(line) for line in self.answers(commands.handle_stats_request)]
        found = [line for line in lines if " :Slots " in line]
        self.assertEqual(len(found), 1, lines)
        return found[0]

    def test_the_next_free_slot_sits_next_to_the_free_slots(self):
        self.both_slots_busy_three_minutes_out()

        self.assertIn(":Slots 0/2 free, next free slot ~3 min, 0 queued. Speed ", self.slots_line())

    def test_a_free_slot_is_now(self):
        self.busy(send(10_000_000, 4_000_000, 100, now=time.time()))

        self.assertIn(":Slots 1/2 free, next free slot now, 0 queued.", self.slots_line())

    def test_no_speed_yet(self):
        self.busy({"user": "a", "file": "x", "bytes_sent": 0},
                  {"user": "b", "file": "y", "bytes_sent": 0})

        self.assertIn("next free slot not known yet, 0 queued.", self.slots_line())

    def test_the_figure_has_the_same_colour_as_the_others(self):
        self.both_slots_busy_three_minutes_out()
        lines = self.answers(commands.handle_stats_request)

        self.assertTrue(any(f"next free slot {config.C_BOLD}{config.C_GREEN}~3 min{config.C_RESET}, "
                            in line for line in lines), lines)


class TheCtcpSlotsLineIsUnchanged(UserNoticeCase):
    """Other scripts parse the SLOTS line. One real advert cycle, every slot
    busy with a known estimate, and the line must be exactly what it was
    before the estimate existed: the "next" field stays 0 (NOW with a slot
    free), never minutes."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL, ANNOUNCE_INTERVAL=0.01, SCRIPT_VERSION="DCCore-test")
        self.patch(announce, "current_worker_id", announce.current_worker_id)
        self.patch(announce, "is_ready", announce.is_ready)
        self.patch(library, "list_name_for_request", lambda channel=None: "main")
        self.patch(stats_mgr, "live_speed", lambda now=None: 5000)
        self.patch(stats_mgr, "get_total_sent_bytes", lambda: 3 * 1024 * 1024)
        self.patch(db, "get_speed_record", lambda: 0)
        self.patch(announce, "get_formatted_stats_strings", lambda: ("3", "1", "2"))

    def slots_line(self):
        announce.is_ready = True
        thread = threading.Thread(target=announce.announce_worker, daemon=True)
        thread.start()
        try:
            def found():
                return [m for _u, m, _v in self.oserve.queued if "\x01SLOTS " in m]
            self.assertTrue(wait_for(found), f"no SLOTS line was sent: {self.oserve.queued!r}")
            return found()[0]
        finally:
            announce.current_worker_id = object()
            thread.join(5)

    def test_every_slot_busy_with_a_known_estimate(self):
        self.both_slots_busy_three_minutes_out()
        self.assertEqual(stats_mgr.next_slot_estimate(), 3)    # there IS an estimate

        self.assertEqual(
            self.slots_line(),
            f"PRIVMSG {CHANNEL} :\x01SLOTS 2 0 0 0 999 5000 1234 1073741824 0 3 3145728 DCCore-test\x01\r\n")

    def test_a_free_slot(self):
        self.busy(send(10_000_000, 4_000_000, 100, now=time.time()))

        self.assertEqual(
            self.slots_line(),
            f"PRIVMSG {CHANNEL} :\x01SLOTS 2 1 NOW 0 999 5000 1234 1073741824 0 3 3145728 DCCore-test\x01\r\n")


class TheLiveTransfersPage(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(MAX_DCC_SLOTS=1)

    def transfer(self):
        return webserver.build_stats_payload(parts=("transfer",))["transfer"]

    def test_minutes(self):
        config.active_transfers[:] = [send(10_000_000, 4_000_000, 100, now=time.time())]

        tr = self.transfer()

        self.assertEqual((tr["next_slot"], tr["next_slot_text"]), (3, "~3 min"))

    def test_now(self):
        config.active_transfers[:] = []
        self.assertEqual(self.transfer()["next_slot"], "now")

    def test_unknown(self):
        config.active_transfers[:] = [{"user": "a", "file": "x", "bytes_sent": 0}]
        tr = self.transfer()
        self.assertIsNone(tr["next_slot"])
        self.assertEqual(tr["next_slot_text"], "not known yet")


class ThePageDrawsIt(unittest.TestCase):

    @staticmethod
    def read(*parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_a_card_on_live_transfers(self):
        html = self.read("web", "index.html")
        live = html.split('id="view-live"', 1)[1].split('<section class="view"', 1)[0]
        self.assertIn('<div class="stat-value" id="st-next-slot">&mdash;</div>'
                      '<div class="stat-label" data-i18n="stats.nextSlotLabel">Next free slot</div>', live)

    def test_the_transfer_figures_fill_it(self):
        js = self.read("web", "app.js")
        render = js.split("function renderTransfer(tr) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("setStat(el.stNextSlot, nextSlotText(tr));", render)
        self.assertIn('stNextSlot:            document.getElementById("st-next-slot"),', js)

    def test_now_and_unknown_are_translated_and_a_time_is_the_servers(self):
        js = self.read("web", "app.js")
        body = js.split("function nextSlotText(tr) {", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn('if (tr.next_slot === "now") { return t("stats.nextSlotNow"); }', body)
        self.assertIn('if (tr.next_slot === null || tr.next_slot === undefined) '
                      '{ return t("stats.nextSlotUnknown"); }', body)
        self.assertIn('return tr.next_slot_text || t("stats.nextSlotUnknown");', body)


if __name__ == "__main__":
    unittest.main()
