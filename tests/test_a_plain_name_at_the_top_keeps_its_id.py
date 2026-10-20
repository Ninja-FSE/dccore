"""A fetched file whose plain name has no "." keeps its request id at the top
of FETCHED_FILES_DIR (#1423).

That is where each bot's own folder goes with FETCHED_FILES_PER_BOT on
(dcc_fetch.bot_folder_name()), and a folder name never has a dot in it. A
file at the top keeps such a name only with its id in front, so the name
stays free for that folder. Inside a bot's own folder, nothing changes: a
plain name loses its id there as before.

And when something other than a folder is already at a fetch folder's name -
a file kept there from an earlier version - the fetch fails with a reason
that names it and says what to do, and the file is left as it was.

Checked on the real code, in a temp FETCHED_FILES_DIR:

  * end to end, through handle_incoming_offer() and a real loopback
    transfer: a no-extension file "alfa" fetched with the setting off stays
    "<id>_alfa", and a later fetch from bot alfa with the setting on goes
    into FETCHED_FILES_DIR/alfa/ and completes. The same when the first file
    comes from a nick that cleans to nothing ("[]") with the setting on;
  * a real transfer whose fetch folder's name is taken by a file: the row
    fails with a reason naming that file and asking for it to be moved or
    renamed, and the file is untouched;
  * _promote_clean_filename() on its own: "alfa." and "alfa " keep their id
    at the top, "a.b" is promoted there, a no-dot name is promoted inside a
    bot's folder, and the top counts as the top however it is spelled (a
    relative FETCHED_FILES_DIR, a trailing separator).
"""

import ipaddress
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import dcc_fetch  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

RID = "0123456789ab"


def _ip_long(ip):
    return int(ipaddress.IPv4Address(ip))


class AFetchIntoTheTop(DCCoreTestCase):
    """Real sockets on 127.0.0.1, as in tests/test_dcc_fetch.py's loopback
    tests: the actual transfer loop runs, makes its folder and writes."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.make_temp_dir(prefix="dccore-top-name-"), "fetched")
        os.makedirs(self.root)
        self.set_config(FETCHED_FILES_DIR=self.root, FETCHED_FILES_PER_BOT=False)

    def fetch(self, bot, filename, payload, per_bot):
        """One fetch from `bot`, answered by a real DCC SEND over loopback.
        Returns the finished row."""
        self.set_config(FETCHED_FILES_PER_BOT=per_bot)
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        except OSError as err:
            self.skipTest(f"cannot listen on 127.0.0.1 here: {err}")
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        rid = dcc_fetch.enqueue_fetch(bot, filename)
        config.fetch_queue[rid]["state"] = "offered"
        config.fetch_queue[rid]["offered_at"] = time.time()

        def serve():
            listener.settimeout(5.0)
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        dcc_fetch.handle_incoming_offer(
            None, bot, f"DCC SEND {filename} {_ip_long('127.0.0.1')} {port} {len(payload)}")
        if config.fetch_queue[rid].get("state") == "failed":
            # Refused before it dialled: nobody will connect, so connect once
            # here to end the listener's accept() now rather than at its timeout.
            try:
                socket.create_connection(("127.0.0.1", port), timeout=2.0).close()
            except OSError:
                pass
        server.join(timeout=5.0)
        return config.fetch_queue[rid]

    def assert_complete_in(self, row, folder, payload):
        self.assertEqual(row["state"], "complete", row.get("reason"))
        path = os.path.join(self.root, folder, row["stored_filename"])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), payload)

    def test_a_plain_name_fetched_with_the_setting_off_keeps_its_id(self):
        row = self.fetch("bravo", "alfa", b"first file", per_bot=False)

        self.assertEqual(row["state"], "complete", row.get("reason"))
        self.assertNotEqual(row["stored_filename"], "alfa")
        self.assertTrue(row["stored_filename"].endswith("_alfa"), row["stored_filename"])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "alfa")))

    def test_then_a_fetch_from_bot_alfa_with_the_setting_on_completes(self):
        first = self.fetch("bravo", "alfa", b"first file", per_bot=False)
        second = self.fetch("alfa", "Track.flac", b"from alfa", per_bot=True)

        self.assert_complete_in(first, "", b"first file")
        self.assertEqual(second["fetched_folder"], "alfa")
        self.assert_complete_in(second, "alfa", b"from alfa")
        self.assertEqual(second["stored_filename"], "Track.flac")

    def test_the_same_when_the_first_file_comes_from_a_nick_with_no_usable_name(self):
        first = self.fetch("[]", "alfa", b"first file", per_bot=True)
        second = self.fetch("alfa", "Track.flac", b"from alfa", per_bot=True)

        self.assertEqual(first["state"], "complete", first.get("reason"))
        self.assertEqual(first["fetched_folder"], dcc_fetch.bot_folder_name("[]"))
        self.assertEqual(second["fetched_folder"], "alfa")
        self.assert_complete_in(second, "alfa", b"from alfa")
        # And the first file is still where its row says it is.
        with open(os.path.join(dcc_fetch.fetched_dir_of(first), first["stored_filename"]), "rb") as handle:
            self.assertEqual(handle.read(), b"first file")

    def test_a_file_where_the_folder_goes_is_named_in_the_reason(self):
        in_the_way = os.path.join(self.root, "alfa")
        with open(in_the_way, "wb") as handle:
            handle.write(b"kept from before")

        row = self.fetch("alfa", "Track.flac", b"from alfa", per_bot=True)

        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["fetched_folder"], "alfa")
        self.assertIn(in_the_way, row["reason"])
        self.assertIn("move or rename", row["reason"])
        self.assertNotIn("Errno", row["reason"])
        # The file is left exactly as it was, and nothing else was written.
        self.assertTrue(os.path.isfile(in_the_way))
        with open(in_the_way, "rb") as handle:
            self.assertEqual(handle.read(), b"kept from before")
        self.assertEqual(os.listdir(self.root), ["alfa"])

    def test_a_folder_already_there_is_used_as_before(self):
        os.makedirs(os.path.join(self.root, "alfa"))

        row = self.fetch("alfa", "Track.flac", b"from alfa", per_bot=True)

        self.assert_complete_in(row, "alfa", b"from alfa")

    def test_a_name_with_a_dot_still_loses_its_id_at_the_top(self):
        row = self.fetch("bravo", "alfa.flac", b"x" * 32, per_bot=False)

        self.assertEqual(row["stored_filename"], "alfa.flac")


class PromotionAtTheTopAndInAFolder(DCCoreTestCase):
    """_promote_clean_filename() itself, on files made in a temp tree."""

    def setUp(self):
        super().setUp()
        # The real path: on macOS the temp folder is under /var, a link to
        # /private/var, which is what os.getcwd() reports - and a relative
        # FETCHED_FILES_DIR is resolved against that (the test below).
        self.root = os.path.join(os.path.realpath(self.make_temp_dir(prefix="dccore-promote-top-")),
                                 "fetched")
        os.makedirs(self.root)
        self.set_config(FETCHED_FILES_DIR=self.root)

    def promote(self, plain, dest_dir=None, made_in=None):
        """Make "<id>_<plain>" in `made_in` (the top by default) and promote
        it, calling with `dest_dir` as given (made_in if None)."""
        made_in = made_in or self.root
        os.makedirs(made_in, exist_ok=True)
        stored = f"{RID}_{plain}"
        # Through long_path(), as the fetch writes it: Windows would drop a
        # trailing dot or space from a plain path.
        with open(platform_compat.long_path(os.path.abspath(os.path.join(made_in, stored))), "wb") as handle:
            handle.write(b"x")
        return dcc_fetch._promote_clean_filename(dest_dir if dest_dir is not None else made_in, stored)

    def test_a_name_with_no_dot_keeps_its_id_at_the_top(self):
        self.assertEqual(self.promote("alfa"), f"{RID}_alfa")
        self.assertEqual(sorted(os.listdir(self.root)), [f"{RID}_alfa"])

    @unittest.skipIf(platform_compat.IS_WINDOWS,
                     "Windows drops a trailing dot or space from a name, so no such file "
                     "can be made here; a fetched name never has one (windows_safe_name())")
    def test_a_dot_or_space_at_the_end_does_not_count_as_a_dot(self):
        for plain in ("alfa.", "alfa ", "alfa. .", "alfa.."):
            with self.subTest(plain=plain):
                self.assertEqual(self.promote(plain), f"{RID}_{plain}")
                names = os.listdir(platform_compat.long_path(self.root))
                self.assertIn(f"{RID}_{plain}", names)
                self.assertNotIn(plain, names)

    def test_a_dot_inside_the_name_is_promoted_at_the_top(self):
        self.assertEqual(self.promote("a.b"), "a.b")
        self.assertEqual(self.promote("Album 2026.rar"), "Album 2026.rar")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.b")))

    def test_a_name_with_no_dot_is_promoted_inside_a_bots_folder(self):
        folder = os.path.join(self.root, "bravo")
        for plain in ("alfa", "README"):
            with self.subTest(plain=plain):
                self.assertEqual(self.promote(plain, made_in=folder), plain)
                self.assertTrue(os.path.isfile(os.path.join(folder, plain)))

    def test_the_top_with_a_trailing_separator_is_still_the_top(self):
        self.assertEqual(self.promote("alfa", dest_dir=self.root + os.sep), f"{RID}_alfa")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "alfa")))

    def test_a_relative_fetched_files_dir_is_still_the_top(self):
        parent = os.path.dirname(self.root)
        here = os.getcwd()
        os.chdir(parent)
        self.addCleanup(os.chdir, here)
        self.set_config(FETCHED_FILES_DIR=os.path.basename(self.root))
        for dest_dir in (self.root, os.path.basename(self.root),
                         os.path.join(os.curdir, os.path.basename(self.root)) + os.sep):
            with self.subTest(dest_dir=dest_dir):
                self.assertEqual(self.promote("alfa", dest_dir=dest_dir), f"{RID}_alfa")
                self.assertFalse(os.path.lexists(os.path.join(self.root, "alfa")))
        # A bot's folder under the relative setting is still a bot's folder.
        self.assertEqual(self.promote("alfa", made_in=os.path.join(self.root, "bravo"),
                                      dest_dir=os.path.join(os.path.basename(self.root), "bravo")),
                         "alfa")


if __name__ == "__main__":
    unittest.main()
