"""The operator's queue controls work on the queue as it really is (#1245).

Found after #1206's controls met #1205's list-first rule:

1. Moving a nick re-stamped every waiting nick in the list-first order, so a
   nick with a list at its head got the oldest wait stamp - kept once the list
   had gone (a list is not a turn), and its files then jumped the whole line.
2. The Queue page ranked nicks by the wait alone while the dispatcher ranks a
   waiting list first, so the arrows moved against a line the page did not show.
3. A queued file being sent was looked for by its path, which only a send that
   never queued carries: it could be moved past, or removed while it arrived.
4. The pack guard took the packing row to be the first, which a list sent to
   the front is not.
5. "The queue has changed" compared display names, so a removal could take the
   other album's Intro.mp3 - and without a name nothing was checked at all.

Driven through the real request, dispatch and pack code wherever the claim on a
row is what matters, so the claims are the ones dcc.py makes.
"""

import contextlib
import io
import json
import os
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import commands  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests import test_the_longest_waiting_nick_gets_the_slot as fair  # noqa: E402
from tests import test_a_list_request_goes_ahead_of_queued_files as lists  # noqa: E402
from tests import test_a_running_pack_can_be_seen_and_cancelled as packing  # noqa: E402
from tests.support import DCCoreTestCase, no_disk_writes, queue_row  # noqa: E402
from tests.test_dccore_downloads_window import make_session  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NICKS = {"kilo", "alfa", "bravo", "zulu"}


def queued(nick):
    return [row["file"] for row in config.dcc_queue.get(nick, [])]


class ALineWithAListWaiting(lists.Case):
    """kilo holds the one slot; alfa, bravo and zulu wait in that order, and
    zulu has asked for the list, so zulu is served first."""

    def setUp(self):
        super().setUp()
        config.channel_users[feed.OTHER] = set(NICKS)
        self.busy_with_files("kilo")
        self.request("B.flac", user="alfa")
        self.request("C.flac", user="bravo")
        self.request("D.flac", user="zulu")
        self.ask_for_the_list("zulu")
        self.assertEqual(commands.queue_order(), ["zulu", "alfa", "bravo"])

    def serve_the_line(self, holder, slots):
        """Let `slots` slots go out one after the other, each finished in turn."""
        served = []
        for _ in range(slots):
            self.finish(holder)
            self.pick_up(holder)
            holder = self.running()[0]
            served.append(self.started()[-1])
        return served


class AMoveSwapsOnlyTheTwoNicks(ALineWithAListWaiting):

    def test_a_list_nick_gets_no_older_wait_from_a_move_it_was_not_in(self):
        ok, message = commands.move_waiting_user("bravo", "up")

        self.assertTrue(ok, message)
        self.assertEqual(self.serve_the_line("kilo", 4),
                         [("zulu", self.list_name), ("bravo", "C.flac"),
                          ("alfa", "B.flac"), ("zulu", "D.flac")])

    def test_no_other_nick_is_re_stamped(self):
        before = dict(runtime.queue_waiting_since)

        commands.move_waiting_user("bravo", "up")

        after = runtime.queue_waiting_since
        self.assertEqual(after["zulu"], before["zulu"])
        self.assertEqual((after["alfa"], after["bravo"]), (before["bravo"], before["alfa"]))

    def test_the_reply_is_the_place_after_the_move(self):
        ok, message = commands.move_waiting_user("bravo", "up")

        self.assertTrue(ok)
        place = commands.queue_order().index("bravo") + 1
        self.assertIn(f"number {place} of 3", message)

    def test_a_nick_is_not_moved_past_a_list_and_the_reply_says_where_it_is(self):
        before = dict(runtime.queue_waiting_since)

        ok, message = commands.move_waiting_user("alfa", "up")

        self.assertFalse(ok)
        self.assertIn("zulu has a list waiting", message)
        self.assertIn("number 2 of 3", message)
        self.assertEqual(commands.queue_order(), ["zulu", "alfa", "bravo"])
        self.assertEqual(runtime.queue_waiting_since, before)

    def test_nor_is_the_list_moved_behind_a_wait(self):
        before = dict(runtime.queue_waiting_since)

        ok, message = commands.move_waiting_user("zulu", "down")

        self.assertFalse(ok)
        self.assertIn("number 1 of 3", message)
        self.assertEqual(runtime.queue_waiting_since, before)

    def test_the_console_says_the_refusal(self):
        session = make_session(self)
        adminchat._cmd_queuemove(session, "alfa up")
        self.assertIn("zulu has a list waiting", "\n".join(session.sent))

    def test_once_the_list_has_gone_the_files_wait_their_own_turn(self):
        commands.move_waiting_user("bravo", "up")
        self.serve_the_line("kilo", 1)

        self.assertEqual(self.started()[-1], ("zulu", self.list_name))
        self.assertEqual(commands.queue_order(), ["bravo", "alfa", "zulu"])


class ThePageShowsTheLineTheDispatcherServes(ALineWithAListWaiting):

    def waiting_on_the_page(self):
        return [row["user"] for row in webserver.build_queue_payload() if row["status"] == "queued"]

    def test_a_waiting_list_is_shown_first(self):
        self.assertEqual(self.waiting_on_the_page(), commands.queue_order())
        self.assertEqual(self.waiting_on_the_page(), ["zulu", "alfa", "bravo"])

    def test_and_after_a_move(self):
        commands.move_waiting_user("bravo", "up")
        self.assertEqual(self.waiting_on_the_page(), ["zulu", "bravo", "alfa"])

    def test_while_a_list_that_went_first_is_out_the_wait_decides_on_the_page_too(self):
        self.serve_the_line("kilo", 1)
        self.assertEqual(self.lists_that_went_first(), ["zulu"])

        users = [row["user"] for row in webserver.build_queue_payload()]

        self.assertEqual(users, commands.queue_order())

    def test_the_arrow_on_the_top_row_is_the_end_of_the_line(self):
        top = self.waiting_on_the_page()[0]
        ok, message = commands.move_waiting_user(top, "up")
        self.assertFalse(ok)
        self.assertIn("already first", message)


class _AQueuedFileInFlight:
    """alfa's first queued file is given the slot through the real dispatch;
    its row stays in alfa's queue until the send settles it. Mixed into one
    case per way the slot is handed out."""

    def setUp(self):
        super().setUp()
        config.channel_users[feed.OTHER] = set(NICKS)
        self.request("A.flac", user="kilo")
        self.request("B.flac", user="alfa")
        self.request("C.flac", user="alfa")
        self.request("D.flac", user="alfa")
        self.assertEqual(queued("alfa"), ["B.flac", "C.flac", "D.flac"])
        self.finish("kilo")
        self.dispatch()
        self.assertEqual(self.started()[-1], ("alfa", "B.flac"))

    def test_the_claim_has_no_path_and_the_row_is_still_queued(self):
        claim = [tx for tx in config.active_transfers if tx["user"] == "alfa"]
        self.assertEqual(len(claim), 1)
        self.assertNotIn("path", claim[0])
        self.assertEqual(queued("alfa"), ["B.flac", "C.flac", "D.flac"])

    def test_nothing_is_moved_past_it(self):
        ok, message = commands.move_queued_file("alfa", 2, "up")
        self.assertFalse(ok)
        self.assertIn("being sent", message)
        self.assertEqual(queued("alfa"), ["B.flac", "C.flac", "D.flac"])

    def test_it_is_not_moved(self):
        ok, message = commands.move_queued_file("alfa", 1, "down")
        self.assertFalse(ok)
        self.assertIn("being sent", message)
        self.assertEqual(queued("alfa"), ["B.flac", "C.flac", "D.flac"])

    def test_it_is_not_removed_and_the_nick_is_not_told_it_was(self):
        ok, message = commands.remove_queued_file("alfa", 1)
        self.assertFalse(ok)
        self.assertIn("being sent right now", message)
        self.assertEqual(queued("alfa"), ["B.flac", "C.flac", "D.flac"])
        self.assertEqual([m for _to, m, _v in self.oserve.queued if "Removed" in m], [])

    def test_the_files_behind_it_can_still_be_moved_and_removed(self):
        self.assertTrue(commands.move_queued_file("alfa", 3, "up")[0])
        self.assertEqual(queued("alfa"), ["B.flac", "D.flac", "C.flac"])
        self.assertTrue(commands.remove_queued_file("alfa", 3)[0])
        self.assertEqual(queued("alfa"), ["B.flac", "D.flac"])

    def test_once_it_is_settled_the_queue_moves_freely_again(self):
        row = config.dcc_queue["alfa"][0]
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.release_queue_entry("alfa", row, delivered=True)
        config.active_transfers[:] = [tx for tx in config.active_transfers if tx["user"] != "alfa"]

        self.assertEqual(queued("alfa"), ["C.flac", "D.flac"])
        self.assertTrue(commands.move_queued_file("alfa", 2, "up")[0])

    def test_the_page_route_refuses_it_too(self):
        status, result = webserver.build_queue_remove_file_result(
            {"nick": "alfa", "position": 1, "id": dcc.queue_row_id(config.dcc_queue["alfa"][0])})
        self.assertEqual(status, 400)
        self.assertIn("being sent", result["error"])


class SentByTheSweep(_AQueuedFileInFlight, fair.Case):
    """The slot kilo freed, handed out by the sweep over the waiting nicks."""

    def dispatch(self):
        self.pick_up("kilo")


class SentByItsOwnTrigger(_AQueuedFileInFlight, fair.Case):
    """The same slot, taken by a trigger for alfa itself."""

    def dispatch(self):
        self.pick_up("alfa")


class _APackRunning(packing.RunningPackTests):
    """A real pack, with a list sent to the front of the same queue while it runs."""

    def setUp(self):
        super().setUp()
        config.channel_users["#somechannel"] = config.channel_users["#somechannel"] | {"alfa"}
        folder = os.path.join(self.tree.music, "Artist", "Album One")
        os.makedirs(folder, exist_ok=True)
        with io.open(os.path.join(folder, "01.flac"), "wb") as handle:
            handle.write(b"\x00" * 5000)
        self.start_a_pack(user="alfa", album="Artist/Album One")
        with dcc.queue_lock:
            rows = config.dcc_queue["alfa"]
            self.packing_row = rows[0]
            rows.append(queue_row(user="alfa", filename="list.zip"))
            dcc.put_the_list_first(rows)
            rows.append(queue_row(user="alfa", filename="after.flac"))
        self.assertEqual(queued("alfa")[0], "list.zip")
        self.assertIs(config.dcc_queue["alfa"][1], self.packing_row)


for _name in [n for n in dir(packing.RunningPackTests) if n.startswith("test")]:
    setattr(_APackRunning, _name, None)


class ThePackGuardFindsThePackingRow(_APackRunning):

    def test_the_file_behind_it_does_not_move_past_it(self):
        ok, message = commands.move_queued_file("alfa", 3, "up")
        self.assertFalse(ok)
        self.assertIn("being packed", message)
        self.assertIs(config.dcc_queue["alfa"][1], self.packing_row)

    def test_it_does_not_move(self):
        ok, message = commands.move_queued_file("alfa", 2, "down")
        self.assertFalse(ok)
        self.assertIn("being packed", message)
        self.assertIs(config.dcc_queue["alfa"][1], self.packing_row)

    def test_it_is_not_removed_the_pack_is_cancelled_instead(self):
        ok, message = commands.remove_queued_file("alfa", 2)
        self.assertFalse(ok)
        self.assertIn("cancel the pack", message)
        self.assertIs(config.dcc_queue["alfa"][1], self.packing_row)

    def test_the_list_at_the_front_is_not_mistaken_for_it(self):
        ok, message = commands.remove_queued_file("alfa", 1)
        self.assertTrue(ok, message)
        self.assertEqual(queued("alfa")[1:], ["after.flac"])
        self.assertIs(config.dcc_queue["alfa"][0], self.packing_row)

    def test_the_packed_archive_being_sent_is_known_by_its_row_too(self):
        started = []
        real = dcc.start_dcc_send
        dcc.start_dcc_send = lambda *args, **kwargs: started.append(args)
        self.addCleanup(setattr, dcc, "start_dcc_send", real)
        with io.open(self.release, "w"):
            pass
        self.wait_for(lambda: started, "the archive's send to start")

        claim = [tx for tx in config.active_transfers if str(tx["user"]).lower() == "alfa"]
        self.assertEqual(len(claim), 1)
        self.assertNotIn("path", claim[0])
        ok, message = commands.move_queued_file("alfa", 3, "up")
        self.assertFalse(ok)
        self.assertIn("being sent", message)
        self.assertFalse(commands.remove_queued_file("alfa", 2)[0])


class TheRouteNamesTheFileNotItsName(DCCoreTestCase):
    """x.mp3 is sent first; the page shows AlbumOne's Intro.mp3 at place 2."""

    def setUp(self):
        super().setUp()
        no_disk_writes(dcc.db)
        config.frozen_queues = {}
        config.active_transfers = []
        config.dcc_queue = {"alfa": [
            queue_row(user="alfa", filename="x.mp3", path="/srv/library/x.mp3"),
            queue_row(user="alfa", filename="Intro.mp3", path="/srv/library/AlbumOne/Intro.mp3"),
            queue_row(user="alfa", filename="Intro.mp3", path="/srv/library/AlbumTwo/Intro.mp3"),
        ]}
        self.page = webserver.build_queue_payload(user="alfa")

    def paths(self):
        return [row["path"] for row in config.dcc_queue.get("alfa", [])]

    def test_the_page_has_one_id_per_file_and_no_path(self):
        self.assertEqual(len(self.page["file_ids"]), 3)
        self.assertEqual(len(set(self.page["file_ids"])), 3)
        self.assertNotIn("/srv/library", json.dumps(self.page))
        summary = [row for row in webserver.build_queue_payload() if row["user"] == "alfa"][0]
        self.assertEqual(summary["file_ids"], self.page["file_ids"])

    def test_the_other_albums_file_is_not_removed_in_its_place(self):
        shown = self.page["file_ids"][1]
        config.dcc_queue["alfa"].pop(0)

        status, result = webserver.build_queue_remove_file_result({"nick": "alfa", "position": 2, "id": shown})

        self.assertEqual(status, 400)
        self.assertIn("The queue has changed", result["error"])
        self.assertEqual(self.paths(), ["/srv/library/AlbumOne/Intro.mp3", "/srv/library/AlbumTwo/Intro.mp3"])

    def test_nor_moved_in_its_place(self):
        shown = self.page["file_ids"][1]
        config.dcc_queue["alfa"].pop(0)

        status, result = webserver.build_queue_move_file_result(
            {"nick": "alfa", "position": 2, "id": shown, "direction": "up"})

        self.assertEqual(status, 400)
        self.assertIn("The queue has changed", result["error"])
        self.assertEqual(self.paths(), ["/srv/library/AlbumOne/Intro.mp3", "/srv/library/AlbumTwo/Intro.mp3"])

    def test_the_file_the_page_meant_is_the_one_removed(self):
        status, _result = webserver.build_queue_remove_file_result(
            {"nick": "alfa", "position": 2, "id": self.page["file_ids"][1]})
        self.assertEqual(status, 200)
        self.assertEqual(self.paths(), ["/srv/library/x.mp3", "/srv/library/AlbumTwo/Intro.mp3"])

    def test_the_id_is_required(self):
        for body in ({"nick": "alfa", "position": 2},
                     {"nick": "alfa", "position": 2, "id": ""},
                     {"nick": "alfa", "position": 2, "file": "Intro.mp3"}):
            with self.subTest(body=body):
                self.assertEqual(webserver.build_queue_remove_file_result(body)[0], 400)
                self.assertEqual(webserver.build_queue_move_file_result(dict(body, direction="up"))[0], 400)
        self.assertEqual(len(self.paths()), 3)

    def test_the_id_follows_a_folder_through_its_pack(self):
        row = queue_row(user="alfa", filename="AlbumOne", path="/srv/library/AlbumOne",
                        is_unpacked_rar_folder=True)
        before = dcc.queue_row_id(row)
        row["source_path"], row["path"], row["file"] = row["path"], "/tmp/AlbumOne.rar", "AlbumOne.rar"
        self.assertEqual(dcc.queue_row_id(row), before)

    def test_the_console_still_goes_by_the_number_it_showed(self):
        self.assertTrue(commands.remove_queued_file("alfa", 2)[0])
        self.assertEqual(self.paths(), ["/srv/library/x.mp3", "/srv/library/AlbumTwo/Intro.mp3"])


class ThePageSendsTheId(unittest.TestCase):

    def setUp(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            self.js = handle.read()

    def test_both_file_controls_post_the_id_the_button_carries(self):
        for route in ("/api/queue/move-file", "/api/queue/remove-file"):
            at = self.js.index('queueControl("' + route + '"')
            call = self.js[at:self.js.index("});", at)]
            self.assertIn('id: target.getAttribute("data-id")', call)
            self.assertNotIn("file:", call)

    def test_the_button_carries_the_files_id_from_the_payload(self):
        at = self.js.index("function queueFileControls(")
        body = self.js[at:self.js.index("\n  }\n", at)]
        self.assertIn("row.file_ids", body)
        self.assertIn('data-id=\\""', body)
        self.assertNotIn("data-file", body)

    def test_the_comment_no_longer_says_the_file_in_flight_is_never_queued(self):
        self.assertNotIn("never puts the in-flight file into dcc_queue", self.js)


if __name__ == "__main__":
    unittest.main()
