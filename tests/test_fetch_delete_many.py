"""webserver.build_fetch_delete_many_result() (#1246 review).

`dlcancel all` / `dlcancel <id> <id> ...` used to call build_fetch_delete_
result() once per id: a fresh acquire of the fetch lock, a fresh
another_row_wants_locked() scan of the whole queue, and a fresh
persist_fetch_history() rewrite of the whole history file, every time.
build_fetch_delete_many_result() does the same job in one pass: one hold of
the lock for the whole batch, one scan per still-held row but all under
that same hold, and one persist at the end - the same batching
build_fetch_clear_result() already used for the Downloads page's Clear
buttons, generalised to an explicit id list.
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
        persist = mock.patch.object(dcc_fetch, "persist_fetch_history")
        self.persist = persist.start()
        self.addCleanup(persist.stop)

    def row(self, name, bot="PeerBot", **fields):
        rid = dcc_fetch.enqueue_fetch(bot, name)
        config.fetch_queue[rid].update(fields)
        return rid

    def old_silent(self, name="Song.flac", bot="PeerBot"):
        return self.row(name, bot=bot, state="failed", reason="no response", offered_at=time.time())


class Batching(Case):
    def test_several_ids_all_go_in_one_persist(self):
        a = self.row("One.flac", state="pending")
        b = self.row("Two.flac", state="queued")
        status, result = webserver.build_fetch_delete_many_result([a, b])
        self.assertEqual(status, 200)
        self.assertEqual(sorted(result["cancelled"]), sorted([a, b]))
        self.assertEqual(result["refused"], [])
        self.assertNotIn(a, config.fetch_queue)
        self.assertNotIn(b, config.fetch_queue)
        self.persist.assert_called_once()

    def test_nothing_cancelled_never_persists(self):
        status, result = webserver.build_fetch_delete_many_result(["000000000000"])
        self.assertEqual(status, 200)
        self.assertEqual(result["cancelled"], [])
        self.persist.assert_not_called()

    def test_a_repeated_id_is_only_counted_once(self):
        a = self.row("One.flac", state="pending")
        status, result = webserver.build_fetch_delete_many_result([a, a])
        self.assertEqual(result["cancelled"], [a])


class PartialSuccess(Case):
    def test_an_unknown_id_is_refused_the_rest_still_goes(self):
        a = self.row("One.flac", state="pending")
        status, result = webserver.build_fetch_delete_many_result([a, "000000000000"])
        self.assertEqual(status, 200)
        self.assertEqual(result["cancelled"], [a])
        self.assertEqual(len(result["refused"]), 1)
        self.assertEqual(result["refused"][0]["id"], "000000000000")
        self.assertIn("Unknown", result["refused"][0]["error"])

    def test_a_started_download_is_refused_the_rest_still_goes(self):
        a = self.row("One.flac", state="pending")
        b = self.row("Two.flac", state="receiving")
        status, result = webserver.build_fetch_delete_many_result(
            [a, b], only_states=("pending", "offered", "queued"))
        self.assertEqual(result["cancelled"], [a])
        self.assertEqual(result["refused"], [{"id": b, "error": "That download is no longer waiting."}])
        self.assertIn(b, config.fetch_queue)

    def test_only_states_refuses_anything_outside_it(self):
        a = self.row("One.flac", state="complete")
        status, result = webserver.build_fetch_delete_many_result(
            [a], only_states=("pending", "offered", "queued"))
        self.assertEqual(result["cancelled"], [])
        self.assertIn(a, config.fetch_queue)


class SparesANewerRequest(Case):
    """Same bug as #1083, now for a whole batch: letting an old, silently-
    failed row go must not take the other bot's queue entry for a file a
    SURVIVING row (of this batch or outside it) still wants."""

    def test_a_newer_row_outside_the_batch_keeps_its_place(self):
        old = self.old_silent()
        self.row("Song.flac", state="queued")
        webserver.build_fetch_delete_many_result([old])
        self.drop.assert_not_called()

    def test_a_newer_row_in_the_same_batch_is_dropped_only_once(self):
        """Both rows point at the same bot and file and both go in this
        batch - nothing is left wanting it afterwards, so it is dropped at
        the bot, but only ONCE: two local rows for the same file are still
        one queue entry over there, and "@bot-remove" clears it regardless
        of how many of our own rows asked for removal (#1246 review)."""
        old = self.old_silent()
        newer = self.row("Song.flac", state="queued")
        status, result = webserver.build_fetch_delete_many_result([old, newer])
        self.assertEqual(sorted(result["cancelled"]), sorted([old, newer]))
        self.drop.assert_called_once_with("PeerBot", "Song.flac", channel=None)

    def test_alone_it_is_still_let_go_at_the_bot(self):
        old = self.old_silent()
        webserver.build_fetch_delete_many_result([old])
        self.drop.assert_called_once_with("PeerBot", "Song.flac", channel=None)
