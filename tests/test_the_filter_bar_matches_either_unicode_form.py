"""The List Browser's filter matches a name in either Unicode form (#1281).

#1270 made @find match a name whether it is written composed (NFC) or
decomposed (NFD), as a library copied from a Mac writes it. The dashboard's
cross-list filter (list_index, FTS5) indexed and queried the text as it came,
and unicode61 folds only a single Latin accent either way: a letter with two
marks (Vietnamese), a Cyrillic short i, a kana with its voicing mark and a
Hangul syllable all split into other tokens. So "Việt" typed in the filter bar
missed a list that holds it decomposed, and the other way round.

Both the indexed text and the query are NFC-normalised now, for matching
only: a row still shows - and a request still names - the file as the list
holds it. An index written before this holds names in the old form, so it is
rebuilt once (schema 3), from the held lists on disk.
"""

import contextlib
import io
import os
import sqlite3
import unicodedata

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase

import list as list_mod  # noqa: E402
import list_index  # noqa: E402


def nfc(text):
    return unicodedata.normalize("NFC", text)


def nfd(text):
    return unicodedata.normalize("NFD", text)


# Invented names, one per script unicode61 does not fold on its own.
VIETNAMESE = "Việt Lullaby Collection.flac"
CYRILLIC = "Йогурт Morning Mix.mp3"
HANGUL = "한강 Evening Session.mp3"
KANA = "がらくた Demo Tape.mp3"

# The schema-2 table (#1130/#1135), exactly as list_index made it before #1281.
SCHEMA_2_CREATE = ("CREATE VIRTUAL TABLE entries USING fts5("
                   "bot, filename, folder UNINDEXED, size UNINDEXED, "
                   "tokenize='unicode61', prefix='1 2 3 4', columnsize=0)")


def held_list(directory, bot, *filenames):
    """A list file on disk and the entry list_fetch keeps for it."""
    path = os.path.join(directory, f"{bot}-list.txt")
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(f"List of {len(filenames)} Files\n\n" + "=" * 20 + "\n")
        handle.write("D:\\MUSIC\\Some Folder\\\n" + "=" * 20 + "\n")
        for name in filenames:
            handle.write(f"!{bot} {name}  ::INFO:: 4.00MB\n")
    return {"bot": bot, "fetched_at": 1, "entry_count": len(filenames),
            "list_path": path}


class IndexCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-index-forms-")
        self.path = os.path.join(self.dir, "idx.db")
        self.set_config(LIST_INDEX_FILE=self.path)
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)

    def index(self, bot, *filenames):
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": name, "folder": "D:\\MUSIC\\", "size": "4.00MB"}
             for name in filenames], bot)
        return list_index.index_bot_list(bot, rows)

    def quiet(self, call, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = call(*args, **kwargs)
        return result, out.getvalue()

    def found(self, typed, bots=("SomeBot",)):
        """The filenames the filter bar's page shows for `typed`."""
        rows = list_index.search(list_index.filter_segments(typed), bots=list(bots))
        return [row["filename"] for row in rows]

    def matched(self, typed, bots=("SomeBot",)):
        matched, _empty = list_index.bots_with_a_match(
            list_index.filter_segments(typed), list(bots))
        return matched

    def stored(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql, args).fetchall()


class TheHazardIsReal(IndexCase):

    def test_unicode61_alone_misses_the_other_form(self):
        """What the normalisation is for: a plain unicode61 table, as the
        index had, does not match these names across the two forms. A
        SQLite whose tokenizer one day folds them makes this moot, not
        wrong - so that is a skip, and the tests below still hold."""
        with contextlib.closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(filename, tokenize='unicode61')")
            conn.execute("INSERT INTO t VALUES (?)", (nfd(VIETNAMESE),))
            hits = conn.execute("SELECT count(*) FROM t WHERE t MATCH ?",
                                ('"' + nfc("việt") + '"',)).fetchone()[0]
        if hits:
            self.skipTest("this SQLite's unicode61 already folds the forms together")
        self.assertEqual(hits, 0)


class EitherFormFindsTheName(IndexCase):

    def test_a_decomposed_list_is_found_by_a_composed_query(self):
        """The Mac-made library and an ordinary keyboard."""
        names = [nfd(VIETNAMESE), nfd(CYRILLIC), nfd(HANGUL), nfd(KANA)]
        self.index("SomeBot", *names)

        for name, typed in zip(names, ("việt", "йогурт", "한강", "がらくた")):
            with self.subTest(typed=ascii(typed)):
                self.assertEqual(self.found(nfc(typed)), [name])
                self.assertEqual(self.matched(nfc(typed)), {"somebot"})

    def test_a_composed_list_is_found_by_a_decomposed_query(self):
        """The other way round: the query side is normalised too."""
        names = [nfc(VIETNAMESE), nfc(CYRILLIC), nfc(HANGUL), nfc(KANA)]
        self.index("SomeBot", *names)

        for name, typed in zip(names, ("việt", "йогурт", "한강", "がらくた")):
            with self.subTest(typed=ascii(typed)):
                self.assertEqual(self.found(nfd(typed)), [name])
                self.assertEqual(self.matched(nfd(typed)), {"somebot"})

    def test_the_word_being_typed_matches_as_a_prefix(self):
        """The filter bar's prefix wildcard, on half a composed word."""
        self.index("SomeBot", nfd(VIETNAMESE))

        self.assertEqual(self.found(nfc("việ")), [nfd(VIETNAMESE)])

    def test_a_list_without_the_name_still_reads_as_empty(self):
        self.index("SomeBot", nfd(VIETNAMESE))
        self.index("OtherBot", "Plain Ascii Song.mp3")

        matched, empty = list_index.bots_with_a_match(
            [nfc("việt")], ["SomeBot", "OtherBot"])

        self.assertEqual((matched, empty), ({"somebot"}, {"otherbot"}))


class TheNameShownIsTheListsOwn(IndexCase):

    def test_a_row_shows_the_bytes_the_list_holds(self):
        """A request has to name the file exactly as the bot's list does, so
        the page shows that form and not the one it was matched in."""
        self.index("SomeBot", nfd(VIETNAMESE), nfc(HANGUL))

        self.assertEqual(self.found(nfc("việt")), [nfd(VIETNAMESE)])
        self.assertEqual(self.found(nfd("한강")), [nfc(HANGUL)])

    def test_only_a_name_the_forms_write_differently_is_kept_twice(self):
        """The second copy costs disk, so an ASCII name and an already
        composed one have none."""
        self.index("SomeBot", "Plain Ascii Song.mp3", nfc(HANGUL), nfd(HANGUL))

        rows = self.stored("SELECT filename, original FROM entries ORDER BY rowid")

        self.assertEqual(rows, [("Plain Ascii Song.mp3", None),
                                (nfc(HANGUL), None),
                                (nfc(HANGUL), nfd(HANGUL))])


class AnIndexFromBeforeIsRebuilt(IndexCase):

    def setUp(self):
        super().setUp()
        self.held = {"somebot": held_list(self.dir, "SomeBot", nfd(VIETNAMESE))}
        self.set_config(fetched_bot_lists=self.held)

    def schema_2_file(self, schema="2"):
        """A schema-2 index holding the held list as the old code indexed it:
        the decomposed name, matched as it is."""
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA_2_CREATE)
            conn.execute("CREATE TABLE folders (id INTEGER PRIMARY KEY, bot TEXT, folder TEXT)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('schema', ?)", (schema,))
            conn.execute("INSERT INTO folders VALUES (1, 'somebot', 'D:\\MUSIC\\Some Folder\\')")
            conn.execute("INSERT INTO folders VALUES (2, 'gonebot', 'D:\\LEFT\\BEHIND\\')")
            conn.execute("INSERT INTO entries (bot, filename, folder, size) VALUES (?, ?, 1, '4.00MB')",
                         ("somebot", nfd(VIETNAMESE)))
            conn.commit()

    def test_the_first_filter_query_finds_the_old_rows_name(self):
        self.schema_2_file()

        rows, log = self.quiet(self.found, nfc("việt"))

        self.assertEqual(rows, [nfd(VIETNAMESE)])
        self.assertIn("Rebuilding the search index once", log)
        self.assertIn("Unicode form", log)
        self.assertEqual(self.stored("SELECT value FROM meta WHERE key = 'schema'"),
                         [(str(list_index._SCHEMA_VERSION),)])

    def test_the_old_headings_go_with_the_old_rows(self):
        """A list that is not held any more is never indexed again, so its
        folder headings would stay behind for good."""
        self.schema_2_file()

        self.quiet(self.found, nfc("việt"))

        self.assertEqual(self.stored("SELECT folder FROM folders WHERE bot = 'gonebot'"), [])

    def test_a_file_an_older_version_wrote_to_is_rebuilt_again(self):
        """The new table, reopened by an older version after a downgrade: it
        writes its own schema number and the names as they come, so the
        number in the meta row is what says the names need doing again."""
        self.quiet(list_index.backfill_missing, self.held, log=print)
        list_index.reset_for_tests()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', '2')")
            conn.execute("INSERT INTO entries (bot, filename, folder, size) VALUES (?, ?, 1, '1MB')",
                         ("somebot", nfd(HANGUL)))
            conn.commit()

        rows, log = self.quiet(self.found, nfc("한강"))

        self.assertIn("Rebuilding the search index once", log)
        # The downgraded version's row is gone with the rebuild; the held list
        # on disk is what comes back, and it does not hold that name.
        self.assertEqual(rows, [])
        self.assertEqual(self.found(nfc("việt")), [nfd(VIETNAMESE)])

    def test_a_current_index_is_not_rebuilt_on_every_open(self):
        self.quiet(list_index.backfill_missing, self.held, log=print)
        list_index.reset_for_tests()

        rows, log = self.quiet(self.found, nfc("việt"))

        self.assertEqual(log, "")
        self.assertEqual(rows, [nfd(VIETNAMESE)])


if __name__ == "__main__":
    import unittest
    unittest.main()
