"""One nick asking for the same file again does not queue it again (#1077).

A nick pasting the same request every few seconds got a new row in its queue
each time, up to MAX_USER_QUEUE, and every copy was then sent in turn: a
queue of a hundred copies of one multi-GB ISO. Not a flood in speed - file
requests are not metered (#888) and should not be - but the queue took the
same file twice.

Now a request for what is already waiting in that nick's own queue is not
added; the nick is told once, privately, where it already is. The path
decides, not the name (#110: two albums can hold a track of the same name).
"""

import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests import test_the_feed_says_which_channel as feed  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

OTHER = feed.OTHER
ALBUM = "!rar Metallica/Black Album (1991)"


class Case(feed.ServesARealRequest):
    def setUp(self):
        super().setUp()
        write_master_list(self.tree.lists, "DCCoreTest",
                          [(None, [("Song.flac", "4KB"), ("Other.flac", "4KB")])])
        with io.open(os.path.join(self.tree.music, "Other.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        # Slots full, so every request queues rather than sends.
        self.fill_the_slots()

    def rows(self, user="dave"):
        return [row["file"] for row in config.dcc_queue.get(user, [])]

    def notices(self, text, user="dave"):
        return [m for who, m, *_ in self.oserve.queued if who == user and text in m]


class AFileAlreadyWaiting(Case):
    def test_asking_again_does_not_add_a_second_row(self):
        self.request("Song.flac")
        self.request("Song.flac")

        self.assertEqual(self.rows(), ["Song.flac"])

    def test_the_nick_is_told_where_it_already_is(self):
        self.request("Song.flac")
        self.request("Other.flac")
        self.request("Song.flac")

        told = self.notices("is already in your personal queue")
        self.assertEqual(len(told), 1, told)
        self.assertIn("Song.flac", told[0])
        self.assertIn("position #1", told[0])
        self.assertTrue(told[0].startswith("NOTICE dave :"))

    def test_a_run_of_repeats_is_told_once_not_every_time(self):
        self.request("Song.flac")
        for _ in range(25):
            self.request("Song.flac")

        self.assertEqual(self.rows(), ["Song.flac"])
        self.assertEqual(len(self.notices("is already in your personal queue")), 1)

    def test_another_file_is_still_queued(self):
        self.request("Song.flac")
        self.request("Other.flac")

        self.assertEqual(self.rows(), ["Song.flac", "Other.flac"])

    def test_the_second_repeat_of_another_file_is_told_too(self):
        """The memory is per file: a second duplicate is not silenced by the first."""
        self.request("Song.flac")
        self.request("Other.flac")
        self.request("Song.flac")
        self.request("Other.flac")

        told = self.notices("is already in your personal queue")
        self.assertEqual(sorted("Song.flac" in t for t in told), [False, True])
        self.assertEqual(self.rows(), ["Song.flac", "Other.flac"])

    def test_another_nick_asking_for_it_is_queued_as_usual(self):
        config.channel_users[OTHER].add("erin")
        self.request("Song.flac", user="dave")
        self.request("Song.flac", user="erin")

        self.assertEqual(self.rows("dave"), ["Song.flac"])
        self.assertEqual(self.rows("erin"), ["Song.flac"])

    def test_once_it_has_left_the_queue_it_can_be_asked_for_again(self):
        self.request("Song.flac")
        config.dcc_queue["dave"].clear()
        self.request("Song.flac")

        self.assertEqual(self.rows(), ["Song.flac"])
        self.assertEqual(self.notices("is already in your personal queue"), [])

    def test_at_the_cap_a_repeat_is_told_it_is_queued_not_that_the_queue_is_full(self):
        self.set_config(MAX_USER_QUEUE=1)
        self.request("Song.flac")
        self.request("Song.flac")

        self.assertEqual(len(self.notices("is already in your personal queue")), 1)
        self.assertEqual(self.notices("personal queue limit"), [])

    def test_the_refusal_is_on_the_console(self):
        out = io.StringIO()
        self.request("Song.flac")
        import contextlib
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, "dave", "Song.flac", OTHER)

        self.assertIn("asked again for 'Song.flac': already queued at #1", out.getvalue())

    def test_the_request_is_still_on_the_feed(self):
        """The operator still sees someone asking."""
        self.request("Song.flac")
        self.request("Song.flac")

        self.assertEqual(self.kinds().count("REQUEST"), 2)
        self.assertEqual(self.kinds().count("QUEUED"), 1)


class AFolderAlreadyWaiting(Case):
    def test_asking_for_the_same_folder_again_adds_nothing(self):
        self.request(ALBUM)
        self.request(ALBUM)

        self.assertEqual(len(config.dcc_queue.get("dave", [])), 1)
        self.assertEqual(len(self.notices("is already in your personal queue")), 1)

    def test_a_file_and_a_folder_are_not_taken_for_each_other(self):
        self.request("Song.flac")
        self.request(ALBUM)

        self.assertEqual(len(config.dcc_queue.get("dave", [])), 2)


class AFolderThatHasBeenPacked(Case):
    """The packer replaces the row's path with the archive (dcc.py,
    "next_file['path'] = target_rar_path"), so a repeat !rar of the folder no
    longer matched and queued a second pack of the whole album."""

    def pack(self):
        """What the packer does to the head row when rar has finished."""
        row = config.dcc_queue["dave"][0]
        row["source_path"] = row.get("source_path") or row.get("path")
        row["path"] = os.path.join(self.tree.root, "tmp_zip", "Black Album (1991).rar")
        row["file"] = "Black Album (1991).rar"
        row["is_unpacked_rar_folder"] = False

    def test_a_repeat_while_the_archive_is_being_sent_is_still_a_repeat(self):
        self.request(ALBUM)
        self.pack()
        self.request(ALBUM)

        self.assertEqual(len(config.dcc_queue["dave"]), 1)
        told = self.notices("is already in your personal queue")
        self.assertEqual(len(told), 1, told)
        self.assertIn("position #1", told[0])

    def test_the_place_is_found_through_the_folder_it_came_from(self):
        self.request(ALBUM)
        folder = config.dcc_queue["dave"][0]["path"]
        self.pack()

        self.assertEqual(dcc.queued_position_of("dave", folder), 1)
        self.assertIsNone(dcc.queued_position_of("dave", config.dcc_queue["dave"][0]["path"]),
                          "the archive is not what anybody asks for")

    def test_the_folder_is_kept_in_the_queue_file_with_the_row(self):
        import json
        self.request(ALBUM)
        self.pack()

        row = json.loads(json.dumps(config.dcc_queue["dave"]))[0]
        self.assertEqual(row["source_path"], os.path.join(self.tree.music, "Metallica", "Black Album (1991)"))

    def test_the_packer_keeps_the_folder_before_it_overwrites_the_path(self):
        """Pinned in the source: the real pack needs rar. The row must say
        where it came from before `path` becomes the archive."""
        import re
        body = feed.source("dcc.py")
        match = re.search(r"next_file\['source_path'\] = .*\n\s*next_file\['path'\] = target_rar_path", body)
        self.assertIsNotNone(match, "the packer overwrites path without keeping the folder")


class AFileThatWasSentAtOnce(feed.ServesARealRequest):
    """A free slot sends at once and never gets a queue row (#1086), so a
    repeat a few seconds later was queued and the file went out twice."""

    def setUp(self):
        super().setUp()
        announce._told_queue_full.clear()
        self.addCleanup(announce._told_queue_full.clear)
        config.user_processing_lock = set()
        self.set_config(MAX_DCC_SLOTS=3)

    def notices(self, text, user="dave"):
        return [m for who, m, *_ in self.oserve.queued if who == user and text in m]

    def test_a_repeat_while_it_is_sending_adds_no_row(self):
        self.request("Song.flac")
        self.request("Song.flac")

        self.assertEqual(config.dcc_queue.get("dave", []), [])
        self.assertEqual(len(config.active_transfers), 1)
        self.assertEqual(len(feed.InlineThread.dispatched), 1)

    def test_the_nick_is_told_it_is_being_sent(self):
        self.request("Song.flac")
        self.request("Song.flac")
        self.request("Song.flac")

        told = self.notices("is already being sent to you")
        self.assertEqual(len(told), 1, told)
        self.assertIn("Song.flac", told[0])
        self.assertTrue(told[0].startswith("NOTICE dave :"))
        self.assertEqual(self.notices("personal queue"), [])

    def test_the_refusal_is_on_the_console(self):
        self.request("Song.flac")
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            dcc.handle_download_request(self.sock, "dave", "Song.flac", OTHER)

        self.assertIn("asked again for 'Song.flac': it is being sent to them now", out.getvalue())

    def test_another_nick_is_sent_it_as_usual(self):
        config.channel_users[OTHER].add("erin")
        self.request("Song.flac", user="dave")
        self.request("Song.flac", user="erin")

        self.assertEqual(len(config.active_transfers), 2)
        self.assertEqual(self.notices("being sent", user="erin"), [])

    def test_once_the_send_is_over_it_can_be_asked_for_again(self):
        self.request("Song.flac")
        config.active_transfers.clear()
        config.user_processing_lock.discard("dave")
        self.request("Song.flac")

        self.assertEqual(len(feed.InlineThread.dispatched), 2)
        self.assertEqual(self.notices("is already being sent"), [])

    def test_another_file_while_one_is_sending_is_still_queued(self):
        write_master_list(self.tree.lists, "DCCoreTest",
                          [(None, [("Song.flac", "4KB"), ("Other.flac", "4KB")])])
        with io.open(os.path.join(self.tree.music, "Other.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.request("Song.flac")
        self.request("Other.flac")

        self.assertEqual([row["file"] for row in config.dcc_queue["dave"]], ["Other.flac"])

    def test_the_entry_carries_the_path_it_is_sending(self):
        self.request("Song.flac")

        self.assertEqual(config.active_transfers[0]["path"], os.path.join(self.tree.music, "Song.flac"))


class ThePathDecides(Case):
    def put(self, *paths):
        config.dcc_queue["dave"] = [{"file": "Intro.flac", "path": path} for path in paths]

    def test_the_same_name_in_another_album_is_not_a_repeat(self):
        self.put(os.path.join("lib", "Album One", "Intro.flac"))

        self.assertIsNone(dcc.queued_position_of("dave", os.path.join("lib", "Album Two", "Intro.flac")))

    def test_the_same_path_spelled_another_way_is_found(self):
        self.put(os.path.join("lib", "Album One", "Intro.flac"))

        spelled = os.path.join("lib", "Album Two", "..", "Album One", "Intro.flac")
        self.assertEqual(dcc.queued_position_of("dave", spelled), 1)

    def test_the_place_is_its_place_in_the_queue(self):
        self.put("a/one.flac", "a/two.flac", "a/three.flac")

        self.assertEqual(dcc.queued_position_of("dave", "a/three.flac"), 3)

    def test_a_nick_with_no_queue_has_no_repeat(self):
        self.assertIsNone(dcc.queued_position_of("nobody", "a/one.flac"))

    def test_a_row_without_a_path_is_not_a_match(self):
        config.dcc_queue["dave"] = [{"file": "x.flac"}, {"file": "y.flac", "path": None}]

        self.assertIsNone(dcc.queued_position_of("dave", "a/one.flac"))


if __name__ == "__main__":
    import unittest
    unittest.main()
