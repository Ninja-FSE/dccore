"""A row we asked for reads "Requested", not "Offered".

From a maintainer, on two list rows sitting at OFFERED in the Downloads
table: "should be REQUESTED, not offered".

Exactly right, and the word was backwards on the SCREEN rather than in the
queue. dcc_fetch.check_fetch_queue() flips a row to `offered` at the moment
it dispatches OUR OWN request line - `@bot` for a list, `!bot <file>`
otherwise - and stamps `offered_at` with the time we sent it. Its own log
line for that moment already says "Requested". So the state means "we have
asked and are waiting for their DCC SEND"; nothing has been offered to us.
The name reads from inside dcc_fetch.py, where the row IS the offer being
waited on, and that reading does not survive being printed on a pill.

WHY ONLY THE LABEL MOVES

The internal name stays. It is written into the fetch queue file, so
renaming it would strand every row in flight across a restart, and it is
matched by name in a dozen places in dcc_fetch.py. The CSS class is built
from the state name too, so .status-offered goes on styling the pill.

The first two cases here are the control: they establish what the state
actually means, so the wording guards below are anchored to the behaviour
rather than to a string somebody once typed.
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


class TheStateMeansWeAsked(DCCoreTestCase):
    """The control. If a row reached `offered` because somebody offered us
    something, the label would be right and these would fail."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL="#somechannel", MAX_FETCH_SLOTS=3)

    def test_the_row_turns_offered_in_the_same_pass_that_sends_our_request(self):
        request_id = dcc_fetch.enqueue_fetch("someotherbot", "A Track.flac")
        self.assertEqual(config.fetch_queue[request_id]["state"], "pending")
        self.assertEqual(self.oserve.queued, [])

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "offered")
        sent = "".join(message for _, message, _ in self.oserve.queued)
        self.assertIn("!someotherbot A Track.flac", sent,
                      "the state changed without our request going out - if "
                      "that is ever true, 'Requested' is the wrong word")

    def test_a_list_row_is_the_same_story_with_the_bare_trigger(self):
        """The two rows in the report were both list fetches."""
        request_id = dcc_fetch.enqueue_fetch("someotherbot", "",
                                             request_type="list")

        dcc_fetch.check_fetch_queue()

        self.assertEqual(config.fetch_queue[request_id]["state"], "offered")
        sent = "".join(message for _, message, _ in self.oserve.queued)
        self.assertIn("@someotherbot", sent)

    def test_the_timestamp_is_when_we_sent_it(self):
        """`offered_at` is stamped by the same code that dispatches, which is
        why the timeout it feeds is a wait for THEIR reply."""
        request_id = dcc_fetch.enqueue_fetch("someotherbot", "A Track.flac")

        dcc_fetch.check_fetch_queue()

        self.assertIsNotNone(config.fetch_queue[request_id]["offered_at"])


class ThePillSaysRequested(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def label_map(self):
        body = self.source().split("DOWNLOAD_STATE_LABELS = {", 1)[1]
        return body.split("}", 1)[0]

    def test_the_offered_state_is_labelled_requested(self):
        self.assertIn('offered: "Requested"', self.label_map())

    def test_no_row_is_labelled_offered_anywhere_in_the_map(self):
        """The whole point. A second state picking the word back up would be
        the same mistake in a new place."""
        self.assertNotIn("Offered", self.label_map())

    def test_the_internal_state_name_is_untouched(self):
        """It is persisted in the fetch queue file and matched by name
        throughout dcc_fetch.py. Only the word on the pill changed."""
        self.assertIn("offered:", self.label_map())

    def test_the_pill_still_has_a_colour(self):
        """The CSS class is built from the STATE, not the label, so the
        rename must not have orphaned the styling."""
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                     encoding="utf-8") as handle:
            css = handle.read()

        self.assertIn(".status-pill.status-offered", css)


class TheSettingsPageTellsTheSameStory(unittest.TestCase):
    """These two settings time the exact same state. Fixing the pill and
    leaving "fetch offer timeout" on the Settings page would leave an
    operator reading two different accounts of one thing."""

    def test_the_wait_is_described_as_a_wait_for_a_reply(self):
        label = webserver.SETTINGS_LABELS["FETCH_OFFER_TIMEOUT"]

        self.assertIn("reply", label.lower())
        self.assertIn("request", label.lower())

    def test_neither_fetch_wait_is_called_an_offer(self):
        for name in ("FETCH_OFFER_TIMEOUT", "FETCH_FOLDER_OFFER_TIMEOUT"):
            self.assertNotIn("offer", webserver.SETTINGS_LABELS[name].lower(),
                             f"{name} still reads as a timeout on an offer "
                             f"somebody made us")

    def test_both_settings_still_have_a_label_at_all(self):
        """Guard on the guard: assertNotIn passes just as happily against a
        missing key's empty string."""
        for name in ("FETCH_OFFER_TIMEOUT", "FETCH_FOLDER_OFFER_TIMEOUT"):
            self.assertGreater(len(webserver.SETTINGS_LABELS.get(name, "")), 10)


if __name__ == "__main__":
    unittest.main()
