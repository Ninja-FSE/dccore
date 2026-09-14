"""Removing a bot's downloaded list, and the three stores that have to agree.

Nothing has ever removed an entry from `config.fetched_bot_lists` - no TTL, no
cap, no button. A list fetched once stayed in the List Browser forever,
including for a bot that will never reconnect (#385). The advert registry has
had exactly this housekeeping for ages (`_prune_known_bots()` in irc.py); the
store the browser actually reads never got it.

This is the manual half, and deliberately first: it is exact, it needs no new
state, and it removes a row **today** rather than in N days. It also covers
what a timer structurally cannot - a bot that renamed, a channel you have
left, a list fetched by mistake, or one bot recorded twice because it
reconnected on its alt nick.

Three stores go together or the operation is a lie:

  * the entry, or the sidebar offers a list whose files are gone
  * the extracted directory, or the disk never comes back - which is the whole
    point for anyone whose fetched directory has outgrown what they meant to
    keep
  * the search index rows, which are roughly as large again as the lists they
    describe. Not a correctness problem (search already restricts its answer
    to lists currently held) but a purge that skipped them would quietly keep
    the largest part of what it claimed to remove.
"""

import os
import shutil
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class PurgeCase(DCCoreTestCase):
    """A bot whose list we hold, with files on disk and rows in the index."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FETCHED_FILES_DIR=os.path.join(self.tree.root, "fetched"),
                        LIST_INDEX_FILE=os.path.join(self.tree.root, "idx.db"))
        list_index.reset_for_tests()
        self.addCleanup(list_index.close)

    def hold(self, nick, markers=()):
        """Record a fetched list for `nick`, on disk and in the index."""
        extract = list_fetch.list_extract_dir(nick)
        os.makedirs(extract, exist_ok=True)
        path = os.path.join(extract, nick + "-list.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Some Folder\n  a-track.mp3 ::INFO:: 4.1MB\n")

        config.fetched_bot_lists[nick.lower()] = {
            "bot": nick,
            "fetched_at": time.time(),
            "list_path": path,
            "entry_count": 1,
            "source_zip": nick + ".zip",
            "lists": {marker: {"entry_count": 1} for marker in markers},
        }
        rows = [{"folder": "Some Folder", "filename": "a-track.mp3",
                 "size": "4.1MB"}]
        list_index.index_bot_list(list_fetch.index_key(nick, ""), rows)
        for marker in markers:
            list_index.index_bot_list(list_fetch.index_key(nick, marker), rows)
        return extract


class ItRemovesAllThree(PurgeCase):

    def test_the_entry_goes(self):
        self.hold("SomeBot")

        ok, _detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertTrue(ok)
        self.assertNotIn("somebot", config.fetched_bot_lists)

    def test_the_files_go(self):
        extract = self.hold("SomeBot")

        list_fetch.purge_fetched_list("SomeBot")

        self.assertFalse(os.path.exists(extract))

    def test_the_index_rows_go(self):
        """The disk half of the operation. Search would have been correct
        without this - it already restricts its answer to lists held - so
        nothing would have looked wrong while the space stayed used."""
        self.hold("SomeBot")

        list_fetch.purge_fetched_list("SomeBot")

        self.assertEqual(list(list_index.indexed_bots()), [])

    def test_forget_bot_drops_every_marker_too(self):
        """The leak #388 shipped with, asserted against the primitive itself
        rather than only through the caller.

        Its own tests could not see it: they seed one bare-nick index entry,
        so `assertNotIn("otherbot", indexed_bots())` passes while
        "otherbot/films" survives - a different string. The fixture has to
        have a marker list in it for the question to be asked at all.
        """
        self.hold("SomeBot", markers=("films", "series"))
        self.assertEqual(sorted(list_index.indexed_bots()),
                         ["somebot", "somebot/films", "somebot/series"])

        list_fetch.forget_bot("SomeBot")

        self.assertEqual(list(list_index.indexed_bots()), [],
                         "a further list's index rows outlived the purge, "
                         "pointing at files it had just deleted")

    def test_an_entry_written_before_archives_held_more_than_one_list(self):
        """No "lists" key at all - the shape on disk for anyone who fetched
        before that field existed. The bare nick still has to go, which is
        what the `| {""}` in forget_bot() is for."""
        self.hold("SomeBot")
        del config.fetched_bot_lists["somebot"]["lists"]

        self.assertTrue(list_fetch.forget_bot("SomeBot"))
        self.assertEqual(list(list_index.indexed_bots()), [])

    def test_every_list_in_the_archive_goes_not_just_the_main_one(self):
        """One entry carries every list the archive held, and they all came
        out of one zip into one directory. Dropping only the bare nick would
        leave the extra lists' rows indexed against files that are gone."""
        self.hold("SomeBot", markers=("films", "series"))

        list_fetch.purge_fetched_list("SomeBot")

        self.assertEqual(list(list_index.indexed_bots()), [])

    def test_it_survives_a_restart(self):
        """Persisted inside the lock, for the reason the fetch path gives: a
        crash before the write brings the entry back on the next boot,
        pointing at files this call has just deleted."""
        import db

        self.hold("SomeBot")

        list_fetch.purge_fetched_list("SomeBot")

        self.assertEqual(db.load_fetched_bot_lists(), {})

    def test_another_bots_list_is_untouched(self):
        other = self.hold("OtherBot")
        self.hold("SomeBot")

        list_fetch.purge_fetched_list("SomeBot")

        self.assertIn("otherbot", config.fetched_bot_lists)
        self.assertTrue(os.path.exists(other))
        self.assertEqual(list(list_index.indexed_bots()), ["otherbot"])


class WhichListWasMeant(PurgeCase):

    def test_a_marker_row_purges_its_bot(self):
        """The row an operator clicks is a LIST; the thing that can be deleted
        is the bot. "<nick>/<marker>" resolves to the nick rather than being
        refused - there is no per-list directory to remove even if the store
        were shaped for it."""
        self.hold("SomeBot", markers=("films",))

        ok, _detail = list_fetch.purge_fetched_list("SomeBot/films")

        self.assertTrue(ok)
        self.assertNotIn("somebot", config.fetched_bot_lists)

    def test_the_nick_is_matched_regardless_of_case(self):
        self.hold("SomeBot")

        ok, _detail = list_fetch.purge_fetched_list("somebot")

        self.assertTrue(ok)
        self.assertNotIn("somebot", config.fetched_bot_lists)

    def test_our_own_list_is_refused(self):
        """It is not fetched from anywhere and its files are the library.
        Reaching this at all would be a UI bug, so it answers rather than
        assuming it cannot happen."""
        ok, detail = list_fetch.purge_fetched_list("__own__")

        self.assertFalse(ok)
        self.assertIn("your own", detail)

    def test_one_of_our_other_lists_is_refused_too(self):
        """Each further served list is "__own__:<name>", and a check against
        the bare string alone would let those through."""
        ok, detail = list_fetch.purge_fetched_list("__own__:films")

        self.assertFalse(ok)
        self.assertIn("your own", detail)

    def test_a_bot_we_hold_nothing_from_is_refused(self):
        ok, detail = list_fetch.purge_fetched_list("NoSuchBot")

        self.assertFalse(ok)
        self.assertIn("Nothing is held", detail)

    def test_an_empty_name_is_refused(self):
        ok, _detail = list_fetch.purge_fetched_list("")

        self.assertFalse(ok)

    def test_nothing_is_deleted_when_it_refuses(self):
        """The control. Every refusal above is only worth having if it stops
        before touching anything."""
        extract = self.hold("SomeBot")

        list_fetch.purge_fetched_list("NoSuchBot")

        self.assertIn("somebot", config.fetched_bot_lists)
        self.assertTrue(os.path.exists(extract))
        self.assertEqual(list(list_index.indexed_bots()), ["somebot"])


class NotWhileAFetchIsRunning(PurgeCase):

    def queue_a_fetch(self, nick, state):
        config.fetch_queue["req-1"] = {"bot": nick, "state": state,
                                       "filename": nick + ".zip"}

    def test_a_running_fetch_blocks_it(self):
        """The thread will write into the very directory being removed, and
        there is no cancellation path for one already running - the same
        reason a fetch in flight cannot be deleted."""
        self.hold("SomeBot")
        self.queue_a_fetch("SomeBot", "receiving")

        ok, detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertFalse(ok)
        self.assertIn("in progress", detail)
        self.assertIn("somebot", config.fetched_bot_lists)

    def test_every_in_flight_state_blocks_it(self):
        for state in ("pending", "offered", "listening", "receiving"):
            with self.subTest(state=state):
                config.fetch_queue.clear()
                config.fetched_bot_lists.clear()
                self.hold("SomeBot")
                self.queue_a_fetch("SomeBot", state)

                ok, _detail = list_fetch.purge_fetched_list("SomeBot")

                self.assertFalse(ok)

    def test_a_pending_fetch_blocks_it_even_though_it_can_be_deleted(self):
        """Deliberately unlike build_fetch_delete_result(), which allows a
        pending row to be removed. Different questions: deleting a pending row
        removes the thing that would have started, while purging leaves it
        queued and pointed at a directory that has just gone - it would
        recreate what was purged, which reads as the purge having failed."""
        self.hold("SomeBot")
        self.queue_a_fetch("SomeBot", "pending")

        ok, _detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertFalse(ok)

    def test_a_finished_fetch_does_not_block_it(self):
        """Otherwise a bot whose list was fetched successfully - the only bots
        that can be purged at all - could never be purged, since the row that
        fetched it stays in the history."""
        self.hold("SomeBot")
        self.queue_a_fetch("SomeBot", "complete")

        ok, _detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertTrue(ok)

    def test_a_fetch_for_a_different_bot_does_not_block_it(self):
        self.hold("SomeBot")
        self.queue_a_fetch("OtherBot", "receiving")

        ok, _detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertTrue(ok)


class WhenTheFilesWillNotGo(PurgeCase):

    def test_the_entry_is_still_removed(self):
        """The row is what the operator asked to be rid of, and leaving it
        would leave them with a row they cannot remove at all. The files are
        named in the log instead."""
        self.hold("SomeBot")

        # A stand-in for list_fetch's OWN reference, not a patch on the shutil
        # module: shutil is shared, tearDown runs before addCleanup, and the
        # harness cleans its temp trees with the same function - so patching
        # the module made this test tear the whole fixture down with it.
        #
        # A NO-OP, not a raise. forget_bot() passes ignore_errors=True, so a
        # directory that will not go does not raise - it simply stays there,
        # which is exactly what this has to simulate. A stub that raised would
        # be testing a path the real code cannot take.
        class OnlyRmtree:
            @staticmethod
            def rmtree(*_args, **_kwargs):
                return None

        real_shutil = list_fetch.shutil
        list_fetch.shutil = OnlyRmtree
        self.addCleanup(setattr, list_fetch, "shutil", real_shutil)

        ok, detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertTrue(ok)
        self.assertNotIn("somebot", config.fetched_bot_lists)
        self.assertIn("could not be deleted", detail)

    def test_a_directory_that_was_already_gone_is_not_an_error(self):
        """Cleared by hand, or by an earlier half-completed purge. There is
        nothing left to do and nothing to complain about."""
        extract = self.hold("SomeBot")
        shutil.rmtree(extract)

        ok, detail = list_fetch.purge_fetched_list("SomeBot")

        self.assertTrue(ok)
        self.assertNotIn("could not be deleted", detail)


class WhatTheRouteAnswers(PurgeCase):
    """The builder's HTTP shape. The wiring - that the route reaches this at
    all - is in tests/test_dashboard_routes.py, where every route's is."""

    def test_a_purge_answers_200(self):
        self.hold("SomeBot")

        status, result = webserver.build_fetched_list_purge_result("SomeBot")

        self.assertEqual(status, 200)
        self.assertTrue(result["purged"])

    def test_a_bot_we_hold_nothing_from_is_404(self):
        status, result = webserver.build_fetched_list_purge_result("NoSuchBot")

        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def test_a_fetch_in_flight_is_409_not_400(self):
        """The request is well formed and would be valid in a moment, which is
        what 409 means - and what tells the page to say "try again" rather
        than "that was wrong"."""
        self.hold("SomeBot")
        config.fetch_queue["req-1"] = {"bot": "SomeBot", "state": "receiving"}

        status, _result = webserver.build_fetched_list_purge_result("SomeBot")

        self.assertEqual(status, 409)

    def test_our_own_list_is_400(self):
        status, _result = webserver.build_fetched_list_purge_result("__own__")

        self.assertEqual(status, 400)


class ThePageOffersItWhereItWorks(unittest.TestCase):
    """Structural, like every other check on the dashboard - nothing here
    executes JavaScript."""

    @staticmethod
    def read(name):
        with open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
            return f.read()

    def test_the_button_exists_and_starts_hidden(self):
        """Hidden until something is open that can be purged. Shown by
        default it would sit there offering to delete your own library."""
        html = self.read("index.html")
        button = html.split('id="filelists-purge-btn"', 1)[1].split(">", 1)[0]

        self.assertIn("hidden", button)

    def test_it_is_hidden_for_our_own_lists_and_for_bots_we_hold_nothing_from(self):
        js = self.read("app.js")
        body = js.split("function renderFilelistsPurge(", 1)[1].split(
            "\n  function ", 1)[0]

        self.assertIn("isOwnSource(source)", body)
        self.assertIn("!row.held", body)

    def test_it_asks_before_deleting(self):
        """It deletes files and cannot be undone from here: getting the list
        back means downloading it again, which needs the bot to still exist."""
        js = self.read("app.js")
        body = js.split("function purgeCurrentList(", 1)[1].split(
            "\n  function ", 1)[0]

        self.assertIn("window.confirm(", body)

    def test_it_reopens_our_own_list_afterwards(self):
        """The list that was open no longer exists. Leaving it selected would
        leave the table showing rows from something just deleted."""
        js = self.read("app.js")
        body = js.split("function purgeCurrentList(", 1)[1].split(
            "\n  function ", 1)[0]

        self.assertIn('state.filelistsSource = "__own__"', body)


if __name__ == "__main__":
    unittest.main()
