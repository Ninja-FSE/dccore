"""#926: the fetch queue waits for a bot, paces itself per bot, and survives a
restart - the way AutoGet kept its download list working by itself.

- A request waits while its bot is away, and goes out a minute after it is
  back (it may still be loading; every other fetcher is asking at once).
- One bot holds at most FETCH_MAX_PER_BOT of our requests - asked, queued
  there or arriving - and the next goes when one finishes. Before this a
  queued request freed its slot and the dispatcher sent the rest of a
  hundred-file selection at once, which a server answers with "queue full".
- A busy answer is asked again later (see the replies tests).
- Unfinished requests survive a restart; one that was mid-flight is asked
  again.
- A bot we know can be queued for while it is offline; a nick we have never
  seen is still refused.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class QueueCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#chan", FETCH_MAX_PER_BOT=3)
        config.channel_users["#chan"] = {"serverone", "servertwo", "someuser"}

    def queue_up(self, count, bot="ServerOne"):
        rids = []
        for n in range(count):
            rid = dcc_fetch.enqueue_fetch(bot, f"Track {n:02d}.flac")
            # Later queue_up() calls come after earlier ones: no ties.
            config.fetch_queue[rid]["requested_at"] = time.time() - 1000 + len(config.fetch_queue) + n
            rids.append(rid)
        return rids

    def states(self, rids):
        return [config.fetch_queue[rid]["state"] for rid in rids]

    def asked(self):
        return [msg for _user, msg, *_ in self.oserve.queued if "PRIVMSG" in msg]


class OneBotHoldsOnlySoMany(QueueCase):
    def test_three_go_out_and_the_rest_wait_their_turn(self):
        rids = self.queue_up(5)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"] * 3 + ["pending"] * 2)
        self.assertEqual(config.fetch_queue[rids[3]]["waiting"], "their-turn")
        self.assertEqual(len(self.asked()), 3)

    def test_a_request_queued_there_still_counts(self):
        """It holds no slot of ours, but a place in their queue."""
        rids = self.queue_up(4)
        dcc_fetch.check_fetch_queue()
        dcc_fetch.handle_bot_reply("ServerOne", "Request Accepted - File: Track 00.flac - Position: 9 - OmeNServE")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["queued", "offered", "offered", "pending"])

    def test_the_next_goes_when_one_finishes(self):
        rids = self.queue_up(4)
        dcc_fetch.check_fetch_queue()
        config.fetch_queue[rids[0]]["state"] = "complete"
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids)[3], "offered")
        self.assertNotIn("waiting", config.fetch_queue[rids[3]])

    def test_each_bot_has_its_own_allowance(self):
        one = self.queue_up(4, bot="ServerOne")
        two = self.queue_up(2, bot="ServerTwo")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(one).count("offered"), 3)
        self.assertEqual(self.states(two), ["offered", "offered"])

    def test_zero_is_no_limit(self):
        self.set_config(FETCH_MAX_PER_BOT=0)
        rids = self.queue_up(6)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids).count("offered"), 6)

    def test_the_slot_limit_still_applies_across_bots(self):
        self.set_config(MAX_FETCH_SLOTS=2)
        one = self.queue_up(2, bot="ServerOne")
        two = self.queue_up(2, bot="ServerTwo")
        dcc_fetch.check_fetch_queue()
        self.assertEqual((self.states(one) + self.states(two)).count("offered"), 2)
        self.assertEqual(config.fetch_queue[two[0]]["waiting"], "slots")


class ItWaitsForTheBot(QueueCase):
    def test_an_absent_bot_is_not_asked(self):
        config.channel_users["#chan"].discard("serverone")
        rids = self.queue_up(2)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["pending", "pending"])
        self.assertEqual(config.fetch_queue[rids[0]]["waiting"], "offline")
        self.assertEqual(self.asked(), [])

    def test_back_again_it_gets_a_minute_then_it_is_asked(self):
        config.channel_users["#chan"].discard("serverone")
        rids = self.queue_up(1)
        dcc_fetch.check_fetch_queue()
        config.channel_users["#chan"].add("serverone")
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rids[0]]["waiting"], "just-back")
        dcc_fetch._back_since["serverone"] -= dcc_fetch.RETURN_DELAY_SECONDS + 1
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"])

    def test_a_bot_here_all_along_is_asked_at_once(self):
        rids = self.queue_up(1)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"])

    def test_while_we_are_still_joining_nothing_is_held_back(self):
        config.channel_users.clear()
        rids = self.queue_up(1)
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"])

    def test_a_busy_request_waits_for_its_time(self):
        rids = self.queue_up(1)
        config.fetch_queue[rids[0]]["retry_at"] = time.time() + 300
        dcc_fetch.check_fetch_queue()
        self.assertEqual(config.fetch_queue[rids[0]]["waiting"], "retry")
        config.fetch_queue[rids[0]]["retry_at"] = time.time() - 1
        dcc_fetch.check_fetch_queue()
        self.assertEqual(self.states(rids), ["offered"])


class ItSurvivesARestart(QueueCase):
    def test_waiting_and_queued_come_back_as_they_were_and_mid_flight_is_asked_again(self):
        config.channel_users["#chan"].discard("serverone")
        waiting, queued, moving = self.queue_up(3)
        config.fetch_queue[queued].update(state="queued", queued_at=time.time(), queue_position=4)
        config.fetch_queue[moving].update(state="receiving", offered_at=time.time(), bytes_received=123)
        dcc_fetch.check_fetch_queue()

        restored = db.load_fetch_history()
        self.assertEqual(restored[waiting]["state"], "pending")
        self.assertEqual(restored[queued]["state"], "queued")
        self.assertEqual(restored[queued]["queue_position"], 4)
        self.assertEqual(restored[moving]["state"], "pending")
        self.assertEqual(restored[moving]["bytes_received"], 0)

    def test_a_ticking_transfer_does_not_rewrite_the_file(self):
        (moving,) = self.queue_up(1)
        config.fetch_queue[moving].update(state="receiving", offered_at=time.time(), bytes_received=1)
        dcc_fetch.check_fetch_queue()
        writes = []
        real = db.save_fetch_history
        db.save_fetch_history = lambda rows: (writes.append(1), real(rows))[-1]
        self.addCleanup(setattr, db, "save_fetch_history", real)
        config.fetch_queue[moving]["bytes_received"] = 99999
        dcc_fetch.check_fetch_queue()
        self.assertEqual(writes, [])


class AKnownBotCanBeQueuedForWhileAway(QueueCase):
    def setUp(self):
        super().setUp()
        config.channel_users["#chan"].discard("serverone")

    def test_a_bot_we_have_seen_advertise(self):
        runtime.known_bots["serverone"] = {"nick": "ServerOne", "last_seen": time.time()}
        self.addCleanup(runtime.known_bots.pop, "serverone", None)
        self.assertTrue(dcc_fetch.bot_is_known("ServerOne"))
        _status, result = webserver.build_fetch_enqueue_result([{"bot": "ServerOne", "filename": "Track.flac"}])
        self.assertEqual(len(result.get("created") or []), 1, result)

    def test_a_nick_we_have_never_seen_is_still_refused(self):
        self.assertFalse(dcc_fetch.bot_is_known("NoSuchServer"))
        _status, result = webserver.build_fetch_enqueue_result([{"bot": "NoSuchServer", "filename": "Track.flac"}])
        self.assertFalse(result.get("created"))
        self.assertTrue(result.get("errors"))


if __name__ == "__main__":
    unittest.main()
