"""Finished cross-bot fetch rows are forgotten eventually.

THE DEFECT (#221)

`complete` and `failed` rows in `config.fetch_queue` were never removed.
`MAX_UNRESOLVED_FETCHES` bounds only pending and in-flight rows, and the sole
way to drop a finished one was the dashboard's delete button, one at a time.

Everything downstream grows with it: the whole history is reloaded into memory
at every startup, `/api/fetch/status` returns all of it with no pagination and
`web/app.js` polls that every 4 seconds whichever tab is open, and
`_claim_matching_offer_locked()` scans it linearly for every inbound DCC SEND.

RETENTION, AND WHY IT IS SHAPED THIS WAY

Age is the primary rule - the Downloads table is a recent record of what
happened, not an archive. A count cap alone discards arbitrarily: whichever
rows are oldest when the cap is hit, whether they are an hour or a month old.
The count stays as a backstop, because a burst of activity inside the window is
exactly what age cannot bound.

The file on disk is NOT deleted - only the daemon's memory of which fetch
produced it. A retention setting that quietly removed somebody's downloaded
album would be a surprising thing; the file stays where the operator can see
it. The cost is that a pruned row's file is no longer deletable from the
dashboard, which is the lesser of the two.
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

DAY = 86400


def row(state="complete", age_days=0, now=None):
    now = time.time() if now is None else now
    return {"state": state, "request_type": "file",
            "requested_at": now - age_days * DAY,
            "stored_filename": "Album.rar"}


class PruningByAge(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.addCleanup(config.fetch_queue.clear)
        self.set_config(FETCH_HISTORY_DAYS=30, FETCH_HISTORY_MAX_ROWS=500)

    def prune(self):
        return dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

    def test_a_row_inside_the_window_is_kept(self):
        config.fetch_queue["recent"] = row(age_days=3)

        self.assertEqual(self.prune(), [])
        self.assertIn("recent", config.fetch_queue)

    def test_a_row_past_the_window_is_dropped(self):
        config.fetch_queue["old"] = row(age_days=45)

        self.assertEqual(self.prune(), ["old"])
        self.assertNotIn("old", config.fetch_queue)

    def test_a_failed_row_ages_out_the_same_way(self):
        """Both terminal states, not just the happy one."""
        config.fetch_queue["old"] = row(state="failed", age_days=45)

        self.assertEqual(self.prune(), ["old"])

    def test_an_in_flight_row_is_never_dropped_however_old(self):
        """A pending row sitting for a month is a bug worth seeing, not history
        worth forgetting - and dropping it would unbook work the dispatcher
        still owns."""
        for state in ("pending", "offered", "listening", "receiving"):
            with self.subTest(state=state):
                config.fetch_queue.clear()
                config.fetch_queue["stuck"] = row(state=state, age_days=400)

                self.assertEqual(self.prune(), [])
                self.assertIn("stuck", config.fetch_queue)

    def test_a_row_with_no_timestamp_is_kept(self):
        """It predates requested_at. Treating "unknown" as "infinitely old"
        would delete exactly the rows whose age cannot be established."""
        config.fetch_queue["undated"] = {"state": "complete"}

        self.assertEqual(self.prune(), [])

    def test_a_zero_timestamp_counts_as_undated_not_as_1970(self):
        """The first version dropped these on sight. Rows built before the
        field existed carry 0, and so does every fixture that omits it - which
        is how this surfaced: an unrelated webserver test lost its whole
        persisted history to a retention rule it never opted into."""
        config.fetch_queue["zero"] = {"state": "complete", "requested_at": 0}

        self.assertEqual(self.prune(), [])
        self.assertIn("zero", config.fetch_queue)

    def test_an_implausible_timestamp_counts_as_undated(self):
        """A fetch cannot predate the software. A value down here is a default,
        a sentinel or corruption - and deleting a row because its timestamp is
        unreadable is the one direction this must not fail in.

        Found by an unrelated webserver fixture using requested_at=1.0, which
        my first rule read as 1 Jan 1970 and dutifully deleted."""
        for stamp in (0, 1.0, -5, 12345):
            with self.subTest(requested_at=stamp):
                config.fetch_queue.clear()
                config.fetch_queue["odd"] = {"state": "complete",
                                             "requested_at": stamp}

                self.assertEqual(self.prune(), [])
                self.assertIn("odd", config.fetch_queue)

    def test_the_window_is_configurable(self):
        self.set_config(FETCH_HISTORY_DAYS=7)
        config.fetch_queue["ten-days"] = row(age_days=10)

        self.assertEqual(self.prune(), ["ten-days"])

    def test_zero_disables_age_pruning(self):
        self.set_config(FETCH_HISTORY_DAYS=0)
        config.fetch_queue["ancient"] = row(age_days=9999)

        self.assertEqual(self.prune(), [])


class TheRowCountBackstop(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.addCleanup(config.fetch_queue.clear)
        self.set_config(FETCH_HISTORY_DAYS=30, FETCH_HISTORY_MAX_ROWS=5)

    def test_a_burst_inside_the_window_is_capped(self):
        """What age cannot bound: fifty fetches in one afternoon are all
        recent, and all kept, without this."""
        for n in range(50):
            config.fetch_queue[f"r{n:02d}"] = row(age_days=0)

        dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

        self.assertEqual(len(config.fetch_queue), 5)

    def test_the_oldest_go_first(self):
        for n in range(8):
            config.fetch_queue[f"r{n}"] = row(age_days=8 - n)

        dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

        self.assertEqual(sorted(config.fetch_queue), ["r3", "r4", "r5", "r6", "r7"])

    def test_in_flight_rows_do_not_count_against_the_cap(self):
        """Otherwise a queue busy with real work would evict its own history."""
        for n in range(10):
            config.fetch_queue[f"busy{n}"] = row(state="pending")
        config.fetch_queue["done"] = row(age_days=1)

        dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

        self.assertIn("done", config.fetch_queue)
        self.assertEqual(len([r for r in config.fetch_queue.values()
                              if r["state"] == "pending"]), 10)

    def test_zero_disables_the_cap(self):
        self.set_config(FETCH_HISTORY_MAX_ROWS=0)
        for n in range(20):
            config.fetch_queue[f"r{n}"] = row(age_days=0)

        dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

        self.assertEqual(len(config.fetch_queue), 20)


class ItRunsWithoutBeingAskedTo(DCCoreTestCase):
    """Retention nobody triggers is retention that does not happen."""

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.addCleanup(config.fetch_queue.clear)
        self.set_config(FETCH_HISTORY_DAYS=30, FETCH_HISTORY_MAX_ROWS=500)

    def test_the_persist_cycle_prunes(self):
        """It already holds the lock and already walks the dict, so this costs
        one comparison per row."""
        config.fetch_queue["old"] = row(age_days=90)

        with dcc_fetch._fetch_lock():
            dcc_fetch._persist_fetch_history_locked(config.fetch_queue)

        self.assertNotIn("old", config.fetch_queue)

    def test_the_unlocked_entry_point_works_for_startup(self):
        """oserve.py calls this after loading the history back, so an upgrade
        cleans up a pre-retention backlog once rather than carrying it."""
        config.fetch_queue["old"] = row(age_days=90)
        config.fetch_queue["new"] = row(age_days=1)

        dropped = dcc_fetch.prune_fetch_history()

        self.assertEqual(dropped, ["old"])
        self.assertIn("new", config.fetch_queue)

    def test_startup_calls_it(self):
        """Structural: the load and the prune have to stay together, or an
        upgraded install carries its whole backlog for the life of the
        process."""
        import ast
        import io as _io

        with _io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        calls = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "prune_fetch_history"]

        self.assertTrue(calls, "startup loads the history back and never prunes it")


class TheDownloadedFilesAreLeftAlone(DCCoreTestCase):
    """The one thing a retention setting must not do."""

    def setUp(self):
        super().setUp()
        config.fetch_queue.clear()
        self.addCleanup(config.fetch_queue.clear)
        self.tree = self.make_tree()
        self.set_config(FETCH_HISTORY_DAYS=1, FETCH_HISTORY_MAX_ROWS=500,
                        FETCHED_FILES_DIR=self.tree.root)

    def test_pruning_a_row_does_not_delete_its_file(self):
        import io as _io

        path = os.path.join(self.tree.root, "Album.rar")
        with _io.open(path, "w", encoding="utf-8") as handle:
            handle.write("payload")
        config.fetch_queue["old"] = row(age_days=30)

        dcc_fetch.prune_fetch_history_locked(config.fetch_queue)

        self.assertNotIn("old", config.fetch_queue)
        self.assertTrue(os.path.exists(path),
                        "a retention setting deleted somebody's download")


if __name__ == "__main__":
    unittest.main()
