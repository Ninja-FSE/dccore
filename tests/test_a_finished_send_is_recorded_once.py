"""#1328: dcc.record_completed_send(), step 1 of splitting start_dcc_send().

What a finished send counts - the totals, the download count, the transfer
record - was a block inside start_dcc_send(), and the tests guarding it
(#454's short send, the resume's skipped bytes) read it as text: reaching it
for real needed a peer, a socket and a partly-sent file. It takes a SentFile
now, so it is called directly here with the three writes recorded.

RecordsASend is shared with test_two_transfers_counted_wrong.py and
test_dcc_resume.py.

Every nick and file here is invented.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc  # noqa: E402
import transfer_log  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

SIZE = 4_000_000


def sent_file(**changes):
    values = dict(row={"user": "alfa"}, user="alfa", next_file={"queued_at": 100.0},
                  file_path=os.path.join("library", "Some Album", "01.flac"), file_name="01.flac",
                  file_size=SIZE, resume_offset=0, bytes_sent=SIZE, transfer_completed=True,
                  already_held=False, offered_at=110.0, start_time=120.0, transfer_finished_at=124.0)
    values.update(changes)
    return dcc.SentFile(**values)


class RecordsASend(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.stats, self.downloads, self.records = [], [], []
        for owner, name, sink in ((db, "update_stats_on_complete", self.stats),
                                  (db, "record_download", self.downloads),
                                  (transfer_log, "record_sent", self.records)):
            patcher = mock.patch.object(owner, name, side_effect=self.recorder(sink))
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def recorder(sink):
        def record(*args, **kwargs):
            sink.append((args, kwargs))
            return (len(sink), 0)
        return record

    def record(self, **changes):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            dcc.record_completed_send(sent_file(**changes))
        return said.getvalue()


class EachWayASendEnds(RecordsASend):

    def test_a_completed_send_is_counted_everywhere(self):
        said = self.record()

        self.assertEqual(self.stats, [((SIZE,), {})])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.records), 1)
        args, kwargs = self.records[0]
        self.assertEqual(args[3:6], (SIZE, SIZE, 4.0))
        self.assertEqual(args[6], SIZE / 4.0, "a measurable send carries its speed")
        self.assertEqual(args[7], 10.0, "the wait in the queue: offered minus queued")
        self.assertEqual(kwargs, {"nick": "alfa"})
        self.assertIn("Statistics updated", said)

    def test_a_short_send_counts_nothing(self):
        said = self.record(transfer_completed=False, bytes_sent=SIZE // 2)

        self.assertEqual((self.stats, self.downloads, self.records), ([], [], []))
        self.assertIn("Not counted", said)
        self.assertNotIn("[DB ERROR]", said)

    def test_a_file_the_receiver_already_held_counts_nothing(self):
        said = self.record(already_held=True, resume_offset=SIZE)

        self.assertEqual((self.stats, self.downloads, self.records), ([], [], []))
        self.assertIn("already held all", said)

    def test_a_resume_counts_only_what_went_on_the_wire(self):
        self.record(resume_offset=1_000_000)

        self.assertEqual(self.stats, [((SIZE - 1_000_000,), {})])
        self.assertEqual(self.records[0][0][4], SIZE - 1_000_000)

    def test_a_row_whose_outcome_is_already_claimed_is_not_recorded_twice(self):
        row = {"user": "alfa"}
        self.record(row=row)
        self.record(row=row)

        self.assertEqual(len(self.records), 1)
        self.assertEqual(len(self.stats), 2, "the totals are not the transfer record's to guard")

    def test_a_failing_database_is_reported_not_raised(self):
        with mock.patch.object(db, "update_stats_on_complete", side_effect=OSError("disk full")):
            said = self.record()

        self.assertIn("[DB ERROR]", said)
        self.assertIn("disk full", said)


if __name__ == "__main__":
    unittest.main()
