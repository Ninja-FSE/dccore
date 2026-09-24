"""The person downloading is told what went wrong on their side (#884).

#879 fixed the operator's line: an accept() timeout says the receiver
never connected, not that a send stalled. The user still got "Could not
send <file> (transfer did not complete)" - no cause, no action - for a
failure that is theirs to fix. An operator's user put it exactly: "it says
active transfer started then gives error", and could take a .jpg but never
a .nfo, which is a client-side DCC ignore list almost every time.

The notice now names the cause and the two things to try, and it goes out
through fit_irc_line() so a long filename cannot push the advice off the
end of a 512-byte line.
"""

import io
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase, no_disk_writes  # noqa: E402
from tests.test_a_receiver_that_never_connects_is_said_so import _NobodyEverConnects  # noqa: E402
from tests.test_dcc_resume_end_to_end import RecordingIrcSocket  # noqa: E402

USER = "ivo"
CONTENT = b"[info]\n" * 40


class TheAdvice(unittest.TestCase):

    def test_it_names_the_cause_and_the_two_things_to_try(self):
        advice = dcc.never_connected_advice()
        self.assertIn("never accepted it", advice)
        self.assertIn("DCC prompt", advice)
        self.assertIn("ignore this kind of file", advice)


class WhatTheUserIsTold(DCCoreTestCase):
    """Through the real send path, with nobody on the other end."""

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        config.dcc_queue.clear()
        self.tmp = tempfile.mkdtemp(prefix="dccore-told-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.set_config(active_transfers=[], MAX_DCC_SLOTS=3, MY_IP_OR_DOCK="8.8.8.8",
                        DCC_PORT_START=51340, DCC_PORT_END=51350, DCC_ACCEPT_TIMEOUT=30)
        runtime.dcc_send_offers.clear()
        self.addCleanup(runtime.dcc_send_offers.clear)
        self.sent = []
        patch = mock.patch.object(self.oserve, "queue_message",
                                  lambda user, text, *a, **k: self.sent.append((user, text)))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(announce, "feed_event", lambda *a, **k: None)
        patch.start()
        self.addCleanup(patch.stop)

    def offer(self, name):
        path = os.path.join(self.tmp, name)
        with io.open(path, "wb") as handle:
            handle.write(CONTENT)
        row = {"file": name, "path": path, "channel": "#somechannel", "user_raw": USER}
        config.active_transfers[:] = [{"user": USER, "file": name, "bytes_sent": 0,
                                       "next_file_obj": row}]
        irc = RecordingIrcSocket()
        self.oserve.irc_connection = irc
        with mock.patch("socket.socket", _NobodyEverConnects), \
                mock.patch("time.sleep", lambda *_a, **_k: None):
            dcc.start_dcc_send(irc, USER, path, name, "#somechannel", row)
        return [text for who, text in self.sent if who == USER]

    def test_the_notice_says_their_client_never_accepted_it(self):
        notices = self.offer("00-Some_Release-ELITE.nfo")

        self.assertEqual(len(notices), 1, notices)
        notice = notices[0]
        self.assertIn("Could not send 00-Some_Release-ELITE.nfo", notice)
        self.assertIn("your client never accepted it", notice)
        self.assertIn("DCC prompt", notice)
        self.assertIn("ignore this kind of file", notice)
        self.assertNotIn("transfer did not complete", notice)

    def test_it_still_says_what_to_do_next(self):
        self.assertIn("Ask for it again when you are ready",
                      self.offer("00-Some_Release-ELITE.nfo")[0])


class ALineTooLongForTheServer(DCCoreTestCase):
    """The notice is built in release_queue_entry, and a name long enough to
    overflow a 512-byte line is longer than Windows will create on disk - so
    this drives the settling directly, as the send path does."""

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        config.dcc_queue.clear()
        self.sent = []
        patch = mock.patch.object(self.oserve, "queue_message",
                                  lambda user, text, *a, **k: self.sent.append((user, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_name_is_shortened_and_the_advice_survives(self):
        name = "00-Some_Very_Long_Release_Name-" + "x" * 420 + ".nfo"
        entry = {"file": name, "path": "/music/" + name, "channel": "#c", "user_raw": USER}

        with mock.patch("builtins.print"):
            dcc.release_queue_entry(USER, entry, delivered=False,
                                    reason=dcc.never_connected_advice())

        notice = self.sent[0][1]
        self.assertLessEqual(len(notice.encode("utf-8")), announce.IRC_LINE_BUDGET)
        self.assertIn("your client never accepted it", notice)
        self.assertIn("Ask for it again when you are ready", notice)
        self.assertIn("...", notice, "the name was shortened, not the advice")


class EveryOtherFailureIsUnchanged(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        config.dcc_queue.clear()
        self.sent = []
        patch = mock.patch.object(self.oserve, "queue_message",
                                  lambda user, text, *a, **k: self.sent.append((user, text)))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_failure_that_is_not_the_accept_window_reads_as_before(self):
        entry = {"file": "Track.flac", "path": "/music/Track.flac", "channel": "#c",
                 "user_raw": USER}
        with mock.patch("builtins.print"):
            dcc.release_queue_entry(USER, entry, delivered=False,
                                    reason="transfer did not complete")

        self.assertEqual(len(self.sent), 1)
        notice = self.sent[0][1]
        self.assertIn("Could not send Track.flac", notice)
        self.assertIn("transfer did not complete", notice)
        self.assertNotIn("DCC prompt", notice)


if __name__ == "__main__":
    unittest.main()
