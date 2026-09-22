"""A private-message request always resolved against the primary list
(audit M51, #653).

list_for_request() answers a target that is not a channel with the primary,
on the stated premise that a PM carries nothing to route on. But the
request text does: a `!Bot !rar <Label>/<Album>` row copied from a list
bound to #films names that list's folder label in its first component -
and sending such a row by /msg is a common habit with serving bots. Resolved
against the primary, the row was refused as not found (the requester got
no reply; the debug channel saw "Directory not found"), or, with the same
label and path under the primary too, the PRIMARY's folder was packed and
queued instead of the one the row advertised - the very outcome dcc.py
says routing exists to prevent.

The label routes it now: the primary keeps the request if it has the label;
otherwise the one other list that has it; two others naming the same folder
are one answer; two naming different folders are ambiguous and the user is
told to ask in the channel the row came from. A bare filename by PM is
still the primary's - it carries no label.
"""

import contextlib
import io
import json
import os
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug  # noqa: E402
from tests.test_path_security import InlineThread  # noqa: E402

BOT = "SomeBot"
USER = "dave"


class TwoListsOverTwoTrees(DCCoreTestCase):
    """Main (primary, #music) over a music tree; Films (#films) over a films
    tree with a "Some Studio/SomeFilm (2020)" folder that exists nowhere
    else (two levels down: a root's direct children are artist roots, which
    the packer refuses on purpose)."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.films = os.path.join(self.tree.root, "films")
        os.makedirs(os.path.join(self.films, "Some Studio", "SomeFilm (2020)"))
        with io.open(os.path.join(self.films, "Some Studio", "SomeFilm (2020)", "somefilm.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.write_lists([
            {"name": "Main", "primary": True, "channels": ["#music"],
             "folders": [{"name": "Music-root", "path": self.tree.music}]},
            {"name": "Films", "primary": False, "channels": ["#films"],
             "folders": [{"name": "Films-root", "path": self.films}]},
        ])
        self.set_config(NICKNAME=BOT, CHANNEL="#music,#films", RAR_ENABLED=True,
                        FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"),
                        bot_joined_channel=True, MAX_DCC_SLOTS=3)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        no_disk_writes(db)
        self.debug = silence_debug(announce)
        self.errors = []
        real_error = announce.send_dcc_error
        announce.send_dcc_error = lambda user, kind: self.errors.append((user, kind))
        self.addCleanup(setattr, announce, "send_dcc_error", real_error)
        InlineThread.dispatched = []
        self._real_thread = threading.Thread
        dcc.threading.Thread = InlineThread
        self.addCleanup(setattr, dcc.threading, "Thread", self._real_thread)

    def write_lists(self, lists):
        path = os.path.join(self.tree.root, "lists.json")
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(lists, handle)
        self.set_config(LISTS_FILE=path)

    def request(self, text, target=BOT):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(RecordingSocket(), USER, text, target)

    def queued_paths(self):
        return [row.get("path") or row.get("source_dir") or "" for row in config.dcc_queue.get(USER, [])]


class TheLabelRoutesThePrivateRow(TwoListsOverTwoTrees):

    def test_the_rule_itself(self):
        self.assertEqual(library.list_name_for_label("Films-root", "Main"), "Films")
        self.assertEqual(library.list_name_for_label("Music-root", "Main"), "Main")
        self.assertEqual(library.list_name_for_label("films-ROOT", "Main"), "Films", "labels are typed by users")
        self.assertEqual(library.list_name_for_label("Nobody-has-this", "Main"), "Main",
                         "no list has it: the primary, and the existence check fails as before")
        self.assertEqual(library.list_name_for_label("", "Main"), "Main")

    def test_a_films_row_sent_by_pm_is_answered_from_films(self):
        """The audit's failure, inverted: the row that used to be refused."""
        self.request("!rar Films-root/Some Studio/SomeFilm (2020)")

        self.assertEqual(self.errors, [])
        self.assertIn(USER, config.dcc_queue, "the request was refused")
        self.assertTrue(any("SomeFilm" in str(row.get("file", "")) or "SomeFilm" in str(row)
                            for row in config.dcc_queue[USER]), config.dcc_queue[USER])

    def test_the_same_row_in_its_own_channel_still_works(self):
        self.request("!rar Films-root/Some Studio/SomeFilm (2020)", target="#films")

        self.assertIn(USER, config.dcc_queue)

    def test_a_main_row_by_pm_is_the_primarys_as_before(self):
        self.request("!rar Music-root/Metallica/Black Album (1991)")

        self.assertEqual(self.errors, [])
        self.assertIn(USER, config.dcc_queue)

    def test_a_shared_label_goes_to_the_primary(self):
        """The wrong-file case: the same label under both lists means the
        primary keeps it - no worse than before, and never silently the
        other list."""
        self.write_lists([
            {"name": "Main", "primary": True, "channels": ["#music"],
             "folders": [{"name": "Shared", "path": self.tree.music}]},
            {"name": "Films", "primary": False, "channels": ["#films"],
             "folders": [{"name": "Shared", "path": self.films}]},
        ])

        self.assertEqual(library.list_name_for_label("Shared", "Main"), "Main")


class AnAmbiguousLabelIsRefusedWithDirections(TwoListsOverTwoTrees):

    def setUp(self):
        super().setUp()
        self.series = os.path.join(self.tree.root, "series")
        os.makedirs(self.series)
        self.write_lists([
            {"name": "Main", "primary": True, "channels": ["#music"],
             "folders": [{"name": "Music-root", "path": self.tree.music}]},
            {"name": "Films", "primary": False, "channels": ["#films"],
             "folders": [{"name": "Video", "path": self.films}]},
            {"name": "Series", "primary": False, "channels": ["#series"],
             "folders": [{"name": "Video", "path": self.series}]},
        ])

    def test_two_other_lists_naming_different_folders(self):
        self.assertIsNone(library.list_name_for_label("Video", "Main"))

    def test_the_user_is_told_where_to_ask(self):
        self.request("!rar Video/Some Studio/SomeFilm (2020)")

        self.assertEqual(self.errors, [(USER, "ambiguous_list")])
        self.assertNotIn(USER, config.dcc_queue)

    def test_two_lists_naming_the_same_folder_are_one_answer(self):
        self.write_lists([
            {"name": "Main", "primary": True, "channels": ["#music"],
             "folders": [{"name": "Music-root", "path": self.tree.music}]},
            {"name": "Films", "primary": False, "channels": ["#films"],
             "folders": [{"name": "Video", "path": self.films}]},
            {"name": "Mirror", "primary": False, "channels": ["#mirror"],
             "folders": [{"name": "Video", "path": self.films}]},
        ])

        self.assertEqual(library.list_name_for_label("Video", "Main"), "Films")

    def test_the_notice_exists(self):
        """send_dcc_error() knows the kind dcc.py sends."""
        import re
        with io.open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertRegex(source, r'"ambiguous_list": "Error: .*channel it was advertised in\."')


if __name__ == "__main__":
    unittest.main()
