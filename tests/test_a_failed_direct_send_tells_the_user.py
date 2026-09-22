"""#599: a failed direct send was silently dropped while the log said "kept for retry".

The first request from a user with no queue and a free slot is sent without
ever entering dcc_queue: the dispatcher builds a synthetic row for it. When that
send failed - a DCC accept dialog nobody clicked within the 30 s listener
timeout, a firewall blocking the connect - release_queue_entry() classified the
row as retryable, marked it retained, and therefore sent NO notice: the user
waited for a file that would never be re-sent, and the operator's log said a
retry was pending. Queued rows get three attempts and a final NOTICE.

A row that is in no queue cannot be retried, so it is settled at once and the
user is told.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, no_disk_writes  # noqa: E402


def row(name="Track.flac", **extra):
    entry = {"file": name, "path": "/music/" + name, "channel": "#c", "user_raw": "Dave"}
    entry.update(extra)
    return entry


class WhenTheSendWasNeverQueued(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        config.dcc_queue.clear()
        self.sent = []
        patch = mock.patch.object(self.oserve, "queue_message",
                                  lambda user, text, *a, **k: self.sent.append((user, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def settle(self, entry, delivered=False, reason="the receiver never connected"):
        with mock.patch("builtins.print") as said:
            retained = dcc.release_queue_entry("Dave", entry, delivered=delivered, reason=reason)
        return retained, " ".join(str(c.args[0]) for c in said.call_args_list)

    def test_a_failure_is_not_kept_for_a_retry_that_cannot_happen(self):
        retained, log = self.settle(row())
        self.assertFalse(retained)
        self.assertNotIn("kept for retry", log)
        self.assertIn("nothing to retry", log)

    def test_the_user_is_told(self):
        self.settle(row("Some Song.flac"))
        self.assertEqual(len(self.sent), 1)
        user, text = self.sent[0]
        self.assertEqual(user, "Dave")
        self.assertIn("Could not send Some Song.flac", text)
        self.assertIn("the receiver never connected", text)

    def test_it_does_not_claim_to_have_removed_it_from_a_queue_it_was_never_in(self):
        self.settle(row())
        self.assertNotIn("Removed from your queue", self.sent[0][1])
        self.assertIn("Ask for it again", self.sent[0][1])

    def test_a_success_says_nothing(self):
        retained, _ = self.settle(row(), delivered=True)
        self.assertFalse(retained)
        self.assertEqual(self.sent, [])

    def test_the_real_queue_is_left_intact(self):
        other = row("Waiting.flac")
        config.dcc_queue["dave"] = [other]
        self.settle(row("Direct.flac"))
        self.assertEqual(config.dcc_queue["dave"], [other])


class WhenTheRowIsInAQueue(DCCoreTestCase):
    """Nothing changes for the ordinary queued file: three attempts, then the
    notice."""

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        config.dcc_queue.clear()
        self.sent = []
        patch = mock.patch.object(self.oserve, "queue_message",
                                  lambda user, text, *a, **k: self.sent.append((user, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def settle(self, entry, delivered=False):
        with mock.patch("builtins.print"):
            return dcc.release_queue_entry("Dave", entry, delivered=delivered, reason="timed out")

    def test_a_queued_row_is_kept_and_silent_until_the_budget_is_spent(self):
        entry = row()
        config.dcc_queue["dave"] = [entry]
        self.assertTrue(self.settle(entry))
        self.assertTrue(self.settle(entry))
        self.assertEqual(self.sent, [])
        self.assertEqual(entry["send_fails"], 2)

    def test_the_last_attempt_removes_it_and_says_so(self):
        entry = row()
        config.dcc_queue["dave"] = [entry]
        for _ in range(3):
            self.settle(entry)
        self.assertNotIn(entry, config.dcc_queue.get("dave", []))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Removed from your queue", self.sent[0][1])

    def test_a_row_under_a_renamed_key_is_still_recognised_as_queued(self):
        """#455: the nick can move while the file sends; the row is the identity."""
        entry = row()
        config.dcc_queue["dave_"] = [entry]
        self.assertTrue(self.settle(entry))


if __name__ == "__main__":
    unittest.main()
