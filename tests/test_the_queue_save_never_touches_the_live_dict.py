"""#606: db.save_dcc_queue() popped emptied keys from the live dcc_queue
without queue_lock, and the RuntimeError that raised in another thread
escaped start_dcc_send's finally.

Two of save_dcc_queue()'s callers run after their `with queue_lock:` block
has closed (release_queue_entry on every completion, and the poisoned-entry
branch of check_queue_and_send). Its pop of an emptied user key therefore
raced the lock-held live walks of config.dcc_queue.items() in
next_waiting_pack_owner() and in start_dcc_send's temp-archive cleanup, and
"dictionary changed size during iteration" was raised in THEIR thread.

In start_dcc_send's finally the redispatch_waiting_pack() call was the one
step with no guard of its own, so that RuntimeError skipped
user_processing_lock.discard() and the delayed fallback trigger: the user
whose pack had just finished stayed "already claimed elsewhere" until a
rehash, and none of their later requests were dispatched.

The fix has two halves, tested separately:
  * save_dcc_queue() only reads the live dict; an emptied key is dropped by
    release_queue_entry() itself, inside its own queue_lock block;
  * the wake in the finally is wrapped like every other step there.
"""

import ast
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase, no_disk_writes, queue_row, silence_debug  # noqa: E402
from tests import test_complete_means_the_receiver_acked_it as ack  # noqa: E402


class TheSaveOnlyReadsTheLiveDict(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-606-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._real_file = db.DCC_QUEUE_FILE
        db.DCC_QUEUE_FILE = os.path.join(self.tmp, "dcc_queue.txt")
        self.addCleanup(setattr, db, "DCC_QUEUE_FILE", self._real_file)

    def save_quietly(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            db.save_dcc_queue()
        return buffer.getvalue()

    def test_a_save_during_a_live_walk_does_not_raise(self):
        """The race, made deterministic: the walk next_waiting_pack_owner()
        and the temp-archive cleanup do under queue_lock, with a save landing
        between two of its steps. The old pop changed the dict's size and the
        walk's next step raised."""
        config.dcc_queue["someone"] = []
        config.dcc_queue["somebody"] = [queue_row(user="somebody")]
        config.dcc_queue["somebodyelse"] = [queue_row(user="somebodyelse")]

        walked = []
        for user_key, entries in config.dcc_queue.items():
            walked.append(user_key)
            self.save_quietly()

        self.assertEqual(walked, ["someone", "somebody", "somebodyelse"])

    def test_an_emptied_key_stays_in_memory_and_leaves_the_file(self):
        config.dcc_queue["someone"] = []
        config.dcc_queue["somebody"] = [queue_row(user="somebody")]

        output = self.save_quietly()

        self.assertIn("someone", config.dcc_queue, "the save must not mutate the live dict")
        with io.open(db.DCC_QUEUE_FILE, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(sorted(on_disk), ["somebody"])
        self.assertNotIn("ERROR", output)


class ReleaseDropsTheEmptiedKeyItself(DCCoreTestCase):
    """The pruning moved from the save to the settlement, under queue_lock.

    save_dcc_queue() is a no-op here, so any key that goes away went away in
    release_queue_entry() - which is where the old code left an empty list
    and relied on the save to clean it up."""

    def setUp(self):
        super().setUp()
        no_disk_writes(db)
        silence_debug(announce)

    def settle(self, user, row, delivered):
        with contextlib.redirect_stdout(io.StringIO()):
            return dcc.release_queue_entry(user, row, delivered=delivered, reason="test")

    def test_the_last_delivered_row_takes_its_key_with_it(self):
        row = queue_row(user="someone", filename="Last.flac")
        config.dcc_queue["someone"] = [row]
        config.dcc_queue["somebody"] = [queue_row(user="somebody")]

        self.settle("someone", row, delivered=True)

        self.assertNotIn("someone", config.dcc_queue)
        self.assertIn("somebody", config.dcc_queue, "only the emptied queue goes")

    def test_a_queue_with_rows_left_keeps_its_key(self):
        sent = queue_row(user="someone", filename="Sent.flac")
        left = queue_row(user="someone", filename="Left.flac")
        config.dcc_queue["someone"] = [sent, left]

        self.settle("someone", sent, delivered=True)

        self.assertEqual(config.dcc_queue["someone"], [left])

    def test_a_row_settled_under_a_renamed_key_takes_that_key_with_it(self):
        """The #455 fallback - the row moved to another key mid-send - prunes
        the key it actually found the row under."""
        row = queue_row(user="someone", filename="Last.flac")
        config.dcc_queue["someone_"] = [row]

        self.settle("someone", row, delivered=True)

        self.assertNotIn("someone_", config.dcc_queue)
        self.assertNotIn("someone", config.dcc_queue)

    def test_giving_up_on_the_last_row_takes_its_key_with_it(self):
        original = getattr(config, "MAX_SEND_FAILS", 3)
        config.MAX_SEND_FAILS = 1
        self.addCleanup(setattr, config, "MAX_SEND_FAILS", original)
        row = queue_row(user="someone", filename="Broken.flac")
        config.dcc_queue["someone"] = [row]

        self.settle("someone", row, delivered=False)

        self.assertNotIn("someone", config.dcc_queue)


class TheWakeInTheFinallyIsGuarded(unittest.TestCase):
    """Read from the source, the way the audit had to: the one
    redispatch_waiting_pack() call inside start_dcc_send's finally sits in a
    try/except of its own, and the lock release comes after it. The loopback
    test below drives the real thing where it can; this one runs everywhere."""

    def finally_body(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        func = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "start_dcc_send")
        # The outermost try/finally of the function body is the settlement.
        tries = [node for node in func.body if isinstance(node, ast.Try) and node.finalbody]
        self.assertEqual(len(tries), 1, "start_dcc_send has one try/finally at its top level")
        return tries[0].finalbody

    @staticmethod
    def calls_named(nodes, name):
        return [sub for node in nodes for sub in ast.walk(node)
                if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == name]

    def test_the_wake_sits_inside_its_own_try(self):
        body = self.finally_body()
        wakes = self.calls_named(body, "redispatch_waiting_pack")
        self.assertEqual(len(wakes), 1, "one wake in the finally")
        wake = wakes[0]
        # A Try WITH handlers whose try-body contains the call. A bare
        # try/finally around it would not stop the exception.
        guarded = any(isinstance(node, ast.Try) and node.handlers
                      and any(wake is sub for stmt in node.body for sub in ast.walk(stmt))
                      for top in body for node in ast.walk(top))
        self.assertTrue(guarded, "redispatch_waiting_pack() in start_dcc_send's finally "
                                 "must be wrapped in try/except, or an exception there "
                                 "skips the user_processing_lock release (#606)")

    def test_the_lock_release_comes_after_the_wake(self):
        body = self.finally_body()
        wake = self.calls_named(body, "redispatch_waiting_pack")[0]
        discards = [node for top in body for node in ast.walk(top)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "discard"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "user_processing_lock"]
        self.assertTrue(discards, "the finally releases user_processing_lock")
        self.assertTrue(all(d.lineno > wake.lineno for d in discards),
                        "the release is what the guard protects; it comes after the wake")


def loopback_is_usable():
    """Bind and dial for real on the range THIS file's sends use."""
    for port in range(ack.PORT_START, ack.PORT_END + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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


USER = ack.USER
CONTENT = ack.CONTENT


@unittest.skipUnless(loopback_is_usable(), "needs a loopback socket")
class AFailedWakeStillReleasesTheUser(ack.ARealReceiver):
    """A real pack send over loopback whose wake blows up in the finally.

    Skipped where loopback is not available; TheWakeInTheFinallyIsGuarded
    above is the half that runs everywhere."""

    def start_pack_send(self):
        irc = ack.RecordingIrcSocket()
        self.oserve.irc_connection = irc
        # A temp-archive row is what makes the finally release rar_inprogress
        # and wake the next pack; the file itself lives outside TMP_ZIP_DIR
        # so the cleanup step leaves it alone.
        row = {"file": "Some_Album.zip", "path": self.served, "channel": "#somechannel",
               "user_raw": USER, "is_temporary_zip": True}
        sender = threading.Thread(
            target=dcc.start_dcc_send,
            args=(irc, USER, self.served, "Some_Album.zip", "#somechannel", row),
            daemon=True)
        sender.start()
        self.addCleanup(sender.join, 30)
        self.assertTrue(irc.handshake_seen.wait(20), "no DCC SEND handshake")
        return irc, sender

    def _send_with_a_broken_wake(self):
        config.user_processing_lock = {USER}
        config.rar_inprogress = True

        def broken_wake(irc_sock, just_finished=None):
            raise RuntimeError("dictionary changed size during iteration")
        real_wake = dcc.redispatch_waiting_pack
        dcc.redispatch_waiting_pack = broken_wake
        self.addCleanup(setattr, dcc, "redispatch_waiting_pack", real_wake)

        self.fallback_fired = threading.Event()
        self.fallback_args = []
        real_trigger = dcc.check_queue_and_send

        def recording_trigger(irc_sock, completed_user):
            # Only THIS test's user sets the event. Every completed send in
            # the suite starts a delayed_queue_trigger_fallback thread that
            # calls dcc.check_queue_and_send 3 s (or 15 s x fails) later,
            # through the module attribute - so one from an earlier test can
            # land here while this stub is installed, and did (#642:
            # ['dave'] != ['someuser'] in preflight's hostile pass, where
            # the test order differs). Recorded, not counted.
            self.fallback_args.append(completed_user)
            if completed_user == USER:
                self.fallback_fired.set()
        dcc.check_queue_and_send = recording_trigger
        self.addCleanup(setattr, dcc, "check_queue_and_send", real_trigger)

        irc, sender = self.start_pack_send()
        client = self.connect(irc.port())
        received = self.receive(client)
        sender.join(30)
        self.assertEqual(received, CONTENT)
        self.assertFalse(sender.is_alive(), "the send thread must have finished")

    def test_the_user_is_no_longer_claimed(self):
        self._send_with_a_broken_wake()
        self.assertNotIn(USER.lower(), config.user_processing_lock)

    def test_the_fallback_trigger_still_fires(self):
        self._send_with_a_broken_wake()
        self.assertTrue(self.fallback_fired.wait(15), "the delayed queue trigger never ran")
        self.assertIn(USER, self.fallback_args)

    def test_the_pack_interlock_is_released(self):
        self._send_with_a_broken_wake()
        self.assertFalse(config.rar_inprogress)


for _name in [n for n in dir(ack.ARealReceiver) if n.startswith("test")]:
    setattr(AFailedWakeStillReleasesTheUser, _name, None)


if __name__ == "__main__":
    unittest.main()
