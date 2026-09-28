"""A folder or list fetch asked for again asks for the folder or list (#963).

A "folder" row goes out as "!Bot !rar <folder>", a "list" row as "@Bot". Neither
knows the name the other bot will give its file until the offer arrives, and
claiming the offer writes that name over the row's filename. When the row then
went back to pending - after a restart (#926), or because the disk filled up -
it was asked for again under the OFFERED name: "!Bot Artist_-_Album.rar", a
request for a file of that name, which the other bot does not have. The pack
or list never came. The original request text was kept all along in
requested_filename; it is now what is asked for again.
"""

import errno
import os
import shutil
import socket
import sys
import tempfile
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_a_failing_bot_is_paused import TcpLike  # noqa: E402

FOLDER = "!rar Artist - Album"
OFFERED = "Artist_-_Album.rar"


class AskedAgainCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=0, bot_joined_channel=True)
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)
        # What the dispatcher says, caught where it would go out.
        self.said = []
        stand_in = types.ModuleType("oserve")
        stand_in.queue_message = lambda target, line, is_vip=False: self.said.append(line)
        real = sys.modules.get("oserve")
        sys.modules["oserve"] = stand_in
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", real) if real
                        else sys.modules.pop("oserve", None))
        # Plenty of room on the disk, whatever the machine has.
        real_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 13, 0, 10 ** 12)
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)

    def ask_and_claim(self, request_type, text, offered):
        rid = dcc_fetch.enqueue_fetch("ServerOne", text, request_type=request_type)
        self.assertIsNotNone(rid)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rid]["state"], "offered")
        with dcc_fetch._fetch_lock():
            claimed = dcc_fetch._claim_matching_offer_locked(config.fetch_queue, "ServerOne", offered)
        self.assertEqual(claimed[0], rid)
        row = config.fetch_queue[rid]
        row["total_size"] = 123
        self.assertEqual(row["filename"], offered, "the claim does overwrite it")
        self.said.clear()
        return rid

    def asked_again(self):
        dcc_fetch.check_fetch_queue()
        return list(self.said)


class AfterARestart(AskedAgainCase):
    def restart(self, rid):
        config.fetch_queue[rid] = dcc_fetch._restart_form(config.fetch_queue[rid])

    def test_a_folder_is_asked_for_as_the_folder(self):
        rid = self.ask_and_claim("folder", FOLDER, OFFERED)
        self.restart(rid)
        said = self.asked_again()
        self.assertEqual(len(said), 1)
        self.assertIn(f"!ServerOne {FOLDER}", said[0])
        self.assertNotIn(OFFERED, said[0])
        self.assertIsNone(config.fetch_queue[rid]["total_size"])

    def test_a_list_is_asked_for_as_the_list(self):
        rid = self.ask_and_claim("list", "", "ServerOne-list.zip")
        self.restart(rid)
        said = self.asked_again()
        self.assertEqual(said, ["PRIVMSG #chan :@ServerOne\r\n"])
        self.assertEqual(config.fetch_queue[rid]["filename"], "")

    def test_a_file_keeps_its_own_name(self):
        rid = self.ask_and_claim("file", "Some Track.flac", "Some Track.flac")
        self.restart(rid)
        self.assertIn("!ServerOne Some Track.flac", self.asked_again()[0])


class AfterTheDiskFilledUp(AskedAgainCase):
    def test_a_folder_is_asked_for_as_the_folder(self):
        rid = self.ask_and_claim("folder", FOLDER, OFFERED)
        row = config.fetch_queue[rid]

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

        dcc_fetch._run_transfer(row, {"size": 64, "ip": None, "port": 0}, dest, OFFERED,
                                sock=TcpLike(ours))

        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["filename"], FOLDER)
        dcc_fetch._disk_was_low[0] = False
        said = self.asked_again()
        self.assertEqual(len(said), 1)
        self.assertIn(f"!ServerOne {FOLDER}", said[0])


if __name__ == "__main__":
    unittest.main()
