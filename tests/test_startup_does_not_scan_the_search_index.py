"""Startup asks the search index about each held bot, not for every bot (#1071).

backfill_missing() runs on every start, and its first step was
indexed_bots(): `SELECT DISTINCT bot FROM entries`. entries is an FTS5 table
with no ordinary index on a column, so that one query read every row of the
index - the whole file, 3.3 GB on a live bot, forty-one seconds between
"Fetched lists" and "Notices" before the bot even connected. Measured on a
synthetic 2-million-row index: 1.08 s for the DISTINCT, 0.005 s for one
`bot:"name"` question per held bot.
"""

import os
from unittest import mock

from tests import support  # noqa: F401  (path setup)

import list_index  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402


class TheStartupCheck(crosslist.IndexCase):
    def held_list(self, bot, *filenames):
        return crosslist.ListsHeldFromBeforeTheIndexExisted.held_list(self, bot, *filenames)

    def statements_during_backfill(self, held):
        conn = list_index._connect()
        seen = []
        conn.set_trace_callback(seen.append)
        try:
            done = list_index.backfill_missing(held, log=lambda _m: None)
        finally:
            try:
                conn.set_trace_callback(None)
            except Exception:
                pass   # closed meanwhile; nothing is left to trace
        return done, seen

    def test_it_never_lists_every_bot_in_the_table(self):
        self.index("BoomBox", "Enter Sandman.flac")
        held = {"boombox": self.held_list("BoomBox", "Something Else.flac"),
                "otherbot": self.held_list("OtherBot", "A Song.flac")}
        done, seen = self.statements_during_backfill(held)
        self.assertEqual(done, 1, "OtherBot was not indexed yet, BoomBox was")
        self.assertFalse([s for s in seen if "DISTINCT" in s.upper()], seen)
        self.assertTrue([s for s in seen if "MATCH" in s.upper() and "LIMIT 1" in s.upper()], seen)

    def test_a_neighbour_with_a_longer_name_is_not_taken_for_it(self):
        """`bot:"Bot"` also matches "Bot-2": the equality decides."""
        self.index("Bot-2", "Enter Sandman.flac")
        held = {"bot": self.held_list("Bot", "A Song.flac")}
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)

    def test_a_name_stored_before_names_were_lower_case_still_counts(self):
        """indexed_bots() lowered what it read, so an old index's "Dude" was
        present for "dude"; read again it would become a second copy."""
        conn = list_index._connect()
        with conn:
            conn.execute("INSERT INTO entries (bot, filename, folder, size) VALUES (?, ?, ?, ?)",
                         ("Dude", "Old Song.flac", "D:\\MUSIC\\", "4"))
        held = {"dude": self.held_list("dude", "New Song.flac")}
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 0)

    def test_a_check_that_fails_leaves_that_bot_and_goes_on(self):
        said = []
        held = {"broken": self.held_list("Broken", "X.flac"),
                "boombox": self.held_list("BoomBox", "Enter Sandman.flac")}
        real = list_index._holds_rows_for

        def check(conn, bot):
            if bot.lower() == "broken":
                raise RuntimeError("disk said no")
            return real(conn, bot)

        with mock.patch.object(list_index, "_holds_rows_for", check):
            done = list_index.backfill_missing(held, log=said.append)
        self.assertEqual(done, 1)
        self.assertTrue(any("Broken" in line and "disk said no" in line for line in said), said)
        self.assertEqual(len(list_index.search(["sandman"])), 1)

    def test_no_index_at_all_indexes_nothing_and_says_nothing_per_bot(self):
        """_connect() has already said why the index is off; a line per held
        bot on top of that would be sixty lines of the same news."""
        said = []
        held = {"boombox": self.held_list("BoomBox", "Enter Sandman.flac"),
                "otherbot": self.held_list("OtherBot", "A Song.flac")}
        with mock.patch.object(list_index, "_connect", lambda: None):
            self.assertEqual(list_index.backfill_missing(held, log=said.append), 0)
        self.assertEqual(said, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
