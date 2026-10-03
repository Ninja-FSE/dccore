"""A netsplit's rejoin costs the same per JOIN however many left.

Every JOIN asked note_possible_reconnect() whether it was a nick coming back
under a collision suffix, and that walked every departure of the last fifteen
seconds - twice, once to prune and once to match - so the rejoin after a
split of N users cost the square of N. The departures are now kept oldest
first and indexed by the bare nick a collision variant would be a retry of, so
a JOIN looks at the few it could possibly match. Counted, not timed: a timing
test is a bet on the machine it runs on.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import irc  # noqa: E402
import runtime  # noqa: E402

WINDOW = irc.ALT_NICK_RECONNECT_WINDOW_SECONDS


class CountingDict(dict):
    """Counts every entry looked at: by key, or walked by items()/values()/copy()."""

    looked = 0

    def __getitem__(self, key):
        CountingDict.looked += 1
        return super().__getitem__(key)

    def get(self, key, default=None):
        CountingDict.looked += 1
        return super().get(key, default)

    def items(self):
        for pair in super().items():
            CountingDict.looked += 1
            yield pair

    def values(self):
        for value in super().values():
            CountingDict.looked += 1
            yield value

    def copy(self):
        CountingDict.looked += len(self)
        return CountingDict(self)


class TheRejoinAfterASplit(unittest.TestCase):
    def setUp(self):
        real = runtime.recent_departures
        runtime.recent_departures = CountingDict()
        CountingDict.looked = 0
        self.addCleanup(setattr, runtime, "recent_departures", real)
        self.addCleanup(runtime.recent_departure_bases.clear)

    def test_each_join_looks_at_a_few_departures_not_all_of_them(self):
        users = 3000
        for n in range(users):
            irc.note_observed_departure(f"user{n}x", "#c", now=1000.0 + n * 0.001)
        CountingDict.looked = 0
        for n in range(users):
            irc.note_possible_reconnect(f"other{n}y", now=1000.0 + users * 0.001 + n * 0.001)
        self.assertLess(CountingDict.looked, 5 * users, "a whole walk per join is the square of this")

    def test_a_split_with_the_same_base_nick_many_times_is_still_cheap_for_unrelated_joins(self):
        for n in range(3000):
            irc.note_observed_departure(f"person{n}", "#c", now=1000.0)
        CountingDict.looked = 0
        for n in range(3000):
            irc.note_possible_reconnect(f"stranger{n}", now=1001.0)
        self.assertLess(CountingDict.looked, 5 * 3000)


class TheMatchingIsUnchanged(unittest.TestCase):
    def setUp(self):
        self.addCleanup(runtime.recent_departures.clear)
        self.addCleanup(runtime.recent_departure_bases.clear)
        self.addCleanup(runtime.nick_aliases.clear)

    def test_a_suffixed_nick_matches_the_bare_one_that_left(self):
        irc.note_observed_departure("SomeBot", "#c", now=1000.0)
        self.assertEqual(irc.note_possible_reconnect("somebot_", now=1001.0), "SomeBot")

    def test_the_bare_nick_matches_a_suffixed_one_that_left(self):
        irc.note_observed_departure("SomeBot_", "#c", now=1000.0)
        self.assertEqual(irc.note_possible_reconnect("somebot", now=1001.0), "SomeBot_")

    def test_two_suffixed_nicks_do_not_match(self):
        irc.note_observed_departure("bot1", "#c", now=1000.0)
        self.assertIsNone(irc.note_possible_reconnect("bot2", now=1001.0))

    def test_a_leading_digit_is_not_a_suffix(self):
        irc.note_observed_departure("bot", "#c", now=1000.0)
        self.assertIsNone(irc.note_possible_reconnect("2bot", now=1001.0))

    def test_the_oldest_of_several_matching_departures_wins(self):
        irc.note_observed_departure("bot_", "#c", now=1000.0)
        irc.note_observed_departure("bot1", "#c", now=1001.0)
        self.assertEqual(irc.note_possible_reconnect("bot", now=1002.0), "bot_")

    def test_a_matched_departure_is_used_up_and_leaves_no_index_behind(self):
        irc.note_observed_departure("somebot_", "#c", now=1000.0)
        irc.note_possible_reconnect("somebot", now=1001.0)
        self.assertEqual(runtime.recent_departures, {})
        self.assertEqual(runtime.recent_departure_bases, {})

    def test_an_old_departure_is_pruned_with_its_index(self):
        irc.note_observed_departure("somebot_", "#c", now=1000.0)
        irc._prune_recent_departures(1000.0 + WINDOW + 1)
        self.assertEqual(runtime.recent_departures, {})
        self.assertEqual(runtime.recent_departure_bases, {})

    def test_leaving_twice_is_remembered_once_and_as_new_as_the_second_time(self):
        irc.note_observed_departure("somebot_", "#c", now=1000.0)
        irc.note_observed_departure("other", "#c", now=1001.0)
        irc.note_observed_departure("somebot_", "#c", now=1000.0 + WINDOW)
        irc._prune_recent_departures(1001.0 + WINDOW + 1)
        self.assertEqual(list(runtime.recent_departures), ["somebot_"])
        self.assertEqual(runtime.recent_departure_bases, {"somebot": {"somebot_": None}})

    def test_rejoining_under_its_own_name_forgets_the_departure_and_its_index(self):
        irc.note_observed_departure("somebot_", "#c", now=1000.0)
        self.assertIsNone(irc.note_possible_reconnect("somebot_", now=1001.0))
        self.assertEqual(runtime.recent_departures, {})
        self.assertEqual(runtime.recent_departure_bases, {})


if __name__ == "__main__":
    unittest.main()
