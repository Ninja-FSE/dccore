"""#1233: ticking many folders of a RAR list queues them all, and the
dispatcher sends one folder per bot at a time.

A "!rar" folder is matched to its offer by the bot alone (the sender names the
.rar), so two of them waiting for an offer from one bot could not be told
apart. Before this, the second was refused at the door; now every folder
queues and the dispatcher lets the next go when the one ahead has ended.
Download selected in a RAR list sends each ticked row as a folder, not as a
file whose name the sender never uses.
"""

import json
import os
import re
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

WEB = os.path.join(REPO_ROOT, "web")


class FolderCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=3)
        config.channel_users["#chan"] = {"goodbot", "otherbot"}

    def folders(self, count, bot="GoodBot"):
        rids = []
        for n in range(count):
            status, result = webserver.build_folder_rar_fetch_enqueue_result(bot, f"Artist/Album {n:03d}")
            self.assertEqual(status, 200, result)
            rid = result["created"][0]
            config.fetch_queue[rid]["requested_at"] = time.time() - 1000 + n
            rids.append(rid)
        return rids

    def states(self, rids):
        return [config.fetch_queue[rid]["state"] for rid in rids]

    def asked(self):
        return [msg for _user, msg, *_ in self.oserve.queued if "PRIVMSG" in msg]


class QueueingMany(FolderCase):
    def test_a_hundred_folders_all_queue(self):
        rids = self.folders(100)
        self.assertEqual(len(set(rids)), 100)
        self.assertEqual(self.states(rids), ["pending"] * 100)

    def test_the_same_folder_twice_is_one_row(self):
        first = webserver.build_folder_rar_fetch_enqueue_result("GoodBot", "Artist/Album")[1]["created"][0]
        second = webserver.build_folder_rar_fetch_enqueue_result("GoodBot", "Artist/Album")[1]["created"][0]
        self.assertEqual(first, second)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_a_folder_asked_for_again_after_it_failed_is_a_new_row(self):
        first = self.folders(1)[0]
        config.fetch_queue[first]["state"] = "failed"
        second = webserver.build_folder_rar_fetch_enqueue_result("GoodBot", "Artist/Album 000")[1]["created"][0]
        self.assertNotEqual(first, second)

    def test_a_list_still_refuses_to_share_the_bot_with_a_folder(self):
        self.folders(1)
        self.assertEqual(webserver.build_list_fetch_enqueue_result("GoodBot")[0], 409)

    def test_a_folder_still_refuses_to_queue_behind_a_list(self):
        self.assertEqual(webserver.build_list_fetch_enqueue_result("GoodBot")[0], 200)
        self.assertEqual(webserver.build_folder_rar_fetch_enqueue_result("GoodBot", "Artist/Album")[0], 409)

    def test_another_bot_may_queue_its_own_folder(self):
        self.folders(1)
        self.assertEqual(webserver.build_folder_rar_fetch_enqueue_result("OtherBot", "Artist/Album")[0], 200)


class SentOneAtATime(FolderCase):
    def test_only_the_first_folder_goes_out(self):
        rids = self.folders(5)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"] + ["pending"] * 4)
        self.assertEqual(config.fetch_queue[rids[1]]["waiting"], "one-at-a-time")
        self.assertEqual(len(self.asked()), 1)
        self.assertIn("Album 000", self.asked()[0])

    def test_nothing_more_goes_while_the_first_is_queued_or_arriving(self):
        rids = self.folders(3)
        dcc_fetch.check_fetch_queue()
        for state in ("queued", "receiving"):
            config.fetch_queue[rids[0]]["state"] = state
            dcc_fetch.check_fetch_queue()
            self.assertEqual(self.states(rids)[1:], ["pending", "pending"], state)

    def test_the_next_goes_when_one_has_ended_either_way(self):
        rids = self.folders(3)
        dcc_fetch.check_fetch_queue()
        config.fetch_queue[rids[0]]["state"] = "complete"
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["complete", "offered", "pending"])
        config.fetch_queue[rids[1]]["state"] = "failed"
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["complete", "failed", "offered"])
        self.assertNotIn("waiting", config.fetch_queue[rids[2]])

    def test_the_oldest_goes_first(self):
        rids = self.folders(3)
        config.fetch_queue[rids[2]]["requested_at"] = 1.0
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["pending", "pending", "offered"])

    def test_another_bot_is_not_held_up(self):
        rids = self.folders(2)
        other = self.folders(1, bot="OtherBot")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered", "pending"])
        self.assertEqual(self.states(other), ["offered"])

    def test_files_from_the_same_bot_are_not_held_by_a_folder(self):
        self.folders(1)
        file_rid = dcc_fetch.enqueue_fetch("GoodBot", "Track.flac")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[file_rid]["state"], "offered")

    def test_a_list_row_waits_for_a_folder_already_on_its_way(self):
        rids = self.folders(1)
        dcc_fetch.check_fetch_queue()
        list_rid = "list00000001"
        config.fetch_queue[list_rid] = dcc_fetch.new_fetch_row("GoodBot", "", request_type="list")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"])
        self.assertEqual(config.fetch_queue[list_rid]["state"], "pending")
        self.assertEqual(config.fetch_queue[list_rid]["waiting"], "one-at-a-time")


class TheBatchRoute(FolderCase):
    def test_every_folder_in_the_body_is_queued(self):
        body = [{"bot": "GoodBot", "folder": f"Artist/Album {n}"} for n in range(20)]
        status, result = webserver.build_folder_rar_batch_enqueue_result(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 20)
        self.assertEqual(result["errors"], [])

    def test_a_bad_item_is_reported_and_the_rest_still_queue(self):
        body = [{"bot": "GoodBot", "folder": "A/One"}, "junk", {"bot": "GoodBot", "folder": ""}]
        status, result = webserver.build_folder_rar_batch_enqueue_result(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["errors"]), 2)

    def test_nothing_queued_carries_the_first_error(self):
        status, result = webserver.build_folder_rar_batch_enqueue_result([{"bot": "GoodBot", "folder": ""}])
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])

    def test_a_body_that_is_not_a_list_is_refused(self):
        self.assertEqual(webserver.build_folder_rar_batch_enqueue_result({"bot": "x"})[0], 400)
        self.assertEqual(webserver.build_folder_rar_batch_enqueue_result([])[0], 400)

    def test_too_many_items_are_refused_outright(self):
        body = [{"bot": "GoodBot", "folder": f"A/{n}"} for n in range(webserver.FETCH_ENQUEUE_MAX_ITEMS + 1)]
        self.assertEqual(webserver.build_folder_rar_batch_enqueue_result(body)[0], 413)
        self.assertEqual(config.fetch_queue, {})

    def test_the_route_takes_a_list_and_still_takes_one_object(self):
        with open(os.path.join(REPO_ROOT, "src", "webserver.py"), encoding="utf-8") as handle:
            source = handle.read()
        route = source[source.index('def api_filelists_fetch_folder_rar'):]
        route = route[:route.index("@app.route")]
        self.assertIn("isinstance(payload, list)", route)
        self.assertIn("build_folder_rar_batch_enqueue_result(payload)", route)
        self.assertIn("build_folder_rar_fetch_enqueue_result(", route)


class DownloadSelectedInARarList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(WEB, "app.js"), encoding="utf-8") as handle:
            cls.js = handle.read()
        start = cls.js.index('el.filelistsDownloadSelectedBtn.addEventListener("click"')
        cls.handler = cls.js[start:cls.js.index("function folderGroupsFrom", start)]

    def test_a_ticked_folder_row_goes_to_the_folder_route(self):
        self.assertIn("box.dataset.rarFolder", self.handler)
        self.assertIn('postJson("/api/filelists/fetch-folder-rar", folders)', self.handler)

    def test_the_other_rows_still_go_as_files(self):
        self.assertIn('postJson("/api/fetch/enqueue", items)', self.handler)

    def test_the_checkbox_carries_the_rows_folder_and_bot(self):
        start = self.js.index("function attachFilelistsCheckboxData")
        body = self.js[start:self.js.index("// Same reasoning and same pattern as attachFilelistsCheckboxData()", start)]
        self.assertIn("boxes[i].dataset.rarFolder = row.rar_folder", body)
        self.assertIn("splitFetchedSource(group.bot || state.filelistsSource).nick", body)

    def test_the_waiting_reason_has_text_in_every_language(self):
        self.assertIn('"one-at-a-time": "download.waiting.oneAtATime"', self.js)
        for lang in ("en", "es", "fr"):
            with open(os.path.join(WEB, "lang", lang + ".json"), encoding="utf-8") as handle:
                self.assertIn("download.waiting.oneAtATime", json.load(handle))

    def test_the_admin_chat_names_the_reason_too(self):
        import adminchat
        self.assertIn("one-at-a-time", adminchat.DOWNLOAD_WAITING_NOTES)


if __name__ == "__main__":
    unittest.main()
