"""A running folder pack shows on every operator surface (#1202).

dcc.pack_status() / dcc.cancel_pack() are tested in
test_a_running_pack_can_be_seen_and_cancelled.py with a real process. Here a
job is put on runtime.pack_job by hand and each surface is read: the
dashboard's Queue payload and cancel route, the admin console commands, the
`DCCORE PACKING` line of the status burst and between bursts, the script
version gate, and the mIRC script's own handling of the line.
"""

import io
import os
import re
import socket
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import announce  # noqa: E402
import dcc  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class WithARunningJob(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        handle, self.archive = tempfile.mkstemp(suffix=".rar")
        os.write(handle, b"x" * 2048)
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(self.archive) and os.remove(self.archive))
        self.job = {"user": "Dave", "name": "Some Album", "archive": self.archive,
                    "started": time.time() - 30, "total": 10240, "process": None,
                    "cancelled": False, "kill_timer": None}
        self.addCleanup(setattr, runtime, "pack_job", runtime.pack_job)
        runtime.pack_job = self.job


class TheQueuePayload(WithARunningJob):

    def test_the_user_being_packed_for_reads_packing_not_queued(self):
        row = [r for r in webserver.build_queue_payload() if r["user"] == "dave"][0]
        self.assertEqual(row["status"], "packing")
        self.assertEqual(row["pack"]["name"], "Some Album")
        self.assertEqual(row["pack"]["done"], 2048)
        self.assertEqual(row["pack"]["total"], 10240)
        self.assertIn(row["pack"]["elapsed"], range(29, 35))
        self.assertFalse(row["pack"]["cancelling"])

    def test_a_single_user_reads_it_too(self):
        row = webserver.build_queue_payload("dave")
        self.assertEqual(row["status"], "packing")
        self.assertEqual(row["pack"]["name"], "Some Album")

    def test_nobody_packing_means_no_pack_field(self):
        runtime.pack_job = None
        for row in webserver.build_queue_payload():
            self.assertNotIn("pack", row)

    def test_the_payload_never_holds_a_path(self):
        self.assertNotIn(self.archive, repr(webserver.build_queue_payload()))


class TheCancelRoute(WithARunningJob):

    def test_it_marks_the_job_and_says_who_and_what(self):
        status, body = webserver.build_pack_cancel_result()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"cancelled": {"user": "Dave", "name": "Some Album"}})
        self.assertTrue(self.job["cancelled"])

    def test_it_is_404_when_nothing_is_packing(self):
        runtime.pack_job = None
        status, body = webserver.build_pack_cancel_result()
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_it_is_a_post_route_behind_the_login(self):
        if not webserver.HAVE_FLASK:
            self.skipTest("Flask not installed")
        client = webserver.create_app().test_client()
        self.assertIn(client.post("/api/queue/pack/cancel").status_code, (302, 401, 403))
        self.assertFalse(self.job["cancelled"], "an unauthenticated POST must not cancel")


class TheAdminConsole(WithARunningJob):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.sent = []
        self.session.send = self.sent.append

    def test_the_commands_are_listed(self):
        self.assertIn("packing", adminchat.COMMANDS)
        self.assertIn("packcancel", adminchat.COMMANDS)

    def test_packing_says_who_and_which_folder(self):
        adminchat._cmd_packing(self.session, "")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Some Album", self.sent[0])
        self.assertIn("Dave", self.sent[0])
        self.assertNotIn(self.archive, self.sent[0])

    def test_packing_with_nothing_running(self):
        runtime.pack_job = None
        adminchat._cmd_packing(self.session, "")
        self.assertEqual(self.sent, ["Nothing is being packed."])

    def test_packcancel_cancels_and_says_so(self):
        adminchat._cmd_packcancel(self.session, "")
        self.assertTrue(self.job["cancelled"])
        self.assertIn("Some Album", self.sent[0])

    def test_packcancel_with_nothing_running(self):
        runtime.pack_job = None
        adminchat._cmd_packcancel(self.session, "")
        self.assertEqual(self.sent, ["Nothing is being packed."])


class ThePackingLine(WithARunningJob):

    def test_the_line_says_user_done_total_elapsed_and_folder_last(self):
        (line,) = adminchat.packing_lines()
        parts = line.split(" ", 6)
        self.assertEqual(parts[:2], ["DCCORE", "PACKING"])
        self.assertEqual(parts[2], "Dave")
        self.assertEqual(parts[3:5], ["2048", "10240"])
        self.assertIn(int(parts[5]), range(29, 35))
        self.assertEqual(parts[6], "Some Album")

    def test_a_folder_name_cannot_break_the_line(self):
        self.job["name"] = "a\r\nDCCORE TAKEN x"
        (line,) = adminchat.packing_lines()
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)

    def test_nothing_while_none_runs(self):
        runtime.pack_job = None
        self.assertEqual(adminchat.packing_lines(), [])

    def test_the_burst_carries_it_only_when_asked(self):
        self.assertFalse([l for l in adminchat.status_lines() if l.startswith("DCCORE PACKING")])
        self.assertTrue([l for l in adminchat.status_lines(packing=True)
                         if l.startswith("DCCORE PACKING")])

    def test_the_burst_of_a_quiet_bot_carries_none(self):
        runtime.pack_job = None
        self.assertFalse([l for l in adminchat.status_lines(packing=True)
                          if l.startswith("DCCORE PACKING")])


class OnlyAScriptThatCanDrawIt(DCCoreTestCase):

    def test_the_versions(self):
        self.assertFalse(adminchat.script_draws_packing("1.13"))
        self.assertFalse(adminchat.script_draws_packing(""))
        self.assertFalse(adminchat.script_draws_packing("garbage"))
        self.assertTrue(adminchat.script_draws_packing("1.14"))
        self.assertTrue(adminchat.script_draws_packing("1.15"))

    def test_hello_records_it(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        session.authenticated = True
        adminchat._cmd_hello(session, "dccore.mrc 1.14")
        self.assertTrue(session.draws_packing)
        adminchat._cmd_hello(session, "dccore.mrc 1.13")
        self.assertFalse(session.draws_packing)


class BetweenBursts(WithARunningJob):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.session.structured = True
        self.session.draws_packing = True
        self.sent = []
        self.session.send = self.sent.append

    def test_a_line_goes_once_the_interval_has_passed(self):
        self.session._packing_sent_at = time.time()
        self.session.send_packing_progress()
        self.assertEqual(self.sent, [], "not before the interval")
        self.session._packing_sent_at = time.time() - adminchat.PACKING_INTERVAL - 1
        self.session.send_packing_progress()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("DCCORE PACKING Dave"))

    def test_one_end_when_it_stops_and_then_silence(self):
        self.session._packing_sent_at = 0.0
        self.session.send_packing_progress()
        runtime.pack_job = None
        self.session._packing_sent_at = 0.0
        self.session.send_packing_progress()
        self.assertEqual(self.sent[-1], "DCCORE PACKING end")
        self.session._packing_sent_at = 0.0
        self.session.send_packing_progress()
        self.assertEqual(len(self.sent), 2)

    def test_a_quiet_bot_sends_nothing(self):
        runtime.pack_job = None
        self.session._packing_sent_at = 0.0
        self.session.send_packing_progress()
        self.assertEqual(self.sent, [])

    def test_a_script_that_cannot_draw_it_gets_nothing(self):
        self.session.draws_packing = False
        self.session._packing_sent_at = 0.0
        self.session.send_packing_progress()
        self.assertEqual(self.sent, [])


class TheScriptAndTheFeed(unittest.TestCase):

    def test_the_script_version_is_at_least_the_one_the_bot_gates_on(self):
        script = read("scripts", "mirc", "dccore.mrc")
        self.assertTrue(adminchat.script_draws_dlqueue(
            re.search(r"alias dccore\.ver \{ return ([\d.]+) \}", script).group(1)))

    def test_the_script_handles_and_clears_the_line(self):
        script = read("scripts", "mirc", "dccore.mrc")
        self.assertIn("if (%type == PACKING)", script)
        self.assertIn("hdel dccore.live packing", script)
        self.assertIn("dccore.send packcancel", script)

    def test_pack_events_go_to_the_feed_only(self):
        self.assertIn("PACK", announce.FEED_ONLY_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
