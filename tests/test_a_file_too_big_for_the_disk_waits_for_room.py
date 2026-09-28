"""A file too big for the free space waits for room, once (#964).

The disk only counted as low under MIN_FREE_BYTES (200 MB). A 1.5 GB pack
offered with 1 GB free was accepted, filled the disk to nothing, failed with
ENOSPC and went back to pending - and once its partial file was removed the
disk had 1 GB free again, not low, so the next tick asked for it again. The
other bot sent it again, the disk filled again, for ever, and every other
write under the full disk (history, stats) failed with it.

Now an offer is weighed against the room left before connecting, and a row
that did not fit waits - saying why - until what it declared fits with
MIN_FREE_BYTES to spare.
"""

import errno
import shutil
import socket
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

# Imported as modules, not names: a TestCase class imported by name is
# collected and run again here.
import tests.test_a_failing_bot_is_paused as paused  # noqa: E402
import tests.test_a_folder_fetch_is_asked_for_again_as_a_folder as asked  # noqa: E402

GB = 1024 ** 3
FOLDER, OFFERED = asked.FOLDER, asked.OFFERED


class RoomCase(asked.AskedAgainCase):
    def setUp(self):
        super().setUp()
        self.set_config(MAX_FETCH_FOLDER_FILE_SIZE=0, MAX_FETCH_FILE_SIZE=0)
        self.free = 1 * GB
        real_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 13, 0, self.free)
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)
        # What would connect or listen, caught instead.
        self.taken = []
        for name in ("_run_transfer", "_serve_passive_offer"):
            real = getattr(dcc_fetch, name)
            setattr(dcc_fetch, name, lambda *a, _name=name, **k: self.taken.append(_name))
            self.addCleanup(setattr, dcc_fetch, name, real)

    def ask(self, request_type=None, text=None):
        rid = dcc_fetch.enqueue_fetch("ServerOne", text or FOLDER, request_type=request_type or "folder")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        self.said.clear()
        return rid

    def offer(self, size, name=OFFERED):
        dcc_fetch.handle_incoming_offer(None, "ServerOne", f"DCC SEND {name} 2130706433 55000 {size}")


class AnOfferThatDoesNotFit(RoomCase):
    def test_is_not_taken_and_waits_as_asked(self):
        rid = self.ask()
        self.offer(int(1.5 * GB))
        row = config.fetch_queue[rid]
        self.assertEqual(self.taken, [], "never connected")
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["waiting"], "disk-full")
        self.assertEqual(row["needs_bytes"], int(1.5 * GB))
        self.assertEqual(row["filename"], FOLDER, "asked again as the folder (#963)")
        self.assertIn("1536 MB", row["reason"])

    def test_is_not_asked_for_again_until_it_fits(self):
        """The audit's loop: 1 GB free is not low, and it was asked again."""
        rid = self.ask()
        self.offer(int(1.5 * GB))
        for _ in range(3):
            self.assertEqual(self.asked_again(), [])
        self.assertEqual(config.fetch_queue[rid]["waiting"], "disk-full")

        self.free = 2 * GB
        said = self.asked_again()
        self.assertEqual(len(said), 1)
        self.assertIn(f"!ServerOne {FOLDER}", said[0])
        self.assertNotIn("needs_bytes", config.fetch_queue[rid])

    def test_the_spare_is_kept_too(self):
        """Exactly the file's size free is not room: MIN_FREE_BYTES stays free."""
        self.ask()
        self.offer(self.free - dcc_fetch.MIN_FREE_BYTES + 1)
        self.assertEqual(self.taken, [])

    def test_counts_what_transfers_under_way_still_write(self):
        """Two files that each fit alone do not both fit together."""
        first = self.ask("file", "One.flac")
        self.offer(600 * 1024 * 1024, "One.flac")
        self.assertEqual(self.taken, ["_run_transfer"])
        config.fetch_queue[first]["state"] = "receiving"

        second = self.ask("file", "Two.flac")
        self.offer(600 * 1024 * 1024, "Two.flac")
        self.assertEqual(self.taken, ["_run_transfer"])
        self.assertEqual(config.fetch_queue[second]["waiting"], "disk-full")

    def test_a_passive_offer_is_not_listened_for_either(self):
        rid = self.ask()
        dcc_fetch.handle_incoming_offer(
            None, "ServerOne", f"DCC SEND {OFFERED} 2130706433 0 {int(1.5 * GB)} 77")
        self.assertEqual(self.taken, [])
        self.assertEqual(config.fetch_queue[rid]["state"], "pending")


class AnOfferThatFits(RoomCase):
    def test_is_taken(self):
        rid = self.ask()
        self.offer(500 * 1024 * 1024)
        self.assertEqual(self.taken, ["_run_transfer"])
        self.assertNotIn("needs_bytes", config.fetch_queue[rid])

    def test_a_disk_that_cannot_be_measured_holds_nothing_back(self):
        def unmeasurable(path):
            raise OSError(errno.EACCES, "denied")

        shutil.disk_usage = unmeasurable
        self.ask()
        self.offer(int(1.5 * GB))
        self.assertEqual(self.taken, ["_run_transfer"])


class TwoRowsWaitingForRoom(RoomCase):
    def test_room_for_one_lets_one_go(self):
        first, second = self.ask("file", "One.flac"), self.ask("file", "Two.flac")
        self.offer(int(1.5 * GB), "One.flac")
        self.offer(int(1.5 * GB), "Two.flac")
        self.free = 2 * GB
        said = self.asked_again()
        self.assertEqual(len(said), 1, "the room is spoken for by the first")
        states = sorted(config.fetch_queue[rid]["state"] for rid in (first, second))
        self.assertEqual(states, ["offered", "pending"])


class AfterTheDiskFilledUpMidway(RoomCase):
    def test_waits_for_the_whole_file(self):
        """The partial file is gone, the disk is not low - and it still waits."""
        rid = self.ask()
        row = config.fetch_queue[rid]
        with dcc_fetch._fetch_lock():
            dcc_fetch._claim_matching_offer_locked(config.fetch_queue, "ServerOne", OFFERED)

        class Full:
            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")

            def close(self):
                pass

        dcc_fetch.open = lambda *args, **kwargs: Full()
        self.addCleanup(delattr, dcc_fetch, "open")
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b"x" * 64)
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        size = int(1.5 * GB)

        run = getattr(type(self), "_real_run_transfer")
        run(row, {"size": size, "ip": None, "port": 0}, dest, OFFERED, sock=paused.TcpLike(ours))

        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["needs_bytes"], size)
        dcc_fetch._disk_was_low[0] = False
        self.assertEqual(self.asked_again(), [], "1 GB free is not low, and not room")
        self.free = 2 * GB
        self.assertEqual(len(self.asked_again()), 1)


AfterTheDiskFilledUpMidway._real_run_transfer = staticmethod(dcc_fetch._run_transfer)


if __name__ == "__main__":
    unittest.main()
