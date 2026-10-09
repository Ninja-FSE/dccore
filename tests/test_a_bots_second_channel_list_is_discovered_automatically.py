"""#1240: AUTO_DISCOVER_CHANNEL_LISTS watches bots we already hold a list
from for a SECOND, genuinely different list bound to another of our
channels, and fetches it automatically - but only once the difference has
held steady for MULTI_CHANNEL_LIST_STABLE_SECONDS on both sides, never off a
single advert (a bot mid-scan in one channel must not be mistaken for a
second list).
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_grab  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

NOW = 1_000_000.0
STABLE = 3600


def channels(chans):
    """{channel: {"files": N, "since": T}, ...} - the shape known_bots[...]
    ["channels"] holds (irc._record_channel_signature()'s own output), keys
    lowercased the way that function stores them."""
    return {str(chan).lower(): dict(info) for chan, info in chans.items()}


class Case(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(AUTO_DISCOVER_CHANNEL_LISTS=True,
                        MULTI_CHANNEL_LIST_STABLE_SECONDS=STABLE,
                        fetched_bot_lists={})
        runtime.secondary_channel_tries = {}
        runtime.secondary_channel_last = None
        self.asked = []
        self.answer = (200, {"created": ["rid"]})

        def fake_enqueue(bot, channel, secondary_raw=False):
            self.asked.append((bot, channel))
            self.assertTrue(secondary_raw, "the discovery tick must always say secondary_raw=True")
            return self.answer

        real = webserver.build_list_fetch_enqueue_result
        webserver.build_list_fetch_enqueue_result = fake_enqueue
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real)

    def hold(self, bot, channel, **lists_extra):
        key = bot.lower()
        config.fetched_bot_lists[key] = {
            "bot": bot, "channel": channel,
            "lists": {"": {"list_path": "x", "entry_count": 1, "channel": channel}, **lists_extra},
        }

    def register(self, bot, chans):
        runtime.known_bots[bot.lower()] = {"nick": bot, "channels": channels(chans)}
        # The bot must be PRESENT, not just once advertised (#1240 review):
        # _secondary_channel_candidates() now checks dcc_fetch.bot_in_our_
        # channel() for every candidate channel, since channels[...] is
        # never pruned and a channel the bot left keeps its last advert on
        # record indefinitely. Every test here means "the bot is there" by
        # registering a channel at all, so this is the one place to say so.
        for chan in chans:
            config.channel_users.setdefault(str(chan).strip().lower(), set()).add(bot.lower())


class Candidates(Case):

    def test_nothing_without_a_held_list(self):
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_genuine_stable_difference_is_found(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [("somebot", "SomeBot", "#chan_b")])

    def test_the_same_file_count_everywhere_is_not_a_candidate(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 100, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_difference_not_yet_stable_on_the_new_channel_is_not_a_candidate(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - 10}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_held_channel_itself_mid_change_is_not_compared_against(self):
        """The channel we already hold a list from is ALSO mid-scan (its own
        signature just changed) - comparing anything against a moving target
        is as wrong as comparing a moving target against us."""
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 150, "since": NOW - 10}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_channel_already_held_as_a_marker_is_not_offered_again(self):
        self.hold("SomeBot", "#chan_a",
                 video={"list_path": "y", "entry_count": 1, "channel": "#chan_b"})
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_only_one_channel_is_never_a_candidate(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_with_no_signature_for_the_held_channel_nothing_qualifies(self):
        """The bot has never advertised in the channel we actually hold its
        list from (fetched by hand, say): two real incidents (two different
        bots, each genuinely serving the SAME list in more than one channel)
        showed why a concrete count elsewhere must NOT be taken as "new" in
        this case - with no real baseline, a channel serving the identical
        content under a different name looks exactly as promising as one
        that truly differs, and gets fetched and stored as a duplicate. Wait
        for the primary's own channel to land on record instead - its next
        ordinary refresh does that for free."""
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [])

    def test_a_cooled_down_pair_is_skipped(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        runtime.secondary_channel_tries = {"somebot:#chan_b": {"tries": 1, "last": NOW - 60}}
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_pair_that_gave_up_is_skipped(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        runtime.secondary_channel_tries = {
            "somebot:#chan_b": {"tries": list_grab.SECONDARY_CHANNEL_TRIES, "last": NOW - 10 ** 6}}
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])


class WithNoPrimarySignatureNothingIsEverDiscovered(Case):
    """Two real incidents, each on a bot with no recorded primary channel,
    each genuinely serving the SAME list from more than one channel: with
    no baseline, a channel showing the identical content under a different
    name is indistinguishable from one that genuinely differs, so NOTHING
    is offered until the primary's own channel lands on record - its
    ordinary refresh cycle does that for free, with no code change needed
    here.
    """

    def test_a_concrete_but_matching_count_is_not_offered(self):
        self.hold("SomeBot", None)
        self.register("SomeBot", {"#chan_a": {"files": 500, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_several_channels_with_no_primary_signature_offer_nothing(self):
        """The exact live shape: a bot advertising several channels, all
        genuinely the same list, none of them the recorded primary."""
        self.hold("SomeBot", None)
        self.register("SomeBot", {"#chan_a": {"files": 500, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 500, "since": NOW - STABLE * 2},
                                  "#chan_c": {"files": 500, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_once_the_primary_channel_is_on_record_discovery_resumes(self):
        """The resolution: an ordinary refresh (AUTO_REFETCH_LISTS, or a
        manual one) backfills entry["channel"] the same way any primary
        fetch already does - after that, a GENUINE difference is found
        exactly as it would be for any other bot."""
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 500, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 900, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [("somebot", "SomeBot", "#chan_b")])


class TwoChannelsWithTheSameContentAreNotBothFetched(Case):
    """A real incident, hit live: a bot with no recorded primary channel (an
    entry held before #1232 existed) advertised the identical huge file
    count in two OTHER shared channels - with no primary signature to
    compare against, both independently looked "different" and were each
    fetched and stored as their own marker, duplicating the exact same
    content under two names. The fix: once one of them is discovered,
    the other must be compared against IT too, not only against the
    (unknown) primary.
    """

    def test_a_channel_already_discovered_blocks_a_matching_new_one(self):
        """The real sequence (secondary_channel_tick() only ever acts on ONE
        candidate per tick, so this is how it actually played out live):
        #chan_a discovered and held first; #chan_b, advertising the SAME
        count, must not then also be fetched on the next tick's scan - a
        single scan with NEITHER yet discovered cannot de-duplicate them
        against each other (nothing is held yet to compare against), but
        that is fine, because only one of them is ever acted on per tick
        anyway, and by the next scan the first is in already_held."""
        self.hold("SomeBot", None,
                 video={"list_path": "y", "entry_count": 500, "channel": "#chan_a",
                        "advert_signature": {"files": 500, "since": NOW - STABLE * 2}})
        self.register("SomeBot", {"#chan_a": {"files": 500, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 500, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [])

    def test_a_channel_already_discovered_still_allows_a_genuinely_different_one(self):
        self.hold("SomeBot", None,
                 video={"list_path": "y", "entry_count": 500, "channel": "#chan_a",
                        "advert_signature": {"files": 500, "since": NOW - STABLE * 2}})
        self.register("SomeBot", {"#chan_a": {"files": 500, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 900, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [("somebot", "SomeBot", "#chan_b")])

    def test_signature_has_content_rejects_an_empty_placeholder(self):
        self.assertFalse(list_grab._signature_has_content({}))
        self.assertFalse(list_grab._signature_has_content(None))
        self.assertTrue(list_grab._signature_has_content({"files": 0}))
        self.assertTrue(list_grab._signature_has_content({"list_date": "Oct 7th"}))


class RefreshingAnAlreadyDiscoveredChannel(Case):
    """Not just discovery once - an already-held secondary marker is offered
    again when ITS OWN channel has genuinely moved on since it was fetched,
    the ongoing upkeep #1240 promises (no manual re-fetch, ever)."""

    def hold_with_video(self, signature_when_fetched):
        self.hold("SomeBot", "#chan_a",
                 video={"list_path": "y", "entry_count": 1, "channel": "#chan_b",
                        "advert_signature": dict(signature_when_fetched)})

    def test_unchanged_since_the_marker_was_fetched_is_not_offered_again(self):
        sig = {"files": 200, "since": NOW - STABLE * 3}
        self.hold_with_video(sig)
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2},
                                  "#chan_b": dict(sig)})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_stable_change_since_the_marker_was_fetched_is_offered_again(self):
        self.hold_with_video({"files": 200, "since": NOW - STABLE * 10})
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 350, "since": NOW - STABLE * 2}})
        found = list_grab._secondary_channel_candidates(NOW)
        self.assertEqual(found, [("somebot", "SomeBot", "#chan_b")])

    def test_a_change_not_yet_stable_is_not_offered_again_yet(self):
        self.hold_with_video({"files": 200, "since": NOW - STABLE * 10})
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2},
                                  "#chan_b": {"files": 350, "since": NOW - 10}})
        self.assertEqual(list_grab._secondary_channel_candidates(NOW), [])

    def test_a_successful_refetch_resets_its_own_tries(self):
        list_grab.note_secondary_channel_list_arrived("SomeBot", "#chan_b")  # no-op, nothing to reset
        runtime.secondary_channel_tries = {"somebot:#chan_b": {"tries": 3, "last": NOW}}
        list_grab.note_secondary_channel_list_arrived("SomeBot", "#chan_b")
        self.assertNotIn("somebot:#chan_b", runtime.secondary_channel_tries)


class Tick(Case):

    def test_off_does_nothing(self):
        self.set_config(AUTO_DISCOVER_CHANNEL_LISTS=False)
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab.secondary_channel_tick(NOW), "off")
        self.assertEqual(self.asked, [])

    def test_a_confirmed_candidate_is_asked_for(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.assertEqual(list_grab.secondary_channel_tick(NOW), "asked")
        self.assertEqual(self.asked, [("SomeBot", "#chan_b")])

    def test_a_refusal_is_not_asked_again_the_same_tick(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.answer = (409, {"error": "busy"})
        self.assertEqual(list_grab.secondary_channel_tick(NOW), "refused")

    def test_nothing_to_find_says_so(self):
        self.assertEqual(list_grab.secondary_channel_tick(NOW), "nothing")

    def test_a_second_tick_too_soon_waits(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        list_grab.secondary_channel_tick(NOW)
        self.assertEqual(list_grab.secondary_channel_tick(NOW + 1), "waiting")

    def test_three_genuine_asks_then_it_gives_up(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        t = NOW
        for _ in range(list_grab.SECONDARY_CHANNEL_TRIES):
            self.assertEqual(list_grab.secondary_channel_tick(t), "asked")
            t += list_grab.SECONDARY_CHANNEL_COOLDOWN_SECONDS + 1
        self.assertEqual(list_grab.secondary_channel_tick(t), "nothing")

    def test_a_persistent_local_refusal_never_exhausts_the_tries_cap(self):
        """#1240 review: a 409 means NOTHING was actually asked of the bot -
        it must not spend one of the three tries, or a transient local
        conflict (busy with another list/folder already, or the bot
        momentarily absent) could leave a real, undiscovered second list
        stuck forever for a reason that never involved asking the bot at
        all. `record["last"]` still paces the retry via the ordinary
        cooldown, so this never means asking every single tick either."""
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        self.answer = (409, {"error": "busy"})
        t = NOW
        for _ in range(list_grab.SECONDARY_CHANNEL_TRIES + 2):
            self.assertEqual(list_grab.secondary_channel_tick(t), "refused")
            t += list_grab.SECONDARY_CHANNEL_COOLDOWN_SECONDS + 1
        self.assertEqual(list_grab._secondary_channel_state().get("somebot:#chan_b", {}).get("tries", 0), 0)

    def test_tries_survive_a_restart(self):
        self.hold("SomeBot", "#chan_a")
        self.register("SomeBot", {"#chan_a": {"files": 100, "since": NOW - STABLE * 2}, "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})
        list_grab.secondary_channel_tick(NOW)
        runtime.secondary_channel_tries = None  # simulate a fresh process
        self.assertEqual(list_grab._secondary_channel_state()["somebot:#chan_b"]["tries"], 1)


class EndToEndThroughTheRealEnqueueFunction(DCCoreTestCase):
    """secondary_channel_tick() through the REAL
    webserver.build_list_fetch_enqueue_result() - not the fake Case installs
    for the tests above - so a signature mismatch between the two (#1240's
    actual first live bug: the channel parameter was dropped by #1239's
    revert and nothing caught it, since every other test here mocks this
    exact call away) fails loudly instead of only in production."""

    def setUp(self):
        super().setUp()
        self.set_config(AUTO_DISCOVER_CHANNEL_LISTS=True,
                        MULTI_CHANNEL_LIST_STABLE_SECONDS=STABLE,
                        fetched_bot_lists={}, fetch_queue={},
                        fetch_feature_disabled=False, bot_joined_channel=True)
        runtime.secondary_channel_tries = {}
        runtime.secondary_channel_last = None
        config.channel_users.clear()
        config.channel_users["#chan_a"] = {"somebot"}
        config.channel_users["#chan_b"] = {"somebot"}

    def test_a_confirmed_candidate_is_really_enqueued(self):
        config.fetched_bot_lists["somebot"] = {
            "bot": "SomeBot", "channel": "#chan_a",
            "lists": {"": {"list_path": "x", "entry_count": 1, "channel": "#chan_a"}},
        }
        runtime.known_bots["somebot"] = {"nick": "SomeBot", "channels": channels(
            {"#chan_a": {"files": 100, "since": NOW - STABLE * 2},
             "#chan_b": {"files": 200, "since": NOW - STABLE * 2}})}

        result = list_grab.secondary_channel_tick(NOW)

        self.assertEqual(result, "asked")
        rows = [row for row in config.fetch_queue.values() if row.get("request_type") == "list"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "#chan_b")


class TheWiring(unittest.TestCase):

    def setUp(self):
        self.real_flag = config.AUTO_DISCOVER_CHANNEL_LISTS
        self.real_started = runtime.secondary_channel_started
        self.addCleanup(setattr, config, "AUTO_DISCOVER_CHANNEL_LISTS", self.real_flag)
        self.addCleanup(setattr, runtime, "secondary_channel_started", self.real_started)

    def test_ensure_worker_is_off_by_default(self):
        config.AUTO_DISCOVER_CHANNEL_LISTS = False
        runtime.secondary_channel_started = False
        self.assertFalse(list_grab.ensure_secondary_channel_worker())

    def test_ensure_worker_starts_once(self):
        config.AUTO_DISCOVER_CHANNEL_LISTS = True
        runtime.secondary_channel_started = False
        started = []
        self.assertTrue(list_grab.ensure_secondary_channel_worker(start=lambda: started.append(1)))
        self.assertFalse(list_grab.ensure_secondary_channel_worker(start=lambda: started.append(1)))
        self.assertEqual(started, [1])

    def test_the_loop_survives_a_tick_that_raises(self):
        real = list_grab.secondary_channel_tick

        def explode(*_a, **_k):
            raise RuntimeError("boom")

        list_grab.secondary_channel_tick = explode
        self.addCleanup(setattr, list_grab, "secondary_channel_tick", real)

        class Enough(Exception):
            pass

        def stop(_seconds):
            raise Enough()

        with self.assertRaises(Enough):
            list_grab.secondary_channel_worker(sleep=stop)

    def test_boot_and_rehash_start_it(self):
        with open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            self.assertIn("ensure_secondary_channel_worker()", handle.read())
        with open(os.path.join(REPO_ROOT, "src", "commands.py"), encoding="utf-8") as handle:
            self.assertIn("if _list_grab_secondary.ensure_secondary_channel_worker():", handle.read())


if __name__ == "__main__":
    unittest.main()
