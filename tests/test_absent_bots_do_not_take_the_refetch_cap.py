"""Bots that have left do not take the automatic re-fetch's places (#966).

Each sweep asks at most AUTO_REFETCH_MAX_PER_RUN bots, oldest list first. The
oldest lists are the likeliest to belong to bots long gone: once their adverts
age out their freshness is unknown, and past UNKNOWN_LIST_MAX_AGE_DAYS they
are due. Three of them took the three places of every sweep and were refused
as "not here" - which is not an ask, so nothing moved them back - and a bot
in the channel whose list had changed was never asked at all.

Now only bots in one of our channels are considered, and only the asks that
went out count toward the bound.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import db  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402
from tests.support import DCCoreTestCase, bots_in_the_channel  # noqa: E402

DAY = 86400.0
NOW = 1000 * DAY
THEN = {"files": 100, "list_date": "Aug 1st"}
CHANGED = {"files": 250, "list_date": "Sep 6th"}


class SweepCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(), runtime.known_bots.update(original)))
        self.set_config(AUTO_REFETCH_LISTS=True, AUTO_REFETCH_INTERVAL_HOURS=24,
                        AUTO_REFETCH_MAX_PER_RUN=3, bot_joined_channel=True)
        real_save = db.save_fetched_bot_lists
        db.save_fetched_bot_lists = lambda registry: None
        self.addCleanup(setattr, db, "save_fetched_bot_lists", real_save)

        self.asked, self.refuse = [], set()
        real_enqueue = webserver.build_list_fetch_enqueue_result

        def enqueue(bot):
            self.asked.append(bot)
            return (409, {"error": "busy"}) if bot in self.refuse else (200, {})

        webserver.build_list_fetch_enqueue_result = enqueue
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real_enqueue)
        self.store = {}
        self.set_config(fetched_bot_lists=self.store)
        # Somebody is in the channel, whatever the test holds.
        bots_in_the_channel("someuser")

    def gone(self, bot, days_old):
        """A list whose bot left long ago: no advert, older than the limit."""
        self.store[bot.lower()] = {"bot": bot, "fetched_at": NOW - days_old * DAY,
                                   "entry_count": 10, "advert_when_fetched": THEN}

    def changed(self, bot, days_old=2):
        """A bot in the channel whose advert says its list changed."""
        self.store[bot.lower()] = {"bot": bot, "fetched_at": NOW - days_old * DAY,
                                   "entry_count": 10, "advert_when_fetched": THEN}
        runtime.known_bots[bot.lower()] = dict(CHANGED, nick=bot)
        bots_in_the_channel(bot)

    def sweep(self):
        return list_fetch.refetch_due_lists(log=lambda *_a: None, now=NOW)


class GoneBotsAreNotAsked(SweepCase):
    def test_the_audit_s_case(self):
        age = list_fetch.UNKNOWN_LIST_MAX_AGE_DAYS
        for n, bot in enumerate(("GoneOne", "GoneTwo", "GoneThree")):
            self.gone(bot, age + 30 + n)
        self.changed("HereBot")
        self.assertIn("GoneOne", list_fetch.lists_worth_refetching(now=NOW),
                      "a gone bot's old list is due - the premise")

        self.assertEqual(self.sweep(), ["HereBot"])
        self.assertEqual(self.asked, ["HereBot"], "nobody gone was even tried")

    def test_a_gone_bot_back_again_is_asked(self):
        self.gone("GoneOne", list_fetch.UNKNOWN_LIST_MAX_AGE_DAYS + 30)
        self.assertEqual(self.sweep(), [])
        bots_in_the_channel("GoneOne")
        self.assertEqual(self.sweep(), ["GoneOne"])


class OnlyAsksCountTowardTheBound(SweepCase):
    def test_a_refused_ask_leaves_its_place_to_the_next(self):
        self.set_config(AUTO_REFETCH_MAX_PER_RUN=1)
        self.changed("BusyBot", days_old=5)
        self.changed("FreeBot", days_old=3)
        self.refuse.add("BusyBot")
        self.assertEqual(self.sweep(), ["FreeBot"])
        self.assertEqual(self.asked, ["BusyBot", "FreeBot"])

    def test_the_bound_still_holds(self):
        self.set_config(AUTO_REFETCH_MAX_PER_RUN=2)
        for n, bot in enumerate(("One", "Two", "Three", "Four")):
            self.changed(bot, days_old=10 - n)
        self.assertEqual(self.sweep(), ["One", "Two"])
        self.assertEqual(self.asked, ["One", "Two"])

    def test_no_bound_asks_everyone_here(self):
        self.set_config(AUTO_REFETCH_MAX_PER_RUN=0)
        for n, bot in enumerate(("One", "Two", "Three", "Four")):
            self.changed(bot, days_old=10 - n)
        self.assertEqual(len(self.sweep()), 4)


if __name__ == "__main__":
    unittest.main()
