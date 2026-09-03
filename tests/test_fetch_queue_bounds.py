"""Audit #162 finding 27: the fetch queue had no bound and no way back out.

POST /api/fetch/enqueue took a list because multi-select is the point, but it
took an unbounded one - 5001 rows created in a single request during the audit.
Three things then combined to make that unrecoverable rather than merely untidy:

  * nothing capped the request body, so one paste could create thousands of rows;
  * nothing capped config.fetch_queue itself, so N requests could do it again;
  * `pending` rows could not be deleted. The delete route refused every state
    except complete/failed, and !rehash preserves fetch_queue - so the only way
    to empty a mistaken bulk enqueue was to restart the daemon.

MSG_DELAY is 5.0 seconds, so the backlog drains as hours of outbound IRC that
the operator cannot stop from the dashboard that created it. Every requester is
an authenticated operator, so this is a footgun rather than an attack - but an
operator holding the only key is exactly who a footgun fires at.

The `pending` half is the interesting one. Its refusal was not arbitrary: the
route's docstring justified it with "there is no cancellation path for a
transfer thread already running", which is true of offered/listening/receiving
and simply is not true of pending - a pending row has no thread, no socket, no
offer on the wire and no file on disk. It was swept in with states it does not
resemble.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import dcc_fetch  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def items(count, bot="goodbot"):
    return [{"bot": bot, "filename": f"Song{n}.flac"} for n in range(count)]


class OneRequestCannotCreateUnboundedRows(DCCoreTestCase):

    def test_a_batch_at_the_cap_is_accepted(self):
        """The control. A cap that refused ordinary multi-select would be a
        different bug, not a fix."""
        status, result = webserver.build_fetch_enqueue_result(
            items(webserver.FETCH_ENQUEUE_MAX_ITEMS))

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), webserver.FETCH_ENQUEUE_MAX_ITEMS)

    def test_one_item_over_the_cap_is_refused_whole(self):
        status, result = webserver.build_fetch_enqueue_result(
            items(webserver.FETCH_ENQUEUE_MAX_ITEMS + 1))

        self.assertEqual(status, 413)
        self.assertIn("error", result)
        self.assertEqual(config.fetch_queue, {},
                         "an over-long body created rows before being refused")

    def test_the_refusal_says_both_numbers(self):
        """An operator who pasted a big list needs to know what the limit is
        and how far over they went, or the fix is guesswork."""
        _status, result = webserver.build_fetch_enqueue_result(items(700))

        self.assertIn(str(webserver.FETCH_ENQUEUE_MAX_ITEMS), result["error"])
        self.assertIn("700", result["error"])

    def test_the_audit_payload_no_longer_lands(self):
        """5001 items, the number the audit actually created."""
        status, _result = webserver.build_fetch_enqueue_result(items(5001))

        self.assertEqual(status, 413)
        self.assertEqual(len(config.fetch_queue), 0)

    def test_the_cap_is_checked_before_per_item_validation(self):
        """Refusing the body without paying for it is the point: an over-long
        body of items that are ALSO individually invalid must still come back
        as the single shape error, not 5001 entries in "errors"."""
        status, result = webserver.build_fetch_enqueue_result(
            [{"bot": "", "filename": ""} for _ in range(5001)])

        self.assertEqual(status, 413)
        self.assertNotIn("errors", result)


class TheQueueItselfIsBounded(DCCoreTestCase):
    """Capping the request alone would only mean asking twice."""

    def setUp(self):
        super().setUp()
        self.real_cap = dcc_fetch.MAX_UNRESOLVED_FETCHES
        dcc_fetch.MAX_UNRESOLVED_FETCHES = 5
        self.addCleanup(setattr, dcc_fetch, "MAX_UNRESOLVED_FETCHES", self.real_cap)

    def test_repeated_requests_cannot_walk_past_the_queue_cap(self):
        for _ in range(4):
            webserver.build_fetch_enqueue_result(items(2))

        self.assertEqual(dcc_fetch.count_unresolved_fetches(), 5)

    def test_the_rows_that_did_not_fit_are_reported_not_silently_dropped(self):
        status, result = webserver.build_fetch_enqueue_result(items(8))

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 5)
        self.assertEqual(len(result["errors"]), 3)
        self.assertIn("full", result["errors"][0]["error"])

    def test_no_none_ever_reaches_created(self):
        """enqueue_fetch() returns None on refusal and this route used to
        append that return value unchecked. A null in "created" reads to the
        dashboard as a queued row it can then never find."""
        _status, result = webserver.build_fetch_enqueue_result(items(8))

        self.assertNotIn(None, result["created"])

    def test_a_full_queue_refuses_rather_than_accepting_nothing_quietly(self):
        webserver.build_fetch_enqueue_result(items(5))

        status, result = webserver.build_fetch_enqueue_result(items(1))

        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_finished_rows_do_not_count_against_the_cap(self):
        """The cap is on outstanding work, not on history. Counting completed
        rows would make the Downloads table fill the queue up over time and
        block new fetches for no reason."""
        for n in range(20):
            config.fetch_queue[f"done{n}"] = dict(
                dcc_fetch.new_fetch_row("goodbot", f"Old{n}.flac"), state="complete")

        status, result = webserver.build_fetch_enqueue_result(items(3))

        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 3)

    def test_the_counter_counts_waiting_as_well_as_moving(self):
        """count_active_fetches() deliberately excludes `pending` - it answers
        "how many slots are busy". If the cap used that number instead, the
        pending backlog it exists to bound would be exactly what it ignored."""
        for state in ("pending", "offered", "listening", "receiving", "complete", "failed"):
            config.fetch_queue[state] = dict(
                dcc_fetch.new_fetch_row("goodbot", "S.flac"), state=state)

        self.assertEqual(dcc_fetch.count_unresolved_fetches(), 4)
        self.assertEqual(dcc_fetch.count_active_fetches(), 3)


class APendingRowCanBeDeleted(DCCoreTestCase):
    """The half that made the others unrecoverable rather than merely untidy."""

    def _put(self, request_id, state):
        config.fetch_queue[request_id] = dict(
            dcc_fetch.new_fetch_row("goodbot", "Song.flac"), state=state)

    def test_a_pending_row_is_removed(self):
        self._put("rid", "pending")

        status, _result = webserver.build_fetch_delete_result("rid")

        self.assertEqual(status, 200)
        self.assertNotIn("rid", config.fetch_queue)

    def test_the_three_genuinely_in_flight_states_are_still_refused(self):
        """The control, and the reason this is not simply "allow everything":
        those three have a thread or a socket that deleting the row would
        orphan. Only pending has neither."""
        for state in ("offered", "listening", "receiving"):
            with self.subTest(state=state):
                self._put(f"r-{state}", state)

                status, _result = webserver.build_fetch_delete_result(f"r-{state}")

                self.assertEqual(status, 409)
                self.assertIn(f"r-{state}", config.fetch_queue)

    def test_a_bulk_enqueue_can_be_undone_without_restarting(self):
        """The whole finding, end to end: queue a batch, then empty it through
        the same API that created it."""
        _status, result = webserver.build_fetch_enqueue_result(items(50))
        self.assertEqual(len(result["created"]), 50)

        for request_id in result["created"]:
            self.assertEqual(webserver.build_fetch_delete_result(request_id)[0], 200)

        self.assertEqual(config.fetch_queue, {})


class TheDashboardOffersTheButton(unittest.TestCase):
    """The server-side half is only half the fix: if renderDownloads() does not
    draw a control for a pending row, the queue stays clearable by API and not
    by the dashboard that filled it - which is the actual complaint.

    web/app.js has no test runner here, so this reads the source the way
    tests/test_web_layout.py and tests/test_web_settings_and_session.py already
    do.
    """

    def render_downloads(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            body = handle.read()
        start = body.index("function renderDownloads(")
        return body[start:body.index("\n  function ", start + 10)]

    def test_a_pending_row_is_offered_a_button(self):
        window = self.render_downloads()
        deletable = window[window.index("var deletable ="):][:200]

        self.assertIn('state === "pending"', deletable)

    def test_the_three_in_flight_states_are_not(self):
        """The control. Offering it for `receiving` would orphan a running
        transfer thread - the reason the original refusal existed at all."""
        window = self.render_downloads()
        deletable = window[window.index("var deletable ="):][:200]

        for state in ("offered", "listening", "receiving"):
            with self.subTest(state=state):
                self.assertNotIn(f'"{state}"', deletable)

    def test_a_pending_row_reaches_the_action_cell(self):
        """The button existing is not enough - the if/else chain that builds
        `action` had no pending branch, so it would have been built and then
        dropped on the floor."""
        window = self.render_downloads()
        chain = window[window.index("var action;"):][:600]

        self.assertIn('state === "pending"', chain)
        self.assertIn("action = deleteBtn", chain)

    def test_the_label_says_cancel_not_delete(self):
        """Nothing has been downloaded for a pending row, so "Delete" would
        describe throwing away a file that does not exist."""
        window = self.render_downloads()

        self.assertIn('"Cancel" : "Delete"', window)


class TheConfirmPromptMatchesWhatIsBeingRemoved(unittest.TestCase):

    def click_handler(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            body = handle.read()
        start = body.index("el.downloadsBody.addEventListener")
        return body[start:start + 1400]

    def test_a_queued_row_does_not_warn_about_an_irreversible_delete(self):
        """A cancelled request can simply be queued again, so the finished-row
        warning is both wrong and needlessly alarming."""
        window = self.click_handler()

        self.assertIn("btn.dataset.pending", window)
        self.assertIn("Nothing has been downloaded yet", window)

    def test_the_finished_row_warning_is_still_there(self):
        """Deleting a completed fetch DOES remove a file from disk."""
        self.assertIn("This cannot be undone", self.click_handler())


# The two HTTP-layer assertions for this finding (413 really reaches the wire,
# and a pending row deletes with a 200) live in tests/test_webserver.py, beside
# the Flask fixture that logs a test client in - see FetchEnqueueRouteTests and
# FetchDeleteRouteTests there. Duplicating that fixture here would mean two
# copies of the login helper to keep in step.


if __name__ == "__main__":
    unittest.main()
