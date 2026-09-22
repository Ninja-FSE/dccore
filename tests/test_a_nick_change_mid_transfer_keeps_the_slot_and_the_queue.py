"""#598 and #601: a /nick in the middle of a transfer, or with files queued.

irc.note_nick_change() rewrites the active_transfers row's user and moves the
in-progress lock to the new nick. start_dcc_send() then found its row and
released its lock by the nick the send STARTED as, so after a rename:

  * the row and the lock outlived the transfer for good - a slot gone until a
    restart, every later request from the renamed user answered "you already
    have a transfer", and every rehash waiting out the full transfer wait;
  * bytes_sent stopped updating on the row (the dashboard's SLOT line froze).

And for the queue (#601): note_nick_change() re-keyed dcc_queue in memory but
never saved it, so a restart before some unrelated write restored the files
under a nick that was gone; and `user_raw`, which the dispatcher addresses the
DCC offer to, kept the old nick.

The transfer half plays the receiver over a real loopback socket, the way
test_complete_means_the_receiver_acked_it.py does.
"""

import json
import os
import socket
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402



def loopback_is_usable():
    """Bind and dial for real on the range THIS file's sends use. (The shared
    check probes another range, which a bot running on the same machine may
    be holding - and then skips tests whose own ports are free.)"""
    for port in range(ack.PORT_START, ack.PORT_END + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # The ports a run has just used sit in TIME_WAIT; without this the
        # second run in a minute finds none free and skips its own tests.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("0.0.0.0", port))
            listener.listen(1)
            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            conn, _ = listener.accept()
            conn.close()
            client.close()
            return True
        except OSError:
            continue
        finally:
            listener.close()
    return False


CONTENT = ack.CONTENT
USER = ack.USER
NEW = USER + "_"


@unittest.skipUnless(loopback_is_usable(), "needs a loopback socket")
class ARenameDuringASend(ack.ARealReceiver):
    # ARealReceiver carries its own tests; a subclass would run every one of
    # them a second time. Only its receiver plumbing is wanted.
    pass


    def _rename_mid_send(self):
        config.channel_users = {"#somechannel": {USER}}
        config.user_processing_lock = {USER}
        irc_sock, sender = self.start_send()
        client = self.connect(irc_sock.port())
        irc.note_nick_change(USER, NEW)
        received = self.receive(client)
        sender.join(30)
        self.assertEqual(received, CONTENT)
        return sender

    def test_the_row_is_gone_when_the_transfer_ends(self):
        self._rename_mid_send()
        self.assertEqual(config.active_transfers, [])

    def test_the_lock_is_released_under_the_new_nick(self):
        self._rename_mid_send()
        self.assertNotIn(NEW.lower(), config.user_processing_lock)
        self.assertNotIn(USER.lower(), config.user_processing_lock)

    def test_the_renamed_user_is_not_seen_as_still_transferring(self):
        self._rename_mid_send()
        self.assertFalse(any(str(tx["user"]).lower() == NEW.lower()
                             for tx in config.active_transfers))

    def test_someone_else_holding_the_old_nick_keeps_their_own_lock(self):
        """The old nick is free after the rename; a new holder's lock is not
        the finished transfer's to release."""
        config.channel_users = {"#somechannel": {USER}}
        config.user_processing_lock = {USER}
        irc_sock, sender = self.start_send()
        client = self.connect(irc_sock.port())
        irc.note_nick_change(USER, NEW)
        config.user_processing_lock.add(USER.lower())      # a new owner of the old nick
        self.receive(client)
        sender.join(30)
        self.assertIn(USER.lower(), config.user_processing_lock)


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(ARenameDuringASend, _name, None)


class TheRowIsFoundByIdentity(DCCoreTestCase):

    def test_the_row_is_found_by_nick_and_file(self):
        row = {"user": "bob", "file": "a.flac", "bytes_sent": 0}
        other = {"user": "bob", "file": "b.flac", "bytes_sent": 0}
        self.set_config(active_transfers=[other, row])
        self.assertIs(dcc._find_transfer_row("bob", "a.flac"), row)

    def test_a_single_row_of_that_nick_is_enough(self):
        row = {"user": "bob", "file": "pack.zip", "bytes_sent": 0}
        self.set_config(active_transfers=[row])
        self.assertIs(dcc._find_transfer_row("Bob", "Some Album.zip"), row)

    def test_two_rows_and_no_file_match_is_no_guess(self):
        self.set_config(active_transfers=[{"user": "bob", "file": "a"}, {"user": "bob", "file": "b"}])
        self.assertIsNone(dcc._find_transfer_row("bob", "c"))

    def test_no_row_is_none(self):
        self.set_config(active_transfers=[])
        self.assertIsNone(dcc._find_transfer_row("bob", "a"))


class ARenameSavesTheQueue(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-nick-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._file = db.DCC_QUEUE_FILE
        db.DCC_QUEUE_FILE = os.path.join(self.tmp, "dcc_queue.txt")
        self.addCleanup(setattr, db, "DCC_QUEUE_FILE", self._file)
        config.dcc_queue.clear()
        config.dcc_queue["oldnick"] = [
            {"file": "a.flac", "path": "/x/a.flac", "channel": "#c", "user_raw": "OldNick"},
            {"file": "b.flac", "path": "/x/b.flac", "channel": "#c", "user_raw": "OldNick"},
        ]
        db.save_dcc_queue()

    def _on_disk(self):
        with open(db.DCC_QUEUE_FILE, encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_file_follows_the_rename(self):
        irc.note_nick_change("OldNick", "NewNick")
        self.assertEqual(list(self._on_disk()), ["newnick"])

    def test_a_restart_finds_the_queue_under_the_new_nick(self):
        irc.note_nick_change("OldNick", "NewNick")
        config.dcc_queue.clear()
        db.load_dcc_queue()
        self.assertEqual(list(config.dcc_queue), ["newnick"])
        self.assertEqual(len(config.dcc_queue["newnick"]), 2)

    def test_user_raw_is_the_new_nick_so_the_offer_goes_to_someone_there(self):
        irc.note_nick_change("OldNick", "NewNick")
        self.assertEqual({r["user_raw"] for r in config.dcc_queue["newnick"]}, {"NewNick"})
        self.assertEqual({r["user_raw"] for r in self._on_disk()["newnick"]}, {"NewNick"})

    def test_a_rename_that_moves_no_queue_writes_nothing(self):
        before = os.stat(db.DCC_QUEUE_FILE).st_mtime_ns
        irc.note_nick_change("someoneelse", "another")
        self.assertEqual(os.stat(db.DCC_QUEUE_FILE).st_mtime_ns, before)

    def test_an_existing_entry_under_the_new_nick_is_left_alone(self):
        config.dcc_queue["newnick"] = [{"file": "theirs.flac", "user_raw": "NewNick"}]
        irc.note_nick_change("OldNick", "NewNick")
        self.assertEqual(config.dcc_queue["newnick"][0]["file"], "theirs.flac")
        self.assertIn("oldnick", config.dcc_queue)


if __name__ == "__main__":
    unittest.main()
