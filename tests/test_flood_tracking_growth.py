"""Audit #162 finding 30: the flood tracker never forgot anybody.

`config.user_requests` and `config.muted_until` are keyed by a nick anybody can
choose, and both grew one permanent entry per distinct nick:

  * user_requests prunes the timestamps INSIDE an entry to REQUEST_WINDOW on
    every call, but never removes the key, so a nick that made one request
    months ago still owns an empty list;
  * muted_until does delete an expired entry - but only on the next request
    from that same nick, and a flooder who was just muted is precisely the
    person who does not come back.

commands.py's PRESERVE_RUNTIME carries both across every !rehash, correctly,
so a restart was the only thing that ever cleared them. Measured at roughly
150-380 bytes a nick, which is ~15 MB/day under sustained nick-cycling.

The interesting half of this is the CONTROLS. An eviction that also drops a
live mute would turn a memory fix into a flood-protection hole: mute somebody,
cycle nicks until the sweep runs, come back clean. Those tests matter more
than the ones proving the leak is closed.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import security  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class FloodCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(MAX_REQUESTS=10, REQUEST_WINDOW=5, MUTE_TIME=30)
        config.user_requests.clear()
        config.muted_until.clear()
        self.addCleanup(config.user_requests.clear)
        self.addCleanup(config.muted_until.clear)
        # Module state, so it survives between tests and would make whichever
        # ran second see a throttled sweep.
        security._last_flood_sweep = 0.0
        self.addCleanup(setattr, security, "_last_flood_sweep", 0.0)


class TheSweepForgetsWhatCannotMatterAnyMore(FloodCase):

    def test_a_stale_request_history_is_dropped(self):
        now = time.time()
        config.user_requests["ghost"] = [now - 600]

        security._prune_flood_tracking(now)

        self.assertNotIn("ghost", config.user_requests)

    def test_an_emptied_history_is_dropped(self):
        """The exact shape the leak left behind: the timestamps were pruned to
        nothing on some earlier call and the key stayed."""
        config.user_requests["ghost"] = []

        security._prune_flood_tracking(time.time())

        self.assertNotIn("ghost", config.user_requests)

    def test_an_expired_mute_is_dropped(self):
        now = time.time()
        config.muted_until["ghost"] = now - 1

        security._prune_flood_tracking(now)

        self.assertNotIn("ghost", config.muted_until)

    def test_it_reports_what_it_dropped(self):
        """A sweep that runs and removes nothing looks identical from outside
        to one that never ran, so the count is the only observable."""
        now = time.time()
        config.user_requests["a"] = [now - 600]
        config.muted_until["b"] = now - 1

        self.assertEqual(security._prune_flood_tracking(now), 2)

    def test_thousands_of_departed_nicks_leave_nothing_behind(self):
        now = time.time()
        for n in range(5000):
            config.user_requests[f"nick{n}"] = [now - 600]
            config.muted_until[f"nick{n}"] = now - 600

        security._prune_flood_tracking(now)

        self.assertEqual(config.user_requests, {})
        self.assertEqual(config.muted_until, {})


class ItNeverDropsSomethingStillInForce(FloodCase):
    """The controls, and the reason this is not simply `dict.clear()`."""

    def test_a_live_mute_survives(self):
        """The hole this must not open: mute somebody, let them cycle nicks
        until a sweep runs, and have them come back unmuted."""
        now = time.time()
        config.muted_until["flooder"] = now + config.MUTE_TIME

        security._prune_flood_tracking(now)

        self.assertIn("flooder", config.muted_until)

    def test_a_history_inside_the_window_survives(self):
        """Dropping this would reset a flooder's count mid-window, so the
        threshold could never be reached."""
        now = time.time()
        config.user_requests["busy"] = [now - 1, now]

        security._prune_flood_tracking(now)

        self.assertIn("busy", config.user_requests)

    def test_the_boundary_belongs_to_the_window(self):
        """A timestamp exactly REQUEST_WINDOW old is outside is_flooding()'s
        own `now - ts < REQUEST_WINDOW` test, so the sweep must agree with it
        rather than keep an entry that no longer counts."""
        now = time.time()
        config.user_requests["edge"] = [now - config.REQUEST_WINDOW]

        security._prune_flood_tracking(now)

        self.assertNotIn("edge", config.user_requests)

    def test_a_long_running_requester_is_judged_on_its_NEWEST_request(self):
        """Every other fixture here has one timestamp, or several from the same
        moment, so `max` and `min` coincide and a sweep keyed on the oldest
        passes them all. Someone who has been requesting steadily for ten
        minutes has an old first entry and a recent last one - judging them on
        the first forgets them mid-window, which resets a flooder's count and
        means the threshold is never reached."""
        now = time.time()
        config.user_requests["steady"] = [now - 600, now - 300, now - 1]

        security._prune_flood_tracking(now)

        self.assertIn("steady", config.user_requests)

    def test_a_mixed_table_loses_only_the_dead_rows(self):
        now = time.time()
        config.user_requests.update({"live": [now], "dead": [now - 600]})
        config.muted_until.update({"muted": now + 30, "released": now - 30})

        security._prune_flood_tracking(now)

        self.assertEqual(sorted(config.user_requests), ["live"])
        self.assertEqual(sorted(config.muted_until), ["muted"])


class TheSweepIsThrottled(FloodCase):
    """It runs on the IRC read thread, once per request, so it cannot be
    O(nicks) every message."""

    def test_the_first_call_sweeps(self):
        now = time.time()
        config.user_requests["ghost"] = [now - 600]

        self.assertEqual(security._sweep_flood_tracking_if_due(now), 1)

    def test_a_second_call_straight_after_does_not(self):
        now = time.time()
        security._sweep_flood_tracking_if_due(now)
        config.user_requests["ghost"] = [now - 600]

        self.assertEqual(security._sweep_flood_tracking_if_due(now), 0)
        self.assertIn("ghost", config.user_requests,
                      "the throttle did not actually skip the sweep")

    def test_it_sweeps_again_once_the_interval_has_passed(self):
        now = time.time()
        security._sweep_flood_tracking_if_due(now)
        config.user_requests["ghost"] = [now - 600]

        later = now + security._FLOOD_SWEEP_EVERY

        self.assertEqual(security._sweep_flood_tracking_if_due(later), 1)


class FloodProtectionStillWorks(FloodCase):
    """The whole point of the controls above, exercised through the real
    entry point rather than the sweep helper."""

    def flood(self, nick, times):
        return [security.is_flooding(nick) for _ in range(times)]

    def test_a_normal_user_is_never_flagged(self):
        self.assertNotIn(True, self.flood("dave", config.MAX_REQUESTS))

    def test_going_over_the_threshold_still_mutes(self):
        self.flood("flooder", config.MAX_REQUESTS + 1)

        self.assertIn("flooder", config.muted_until)

    def test_a_muted_user_who_returns_is_still_banned_until_midnight(self):
        """The escalation path, which reads muted_until - the dict the sweep
        now prunes. If a live mute were swept, this would silently stop."""
        self.flood("flooder", config.MAX_REQUESTS + 1)
        security._last_flood_sweep = 0.0        # force a sweep on the next call

        self.assertTrue(security.is_flooding("flooder"))
        self.assertIn("flooder", config.banned_users)

    def test_the_sweep_does_not_leave_the_current_user_untracked(self):
        """The sweep runs at the top of is_flooding(), so it can delete the
        very entry the rest of the call is about. That has to be harmless."""
        security.is_flooding("dave")
        security._last_flood_sweep = 0.0
        config.user_requests["dave"] = [time.time() - 600]   # now sweepable

        security.is_flooding("dave")

        self.assertIn("dave", config.user_requests)
        self.assertEqual(len(config.user_requests["dave"]), 1)


class TheLeakIsClosedThroughTheRealEntryPoint(FloodCase):
    """End to end: nicks that pass through once and never return."""

    def test_a_nick_cycling_attacker_does_not_grow_the_table(self):
        for n in range(500):
            security.is_flooding(f"drive-by{n}")

        # Every one of those is now outside REQUEST_WINDOW.
        security._last_flood_sweep = 0.0
        security._sweep_flood_tracking_if_due(time.time() + config.REQUEST_WINDOW + 1)

        self.assertEqual(config.user_requests, {})

    def test_is_flooding_is_what_drives_the_sweep(self):
        """The wiring, not the helper. Every other test here reaches for
        _prune_flood_tracking() or _sweep_flood_tracking_if_due() directly,
        which all still pass with the call deleted from is_flooding() - and
        then nothing sweeps anything in a running daemon.

        Uses a DIFFERENT nick from the one being requested, so it cannot pass
        by way of is_flooding's own handling of the caller's own entry.
        """
        config.user_requests["ghost"] = [time.time() - 600]
        security._last_flood_sweep = 0.0

        security.is_flooding("dave")

        self.assertNotIn("ghost", config.user_requests,
                         "is_flooding() no longer sweeps, so nothing ever does")


class TheRehashStillCarriesThem(unittest.TestCase):
    """PRESERVE_RUNTIME keeping both dicts is deliberate and load-bearing -
    dropping them on !rehash would release every live mute. The sweep is what
    bounds them now, so the preservation must stay."""

    def test_both_are_still_preserved_across_a_rehash(self):
        import commands

        self.assertIn("muted_until", commands.PRESERVE_RUNTIME)
        self.assertIn("user_requests", commands.PRESERVE_RUNTIME)


if __name__ == "__main__":
    unittest.main()
