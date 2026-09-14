"""Two ways a transfer was accounted for wrongly.

  * #454 a send that ended SHORT was reported as a failure in one log line and
    recorded as a success everywhere that matters - the lifetime totals, the
    per-file download counter, the speed record that feeds the advert, and the
    "Sent the whole file" the operator reads.
  * #455 a row was settled under the nick the send STARTED as. A transfer
    takes minutes and a rename moves the queue to a new key, so the lookup
    found nothing, the delivered row was never removed, and the same file was
    handed out again on the next trigger.

The second is the other half of #431: that fix carries the queue to the new
nick, which is what leaves this lookup pointing at a key nobody uses.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve  # noqa: E402


class ARowIsSettledWhereverItNowSits(DCCoreTestCase):
    """#455."""

    def setUp(self):
        super().setUp()
        install_fake_oserve()
        self.row = {"file": "a.mp3", "path": "x", "send_fails": 0}
        config.dcc_queue["someuser"] = [self.row]
        config.channel_users["#chan"] = {"someuser"}

    def queued(self):
        return {k: len(v) for k, v in config.dcc_queue.items() if v}

    def test_a_rename_during_the_send_does_not_strand_the_row(self):
        """Settled under the nick the send started as, while the queue has
        already moved to the new one."""
        irc.note_nick_change("SomeUser", "SomeUser2")

        dcc.release_queue_entry("SomeUser", self.row, delivered=True, reason="done")

        self.assertEqual(self.queued(), {},
                         "the delivered row is still queued, so the same file "
                         "goes out again on the next trigger")

    def test_the_ordinary_case_still_goes_through_the_key(self):
        """The fallback is a fallback. With no rename the keyed removal is
        what runs, and it still works."""
        dcc.release_queue_entry("SomeUser", self.row, delivered=True, reason="done")

        self.assertEqual(self.queued(), {})

    def test_somebody_elses_row_is_not_taken(self):
        """The scan walks every queue, so it has to match the row OBJECT
        rather than anything about its contents - two people can queue the
        same filename."""
        other = {"file": "a.mp3", "path": "x", "send_fails": 0}
        config.dcc_queue["another"] = [other]
        irc.note_nick_change("SomeUser", "SomeUser2")

        dcc.release_queue_entry("SomeUser", self.row, delivered=True, reason="done")

        self.assertEqual(self.queued(), {"another": 1})

    def test_a_row_that_is_nowhere_is_not_an_error(self):
        """Settled twice, or settled after a !clearqueue took it - neither is
        a fault and neither may raise on the transfer thread."""
        config.dcc_queue.clear()

        dcc.release_queue_entry("SomeUser", self.row, delivered=True, reason="done")

        self.assertEqual(self.queued(), {})


class AShortSendIsNotACompletedTransfer(unittest.TestCase):
    """#454. Read from the source: reaching the real accounting means a peer,
    a socket and a partly-sent file, and the property is which statements are
    conditional."""

    @staticmethod
    def completion_block():
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        block = source.split("transfer_completed = bytes_sent >= file_size", 1)[1]
        return block.split("except socket.timeout", 1)[0]

    def test_the_success_line_is_conditional(self):
        block = self.completion_block()
        at = block.index("[DCC-SUCCESS]")
        before = block[:at]

        self.assertIn("if transfer_completed:", before,
                      "a truncated send still prints 'Sent the whole file'")

    def test_the_statistics_are_not_written_for_a_short_send(self):
        block = self.completion_block()
        stats_at = block.index("update_stats_on_complete")

        self.assertIn("if not transfer_completed:", block[:stats_at],
                      "the lifetime totals and the speed record still count a "
                      "send that ended early")

    def test_the_download_counter_is_behind_the_same_guard(self):
        """It sits after the stats write in the same try, so the skip has to
        carry past it rather than only guarding the first statement.

        assertIn before assertLess: str.index raises ValueError when the
        needle is gone, and "substring not found" tells whoever hits it
        nothing about what broke."""
        block = self.completion_block()

        self.assertIn("raise _ShortSend()", block,
                      "nothing skips the completion bookkeeping, so a short "
                      "send is counted again")
        self.assertIn("record_download", block)
        self.assertLess(block.index("raise _ShortSend()"),
                        block.index("record_download"))

    def test_the_skip_is_not_reported_as_a_database_error(self):
        """The block is wrapped in `except Exception` for the database writes.
        A deliberate skip caught by that would print 'Could not increment the
        sharing statistics' and send the next reader hunting a fault that
        never happened."""
        block = self.completion_block()

        self.assertIn("except _ShortSend:", block)
        self.assertLess(block.index("except _ShortSend:"),
                        block.index("except Exception as db_err:"))
