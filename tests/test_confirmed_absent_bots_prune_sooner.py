"""A confirmed-absent bot is pruned from the advert registry sooner than a
merely-quiet one.

Reported against the live List Browser: a sidebar full of bots showing the
red "not in a channel" dot, and a purge button that had nothing to purge -
because that button only ever touches config.fetched_bot_lists (issue #385),
and every one of those red rows was an advert-only "not downloaded" entry in
runtime.known_bots instead. That store already had its own housekeeping -
_prune_known_bots(), a flat KNOWN_BOTS_TTL_SECONDS (a week) - but it read only
"last_seen", never presence, so a bot that had plainly left days ago sat in
the registry for most of a week regardless of the dot already saying so.

THE TWO SIGNALS STAY INDEPENDENT

Going quiet (no advert for a while) and being confirmed absent (not in any
channel we share, right now) are different claims. A bot that is still in
the channel but has not advertised in three days keeps the full week - the
dot says nothing is wrong with it. A bot _bot_confirmed_absent() can actually
vouch for is pruned after KNOWN_BOTS_ABSENT_TTL_SECONDS (a day) instead.

"CONFIRMED" IS THE OPERATIVE WORD

An empty config.channel_users - still joining, in the settle window right
after a (re)connect - must never read as absence, or every known bot would
be pruned within a day of every restart. This mirrors webserver.
present_nicks()'s identical "empty means unknown, not nobody there" rule.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

T0 = 1_000_000.0
DAY = 24 * 60 * 60
WEEK = 7 * DAY


class PruningTestCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)

    def seed(self, nick, last_seen):
        runtime.known_bots[nick.lower()] = {
            "nick": nick, "last_seen": last_seen, "files": 100}

    def present(self, channel, *nicks):
        config.channel_users[channel] = set(nicks)


class ABotConfirmedAbsentIsPrunedSooner(PruningTestCase):

    def test_confirmed_absent_past_the_short_ttl_is_pruned(self):
        self.present("#chan", "someoneelse")
        self.seed("gonebot", T0 - DAY - 1)

        irc._prune_known_bots(T0)

        self.assertNotIn("gonebot", runtime.known_bots)

    def test_confirmed_absent_but_not_past_the_short_ttl_yet_survives(self):
        self.present("#chan", "someoneelse")
        self.seed("recentlygonebot", T0 - DAY + 60)

        irc._prune_known_bots(T0)

        self.assertIn("recentlygonebot", runtime.known_bots)

    def test_still_present_keeps_the_full_week_even_past_the_short_ttl(self):
        """Quiet is not the same claim as gone - a bot the dot still shows
        green must not be swept just because it stopped advertising."""
        self.present("#chan", "quietbot")
        self.seed("quietbot", T0 - DAY - 1)

        irc._prune_known_bots(T0)

        self.assertIn("quietbot", runtime.known_bots)

    def test_still_present_is_pruned_once_it_passes_the_full_week_anyway(self):
        """The long TTL is not repealed - it is a ceiling neither signal can
        talk its way past forever."""
        self.present("#chan", "eversilentbot")
        self.seed("eversilentbot", T0 - WEEK - 1)

        irc._prune_known_bots(T0)

        self.assertNotIn("eversilentbot", runtime.known_bots)

    def test_an_empty_channel_users_never_counts_as_confirmed_absent(self):
        """The settle window right after a restart: nothing has been
        confirmed one way or the other yet, so nothing may be pruned early
        on that account. self.present() is deliberately not called here."""
        self.assertEqual(dict(config.channel_users), {})
        self.seed("unluckytimingbot", T0 - DAY - 1)

        irc._prune_known_bots(T0)

        self.assertIn("unluckytimingbot", runtime.known_bots)

    def test_a_channel_with_nobody_in_it_is_the_same_as_no_channel_at_all(self):
        """Joined, but the NAMES reply has not arrived - present_nicks()'s
        own distinction in webserver.py, mirrored here."""
        config.channel_users["#chan"] = set()
        self.seed("stillsettlingbot", T0 - DAY - 1)

        irc._prune_known_bots(T0)

        self.assertIn("stillsettlingbot", runtime.known_bots)

    def test_eviction_by_the_max_cap_is_unaffected(self):
        """The two-TTL change touches only WHICH entries are time-pruned,
        not the separate count cap below it - a regression here would be
        easy to miss since both prunes run back to back."""
        original_max = irc.KNOWN_BOTS_MAX
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", original_max)
        irc.KNOWN_BOTS_MAX = 2

        self.present("#chan", "a", "b", "c")
        self.seed("a", T0 - 30)
        self.seed("b", T0 - 20)
        self.seed("c", T0 - 10)

        irc._prune_known_bots(T0)

        self.assertEqual(len(runtime.known_bots), 2)
        self.assertNotIn("a", runtime.known_bots,
                          "eviction is by last_seen ascending - the oldest "
                          "of the three must be what goes")


class BotConfirmedAbsentDirectly(PruningTestCase):
    """irc._bot_confirmed_absent() in isolation."""

    def test_true_when_the_key_is_in_no_channel_we_share(self):
        self.present("#chan", "someoneelse")

        self.assertTrue(irc._bot_confirmed_absent("gonebot"))

    def test_false_when_present(self):
        self.present("#chan", "herebot")

        self.assertFalse(irc._bot_confirmed_absent("herebot"))

    def test_false_when_channel_users_is_completely_empty(self):
        self.assertEqual(dict(config.channel_users), {})

        self.assertFalse(irc._bot_confirmed_absent("anybot"))

    def test_false_when_every_known_channel_is_empty(self):
        config.channel_users["#chan"] = set()
        config.channel_users["#other"] = set()

        self.assertFalse(irc._bot_confirmed_absent("anybot"))

    def test_true_once_at_least_one_channel_has_a_confirmed_roster(self):
        """Mixed state: one channel still settling, another already synced.
        The synced one is enough to make an absence claim."""
        config.channel_users["#stillsettling"] = set()
        config.channel_users["#synced"] = {"someoneelse"}

        self.assertTrue(irc._bot_confirmed_absent("gonebot"))

    def test_the_comparison_is_case_insensitive(self):
        """The key is already lower-cased by the caller - see
        _known_bot_is_stale()'s docstring - but channel_users holds nicks in
        whatever case the server sent, so this side of the comparison must
        fold too."""
        self.present("#chan", "HereBot")

        self.assertFalse(irc._bot_confirmed_absent("herebot"))


class WiredIntoARealCapture(PruningTestCase):
    """_prune_known_bots() is never called directly in production - it runs
    as a side effect of _capture_channel_advert() recording SOME bot's
    advert. One test exercising that real entry point, so the two-TTL logic
    above is not only proven in isolation."""

    def setUp(self):
        super().setUp()
        runtime.known_bots_flushed_at = T0

    def test_a_fresh_advert_prunes_a_confirmed_absent_bot_in_the_same_pass(self):
        self.present("#DCCore-Test", "newbot")
        runtime.known_bots["gonebot"] = {
            "nick": "GoneBot", "last_seen": T0 - DAY - 1}

        irc._capture_channel_advert(
            "newbot", "#DCCore-Test",
            "Type: @newbot For My List Of: 1,234 Files List: Sep 1st", now=T0)

        self.assertIn("newbot", runtime.known_bots)
        self.assertNotIn("gonebot", runtime.known_bots)


if __name__ == "__main__":
    unittest.main()
