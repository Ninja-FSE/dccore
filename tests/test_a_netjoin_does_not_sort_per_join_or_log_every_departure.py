"""A netjoin does not sort the chat rate table once per JOIN, and an ordinary
user's departure is not a console line.

#1145, two small costs on the JOIN/PART/QUIT path:

  * serverschat.note_join() keeps a rate record per stranger who joins. Once
    more than _TRACK_MAX of them had joined inside one window - a netjoin -
    _prune() trimmed the table back to exactly _TRACK_MAX, so EVERY following
    JOIN filtered and sorted all 200 entries under chat_lock to drop one. It
    now trims to _TRACK_KEEP, three quarters of the cap, so the next quarter's
    worth of JOINs do neither. Forgetting a stranger's count early only lets
    one more WHO through, and the JOIN-WHO cap still bounds those.

  * note_gone() printed a line for every PART and QUIT of somebody who was
    never a DCCore Chat peer - nearly all of them - and that line became most
    of the console log, rotating half a day of diagnostics away at a few
    departures a second. It now prints only under DEBUG_MODE; a known peer's
    departure is still said.
"""

import contextlib
import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import runtime  # noqa: E402
import serverschat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CHAN = "#somechannel"


class Case(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", DEBUG_MODE=False)
        self.oserve = sys.modules.pop("oserve", None)
        self.addCleanup(lambda: self.oserve and sys.modules.__setitem__("oserve", self.oserve))
        for table in (runtime.chat_rate, runtime.chat_peers, runtime.chat_muted):
            saved = dict(table)
            table.clear()
            self.addCleanup(table.update, saved)
            self.addCleanup(table.clear)


class ANetjoinDoesNotSortPerJoin(Case):

    def setUp(self):
        super().setUp()
        self.sorts = 0

        def counting_sorted(*args, **kwargs):
            self.sorts += 1
            return sorted(*args, **kwargs)

        # The module-level name shadows the builtin for serverschat only.
        serverschat.sorted = counting_sorted
        self.addCleanup(delattr, serverschat, "sorted")

    def strangers(self):
        return [k for k in runtime.chat_rate if k.startswith("who:")]

    def test_a_burst_of_strangers_sorts_once_per_quarter_of_the_cap(self):
        joins = serverschat._TRACK_MAX * 10
        for n in range(joins):
            serverschat.note_join(f"Stranger{n}", CHAN)
        step = serverschat._TRACK_MAX - serverschat._TRACK_KEEP
        self.assertGreater(self.sorts, 0, "the burst must have reached the cap")
        self.assertLessEqual(self.sorts, joins // step + 1)
        self.assertLessEqual(len(runtime.chat_rate), serverschat._TRACK_MAX + 2)

    def test_a_trim_keeps_the_newest_and_goes_down_to_the_keep_size(self):
        # One over the cap, every window still open (so none is stale).
        now = time.time() + 1
        fillers = [f"who:filler{n}" for n in range(serverschat._TRACK_MAX + 1)]
        for n, key in enumerate(fillers):
            runtime.chat_rate[key] = [now + n * 0.001, 1]
        serverschat.note_join("OneMore", CHAN)
        self.assertEqual(self.sorts, 1)
        # Deep enough to stop a sort per JOIN, shallow enough to forget only a
        # quarter of the counts: forgetting one only lets one more WHO out.
        self.assertEqual(serverschat._TRACK_KEEP, serverschat._TRACK_MAX * 3 // 4)
        kept = self.strangers()
        # _TRACK_KEEP survive the trim - the newest - and then OneMore's own
        # record is added.
        self.assertEqual(sorted(kept),
                         sorted(fillers[-serverschat._TRACK_KEEP:] + ["who:onemore"]))
        # ...and the next quarter of the cap's JOINs neither sort nor drop.
        for n in range(serverschat._TRACK_MAX - serverschat._TRACK_KEEP - 1):
            serverschat.note_join(f"Later{n}", CHAN)
        self.assertEqual(self.sorts, 1)
        self.assertEqual(len(self.strangers()), serverschat._TRACK_MAX)

    def test_below_the_cap_nothing_is_sorted_or_dropped(self):
        for n in range(serverschat._TRACK_MAX - 1):
            serverschat.note_join(f"Stranger{n}", CHAN)
        self.assertEqual(self.sorts, 0)
        self.assertEqual(len(self.strangers()), serverschat._TRACK_MAX - 1)

    def test_the_join_who_cap_survives_the_trims(self):
        """Trimming deeper must not take the all-strangers WHO cap with it."""
        for n in range(serverschat._TRACK_MAX * 3):
            serverschat.note_join(f"Stranger{n}", CHAN)
        self.assertGreater(self.sorts, 1)
        self.assertIn(serverschat._JOIN_WHO_ALL, runtime.chat_rate)
        queued = (self.config.send_queue or {}).get(serverschat.JOIN_WHO_QUEUE, [])
        self.assertEqual(len(queued), serverschat.JOIN_WHO_MOST)


class AnOrdinaryDepartureIsNotAConsoleLine(Case):

    def gone(self, nick, chan=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            serverschat.note_gone(nick, chan)
        return buffer.getvalue()

    def test_a_stranger_leaving_prints_nothing(self):
        self.assertEqual(self.gone("dave", CHAN), "")
        self.assertEqual(self.gone("dave"), "")

    def test_under_debug_mode_it_is_still_said(self):
        self.set_config(DEBUG_MODE=True)
        self.assertIn("was not a known DCCore Chat peer", self.gone("dave", CHAN))
        self.assertIn("the network", self.gone("dave"))

    def test_a_known_peer_leaving_is_still_said(self):
        runtime.chat_peers["otherbot"] = {CHAN: time.time()}
        said = self.gone("OtherBot", CHAN)
        self.assertIn("otherbot is gone from DCCore Chat peers", said)
        self.assertNotIn("otherbot", runtime.chat_peers)


if __name__ == "__main__":
    unittest.main()
