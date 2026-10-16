"""#1300: dcc.pack_folder_for_request(), step 2 of splitting dcc.py.

The inline RAR packer was _inline_rar_packer_body(), a closure inside
check_queue_and_send(), reachable only by queueing a !rar row and driving a
whole dispatch. It is called directly here, for each way it can end:

  poisoned row  -> None, the row dropped, rar never run
  withdrawn row -> False, rar never run
  rar failed    -> False, the partial archive gone
  packed, but no free slot -> None, the row now the .rar and still queued

The wrapper that keeps the interlocks and releases them (inline_rar_packer)
is still driven through check_queue_and_send() by test_path_security.py,
test_the_pack_interlocks_are_released_once.py and the rest.

Every nick, channel and folder here is invented.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402


class ThePackerOnItsOwn(security.PathSecurityBase):

    make_row = security.PoisonedQueueRowTests.make_row

    def setUp(self):
        super().setUp()
        config.channel_users = {"#dccore-test": {"dave"}}
        config.bot_joined_channel = True
        # No real rar, as in test_path_security's PoisonedQueueRowTests: a
        # failure unless a test says otherwise, and every call remembered.
        self.rar_calls = []

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "stubbed rar"

        def failing(cmd, *a, **kw):
            self.rar_calls.append(cmd)
            return Failed()

        support.fake_rar_runs(self, dcc.subprocess, failing)
        import platform_compat
        real = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(self.tree.root, "rar")
        self.addCleanup(setattr, platform_compat, "rar_command", real)
        self.album = os.path.join(self.tree.music, "Some Album")
        os.makedirs(self.album, exist_ok=True)
        with open(os.path.join(self.album, "01.flac"), "wb") as handle:
            handle.write(b"\x00" * 1024)

    def pack(self, row, owner="dave"):
        with security.quiet():
            return dcc.pack_folder_for_request(self.sock, row, lambda: owner, "#dccore-test", None)

    def queued(self, row):
        return any(entry is row for rows in config.dcc_queue.values() for entry in rows)

    def archives(self):
        return [name for name in os.listdir(config.TMP_ZIP_DIR) if name.lower().endswith(".rar")] \
            if os.path.isdir(config.TMP_ZIP_DIR) else []

    def test_a_poisoned_row_is_dropped_and_never_packed(self):
        row = self.make_row(self.tree.secret)
        config.dcc_queue["dave"] = [row]

        self.assertIsNone(self.pack(row))

        self.assertFalse(self.queued(row))
        self.assertEqual(self.rar_calls, [])

    def test_a_row_withdrawn_before_packing_is_not_packed(self):
        row = self.make_row(self.album)

        self.assertIs(self.pack(row), False)

        self.assertEqual(self.rar_calls, [])

    def test_a_failed_rar_leaves_no_archive(self):
        def half_written(cmd, *a, **kw):
            # rar gives up partway: a partial archive on disk, a failure code.
            self.rar_calls.append(cmd)
            with open(cmd[-2], "wb") as handle:
                handle.write(b"Rar!partial")

            class Failed:
                returncode = 1
                stdout = ""
                stderr = "disk full"
            return Failed()

        support.fake_rar_runs(self, dcc.subprocess, half_written)
        row = self.make_row(self.album)
        config.dcc_queue["dave"] = [row]

        self.assertIs(self.pack(row), False)

        self.assertEqual(len(self.rar_calls), 1)
        self.assertEqual(self.archives(), [], "the partial archive was left behind")

    def test_packed_with_no_free_slot_the_row_waits_as_the_archive(self):
        def made_it(cmd, *a, **kw):
            self.rar_calls.append(cmd)
            with open(cmd[-2], "wb") as handle:
                handle.write(b"Rar!")

            class Done:
                returncode = 0
                stdout = ""
                stderr = ""
            return Done()

        support.fake_rar_runs(self, dcc.subprocess, made_it)
        self.set_config(MAX_DCC_SLOTS=0)
        row = self.make_row(self.album)
        config.dcc_queue["dave"] = [row]

        self.assertIsNone(self.pack(row), "nothing was handed to a send")

        self.assertTrue(self.queued(row))
        self.assertTrue(row["path"].lower().endswith(".rar"), row["path"])
        self.assertTrue(os.path.exists(row["path"]))
        self.assertEqual(row["source_path"], self.album)

    def test_the_row_helper_finds_a_row_under_any_nick(self):
        row = self.make_row(self.album)
        config.dcc_queue["someone_renamed"] = [dict(row), row]
        with dcc.queue_lock:
            self.assertTrue(dcc._row_is_still_queued(row))
            self.assertFalse(dcc._row_is_still_queued(dict(row)), "by the row itself, not an equal one")


if __name__ == "__main__":
    unittest.main()
