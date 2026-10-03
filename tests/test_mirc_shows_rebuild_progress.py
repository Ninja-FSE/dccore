"""The @DCCore window shows how far a list rebuild has got (#1024).

`update` typed in the window said "this can take minutes" and then nothing,
although the rebuild writes its progress all the time. While one runs, the bot
now sends `DCCORE REBUILD <phase> <folder_index> <folder_count> <files>
<elapsed>` - in the status burst and every few seconds between - and one
`DCCORE REBUILD end` when it stops. Only to a script that can draw it.
"""

import io
import json
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
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class TheRebuildLine(DCCoreTestCase):

    def running(self, **progress):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        if progress:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(dict(progress, at=time.time(), started_at=time.time() - 75), fh)
        else:
            os.remove(path)
            self.addCleanup(lambda: None)
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE=path)

    def test_nothing_while_no_rebuild_runs(self):
        self.assertEqual(adminchat.rebuild_lines(), [])

    def test_a_running_rebuild_says_phase_folder_files_and_time(self):
        self.running(phase="scanning", folder="Music", folder_index=7, folder_count=20, files=312000)
        (line,) = adminchat.rebuild_lines()
        parts = line.split(" ")
        self.assertEqual(parts[:6], ["DCCORE", "REBUILD", "scanning", "7", "20", "312000"])
        self.assertIn(int(parts[6]), range(74, 80))

    def test_before_the_first_progress_write_it_says_starting(self):
        self.running()
        self.assertEqual(adminchat.rebuild_lines(), ["DCCORE REBUILD starting 0 0 0 0"])

    def test_a_phase_cannot_break_the_line(self):
        self.running(phase="a b\r\nDCCORE TAKEN x", folder_count=1)
        (line,) = adminchat.rebuild_lines()
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertEqual(len(line.split(" ")), 7)

    def test_the_status_burst_carries_it_only_when_asked(self):
        self.running(phase="audio", folder_index=3, folder_count=4, files=10)
        self.assertFalse([l for l in adminchat.status_lines() if l.startswith("DCCORE REBUILD")])
        self.assertTrue([l for l in adminchat.status_lines(rebuild=True) if l.startswith("DCCORE REBUILD")])

    def test_the_burst_of_a_quiet_bot_carries_none(self):
        self.assertFalse([l for l in adminchat.status_lines(rebuild=True) if l.startswith("DCCORE REBUILD")])


class OnlyAScriptThatCanDrawIt(DCCoreTestCase):

    def test_the_versions(self):
        self.assertFalse(adminchat.script_draws_rebuild("1.8"))
        self.assertFalse(adminchat.script_draws_rebuild(""))
        self.assertFalse(adminchat.script_draws_rebuild("garbage"))
        self.assertTrue(adminchat.script_draws_rebuild("1.9"))
        self.assertTrue(adminchat.script_draws_rebuild("1.10"))

    def test_hello_records_it(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        session.authenticated = True
        adminchat._cmd_hello(session, "dccore.mrc 1.9")
        self.assertTrue(session.draws_rebuild)
        adminchat._cmd_hello(session, "dccore.mrc 1.8")
        self.assertFalse(session.draws_rebuild)


class BetweenBursts(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        self.session = adminchat.Session(near, "192.0.2.1", "Op", "op.example")
        self.session.authenticated = True
        self.session.structured = True
        self.session.draws_rebuild = True
        self.sent = []
        self.session.send = self.sent.append

    def test_a_line_goes_once_the_interval_has_passed_while_a_rebuild_runs(self):
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session._rebuild_sent_at = time.time()
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent, [], "not before the interval")
        self.session._rebuild_sent_at = time.time() - adminchat.REBUILD_INTERVAL - 1
        self.session.send_rebuild_progress()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("DCCORE REBUILD starting"))

    def test_one_end_when_it_stops_and_then_silence(self):
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        config.update_inprogress = False
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent[-1], "DCCORE REBUILD end")
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(len(self.sent), 2)

    def test_a_quiet_bot_sends_nothing_at_all(self):
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent, [])

    def test_a_script_that_cannot_draw_it_is_sent_nothing(self):
        self.set_config(update_inprogress=True, LIST_PROGRESS_FILE="/nonexistent/progress.json")
        self.session.draws_rebuild = False
        self.session._rebuild_sent_at = 0.0
        self.session.send_rebuild_progress()
        self.assertEqual(self.sent, [])


class TheScript(unittest.TestCase):

    def setUp(self):
        self.text = read("scripts", "mirc", "dccore.mrc")

    def test_the_version_is_the_one_the_bot_gates_on(self):
        match = re.search(r"alias dccore\.ver \{ return ([0-9.]+) \}", self.text)
        self.assertGreaterEqual(tuple(int(p) for p in match.group(1).split(".")),
                                tuple(int(p) for p in adminchat.REBUILD_SCRIPT_VERSION.split(".")))

    def test_a_rebuild_line_is_stored_and_end_clears_it(self):
        body = self.text[self.text.index("if (%type == REBUILD) {"):]
        body = body[:body.index("\n  }")]
        self.assertIn("hadd dccore.live rebuild $2-", body)
        self.assertIn("hdel dccore.live rebuild", body)
        self.assertIn("dccore.panel.soon", body)
        self.assertIn("dccore.title", body)

    def test_each_status_burst_forgets_it_so_a_missed_end_lasts_one_burst(self):
        status = self.text[self.text.index("alias dccore.status {"):]
        status = status[:status.index("\n}")]
        self.assertIn("hdel dccore.live rebuild", status)

    def test_the_panel_has_a_rebuilding_section_before_the_queue(self):
        panel = self.text[self.text.index("alias dccore.panel {"):]
        panel = panel[:panel.index("\n}")]
        self.assertIn("Rebuilding", panel)
        self.assertIn("if ($dccore.st(rebuild) != $null)", panel)
        self.assertLess(panel.index("Sending"), panel.index("Rebuilding"))
        self.assertLess(panel.index("Rebuilding"), panel.index("aline -l %head $dccore.win Queue"))

    def test_the_title_bar_says_it_when_the_panel_is_off(self):
        title = self.text[self.text.index("alias dccore.title {"):]
        title = title[:title.index("\n}")]
        self.assertIn("dccore.rebuild.short", title)
        self.assertIn("!$dccore.opt(panel)", title)

    def test_the_thousands_separator_needs_no_dollar_bytes(self):
        """$bytes().suffix gave no unit on a real run (see dccore.bytes)."""
        rebuilding = self.text[self.text.index("Rebuilding (#1024)"):]
        rebuilding = rebuilding[:rebuilding.index("aline -l %head $dccore.win Queue")]
        self.assertNotIn("$bytes(", rebuilding)
        self.assertIn("$dccore.num(", rebuilding)


if __name__ == "__main__":
    unittest.main()
