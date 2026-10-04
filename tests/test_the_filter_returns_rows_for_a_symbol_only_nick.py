"""The filter bar returns the rows of a held list whose nick is only symbols.

#1091 taught bots_with_a_match() and the startup check that a nick made only
of IRC's special characters - ^_^, [_], |-| - is no FTS5 phrase at all:
unicode61 keeps letters and digits and splits on everything else, so
`bot:"^_^"` matches no row. search() never got the same rule. It still put
every held name into its `bot:"..."` pre-filter, so such a list's rows never
came back, while the sidebar, asked by bots_with_a_match(), showed it as
matched. The operator saw a list marked as holding the file and no row to
select or queue.

When a held name has no tokens, search() leaves the phrase pre-filter out and
the held names are chosen by the query's `bot IN (...)` alone.
"""

from tests import support  # noqa: F401  (path setup)

import list_index  # noqa: E402
import webserver  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402

SYMBOL_NICKS = ("^_^", "[_]", "|-|", "[]", "_")


class TheSearch(crosslist.IndexCase):

    def test_returns_a_symbol_nicks_rows_beside_a_named_one(self):
        """The audit's probe."""
        self.index("^_^", *[f"Love Song {n}.flac" for n in range(5)])
        self.index("TuneBox", *[f"Love Tune {n}.flac" for n in range(5)])

        rows = list_index.search(["love"], bots=["^_^", "TuneBox"])

        self.assertEqual(sorted({row["bot"] for row in rows}), ["^_^", "tunebox"])
        self.assertEqual(len(rows), 10)

    def test_returns_them_when_it_is_the_only_one_held(self):
        for nick in SYMBOL_NICKS:
            with self.subTest(nick=nick):
                self.index(nick, "Love Song.flac")

                rows = list_index.search(["love"], bots=[nick])

                self.assertEqual([(row["bot"], row["filename"]) for row in rows],
                                 [(nick, "Love Song.flac")])

    def test_another_symbol_nick_holding_the_file_is_not_returned(self):
        """With no phrase to narrow by, the IN alone decides."""
        self.index("^_^", "Something Else.flac")
        self.index("[_]", "Love Song.flac")
        self.index("TuneBox", "Love Song.flac")

        rows = list_index.search(["love"], bots=["^_^"])

        self.assertEqual(rows, [])

    def test_nor_does_a_busy_one_hide_it(self):
        self.index("[_]", *[f"Love Song {n:03d}.flac" for n in range(300)])
        self.index("^_^", "Love Song.flac")

        rows = list_index.search(["love"], limit=200, bots=["^_^"])

        self.assertEqual([row["bot"] for row in rows], ["^_^"])


class TheFilterPayload(crosslist.IndexCase):

    def test_a_list_shown_as_matched_has_its_rows_on_the_page(self):
        self.index("^_^", "Love Song.flac")
        self.index("TuneBox", "Love Tune.flac")
        self.hold("^_^", "TuneBox")

        payload = webserver.build_crosslist_search_payload("love")

        self.assertEqual(payload["matched"], ["^_^", "tunebox"])
        self.assertEqual(sorted(group["bot"] for group in payload["folders"]),
                         ["TuneBox", "^_^"])
