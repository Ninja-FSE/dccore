"""Remembering joins costs the same per JOIN however many there were (#981).

irc.note_join_seen() runs for every JOIN in every channel we are in - it has
to: a bot back under a new nick is not a known bot yet when it joins, and that
join time is what #376's merge compares. Each call walked every join of the
last ten minutes to drop the old ones, under bot_idents_lock and on the read
loop, so a netjoin into big channels cost the square of its size: 10,000 joins,
4.5 seconds. Now the joins are kept oldest first and only stale ones are looked
at. Counted, not timed: a timing test is a bet on the machine it runs on.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import irc  # noqa: E402
import runtime  # noqa: E402

WINDOW = irc.IDENT_MERGE_WINDOW_SECONDS


class CountingDict(dict):
    """Counts every entry looked at: by key, or walked by items()/values()."""

    looked = 0

    def __getitem__(self, key):
        CountingDict.looked += 1
        return super().__getitem__(key)

    def items(self):
        for pair in super().items():
            CountingDict.looked += 1
            yield pair

    def values(self):
        for value in super().values():
            CountingDict.looked += 1
            yield value


class Joins(unittest.TestCase):
    def setUp(self):
        real = runtime.recent_joins
        runtime.recent_joins = CountingDict()
        CountingDict.looked = 0
        self.addCleanup(setattr, runtime, "recent_joins", real)

    def test_a_netjoin_costs_about_one_look_a_join(self):
        joins = 5000
        for n in range(joins):
            irc.note_join_seen(f"user{n}", now=1000.0 + n * 0.01)
        self.assertEqual(len(runtime.recent_joins), joins)
        self.assertLess(CountingDict.looked, 3 * joins, "a whole walk per join is the square of this")

    def test_old_joins_are_still_dropped(self):
        irc.note_join_seen("early", now=1000.0)
        irc.note_join_seen("middle", now=1000.0 + WINDOW / 2)
        irc.note_join_seen("late", now=1000.0 + WINDOW + 1)
        self.assertEqual(list(runtime.recent_joins), ["middle", "late"])

    def test_a_rejoin_is_as_new_as_it_is(self):
        """Put back at the end, so it is not dropped with the joins it came
        in with - and does not shelter older ones behind it."""
        irc.note_join_seen("returning", now=1000.0)
        irc.note_join_seen("other", now=1001.0)
        irc.note_join_seen("returning", now=1000.0 + WINDOW)
        irc.note_join_seen("later", now=1001.0 + WINDOW + 1)
        self.assertEqual(list(runtime.recent_joins), ["returning", "later"])
        self.assertEqual(runtime.recent_joins["returning"], 1000.0 + WINDOW)


if __name__ == "__main__":
    unittest.main()
