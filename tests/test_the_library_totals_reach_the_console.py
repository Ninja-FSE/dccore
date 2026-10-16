"""The library's totals reach the console, for the control panel's start page (#1359).

dccore.mrc's start page shows the bot's figures from what the console already
sends, but nothing said how big the library is: its files, folders and size,
when it was last rebuilt and how long that took, and the search index. A
rebuild now records what the list files cannot say - its folder count, when it
published and how long it took - beside the size files, and `library` answers
with one `DCCORE LIBRARY` line per list and a `DCCORE SEARCHINDEX` line, then
puts them in every status burst of that session.

Every nick, list name and path here is invented.
"""

import contextlib
import io
import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import console_settings  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import list_index  # noqa: E402
import runtime  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


class TheSummaryARebuildWrites(DCCoreTestCase):
    """A real rebuild of a small library writes dccore.library.json."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.library = os.path.join(self.tree.root, "library")
        write(os.path.join(self.library, "Band Kilo", "Live", "a.flac"), b"x" * 300)
        write(os.path.join(self.library, "Band Kilo", "Live", "b.flac"), b"x" * 200)
        write(os.path.join(self.library, "Band Lima", "c.flac"), b"x" * 100)
        self.set_config(FILE_DIRECTORY=self.library, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="kilo", NICKNAME="kilo", ORIGINAL_NICK="kilo")

    def build(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(), out.getvalue())

    def test_it_counts_the_folders_the_list_has(self):
        self.build()
        self.assertEqual(list_mod.read_library_summary()["folders"], 2)

    def test_it_says_when_it_published_and_how_long_it_took(self):
        before = int(time.time())
        self.build()
        summary = list_mod.read_library_summary()
        self.assertGreaterEqual(summary["finished_at"], before)
        self.assertLessEqual(summary["finished_at"], int(time.time()))
        self.assertLessEqual(summary["seconds"], int(time.time()) - before + 1)

    def test_it_sits_beside_the_size_files(self):
        self.build()
        self.assertEqual(os.path.dirname(list_mod.library_summary_path()),
                         os.path.dirname(list_mod.size_file_path()))
        self.assertTrue(os.path.exists(list_mod.library_summary_path()))

    def test_the_line_has_the_adverts_files_and_bytes(self):
        self.build()
        (line, index) = adminchat.library_lines()
        parts = line.split(" ")
        self.assertEqual(parts[:5], ["DCCORE", "LIBRARY", "3", "2", "600"])
        self.assertEqual(parts[-2:], ["1", library.DEFAULT_LIST_NAME])
        self.assertTrue(index.startswith("DCCORE SEARCHINDEX "))

    def test_a_summary_that_cannot_be_written_does_not_fail_the_rebuild(self):
        import db

        def refuse(path, text):
            if path.endswith(list_mod.LIBRARY_SUMMARY_FILE):
                raise OSError("disk full")
            return original(path, text)

        original = db._atomic_write
        with mock.patch.object(db, "_atomic_write", refuse):
            self.build()
        self.assertEqual(list_mod.read_library_summary(),
                         {"folders": 0, "finished_at": 0, "seconds": 0})
        self.assertTrue(list_mod.find_latest_list())


class TheFolderCount(DCCoreTestCase):
    """_write_library_summary() on its own: music and film together."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_music_and_film_folders_both_count_and_a_shared_one_once(self):
        music = [("M\\One", "a.flac", 1), ("M\\One", "b.flac", 1), ("M\\Two", "c.flac", 1)]
        films = [("F\\Three", "d.mkv", 1), ("M\\Two", "e.mkv", 1)]
        with contextlib.redirect_stdout(io.StringIO()):
            update_list._write_library_summary(None, music, films, started=time.time() - 42)
        summary = list_mod.read_library_summary()
        self.assertEqual(summary["folders"], 3)
        self.assertIn(summary["seconds"], (42, 43))


class ReadingTheSummary(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def stored(self, text):
        write(list_mod.library_summary_path(), text.encode("utf-8"))

    def test_none_yet_reads_zero(self):
        self.assertEqual(list_mod.read_library_summary(),
                         {"folders": 0, "finished_at": 0, "seconds": 0})

    def test_a_damaged_file_reads_zero(self):
        for text in ("{", "[1, 2]", '"folders"', ""):
            with self.subTest(text=text):
                self.stored(text)
                self.assertEqual(list_mod.read_library_summary(),
                                 {"folders": 0, "finished_at": 0, "seconds": 0})

    def test_a_bad_figure_reads_zero_and_the_others_stay(self):
        self.stored(json.dumps({"folders": "many", "finished_at": 1700000000, "seconds": -5}))
        self.assertEqual(list_mod.read_library_summary(),
                         {"folders": 0, "finished_at": 1700000000, "seconds": 0})


ALFA = library.ServedList("Alfa Music", True, [], [])
BRAVO = library.ServedList("Bravo", False, ["#bravo"], [])


class TheLines(DCCoreTestCase):
    """library_lines() for two lists, with the figures stood in for."""

    def setUp(self):
        super().setUp()
        figures = {"Alfa Music": (48213, "Oct 13th", "9.30TB", 10225000000000),
                   "Bravo": (7, "Oct 1st", "1.00GB", 1073741824)}
        summaries = {"Alfa Music": {"folders": 1902, "finished_at": 1760328000, "seconds": 360},
                     "Bravo": {"folders": 0, "finished_at": 0, "seconds": 0}}
        patches = [
            mock.patch.object(library, "lists", return_value=[ALFA, BRAVO]),
            mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes",
                              side_effect=lambda name=None: figures[name]),
            mock.patch.object(list_mod, "read_library_summary",
                              side_effect=lambda name=None: summaries[name]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.set_config(fetched_bot_lists={})

    def test_one_line_per_list_in_order_then_the_index(self):
        self.assertEqual(adminchat.library_lines(), [
            "DCCORE LIBRARY 48213 1902 10225000000000 1760328000 360 1 Alfa Music",
            "DCCORE LIBRARY 7 0 1073741824 0 0 0 Bravo",
            "DCCORE SEARCHINDEX 0 0 none",
        ])

    def test_a_list_that_cannot_be_read_is_left_out_not_the_others(self):
        def figures(name=None):
            if name == "Alfa Music":
                raise OSError("unplugged")
            return (7, "Oct 1st", "1.00GB", 1073741824)
        with mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes", side_effect=figures), \
                contextlib.redirect_stdout(io.StringIO()):
            lines = adminchat.library_lines()
        self.assertEqual([line.split(" ")[-1] for line in lines], ["Bravo", "none"])

    def test_a_name_cannot_break_the_line(self):
        evil = ALFA._replace(name="x\r\nDCCORE TAKEN y")
        with mock.patch.object(library, "lists", return_value=[evil]), \
                mock.patch.object(list_mod, "get_file_count_date_size_and_raw_bytes",
                                  return_value=(1, "", "1B", 1)), \
                mock.patch.object(list_mod, "read_library_summary",
                                  return_value={"folders": 1, "finished_at": 1, "seconds": 1}):
            line = adminchat.library_lines()[0]
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)

    def test_the_burst_carries_them_only_when_asked(self):
        def kinds(lines):
            return [line.split(" ")[1] for line in lines if line.startswith("DCCORE ")]
        self.assertNotIn("LIBRARY", kinds(adminchat.status_lines()))
        self.assertNotIn("SEARCHINDEX", kinds(adminchat.status_lines()))
        asked = kinds(adminchat.status_lines(library=True))
        self.assertEqual(asked.count("LIBRARY"), 2)
        self.assertEqual(asked.count("SEARCHINDEX"), 1)

    def test_a_person_reads_them(self):
        session = adminchat.Session(socket.socket(), "192.0.2.1", "Op", "op.example")
        self.addCleanup(session.sock.close)
        sent = []
        session.send = sent.append
        adminchat._cmd_library(session, "")
        self.assertEqual(sent[0], "Alfa Music (primary): 48,213 files in 1,902 folders, 9.30TB; rebuilt "
                         + time.strftime("%Y-%m-%d %H:%M", time.localtime(1760328000))
                         + ", in 6 Min.")
        self.assertEqual(sent[1], "Bravo: 7 files, 1.00GB.")
        self.assertEqual(sent[2], "Search index: no list held from other bots.")


class TheSearchIndex(DCCoreTestCase):
    """list_index.held_totals(): from what the fetches counted."""

    def setUp(self):
        super().setUp()
        self.addCleanup(runtime.list_index_backfill_done.set)
        self.addCleanup(setattr, list_index, "_rebuild_pending", list_index._rebuild_pending)
        list_index._rebuild_pending = False

    def test_nothing_held(self):
        self.assertEqual(list_index.held_totals({}), (0, 0, "none"))
        self.assertEqual(list_index.held_totals(None), (0, 0, "none"))

    def test_every_list_of_every_bot(self):
        held = {
            "botalfa": {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": 100,
                        "lists": {"": {"list_path": "/x/a.txt", "entry_count": 100},
                                  "RAR": {"list_path": "/x/a-rar.txt", "entry_count": 20}}},
            # From before an archive could hold more than one list.
            "botbravo": {"bot": "BotBravo", "list_path": "/x/b.txt", "entry_count": 5},
            # Its other lists recorded, its main one only on the entry.
            "botcharlie": {"bot": "BotCharlie", "list_path": "/x/c.txt", "entry_count": 7,
                           "lists": {"RAR": {"list_path": "/x/c-rar.txt", "entry_count": 3}}},
        }
        self.assertEqual(list_index.held_totals(held), (135, 5, "ok"))

    def test_a_bad_count_is_no_rows_but_still_a_list(self):
        held = {"botalfa": {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": "lots"},
                "junk": "not an entry"}
        self.assertEqual(list_index.held_totals(held), (0, 1, "ok"))

    def test_filling_while_the_startup_backfill_runs(self):
        runtime.list_index_backfill_done.clear()
        held = {"botalfa": {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": 9}}
        self.assertEqual(list_index.held_totals(held), (9, 1, "filling"))

    def test_a_repairs_backfill_owed_but_not_begun_is_not_filling(self):
        """#1367: the flag says one is owed, and nothing may run it for days."""
        list_index._rebuild_pending = True
        held = {"botalfa": {"bot": "BotAlfa", "list_path": "/x/a.txt", "entry_count": 9}}
        self.assertEqual(list_index.held_totals(held), (9, 1, "ok"))


class TheSessionAsksOnce(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.sent = []
        self.session.send = self.sent.append
        self.asked = []
        self.session.request_status = lambda: self.asked.append(True)

    def test_hello_alone_never_sends_them(self):
        """An older script prints a line it has no handler for."""
        adminchat._cmd_hello(self.session, "dccore.mrc 9.99")
        self.assertFalse(self.session.reads_library)

    def test_library_subscribes_and_asks_for_a_burst(self):
        self.session.structured = True
        adminchat._cmd_library(self.session, "")
        self.assertTrue(self.session.reads_library)
        self.assertEqual(self.asked, [True])
        self.assertEqual(self.sent, [], "the lines come in the burst, off this thread")

    def test_the_bursts_after_it_carry_them(self):
        self.session.structured = True
        adminchat._cmd_library(self.session, "")
        seen = {}

        def status_lines(**kwargs):
            seen.update(kwargs)
            return []
        with mock.patch.object(adminchat, "status_lines", status_lines):
            self.session.send_status()
        self.assertTrue(seen.get("library"))

    def test_a_rebuild_that_ends_asks_for_a_burst(self):
        self.session.structured = True
        self.session.draws_rebuild = True
        self.session.reads_library = True
        self.set_config(update_inprogress=False)
        self.session._rebuild_shown = True
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent, ["DCCORE REBUILD end"])
        self.assertEqual(self.asked, [True])

    def test_not_for_a_session_that_never_asked(self):
        self.session.structured = True
        self.session.draws_rebuild = True
        self.set_config(update_inprogress=False)
        self.session._rebuild_shown = True
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent, ["DCCORE REBUILD end"])
        self.assertEqual(self.asked, [])

    def test_a_running_rebuild_asks_nothing(self):
        self.session.structured = True
        self.session.draws_rebuild = True
        self.session.reads_library = True
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertTrue(self.sent[0].startswith("DCCORE REBUILD "))
        self.assertEqual(self.asked, [])


class AScriptCanTellABotThatHasIt(unittest.TestCase):

    def test_consolecaps_names_it(self):
        self.assertIn(("library", 1), console_settings.CAPS)
        self.assertIn(" library:1 ", console_settings.caps_line())


if __name__ == "__main__":
    unittest.main()
