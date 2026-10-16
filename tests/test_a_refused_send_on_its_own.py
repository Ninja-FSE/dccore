"""#1340: start_dcc_send()'s two refusals, called directly.

refuse_an_unsendable_file() turns away a send nobody could receive - no
public address, or an empty or vanished file - and refuse_without_a_port()
one with no DCC port or listener. Both were early exits of start_dcc_send()
until step 3 of splitting it; test_dcc_address_is_dialable.py and
test_serving_keeps_shared_archives_and_claims_straight.py still drive them
through it. Here each is handed plain values and what it released, kept
and scheduled is read back.

Every nick and file name here is invented.
"""

import contextlib
import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import transfer_log  # noqa: E402

from tests.support import DCCoreTestCase, hold_send_follow_ups, silence_debug  # noqa: E402

USER = "bravo"
ADDRESS = 134744072          # 8.8.8.8, a dialable public address


class IrcSocket:
    def __init__(self):
        self.lines = []

    def sendall(self, data):
        self.lines.append(data.decode("utf-8"))


class Listener:
    closed = False

    def close(self):
        self.closed = True


class Refusal(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        silence_debug(announce)
        self.row = {"user": USER, "file": "Some_Album.rar", "bytes_sent": 0}
        self.set_config(active_transfers=[self.row, {"user": "charlie", "file": "other.flac"}],
                        rar_inprogress=True)
        config.user_processing_lock.add(USER)
        self.addCleanup(config.user_processing_lock.discard, USER)
        self.calls = []
        self.stub(dcc, "release_queue_entry",
                  lambda user, next_file, delivered, reason="", **_: self.calls.append(("settle", delivered, reason)))
        self.stub(dcc, "_record_send_outcome",
                  lambda holder, status, identity, size, reached, nick: self.calls.append(("outcome", status)))
        self.stub(dcc, "redispatch_waiting_pack",
                  lambda irc_sock, just_finished=None: self.calls.append(("wake pack", just_finished)))
        self.stub(dcc, "check_queue_and_send", lambda irc_sock, user: self.calls.append(("wake queue", user)))
        self.stub(time, "sleep", lambda seconds: self.calls.append(("sleep", seconds)))
        self.irc = IrcSocket()

    def stub(self, module, name, value):
        real = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, real)

    def kinds(self):
        return [call[0] for call in self.calls]

    def freed(self):
        self.assertEqual(config.active_transfers, [{"user": "charlie", "file": "other.flac"}])
        self.assertNotIn(USER, config.user_processing_lock)


class AnUnsendableFile(Refusal):

    def refuse(self, ip_long, file_size=0, owns_packer=False, kind=transfer_log.KIND_FILE):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.refuse_an_unsendable_file(
                self.irc, USER, self.row, {"file": "Some_Album.rar", "user": USER},
                os.path.join("nowhere", "Some_Album.rar"), file_size, ip_long, owns_packer,
                ("k", "Some_Album.rar", kind))

    def test_without_an_address_the_queue_is_left_alone(self):
        self.refuse(ip_long=0)

        self.assertIn("no usable public address", self.irc.lines[0])
        self.freed()
        self.assertNotIn("settle", self.kinds(), "the file is not at fault")
        self.assertNotIn("wake queue", self.kinds(), "nothing can be retried until an address is set")

    def test_a_bad_file_is_charged_recorded_and_the_queue_woken(self):
        self.stub(dcc, "is_offerable_to_strangers", lambda *a: True)

        self.refuse(ip_long=ADDRESS)

        self.assertIn("file access issue or empty payload", self.irc.lines[0])
        self.freed()
        self.assertEqual(self.calls, [("settle", False, "file missing or empty"),
                                      ("outcome", transfer_log.STATUS_FAILED),
                                      ("sleep", 3.0), ("wake queue", USER)])

    def test_a_bad_list_is_recorded_as_cancelled(self):
        self.stub(dcc, "is_offerable_to_strangers", lambda *a: True)
        self.refuse(ip_long=ADDRESS, kind=transfer_log.KIND_LIST)
        self.assertIn(("outcome", transfer_log.STATUS_CANCELLED), self.calls)

    def test_the_packer_is_released_only_by_the_send_that_owns_it(self):
        self.refuse(ip_long=0, owns_packer=False)
        self.assertTrue(config.rar_inprogress)

        self.refuse(ip_long=0, owns_packer=True)
        self.assertFalse(config.rar_inprogress)
        self.assertIn(("wake pack", USER), self.calls)


class NoPort(Refusal):

    def setUp(self):
        super().setUp()
        # The retry thread is real, held inside this test as every send's
        # follow-ups are (tests/support.py): this list is what it started.
        self.held = hold_send_follow_ups(self)
        self.listener = Listener()

    def refuse(self, listener_error=None, owns_packer=False):
        with contextlib.redirect_stdout(io.StringIO()):
            dcc.refuse_without_a_port(self.irc, USER, self.row, {"file": "Some_Album.rar", "user": USER},
                                      self.listener, listener_error, owns_packer)

    def test_the_row_is_kept_uncharged_and_retried_once(self):
        self.refuse()

        self.assertIn("No available DCC ports.", self.irc.lines[0])
        self.freed()
        self.assertNotIn("settle", self.kinds(), "a busy spell is not the file's fault")
        self.assertEqual([thread._target.__name__ for thread in self.held], ["delayed_port_retry"])
        self.assertTrue(self.listener.closed)

    def test_a_listener_that_could_not_be_made_says_the_file_stays_queued(self):
        self.refuse(listener_error=OSError("too many open files"))
        self.assertIn("Your file stays queued.", self.irc.lines[0])

    def test_the_packer_is_released_only_by_the_send_that_owns_it(self):
        self.refuse(owns_packer=False)
        self.assertTrue(config.rar_inprogress)

        self.refuse(owns_packer=True)
        self.assertFalse(config.rar_inprogress)


if __name__ == "__main__":
    unittest.main()
