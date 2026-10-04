"""A list that is not held does not fill the filter bar's page.

search() narrowed the index to held lists with an FTS5 phrase, `bot:"tunebox"`,
which is a tokenised match: it also matches "tunebox-2", "tunebox|away" and
"tunebox/rar". The query then took its LIMIT, and only after that were the
rows of lists that are not held removed, in Python. A list that is not held -
offline under Online Only, or left in the index by a refetch - with enough
matches filled the whole window, and the held list's matches were not on the
page at all. The sidebar still showed the held list as matched, from
bots_with_a_match(), which had put `bot = ?` in its query long ago for exactly
this reason, and the page was not marked as cut short.

The held lists are chosen in the query now, with `bot IN (...)`, so the LIMIT
counts only rows that can be offered.
"""

import io
import os
import zipfile

from tests import support  # noqa: F401  (path setup)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402
import webserver  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402

BS = chr(92)
FOLDER = "D:" + BS + "MUSIC" + BS + "Some Folder" + BS


def songs(count, word="Love"):
    return [f"{word} Song {n:03d}.flac" for n in range(count)]


class TheHeldListIsOnThePage(crosslist.IndexCase):

    def test_a_busier_neighbour_that_is_not_held_does_not_hide_it(self):
        """The audit's probe: 300 matches in "TuneBox-2", indexed first, and 5
        in the held "TuneBox"."""
        self.index("TuneBox-2", *songs(300))
        self.index("TuneBox", *songs(5))

        rows = list_index.search(["love"], limit=200, bots=["TuneBox"])

        self.assertEqual(len(rows), 5)
        self.assertEqual({row["bot"] for row in rows}, {"tunebox"})

    def test_nor_does_a_list_a_refetch_left_behind(self):
        self.index("TuneBox/rar", *songs(300))
        self.index("TuneBox", *songs(5))

        rows = list_index.search(["love"], limit=200, bots=["TuneBox"])

        self.assertEqual(len(rows), 5)
        self.assertEqual({row["bot"] for row in rows}, {"tunebox"})

    def test_a_full_page_holds_only_held_rows(self):
        self.index("TuneBox-2", *songs(300))
        self.index("TuneBox", *songs(250))

        rows = list_index.search(["love"], limit=200, bots=["TuneBox"])

        self.assertEqual(len(rows), 200)
        self.assertEqual({row["bot"] for row in rows}, {"tunebox"})

    def test_the_held_names_are_compared_without_case(self):
        self.index("TuneBox-2", *songs(30))
        self.index("TuneBox", *songs(5))

        rows = list_index.search(["love"], limit=20, bots=["TUNEBOX"])

        self.assertEqual(len(rows), 5)

    def test_the_rows_carry_their_folder_heading(self):
        """The heading comes back as text, not as the id the index stores it
        under (#1135), on the held-list query as on any other."""
        self.index("TuneBox-2", *songs(30))
        self.index("TuneBox", *songs(2))

        rows = list_index.search(["love"], limit=10, bots=["TuneBox"])

        self.assertEqual({row["folder"] for row in rows}, {FOLDER})
        self.assertEqual({row["size"] for row in rows}, {"4.00MB"})


class ThePhrasesStayAPreFilter(crosslist.IndexCase):
    """`bot:"<name>"` is what the full-text index answers without reading
    every match; the IN only decides among what it lets through. A name with
    no tokens is no phrase (#1091), and then the IN works alone."""

    def statements(self, bots):
        list_index.search(["love"], bots=bots)
        conn = list_index._reader()
        seen = []
        conn.set_trace_callback(seen.append)
        try:
            list_index.search(["love"], bots=bots)
        finally:
            conn.set_trace_callback(None)
        return [s for s in seen if "FROM entries WHERE entries MATCH" in s]

    def test_held_names_with_letters_narrow_the_match(self):
        self.index("TuneBox", *songs(2))

        (statement,) = self.statements(["TuneBox", "TuneBox2"])

        self.assertIn('(bot:"tunebox" OR bot:"tunebox2") AND filename:', statement)
        self.assertIn("AND bot IN ('tunebox', 'tunebox2') LIMIT", statement)

    def test_a_name_with_none_leaves_the_phrases_out(self):
        self.index("^_^", *songs(2))

        (statement,) = self.statements(["^_^", "TuneBox"])

        self.assertNotIn("bot:", statement)
        self.assertIn("AND bot IN ('^_^', 'tunebox') LIMIT", statement)


class ManyHeldLists(crosslist.IndexCase):
    """Asked in batches, since SQLite before 3.32 takes 999 parameters at
    most. Small batches here, so a handful of lists crosses them."""

    def setUp(self):
        super().setUp()
        original = list_index._HELD_KEYS_PER_QUERY
        list_index._HELD_KEYS_PER_QUERY = 2
        self.addCleanup(setattr, list_index, "_HELD_KEYS_PER_QUERY", original)
        for n in range(5):
            self.index(f"TuneBox{n}", *songs(4))
            self.index(f"TuneBox{n}-x", *songs(40))

    def test_every_held_list_is_searched(self):
        held = [f"TuneBox{n}" for n in range(5)]

        rows = list_index.search(["love"], limit=200, bots=held)

        self.assertEqual(len(rows), 20)
        self.assertEqual({row["bot"] for row in rows},
                         {name.lower() for name in held})

    def test_the_limit_holds_across_batches(self):
        held = [f"TuneBox{n}" for n in range(5)]

        rows = list_index.search(["love"], limit=10, bots=held)

        self.assertEqual(len(rows), 10)
        self.assertTrue({row["bot"] for row in rows} <= {name.lower() for name in held})


class TheFilterPayload(crosslist.IndexCase):
    """What the dashboard is handed: the rows, and whether there are more."""

    def test_the_held_list_is_shown_and_the_page_is_not_cut_short(self):
        self.index("TuneBox-2", *songs(300))
        self.index("TuneBox", *songs(5))
        self.hold("TuneBox")

        payload = webserver.build_crosslist_search_payload("love", limit=200)

        self.assertEqual(payload["total_files"], 5)
        self.assertEqual(payload["matched"], ["tunebox"])
        self.assertFalse(payload["truncated"])

    def test_a_page_full_of_held_rows_says_it_was_cut_short(self):
        """The flag counts the rows the page holds. Filtered after the
        LIMIT, 300 rows of a list that is not held came back as an empty page
        that said it was complete."""
        self.index("TuneBox-2", *songs(300))
        self.index("TuneBox", *songs(250))
        self.hold("TuneBox")

        payload = webserver.build_crosslist_search_payload("love", limit=200)

        self.assertEqual(payload["total_files"], 200)
        self.assertTrue(payload["truncated"])


def list_text(base, names):
    out = ["Header" + chr(10), "=" * 30 + chr(10), FOLDER + chr(10), "=" * 30 + chr(10)]
    for name in names:
        out.append(f"!{base} {name}  ::INFO:: 5000000" + chr(10))
    return "".join(out)


class AfterARealFetch(support.DCCoreTestCase):
    """The same through process_fetched_list_zip(): two bots fetched, and the
    filter asked about one of them alone, as Online Only does when the other
    is offline."""

    def setUp(self):
        super().setUp()
        self.set_config(FETCHED_FILES_DIR=self.make_temp_dir(prefix="dccore-held-page-"))

    def fetch(self, nick, names):
        path = os.path.join(config.FETCHED_FILES_DIR, "incoming.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{nick}-Default(2026-01-02)-OS.txt",
                             list_text(nick, names))
        with open(path, "wb") as handle:
            handle.write(buf.getvalue())
        ok, reason = list_fetch.process_fetched_list_zip(nick, path)
        self.assertTrue(ok, reason)

    def test_the_online_list_is_found_beside_a_busier_offline_one(self):
        self.fetch("TuneBox-2", songs(300))
        self.fetch("TuneBox", songs(5))

        rows = list_index.search(["love"], limit=200, bots=["TuneBox"])

        self.assertEqual(len(rows), 5)
        self.assertEqual({row["bot"] for row in rows}, {"tunebox"})
