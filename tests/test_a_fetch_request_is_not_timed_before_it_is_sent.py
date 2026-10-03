"""A fetch request goes out once, and is timed from when it goes out (#1028).

The dispatcher logged "Requested" and started the offer timer when it handed
the line to the send queue, where it waited its turn behind every other user's
replies. On a busy bot the timer ran out before the line was sent, each expiry
queued the request again behind the first, and a file was asked for four or
five times - and sent twice by the other bot.
"""

import os
import shutil
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import oserve  # noqa: E402  (the real one: discovery imports every module before any test installs a stub)

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_the_vip_lane_gets_one_slot_per_pass import _WorkerCase  # noqa: E402

FILE = "Artist - Song.mp3"


class Case(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=0, bot_joined_channel=True,
                        FETCH_OFFER_TIMEOUT=60, fetch_request_queue=[])
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)
        self.set_config(vip_queue=[], send_queue={})
        # The real send queue, not the stub the base class installs: the lane
        # a request lands in is oserve.queue_message()'s to decide.
        stub = sys.modules.get("oserve")
        sys.modules["oserve"] = oserve
        self.addCleanup(sys.modules.__setitem__, "oserve", stub)
        real_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 13, 0, 10 ** 12)
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)

    @property
    def ordinary(self):
        return [line for lines in config.send_queue.values() for line in lines]

    def ask(self, name=FILE):
        rid = dcc_fetch.enqueue_fetch("ServerOne", name)
        self.assertIsNotNone(rid)
        dcc_fetch.check_fetch_queue()
        return rid

    def age(self, rid, seconds):
        config.fetch_queue[rid]["offered_at"] -= seconds


class TheLane(Case):
    def test_the_request_goes_in_its_own_lane(self):
        self.ask()
        self.assertEqual(len(config.fetch_request_queue), 1)
        self.assertIn(f"!ServerOne {FILE}", config.fetch_request_queue[0])
        self.assertEqual(self.ordinary, [])

    def test_oserve_puts_it_there_and_not_behind_the_advert(self):
        import oserve
        self.set_config(vip_queue=["the advert"], send_queue={})
        oserve.queue_message("ServerOne", "PRIVMSG #chan :!ServerOne x\r\n", is_vip=oserve.FETCH_LANE)
        self.assertEqual(config.fetch_request_queue, ["PRIVMSG #chan :!ServerOne x\r\n"])
        self.assertEqual(config.vip_queue, ["the advert"])
        self.assertEqual(config.send_queue, {})


class TheTimer(Case):
    def test_it_does_not_run_while_the_line_is_still_waiting(self):
        rid = self.ask()
        self.age(rid, 500)
        dcc_fetch.check_fetch_queue()
        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "offered")
        self.assertEqual(len(config.fetch_request_queue), 1, "nothing was asked a second time")
        self.assertLess(time.time() - row["offered_at"], 5)

    def test_it_runs_once_the_line_has_gone(self):
        rid = self.ask()
        config.fetch_request_queue.clear()
        self.age(rid, 500)
        dcc_fetch.check_fetch_queue()
        row = config.fetch_queue[rid]
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["silent_asks"], 2)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(row["state"], "offered")
        self.assertEqual(len(config.fetch_request_queue), 1)


class NoSecondLine(Case):
    def test_asking_again_replaces_a_line_still_waiting(self):
        rid = self.ask()
        config.fetch_queue[rid].update(state="pending", offered_at=None)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(len(config.fetch_request_queue), 1)

    def test_a_finished_row_takes_its_waiting_line_back(self):
        rid = self.ask()
        config.fetch_queue[rid]["state"] = "complete"
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_request_queue, [])
        self.assertNotIn("request_line", config.fetch_queue[rid])

    def test_a_deleted_row_takes_its_waiting_line_back(self):
        import webserver
        rid = self.ask()
        status, _ = webserver.build_fetch_delete_result(rid)
        self.assertEqual(status, 200)
        self.assertEqual(config.fetch_request_queue, [])

    def test_another_rows_line_is_left_alone(self):
        first = self.ask()
        self.ask("Other - Song.mp3")
        config.fetch_queue[first]["state"] = "failed"
        dcc_fetch.check_fetch_queue()
        self.assertEqual(len(config.fetch_request_queue), 1)
        self.assertIn("Other - Song.mp3", config.fetch_request_queue[0])


class TheDispatchIsAtomicWithTheRow(Case):
    def _row_changes_before_the_line_goes_out(self, change):
        rid = dcc_fetch.enqueue_fetch("ServerOne", FILE)
        real = dcc_fetch.dcc.channel_containing_user

        def channel_then_change(bot):
            change(rid)
            return real(bot) or "#chan"

        with mock.patch.object(dcc_fetch.dcc, "channel_containing_user", channel_then_change):
            dcc_fetch.check_fetch_queue()
        return rid

    def test_a_row_deleted_in_the_gap_sends_no_line(self):
        self._row_changes_before_the_line_goes_out(lambda rid: config.fetch_queue.pop(rid))
        self.assertEqual(config.fetch_request_queue, [])

    def test_a_row_that_failed_in_the_gap_sends_no_line(self):
        rid = self._row_changes_before_the_line_goes_out(
            lambda rid: config.fetch_queue[rid].update(state="failed"))
        self.assertEqual(config.fetch_request_queue, [])
        self.assertNotIn("request_line", config.fetch_queue[rid])

    def test_a_row_still_waiting_gets_its_line_recorded(self):
        rid = self._row_changes_before_the_line_goes_out(lambda rid: None)
        self.assertEqual(config.fetch_queue[rid]["request_line"], config.fetch_request_queue[0])


class TheTakeBackIsForEveryStateButOffered(Case):
    def test_a_pending_row_takes_a_stale_line_back(self):
        rid = self.ask()
        config.fetch_queue[rid].update(state="pending", offered_at=None, retry_at=time.time() + 3600)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_request_queue, [])
        self.assertNotIn("request_line", config.fetch_queue[rid])

    def test_a_restart_does_not_bring_the_line_back(self):
        rid = self.ask()
        restored = dcc_fetch._restart_form(config.fetch_queue[rid])
        self.assertEqual(restored["state"], "pending")
        self.assertNotIn("request_line", restored)


class AnUnsentRequestIsNotWithdrawnAtTheBot(Case):
    def _delete(self, rid):
        import webserver
        with mock.patch.object(dcc_fetch, "drop_our_request_at", return_value=True) as drop:
            status, _ = webserver.build_fetch_delete_result(rid)
        self.assertEqual(status, 200)
        return drop

    def test_a_line_that_never_left_needs_no_remove(self):
        drop = self._delete(self.ask())
        drop.assert_not_called()

    def test_a_line_that_went_out_is_withdrawn(self):
        rid = self.ask()
        config.fetch_request_queue.clear()
        drop = self._delete(rid)
        drop.assert_called_once()

    def test_clearing_finished_rows_takes_their_unsent_lines_back(self):
        import webserver
        rid = self.ask()
        config.fetch_queue[rid]["state"] = "complete"
        status, body = webserver.build_fetch_clear_result({"which": "finished"})
        self.assertEqual((status, body["cleared"]), (200, 1))
        self.assertEqual(config.fetch_request_queue, [])


class ThePumpSendsRequestsWithoutStarvingAnyone(_WorkerCase):
    def setUp(self):
        super().setUp()
        config.fetch_request_queue = []

    def test_a_request_goes_out_ahead_of_a_vip_backlog(self):
        config.vip_queue.extend(f"PRIVMSG #chan :advert {n}\r\n" for n in range(50))
        config.fetch_request_queue.append("PRIVMSG #chan :!ServerOne x\r\n")
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: len(self.sent()) >= 2))
        self.assertEqual(self.sent()[0], "PRIVMSG #chan :!ServerOne x\r\n")

    def test_the_standard_lane_still_runs_through_a_backlog_of_requests(self):
        config.fetch_request_queue.extend(f"PRIVMSG #chan :!ServerOne f{n}\r\n" for n in range(50))
        config.send_queue["user1"] = ["NOTICE user1 :a reply\r\n"]
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: any("NOTICE user1" in line for line in self.sent()), 1.5),
                        "the ordinary lane starved behind the requests")
        position = [i for i, line in enumerate(self.sent()) if "NOTICE user1" in line][0]
        self.assertLessEqual(position, 2)

    def test_a_vip_line_is_not_lost_when_the_requests_run_out(self):
        config.fetch_request_queue.append("PRIVMSG #chan :!ServerOne x\r\n")
        config.vip_queue.append("PRIVMSG #chan :advert\r\n")
        self.start_worker()
        self.assertTrue(self.wait_for(lambda: len(self.sent()) >= 2))
        self.assertEqual(self.sent(), ["PRIVMSG #chan :!ServerOne x\r\n", "PRIVMSG #chan :advert\r\n"])


if __name__ == "__main__":
    unittest.main()
