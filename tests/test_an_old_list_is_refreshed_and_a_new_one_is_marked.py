"""#926 item 6: a list with no date is refreshed once it is old, and a list
nobody has opened yet says "New".

- The automatic refresh acts on a bot's advert saying its list changed. A bot
  that publishes no date never says so, and its list was never refreshed. Age
  is the only evidence such a bot leaves: past UNKNOWN_LIST_MAX_AGE_DAYS it is
  refreshed (AutoGet expired lists after N days). A dated bot is unchanged.
- Every fetch leaves the list unseen; opening it in the List Browser marks it
  seen. Entries from before this carry no seen_at and are never marked.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

DAY = 86400.0
NOW = 100 * DAY


class ListCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(), runtime.known_bots.update(original)))
        self.set_config(AUTO_REFETCH_LISTS=True, AUTO_REFETCH_INTERVAL_HOURS=24, bot_joined_channel=True)
        self.saved = []
        real_save = db.save_fetched_bot_lists
        db.save_fetched_bot_lists = lambda registry: self.saved.append(registry)
        self.addCleanup(setattr, db, "save_fetched_bot_lists", real_save)
        self.store = {}
        self.set_config(fetched_bot_lists=self.store)

    def hold(self, bot, fetched_at, advert_then=None, advert_now=None, **extra):
        self.store[bot.lower()] = dict({"bot": bot, "fetched_at": fetched_at, "entry_count": 10,
                                        "advert_when_fetched": advert_then or {}}, **extra)
        if advert_now is not None:
            runtime.known_bots[bot.lower()] = dict(advert_now, nick=bot)


class AnUndatedListIsRefreshedWhenOld(ListCase):
    def test_older_than_the_limit(self):
        self.hold("QuietBot", NOW - (list_fetch.UNKNOWN_LIST_MAX_AGE_DAYS + 1) * DAY)
        self.assertEqual(list_fetch.lists_worth_refetching(now=NOW), ["QuietBot"])

    def test_younger_than_the_limit(self):
        self.hold("QuietBot", NOW - (list_fetch.UNKNOWN_LIST_MAX_AGE_DAYS - 1) * DAY)
        self.assertEqual(list_fetch.lists_worth_refetching(now=NOW), [])

    def test_a_dated_bot_that_has_not_changed_is_left_alone_however_old(self):
        same = {"files": 100, "list_date": "Aug 1st"}
        self.hold("DatedBot", NOW - 60 * DAY, advert_then=same, advert_now=same)
        self.assertEqual(list_fetch.lists_worth_refetching(now=NOW), [])

    def test_the_sweep_says_why(self):
        """An undated list did not "change" - the console line says it is old."""
        asked = []
        real = webserver.build_list_fetch_enqueue_result
        webserver.build_list_fetch_enqueue_result = lambda bot: (asked.append(bot), (200, {}))[1]
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real)
        told = []
        real_tell = list_fetch._tell_the_console
        list_fetch._tell_the_console = lambda bot, action, text: told.append(text)
        self.addCleanup(setattr, list_fetch, "_tell_the_console", real_tell)
        self.hold("QuietBot", NOW - 60 * DAY)
        self.hold("DatedBot", NOW - 60 * DAY, advert_then={"files": 1, "list_date": "Aug 1st"},
                  advert_now={"files": 2, "list_date": "Sep 1st"})
        lines = []
        list_fetch.refetch_due_lists(log=lines.append, now=NOW)
        self.assertEqual(sorted(asked), ["DatedBot", "QuietBot"])
        self.assertIn("QuietBot's list is over 14 days old - asking again automatically", told)
        self.assertIn("DatedBot's list has changed - asking again automatically", told)
        self.assertTrue(any("QuietBot" in line and "shows no date" in line for line in lines))

    def test_off_is_off(self):
        self.set_config(AUTO_REFETCH_LISTS=False)
        self.hold("QuietBot", NOW - 60 * DAY)
        self.assertEqual(list_fetch.lists_worth_refetching(now=NOW), [])


class ANewListSaysSo(ListCase):
    def row(self, bot):
        return [r for r in webserver.build_fetched_bot_list_summaries()
                if r.get("bot") == bot][0]

    def test_fetched_and_not_opened_is_unseen(self):
        self.hold("NewBot", NOW, seen_at=0)
        self.assertTrue(self.row("NewBot")["unseen"])

    def test_opening_it_marks_it_seen_and_saves(self):
        self.hold("NewBot", 1000.0, seen_at=0)
        self.assertTrue(list_fetch.mark_seen("NewBot"))
        self.assertFalse(self.row("NewBot")["unseen"])
        self.assertTrue(self.saved)
        self.assertFalse(list_fetch.mark_seen("NewBot"), "already seen: nothing to do")

    def test_an_entry_from_before_this_is_not_marked(self):
        self.hold("OldBot", NOW)
        self.assertFalse(self.row("OldBot")["unseen"])
        self.assertFalse(list_fetch.mark_seen("OldBot"))

    def test_a_refreshed_list_is_new_again(self):
        """Every fetch writes seen_at = 0 - read from the code that stores it."""
        import io
        with io.open(os.path.join(REPO_ROOT, "list_fetch.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index('"advert_when_fetched": _advert_snapshot(bot),')
        self.assertIn('"seen_at": 0,', code[at:at + 500])

    def test_the_route_marks_it_when_the_list_is_served(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "webserver.py"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index("def api_filelists_bot(nick):")
        body = code[at:at + 900]
        self.assertIn("if status == 200:", body)
        self.assertIn("list_fetch.mark_seen(nick)", body)


if __name__ == "__main__":
    unittest.main()
