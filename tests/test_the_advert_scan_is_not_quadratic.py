"""Recording an advert does not scan every channel member once per old entry.

_prune_known_bots() asked _bot_confirmed_absent() about each registry entry
older than a day, and that walked every nick in every channel - so one advert
cost (old entries) x (everybody present). Who is present is now worked out
once per pass, and only if an entry is old enough to need it. Counted, not
timed.
"""

import unittest

from tests.support import DCCoreTestCase

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402

T0 = 1_000_000.0
DAY = 24 * 60 * 60


class CountingSet(set):
    """A channel's members; counts every one walked."""

    walked = 0

    def __iter__(self):
        for member in super().__iter__():
            CountingSet.walked += 1
            yield member


class OnePassOneLook(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        CountingSet.walked = 0

    def test_many_old_entries_share_one_look_at_who_is_present(self):
        members = 2000
        config.channel_users["#big"] = CountingSet(f"member{n}" for n in range(members))
        for n in range(500):
            runtime.known_bots[f"gone{n}"] = {"nick": f"Gone{n}", "last_seen": T0 - DAY - 1}

        irc._prune_known_bots(T0)

        self.assertEqual(runtime.known_bots, {})
        self.assertLessEqual(CountingSet.walked, members, "one look per old entry is 500 times this")

    def test_nobody_is_looked_at_when_no_entry_is_old_enough_to_ask(self):
        config.channel_users["#big"] = CountingSet(f"member{n}" for n in range(100))
        for n in range(50):
            runtime.known_bots[f"fresh{n}"] = {"nick": f"Fresh{n}", "last_seen": T0 - 60}

        irc._prune_known_bots(T0)

        self.assertEqual(len(runtime.known_bots), 50)
        self.assertEqual(CountingSet.walked, 0)

    def test_the_answer_is_the_same_for_each_entry(self):
        config.channel_users["#chan"] = {"Herebot", "other"}
        runtime.known_bots["herebot"] = {"nick": "Herebot", "last_seen": T0 - DAY - 1}
        runtime.known_bots["gonebot"] = {"nick": "Gonebot", "last_seen": T0 - DAY - 1}

        irc._prune_known_bots(T0)

        self.assertEqual(list(runtime.known_bots), ["herebot"])


class ExpiryIsOncePerInterval(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        runtime.known_bots_flushed_at = T0

    def advert(self, nick, now):
        irc._capture_channel_advert(
            nick, "#chan", f"Type: @{nick} For My List Of: 1,234 Files List: Sep 1st", now=now)

    def old_entry(self, nick="gonebot"):
        config.channel_users["#chan"] = {"newbot", "newerbot"}
        runtime.known_bots[nick] = {"nick": nick, "last_seen": T0 - DAY - 1}

    def test_the_first_advert_expires_old_entries(self):
        self.old_entry()
        self.advert("newbot", T0)
        self.assertNotIn("gonebot", runtime.known_bots)

    def test_a_second_advert_inside_the_interval_does_not_look_again(self):
        self.advert("newbot", T0)
        self.old_entry()
        self.advert("newerbot", T0 + irc.KNOWN_BOTS_EXPIRY_INTERVAL_SECONDS - 1)
        self.assertIn("gonebot", runtime.known_bots)

    def test_an_advert_after_the_interval_does(self):
        self.advert("newbot", T0)
        self.old_entry()
        self.advert("newerbot", T0 + irc.KNOWN_BOTS_EXPIRY_INTERVAL_SECONDS)
        self.assertNotIn("gonebot", runtime.known_bots)

    def test_the_size_cap_is_not_throttled(self):
        original = irc.KNOWN_BOTS_MAX
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", original)
        irc.KNOWN_BOTS_MAX = 2
        self.advert("one", T0)
        self.advert("two", T0 + 1)
        self.advert("three", T0 + 2)
        self.assertEqual(len(runtime.known_bots), 2)


if __name__ == "__main__":
    unittest.main()
