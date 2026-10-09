"""The Download queues window (#1217).

What the bot has asked other bots for and that has not started yet - files,
!rar folders and lists - in a popup of its own, each one removable. The bot
sends a whole snapshot (`DCCORE DQBEGIN`, a `DQROW` per request, `DQEND`) when
asked with `dlqueue`; Remove is `dlcancel <id>...` / `dlcancel all`, and a
download that has started is left alone.
"""

import io
import os
import re
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_dccore_downloads_window import make_session, row  # noqa: E402


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def dqrows(lines):
    return [l.split(" ", 8) for l in lines if l.startswith("DCCORE DQROW ")]


class TheSnapshot(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()

    def test_it_is_framed_by_begin_and_end_with_the_count(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        config.fetch_queue["bbbbbbbbbbbb"] = row("queued", queue_position=2)
        lines = adminchat.dlqueue_lines(adminchat.dlqueue_requests())
        self.assertEqual(lines[0], "DCCORE DQBEGIN")
        self.assertEqual(lines[-1], "DCCORE DQEND 2")

    def test_nothing_waiting_is_still_a_whole_snapshot(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("receiving")
        config.fetch_queue["bbbbbbbbbbbb"] = row("complete")
        self.assertEqual(adminchat.dlqueue_lines(adminchat.dlqueue_requests()),
                         ["DCCORE DQBEGIN", "DCCORE DQEND 0"])

    def test_only_what_has_not_started_is_in_it(self):
        for n, state in enumerate(("pending", "offered", "queued", "listening", "receiving", "complete", "failed")):
            config.fetch_queue[f"{n:012d}"] = row(state, name=f"{state}.flac", at=float(n))
        rows = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        self.assertEqual([r[4] for r in rows], ["pending", "offered", "queued"])

    def test_a_file_a_folder_and_a_list_say_which_they_are(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending", at=1.0)
        config.fetch_queue["bbbbbbbbbbbb"] = row("pending", at=2.0, request_type="folder",
                                                filename="!rar Some Folder", requested_filename="!rar Some Folder")
        config.fetch_queue["cccccccccccc"] = row("pending", at=3.0, request_type="list",
                                                filename="list.txt", requested_filename="list.txt")
        rows = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        self.assertEqual([r[3] for r in rows], ["f", "r", "l"])

    def test_a_row_carries_id_bot_why_and_the_name_whole(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("queued", bot="Some|Bot", name="a b.flac", queue_position=3)
        (r,) = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        self.assertEqual(r[2], "aaaaaaaaaaaa")
        self.assertEqual(r[5], "Some|Bot")
        self.assertNotIn(" ", r[6])
        self.assertEqual(r[7] + " " + r[8] if len(r) > 8 else r[7], "a b.flac")

    def test_the_rows_of_one_bot_sit_together(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending", bot="BotA", at=1.0)
        config.fetch_queue["bbbbbbbbbbbb"] = row("pending", bot="BotB", at=2.0)
        config.fetch_queue["cccccccccccc"] = row("pending", bot="BotA", at=3.0)
        rows = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        bots = [r[5] for r in rows]
        self.assertEqual(bots, sorted(bots, key=bots.index))
        self.assertEqual(bots.count("BotA"), 2)
        self.assertEqual(bots[0], bots[1])


class TheCap(DCCoreTestCase):
    """DOWNLOADS_WAITING_MAX rows per snapshot (#1246 review): the session's
    outgoing buffer holds only 500 lines and drops the OLDEST once full, so
    with no cap a big enough queue could push DQBEGIN itself out before the
    client ever saw it. DQEND still reports the true, uncapped count - the
    same split the Downloads window's own DLEND already makes for its
    "waiting" rows."""

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        for n in range(adminchat.DOWNLOADS_WAITING_MAX + 5):
            config.fetch_queue[f"{n:012d}"] = row("pending", name=f"{n}.flac", at=float(n))

    def test_only_the_cap_is_drawn(self):
        rows = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        self.assertEqual(len(rows), adminchat.DOWNLOADS_WAITING_MAX)

    def test_dqend_still_carries_the_real_total(self):
        lines = adminchat.dlqueue_lines(adminchat.dlqueue_requests())
        self.assertEqual(lines[-1], f"DCCORE DQEND {adminchat.DOWNLOADS_WAITING_MAX + 5}")

    def test_under_the_cap_nothing_is_left_out(self):
        config.fetch_queue.clear()
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        rows = dqrows(adminchat.dlqueue_lines(adminchat.dlqueue_requests()))
        self.assertEqual(len(rows), 1)


class TheCommand(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.session = make_session(self)
        self.session.draws_dlqueue = True
        persist = dcc_fetch.persist_fetch_history
        dcc_fetch.persist_fetch_history = lambda: None
        self.addCleanup(setattr, dcc_fetch, "persist_fetch_history", persist)

    def test_a_script_that_can_draw_it_gets_the_snapshot(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        adminchat._cmd_dlqueue(self.session, "")
        self.assertEqual(self.session.sent[0], "DCCORE DQBEGIN")
        self.assertEqual(self.session.sent[-1], "DCCORE DQEND 1")

    def test_a_script_that_cannot_gets_the_ids_as_text(self):
        self.session.draws_dlqueue = False
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        adminchat._cmd_dlqueue(self.session, "")
        self.assertFalse(any(l.startswith("DCCORE ") for l in self.session.sent))
        text = "\n".join(self.session.sent)
        self.assertIn("aaaaaaaaaaaa", text)
        self.assertIn("dlcancel", text)

    def test_nothing_waiting_is_said_in_text(self):
        self.session.structured = False
        adminchat._cmd_dlqueue(self.session, "")
        self.assertEqual(self.session.sent, ["No request is waiting."])

    def test_the_command_is_listed_and_dlcancel_takes_many(self):
        self.assertIn("dlqueue", adminchat.COMMANDS)
        self.assertIn("all", adminchat.COMMANDS["dlcancel"][2])

    def test_the_text_path_is_capped_too_with_a_count_of_the_rest(self):
        """Capped the same way dlqueue_lines() is (#1246 review): one line
        per row with no cap could push the header line itself out of the
        session's own 500-line outgoing buffer on a big enough queue."""
        self.session.draws_dlqueue = False
        for n in range(adminchat.DOWNLOADS_WAITING_MAX + 5):
            config.fetch_queue[f"{n:012d}"] = row("pending", name=f"{n}.flac", at=float(n))
        adminchat._cmd_dlqueue(self.session, "")
        shown = [l for l in self.session.sent if re.match(r"^  [0-9a-f]{12}  ", l)]
        self.assertEqual(len(shown), adminchat.DOWNLOADS_WAITING_MAX)
        self.assertIn("... and 5 more.", self.session.sent[-1])

    def test_the_text_path_says_nothing_extra_under_the_cap(self):
        self.session.draws_dlqueue = False
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        adminchat._cmd_dlqueue(self.session, "")
        self.assertFalse(any("more." in l for l in self.session.sent))

    def test_the_hello_of_an_old_script_does_not_draw_it(self):
        self.assertFalse(adminchat.script_draws_dlqueue("1.14"))
        self.assertTrue(adminchat.script_draws_dlqueue(adminchat.DLQUEUE_SCRIPT_VERSION))
        self.assertTrue(adminchat.script_draws_dlqueue("1.16"))


class RemovingSeveral(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.session = make_session(self)
        persist = dcc_fetch.persist_fetch_history
        dcc_fetch.persist_fetch_history = lambda: None
        self.addCleanup(setattr, dcc_fetch, "persist_fetch_history", persist)

    def dlcancel(self, args):
        """Several ids and `all` do the actual cancelling on a background
        thread (#1246 review: one batched pass instead of one lock/history-
        write per id) - wait for it to finish before the caller asserts on
        the queue or the final reply, instead of racing it."""
        before = set(threading.enumerate())
        adminchat._cmd_dlcancel(self.session, args)
        for thread in set(threading.enumerate()) - before:
            thread.join(5)

    def test_several_ids_let_those_requests_go(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        config.fetch_queue["bbbbbbbbbbbb"] = row("queued")
        config.fetch_queue["cccccccccccc"] = row("pending")
        self.dlcancel("aaaaaaaaaaaa bbbbbbbbbbbb")
        self.assertEqual(list(config.fetch_queue), ["cccccccccccc"])
        self.assertIn("Cancelled 2", self.session.sent[-1])

    def test_all_lets_every_waiting_request_go_and_nothing_else(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        config.fetch_queue["bbbbbbbbbbbb"] = row("offered")
        config.fetch_queue["cccccccccccc"] = row("receiving")
        config.fetch_queue["dddddddddddd"] = row("complete")
        config.fetch_queue["eeeeeeeeeeee"] = row("listening")
        self.dlcancel("all")
        self.assertEqual(sorted(config.fetch_queue), ["cccccccccccc", "dddddddddddd", "eeeeeeeeeeee"])

    def test_a_download_that_has_started_is_left_and_said_so(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        config.fetch_queue["bbbbbbbbbbbb"] = row("receiving")
        self.dlcancel("aaaaaaaaaaaa bbbbbbbbbbbb")
        self.assertEqual(list(config.fetch_queue), ["bbbbbbbbbbbb"])
        self.assertIn("Cancelled 1", self.session.sent[-1])
        self.assertIn("left", self.session.sent[-1])

    def test_one_bad_id_stops_the_whole_command(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        self.dlcancel("aaaaaaaaaaaa nonsense")
        self.assertIn("aaaaaaaaaaaa", config.fetch_queue)
        self.assertIn("Usage", self.session.sent[-1])

    def test_a_repeated_id_is_cancelled_once(self):
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        self.dlcancel("aaaaaaaaaaaa aaaaaaaaaaaa")
        self.assertIn("Cancelled 1", self.session.sent[-1])

    def test_all_with_extra_words_is_refused_instead_of_cancelling_everything(self):
        """#1246 review: `dlcancel all abc123` used to silently ignore the
        extra word and cancel everything anyway - most likely a typo for two
        separate ids, not `all` at all."""
        config.fetch_queue["aaaaaaaaaaaa"] = row("pending")
        self.dlcancel("all aaaaaaaaaaaa")
        self.assertIn("aaaaaaaaaaaa", config.fetch_queue)
        self.assertIn("Usage", self.session.sent[-1])
        self.assertIn("not both", self.session.sent[-1])


class TheScript(unittest.TestCase):

    def setUp(self):
        self.text = script()

    def test_the_version_is_at_least_the_one_the_bot_gates_on(self):
        self.assertTrue(adminchat.script_draws_dlqueue(
            re.search(r"alias dccore\.ver \{ return ([\d.]+) \}", self.text).group(1)))

    def test_the_three_lines_are_handled(self):
        for word in ("DQBEGIN", "DQROW", "DQEND"):
            self.assertIn("%type == " + word, self.text)

    def test_the_dialog_and_its_buttons_exist(self):
        self.assertIn("dialog dccore.dq {", self.text)
        for label in ("Remove selected", "Remove all...", "Refresh"):
            self.assertIn('"' + label + '"', self.text)
        self.assertIn("dlcancel all", self.text)

    def test_remove_all_asks_first(self):
        self.assertIn("Remove all", self.text)
        self.assertIn("$input(Remove all", self.text)

    def test_cancel_and_remove_all_check_the_chat_is_up_first(self):
        """dccore.dq.ask already guarded its own send with $chat($dccore.
        bot) - dccore.dq.cancel and the "Remove all" button did not (#1246
        review): a stale window left open after a disconnect queued a DCC
        CHAT message nobody would ever read."""
        cancel_body = self.text.split("alias dccore.dq.cancel {", 1)[1].split("\n}", 1)[0]
        self.assertIn("$chat($dccore.bot)", cancel_body)
        remove_all = self.text.split("on *:dialog:dccore.dq:sclick:4:", 1)[1].split("\n}", 1)[0]
        self.assertIn("$chat($dccore.bot)", remove_all)

    def test_the_menu_item_is_in_the_info_group_beside_the_other_views(self):
        menu = self.text.split("menu @DCCore", 1)[1]
        info, lists = menu.index("\n  Info\n"), menu.index("\n  Lists\n")
        self.assertLess(info, menu.index(".Download queues...:dccore.queues"))
        self.assertLess(menu.index(".Download queues...:dccore.queues"), lists)

    def test_every_control_in_the_dialog_has_an_id_of_its_own(self):
        body = self.text.split("dialog dccore.dq {", 1)[1].split("\n}", 1)[0]
        ids = re.findall(r'^\s*(?:text|list|button|edit|check|radio|box|combo)\s+(?:"[^"]*"\s*,\s*)?(\d+)\s*,', body, re.M)
        self.assertGreaterEqual(len(ids), 5)
        self.assertEqual(len(ids), len(set(ids)), ids)

if __name__ == "__main__":
    unittest.main()
