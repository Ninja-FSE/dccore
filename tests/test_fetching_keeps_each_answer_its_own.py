"""Fetching from bots: every answer, file and list stays with what it belongs
to (#1269).

Four findings from an audit of fetching from other bots, each driven here
through the real functions:

1. The #1244 claim that hands a late answer to a pending "file" row never
   ran. It tested offered_at, and every real way back to pending - a busy
   reply, silence, a full disk, a restart - sets offered_at to None. Its
   own tests built a state the code never produces. A folder row offered
   to the same bot still took the file.
2. Deleting an old finished fetch deleted a newer fetch's file: once the
   old file was moved out of the folder, a later fetch of the same name was
   promoted to the same plain name, and both rows named one file.
3. A second channel's list request that fell back to the bot's main channel
   was merged as that channel's list and dropped the main and RAR lists;
   a main-list refresh that fell back to a second channel replaced the main
   list with that channel's.
4. Purging a bot left its second channels' lists on disk for good.
"""

import os
import socket
import threading
import time
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402
import webserver  # noqa: E402
from tests.test_list_fetch import _list_txt, _write_zip  # noqa: E402

BUSY = "Error: The server's global queue is full"


class FetchCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = self.make_temp_dir(prefix="dccore-1269-")
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#music", FETCH_MAX_PER_BOT=3, bot_joined_channel=True,
                        FETCHED_FILES_DIR=self.tmp)
        config.channel_users["#music"] = {"alfa", "bravo", "someuser"}

    def tick(self):
        dcc_fetch.check_fetch_queue()

    def claim(self, bot, name):
        with dcc_fetch._fetch_lock():
            return dcc_fetch._claim_matching_offer_locked(config.fetch_queue, bot, name)[0]


class ALateAnswerFindsItsPendingRow(FetchCase):
    """Finding 1: each real way back to pending, then the late answer."""

    def ask_file(self):
        rid = dcc_fetch.enqueue_fetch("alfa", "Song.flac")
        self.tick()
        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        return rid

    def offer_folder(self):
        rid = dcc_fetch.enqueue_fetch("alfa", "!rar Artist/Album", request_type="folder")
        self.assertIsNotNone(rid)
        self.tick()
        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        return rid

    def assert_pending_without_offered_at(self, rid):
        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "pending")
        self.assertIsNone(row["offered_at"], "the real path clears it - which is the bug")

    def test_after_a_busy_reply(self):
        file_rid = self.ask_file()
        self.assertEqual(dcc_fetch.handle_bot_reply("alfa", BUSY), "busy")
        self.assert_pending_without_offered_at(file_rid)
        folder_rid = self.offer_folder()

        self.assertEqual(self.claim("alfa", "Song.flac"), file_rid)
        self.assertEqual(config.fetch_queue[file_rid]["state"], "receiving")
        self.assertEqual(config.fetch_queue[folder_rid]["state"], "offered", "untouched")

    def test_after_silence(self):
        file_rid = self.ask_file()
        folder_rid = self.offer_folder()
        # No free slot, so the file is not asked again in the same tick.
        self.set_config(MAX_FETCH_SLOTS=1)
        config.fetch_queue[file_rid]["offered_at"] = time.time() - 3600
        self.tick()
        self.assert_pending_without_offered_at(file_rid)
        self.assertIn("asking again", config.fetch_queue[file_rid]["reason"])

        self.assertEqual(self.claim("alfa", "Song.flac"), file_rid)
        self.assertEqual(config.fetch_queue[folder_rid]["state"], "offered", "untouched")

    def test_after_a_restart(self):
        file_rid = self.ask_file()
        self.offer_folder()
        # The file row as it comes back from the history after a restart,
        # claimed before the next tick asks for it again.
        config.fetch_queue[file_rid] = dcc_fetch._restart_form(config.fetch_queue[file_rid])
        self.assert_pending_without_offered_at(file_rid)

        self.assertEqual(self.claim("alfa", "Song.flac"), file_rid)

    def test_after_a_hold_for_disk_space(self):
        import shutil
        real_usage = shutil.disk_usage
        free = [10 ** 9]     # 1 GB: not a low disk, but too little for the file
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 13, 0, free[0])
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)
        self.set_config(MAX_FETCH_FILE_SIZE=0)
        file_rid = self.ask_file()
        dcc_fetch.handle_incoming_offer(None, "alfa", f"DCC SEND Song.flac 2130706433 55000 {2 * 10 ** 9}")
        self.assert_pending_without_offered_at(file_rid)
        self.assertEqual(config.fetch_queue[file_rid]["waiting"], "disk-full")
        folder_rid = self.offer_folder()

        self.assertEqual(self.claim("alfa", "Song.flac"), file_rid)
        self.assertEqual(config.fetch_queue[folder_rid]["state"], "offered", "untouched")

    def test_a_request_dropped_before_it_went_out_was_never_asked(self):
        """Control: the line never left us, so an answer of that name is not
        this row's - the folder's own bot-alone match keeps it."""
        rid = self.ask_file()
        folder_rid = self.offer_folder()
        # What queue_mgr does with a line its cap trimmed off the send queue
        # before it went out: already removed, then handed back here.
        line = config.fetch_queue[rid]["request_line"]
        self.assertNotIn(line, config.fetch_request_queue)
        self.assertEqual(dcc_fetch.requests_not_sent([line]), 1)
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")
        self.assertNotIn("asked_before", config.fetch_queue[rid])

        self.assertEqual(self.claim("alfa", "Song.flac"), folder_rid)


class AFinishedFetchsFileIsItsOwn(FetchCase):
    """Finding 2: two rows never come to name one file."""

    def fetch(self, bot, name, payload):
        rid = dcc_fetch.enqueue_fetch(bot, name)
        self.tick()
        self.assertEqual(self.claim(bot, name), rid)
        row = config.fetch_queue[rid]
        dest_dir, stored = dcc_fetch._resolve_destination_path(rid, name)
        row["stored_filename"] = stored
        ours, theirs = socket.socketpair()

        def send():
            theirs.sendall(payload)
            try:
                while theirs.recv(4):
                    pass
            except OSError:
                pass
            theirs.close()

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        dcc_fetch._run_transfer(row, {"filename": name, "ip": "127.0.0.1", "port": 1,
                                      "size": len(payload)}, dest_dir, stored, sock=ours)
        sender.join(10)
        self.assertFalse(sender.is_alive())
        self.assertEqual(row["state"], "complete", row.get("reason"))
        return rid, row

    def path(self, row):
        return os.path.join(self.tmp, row["stored_filename"])

    def test_deleting_the_old_row_keeps_the_newer_rows_file(self):
        old, old_row = self.fetch("alfa", "cover.jpg", b"OLD" * 100)
        self.assertEqual(old_row["stored_filename"], "cover.jpg", "promoted to the plain name")
        # The operator moves the finished file into their library.
        os.replace(self.path(old_row), os.path.join(self.make_temp_dir(), "moved.jpg"))

        new, new_row = self.fetch("bravo", "cover.jpg", b"NEW" * 100)
        self.assertNotEqual(new_row["stored_filename"], "cover.jpg",
                            "a plain name another row still names is not taken")
        self.assertEqual(webserver.build_fetch_delete_result(old)[0], 200)

        with open(self.path(new_row), "rb") as fh:
            self.assertEqual(fh.read(), b"NEW" * 100)

    def test_the_plain_name_is_still_taken_when_no_row_names_it(self):
        """Control: the promotion itself still happens."""
        _rid, row = self.fetch("alfa", "cover.jpg", b"ONE" * 10)
        self.assertEqual(row["stored_filename"], "cover.jpg")

    def two_rows_naming_one_file(self):
        """What the promotion produced before this fix, still in histories."""
        first, first_row = self.fetch("alfa", "cover.jpg", b"OLD")
        second, second_row = self.fetch("bravo", "Other.jpg", b"NEW")
        os.replace(self.path(second_row), self.path(first_row))
        second_row["stored_filename"] = first_row["stored_filename"]
        return first, second, self.path(first_row)

    def test_a_delete_leaves_a_file_another_row_still_names(self):
        first, second, path = self.two_rows_naming_one_file()
        self.assertEqual(webserver.build_fetch_delete_result(first)[0], 200)
        self.assertTrue(os.path.exists(path))
        # The last row naming it takes it with it.
        self.assertEqual(webserver.build_fetch_delete_result(second)[0], 200)
        self.assertFalse(os.path.exists(path))

    def test_a_batch_delete_leaves_it_too_unless_the_batch_holds_both(self):
        first, second, path = self.two_rows_naming_one_file()
        self.assertEqual(webserver.build_fetch_delete_many_result([first])[1]["cancelled"], [first])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(webserver.build_fetch_delete_many_result([second])[1]["cancelled"], [second])
        self.assertFalse(os.path.exists(path))

    def test_a_batch_of_both_removes_the_file(self):
        first, second, path = self.two_rows_naming_one_file()
        webserver.build_fetch_delete_many_result([first, second])
        self.assertFalse(os.path.exists(path))


class AListStaysInItsOwnChannel(FetchCase):
    """Finding 3: neither kind of list request takes the dispatcher's
    fallback into a channel whose list means something else."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#music,#video,#jazz")
        config.channel_users["#video"] = {"alfa", "someuser"}
        config.channel_users["#jazz"] = {"someuser"}

    def zip_of(self, name, members):
        path = os.path.join(self.make_temp_dir(), name)
        _write_zip(path, [(member, _list_txt(base_name="alfa", files=((title, "10.0MB"),)))
                          for member, title in members])
        return path

    def hold_main_and_rar(self):
        ok, why = list_fetch.process_fetched_list_zip("alfa", self.zip_of("m.zip", [
            ("alfa-2026-08-27.txt", "Song.flac"), ("alfa-RAR-2026-08-27.txt", "Album.rar")]),
            channel="#music")
        self.assertTrue(ok, why)

    def hold_video(self):
        ok, why = list_fetch.process_fetched_list_zip("alfa", self.zip_of("v.zip", [
            ("alfa-2026-08-27.txt", "Clip.mkv")]), channel="#video", secondary=True)
        self.assertTrue(ok, why)

    def held(self):
        return {marker: info.get("channel")
                for marker, info in config.fetched_bot_lists["alfa"]["lists"].items()}

    def requests_sent(self):
        return [line for _user, line, *_rest in self.oserve.queued if "@alfa" in line]

    def test_a_second_channels_request_is_not_sent_to_the_main_channel(self):
        self.hold_main_and_rar()
        config.channel_users["#video"] = {"someuser"}     # alfa has left #video
        rid = dcc_fetch.enqueue_fetch("alfa", "", request_type="list", channel="#video",
                                      secondary_channel=True)
        self.tick()

        self.assertEqual(config.fetch_queue[rid]["state"], "failed")
        self.assertIn("#video", config.fetch_queue[rid]["reason"])
        self.assertEqual(self.requests_sent(), [])

    def test_a_main_channel_answer_is_not_merged_as_a_second_channels_list(self):
        self.hold_main_and_rar()
        before = self.held()
        ok, why = list_fetch.process_fetched_list_zip("alfa", self.zip_of("again.zip", [
            ("alfa-2026-08-28.txt", "Song2.flac")]), channel="#music", secondary=True)

        self.assertFalse(ok)
        self.assertIn("main list", why)
        self.assertEqual(self.held(), before)
        self.assertTrue(list_index.search(["Album"], bots=["alfa/RAR"]), "the RAR list's rows stay")
        self.assertFalse(os.path.exists(list_fetch.secondary_channel_extract_dir("alfa", "#music")),
                         "nothing was extracted")

    def test_a_main_refresh_is_not_sent_to_a_second_channel(self):
        self.hold_main_and_rar()
        self.hold_video()
        config.channel_users["#music"] = {"someuser"}     # alfa is out of #music for now
        rid = dcc_fetch.enqueue_fetch("alfa", "", request_type="list", channel="#music")
        self.tick()

        self.assertEqual(config.fetch_queue[rid]["state"], "failed")
        self.assertEqual(self.requests_sent(), [])

    def test_a_second_channels_answer_is_not_installed_as_the_main_list(self):
        self.hold_main_and_rar()
        self.hold_video()
        before = self.held()
        main_before = config.fetched_bot_lists["alfa"]["list_path"]
        ok, why = list_fetch.process_fetched_list_zip("alfa", self.zip_of("again.zip", [
            ("alfa-2026-08-28.txt", "Clip2.mkv")]), channel="#video")

        self.assertFalse(ok)
        self.assertIn("second channel", why)
        self.assertEqual(self.held(), before)
        self.assertEqual(config.fetched_bot_lists["alfa"]["channel"], "#music")
        self.assertEqual(config.fetched_bot_lists["alfa"]["list_path"], main_before)
        self.assertTrue(os.path.exists(main_before), "the main list's file is still there")

    def test_a_second_channels_request_still_goes_out_in_its_own_channel(self):
        """Control."""
        self.hold_main_and_rar()
        rid = dcc_fetch.enqueue_fetch("alfa", "", request_type="list", channel="#video",
                                      secondary_channel=True)
        self.tick()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.assertEqual(self.requests_sent(), ["PRIVMSG #video :@alfa\r\n"])

    def test_a_main_refresh_still_falls_back_to_a_channel_nothing_held_came_from(self):
        """Control: #1232's fallback stays for a channel that means nothing yet."""
        self.hold_main_and_rar()
        config.channel_users["#music"] = {"someuser"}
        config.channel_users["#video"] = {"someuser"}
        config.channel_users["#jazz"] = {"alfa", "someuser"}
        rid = dcc_fetch.enqueue_fetch("alfa", "", request_type="list", channel="#music")
        self.tick()

        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.assertEqual(self.requests_sent(), ["PRIVMSG #jazz :@alfa\r\n"])


class PurgingABotRemovesItsSecondChannelsLists(FetchCase):
    """Finding 4."""

    def fetch(self, bot, channel, secondary=False, title="Song.flac"):
        path = os.path.join(self.make_temp_dir(), "l.zip")
        _write_zip(path, [(f"{bot}-2026-08-27.txt", _list_txt(base_name=bot, files=((title, "10.0MB"),)))])
        ok, why = list_fetch.process_fetched_list_zip(bot, path, channel=channel, secondary=secondary)
        self.assertTrue(ok, why)

    def test_purge_removes_them_and_their_folder_tables(self):
        self.set_config(CHANNEL="#music,#video")
        self.fetch("alfa", "#music")
        self.fetch("alfa", "#video", secondary=True, title="Clip.mkv")
        self.fetch("bravo", "#music")
        self.fetch("bravo", "#video", secondary=True, title="Clip.mkv")
        alfa_video = list_fetch.secondary_channel_extract_dir("alfa", "#video")
        bravo_video = list_fetch.secondary_channel_extract_dir("bravo", "#video")
        self.assertTrue(os.path.isdir(alfa_video))
        forgotten = []
        real_forget = list_mod.forget_folder_tables
        list_mod.forget_folder_tables = lambda under=None, **kw: (forgotten.append(under),
                                                                  real_forget(under=under, **kw))
        self.addCleanup(setattr, list_mod, "forget_folder_tables", real_forget)

        ok, detail = list_fetch.purge_fetched_list("alfa")

        self.assertTrue(ok, detail)
        self.assertEqual(detail, "Purged everything held from alfa.")
        self.assertFalse(os.path.exists(os.path.dirname(alfa_video)))
        self.assertIn(list_fetch.secondary_channels_dir("alfa"), forgotten)
        self.assertTrue(os.path.isdir(bravo_video), "another bot's are its own")


if __name__ == "__main__":
    unittest.main()
