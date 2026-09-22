"""#133's freshness signal: is the list you hold still what that bot offers?

THE RULE, AND THE ONE IT REPLACED

Compare **their advert then against their advert now** — not their advert
against our own parsed row count.

#133 settled that against the obvious alternative for a concrete reason: bots
count differently (some include header lines, some count album rows
separately), so an off-by-a-few would leave a list permanently marked stale
with nothing actually wrong. A bot compared against its own earlier claim has
no such problem.

**Date first, count second.** A count can coincidentally match after an edit; a
date cannot. And from the capture #133 records, 31 of 32 bots advertising in
one channel publish a date, so it is very nearly universal.

WHY "UNKNOWN" IS A REAL ANSWER

A missing field means "this bot did not say", never zero — the rule
irc.parse_channel_advert() already follows. We can fetch a list from a bot
whose advert has not come round yet, and a bot that publishes no date should
show no freshness claim rather than an invented one. Saying "we cannot tell"
is honest; the page renders it as nothing at all.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class WhatTheyAdvertisedWhenWeFetched(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(self.original)))

    def test_it_records_what_that_bot_published(self):
        runtime.known_bots["reelbot"] = {
            "nick": "ReelBot", "files": 12004, "list_date": "Aug 28th"}

        self.assertEqual(list_fetch._advert_snapshot("ReelBot"),
                         {"files": 12004, "list_date": "Aug 28th"})

    def test_a_bot_we_have_never_seen_records_nothing(self):
        """An ordinary state: a list can be fetched from a bot whose advert has
        not come round yet."""
        self.assertEqual(list_fetch._advert_snapshot("NeverSeen"), {})

    def test_a_field_the_bot_did_not_publish_is_absent_not_zero(self):
        """Zero would be a claim they never made, and would then compare
        unequal against every later advert for ever."""
        runtime.known_bots["quietbot"] = {"nick": "QuietBot", "files": 900}

        snapshot = list_fetch._advert_snapshot("QuietBot")

        self.assertEqual(snapshot, {"files": 900})
        self.assertNotIn("list_date", snapshot)

    def test_the_lookup_is_case_insensitive(self):
        """known_bots is keyed by nick.lower(); the bot argument is whatever
        the operator or the advert spelled."""
        runtime.known_bots["boombox"] = {"nick": "BoomBox", "files": 1}

        self.assertEqual(list_fetch._advert_snapshot("  BOOMBOX  "),
                         {"files": 1})


class TheirAdvertThenAgainstTheirAdvertNow(unittest.TestCase):

    def verdict(self, then, now):
        return webserver._freshness(then, now)

    def test_an_unchanged_advert_is_current(self):
        advert = {"files": 12004, "list_date": "Aug 28th"}

        self.assertEqual(self.verdict(advert, dict(advert)), "current")

    def test_a_new_date_is_changed(self):
        self.assertEqual(
            self.verdict({"files": 7902, "list_date": "Aug 10th"},
                         {"files": 8110, "list_date": "Aug 28th"}), "changed")

    def test_the_date_decides_before_the_count(self):
        """A count can coincidentally match after an edit; a date cannot. So a
        rebuilt list with the same total is still a changed list."""
        self.assertEqual(
            self.verdict({"files": 8110, "list_date": "Aug 10th"},
                         {"files": 8110, "list_date": "Aug 28th"}), "changed")

    def test_the_count_is_used_when_neither_side_gives_a_date(self):
        self.assertEqual(self.verdict({"files": 900}, {"files": 900}), "current")
        self.assertEqual(self.verdict({"files": 900}, {"files": 950}), "changed")

    def test_nothing_recorded_is_unknown(self):
        self.assertEqual(self.verdict({}, {"files": 1}), "unknown")

    def test_nothing_advertised_now_is_unknown(self):
        """They may simply not have advertised since the daemon started."""
        self.assertEqual(self.verdict({"files": 1}, {}), "unknown")

    def test_two_adverts_with_no_field_in_common_are_unknown(self):
        """Not "changed". We have no basis to say either way, and claiming
        staleness would send the operator to re-fetch for nothing."""
        self.assertEqual(
            self.verdict({"list_date": "Aug 10th"}, {"files": 900}), "unknown")


class TheSummariesCarryIt(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(self.original)))
        runtime.known_bots.update({
            "reelbot": {"nick": "ReelBot", "files": 12004,
                          "list_date": "Aug 28th"},
            "boombox": {"nick": "BoomBox", "files": 8110,
                         "list_date": "Aug 28th"},
        })
        self.set_config(fetched_bot_lists={
            "reelbot": {"bot": "ReelBot", "fetched_at": 1,
                          "entry_count": 12004,
                          "advert_when_fetched": {"files": 12004,
                                                  "list_date": "Aug 28th"}},
            "boombox": {"bot": "BoomBox", "fetched_at": 1,
                         "entry_count": 7902,
                         "advert_when_fetched": {"files": 7902,
                                                 "list_date": "Aug 10th"}},
        })

    def rows(self):
        return {row["bot"]: row
                for row in webserver.build_fetched_bot_list_summaries()}

    def test_an_unchanged_list_reads_current(self):
        self.assertEqual(self.rows()["ReelBot"]["freshness"], "current")

    def test_a_changed_list_reads_changed(self):
        self.assertEqual(self.rows()["BoomBox"]["freshness"], "changed")

    def test_both_adverts_are_returned_so_the_page_can_say_what_changed(self):
        """The banner names the numbers - "they advertised 7,902 files built
        10 Aug, and now advertise 8,110 built 28 Aug" - which it cannot do
        from a verdict alone."""
        row = self.rows()["BoomBox"]

        self.assertEqual(row["advert_then"]["files"], 7902)
        self.assertEqual(row["advert_now"]["files"], 8110)

    def test_a_list_recorded_before_this_existed_is_unknown(self):
        """Every list already on disk was fetched without an advert snapshot.
        It must read as "we cannot tell", not as stale."""
        self.set_config(fetched_bot_lists={
            "old": {"bot": "OldBot", "fetched_at": 1, "entry_count": 5}})

        self.assertEqual(self.rows()["OldBot"]["freshness"], "unknown")

    def test_the_existing_fields_are_untouched(self):
        """The switcher renders bot and count; adding freshness must not
        disturb what was already there."""
        row = self.rows()["ReelBot"]

        self.assertEqual(row["count"], 12004)
        self.assertEqual(row["fetched_at"], 1)


class TheFetchRecordsIt(unittest.TestCase):
    """The wiring, read out of the source.

    Every other test here builds `fetched_bot_lists` directly, so none of them
    can see whether the fetch path stores the snapshot at all - a mutation
    replacing the call with {} passed all of them. Driving the real fetch
    needs a socket, a zip and a peer; this pins the one line that matters
    instead, and says plainly that is what it is.
    """

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "list_fetch.py"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_stored_record_carries_the_advert(self):
        code = "\n".join(line.split("#", 1)[0]
                         for line in self.source().splitlines())

        self.assertIn('"advert_when_fetched": _advert_snapshot(bot)', code,
                      "the fetch no longer records what that bot was "
                      "advertising, so every held list reads as \"unknown\" "
                      "for ever")

    def test_it_is_recorded_beside_the_rest_of_the_entry(self):
        """In the same dict literal that is persisted, not somewhere a later
        failure could skip."""
        code = self.source()
        entry = code.split("store[str(bot).strip().lower()] = {", 1)[1]
        entry = entry.split("\n    }", 1)[0]

        self.assertIn("advert_when_fetched", entry)


class ThePageRendersItHonestly(unittest.TestCase):
    """Read out of the source: nothing here executes JavaScript."""

    def js(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_only_a_changed_list_shows_the_banner(self):
        """"unknown" must render as nothing at all - no freshness claim is the
        honest output when we cannot tell."""
        block = self.js().split("function renderFilelistsFreshness(", 1)[1]
        block = block.split("function describeAdvert(", 1)[0]

        self.assertIn('row.freshness !== "changed"', block)
        self.assertIn("banner.hidden = true;", block)

    def test_an_advert_with_nothing_readable_says_so(self):
        """The wording is a translation key, not literal text - see
        web/lang/en.json's filelists.advertNothing."""
        block = self.js().split("function describeAdvert(", 1)[1][:400]

        self.assertIn('t("filelists.advertNothing")', block)


class AskingAgainWithoutBeingAsked(DCCoreTestCase):
    """#302: "Implement auto-downloading of lists from bots to keep them
    automatically up to date."

    The advert decides, not a timer. #286 already worked out what "moved on"
    means and why; a timer alone would ask every bot for a list we already
    have, every interval, for ever - other people's bandwidth and other
    people's transfer slots.
    """

    def setUp(self):
        super().setUp()
        original = dict(runtime.known_bots)
        runtime.known_bots.clear()
        self.addCleanup(lambda: (runtime.known_bots.clear(),
                                 runtime.known_bots.update(original)))
        self.set_config(AUTO_REFETCH_LISTS=True,
                        AUTO_REFETCH_INTERVAL_HOURS=0,
                        AUTO_REFETCH_MAX_PER_RUN=3,
                        # The steady state every test below means to
                        # represent: already settled into its channels. The
                        # one test for the OTHER state
                        # (test_nothing_is_asked_before_the_bot_has_joined_anything)
                        # overrides this for itself.
                        bot_joined_channel=True)

    def hold(self, bot, then, fetched_at=1.0):
        store = dict(getattr(config, "fetched_bot_lists", {}) or {})
        store[bot.lower()] = {"bot": bot, "fetched_at": fetched_at,
                              "entry_count": 10, "advert_when_fetched": then}
        self.set_config(fetched_bot_lists=store)

    def advertise(self, bot, now):
        runtime.known_bots[bot.lower()] = dict(now, nick=bot)

    def test_the_worker_is_started_when_it_is_switched_on(self):
        """Read out of oserve.py: driving startup() needs a live socket, and
        the one thing that matters is that the loop is reached at all.

        Boot goes through ensure_auto_refetch_worker() (#625), the same
        guarded call the rehash makes, so the setting-gate and the
        once-only guard are evaluated in tests/test_turning_auto_refetch_
        on_live_starts_the_worker.py rather than read out of here; what
        startup() has to do is call it."""
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            code = chr(10).join(line.split("#", 1)[0]
                                for line in handle.read().splitlines())
        body = code.split("def startup(", 1)[1]

        self.assertIn("list_fetch.ensure_auto_refetch_worker()", body,
                      "startup() no longer starts the refresh worker")
        self.assertNotIn("list_fetch.auto_refetch_worker", body,
                         "startup() starts the loop directly, bypassing the "
                         "once-only guard the rehash shares")

    def test_the_loop_itself_runs_a_sweep_and_then_waits(self):
        """The worker is an endless loop, so it takes its sleep as an argument
        - which is what lets a test enter it, watch one pass, and leave. The
        coverage gate caught it having none, and allow-listing a loop that was
        built to be drivable would have been the wrong answer."""
        swept = []
        real = list_fetch.refetch_due_lists
        list_fetch.refetch_due_lists = lambda *a, **k: swept.append(1) or []
        self.addCleanup(setattr, list_fetch, "refetch_due_lists", real)

        class Enough(Exception):
            pass

        def stop(seconds):
            self.slept = seconds
            raise Enough()

        with self.assertRaises(Enough):
            list_fetch.auto_refetch_worker(sleep=stop)

        self.assertEqual(len(swept), 1)
        self.assertEqual(self.slept, 3600.0,
                         "the sweep interval is not the configured staleness "
                         "window - it is how often the check runs")

    def test_the_loop_survives_a_sweep_that_raises(self):
        """A background thread that dies on one bad sweep stops refreshing
        anything, silently, until the daemon is restarted."""
        real = list_fetch.refetch_due_lists

        def explode(*_a, **_k):
            raise RuntimeError("the registry went away")

        list_fetch.refetch_due_lists = explode
        self.addCleanup(setattr, list_fetch, "refetch_due_lists", real)

        class Enough(Exception):
            pass

        def stop(_seconds):
            raise Enough()

        with self.assertRaises(Enough):
            list_fetch.auto_refetch_worker(sleep=stop)

    def test_a_list_its_bot_says_has_changed_is_due(self):
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"})
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})

        self.assertEqual(list_fetch.lists_worth_refetching(now=10 ** 9),
                         ["ReelBot"])

    def test_a_list_that_has_not_changed_is_left_alone(self):
        """Otherwise this is a timer, and a timer re-asks every bot for a list
        we already have."""
        same = {"files": 100, "list_date": "Aug 1st"}
        self.hold("ReelBot", same)
        self.advertise("ReelBot", same)

        self.assertEqual(list_fetch.lists_worth_refetching(now=10 ** 9), [])

    def test_unknown_is_not_changed(self):
        """A bot that publishes no date gives no evidence either way, and
        acting on no evidence is what makes an automatic feature
        untrustworthy."""
        self.hold("ReelBot", {})
        self.advertise("ReelBot", {"files": 250})

        self.assertEqual(list_fetch.lists_worth_refetching(now=10 ** 9), [])

    def test_it_is_off_unless_switched_on(self):
        """It spends other people's bandwidth and other people's slots, which
        is a decision to make rather than one to inherit."""
        self.set_config(AUTO_REFETCH_LISTS=False)
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"})
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})

        self.assertEqual(list_fetch.lists_worth_refetching(now=10 ** 9), [])

    def test_one_bot_is_not_re_asked_more_often_than_the_interval(self):
        """A bot rebuilding its list hourly would otherwise be re-fetched
        hourly, however loudly its advert changed."""
        self.set_config(AUTO_REFETCH_INTERVAL_HOURS=24)
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"},
                  fetched_at=1000.0)
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})

        # An hour after the fetch: changed, but not yet stale enough.
        self.assertEqual(list_fetch.lists_worth_refetching(now=1000.0 + 3600), [])
        # Two days after: due.
        self.assertEqual(
            list_fetch.lists_worth_refetching(now=1000.0 + 48 * 3600),
            ["ReelBot"])

    def test_the_stalest_go_first_and_a_sweep_is_capped(self):
        """A bot back after a month offline has a lot of stale lists, and
        asking for all of them at once is a burst of outbound requests nobody
        asked for."""
        for index, bot in enumerate(("Oldest", "Middle", "Newest")):
            self.hold(bot, {"files": 1, "list_date": "Aug 1st"},
                      fetched_at=float(index))
            self.advertise(bot, {"files": 99, "list_date": "Sep 6th"})
        store = dict(getattr(config, "fetched_bot_lists", {}))
        self.set_config(fetched_bot_lists=store)

        due = list_fetch.lists_worth_refetching(now=10 ** 9)

        self.assertEqual(due, ["Oldest", "Middle", "Newest"])

    def test_nothing_is_asked_before_the_bot_has_joined_anything(self):
        """Reported live: the very first sweep ran from oserve.startup()
        before the IRC socket had even finished registering, let alone
        joined a channel - so the PRIVMSG it queued went out mid-handshake,
        into whatever channel happened to be first in config.CHANNEL rather
        than one the bot was actually in, and the target bot never saw it.
        Refusing outright while unsettled - the same gate dcc.py's own
        presence decisions already use - means the sweep simply waits for
        the activation hook (irc.delayed_activate()) instead of firing into
        a connection that is not ready for it."""
        self.set_config(bot_joined_channel=False)
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"})
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})

        started = list_fetch.refetch_due_lists(log=lambda *_a: None, now=10 ** 9)

        self.assertEqual(started, [])

    def test_a_sweep_asks_through_the_same_enqueue_the_dashboard_uses(self):
        """So the slot limits, the duplicate guard and the queue ceiling all
        apply exactly as they do to a fetch started by hand."""
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"})
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})
        calls = []
        import webserver
        real = webserver.build_list_fetch_enqueue_result

        def watched(payload):
            calls.append(payload)
            return real(payload)

        webserver.build_list_fetch_enqueue_result = watched
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real)

        started = list_fetch.refetch_due_lists(log=lambda *_a: None, now=10 ** 9)

        # The bot nick ITSELF, not a dict wrapping it (#535) -
        # build_list_fetch_enqueue_result(bot_raw) wants bot_raw to BE the
        # nick, exactly as the real HTTP route already calls it
        # (build_list_fetch_enqueue_result(body.get("bot", ""))). A dict here
        # failed that function's own isinstance(value, str) check on every
        # single call, silently disabling this feature entirely - this test
        # asserted the broken shape and so never caught it.
        self.assertEqual(calls, ["ReelBot"])
        # `started` is what the enqueue ACCEPTED. This fixture never
        # populates channel_users at all, which reads as "presence unknown"
        # rather than "bot absent" (see
        # tests/test_we_do_not_ask_a_bot_that_is_not_there.py's own docstring
        # on that distinction) - an unknown presence never refuses, so the
        # fetch goes through the same way an operator's own click would.
        self.assertEqual(started, ["ReelBot"])

    def test_a_reachable_bot_is_actually_re_fetched(self):
        """#535, end to end: the sibling test above only pinned the CALL
        SHAPE, and its fixture's bot was never reachable either way - so
        `started == []` there was true for two different reasons at once and
        could not tell them apart. Give the sweep a bot channel_users can
        actually see, and the fix means the fetch is now ACCEPTED rather
        than refused before it even reaches that check."""
        self.set_config(CHANNEL="#somechannel", MAX_FETCH_SLOTS=3)
        config.channel_users["#somechannel"] = {"reelbot"}
        self.hold("ReelBot", {"files": 100, "list_date": "Aug 1st"})
        self.advertise("ReelBot", {"files": 250, "list_date": "Sep 6th"})

        started = list_fetch.refetch_due_lists(log=lambda *_a: None, now=10 ** 9)

        self.assertEqual(started, ["ReelBot"])

    def test_a_run_is_capped_even_when_more_are_due(self):
        self.set_config(AUTO_REFETCH_MAX_PER_RUN=1)
        for index, bot in enumerate(("Oldest", "Middle")):
            self.hold(bot, {"files": 1, "list_date": "Aug 1st"},
                      fetched_at=float(index))
            self.advertise(bot, {"files": 99, "list_date": "Sep 6th"})
        store = dict(getattr(config, "fetched_bot_lists", {}))
        self.set_config(fetched_bot_lists=store)

        calls = []
        import webserver
        real = webserver.build_list_fetch_enqueue_result

        def watched(payload):
            calls.append(payload)
            return real(payload)

        webserver.build_list_fetch_enqueue_result = watched
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real)

        list_fetch.refetch_due_lists(log=lambda *_a: None, now=10 ** 9)

        # The cap bounds how many are ASKED FOR - which is the burst it exists
        # to prevent, whether or not the enqueue then accepts them. The bot
        # nick itself, not a dict wrapping it - see the sibling test above
        # and #535.
        self.assertEqual(calls, ["Oldest"])


class ActivationWakesTheSweepToo(unittest.TestCase):
    """Read from the source, the same way
    test_the_sweep_could_not_see_a_pm_requester.py's
    test_activation_runs_it_once_channel_users_is_trusted checks
    dcc.wake_restored_queues: delayed_activate is a closure inside irc_loop,
    and the call has to sit in the branch that just claimed channel sync,
    after the claim - the auto-refetch sweep refuses outright before that
    point (see refetch_due_lists()'s own guard), for the identical reason
    dcc.py's presence decisions do."""

    def body(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()
        block = source[source.index("def delayed_activate("):]
        return block[:block.index("def background_nick_monitor(")]

    def test_it_runs_after_channel_sync_is_claimed(self):
        body = self.body()

        claimed = body.index("config.bot_joined_channel = True")
        woken = body.index("threading.Thread(target=list_fetch.refetch_due_lists")
        unsynced = body.index("No channel members known yet")

        self.assertLess(claimed, woken, "the sweep runs before channel sync is claimed")
        self.assertLess(woken, unsynced, "the sweep is not inside the synced branch")

    def test_it_only_runs_when_the_feature_is_on(self):
        """A thread started unconditionally would import list_fetch and spin
        up a sweep even for an operator who never turned this on."""
        block = self.body()
        woken = block.index("dcc.wake_restored_queues")
        block = block[woken:block.index("threading.Thread(target=list_fetch.refetch_due_lists")]

        self.assertIn("AUTO_REFETCH_LISTS", block)

    def test_it_does_not_block_activation(self):
        """The same reasoning wake_restored_queues gets its own thread for:
        a slow read over every held list must not hold up the connection
        settling."""
        block = self.body()

        self.assertIn("threading.Thread(target=list_fetch.refetch_due_lists", block)


if __name__ == "__main__":
    unittest.main()
