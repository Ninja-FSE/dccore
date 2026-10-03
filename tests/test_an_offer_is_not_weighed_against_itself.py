"""An offer is not weighed against its own request's old size (#1039).

A file row asked for again after a restart or a disk-full hold keeps the
total_size of its last offer. When the new offer arrives, the claim has
already moved the row to "receiving", so the room check counted that old size
as a transfer under way - the offer was weighed against itself, held, asked
again, and held again, for as long as the disk had less than twice its size
free (#964's loop, back in a narrower window).
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_a_file_too_big_for_the_disk_waits_for_room as room  # noqa: E402

GB = room.GB


class AskedAgainAfterAHold(room.RoomCase):
    def setUp(self):
        super().setUp()
        self.free = int(1.5 * GB)

    def held_once(self):
        """A file row that was offered 1 GB and held for room."""
        rid = self.ask("file", "Big.flac")
        row = config.fetch_queue[rid]
        row["total_size"] = GB          # what its first offer said
        dcc_fetch._hold_for_space(row, GB, "the disk filled up")
        return rid

    def test_the_offer_is_taken_when_it_fits(self):
        """1.5 GB free fits 1 GB with the 200 MB spare - once, not twice."""
        rid = self.held_once()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.offer(GB, "Big.flac")
        self.assertEqual(self.taken, ["_run_transfer"])

    def test_other_transfers_under_way_still_count(self):
        rid = self.held_once()
        dcc_fetch.check_fetch_queue()
        other = self.ask("file", "Other.flac")
        config.fetch_queue[other].update(state="receiving", total_size=GB // 2, bytes_received=0)
        self.offer(GB, "Big.flac")
        self.assertEqual(self.taken, [], "1.5 GB less the other half-gigabyte does not fit 1 GB + spare")
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
