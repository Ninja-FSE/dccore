"""The operator controls who is served next (#1206, part 2): a nick moved up or
down the line, one of its files moved inside its own queue, one file removed
(the nick is told, exactly as for `@<bot>-remove <file>`), and everything
cleared. The core, the console and the dashboard's routes - and that the Queue
page's buttons and strings are wired.
"""

import os
import sys
import time
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import commands  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, no_disk_writes, silence_debug  # noqa: E402
from tests.test_dccore_downloads_window import make_session  # noqa: E402


def read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def row(name, **extra):
    return dict({"file": name, "path": "/music/" + name, "channel": "#c",
                 "user_raw": "Dave", "is_temporary_zip": False}, **extra)


def names(nick):
    return [r["file"] for r in config.dcc_queue.get(nick, [])]


def row_id(nick, place):
    """The id the Queue page sends back for the file at 1-based `place` (#1245)."""
    return dcc.queue_row_id(config.dcc_queue[nick][place - 1])


def sending(nick, place):
    """The slot claim dcc.py makes for a queued row: its row, and no path (#1245)."""
    queued = config.dcc_queue[nick][place - 1]
    return {"user": nick, "file": queued["file"], "bytes_sent": 0,
            "next_file_obj": queued["file"], "queue_row": queued}


class Base(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        no_disk_writes(db)
        self.notices = []
        self._old = sys.modules.get("oserve")
        sys.modules["oserve"] = types.SimpleNamespace(
            queue_message=lambda to, msg, *a, **k: self.notices.append((to, msg)))
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", self._old) if self._old
                        else sys.modules.pop("oserve", None))
        config.frozen_queues = {}
        config.active_transfers = []
        runtime.queue_waiting_since.clear()
        self.addCleanup(runtime.queue_waiting_since.clear)
        self.addCleanup(lambda: setattr(runtime, "pack_job", None))
        config.dcc_queue = {"ann": [row("a1.mp3"), row("a2.mp3"), row("a3.mp3")],
                            "bob": [row("b1.mp3")],
                            "cat": [row("c1.mp3"), row("c2.mp3")]}


class TheLineForAFreeSlot(Base):

    def served(self):
        """Who the dispatcher would try first: the order check_queue_and_send walks."""
        return [k for k, _ in sorted(config.dcc_queue.items(),
                                     key=lambda entry: dcc.queue_waiting_since(entry[0]))]

    def test_the_line_starts_in_arrival_order(self):
        self.assertEqual(commands.queue_order(), ["ann", "bob", "cat"])

    def test_up_puts_a_nick_before_the_one_ahead(self):
        ok, message = commands.move_waiting_user("cat", "up")
        self.assertTrue(ok)
        self.assertEqual(self.served(), ["ann", "cat", "bob"])
        self.assertIn("number 2 of 3", message)

    def test_down_puts_a_nick_after_the_one_behind(self):
        commands.move_waiting_user("ann", "down")
        self.assertEqual(self.served(), ["bob", "ann", "cat"])

    def test_a_nick_can_be_taken_all_the_way_up(self):
        commands.move_waiting_user("cat", "up")
        commands.move_waiting_user("cat", "up")
        self.assertEqual(self.served(), ["cat", "ann", "bob"])

    def test_the_order_holds_when_stamps_exist(self):
        now = time.time()
        runtime.queue_waiting_since.update({"ann": now - 30, "bob": now - 20, "cat": now - 10})
        commands.move_waiting_user("bob", "up")
        self.assertEqual(self.served(), ["bob", "ann", "cat"])

    def test_a_nick_that_goes_to_the_back_afterwards_is_behind_everyone(self):
        commands.move_waiting_user("cat", "up")
        dcc.go_to_the_back("ann")
        self.assertEqual(self.served(), ["cat", "bob", "ann"])

    def test_the_ends_say_so_and_change_nothing(self):
        ok, message = commands.move_waiting_user("ann", "up")
        self.assertFalse(ok)
        self.assertIn("already first", message)
        ok, message = commands.move_waiting_user("cat", "down")
        self.assertFalse(ok)
        self.assertIn("already last", message)
        self.assertEqual(self.served(), ["ann", "bob", "cat"])

    def test_a_nick_with_nothing_queued_and_a_bad_direction_are_refused(self):
        self.assertFalse(commands.move_waiting_user("zed", "up")[0])
        self.assertFalse(commands.move_waiting_user("ann", "sideways")[0])

    def test_the_nick_may_be_written_in_any_case(self):
        self.assertTrue(commands.move_waiting_user("CAT", "up")[0])

    def test_the_dashboard_lists_them_in_that_order(self):
        commands.move_waiting_user("cat", "up")
        commands.move_waiting_user("cat", "up")
        rows = [r["user"] for r in webserver.build_queue_payload()]
        self.assertEqual(rows, ["cat", "ann", "bob"])

    def test_the_console_lists_them_in_that_order(self):
        commands.move_waiting_user("cat", "up")
        commands.move_waiting_user("cat", "up")
        session = make_session(self)
        adminchat._cmd_queue(session, "")
        users = [line.split()[0] for line in session.sent if line.startswith("  ")]
        self.assertEqual(users, ["cat", "ann", "bob"])


class AFileInsideItsQueue(Base):

    def test_down_swaps_it_with_the_next(self):
        ok, message = commands.move_queued_file("ann", 1, "down")
        self.assertTrue(ok)
        self.assertEqual(names("ann"), ["a2.mp3", "a1.mp3", "a3.mp3"])
        self.assertIn("place 2 of 3", message)

    def test_up_to_the_top_makes_it_the_one_sent_next(self):
        commands.move_queued_file("ann", 3, "up")
        commands.move_queued_file("ann", 2, "up")
        self.assertEqual(names("ann")[0], "a3.mp3")

    def test_the_ends_are_refused(self):
        self.assertIn("already first", commands.move_queued_file("ann", 1, "up")[1])
        self.assertIn("already last", commands.move_queued_file("ann", 3, "down")[1])
        self.assertEqual(names("ann"), ["a1.mp3", "a2.mp3", "a3.mp3"])

    def test_a_place_that_is_not_there_is_refused(self):
        for position in (0, 4, "x", None, -1):
            ok, _ = commands.move_queued_file("ann", position, "up")
            self.assertFalse(ok, position)

    def test_the_file_the_page_saw_has_to_still_be_there(self):
        ok, message = commands.move_queued_file("ann", 2, "up", row_id=row_id("ann", 3))
        self.assertFalse(ok)
        self.assertIn("changed", message)
        self.assertEqual(names("ann"), ["a1.mp3", "a2.mp3", "a3.mp3"])
        self.assertTrue(commands.move_queued_file("ann", 2, "up", row_id=row_id("ann", 2))[0])

    def test_the_other_nicks_are_untouched(self):
        commands.move_queued_file("ann", 1, "down")
        self.assertEqual(names("cat"), ["c1.mp3", "c2.mp3"])

    def test_the_new_order_is_saved(self):
        saved = []
        db.save_dcc_queue = lambda *a, **k: saved.append(True)
        commands.move_queued_file("ann", 1, "down")
        self.assertEqual(saved, [True])

    def test_a_file_being_sent_stays_put(self):
        config.active_transfers = [sending("ann", 1)]
        for position, direction in ((1, "down"), (2, "up")):
            ok, message = commands.move_queued_file("ann", position, direction)
            self.assertFalse(ok)
            self.assertIn("being sent", message)
        self.assertEqual(names("ann"), ["a1.mp3", "a2.mp3", "a3.mp3"])

    def test_the_rest_can_still_be_moved_while_one_is_sent(self):
        config.active_transfers = [sending("ann", 1)]
        self.assertTrue(commands.move_queued_file("ann", 3, "up")[0])
        self.assertEqual(names("ann"), ["a1.mp3", "a3.mp3", "a2.mp3"])

    def test_a_folder_being_packed_stays_put(self):
        config.dcc_queue["ann"][0]["is_unpacked_rar_folder"] = True
        runtime.pack_job = {"user": "Ann", "name": "Album", "archive": "/nowhere.rar",
                            "started": time.time(), "total": 0, "cancelled": False,
                            "row": config.dcc_queue["ann"][0]}
        ok, message = commands.move_queued_file("ann", 2, "up")
        self.assertFalse(ok)
        self.assertIn("being packed", message)


class OneFileRemoved(Base):

    def test_it_goes_and_the_rest_stay(self):
        ok, message = commands.remove_queued_file("ann", 2)
        self.assertTrue(ok)
        self.assertEqual(names("ann"), ["a1.mp3", "a3.mp3"])
        self.assertIn("a2.mp3", message)

    def test_the_nick_gets_the_notice_their_own_remove_gives(self):
        commands.remove_queued_file("ann", 2)
        config.dcc_queue["dave"] = [row("x.mp3")]
        commands.handle_queue_remove_file(None, "Dave", "#c", "x.mp3")
        self.assertEqual(self.notices[0][1], "NOTICE Dave :Removed \"a2.mp3\" from your queue. \r\n")
        self.assertEqual(self.notices[1][1], "NOTICE Dave :Removed \"x.mp3\" from your queue. \r\n")

    def test_the_notice_goes_to_the_nick_as_they_wrote_it(self):
        config.dcc_queue["ann"][0]["user_raw"] = "Ann_"
        commands.remove_queued_file("ann", 1)
        self.assertEqual(self.notices[0][0], "Ann_")
        self.assertTrue(self.notices[0][1].startswith("NOTICE Ann_ :Removed"))

    def test_the_last_file_takes_the_nick_out_of_the_line(self):
        config.frozen_queues = {"bob": 1000.0}
        commands.remove_queued_file("bob", 1)
        self.assertNotIn("bob", config.dcc_queue)
        self.assertNotIn("bob", config.frozen_queues)
        self.assertEqual(commands.queue_order(), ["ann", "cat"])

    def test_two_files_with_one_name_lose_only_the_one_picked(self):
        config.dcc_queue["ann"] = [row("same.mp3", path="/one/same.mp3"), row("same.mp3", path="/two/same.mp3")]
        commands.remove_queued_file("ann", 2)
        self.assertEqual([r["path"] for r in config.dcc_queue["ann"]], ["/one/same.mp3"])

    def test_the_file_the_page_saw_has_to_still_be_there(self):
        ok, message = commands.remove_queued_file("ann", 1, row_id=row_id("ann", 2))
        self.assertFalse(ok)
        self.assertIn("changed", message)
        self.assertEqual(len(names("ann")), 3)
        self.assertEqual(self.notices, [])

    def test_a_place_that_is_not_there_is_refused_without_a_notice(self):
        for position in (0, 9, "two", None):
            self.assertFalse(commands.remove_queued_file("ann", position)[0], position)
        self.assertEqual(self.notices, [])

    def test_it_is_saved(self):
        saved = []
        db.save_dcc_queue = lambda *a, **k: saved.append(True)
        commands.remove_queued_file("ann", 1)
        self.assertEqual(saved, [True])

    def test_the_temp_archive_of_a_packed_folder_goes_with_its_row(self):
        gone = []
        old = dcc.discard_orphaned_temp_archives
        dcc.discard_orphaned_temp_archives = lambda key, rows=None: gone.append(rows) or ["x.rar"]
        self.addCleanup(lambda: setattr(dcc, "discard_orphaned_temp_archives", old))
        commands.remove_queued_file("ann", 1)
        self.assertEqual([r["file"] for r in gone[0]], ["a1.mp3"])


class TheConsole(Base):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def run_command(self, line):
        name, _, args = line.partition(" ")
        adminchat.COMMANDS[name][0](self.session, args)
        return "\n".join(self.session.sent)

    def test_queue_numbers_a_nicks_files(self):
        out = self.run_command("queue ann")
        self.assertIn("1. a1.mp3", out)
        self.assertIn("3. a3.mp3", out)

    def test_queuemove_moves_a_nick(self):
        out = self.run_command("queuemove cat up")
        self.assertIn("number 2 of 3", out)
        self.assertEqual(commands.queue_order(), ["ann", "cat", "bob"])

    def test_queuemove_moves_a_file(self):
        out = self.run_command("queuemove ann 3 up")
        self.assertIn("place 2 of 3", out)
        self.assertEqual(names("ann"), ["a1.mp3", "a3.mp3", "a2.mp3"])

    def test_queueremove_removes_a_file_and_tells_the_nick(self):
        out = self.run_command("queueremove ann 1")
        self.assertIn("Removed", out)
        self.assertEqual(names("ann"), ["a2.mp3", "a3.mp3"])
        self.assertEqual(len(self.notices), 1)

    def test_a_wrong_line_says_how_it_is_written(self):
        self.assertIn("Usage", self.run_command("queuemove ann"))
        self.session.sent.clear()
        self.assertIn("Usage", self.run_command("queueremove ann"))

    def test_a_refusal_is_said(self):
        self.assertIn("already first", self.run_command("queuemove ann up"))

    def test_the_commands_are_listed_and_documented(self):
        docs = read("docs", "ADMIN-CONSOLE.md")
        for command in ("queuemove", "queueremove"):
            self.assertIn(command, adminchat.COMMANDS)
            self.assertIn("`" + command + " ", docs)


class TheDashboardRoutes(Base):

    def test_move_user(self):
        status, result = webserver.build_queue_move_user_result({"nick": "cat", "direction": "up"})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"], "cat")
        self.assertEqual(commands.queue_order(), ["ann", "cat", "bob"])

    def test_move_user_refused(self):
        status, result = webserver.build_queue_move_user_result({"nick": "ann", "direction": "up"})
        self.assertEqual(status, 400)
        self.assertIn("already first", result["error"])

    def test_move_file(self):
        status, _ = webserver.build_queue_move_file_result(
            {"nick": "ann", "position": "2", "id": row_id("ann", 2), "direction": "down"})
        self.assertEqual(status, 200)
        self.assertEqual(names("ann"), ["a1.mp3", "a3.mp3", "a2.mp3"])

    def test_move_file_of_a_queue_that_changed_is_refused(self):
        status, result = webserver.build_queue_move_file_result(
            {"nick": "ann", "position": "2", "id": dcc.queue_row_id(row("gone.mp3")), "direction": "down"})
        self.assertEqual(status, 400)
        self.assertIn("changed", result["error"])

    def test_remove_file(self):
        status, result = webserver.build_queue_remove_file_result(
            {"nick": "ann", "position": 1, "id": row_id("ann", 1)})
        self.assertEqual(status, 200)
        self.assertIn("a1.mp3", result["message"])
        self.assertEqual(names("ann"), ["a2.mp3", "a3.mp3"])
        self.assertEqual(len(self.notices), 1)

    def test_remove_file_without_a_nick_or_a_body(self):
        self.assertEqual(webserver.build_queue_remove_file_result({})[0], 400)
        self.assertEqual(webserver.build_queue_move_file_result({})[0], 400)
        self.assertEqual(webserver.build_queue_move_user_result({})[0], 400)

    def test_clear_takes_everything_the_nick_had(self):
        status, result = webserver.build_queue_clear_result({"nick": "Ann"})
        self.assertEqual(status, 200)
        self.assertEqual(result["removed"], 3)
        self.assertNotIn("ann", config.dcc_queue)
        self.assertIn("bob", config.dcc_queue)

    def test_clear_of_a_nick_with_nothing_is_a_404(self):
        self.assertEqual(webserver.build_queue_clear_result({"nick": "zed"})[0], 404)
        self.assertEqual(webserver.build_queue_clear_result({"nick": "two words"})[0], 400)
        self.assertEqual(webserver.build_queue_clear_result({})[0], 400)

    def test_the_routes_exist(self):
        source = read("src", "webserver.py")
        for route in ("/api/queue/move-user", "/api/queue/move-file",
                      "/api/queue/remove-file", "/api/queue/clear"):
            self.assertIn('"' + route + '", methods=["POST"]', source)


class ThePageHasTheButtons(unittest.TestCase):

    def test_every_control_is_wired_to_its_route(self):
        js = read("web", "app.js")
        for cls in ("qmove-user-btn", "qmove-file-btn", "qremove-btn", "qclear-btn"):
            self.assertIn('contains("' + cls + '")', js)
            self.assertIn('"' + cls, js)
        for route in ("/api/queue/move-user", "/api/queue/move-file",
                      "/api/queue/remove-file", "/api/queue/clear"):
            self.assertIn('"' + route + '"', js)

    def test_clearing_asks_first(self):
        js = read("web", "app.js")
        at = js.index('contains("qclear-btn")')
        self.assertIn("window.confirm(", js[at:at + 400])

    def test_the_open_files_stay_open_through_a_refresh(self):
        js = read("web", "app.js")
        self.assertIn('details.queue-files[open]', js)

    def test_the_strings_are_in_every_language(self):
        import json
        for lang in ("en", "es", "fr"):
            with open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as handle:
                strings = json.load(handle)
            for key in ("queue.earlier", "queue.later", "queue.clear", "queue.fileEarlier",
                        "queue.fileLater", "queue.removeFile", "queue.clearConfirm", "queue.controlFailed"):
                self.assertTrue(strings.get(key), (lang, key))


if __name__ == "__main__":
    unittest.main()
