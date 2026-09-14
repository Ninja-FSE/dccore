"""#133's filter bar: one term against every list you hold, live as you type.

WHY THERE IS AN INDEX AT ALL

`list_fetch` re-parses a list fresh on every call - deliberately, since #76
removed unbounded retention - so searching every held list by re-reading them
is arithmetically out of reach rather than merely slow. #133 measured one
719k-file list at 2.0s and ten held lists at about 11s. No amount of
debouncing turns eleven seconds into typing.

WHY FTS5 AND NOT AN ORDINARY INDEXED TABLE

#133 proposed the latter and called it "milliseconds across millions of rows".
That is true of one of the two queries this feature needs and false of the
other, and the false half is the headline behaviour. Measured against
4,000,000 rows in ten lists - the sizes #133's own channel capture recorded:

    plain table, LIKE '%term%'   page  1-41ms     which-bots  1150-1400ms
    FTS5, per-bot LIMIT 1        page   1- 4ms    which-bots     2-  4ms

Paging is fast either way because LIMIT stops the scan early. "Which bots have
nothing", the question that greys the sidebar, must prove a negative for every
bot and has no such escape - unless it is asked per bot, where each stops at
its first hit.

WHAT IT COSTS

FTS5 tokenises where find_matching_entries() does substring, so a mid-word
fragment no longer matches. That is a real behavioural difference and it is
what buys the feature: substring matching over four million rows is the 1.4s
query above. @find over our own list is untouched.
"""

import io
import sqlite3
import contextlib
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import list_index  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class IndexCase(DCCoreTestCase):
    """A throwaway index per test, and never the operator's own."""

    def setUp(self):
        super().setUp()
        self.index_dir = tempfile.mkdtemp(prefix="dccore-list-index-")
        self.set_config(LIST_INDEX_FILE=os.path.join(self.index_dir, "idx.db"))
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def index(self, bot, *filenames, folder="D:\\MUSIC\\Some Folder\\"):
        """Indexed through the REAL producer, not a hand-built dict.

        This built its own rows carrying a "filename" key, which
        list.entries_to_filelist_rows() has never produced - it writes
        "title". So every test here passed while the production feed path
        (list_fetch -> entries_to_filelist_rows -> index_bot_list) put an
        EMPTY name on every row, and the cross-list filter matched nothing at
        all for a real operator. An audit found it; this suite could not,
        because it was the only caller that did not use the producer.

        Going through the producer is what stops that repeating: a key
        renamed on either side now fails here.
        """
        import list as list_mod

        rows = list_mod.entries_to_filelist_rows(
            [{"filename": name, "size": "4.00MB", "folder": folder}
             for name in filenames], bot)
        return list_index.index_bot_list(bot, rows)

    def hold(self, *bots):
        self.set_config(fetched_bot_lists={
            bot.lower(): {"bot": bot, "fetched_at": 1, "entry_count": 1}
            for bot in bots})


class TheIndexAnswersTheQuestionsTheFilterAsks(IndexCase):

    def test_a_term_finds_a_file_in_a_held_list(self):
        self.index("BoomBox", "01 - Enter Sandman.flac")

        found = list_index.search(["sandman"])

        self.assertEqual([row["filename"] for row in found],
                         ["01 - Enter Sandman.flac"])

    def test_a_partial_word_finds_it_too(self):
        """The point of a filter bar rather than a search button: results
        appear before the word is finished."""
        self.index("BoomBox", "01 - Enter Sandman.flac")

        self.assertEqual(len(list_index.search(["sandm"])), 1)

    def test_every_word_must_match(self):
        """The same AND-across-words rule find_matching_entries() applies, so
        narrowing a term narrows the result rather than widening it."""
        self.index("BoomBox", "Enter Sandman.flac", "Sad But True.flac")

        self.assertEqual(len(list_index.search(["enter", "sandman"])), 1)
        self.assertEqual(len(list_index.search(["enter", "nirvana"])), 0)

    def test_a_mid_word_fragment_does_not_match(self):
        """Stated as a test because it is the one thing the index gives up.
        FTS5 tokenises; "andma" is not a token or a prefix of one. Substring
        matching over four million rows is the 1.4-second query this whole
        module exists to avoid, and @find over our own list still does it."""
        self.index("BoomBox", "01 - Enter Sandman.flac")

        self.assertEqual(list_index.search(["andma"]), [])

    def test_matching_ignores_case(self):
        self.index("BoomBox", "01 - Enter SANDMAN.flac")

        self.assertEqual(len(list_index.search(["sandman"])), 1)

    def test_an_empty_term_finds_nothing_rather_than_everything(self):
        """A cleared filter box must not page four million rows through the
        route on its way to showing the browse view again."""
        self.index("BoomBox", "01 - Enter Sandman.flac")

        self.assertEqual(list_index.search([]), [])
        self.assertEqual(list_index.search(["   "]), [])

    def test_a_query_full_of_fts_syntax_is_treated_as_text(self):
        """A term is whatever somebody typed into a box, and FTS5's expression
        language has operators in it. Unquoted, `NEAR` or a bare `-` is read
        as syntax and at best answers a different question; at worst it raises
        and the filter goes dead.

        Asserting the ANSWER, not the absence of an exception: search() is
        best-effort and swallows its own failures, which is right for
        production and means "did not raise" passes whether the term was
        quoted or not. A mutation removing the quoting proved exactly that."""
        self.index("BoomBox", "AC-DC Back In Black.flac", "Nirvana.flac")

        self.assertEqual([row["filename"] for row in list_index.search(["ac-dc"])],
                         ["AC-DC Back In Black.flac"])

        # None of these appear in either filename, so each must find nothing -
        # rather than being read as an operator joining what came before and
        # after it.
        for typed in ("NEAR", "OR", "AND", "NOT", '"', "^x"):
            with self.subTest(typed=typed):
                self.assertEqual(list_index.search([typed]), [],
                                 f"{typed!r} was read as FTS5 syntax")

    def test_an_empty_term_builds_no_query_at_all(self):
        """The property behind test_an_empty_term_finds_nothing: no query is
        constructed, so nothing downstream can be asked to match everything.
        Asserted here rather than through search(), which returns [] for a
        malformed query too and so cannot tell the two apart."""
        for nothing in ([], None, ["  "], ["", "\t"]):
            with self.subTest(terms=nothing):
                self.assertIsNone(list_index.build_match_query(nothing))

    def test_a_refetch_replaces_a_list_rather_than_doubling_it(self):
        self.index("BoomBox", "Old Track.flac")
        self.index("BoomBox", "New Track.flac")

        found = [row["filename"] for row in list_index.search(["track"])]

        self.assertEqual(found, ["New Track.flac"])

    def test_dropping_a_bot_forgets_its_list(self):
        self.index("BoomBox", "Only Track.flac")

        list_index.drop_bot("BoomBox")

        self.assertEqual(list_index.search(["track"]), [])
        self.assertEqual(list_index.indexed_bots(), set())

    def test_the_page_is_capped(self):
        self.index("BoomBox", *[f"Track {i}.flac" for i in range(50)])

        self.assertEqual(len(list_index.search(["track"], limit=10)), 10)

    def test_an_absurd_limit_is_clamped_rather_than_honoured(self):
        """A query parameter decides how many rows this materialises, so the
        ceiling has to be the server's rather than the caller's.

        Indexed past MAX_SEARCH_LIMIT deliberately: with five rows in the
        table every limit returns five, so the clamp is invisible and a
        mutation removing it passes."""
        over = list_index.MAX_SEARCH_LIMIT + 10
        self.index("BoomBox", *[f"Track {i}.flac" for i in range(over)])

        self.assertEqual(len(list_index.search(["track"], limit=10 ** 9)),
                         list_index.MAX_SEARCH_LIMIT)
        self.assertEqual(len(list_index.search(["track"], limit=-1)),
                         list_index.DEFAULT_SEARCH_LIMIT)
        self.assertEqual(len(list_index.search(["track"], limit="nonsense")),
                         list_index.DEFAULT_SEARCH_LIMIT)


class WhichBotsHaveNothing(IndexCase):
    """What greys the sidebar out.

    Asked per bot with LIMIT 1 rather than as one DISTINCT over every match -
    the difference between 2ms and 1.4s at four million rows, and therefore
    the difference between a filter bar and a search button.
    """

    def test_a_bot_with_a_match_is_matched(self):
        self.index("BoomBox", "Enter Sandman.flac")

        matched, empty = list_index.bots_with_a_match(["sandman"], ["BoomBox"])

        self.assertEqual(matched, {"boombox"})
        self.assertEqual(empty, set())

    def test_a_bot_with_no_match_is_empty(self):
        self.index("BoomBox", "Enter Sandman.flac")
        self.index("ReelBot", "Nevermind.flac")

        matched, empty = list_index.bots_with_a_match(
            ["sandman"], ["BoomBox", "ReelBot"])

        self.assertEqual(matched, {"boombox"})
        self.assertEqual(empty, {"reelbot"})

    def test_a_held_bot_that_was_never_indexed_is_empty_not_missing(self):
        """A list held from before the index existed, or one whose indexing
        failed. It has no matches to show, which is what the sidebar says -
        rather than the row disappearing from the answer entirely."""
        matched, empty = list_index.bots_with_a_match(["anything"], ["NewBot"])

        self.assertEqual(matched, set())
        self.assertEqual(empty, {"newbot"})

    def test_the_existence_question_stops_at_the_first_hit(self):
        """A performance property, and the one the whole feature rests on, so
        it is asserted even though nothing about the ANSWER changes without
        it - fetchone() takes one row either way. Read out of the source
        because that is the only place the difference is visible.

        Without LIMIT 1 this enumerates every match for every bot on every
        keystroke: measured at 1150-1400ms across four million rows, against
        2-4ms with it. That is the difference between a filter bar and a
        search button."""
        with open(os.path.join(REPO_ROOT, "list_index.py"),
                  encoding="utf-8") as handle:
            code = "\n".join(line.split("#", 1)[0]
                              for line in handle.read().splitlines())
        block = code.split("def bots_with_a_match(", 1)[1].split("\ndef ", 1)[0]

        # The STATEMENT, not the words. Stripping "#" comments leaves
        # docstrings behind, and that function's own docstring says "asked
        # once per bot with LIMIT 1" - so a bare search for "LIMIT 1" matched
        # the explanation of the code rather than the code, and passed with
        # the clause deleted.
        # BOUNDED, and back to one row. It went LIMIT 1 -> LIMIT 25 when the
        # bot column turned out to be matched as an FTS5 phrase rather than
        # compared for equality ("Bot" matches "Bot-2"), so a near-miss
        # neighbour could occupy the first row and the equality check needed
        # a few rows to look at.
        #
        # Twenty-five was not enough either: a neighbour holding fifty
        # matching files fills the whole window, and the real bot beneath it
        # was reported as having NO match. Whatever number is picked, a busy
        # neighbour can exceed it - so the equality moved INTO the query as a
        # plain `bot = ?` column filter, which is exact, and one row is proof
        # again. That is also what this function's docstring has said all
        # along.
        self.assertIn("MATCH ? AND bot = ? LIMIT 1", block,
                      "the existence query enumerates every match instead of "
                      "stopping at the first proof")

    def test_no_term_greys_nobody(self):
        self.index("BoomBox", "Enter Sandman.flac")

        matched, empty = list_index.bots_with_a_match([], ["BoomBox"])

        self.assertEqual((matched, empty), (set(), set()))


class TheIndexIsNotTheRecordOfWhatIsHeld(IndexCase):
    """`fetched_bot_lists` is. The two can drift.

    A list file removed by hand, a reset store, an entry that went away while
    its rows stayed - and a row for a list we no longer hold offers a file
    that cannot be requested, which is worse than not finding it.
    """

    def test_a_search_is_scoped_to_the_lists_held(self):
        """The index answers with its NORMALISED key.

        A bot is stored under one lower-cased key so that a refetch typed with
        different capitalisation replaces its list rather than leaving both
        copies searchable for ever. Recovering the nick as that bot spells it
        is the payload's job - build_crosslist_search_payload maps back
        through `fetched_bot_lists`, which is keyed the same way and holds the
        real spelling."""
        self.index("BoomBox", "Enter Sandman.flac")
        self.index("GoneBot", "Enter Sandman.flac")

        found = list_index.search(["sandman"], bots=["BoomBox"])

        self.assertEqual([row["bot"] for row in found], ["boombox"])

    def test_a_refetch_typed_differently_replaces_rather_than_doubles(self):
        """The reason the key is normalised at all.

        SQLite's `=` is binary, so the delete matched only the exact spelling
        while every reader case-folds. Fetch from "Dude", refetch from "DUDE" -
        the ordinary case, since the sidebar prefills the nick and a refetch is
        retyped by hand - and both copies stayed. The stale one answered
        searches beside the new one while indexed_bots() reported a single
        bot, so nothing could see it and nothing could free it."""
        self.index("Dude", "Old Song.flac")
        self.index("DUDE", "New Song.flac")

        found = [row["filename"] for row in list_index.search(["song"])]

        self.assertEqual(found, ["New Song.flac"])
        self.assertEqual(list_index.indexed_bots(), {"dude"})

    def test_a_bot_whose_name_contains_another_is_not_mistaken_for_it(self):
        """`bot:"Bot"` is an FTS5 PHRASE over a tokenised column, not
        equality - unicode61 splits on the punctuation, so it matches "Bot-2",
        "Bot_away" and "Bot|gone". Hold Bot and Bot-2 with only Bot-2
        matching, and the sidebar left Bot undimmed while the status line said
        "1 match in 2 lists". Every fixture here was token-disjoint, which is
        why this passed."""
        self.index("Bot", "Alpha.flac")
        self.index("Bot-2", "Target.flac")

        matched, empty = list_index.bots_with_a_match(
            ["target"], ["Bot", "Bot-2"])

        self.assertEqual(matched, {"bot-2"})
        self.assertEqual(empty, {"bot"})
        self.assertEqual(list_index.search(["target"], bots=["Bot"]), [],
                         "holding only Bot returned Bot-2's file")

    def test_holding_nothing_finds_nothing_however_full_the_index(self):
        self.index("GoneBot", "Enter Sandman.flac")

        self.assertEqual(list_index.search(["sandman"], bots=[]), [])

    def test_the_route_only_ever_offers_held_lists(self):
        self.index("BoomBox", "Enter Sandman.flac")
        self.index("GoneBot", "Enter Sandman.flac")
        self.hold("BoomBox")

        payload = webserver.build_crosslist_search_payload("sandman")

        self.assertEqual([group["bot"] for group in payload["folders"]],
                         ["BoomBox"])


class ThePayloadTheBrowserRenders(IndexCase):
    """The same shape GET /api/filelists returns.

    Deliberately: the browser's folder rendering, its checkboxes and its
    "Download selected" already work on that shape, and each row's "source" is
    its bot - so selecting across several lists at once needed no new
    plumbing at all.
    """

    def test_it_returns_folder_groups_like_the_browse_view(self):
        self.index("BoomBox", "Enter Sandman.flac")
        self.hold("BoomBox")

        payload = webserver.build_crosslist_search_payload("sandman")

        self.assertEqual(payload["total"], 1)
        group = payload["folders"][0]
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["entries"][0]["title"], "Enter Sandman.flac")

    def test_every_row_carries_the_bot_it_came_from(self):
        """What makes cross-list selection work: the checkbox reads its bot
        from here, so a page of results from four bots queues correctly."""
        self.index("BoomBox", "Enter Sandman.flac")
        self.index("ReelBot", "Sandman Live.flac")
        self.hold("BoomBox", "ReelBot")

        payload = webserver.build_crosslist_search_payload("sandman")
        sources = {entry["source"]
                   for group in payload["folders"] for entry in group["entries"]}

        self.assertEqual(sources, {"BoomBox", "ReelBot"})

    def test_two_bots_sharing_a_folder_path_stay_apart(self):
        """list.group_rows_by_folder() keys on the folder string, which is
        right for one list and wrong across several: two bots can both have
        "D:\\MUSIC\\Metallica\\". Merged, one bot's files would sit under the
        other's heading and the folder's !rar button would point at whichever
        came first."""
        shared = "D:\\MUSIC\\Metallica\\"
        self.index("BoomBox", "Enter Sandman.flac", folder=shared)
        self.index("ReelBot", "Sandman Live.flac", folder=shared)
        self.hold("BoomBox", "ReelBot")

        payload = webserver.build_crosslist_search_payload("sandman")

        self.assertEqual(len(payload["folders"]), 2)
        self.assertEqual({group["bot"] for group in payload["folders"]},
                         {"BoomBox", "ReelBot"})

    def test_the_bots_with_nothing_are_named_for_the_sidebar(self):
        self.index("BoomBox", "Enter Sandman.flac")
        self.hold("BoomBox", "ReelBot", "MetalHead")

        payload = webserver.build_crosslist_search_payload("sandman")

        self.assertEqual(payload["matched"], ["boombox"])
        self.assertEqual(payload["empty"], ["metalhead", "reelbot"])

    def test_a_bot_is_not_called_empty_just_because_the_page_filled_up(self):
        """The reason matched/empty is asked separately rather than derived
        from the rows. The page is capped, so a bot whose matches all fall
        past the cap would look empty when it is not - and the sidebar would
        cross out a list that has exactly what the operator is looking for."""
        self.index("BoomBox", *[f"Track {i}.flac" for i in range(30)])
        self.index("ReelBot", "Track Rare.flac")
        self.hold("BoomBox", "ReelBot")

        payload = webserver.build_crosslist_search_payload("track", limit=5)

        self.assertEqual(payload["empty"], [])
        self.assertEqual(sorted(payload["matched"]), ["boombox", "reelbot"])

    def test_it_says_when_it_had_to_stop_early(self):
        self.index("BoomBox", *[f"Track {i}.flac" for i in range(30)])
        self.hold("BoomBox")

        self.assertTrue(
            webserver.build_crosslist_search_payload("track", limit=5)["truncated"])
        self.assertFalse(
            webserver.build_crosslist_search_payload("track", limit=100)["truncated"])

    def test_an_empty_term_greys_nobody_and_returns_nothing(self):
        """A cleared box goes back to browsing; it must not cross out every
        bot on the way."""
        self.index("BoomBox", "Enter Sandman.flac")
        self.hold("BoomBox", "ReelBot")

        payload = webserver.build_crosslist_search_payload("")

        self.assertEqual(payload["folders"], [])
        self.assertEqual(payload["matched"], [])
        self.assertEqual(payload["empty"], ["boombox", "reelbot"])


class WhenTheIndexIsNotThere(IndexCase):
    """Best-effort throughout. A missing or unusable index costs the filter
    bar and nothing else: the lists are on disk, the browser still pages them,
    @find still works, and the next fetch rebuilds it."""

    def test_searching_without_one_returns_nothing_rather_than_raising(self):
        self.set_config(LIST_INDEX_FILE=os.path.join(
            self.index_dir, "no-such-dir", "nested", "idx.db"))
        list_index.reset_for_tests()

        self.assertEqual(list_index.search(["anything"]), [])

    def test_a_corrupt_database_costs_the_filter_and_nothing_else(self):
        path = os.path.join(self.index_dir, "corrupt.db")
        with open(path, "wb") as handle:
            handle.write(b"this is not a database, it is a text file")
        self.set_config(LIST_INDEX_FILE=path)
        list_index.reset_for_tests()

        self.assertEqual(list_index.search(["anything"]), [])
        self.assertEqual(list_index.index_bot_list("BoomBox", []), 0)

    def test_the_handle_it_could_not_use_is_closed_before_it_is_dropped(self):
        """sqlite3.connect() is LAZY - it succeeds on a corrupt file, on a
        text file, on anything openable - so the failure lands on the first
        execute() with a real open handle already in hand. Returning None
        without closing it leaked one connection per call, and this is called
        on every search: an operator with a damaged index leaked one for every
        keystroke in the filter bar. On Windows the file stays locked until
        the object is collected, so the next attempt then fails for a new
        reason and the log stops describing the original one.
        """
        import sqlite3 as sqlite3_mod

        opened = []
        real = sqlite3_mod.connect

        def watched(*args, **kwargs):
            conn = real(*args, **kwargs)
            opened.append(conn)
            return conn

        path = os.path.join(self.index_dir, "corrupt3.db")
        with open(path, "wb") as handle:
            handle.write(b"not a database")
        self.set_config(LIST_INDEX_FILE=path)
        list_index.reset_for_tests()

        list_index.sqlite3.connect = watched
        self.addCleanup(setattr, list_index.sqlite3, "connect", real)
        self.assertEqual(list_index.search(["anything"]), [])

        self.assertEqual(len(opened), 1, "the failing path opened nothing")
        with self.assertRaises(sqlite3_mod.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_no_index_does_not_grey_out_every_bot(self):
        """"We cannot tell" is not "nobody has it". Crossing out the whole
        sidebar would read as a definite answer, and the wrong one."""
        path = os.path.join(self.index_dir, "corrupt2.db")
        with open(path, "wb") as handle:
            handle.write(b"nope")
        self.set_config(LIST_INDEX_FILE=path)
        list_index.reset_for_tests()

        matched, empty = list_index.bots_with_a_match(["x"], ["BoomBox"])

        self.assertEqual((matched, empty), (set(), set()))


class SwitchingOneListOffWhileFiltering(unittest.TestCase):
    """#133: "clicking a bot toggles its results in or out; select-all and
    select-none for the sidebar".

    Read out of app.js - nothing in this project executes JavaScript. The
    property worth pinning is that toggling is done over the answer already
    held rather than by asking again: #133 calls it trivial precisely because
    the rows are already in the browser, and a round trip per click would be
    slower than the search that fetched them.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"),
                  encoding="utf-8") as handle:
            cls.js = handle.read()

    def block(self, name):
        start = self.js.index("function " + name + "(")
        depth = 0
        for i in range(self.js.index("{", start), len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
        raise AssertionError("unbalanced braces in " + name)

    def test_toggling_re_renders_rather_than_re_fetching(self):
        body = self.block("rerenderFromFilterPayload")

        self.assertIn("state.filelistsFilterPayload", body)
        self.assertNotIn("fetchJson", body)
        self.assertNotIn("loadFilelists", body)

    def test_a_switched_off_bot_is_dropped_from_what_is_shown(self):
        body = self.block("visibleFilterGroups")

        self.assertIn("state.filelistsExcluded", body)

    def test_nothing_is_hidden_when_no_term_is_set(self):
        """Browsing one list, the sidebar picks the source and a switched-off
        bot has no meaning. The exclusions must not leak into that view."""
        body = self.block("visibleFilterGroups")

        self.assertIn("if (!(state.filelistsFilter || \"\").trim()) { return groups; }",
                      body)

    def test_a_new_term_shows_every_list_again(self):
        """A new term is a new question. A bot switched off while looking for
        one thing must not stay switched off, invisibly, for the next."""
        body = self.block("runFilelistsFilter")

        self.assertIn("state.filelistsExcluded = {}", body)

    def test_show_none_switches_off_only_the_lists_that_matched(self):
        """Not every bot in the sidebar: one with no matches is already
        saying so, and marking it switched off as well would claim the
        operator made a choice they did not make."""
        body = self.block("setEveryListShown")

        self.assertIn("payload.matched", body)

    def test_all_of_them_switched_off_says_so(self):
        """Distinct from "nothing matches". One is an answer about the
        library, the other is a thing the operator did and can undo."""
        body = self.block("renderFilelistGroups")

        self.assertIn("Every list with a match is switched off.", body)
        self.assertIn("Nothing in any list you hold matches that.", body)


class MarkingWhatYouAlreadyAskedFor(IndexCase):
    """#133: "mark what you already requested", and it wants two states.

    requested  still in flight. dcc_fetch groups exactly these as
               _UNRESOLVED_FETCH_STATES, and reusing that tuple is what stops
               this drifting the day a state is added there.
    received   complete.
    nothing    FAILED, deliberately. A failure is not a thing you have, and
               marking it would discourage the one useful action left.
    """

    def queue(self, **rows):
        self.set_config(fetch_queue={
            key: dict(row) for key, row in rows.items()})

    def marks_for(self, term="flac"):
        payload = webserver.build_crosslist_search_payload(term)
        return {entry["title"]: entry["mark"]
                for group in payload["folders"] for entry in group["entries"]}

    def setUp(self):
        super().setUp()
        self.index("BoomBox", "Asked.flac", "Have.flac", "Failed.flac",
                   "Untouched.flac")
        self.hold("BoomBox")

    def test_an_in_flight_request_reads_as_asked(self):
        for state in ("pending", "offered", "listening", "receiving"):
            with self.subTest(state=state):
                self.queue(a={"bot": "BoomBox", "requested_filename": "Asked.flac",
                              "request_type": "file", "state": state})
                self.assertEqual(self.marks_for()["Asked.flac"], "requested")

    def test_every_unresolved_state_dcc_fetch_knows_is_covered(self):
        """Derived from dcc_fetch's own tuple rather than listed again here:
        a state added there must not silently stop being marked."""
        import dcc_fetch

        for state in dcc_fetch._UNRESOLVED_FETCH_STATES:
            with self.subTest(state=state):
                self.queue(a={"bot": "BoomBox", "requested_filename": "Asked.flac",
                              "request_type": "file", "state": state})
                self.assertEqual(self.marks_for()["Asked.flac"], "requested")

    def test_a_completed_one_reads_as_received(self):
        self.queue(a={"bot": "BoomBox", "requested_filename": "Have.flac",
                      "request_type": "file", "state": "complete"})

        self.assertEqual(self.marks_for()["Have.flac"], "received")

    def test_a_failed_one_is_not_marked_at_all(self):
        """The useful action is to ask again, and a mark would discourage
        it."""
        self.queue(a={"bot": "BoomBox", "requested_filename": "Failed.flac",
                      "request_type": "file", "state": "failed"})

        self.assertEqual(self.marks_for()["Failed.flac"], "")

    def test_a_file_never_asked_for_is_not_marked(self):
        self.queue()

        self.assertEqual(self.marks_for()["Untouched.flac"], "")

    def test_having_it_wins_over_having_asked(self):
        """Asked twice, arrived once. "You have this" is the more useful of
        the two answers."""
        # The COMPLETED one first, so that "last row wins" would give the
        # wrong answer. Ordered the other way this passed with the guard
        # deleted - the completed row happened to be seen last and won by
        # accident rather than by rule.
        self.queue(
            a={"bot": "BoomBox", "requested_filename": "Have.flac",
               "request_type": "file", "state": "complete"},
            b={"bot": "BoomBox", "requested_filename": "Have.flac",
               "request_type": "file", "state": "receiving"})

        self.assertEqual(self.marks_for()["Have.flac"], "received")

    def test_a_mark_belongs_to_one_bot_only(self):
        """Two bots can hold a file of the same name. Asking one for it says
        nothing about the other, and marking both would claim a request that
        was never made."""
        self.index("ReelBot", "Asked.flac")
        self.hold("BoomBox", "ReelBot")
        self.queue(a={"bot": "BoomBox", "requested_filename": "Asked.flac",
                      "request_type": "file", "state": "receiving"})

        payload = webserver.build_crosslist_search_payload("asked")
        marks = {(group["bot"], entry["title"]): entry["mark"]
                 for group in payload["folders"] for entry in group["entries"]}

        self.assertEqual(marks[("BoomBox", "Asked.flac")], "requested")
        self.assertEqual(marks[("ReelBot", "Asked.flac")], "")

    def test_a_whole_list_request_does_not_mark_a_file(self):
        """A "list" row asks for the bot's list, not for any row in the
        table. Marking a filename from one would claim something that never
        happened - and the filename field of such a row is empty anyway."""
        self.queue(a={"bot": "BoomBox", "requested_filename": "Asked.flac",
                      "request_type": "list", "state": "receiving"})

        self.assertEqual(self.marks_for()["Asked.flac"], "")

    def test_a_folder_request_does_not_mark_a_file_either(self):
        self.queue(a={"bot": "BoomBox", "requested_filename": "Asked.flac",
                      "request_type": "folder", "state": "receiving"})

        self.assertEqual(self.marks_for()["Asked.flac"], "")

    def test_the_match_ignores_case(self):
        """The list writes whatever that bot wrote; the request carries
        whatever was clicked. They agree in practice and must not depend on
        it."""
        self.queue(a={"bot": "boombox", "requested_filename": "ASKED.FLAC",
                      "request_type": "file", "state": "receiving"})

        self.assertEqual(self.marks_for()["Asked.flac"], "requested")


class TheRowShapeIsOne(unittest.TestCase):
    """Our own list and a fetched one go through list.entries_to_filelist_rows()
    precisely so the frontend sees one row shape. A key present in one and
    absent in the other is how that stops being true."""

    def test_mark_is_part_of_the_shape_rather_than_added_by_a_payload(self):
        import list as list_mod

        rows = list_mod.entries_to_filelist_rows(
            [{"filename": "A.flac", "size": "1MB", "folder": "F"}], "Someone")

        self.assertIn("mark", rows[0])
        self.assertEqual(rows[0]["mark"], "",
                         "our own list is never something we requested")


class ListsHeldFromBeforeTheIndexExisted(IndexCase):
    """The upgrade case, and it is the ordinary one.

    index_bot_list() has exactly one caller in the daemon - a fetch
    completing - while held lists survive restarts, because list_fetch
    persists them and oserve restores them. So an operator upgrading with
    lists already fetched had a full fetched_bot_lists and an empty index.

    Worse than it sounds, because the module's "we cannot tell" guard does not
    cover it: _connect() creates the database on demand, so the connection is
    NOT None, every per-bot query simply misses, and the page states
    positively that no list holds a match. The filter reads as working and
    answers wrongly, for lists that are full of matches, until each is
    re-fetched by hand.
    """

    def held_list(self, bot, *filenames):
        """A list file on disk and an entry pointing at it - what a restart
        leaves behind, with nothing in the index."""
        path = os.path.join(self.index_dir, f"{bot}-list.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(f"List of {len(filenames)} Files\n\n")
            handle.write("=" * 20 + "\n")
            handle.write("D:\\MUSIC\\Some Folder\\\n")
            handle.write("=" * 20 + "\n")
            for name in filenames:
                handle.write(f"!{bot} {name}  ::INFO:: 4.00MB\n")
        return {"bot": bot, "fetched_at": 1, "entry_count": len(filenames),
                "list_path": path}

    def test_a_held_list_is_indexed_at_startup(self):
        held = {"boombox": self.held_list("BoomBox", "Enter Sandman.flac")}
        self.assertEqual(list_index.search(["sandman"]), [])

        list_index.backfill_missing(held, log=lambda _m: None)

        self.assertEqual([row["filename"] for row in list_index.search(["sandman"])],
                         ["Enter Sandman.flac"])

    def test_it_reads_the_list_the_same_way_the_fetch_does(self):
        """Through find_matching_entries + entries_to_filelist_rows, not a
        second parser - which is also what stops the row-key mismatch that
        made every indexed name empty from happening again on this path."""
        held = {"boombox": self.held_list("BoomBox", "A Song.flac")}

        list_index.backfill_missing(held, log=lambda _m: None)

        row = list_index.search(["song"])[0]
        self.assertEqual(row["filename"], "A Song.flac")
        self.assertEqual(row["folder"], "D:\\MUSIC\\Some Folder\\")

    def test_a_list_already_indexed_is_not_read_again(self):
        """Idempotent, because this runs on every start. Re-indexing every
        held list at boot would re-parse hundreds of megabytes to arrive
        where it already was."""
        self.index("BoomBox", "Enter Sandman.flac")
        held = {"boombox": self.held_list("BoomBox", "Something Else.flac")}

        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 0)
        self.assertEqual([row["filename"] for row in list_index.search(["sandman"])],
                         ["Enter Sandman.flac"],
                         "the backfill overwrote a list that was already indexed")

    def test_an_entry_whose_file_has_gone_is_skipped(self):
        """The extracted list can be deleted while the entry survives. That
        costs its own row in the filter, not the startup."""
        entry = self.held_list("BoomBox", "Enter Sandman.flac")
        os.remove(entry["list_path"])

        self.assertEqual(
            list_index.backfill_missing({"boombox": entry}, log=lambda _m: None), 0)

    def test_one_unreadable_list_does_not_stop_the_others(self):
        held = {
            "broken": {"bot": "Broken", "list_path": None},
            "boombox": self.held_list("BoomBox", "Enter Sandman.flac"),
        }

        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)
        self.assertEqual(len(list_index.search(["sandman"])), 1)

    def test_the_daemon_runs_it_at_startup(self):
        """Read out of oserve.py: driving a real startup opens sockets, and
        the one thing that matters is that the call is there at all."""
        with io.open(os.path.join(REPO_ROOT, "oserve.py"),
                     encoding="utf-8") as handle:
            code = "\n".join(line.split("#", 1)[0]
                              for line in handle.read().splitlines())

        self.assertIn("list_index.backfill_missing(config.fetched_bot_lists)", code,
                      "nothing indexes lists held from before the index "
                      "existed, so the filter reports no matches for lists "
                      "that are full of them")


class TheFourSecondPollKeepsWhatTheFilterDid(unittest.TestCase):
    """The sidebar is rebuilt from scratch every FILELISTS_BOTS_POLL_MS.

    Everything the filter puts on it lives in classes on those rows, so
    without a restore the greying, the crossed-out names and the operator's
    own switched-off choices all vanished four seconds after appearing -
    repeatedly, while they were still typing. The scroll position went with
    them, which on the thirty-two-advertiser channel #133 was written from
    means the list jumps back to the top while being read.

    Read out of app.js: nothing here executes JavaScript.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"),
                  encoding="utf-8") as handle:
            cls.js = handle.read()

    def switcher(self):
        start = self.js.index("function renderFilelistsSwitcher(")
        depth = 0
        for i in range(self.js.index("{", start), len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
        raise AssertionError("unbalanced braces")

    def test_the_rebuild_reapplies_the_filter_marks(self):
        body = self.switcher()

        # The CALL AND ITS GUARD as one sequence. Asserting only that the call
        # appears passed against a mutation that put it behind `if (false)` -
        # present in the source, unreachable at runtime, which is exactly the
        # distinction a source-reading test is worst at making.
        guarded = "\n".join([
            "if (state.filelistsFilterPayload) {",
            "      applyFilterHighlight(state.filelistsFilterPayload);",
            "    }",
        ])

        self.assertIn(guarded, body,
                      "the rebuild does not reapply the filter's marks")

    def test_the_rebuild_keeps_the_scroll_position(self):
        body = self.switcher()

        self.assertIn("var keptScroll = el.filelistsBotList.scrollTop", body)
        self.assertIn("el.filelistsBotList.scrollTop = keptScroll", body)

    def test_the_rebuild_keeps_keyboard_focus_on_the_row(self):
        """Tabbing to a bot and having focus thrown to the document four
        seconds later makes the list unusable from the keyboard."""
        body = self.switcher()

        self.assertIn("refocusBot", body)
        self.assertIn(".focus()", body)

    def test_the_row_is_found_without_building_a_selector_from_a_nick(self):
        """A nick is remote input. This file's rule is that one never gets
        concatenated into anything that is then parsed - and the XSS guards
        do not care whether the result is markup or a CSS selector."""
        body = self.switcher()

        self.assertIn("dataset.bot === refocusBot", body)
        self.assertNotIn("querySelector(", body.split("refocusBot")[-1])


class OnlyTheNewestReplyIsDrawn(unittest.TestCase):
    """Two requests are easily in flight at 120ms of debounce.

    A broad term is slow; one more character is narrow and fast, and the
    narrow reply arrives first. The broad one then lands and repaints the
    table and the sidebar under the LATER term still in the box - and because
    it also caches itself as filelistsFilterPayload, every later re-render
    keeps serving it until the next keystroke.

    runFilelistsFilter() already had a token and its comment claimed "only the
    newest is allowed to render", but it was compared inside the debounce
    callback, before loadFilelists() was called at all: it decided which
    request to SEND and nothing decided which reply to DRAW.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "web", "app.js"),
                  encoding="utf-8") as handle:
            cls.js = handle.read()

    def loader(self):
        start = self.js.index("function loadFilelists()")
        depth = 0
        for i in range(self.js.index("{", start), len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
        raise AssertionError("unbalanced braces")

    def test_the_load_takes_a_token_of_its_own(self):
        body = self.loader()

        self.assertIn("state.filelistsLoadToken += 1", body)
        self.assertIn("var loadToken = state.filelistsLoadToken", body)

    def test_a_superseded_reply_does_not_render(self):
        body = self.loader()
        after_then = body.split(".then(function (payload)", 1)[1]

        self.assertIn("if (loadToken !== state.filelistsLoadToken) { return; }",
                      after_then.split("state.filelistsLoaded")[0],
                      "a stale reply repaints the table")

    def test_a_superseded_failure_does_not_render_either(self):
        """The same staleness wearing an error message: a request the operator
        has already moved on from failing must not paint over the results of
        the one they are waiting for."""
        body = self.loader()
        after_catch = body.split(".catch(function (err)", 1)[1]

        self.assertIn("if (loadToken !== state.filelistsLoadToken) { return; }",
                      after_catch)

    def test_it_is_not_the_debounce_token(self):
        """Two different questions - which keystroke may send a request, and
        which reply may draw. Conflating them is what let this through."""
        body = self.loader()

        self.assertNotIn("filelistsFilterToken", body)


class TheIndexIsWrittenByTheFetch(unittest.TestCase):
    """Read out of the source: driving a real fetch needs a socket, a zip and
    a peer, and the one thing that matters is that it happens in the parse
    that was already walking the file."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "list_fetch.py"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_the_fetch_indexes_the_rows_it_already_parsed(self):
        code = "\n".join(line.split("#", 1)[0]
                         for line in self.source().splitlines())

        self.assertIn("list_index.index_bot_list(", code,
                      "a fetched list never reaches the search index, so the "
                      "cross-list filter can never see it")

    def test_it_reuses_the_parse_rather_than_walking_the_file_again(self):
        """The rows were parsed only to be counted and thrown away. Indexing
        them costs a pass that was already happening; a second walk of a
        719k-line file would not be."""
        # Sliced out of _install_fetched_list() BY NAME. It used to anchor on
        # the first "rows = entries_to_filelist_rows(" in the file, which
        # stopped being this function's the day a helper above it parsed a
        # list too - and the block then ran across both, so the assertion
        # below read a call that was never the one it is about.
        code = self.source().split("def _install_fetched_list(", 1)[1]
        block = code.split("rows = list_mod.entries_to_filelist_rows(", 1)[1]
        block = block.split("store = _ensure_fetched_bot_lists()", 1)[0]

        self.assertIn("list_index.index_bot_list(", block)
        self.assertNotIn("find_matching_entries(", block,
                         "the index is built from a second parse of its own")

    def test_the_slice_above_is_looking_at_the_right_function(self):
        """Guard on the guard: an anchor that matched somewhere else would
        make the assertion vacuous rather than failing."""
        code = self.source().split("def _install_fetched_list(", 1)[1]
        block = code.split("rows = list_mod.entries_to_filelist_rows(", 1)[1]
        block = block.split("store = _ensure_fetched_bot_lists()", 1)[0]

        self.assertIn("index_bot_list", block)
        self.assertLess(len(block), 4000,
                        "the slice has run past the function it is about")


class AQueryThatFailedIsNotAListThatIsEmpty(IndexCase):
    """`empty` is rendered as a POSITIVE statement - the list greys out and
    the status line counts it as holding no match. Only something we actually
    established may go in it."""

    def test_a_bot_whose_query_raised_is_left_unmarked(self):
        """One sqlite error and a list that DOES match was shown to the
        operator as one that does not."""
        self.index("keeper", "Blue Monday.mp3")
        self.index("broken", "Blue Monday.mp3")

        real = list_index._connect

        class OneBotFails(object):
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, params=()):
                if "broken" in str(params):
                    raise sqlite3.OperationalError("no such column")
                return self.conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        list_index._connect = lambda: OneBotFails(real())
        self.addCleanup(setattr, list_index, "_connect", real)

        matched, empty = list_index.bots_with_a_match(["blue"], ["keeper", "broken"])

        self.assertEqual(matched, {"keeper"})
        self.assertNotIn("broken", empty)

    def test_and_the_operator_is_told_rather_than_shown_nothing(self):
        real = list_index._connect

        class AlwaysFails(object):
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, params=()):
                if "MATCH" in sql:
                    raise sqlite3.OperationalError("no such column")
                return self.conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        self.index("broken", "Blue Monday.mp3")
        list_index._connect = lambda: AlwaysFails(real())
        self.addCleanup(setattr, list_index, "_connect", real)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            list_index.bots_with_a_match(["blue"], ["broken"])

        self.assertIn("rather than shown as empty", buffer.getvalue())

    def test_a_bot_that_genuinely_has_no_match_is_still_empty(self):
        """The distinction is the whole point: "we could not tell" and "we
        checked and there is nothing" must not collapse into each other."""
        self.index("keeper", "Blue Monday.mp3")
        self.index("other", "Temptation.mp3")

        matched, empty = list_index.bots_with_a_match(["blue"], ["keeper", "other"])

        self.assertEqual(matched, {"keeper"})
        self.assertEqual(empty, {"other"})


class MovingTheIndexFileTakesEffect(IndexCase):
    """!rehash can repoint LIST_INDEX_FILE, and this module caches a
    connection - which is exactly the pair that goes wrong quietly."""

    def test_the_next_call_reconnects_rather_than_writing_to_the_old_file(self):
        """The audit flagged this as unhandled and it is handled: _index_path()
        is read through config on every call rather than captured at import,
        and _connect() compares it against the cached one. Guarded here so it
        stays that way - caching the path at import is the obvious tidy-up and
        would leave the operator's writes going to a file nothing reads."""
        self.index("dude", "Blue Monday.mp3")
        first = list_index._connection_path

        moved = os.path.join(self.index_dir, "moved.db")
        self.set_config(LIST_INDEX_FILE=moved)
        self.index("dude", "Temptation.mp3")

        self.assertEqual(list_index._connection_path, moved)
        self.assertNotEqual(first, moved)
        self.assertTrue(os.path.exists(moved))
        # And the old connection is not left open beside the new one.
        self.assertEqual(list_index.search(["temptation"])[0]["filename"],
                         "Temptation.mp3")
        self.assertEqual(list_index.search(["blue"]), [])


class TheQueueIsReadUnderTheLockItsWritersUse(DCCoreTestCase):
    """enqueue_fetch() inserts rows from the transfer thread while the List
    Browser reads them on Flask's."""

    def test_the_marks_are_built_inside_it(self):
        """Iterating a dict being inserted into raises "dictionary changed
        size during iteration" - here, a 500 on the List Browser at the exact
        moment somebody starts a fetch, which is when they are most likely to
        be looking at it. build_fetch_status_payload() already reads the queue
        this way; this one did not."""
        import dcc_fetch

        depth = []
        seen = []

        class WatchedLock(object):
            def __enter__(self):
                depth.append(True)
                return self

            def __exit__(self, *_exc):
                depth.pop()
                return False

        class WatchedQueue(dict):
            def values(self):
                seen.append(bool(depth))
                return dict.values(self)

        queue = WatchedQueue({"r1": {"request_type": "file", "bot": "Dude",
                                     "requested_filename": "Song.mp3",
                                     "state": "complete"}})
        self.set_config(fetch_queue=queue, fetch_queue_lock=WatchedLock())

        marks = webserver.fetch_marks_by_bot()

        self.assertEqual(seen, [True],
                         "the queue was read outside its lock")
        self.assertEqual(marks, {"dude": {"song.mp3": "received"}})


class TheRequestBodyHasACeiling(DCCoreTestCase):
    """Flask reads a body into memory before any route decides what to do
    with it, and this daemon shares a machine with transfers it must not
    starve."""

    def setUp(self):
        super().setUp()
        if not webserver.HAVE_FLASK:
            self.skipTest("Flask is not installed")
        import adminchat
        self.set_config(
            ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw", iterations=1000))
        self.app = webserver.create_app()

    def test_it_is_set_and_is_room_enough_for_the_largest_real_body(self):
        """A pasted vars.ini is the biggest legitimate one, and it is a text
        file of a few hundred lines."""
        ceiling = self.app.config.get("MAX_CONTENT_LENGTH")

        self.assertIsNotNone(ceiling, "no ceiling on the request body")
        self.assertGreaterEqual(ceiling, 1024 * 1024)

    def test_a_body_past_it_is_refused_rather_than_buffered(self):
        client = self.app.test_client()

        resp = client.post("/login",
                           data={"password": "x" * (self.app.config["MAX_CONTENT_LENGTH"] + 1)})

        self.assertEqual(resp.status_code, 413)

    def test_a_body_within_it_still_reaches_the_route(self):
        """A ceiling that refused ordinary traffic would be worse than none.
        The 413 above is posted to /login WITHOUT a session, which is the
        case that matters: it applies before authentication, where the daemon
        has the least reason to trust what it is handed."""
        client = self.app.test_client()

        resp = client.post("/login", data={"password": "wrong"})

        self.assertNotEqual(resp.status_code, 413)


class AWriteCheckpointsTheWalLogAfterward(IndexCase):
    """Reported live: an 845MB index and a 128MB .db-wal file that only ever
    grew. SQLite's own automatic checkpoint is PASSIVE (best-effort) and can
    only run between transactions - index_bot_list() deletes and re-inserts a
    whole bot's list as ONE transaction, and the largest bot measured on the
    live index is 1.3 million rows, so the log grows to that entire write's
    size before there is even a boundary for the passive checkpoint to
    attempt, and a concurrent read (the dashboard's filter bar, polled
    continuously) can leave it incomplete when it does attempt one.

    A manual `PRAGMA wal_checkpoint(TRUNCATE)` against the live file cleared
    128MB to zero in one call, with nothing in flight - confirming SQLite's
    own checkpoint can always do this when nothing stops it; what was missing
    was ever asking for a FULL one after the writes most likely to need it.
    """

    def _wal_path(self):
        return config.LIST_INDEX_FILE + "-wal"

    def _wal_size(self):
        try:
            return os.path.getsize(self._wal_path())
        except OSError:
            return 0

    def test_indexing_a_bot_leaves_the_wal_log_small(self):
        self.index("BoomBox", *[f"Track {n}.flac" for n in range(500)])

        self.assertLess(self._wal_size(), 4096,
                        "a checkpoint should have folded the write back into "
                        "the main file rather than leaving it in the log")

    def test_dropping_a_bot_leaves_the_wal_log_small_too(self):
        self.index("BoomBox", "Track.flac")

        list_index.drop_bot("BoomBox")

        self.assertLess(self._wal_size(), 4096)

    def test_a_checkpoint_failure_is_caught_inside_the_helper_itself(self):
        """_checkpoint_locked() must never raise out - index_bot_list() and
        drop_bot() call it AFTER their own conn.commit(), inside the same
        try/except that decides whether the WRITE succeeded. An exception
        escaping from here would be caught by that outer handler and
        reported as a failed index/drop, even though the row change had
        already committed successfully - turning a checkpoint problem into a
        false failure report for an operation that worked.

        A fake connection, not the real sqlite3 one: sqlite3.Connection.
        execute is read-only on the C extension type and cannot be wrapped
        to raise on demand, which a first version of this test found out the
        hard way. _checkpoint_locked() only ever calls conn.execute(sql), so
        any object with that method exercises it identically."""
        class _BoomConnection:
            def execute(self, sql, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        list_index._checkpoint_locked(_BoomConnection())  # must not raise

    def test_index_bot_list_checkpoints_after_committing(self):
        """The wiring: index_bot_list() actually calls the helper, and a
        write still reports success when it does."""
        calls = []
        real_checkpoint = list_index._checkpoint_locked
        list_index._checkpoint_locked = lambda conn: calls.append(conn)
        self.addCleanup(setattr, list_index, "_checkpoint_locked", real_checkpoint)

        indexed = self.index("BoomBox", "Track.flac")

        self.assertEqual(indexed, 1)
        self.assertEqual(len(calls), 1)

    def test_drop_bot_checkpoints_after_committing_too(self):
        self.index("BoomBox", "Track.flac")
        calls = []
        real_checkpoint = list_index._checkpoint_locked
        list_index._checkpoint_locked = lambda conn: calls.append(conn)
        self.addCleanup(setattr, list_index, "_checkpoint_locked", real_checkpoint)

        dropped = list_index.drop_bot("BoomBox")

        self.assertTrue(dropped)
        self.assertEqual(len(calls), 1)

    def test_the_checkpoint_is_the_truncating_kind(self):
        """TRUNCATE, not the default PASSIVE mode wal_checkpoint() would run
        with no argument - PASSIVE can succeed while still leaving the log
        file at whatever size it last grew to, which is exactly the
        behaviour reported live. Read from the live function object rather
        than re-run against the real connection (sqlite3.Connection.execute
        cannot be wrapped to observe the call - see the test above), the
        same "ask the source what it actually sends" idiom already used
        elsewhere in this suite for a query built as a string."""
        import inspect

        source = inspect.getsource(list_index._checkpoint_locked)

        self.assertIn("wal_checkpoint(TRUNCATE)", source)


if __name__ == "__main__":
    unittest.main()
