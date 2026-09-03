"""Regression tests for dcc.release_queue_entry - queue row settlement.

The old code settled a finished transfer with ``config.dcc_queue[u_key].pop(0)``
in start_dcc_send's ``finally``. That removes whatever happens to be FIRST at
that instant, which is not necessarily the entry that was actually sent:

  * the direct-send fast path builds a synthetic entry that was never inserted
    into the queue, so position 0 is by construction a different, unsent file;
  * the queue can be appended to or promoted (VIP) while a transfer runs;
  * a FAILED attempt consumed the row as eagerly as a successful one, so the
    file was silently lost.

release_queue_entry replaced it: removal is by IDENTITY, a failure keeps the row
for retry with the attempt count stored ON the row as 'send_fails', and the retry
budget is config.MAX_SEND_FAILS. Rows that cannot possibly succeed on a retry
(a consumed temporary archive whose .rar the cleanup step has already deleted,
and legacy non-dict rows with nowhere to store a counter) are settled on their
first failure instead.

Every test below is written so that it FAILS against the old positional pop or
against a naive re-implementation (side-dict counters, equality-based removal,
hardcoded budget).
"""

import contextlib
import io
import unittest

from tests.support import (DCCoreTestCase, no_disk_writes, queue_row,
                           silence_debug)

import announce
import defaults as config_mod
import db
import dcc


class ReleaseQueueEntryTests(DCCoreTestCase):
    """dcc.release_queue_entry(user, next_file, delivered, reason="") -> retained"""

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        self.debug = silence_debug(announce)
        # release_queue_entry prints an audit line for every settlement; keep the
        # unittest output readable but hold on to the text for assertions.
        self.printed = []

    # -- helpers ---------------------------------------------------------

    def settle(self, user, entry, delivered, reason="test"):
        """Call the real function with stdout captured."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            retained = dcc.release_queue_entry(user, entry, delivered=delivered,
                                               reason=reason)
        self.printed.append(buffer.getvalue())
        return retained

    def set_budget(self, budget):
        """Override config.MAX_SEND_FAILS for one test and restore it after."""
        original = getattr(config_mod, "MAX_SEND_FAILS", 3)
        config_mod.MAX_SEND_FAILS = budget
        self.addCleanup(setattr, config_mod, "MAX_SEND_FAILS", original)

    def notices(self):
        """Messages the stub oserve was asked to queue, as plain strings."""
        return [message for _user, message, _vip in self.oserve.queued]

    # -- identity removal ------------------------------------------------

    def test_delivered_row_removed_by_identity_not_position(self):
        """Guards: pop(0) removed the FIRST row, not the row actually sent."""
        first = queue_row(user="dave", filename="01 - Enter Sandman.flac")
        second = queue_row(user="dave", filename="02 - Sad But True.flac")
        self.config.dcc_queue["dave"] = [first, second]

        retained = self.settle("dave", second, delivered=True)

        rows = self.config.dcc_queue["dave"]
        self.assertFalse(retained, "a delivered row is consumed, never retained")
        self.assertEqual(len(rows), 1)
        # Identity, not equality: the surviving row must be the very object that
        # was never sent. Positional pop(0) would have killed 'first' instead.
        self.assertIs(rows[0], first)
        self.assertNotIn(second, rows)

    def test_removal_is_by_identity_even_with_identical_rows(self):
        """Guards: equality-based removal would drop a duplicate request too."""
        # Same user asking for the same file twice produces two equal dicts.
        first = queue_row(user="dave", filename="Song.flac")
        second = queue_row(user="dave", filename="Song.flac")
        self.assertEqual(first, second)  # equal by value...
        self.assertIsNot(first, second)  # ...but distinct objects
        self.config.dcc_queue["dave"] = [first, second]

        self.settle("dave", second, delivered=True)

        rows = self.config.dcc_queue["dave"]
        self.assertEqual(len(rows), 1, "only the sent object may be removed")
        self.assertIs(rows[0], first)

    def test_synthetic_direct_send_entry_removes_nothing(self):
        """Guards: the direct-send path's synthetic entry ate a queued row."""
        queued_a = queue_row(user="dave", filename="Queued A.flac")
        queued_b = queue_row(user="dave", filename="Queued B.flac")
        self.config.dcc_queue["dave"] = [queued_a, queued_b]

        # start_dcc_send's direct path hands over an entry it built on the fly and
        # never inserted into config.dcc_queue.
        synthetic = queue_row(user="dave", filename="Direct Send.flac")

        retained = self.settle("dave", synthetic, delivered=True)

        rows = self.config.dcc_queue["dave"]
        self.assertFalse(retained)
        self.assertEqual(len(rows), 2, "a synthetic entry must consume nothing")
        self.assertIs(rows[0], queued_a)
        self.assertIs(rows[1], queued_b)

    def test_other_users_queues_are_never_touched(self):
        """Guards: settlement is scoped to the sending user's own queue."""
        mine = queue_row(user="dave", filename="Song.flac")
        theirs = queue_row(user="erin", filename="Song.flac")
        self.config.dcc_queue["dave"] = [mine]
        self.config.dcc_queue["erin"] = [theirs]

        self.settle("dave", mine, delivered=True)

        self.assertEqual(self.config.dcc_queue["dave"], [])
        self.assertEqual(self.config.dcc_queue["erin"], [theirs])

    def test_user_key_is_case_insensitive(self):
        """Guards: queues are keyed lowercase; IRC hands back mixed case."""
        row = queue_row(user="Dave", filename="Song.flac")
        self.config.dcc_queue["dave"] = [row]

        self.settle("DaVe", row, delivered=True)

        self.assertEqual(self.config.dcc_queue["dave"], [])

    # -- failure keeps the row until the budget runs out -----------------

    def test_failed_attempt_keeps_row_until_budget_then_drops_it(self):
        """Guards: a failed attempt used to consume the row immediately."""
        self.set_budget(3)
        target = queue_row(user="dave", filename="Broken.flac")
        neighbour = queue_row(user="dave", filename="Neighbour.flac")
        self.config.dcc_queue["dave"] = [target, neighbour]

        # Attempts 1..N-1: the row survives and carries its own counter.
        for attempt in (1, 2):
            retained = self.settle("dave", target, delivered=False,
                                   reason="transfer did not complete")
            self.assertTrue(retained, "attempt %d must keep the row" % attempt)
            self.assertEqual(target.get("send_fails"), attempt,
                             "the attempt count lives ON the row")
            self.assertIn(target, self.config.dcc_queue["dave"])
            self.assertEqual(self.notices(), [],
                             "a retryable failure must not tell the user it was dropped")

        # Attempt N: budget spent, row dropped - and only that row.
        retained = self.settle("dave", target, delivered=False,
                               reason="transfer did not complete")
        rows = self.config.dcc_queue["dave"]
        self.assertFalse(retained)
        self.assertEqual(target["send_fails"], 3)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0], neighbour, "the neighbouring row is never touched")
        self.assertNotIn("send_fails", neighbour,
                         "the counter belongs to the failing row alone")

    def test_retry_budget_is_read_from_config(self):
        """Guards: MAX_SEND_FAILS must be honoured, not hardcoded to 3."""
        self.set_budget(5)
        row = queue_row(user="dave", filename="Stubborn.flac")
        self.config.dcc_queue["dave"] = [row]

        for attempt in range(1, 5):
            self.assertTrue(self.settle("dave", row, delivered=False),
                            "attempt %d is still within a budget of 5" % attempt)
            self.assertIn(row, self.config.dcc_queue["dave"])

        self.assertFalse(self.settle("dave", row, delivered=False))
        self.assertEqual(self.config.dcc_queue["dave"], [])
        self.assertEqual(row["send_fails"], 5)

    def test_budget_of_one_drops_on_the_first_failure(self):
        """Guards: an off-by-one in the budget comparison (> vs >=)."""
        self.set_budget(1)
        row = queue_row(user="dave", filename="OneShot.flac")
        self.config.dcc_queue["dave"] = [row]

        retained = self.settle("dave", row, delivered=False)

        self.assertFalse(retained)
        self.assertEqual(self.config.dcc_queue["dave"], [])

    def test_dropped_row_notifies_the_user(self):
        """Guards: the positional pop discarded files with no word to the user."""
        self.set_budget(1)
        row = queue_row(user="dave", filename="Gone.flac")
        self.config.dcc_queue["dave"] = [row]

        self.settle("dave", row, delivered=False, reason="peer went away")

        messages = self.notices()
        self.assertEqual(len(messages), 1)
        text = messages[0]
        self.assertIn("Gone.flac", text)
        self.assertIn("peer went away", text)
        self.assertIn("dave", text)
        self.assertTrue(text.endswith("\r\n"), "IRC lines must be CRLF terminated")
        self.assertEqual(self.oserve.queued[0][0], "dave")

    def test_delivery_sends_no_dropped_notice(self):
        """Guards: a successful send must not warn the user about a removal."""
        row = queue_row(user="dave", filename="Fine.flac")
        self.config.dcc_queue["dave"] = [row]

        self.settle("dave", row, delivered=True, reason="transfer complete")

        self.assertEqual(self.notices(), [])

    def test_delivered_row_takes_its_failure_count_with_it(self):
        """Guards: a side-dict counter would be inherited by the next request."""
        self.set_budget(3)
        row = queue_row(user="dave", filename="Flaky.flac")
        self.config.dcc_queue["dave"] = [row]

        self.assertTrue(self.settle("dave", row, delivered=False))
        self.assertTrue(self.settle("dave", row, delivered=False))
        self.assertEqual(row["send_fails"], 2)

        # Third attempt finally gets through: the row - counter and all - is gone.
        self.assertFalse(self.settle("dave", row, delivered=True))
        self.assertEqual(self.config.dcc_queue["dave"], [])

        # The same user asks for the same file again. A fresh row must start from
        # zero; if the count had been kept in a dict keyed by user/filename this
        # single failure would already exhaust the budget.
        again = queue_row(user="dave", filename="Flaky.flac")
        self.config.dcc_queue["dave"] = [again]

        retained = self.settle("dave", again, delivered=False)

        self.assertTrue(retained, "a new row must not inherit an old failure count")
        self.assertEqual(again["send_fails"], 1)
        self.assertIn(again, self.config.dcc_queue["dave"])

    # -- rows that must not be retried -----------------------------------

    def test_consumed_temporary_archive_is_dropped_on_first_failure(self):
        """Guards: retrying a temp .rar the cleanup step already deleted."""
        # The cleanup step in start_dcc_send removes the temporary archive from
        # disk, so any retry would abort on file_size == 0 and emit a misleading
        # error to the channel. Such a row is settled at once.
        temp = queue_row(user="dave", filename="Album.rar", is_temporary_zip=True)
        neighbour = queue_row(user="dave", filename="Neighbour.flac")
        self.config.dcc_queue["dave"] = [temp, neighbour]

        retained = self.settle("dave", temp, delivered=False, reason="socket died")

        rows = self.config.dcc_queue["dave"]
        self.assertFalse(retained)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0], neighbour)
        self.assertNotIn("send_fails", temp,
                         "a non-retryable row is not charged to the retry budget")
        self.assertEqual(len(self.notices()), 1, "the user is told the file was dropped")
        self.assertIn("Album.rar", self.notices()[0])

    def test_pending_rar_folder_row_is_still_retryable(self):
        """Guards: !rar folder rows wrongly lumped in with consumed archives."""
        # is_temporary_zip is also set on a !rar folder request whose archive has
        # not been built and consumed yet; is_unpacked_rar_folder marks it as such,
        # and it must keep its retries.
        row = queue_row(user="dave", filename="Black Album", is_temporary_zip=True,
                        is_unpacked_rar_folder=True)
        self.config.dcc_queue["dave"] = [row]

        retained = self.settle("dave", row, delivered=False, reason="socket died")

        self.assertTrue(retained)
        self.assertEqual(row["send_fails"], 1)
        self.assertIn(row, self.config.dcc_queue["dave"])
        self.assertEqual(self.notices(), [])

    def test_legacy_non_dict_row_is_dropped_on_first_failure(self):
        """Guards: a plain-string row retried forever with nowhere to count."""
        legacy = "Old Format Song.flac"
        neighbour = queue_row(user="dave", filename="Neighbour.flac")
        self.config.dcc_queue["dave"] = [legacy, neighbour]

        retained = self.settle("dave", legacy, delivered=False, reason="socket died")

        rows = self.config.dcc_queue["dave"]
        self.assertFalse(retained, "a non-dict row cannot carry a counter, so no retry")
        self.assertEqual(rows, [neighbour])
        self.assertIn(legacy, self.notices()[0],
                      "the dropped-file notice falls back to str(row)")

    def test_legacy_non_dict_row_is_removed_on_delivery(self):
        """Guards: identity removal has to cope with pre-dict queue rows too."""
        legacy = "Old Format Song.flac"
        self.config.dcc_queue["dave"] = [legacy]

        retained = self.settle("dave", legacy, delivered=True)

        self.assertFalse(retained)
        self.assertEqual(self.config.dcc_queue["dave"], [])

    # -- degenerate input must never raise -------------------------------

    def test_unknown_user_does_not_raise(self):
        """Guards: settling for a user with no queue at all (KeyError)."""
        orphan = queue_row(user="ghost", filename="Nothing.flac")

        self.assertFalse(self.settle("ghost", orphan, delivered=True))
        # A failure still charges the retry budget - the row simply is not in any
        # queue, so nothing is removed. What matters is that it does not raise.
        self.assertTrue(self.settle("ghost", orphan, delivered=False))
        self.assertEqual(orphan["send_fails"], 1)
        # No queue was created as a side effect of settling.
        self.assertEqual(self.config.dcc_queue.get("ghost", []), [])
        self.assertNotIn("ghost", self.config.dcc_queue)

    def test_empty_queue_does_not_raise(self):
        """Guards: settling against an empty list (IndexError from pop(0))."""
        self.config.dcc_queue["dave"] = []
        row = queue_row(user="dave", filename="Nothing.flac")

        self.assertFalse(self.settle("dave", row, delivered=True))
        self.assertEqual(self.config.dcc_queue["dave"], [])

    def test_none_entry_does_not_raise(self):
        """Guards: a crash settling a None entry after an aborted selection."""
        survivor = queue_row(user="dave", filename="Survivor.flac")
        self.config.dcc_queue["dave"] = [survivor]

        self.assertFalse(self.settle("dave", None, delivered=True))
        self.assertFalse(self.settle("dave", None, delivered=False))

        self.assertEqual(self.config.dcc_queue["dave"], [survivor],
                         "a None entry matches nothing and removes nothing")

    def test_missing_oserve_does_not_raise(self):
        """Guards: dropping a row before/after oserve is in sys.modules."""
        import sys
        removed = sys.modules.pop("oserve")
        self.addCleanup(sys.modules.__setitem__, "oserve", removed)

        self.set_budget(1)
        row = queue_row(user="dave", filename="Gone.flac")
        self.config.dcc_queue["dave"] = [row]

        self.assertFalse(self.settle("dave", row, delivered=False))
        self.assertEqual(self.config.dcc_queue["dave"], [])

    def test_persistence_failure_does_not_lose_the_settlement(self):
        """Guards: a disk error must not abort the in-RAM queue update."""
        def explode():
            raise IOError("data/ is read-only")

        db.save_dcc_queue = explode
        row = queue_row(user="dave", filename="Song.flac")
        self.config.dcc_queue["dave"] = [row]

        self.assertFalse(self.settle("dave", row, delivered=True))
        self.assertEqual(self.config.dcc_queue["dave"], [])


if __name__ == "__main__":
    unittest.main()
