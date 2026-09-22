"""An automatic re-fetch counts its own asks, not only its successes.

`lists_worth_refetching()` skips a bot whose held list is younger than
AUTO_REFETCH_INTERVAL_HOURS. That age was measured from `fetched_at` - the last
fetch that COMPLETED. A bot whose list never arrives (it is not answering, or
the daemon was offline when it did) keeps its old `fetched_at` for ever, so it
stayed permanently "stale enough": every hourly sweep asked it again, and so
did every restart, because the restart sweep runs at once. Seen live: one bot
asked at 00:34, 01:34, 02:34 ... 06:34 with no list arriving in between.

The floor now runs from the LATER of the last completed fetch and the last
automatic ask (`last_attempt`, written to disk with the entry so a restart
keeps it). The operator's own click on Re-download list is not counted - it is
theirs, not the sweep's.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import db  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402

HOUR = 3600.0
CHANGED_THEN = {"files": 100, "list_date": "Aug 1st"}
CHANGED_NOW = {"files": 250, "list_date": "Sep 6th"}


class TheFloorRunsFromTheLastAsk(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(original)))
        self.set_config(AUTO_REFETCH_LISTS=True, AUTO_REFETCH_INTERVAL_HOURS=24,
                        AUTO_REFETCH_MAX_PER_RUN=3, bot_joined_channel=True)

        self.saved = []
        real_save = db.save_fetched_bot_lists
        db.save_fetched_bot_lists = lambda registry: self.saved.append(registry)
        self.addCleanup(setattr, db, "save_fetched_bot_lists", real_save)

        self.answer = (200, {})
        real_enqueue = webserver.build_list_fetch_enqueue_result
        webserver.build_list_fetch_enqueue_result = lambda bot: self.answer
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real_enqueue)

        self.store = {}
        self.set_config(fetched_bot_lists=self.store)

    def hold(self, bot="ReelBot", fetched_at=1000.0, **extra):
        self.store[bot.lower()] = dict(
            {"bot": bot, "fetched_at": fetched_at, "entry_count": 10,
             "advert_when_fetched": CHANGED_THEN}, **extra)
        runtime.known_bots[bot.lower()] = dict(CHANGED_NOW, nick=bot)

    def sweep(self, now):
        return list_fetch.refetch_due_lists(log=lambda *_a: None, now=now)

    def test_a_failed_ask_is_not_repeated_at_the_next_sweep(self):
        """The reproduction: the list is 48 hours old and its advert says it
        changed, so it is asked - and when nothing arrives, the sweep an hour
        later must not ask again."""
        self.hold(fetched_at=1000.0)
        first = 1000.0 + 48 * HOUR

        self.assertEqual(self.sweep(first), ["ReelBot"])
        self.assertEqual(self.sweep(first + HOUR), [],
                         "asked again an hour after asking, with nothing received")
        self.assertEqual(self.sweep(first + 23 * HOUR), [])

    def test_it_is_asked_again_once_the_interval_has_passed_since_the_ask(self):
        self.hold(fetched_at=1000.0)
        first = 1000.0 + 48 * HOUR

        self.sweep(first)

        self.assertEqual(self.sweep(first + 24 * HOUR), ["ReelBot"])

    def test_the_ask_is_written_to_disk_with_the_entry(self):
        self.hold(fetched_at=1000.0)

        self.sweep(1000.0 + 48 * HOUR)

        self.assertTrue(self.saved, "the registry was not saved after asking")
        self.assertEqual(self.saved[-1]["reelbot"]["last_attempt"], 1000.0 + 48 * HOUR)

    def test_a_restart_keeps_it(self):
        """The registry as saved, loaded into a fresh daemon: the sweep the
        restart runs at once must not ask the bot again."""
        self.hold(fetched_at=1000.0)
        first = 1000.0 + 48 * HOUR
        self.sweep(first)
        on_disk = self.saved[-1]

        self.store.clear()
        self.store.update({key: dict(value) for key, value in on_disk.items()})

        self.assertEqual(self.sweep(first + 5 * 60), [],
                         "asked again straight after a restart")

    def test_a_refused_ask_is_not_counted_as_an_ask(self):
        """409 - the bot is not here, or a fetch of it is running. Nothing was
        sent, so nothing is remembered and the next sweep may try."""
        self.hold(fetched_at=1000.0)
        self.answer = (409, {"error": "not in any channel"})
        first = 1000.0 + 48 * HOUR

        self.assertEqual(self.sweep(first), [])
        self.assertNotIn("last_attempt", self.store["reelbot"])
        self.assertEqual(self.saved, [])

        self.answer = (200, {})
        self.assertEqual(self.sweep(first + HOUR), ["ReelBot"])

    def test_a_completed_fetch_still_resets_the_floor(self):
        """fetched_at newer than the last ask: the floor runs from the fetch."""
        self.hold(fetched_at=1000.0 + 50 * HOUR, last_attempt=1000.0 + 40 * HOUR)

        self.assertEqual(self.sweep(1000.0 + 60 * HOUR), [])
        self.assertEqual(self.sweep(1000.0 + 75 * HOUR), ["ReelBot"])

    def test_a_damaged_mark_is_ignored_not_fatal(self):
        self.hold(fetched_at=1000.0, last_attempt="not a number")

        self.assertEqual(self.sweep(1000.0 + 48 * HOUR), ["ReelBot"])

    def test_no_interval_means_no_floor(self):
        """AUTO_REFETCH_INTERVAL_HOURS = 0 is the operator asking for the old
        behaviour; remembering asks must not quietly override it."""
        self.set_config(AUTO_REFETCH_INTERVAL_HOURS=0)
        self.hold(fetched_at=1000.0)

        self.assertEqual(self.sweep(5000.0), ["ReelBot"])
        self.assertEqual(self.sweep(5000.0 + HOUR), ["ReelBot"])


if __name__ == "__main__":
    unittest.main()
