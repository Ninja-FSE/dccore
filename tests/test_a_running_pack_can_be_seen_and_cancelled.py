"""A folder pack the operator could neither see nor stop (#1202).

`rar` ran inside a blocking subprocess.run with no handle kept, so the user
being packed for looked "queued" and nothing short of RAR_TIMEOUT (or
stopping the bot) ended a pack that held the one pack interlock for every
other user. The pack is now a Popen the packer keeps on runtime.pack_job:
dcc.pack_status() says who it is for, which folder (never its path), how
long it has run and how big the archive is so far; dcc.cancel_pack()
terminates THAT process and the packer clears up after it.

The fake rar is a real process - a Python script that writes slowly - so the
cancel is a real terminate, on every CI platform.
"""

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
import unittest

from tests import support
from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug

import announce
import db
import dcc
import defaults as config
import platform_compat
import runtime

USER = "dave"
OTHER = "dan"

SLOW_RAR = r'''
import os, sys, time
target = [a for a in sys.argv if a.endswith(".rar")][0]
release = os.environ.get("FAKE_RAR_RELEASE", "")
with open(target, "wb") as out:
    while not (release and os.path.exists(release)):
        out.write(b"R" * 4096)
        out.flush()
        time.sleep(0.02)
sys.exit(0)
'''


class RunningPackTests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        CHANNEL="#somechannel", MAX_DCC_SLOTS=3, RAR_ENABLED=True,
                        TMP_ZIP_DIR=os.path.join(self.tree.root, "tmp"),
                        bot_joined_channel=True, rar_inprogress=False)
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        no_disk_writes(db)
        silence_debug(announce)
        self.oserve = support.install_fake_oserve()
        config.channel_users["#somechannel"] = {USER, OTHER}
        for album in ("Metallica/Black Album (1991)", "Pink Floyd/The Wall (1979)"):
            folder = os.path.join(self.tree.music, *album.split("/"))
            os.makedirs(folder, exist_ok=True)
            with io.open(os.path.join(folder, "track.flac"), "wb") as handle:
                handle.write(b"\x00" * 5000)

        scratch = support.temp_dir(self)
        script = os.path.join(scratch, "slow_rar")
        with io.open(script, "w") as handle:
            handle.write(SLOW_RAR)
        self.release = os.path.join(scratch, "release")
        os.environ["FAKE_RAR_RELEASE"] = self.release
        self.addCleanup(os.environ.pop, "FAKE_RAR_RELEASE", None)
        self.rars = []
        real_popen = subprocess.Popen

        def popen(cmd, *args, **kwargs):
            process = real_popen([sys.executable, script] + list(cmd[1:]), *args, **kwargs)
            self.rars.append(process)
            return process

        subprocess.Popen = popen
        self.addCleanup(setattr, subprocess, "Popen", real_popen)
        self._real_rar = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: "rar-for-the-test"
        self.addCleanup(setattr, platform_compat, "rar_command", self._real_rar)
        real_sleep = dcc.time.sleep
        dcc.time.sleep = lambda *_a: None
        self.addCleanup(setattr, dcc.time, "sleep", real_sleep)
        self.addCleanup(setattr, runtime, "packer_thread", None)
        self.addCleanup(setattr, runtime, "pack_job", None)
        self.addCleanup(self._let_everything_finish)

    def _let_everything_finish(self):
        with io.open(self.release, "w"):
            pass
        for process in self.rars:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        thread = runtime.packer_thread
        if thread is not None:
            thread.join(10)

    def request(self, user, album):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.handle_download_request(RecordingSocket(), user, "!rar " + album, "#somechannel")

    def wait_for(self, condition, what, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for " + what)

    def start_a_pack(self, user=USER, album="Metallica/Black Album (1991)"):
        self.request(user, album)
        self.wait_for(lambda: dcc.pack_status() is not None and dcc.pack_status()["done"] > 0,
                      "rar to start writing")

    def test_nothing_packing_has_no_status_and_a_cancel_is_a_no_op(self):
        self.assertIsNone(dcc.pack_status())

        self.assertIsNone(dcc.cancel_pack())

        self.assertFalse(config.rar_inprogress)

    def test_the_status_names_the_user_and_the_folder_and_never_a_path(self):
        self.start_a_pack()

        status = dcc.pack_status()

        self.assertEqual(status["user"], USER)
        self.assertEqual(status["name"], "Black Album (1991)")
        self.assertNotIn(self.tree.root, repr(status))
        self.assertNotIn(os.sep, status["name"])

    def test_the_archive_size_grows_and_the_folder_total_is_known(self):
        self.start_a_pack()
        first = dcc.pack_status()["done"]

        self.wait_for(lambda: dcc.pack_status()["done"] > first, "the archive to grow")
        self.wait_for(lambda: dcc.pack_status()["total"] > 0, "the folder to be measured")

        on_disk = sum(os.path.getsize(os.path.join(here, name))
                      for here, _dirs, files in os.walk(os.path.join(self.tree.music, "Metallica", "Black Album (1991)"))
                      for name in files)
        self.assertEqual(dcc.pack_status()["total"], on_disk)
        self.assertGreaterEqual(dcc.pack_status()["elapsed"], 0)

    def test_a_cancel_terminates_that_process_and_removes_the_partial_archive(self):
        self.start_a_pack()
        archive = runtime.pack_job["archive"]
        self.assertTrue(os.path.exists(archive))
        process = self.rars[0]

        cancelled = dcc.cancel_pack()

        self.assertEqual(cancelled["user"], USER)
        self.assertEqual(cancelled["name"], "Black Album (1991)")
        self.wait_for(lambda: process.poll() is not None, "rar to stop")
        self.wait_for(lambda: not dcc.a_pack_is_running(), "the packer to finish")
        self.assertFalse(os.path.exists(archive), "the partial archive was left behind")
        self.assertEqual(os.listdir(config.TMP_ZIP_DIR), [])

    def test_a_cancel_releases_the_interlock_and_leaves_no_job(self):
        self.start_a_pack()

        dcc.cancel_pack()
        self.wait_for(lambda: not dcc.a_pack_is_running(), "the packer to finish")

        self.assertFalse(config.rar_inprogress)
        self.assertNotIn(USER, config.user_processing_lock)
        self.assertIsNone(runtime.pack_job)
        self.assertIsNone(dcc.pack_status())

    def test_a_second_cancel_is_a_no_op(self):
        self.start_a_pack()

        self.assertIsNotNone(dcc.cancel_pack())
        self.assertIsNone(dcc.cancel_pack())

    def test_the_cancelled_row_is_removed_and_not_charged_to_the_retry_budget(self):
        self.start_a_pack()
        row = config.dcc_queue[USER][0]

        dcc.cancel_pack()
        self.wait_for(lambda: not dcc.a_pack_is_running(), "the packer to finish")

        self.assertNotIn(row, config.dcc_queue.get(USER, []))
        self.assertNotIn("send_fails", row)

    def test_the_user_is_told_without_any_local_path(self):
        self.start_a_pack()

        dcc.cancel_pack()
        self.wait_for(lambda: not dcc.a_pack_is_running(), "the packer to finish")

        notices = [m for u, m, _v in self.oserve.queued if u == USER and "cancelled" in m]
        self.assertEqual(len(notices), 1, notices)
        self.assertIn("was cancelled by the operator", notices[0])
        self.assertIn("Black_Album_(1991).rar", notices[0])
        self.assertIn("Please ask again", notices[0])
        self.assertNotIn(self.tree.root, notices[0])
        self.assertNotIn(os.sep, notices[0].split(":", 1)[1])

    def test_the_next_waiting_request_starts_when_the_pack_is_cancelled(self):
        self.start_a_pack()
        self.request(OTHER, "Pink Floyd/The Wall (1979)")
        self.assertEqual(len(self.rars), 1, "the second pack must wait for the interlock")

        dcc.cancel_pack()

        self.wait_for(lambda: len(self.rars) == 2, "the waiting pack to start")
        self.wait_for(lambda: (dcc.pack_status() or {}).get("user") == OTHER, "its status")
        self.assertEqual(dcc.pack_status()["name"], "The Wall (1979)")

    def test_a_finished_pack_is_not_a_cancel(self):
        """The same path without the cancel: the job ends, the archive stays."""
        self.start_a_pack()
        archive = runtime.pack_job["archive"]

        with io.open(self.release, "w"):
            pass
        self.wait_for(lambda: runtime.pack_job is None, "the pack to finish")

        self.assertIsNone(dcc.cancel_pack())
        self.assertTrue(os.path.exists(archive))


if __name__ == "__main__":
    unittest.main()
