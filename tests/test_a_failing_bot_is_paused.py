"""#926 item 4: a bot we cannot reach is paused, any bot can be paused and
resumed, and a full disk holds fetching back instead of failing it.

- Three ACTIVE connect failures in a row pause a bot (AutoGet disabled a nick
  after three "unable to connect"): it fails every file the same way. A
  finished transfer resets the count; a passive offer nobody connects back to
  is our side, not theirs, and does not count.
- A paused bot's requests wait, "Paused", with a Resume button; the pause
  survives a restart.
- Under MIN_FREE_BYTES free where fetched files go, no new fetch starts; a
  transfer that fills the disk goes back to pending; both carry on by
  themselves once there is space.
"""

import errno
import io
import os
import shutil
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class TcpLike:
    """One end of socket.socketpair(), answering getpeername() the way a real
    fetch's TCP socket does. On Linux and macOS the pair is AF_UNIX, whose
    getpeername() is '' - and _run_transfer() reads [0] of it, which only a
    TCP address has. On Windows the pair is TCP already."""

    def __init__(self, sock):
        self._sock = sock

    def getpeername(self):
        return ("127.0.0.1", 50000)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class PauseCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=0)
        config.channel_users["#chan"] = {"serverone", "servertwo"}
        self.feed = silence_debug(announce)

    def queue_up(self, bot="ServerOne", count=1):
        return [dcc_fetch.enqueue_fetch(bot, f"Track {n}.flac") for n in range(count)]

    def state(self, rid):
        return config.fetch_queue[rid]["state"]


class ThreeFailuresPauseABot(PauseCase):
    def test_the_third_in_a_row_pauses_it_and_says_so(self):
        for _ in range(2):
            dcc_fetch._note_connect_failure("ServerOne")
        self.assertNotIn("serverone", dcc_fetch.paused_bots())
        dcc_fetch._note_connect_failure("ServerOne")
        paused = dcc_fetch.paused_bots()["serverone"]
        self.assertEqual(paused["by"], "auto")
        self.assertIn("could not connect 3 times", paused["reason"])
        self.assertTrue(any("Fetching from ServerOne is paused" in text for _c, text in self.feed))

    def test_a_finished_transfer_resets_the_count(self):
        dcc_fetch._note_connect_failure("ServerOne")
        dcc_fetch._note_connect_failure("ServerOne")
        dcc_fetch._note_connect_success("ServerOne")
        dcc_fetch._note_connect_failure("ServerOne")
        self.assertNotIn("serverone", dcc_fetch.paused_bots())

    def test_a_real_finished_transfer_resets_it(self):
        """Through _run_transfer() itself, over a local socket pair."""
        import socket
        import tempfile
        dcc_fetch._note_connect_failure("ServerOne")
        dcc_fetch._note_connect_failure("ServerOne")
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b"x" * 64)
        (rid,) = self.queue_up()
        row = config.fetch_queue[rid]
        row.update(state="receiving")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        dcc_fetch._run_transfer(row, {"size": 64, "ip": None, "port": 0}, dest, "Track.flac", sock=TcpLike(ours))
        self.assertEqual(row["state"], "complete")
        dcc_fetch._note_connect_failure("ServerOne")
        self.assertNotIn("serverone", dcc_fetch.paused_bots())

    def test_only_an_active_connect_counts(self):
        """The call sits in the active connect's failure branch, not the
        passive listener's."""
        with io.open(os.path.join(REPO_ROOT, "dcc_fetch.py"), encoding="utf-8") as handle:
            code = handle.read()
        connect = code.index('_mark_failed_locked(row, f"connect error: {connect_err}")')
        self.assertIn("_note_connect_failure(row.get(\"bot\"))", code[connect:connect + 600])
        passive = code.index('_mark_failed_locked(row, "passive offer: no connection received")')
        self.assertNotIn("_note_connect_failure", code[passive - 400:passive + 400])


class APausedBotWaits(PauseCase):
    def test_its_requests_wait_as_paused_and_others_go(self):
        dcc_fetch.pause_bot("ServerOne", "testing")
        (one,) = self.queue_up("ServerOne")
        (two,) = self.queue_up("ServerTwo")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(one), "pending")
        self.assertEqual(config.fetch_queue[one]["waiting"], "paused")
        self.assertEqual(self.state(two), "offered")

    def test_resumed_it_goes(self):
        dcc_fetch.pause_bot("ServerOne", "testing")
        (one,) = self.queue_up("ServerOne")
        dcc_fetch.check_fetch_queue()
        status, _result = webserver.build_fetch_pause_result({"bot": "serverone"}, False)
        self.assertEqual(status, 200)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(one), "offered")

    def test_the_operator_can_pause_any_bot(self):
        status, _result = webserver.build_fetch_pause_result({"bot": "ServerTwo"}, True)
        self.assertEqual(status, 200)
        self.assertEqual(dcc_fetch.paused_bots()["servertwo"]["by"], "operator")

    def test_resuming_a_bot_that_is_not_paused_says_so(self):
        status, _result = webserver.build_fetch_pause_result({"bot": "ServerTwo"}, False)
        self.assertEqual(status, 404)
        self.assertEqual(webserver.build_fetch_pause_result({}, True)[0], 400)

    def test_a_pause_survives_a_restart(self):
        dcc_fetch.pause_bot("ServerOne", "testing")
        dcc_fetch._paused.clear()
        dcc_fetch.load_paused_bots()
        self.assertIn("serverone", dcc_fetch.paused_bots())
        dcc_fetch.resume_bot("ServerOne")
        dcc_fetch._paused["serverone"] = {"nick": "x"}
        dcc_fetch.load_paused_bots()
        self.assertNotIn("serverone", dcc_fetch.paused_bots(), "the resume was saved too")


class AFullDiskWaits(PauseCase):
    def low_disk(self, free):
        real = shutil.disk_usage

        def usage(path):
            return shutil._ntuple_diskusage(10 ** 12, 10 ** 12 - free, free)
        shutil.disk_usage = usage
        self.addCleanup(setattr, shutil, "disk_usage", real)

    def test_no_new_fetch_starts_and_it_is_said_once(self):
        self.low_disk(10 * 1024 * 1024)
        (rid,) = self.queue_up()
        dcc_fetch.check_fetch_queue()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(rid), "pending")
        self.assertEqual(config.fetch_queue[rid]["waiting"], "disk-full")
        said = [text for _c, text in self.feed if "Fetching is waiting" in text]
        self.assertEqual(len(said), 1)

    def test_it_carries_on_by_itself_once_there_is_space(self):
        self.low_disk(10 * 1024 * 1024)
        (rid,) = self.queue_up()
        dcc_fetch.check_fetch_queue()
        self.low_disk(10 * 1024 ** 3)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(rid), "offered")
        self.assertTrue(any("there is space for fetched files again" in text for _c, text in self.feed))

    def test_a_disk_that_cannot_be_measured_is_not_called_low(self):
        def broken(path):
            raise OSError("no such device")
        real = shutil.disk_usage
        shutil.disk_usage = broken
        self.addCleanup(setattr, shutil, "disk_usage", real)
        (rid,) = self.queue_up()
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.state(rid), "offered")

    def test_a_transfer_that_fills_the_disk_goes_back_to_pending(self):
        """The real _run_transfer(), over a local socket pair, into a file
        whose write says there is no space left."""
        import socket
        import tempfile

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
        (rid,) = self.queue_up()
        row = config.fetch_queue[rid]
        row.update(state="receiving")
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)

        dcc_fetch._run_transfer(row, {"size": 64, "ip": None, "port": 0}, dest, "Track.flac", sock=TcpLike(ours))

        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["waiting"], "disk-full")
        self.assertEqual(row["bytes_received"], 0)

    def test_disk_full_is_recognised(self):
        self.assertTrue(dcc_fetch._is_disk_full(OSError(errno.ENOSPC, "No space left on device")))
        self.assertFalse(dcc_fetch._is_disk_full(OSError(errno.ECONNRESET, "reset")))
        self.assertFalse(dcc_fetch._is_disk_full(ValueError("x")))


if __name__ == "__main__":
    unittest.main()
