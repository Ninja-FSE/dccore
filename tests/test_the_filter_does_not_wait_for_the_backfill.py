"""#1416: the List Browser's filter does not wait out the startup backfill.

Since #1296 the bot connects while the search index is filled on a thread of
its own, and the index's readers waited for it with no timeout - about ten
minutes on a large index, each keystroke of the filter holding a request
thread for all of them. Now:

  * a reader waits list_index.FILL_WAIT_SECONDS at most, then answers as
    if there were no index - never from the half-filled one;
  * the filter's payload says `filling`, and calls no held list empty (the
    sidebar would dim it as "no match" when the index has not reached it);
  * once the backfill is done, the filter answers from the index as before.
"""

import os
import sys
import threading
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_index  # noqa: E402
import runtime  # noqa: E402
import web_lists  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_crosslist_search import IndexCase  # noqa: E402


class FillingCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        done = runtime.list_index_backfill_done
        # Set again whatever happens: a reader the old code left waiting on
        # it is released, not left behind for the rest of the run.
        self.addCleanup(done.set)
        done.clear()
        self.addCleanup(setattr, list_index, "FILL_WAIT_SECONDS", list_index.FILL_WAIT_SECONDS)
        list_index.FILL_WAIT_SECONDS = 0.05

    def answered(self, call):
        """call() on a thread of its own: its result, or a failure naming a
        reader that is still waiting after ten seconds."""
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("value", call()), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "the reader is still waiting for the backfill")
        return result.get("value")


class AReaderDoesNotWaitItOut(IndexCase, FillingCase):
    """A real index holding a match, put in before the backfill is said to
    be running: what it holds must not be answered from while it fills."""

    def setUp(self):
        IndexCase.setUp(self)
        runtime.list_index_backfill_done.set()
        self.index("SomeBot", "Some Song.flac")
        self.assertEqual(len(list_index.search(["song"], bots=["SomeBot"])), 1, "control")
        self.addCleanup(runtime.list_index_backfill_done.set)
        runtime.list_index_backfill_done.clear()
        self.addCleanup(setattr, list_index, "FILL_WAIT_SECONDS", list_index.FILL_WAIT_SECONDS)
        list_index.FILL_WAIT_SECONDS = 0.05

    def test_it_says_the_index_is_filling(self):
        self.assertTrue(list_index.index_filling())
        runtime.list_index_backfill_done.set()
        self.assertFalse(list_index.index_filling())

    def test_which_bots_match_answers_as_if_there_were_no_index(self):
        """No claim either way, rather than every list it has not reached
        reported as having no match."""
        self.assertEqual(self.answered(lambda: list_index.bots_with_a_match(["song"], ["SomeBot"])),
                         (set(), set()))

    def test_the_search_answers_with_nothing(self):
        self.assertEqual(self.answered(lambda: list_index.search(["song"], bots=["SomeBot"])), [])


class TheFilterSaysSo(FillingCase):

    def setUp(self):
        super().setUp()
        self.set_config(fetched_bot_lists={"somebot": {"bot": "SomeBot"}, "otherbot": {"bot": "OtherBot"}})

    def test_filling_and_no_held_list_called_empty(self):
        with mock.patch.object(list_index, "search", side_effect=AssertionError("asked the index")):
            payload = self.answered(lambda: web_lists.build_crosslist_search_payload("song"))

        self.assertTrue(payload["filling"])
        self.assertEqual(payload["matched"], [])
        self.assertEqual(payload["empty"], [])
        self.assertEqual(payload["folders"], [])

    def test_once_it_is_filled_the_index_is_asked(self):
        runtime.list_index_backfill_done.set()
        asked = []
        with mock.patch.object(list_index, "search", side_effect=lambda *a, **k: asked.append(a) or []), \
                mock.patch.object(list_index, "bots_with_a_match", return_value=(set(), {"somebot"})):
            payload = web_lists.build_crosslist_search_payload("song")

        self.assertEqual(len(asked), 1)
        self.assertNotIn("filling", payload)

    def test_no_term_is_not_a_question_for_the_index_at_all(self):
        payload = web_lists.build_crosslist_search_payload("")
        self.assertNotIn("filling", payload)


if __name__ == "__main__":
    unittest.main()
