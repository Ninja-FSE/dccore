"""The @DCCore-Downloads window in dccore.mrc (#1022).

What the bot is fetching from other bots, in an mIRC window of its own: the
bot sends a whole snapshot (`DCCORE DLBEGIN`, a `DLROW` per download, `DLEND`)
every few seconds, only while the window is open and only to a script that can
draw it, and takes Cancel / Download again / Clear finished back as commands.
"""

import io
import os
import re
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def row(state, bot="SomeBot", name="a.flac", at=1.0, **more):
    base = {"bot": bot, "filename": name, "requested_filename": name, "state": state,
            "request_type": "file", "requested_at": at}
    base.update(more)
    return base


def dlrows(lines):
    return [l.split(" ", 11) for l in lines if l.startswith("DCCORE DLROW ")]


class TheSnapshot(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()

    def test_it_is_framed_by_begin_and_end_with_the_totals(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("queued")
        config.fetch_queue["bbbbbbbbbbbb"] = row("complete")
        lines = adminchat.downloads_lines()
        self.assertEqual(lines[0], "DCCORE DLBEGIN")
        self.assertEqual(lines[-1], "DCCORE DLEND 1 1 0")

    def test_an_empty_queue_is_still_a_whole_snapshot(self):
        self.assertEqual(adminchat.downloads_lines(), ["DCCORE DLBEGIN", "DCCORE DLEND 0 0 0"])

    def test_coming_in_then_waiting_then_finished(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("complete", at=1.0)
        config.fetch_queue["dddddddddddd"] = row("failed", at=1.5)
        config.fetch_queue["bbbbbbbbbbbb"] = row("queued", at=2.0, queue_position=3)
        config.fetch_queue["cccccccccccc"] = row("receiving", at=3.0, bytes_received=1000,
                                                total_size=4000, receiving_since=100.0)
        rows = dlrows(adminchat.downloads_lines(now=110.0))
        self.assertEqual([r[3] for r in rows], ["d", "w", "c", "f"])

    def test_a_download_under_way_carries_its_progress_and_speed(self):
        config.fetch_queue["cccccccccccc"] = row("receiving", bytes_received=1000, total_size=4000,
                                                receiving_since=100.0)
        (r,) = dlrows(adminchat.downloads_lines(now=110.0))
        self.assertEqual(r[2:11], ["cccccccccccc", "d", "receiving", "SomeBot", "1000", "4000", "100", "0", "-"])
        self.assertEqual(r[11], "a.flac")

    def test_a_waiting_row_says_why_as_one_token(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending", waiting="offline")
        config.fetch_queue["bbbbbbbbbbbb"] = row("queued", queue_position=2, at=2.0)
        config.fetch_queue["cccccccccccc"] = row("offered", at=3.0)
        notes = [r[10] for r in dlrows(adminchat.downloads_lines())]
        for note in notes:
            self.assertNotIn(" ", note)
            self.assertTrue(note)
        self.assertIn("SomeBot", notes[0].replace("_", " "))
        self.assertIn("#2", notes[1])

    def test_failed_says_the_reason_and_a_rejected_list_says_so(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("failed", reason="no response", finished_at=50.0, at=1.0)
        config.fetch_queue["bbbbbbbbbbbb"] = row("complete", request_type="list", name="",
                                                list_processing_error="not an archive",
                                                finished_at=60.0, at=2.0)
        second, first = dlrows(adminchat.downloads_lines())  # newest first
        self.assertEqual((first[4], second[4]), ("failed", "failed"))
        self.assertEqual((first[3], second[3]), ("f", "f"))
        self.assertIn("rejected", second[10])
        self.assertTrue(second[11].endswith("file list"))
        self.assertEqual(first[9], "50")

    def test_finished_newest_first_and_capped_by_what_the_window_asks(self):
        for i in range(5):
            config.fetch_queue[f"{i:012x}"] = row("complete", name=f"f{i}.flac", at=float(i),
                                                 finished_at=float(10 + i))
        lines = adminchat.downloads_lines(finished_rows=2)
        self.assertEqual([r[11] for r in dlrows(lines)], ["f4.flac", "f3.flac"])
        self.assertEqual(lines[-1], "DCCORE DLEND 0 5 0", "the total still says how many there are")

    def test_finished_and_failed_are_kept_apart_and_each_capped_at_fifteen(self):
        for i in range(20):
            config.fetch_queue[f"a{i:011x}"] = row("complete", name=f"ok{i}.flac", at=float(i), finished_at=float(i))
            config.fetch_queue[f"b{i:011x}"] = row("failed", name=f"bad{i}.flac", at=float(i), finished_at=float(i),
                                                  reason="x")
        lines = adminchat.downloads_lines(finished_rows=100)
        rows = dlrows(lines)
        self.assertEqual(sum(1 for r in rows if r[3] == "c"), 15)
        self.assertEqual(sum(1 for r in rows if r[3] == "f"), 15)
        self.assertTrue(all(r[4] == "complete" for r in rows if r[3] == "c"))
        self.assertTrue(all(r[4] == "failed" for r in rows if r[3] == "f"))
        self.assertEqual(lines[-1], "DCCORE DLEND 0 20 20")
        self.assertEqual(adminchat.DOWNLOADS_FINISHED_MAX, 15)
        self.assertEqual(adminchat.DOWNLOADS_FINISHED_DEFAULT, 15)

    def test_waiting_is_capped_but_counted(self):
        for i in range(adminchat.DOWNLOADS_WAITING_MAX + 7):
            config.fetch_queue[f"{i:012x}"] = row("pending", at=float(i))
        lines = adminchat.downloads_lines()
        self.assertEqual(len(dlrows(lines)), adminchat.DOWNLOADS_WAITING_MAX)
        self.assertEqual(lines[-1], f"DCCORE DLEND {adminchat.DOWNLOADS_WAITING_MAX + 7} 0 0")

    def test_one_bots_rows_sit_together_in_each_kind(self):
        for i, bot in enumerate(["BotA", "BotB", "BotA", "BotC", "BotB"]):
            config.fetch_queue[f"{i:012x}"] = row("complete", bot=bot, name=f"f{i}.flac", at=float(i),
                                                 finished_at=float(10 + i))
        rows = dlrows(adminchat.downloads_lines())
        self.assertEqual([r[5] for r in rows], ["BotB", "BotB", "BotC", "BotA", "BotA"],
                         "bots in the order of their newest row, each one's files together, newest first")
        self.assertEqual([r[11] for r in rows], ["f4.flac", "f1.flac", "f3.flac", "f2.flac", "f0.flac"])

    def test_a_name_cannot_break_the_line(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending", name="a\r\nDCCORE TAKEN x.flac")
        for line in adminchat.downloads_lines():
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)

    def test_a_held_fetch_lock_gives_nothing_rather_than_a_stall(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        lock = dcc_fetch._fetch_lock()
        held, release = threading.Event(), threading.Event()

        def hold():
            with lock:
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        held.wait(5)
        began = time.time()
        self.assertIsNone(adminchat.downloads_lines())
        self.assertLess(time.time() - began, adminchat.DOWNLOADS_LOCK_WAIT + 1.0)


class OnlyAScriptThatCanDrawIt(DCCoreTestCase):

    def test_the_versions(self):
        self.assertFalse(adminchat.script_draws_downloads("1.9"))
        self.assertFalse(adminchat.script_draws_downloads(""))
        self.assertFalse(adminchat.script_draws_downloads("garbage"))
        self.assertTrue(adminchat.script_draws_downloads("1.10"))
        self.assertTrue(adminchat.script_draws_downloads("2.0"))

    def test_hello_records_it(self):
        session = make_session(self)
        adminchat._cmd_hello(session, "dccore.mrc 1.10")
        self.assertTrue(session.draws_downloads)
        adminchat._cmd_hello(session, "dccore.mrc 1.9")
        self.assertFalse(session.draws_downloads)


def make_session(case):
    near, far = socket.socketpair()
    case.addCleanup(near.close)
    case.addCleanup(far.close)
    session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
    session.authenticated = True
    session.structured = True
    session.draws_downloads = True
    session.sent = []
    session.send = session.sent.append
    return session


class TheDownloadsCommand(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def test_on_opens_it_with_the_default_rows(self):
        adminchat._cmd_downloads(self.session, "on")
        self.assertEqual(self.session.downloads_finished, adminchat.DOWNLOADS_FINISHED_DEFAULT)

    def test_rows_are_clamped(self):
        adminchat._cmd_downloads(self.session, "on 5000")
        self.assertEqual(self.session.downloads_finished, 15)
        adminchat._cmd_downloads(self.session, "on 0")
        self.assertEqual(self.session.downloads_finished, 1)
        adminchat._cmd_downloads(self.session, "on lots")
        self.assertEqual(self.session.downloads_finished, adminchat.DOWNLOADS_FINISHED_DEFAULT)

    def test_off_closes_it(self):
        adminchat._cmd_downloads(self.session, "on 10")
        adminchat._cmd_downloads(self.session, "off")
        self.assertEqual(self.session.downloads_finished, 0)

    def test_an_old_script_is_told_why_not(self):
        self.session.draws_downloads = False
        self.session.client = "dccore.mrc 1.9"
        adminchat._cmd_downloads(self.session, "on")
        self.assertEqual(self.session.downloads_finished, 0)
        self.assertIn("1.10", self.session.sent[-1])

    def test_anything_else_says_the_usage(self):
        adminchat._cmd_downloads(self.session, "")
        self.assertIn("Usage", self.session.sent[-1])

    def test_the_four_commands_are_registered(self):
        for name in ("downloads", "dlcancel", "dlagain", "dlclear"):
            self.assertIn(name, adminchat.COMMANDS)


class BetweenBursts(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        self.session = make_session(self)

    def test_nothing_while_the_window_is_closed(self):
        self.session.send_downloads()
        self.assertEqual(self.session.sent, [])

    def test_a_snapshot_goes_when_asked_and_not_again_unchanged(self):
        self.session.downloads_finished = 20
        self.session.request_downloads()
        self.session.send_downloads()
        self.assertEqual(self.session.sent[0], "DCCORE DLBEGIN")
        sent = len(self.session.sent)
        self.session._downloads_sent_at = time.time() - adminchat.DOWNLOADS_INTERVAL - 1
        self.session.send_downloads()
        self.assertEqual(len(self.session.sent), sent, "the same snapshot is not sent twice")

    def test_a_change_goes_after_the_interval_and_not_before(self):
        self.session.downloads_finished = 20
        self.session.request_downloads()
        self.session.send_downloads()
        sent = len(self.session.sent)
        config.fetch_queue["bbbbbbbbbbbb"] = row("queued", at=2.0)
        self.session.send_downloads()
        self.assertEqual(len(self.session.sent), sent, "not before the interval")
        self.session._downloads_sent_at = time.time() - adminchat.DOWNLOADS_INTERVAL - 1
        self.session.send_downloads()
        self.assertGreater(len(self.session.sent), sent)

    def test_a_script_that_cannot_draw_it_is_sent_nothing(self):
        self.session.downloads_finished = 20
        self.session.draws_downloads = False
        self.session.request_downloads()
        self.session.send_downloads()
        self.assertEqual(self.session.sent, [])

    def test_the_writer_loop_calls_it(self):
        import inspect
        self.assertIn("send_downloads()", inspect.getsource(adminchat.Session._writer_loop))


class TheWindowsCommands(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.session = make_session(self)
        persist = dcc_fetch.persist_fetch_history
        dcc_fetch.persist_fetch_history = lambda: None
        self.addCleanup(setattr, dcc_fetch, "persist_fetch_history", persist)

    def test_cancel_lets_a_waiting_request_go(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        adminchat._cmd_dlcancel(self.session, "aaaaaaaaaaaa")
        self.assertNotIn("aaaaaaaaaaaa", config.fetch_queue)
        self.assertIn("Cancelled", self.session.sent[-1])

    def test_cancel_never_touches_a_transfer_or_a_finished_row(self):
        for state in ("receiving", "listening", "complete", "failed"):
            config.fetch_queue["aaaaaaaaaaaa"] = row(state)
            adminchat._cmd_dlcancel(self.session, "aaaaaaaaaaaa")
            self.assertIn("aaaaaaaaaaaa", config.fetch_queue, state)

    def test_the_delete_helper_refuses_outside_only_states_under_its_lock(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("complete")
        status, _ = webserver.build_fetch_delete_result("aaaaaaaaaaaa", only_states=("pending",))
        self.assertEqual(status, 409)
        self.assertIn("aaaaaaaaaaaa", config.fetch_queue)

    def test_a_bad_or_unknown_id_is_told_not_acted_on(self):
        adminchat._cmd_dlcancel(self.session, "nonsense")
        self.assertIn("Usage", self.session.sent[-1])
        adminchat._cmd_dlcancel(self.session, "0" * 12)
        self.assertIn("no longer", self.session.sent[-1])

    def test_again_only_for_a_failed_download(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("complete")
        adminchat._cmd_dlagain(self.session, "aaaaaaaaaaaa")
        self.assertIn("Only a download that failed", self.session.sent[-1])
        self.assertEqual(len(config.fetch_queue), 1)

    def test_again_asks_for_a_failed_file_through_the_enqueue(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("failed", reason="no response")
        asked = []
        real = webserver.build_fetch_enqueue_result
        webserver.build_fetch_enqueue_result = lambda items: (asked.append(items) or (200, {}))
        self.addCleanup(setattr, webserver, "build_fetch_enqueue_result", real)
        adminchat._cmd_dlagain(self.session, "aaaaaaaaaaaa")
        self.assertEqual(asked, [[{"bot": "SomeBot", "filename": "a.flac"}]])
        self.assertIn("aaaaaaaaaaaa", config.fetch_queue, "the old row stays as the record")

    def test_again_asks_a_failed_list_through_the_list_route(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("failed", request_type="list", name="")
        asked = []
        real = adminchat._ask_for_list
        adminchat._ask_for_list = lambda bot: (asked.append(bot) or (True, "ok"))
        self.addCleanup(setattr, adminchat, "_ask_for_list", real)
        adminchat._cmd_dlagain(self.session, "aaaaaaaaaaaa")
        self.assertEqual(asked, ["SomeBot"])

    def test_clear_forgets_finished_rows_only(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("complete")
        config.fetch_queue["bbbbbbbbbbbb"] = row("failed")
        config.fetch_queue["cccccccccccc"] = row("pending")
        adminchat._cmd_dlclear(self.session, "")
        self.assertEqual(list(config.fetch_queue), ["cccccccccccc"])
        self.assertIn("2", self.session.sent[-1])


class WhenARowFinished(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        dcc_fetch._fetch_feed_told.clear()
        dcc_fetch._fetch_feed_seeded[0] = False
        self.addCleanup(dcc_fetch._fetch_feed_told.clear)
        self.addCleanup(dcc_fetch._fetch_feed_seeded.__setitem__, 0, False)

    def test_a_row_seen_to_finish_is_stamped_but_not_the_first_pass(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("complete")
        dcc_fetch.tell_the_fetch_feed()
        self.assertNotIn("finished_at", config.fetch_queue["aaaaaaaaaaaa"], "history loaded at startup")
        config.fetch_queue["bbbbbbbbbbbb"] = row("receiving")
        dcc_fetch.tell_the_fetch_feed()
        config.fetch_queue["bbbbbbbbbbbb"]["state"] = "complete"
        dcc_fetch.tell_the_fetch_feed()
        self.assertAlmostEqual(config.fetch_queue["bbbbbbbbbbbb"]["finished_at"], time.time(), delta=5)

    def test_the_stamp_is_written_once(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("failed")
        dcc_fetch._stamp_finished(["aaaaaaaaaaaa", "missing"])
        first = config.fetch_queue["aaaaaaaaaaaa"]["finished_at"]
        dcc_fetch.tell_the_fetch_feed()
        self.assertEqual(config.fetch_queue["aaaaaaaaaaaa"]["finished_at"], first)


class TheScript(unittest.TestCase):

    def setUp(self):
        self.text = read("scripts", "mirc", "dccore.mrc")

    def test_the_version_is_the_one_the_bot_gates_on(self):
        match = re.search(r"alias dccore\.ver \{ return ([0-9.]+) \}", self.text)
        self.assertGreaterEqual(tuple(int(p) for p in match.group(1).split(".")),
                                tuple(int(p) for p in adminchat.DOWNLOADS_SCRIPT_VERSION.split(".")))

    def test_the_snapshot_lines_are_stored_and_drawn_at_the_end(self):
        self.assertIn("if (%type == DLBEGIN)", self.text)
        self.assertIn("if (%type == DLROW)", self.text)
        end = self.text[self.text.index("if (%type == DLEND)"):]
        end = end[:end.index("\n")]
        self.assertIn("dccore.dl.draw", end)

    def test_the_window_tells_the_bot_when_it_opens_and_closes(self):
        self.assertIn("downloads on $dccore.dl.rows", self.text)
        close = self.text[self.text.index("on *:CLOSE:@DCCore-Downloads:"):]
        close = close[:close.index("\n}")]
        self.assertIn("downloads off", close)

    def test_a_window_left_open_asks_again_after_a_reconnect(self):
        self.assertIn("if ($window($dccore.dl.win)) { hdel dccore.live dlend | dccore.dl.tell | dccore.dl.draw }",
                      self.text)

    def test_the_menu_sends_the_commands_with_the_rows_id(self):
        menu = self.text[self.text.index("menu @DCCore-Downloads {"):]
        menu = menu[:menu.index("\n}")]
        self.assertIn("dccore.dl.do dlcancel", menu)
        self.assertIn("dccore.dl.do dlagain", menu)
        self.assertIn("dlclear", menu)
        self.assertIn("dccore.dl.web", menu)

    def test_cancel_is_offered_only_for_a_waiting_row_and_again_only_for_a_failed_one(self):
        menu = self.text[self.text.index("menu @DCCore-Downloads {"):]
        self.assertIn("$iif($gettok($dccore.dl.pick,1,58) == w,Cancel this request)", menu)
        self.assertIn("$iif($gettok($dccore.dl.pick,2,58) == failed,Download again)", menu)

    def test_the_window_is_in_the_command_list_and_the_menus(self):
        self.assertIn("/dccore downloads", self.text)
        self.assertIn(".Open the Downloads window:dccore downloads", self.text)
        self.assertIn("Downloads window:dccore downloads", self.text)

    def test_the_options_dialog_keeps_the_finished_rows(self):
        self.assertIn("hadd dccore dlfinished", self.text)
        self.assertIn("did -ra dccore.opt 310 $dccore.dl.rows", self.text)

    def test_the_fetch_feed_goes_to_the_downloads_window_and_no_longer_to_the_main_one(self):
        handler = self.text[self.text.index("if (%type == FETCH) {"):]
        handler = handler[:handler.index("\n  }\n")]
        self.assertIn("dccore.dl.log $3 $4-", handler)
        self.assertNotIn("dccore.msg", handler)
        self.assertNotIn("dccore.alert", handler)

    def test_the_log_tags_each_event_and_stays_silent_when_the_window_is_closed(self):
        body = self.text[self.text.index("alias dccore.dl.log {"):]
        body = body[:body.index("\n}")]
        self.assertIn("if (!$window($dccore.dl.win)) { return }", body)
        for tag in ("REQUEST", "QUEUE", "DOWNLOADING", "FINISHED", "FAILED"):
            self.assertIn(f"$dccore.tag({tag},", body)
        self.assertIn("Started downloading $3-", body)
        self.assertIn("Received $3-", body)

    def test_the_list_is_a_narrow_side_panel_and_each_download_names_its_file_on_its_own_line(self):
        self.assertIn("window -l64 $dccore.dl.win", self.text)
        body = self.text[self.text.index("alias dccore.dl.draw {"):]
        body = body[:body.index("\nmenu @DCCore-Downloads")]
        self.assertIn("$right(%name,42)", body)
        self.assertIn("if (%bot != %lastbot)", body)

    def test_the_bars_need_no_bytes_function_and_no_unicode(self):
        body = self.text[self.text.index("alias dccore.dl.draw {"):]
        body = body[:body.index("\nmenu @DCCore-Downloads")]
        self.assertNotIn("$bytes(", body)
        self.assertTrue(all(ord(c) < 128 for c in body))


if __name__ == "__main__":
    unittest.main()
