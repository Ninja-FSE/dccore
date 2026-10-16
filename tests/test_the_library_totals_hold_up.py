"""The library's totals hold up (#1367, the review of #1363).

The console's LIBRARY and SEARCHINDEX lines reached the start page with the
right figures, but: a fetch completing during a burst could take the lines out
of it, the index said `filling` for days after a repair, the burst asked for
at a rebuild's end missed its deadline and came 30 s later, a `library` that
joined a burst begun without the lines got none, a list that could not be
counted read as an empty one, a damaged summary could raise, a short rebuild
read "0 Min", a session that does not draw REBUILD lines was never sent the
new totals, the index counted lists by a rule of its own, and every burst read
lists.json six times per list.

Every nick, list name and path here is invented.
"""

import contextlib
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

ALFA = library.ServedList("Alfa Music", True, [], [])
BRAVO = library.ServedList("Bravo", False, ["#bravo"], [])


def held_list(count):
    return {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": count}


class TheIndexTotalsTakeACopy(DCCoreTestCase):
    """Item 1: a fetch completing while the burst's helper counts."""

    def test_a_fetch_landing_mid_count_changes_nothing_under_the_loop(self):
        held = {"botalfa": {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": 4,
                            "lists": {"RAR": {"list_path": "/x/a-rar.txt", "entry_count": 2}}},
                "botbravo": held_list(3)}
        real = list_fetch.index_key
        landed = []

        def index_key(bot, marker):
            # What the transfer thread does as a fetch completes.
            if not landed:
                landed.append(True)
                held["botnew"] = held_list(50)
                held["botalfa"]["lists"]["MORE"] = {"list_path": "/x/m.txt", "entry_count": 9}
            return real(bot, marker)

        with mock.patch.object(list_fetch, "index_key", index_key):
            totals = list_index.held_totals(held)
        self.assertTrue(landed)
        self.assertEqual(totals, (9, 3, "ok"))

    def test_the_lines_survive_an_index_that_cannot_be_counted(self):
        with mock.patch.object(library, "lists", return_value=[ALFA]), \
                mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes",
                                  return_value=(5, "Oct 1st", "5B", 5)), \
                mock.patch.object(list_mod, "read_library_summary",
                                  return_value={"folders": 1, "finished_at": 9, "seconds": 2}), \
                mock.patch.object(list_index, "held_totals", side_effect=RuntimeError("changed size")), \
                contextlib.redirect_stdout(io.StringIO()):
            lines = adminchat.library_lines()
        self.assertEqual(lines, ["DCCORE LIBRARY 5 1 5 9 2 1 Alfa Music"])


class FillingMeansABackfillIsRunning(DCCoreTestCase):
    """Item 2: `filling` while one runs, not while one is owed."""

    def setUp(self):
        super().setUp()
        self.addCleanup(runtime.list_index_backfill_done.set)
        self.addCleanup(setattr, list_index, "_rebuild_pending", list_index._rebuild_pending)
        self.held = {"botalfa": held_list(9)}

    def test_filling_while_a_backfill_indexes(self):
        seen = []

        def indexing(held, log):
            seen.append(list_index.held_totals(self.held)[2])
            return 0
        with mock.patch.object(list_index, "_backfill_missing", indexing):
            list_index.backfill_missing(self.held)
        self.assertEqual(seen, ["filling"])
        self.assertEqual(list_index.held_totals(self.held)[2], "ok")

    def test_a_backfill_that_fails_is_not_filling_for_ever(self):
        with mock.patch.object(list_index, "_backfill_missing", side_effect=OSError("gone")):
            with self.assertRaises(OSError):
                list_index.backfill_missing(self.held)
        self.assertEqual(list_index._backfills_running, 0)
        self.assertEqual(list_index.held_totals(self.held)[2], "ok")

    def test_a_repair_after_the_startup_backfill_is_not_filling_for_days(self):
        """The review's case: damaged at startup, every held list indexed, the
        flag still up because nobody typed in the filter bar."""
        list_index._rebuild_pending = True
        with mock.patch.object(list_index, "_backfill_missing", return_value=1):
            list_index.backfill_in_background(self.held).join(5)
        self.assertTrue(runtime.list_index_backfill_done.is_set())
        self.assertEqual(list_index.held_totals(self.held)[2], "ok")


class SessionCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.session.structured = True
        self.sent = []
        self.session.send = self.sent.append
        self.set_config(update_inprogress=False)


class ALateBurstGoesOutWhenItIsReady(SessionCase):
    """Items 3 and 4: a burst that missed STATUS_WAIT."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(adminchat, "STATUS_WAIT", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.calls = []

        def status_lines(**kwargs):
            self.calls.append(kwargs)
            self.release.wait(10)
            return ["DCCORE STATUS slow"] + (["DCCORE LIBRARY x"] if kwargs.get("library") else [])
        patcher = mock.patch.object(adminchat, "status_lines", status_lines)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self):
        self.release.set()
        self.session._status_job[0].join(5)

    def test_it_follows_its_ping_as_soon_as_it_is_done(self):
        self.session.send_status()
        self.assertEqual(self.sent[-1], "DCCORE PING")
        self.finish()
        self.session.send_finished_status()
        self.assertEqual(self.sent[-1], "DCCORE STATUS slow")
        self.assertIsNone(self.session._status_job)

    def test_nothing_while_it_is_still_being_computed(self):
        self.session.send_status()
        before = list(self.sent)
        self.session.send_finished_status()
        self.assertEqual(self.sent, before)

    def test_the_writer_sends_it_long_before_the_next_timer_burst(self):
        self.session._status_sent_at = time.time()
        self.session.start_writer()
        self.addCleanup(self.session._wake.set)
        self.addCleanup(setattr, self.session, "closed", True)
        self.session.request_status()
        deadline = time.time() + 5
        while "DCCORE PING" not in self.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn("DCCORE PING", self.sent)
        self.release.set()
        deadline = time.time() + 5
        while "DCCORE STATUS slow" not in self.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn("DCCORE STATUS slow", self.sent)

    def test_library_asked_during_a_burst_without_it_gets_one_with_it(self):
        asked = []
        self.session.send_status()
        self.assertFalse(self.calls[0]["library"])
        adminchat._cmd_library(self.session, "")
        self.session.request_status = lambda: asked.append(True)
        self.finish()
        self.session.send_finished_status()
        self.assertEqual(asked, [True])

    def test_a_burst_that_had_them_asks_for_no_other(self):
        asked = []
        self.session.reads_library = True
        self.session.send_status()
        self.session.request_status = lambda: asked.append(True)
        self.finish()
        self.session.send_finished_status()
        self.assertIn("DCCORE LIBRARY x", self.sent)
        self.assertEqual(asked, [])


class AListThatCannotBeCounted(DCCoreTestCase):
    """Item 5: 'Error' is not an empty list; 'No List' is."""

    def setUp(self):
        super().setUp()
        self.figures = {"Alfa Music": (0, "Error", "0B", 0),
                        "Bravo": (0, "No List", "0B", 0)}
        for patch in (mock.patch.object(library, "lists", return_value=[ALFA, BRAVO]),
                      mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes",
                                        side_effect=lambda name=None: self.figures[name]),
                      mock.patch.object(list_mod, "read_library_summary",
                                        return_value={"folders": 0, "finished_at": 0, "seconds": 0})):
            patch.start()
            self.addCleanup(patch.stop)
        self.set_config(fetched_bot_lists={})

    def test_its_line_is_left_out_and_one_not_built_reads_empty(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lines = adminchat.library_lines()
        self.assertEqual(lines, ["DCCORE LIBRARY 0 0 0 0 0 0 Bravo",
                                 "DCCORE SEARCHINDEX 0 0 none"])
        self.assertIn("Alfa Music", out.getvalue())

    def test_a_person_is_told_which_is_which(self):
        session = adminchat.Session(socket.socket(), "192.0.2.1", "Op", "op.example")
        self.addCleanup(session.sock.close)
        sent = []
        session.send = sent.append
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._cmd_library(session, "")
        self.assertEqual(sent, ["Bravo: no list built yet.",
                                "Alfa Music (primary): its figures could not be read; the log says why.",
                                "Search index: no list held from other bots."])


class ADamagedSummaryNeverRaises(DCCoreTestCase):
    """Item 6."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def stored(self, text):
        path = list_mod.library_summary_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_figure_too_big_for_a_number_reads_zero_and_the_others_stay(self):
        self.stored('{"folders": 1e999, "finished_at": 5, "seconds": -1e999}')
        self.assertEqual(list_mod.read_library_summary(),
                         {"folders": 0, "finished_at": 5, "seconds": 0})

    def test_a_file_nested_too_deep_reads_zero(self):
        self.stored("[" * 200000)
        self.assertEqual(list_mod.read_library_summary(),
                         {"folders": 0, "finished_at": 0, "seconds": 0})


class AShortRebuildHasItsSeconds(DCCoreTestCase):
    """Item 7."""

    def reply(self, seconds):
        session = adminchat.Session(socket.socket(), "192.0.2.1", "Op", "op.example")
        self.addCleanup(session.sock.close)
        sent = []
        session.send = sent.append
        self.set_config(fetched_bot_lists={})
        with mock.patch.object(library, "lists", return_value=[ALFA]), \
                mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes",
                                  return_value=(3, "Oct 1st", "3B", 3)), \
                mock.patch.object(list_mod, "read_library_summary",
                                  return_value={"folders": 1, "finished_at": 1760328000, "seconds": seconds}):
            adminchat._cmd_library(session, "")
        return sent[0]

    def test_under_a_minute_in_seconds(self):
        self.assertTrue(self.reply(45).endswith(", in 45 Sec."), self.reply(45))

    def test_a_minute_or_more_as_before(self):
        self.assertTrue(self.reply(360).endswith(", in 6 Min."), self.reply(360))


class ASessionThatDoesNotDrawRebuilds(SessionCase):
    """Item 8: it reads the library, so it hears when a rebuild ends."""

    def setUp(self):
        super().setUp()
        self.session.reads_library = True
        self.asked = []
        self.session.request_status = lambda: self.asked.append(True)

    def test_it_is_sent_no_rebuild_line_and_asks_for_a_burst_at_the_end(self):
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual((self.sent, self.asked), ([], []))
        self.set_config(update_inprogress=False)
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual((self.sent, self.asked), ([], [True]))

    def test_its_bursts_do_not_forget_the_rebuild_is_running(self):
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        with mock.patch.object(adminchat, "status_lines", return_value=["DCCORE STATUS 0"]):
            self.session.send_status()
        self.set_config(update_inprogress=False)
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.asked, [True])

    def test_a_session_that_reads_neither_watches_nothing(self):
        self.session.reads_library = False
        self.session._rebuild_shown = True
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual((self.sent, self.asked), ([], []))


class TheIndexCountsTheListsTheBackfillIndexes(DCCoreTestCase):
    """Item 9: one rule for which lists a held entry has."""

    def test_a_main_marker_without_a_path_counts_the_entrys_own_list(self):
        entry = {"bot": "BotCharlie", "list_path": "/x/c.txt", "entry_count": 7,
                 "lists": {"": {"entry_count": 1}, "RAR": {"list_path": "/x/c-rar.txt", "entry_count": 3}}}
        self.assertEqual(len(list_index._held_lists("BotCharlie", entry)), 2)
        self.assertEqual(list_index.held_totals({"botcharlie": entry}), (10, 2, "ok"))


class ABurstResolvesEachListOnce(DCCoreTestCase):
    """Item 10: lists.json once per list, not six times."""

    def test_one_lookup_per_list_and_none_kept_afterwards(self):
        self.make_tree()
        self.set_config(fetched_bot_lists={})
        real = library.list_by_name
        looked_up = []

        def list_by_name(name):
            looked_up.append(name)
            return real(name)
        with mock.patch.object(library, "lists", return_value=[ALFA, BRAVO]), \
                mock.patch.object(library, "list_by_name", list_by_name), \
                contextlib.redirect_stdout(io.StringIO()):
            served, unreadable, _index = adminchat.library_figures()
            self.assertEqual(sorted(looked_up), ["Alfa Music", "Bravo"])
            self.assertEqual(len(served) + len(unreadable), 2)
            list_mod.list_dir("Bravo")
        self.assertEqual(looked_up.count("Bravo"), 2, "nothing is kept after the burst")

    def test_another_thread_is_not_answered_from_it(self):
        self.make_tree()
        answers = []
        with mock.patch.object(library, "lists", return_value=[ALFA, BRAVO]):
            with list_mod.list_dirs_held():
                list_mod.list_dir("Bravo")
                with mock.patch.object(library, "list_by_name", return_value=None):
                    other = threading.Thread(target=lambda: answers.append(list_mod.list_dir("Bravo")))
                    other.start()
                    other.join(5)
        self.assertEqual(answers, [list_mod.config.LOCAL_LIST_DIR])


if __name__ == "__main__":
    unittest.main()
