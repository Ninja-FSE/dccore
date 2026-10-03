"""Letting an old request go never cancels a newer one for the same file (#1083).

"@bot-remove <file>" is matched by name at the other bot and takes every entry
of ours for that file. Clear failed (#1047) sent it for each old "no response"
row - also when "Download again", or asking again from the list, had a newer
row waiting there for the same file. That newer request lost its place and sat
queued here with nothing coming until it timed out. Clear, and a row's own
Delete, now leave the other bot alone while another row still waits on the same
bot and file.
"""

import time
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402


class Case(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={})
        config.fetch_request_queue = []
        patch = mock.patch.object(dcc_fetch, "drop_our_request_at", return_value=True)
        self.drop = patch.start()
        self.addCleanup(patch.stop)

    def row(self, name, bot="PeerBot", **fields):
        rid = dcc_fetch.enqueue_fetch(bot, name)
        config.fetch_queue[rid].update(fields)
        return rid

    def old_silent(self, name="Song.flac", bot="PeerBot"):
        return self.row(name, bot=bot, state="failed", reason="no response", offered_at=time.time())


class Clear(Case):
    def test_a_newer_request_for_the_same_file_keeps_its_place(self):
        self.old_silent()
        for state in ("pending", "offered", "queued", "listening", "receiving"):
            with self.subTest(state=state):
                config.fetch_queue.clear()
                self.drop.reset_mock()
                self.old_silent()
                self.row("Song.flac", state=state)
                webserver.build_fetch_clear_result({"which": "failed"})
                self.drop.assert_not_called()

    def test_names_are_compared_as_the_other_bot_compares_them(self):
        """Spaces sent as underscores, and any case, are the same file."""
        self.old_silent("Some Song.flac")
        self.row("some_song.FLAC", state="queued")
        webserver.build_fetch_clear_result({"which": "failed"})
        self.drop.assert_not_called()

    def test_the_same_file_from_another_bot_does_not_stop_it(self):
        self.old_silent()
        self.row("Song.flac", bot="OtherBot", state="queued")
        webserver.build_fetch_clear_result({"which": "failed"})
        self.drop.assert_called_once_with("PeerBot", "Song.flac")

    def test_a_finished_or_failed_twin_does_not_stop_it(self):
        self.old_silent()
        self.row("Song.flac", state="complete")
        webserver.build_fetch_clear_result({"which": "failed"})
        self.drop.assert_called_once_with("PeerBot", "Song.flac")


class Delete(Case):
    def test_deleting_the_old_row_spares_the_newer_request(self):
        old = self.old_silent()
        self.row("Song.flac", state="queued")
        status, result = webserver.build_fetch_delete_result(old)
        self.assertEqual(status, 200)
        self.drop.assert_not_called()
        self.assertNotIn("removed_at_bot", result)

    def test_alone_it_is_still_let_go(self):
        old = self.old_silent()
        webserver.build_fetch_delete_result(old)
        self.drop.assert_called_once_with("PeerBot", "Song.flac")
