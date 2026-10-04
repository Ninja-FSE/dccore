"""The search index's schema 2 answers every query exactly as schema 1 did.

#1130: the filter bar puts a prefix wildcard on the word being typed, and the
FTS5 table had no prefix index, so a two-letter prefix merged the doclist of
every token starting with it - once per held list. "al" took seconds per
keystroke. The table now has prefix='1 2 3 4'.

#1135: every row stored its full folder heading and FTS5 kept a per-row
column-size record nothing reads. Headings now live once per list in a
`folders` table, the row holds an id, and the table has columnsize=0.

Both change the on-disk schema, so they are one version and one rebuild: an
index made before them is dropped and made again, and the held lists are
indexed again from disk - by the startup backfill, or before the first filter
query answers when a fetch was what opened it.

Neither may change an answer. So the reference below is the schema-1 table
and queries as they were, filled with the same rows in the same order, and
every query - one-, two- and three-letter prefixes, non-ASCII, phrases,
several segments, FTS5 syntax typed into the box - must come back the same:
the same rows, the same order, the same folder text, the same matched and
empty lists.
"""

import contextlib
import io
import os
import random
import sqlite3

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase

import list as list_mod  # noqa: E402
import list_index  # noqa: E402


# The schema-1 table, exactly as list_index created it before #1130/#1135.
OLD_CREATE = ("CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5("
              "bot, filename, folder UNINDEXED, size UNINDEXED, "
              "tokenize='unicode61')")


def old_schema(path):
    """A schema-1 index file, as the code before #1130 left one."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(OLD_CREATE)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', '1')")
    conn.commit()
    return conn


def old_index(conn, bot, rows):
    """Schema 1's index_bot_list(): the heading as text in every row."""
    name = str(bot).strip().lower()
    conn.execute("DELETE FROM entries WHERE bot = ?", (name,))
    conn.executemany(
        "INSERT INTO entries (bot, filename, folder, size) VALUES (?, ?, ?, ?)",
        [(name, str(row.get("title") or row.get("filename") or ""),
          str(row.get("folder") or ""), str(row.get("size") or ""))
         for row in rows])
    conn.commit()


def old_search(conn, terms, limit, bots):
    """search() over the schema-1 table, with the limit already normalised.

    The held lists are chosen as search() chooses them now: `bot IN (...)` in
    the query, before the LIMIT, and no phrase pre-filter when a held name
    has no tokens. Schema 1's search() chose them in Python after the LIMIT,
    and left a symbol-only nick's rows out entirely - both fixed in search()
    itself, which is not what this file compares. It compares the tables."""
    query = list_index.build_match_query(terms)
    if query is None:
        return []
    held_keys = None
    if bots is not None:
        held = [str(b).strip() for b in bots if str(b).strip()]
        if not held:
            return []
        held_keys = sorted({b.strip().lower() for b in held})
        if all(list_index._has_tokens(b) for b in held_keys):
            query = ("(" + " OR ".join(f"bot:{list_index._quote(b)}"
                                       for b in held_keys)
                     + ") AND " + query)
    sql = "SELECT bot, filename, folder, size FROM entries WHERE entries MATCH ?"
    if held_keys is not None:
        sql += " AND bot IN (" + ", ".join(["?"] * len(held_keys)) + ")"
    found = conn.execute(sql + " LIMIT ?",
                         (query, *(held_keys or ()), limit)).fetchall()
    return [{"bot": r[0], "filename": r[1], "folder": r[2], "size": r[3]}
            for r in found]


def old_bots_with_a_match(conn, terms, bots):
    """Schema 1's bots_with_a_match()."""
    query = list_index.build_match_query(terms)
    candidates = [str(b).strip() for b in (bots or []) if str(b).strip()]
    if query is None or not candidates:
        return set(), set()
    matched = set()
    for bot in candidates:
        wanted = bot.strip().lower()
        match = (f"bot:{list_index._quote(wanted)} AND {query}"
                 if list_index._has_tokens(wanted) else query)
        rows = conn.execute("SELECT bot FROM entries WHERE entries MATCH ? "
                            "AND bot = ? LIMIT 1", (match, wanted)).fetchall()
        if any(str(r[0]).strip().lower() == wanted for r in rows):
            matched.add(wanted)
    return matched, {b.lower() for b in candidates} - matched


WORDS = ["love", "lovely", "lover", "me", "melody", "amon", "amarth", "alan",
         "alanexdo", "album", "alpha", "all", "the", "then", "theme", "a",
         "an", "and", "b", "café", "Ångström", "naïve", "straße", "Δέλτα",
         "δέκα", "日本語", "日本", "ölmusik", "öl", "zz", "zzz", "1999", "2001",
         "x", "mi", "mix", "remix", "ÉTÉ", "été", "it's", "rock-n-roll"]
FOLDERS = ["D:\\MUSIC\\Some Folder\\", "D:\\MUSIC\\some folder\\",
           "D:\\MUSIC\\Café Album (2001) [FLAC]\\", "E:\\日本\\Δέλτα\\", "",
           None, "D:\\MUSIC\\Some Folder\\"]


def make_lists(seed=1130):
    """Rows for several lists, through the real producer.

    Headings repeat, differ only in case, are empty or missing; names carry
    accents, Greek, CJK, digits and punctuation."""
    rng = random.Random(seed)
    out = []
    for bot in ("BotA", "Bot-2", "BotA/rar", "Other|Bot", "^_^"):
        entries = []
        for i in range(rng.randint(250, 450)):
            folder = FOLDERS[(i // rng.randint(3, 12)) % len(FOLDERS)]
            words = rng.choices(WORDS, k=rng.randint(1, 6))
            name = " ".join(rng.choice([w, w.upper(), w.capitalize()]) for w in words)
            name += rng.choice([".mp3", ".flac", "", " (Live).mkv", " - 02.ogg"])
            entries.append({"filename": name, "folder": folder,
                            "size": "%.2fMB" % (rng.random() * 9 + 0.1)})
        out.append((bot, list_mod.entries_to_filelist_rows(entries, bot.split("/")[0])))
    return out


def queries():
    """1-, 2- and 3-letter prefixes, non-ASCII, phrases, segments, syntax."""
    fixed = ["a", "al", "ala", "alan", "alanexdo", "l", "lo", "lov", "love",
             "love m", "love me", "love mel", "the", "th", "t", "the a", "b",
             "c", "ca", "caf", "café", "cafe", "å", "ån", "ång", "ö", "öl",
             "ölm", "δ", "δέ", "δέκ", "Δέλτα", "日", "日本", "日本語", "e", "ét",
             "été", "ete", "1", "19", "199", "2001", "z", "zz", "zzz", "x",
             "amon a", "amon am", "amon*th", "love*me", "*al", "al*", "a*b",
             "mi", "mix", "re", "rem", "it", "it s", "rock n", "rock-n-r",
             '"', 'lo"ve', "-", "- al", "NEAR", "near al", "AND", "al AND",
             "OR lo", "NOT", "(", "al)", "^", "st", "straße", "strasse",
             "LOVE", "LoVe M", "  al  ", "nothing-matches-this", "q"]
    rng = random.Random(1135)
    words = [w.lower() for w in WORDS]
    for _ in range(60):
        word = rng.choice(words)
        cut = word[:rng.randint(1, 4)]
        fixed.append(cut if rng.random() < 0.6 else rng.choice(words) + " " + cut)
    return fixed


class EquivalenceCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-index-schema-")
        self.set_config(LIST_INDEX_FILE=os.path.join(self.dir, "idx.db"))
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)
        self.old = old_schema(os.path.join(self.dir, "old.db"))
        self.addCleanup(self.old.close)
        self.lists = make_lists()
        for bot, rows in self.lists:
            old_index(self.old, bot, rows)
            self.assertEqual(list_index.index_bot_list(bot, rows), len(rows))
        self.bots = [bot for bot, _rows in self.lists]


class EveryQueryIsAnsweredAsBefore(EquivalenceCase):

    def test_search_returns_the_same_rows_in_the_same_order(self):
        for text in queries():
            terms = list_index.filter_segments(text)
            for limit, expect_limit in ((None, 200), (2000, 2000), (7, 7)):
                for bots in (None, self.bots, ["BotA", "botA/RAR"], ["Bot"], ["^_^"]):
                    with self.subTest(text=text, limit=limit, bots=bots):
                        self.assertEqual(
                            list_index.search(terms, limit=limit, bots=bots),
                            old_search(self.old, terms, expect_limit, bots))

    def test_the_lists_with_a_match_are_the_same(self):
        asked = self.bots + ["Bot", "NotHeld", "bota"]
        for text in queries():
            terms = list_index.filter_segments(text)
            with self.subTest(text=text):
                self.assertEqual(list_index.bots_with_a_match(terms, asked),
                                 old_bots_with_a_match(self.old, terms, asked))

    def test_the_comparison_is_not_empty(self):
        """A reference that matched nothing would make the two tests above
        pass by agreeing on nothing."""
        hits = [text for text in queries()
                if len(old_search(self.old, list_index.filter_segments(text), 2000, None)) > 200]
        self.assertGreater(len(hits), 5, "too few queries reach past one page")
        one_letter_phrase = old_search(self.old, ["love m"], 2000, None)
        self.assertTrue(one_letter_phrase)
        non_ascii = old_search(self.old, ["δέ"], 2000, None)
        self.assertTrue(non_ascii)

    def test_every_heading_comes_back_as_it_was_written(self):
        """Case twins, the empty heading and a missing one, each exactly."""
        shown = {row["folder"] for row in list_index.search(["a"], limit=2000)}
        shown |= {row["folder"] for row in list_index.search(["love"], limit=2000)}
        self.assertEqual(shown, {f or "" for f in FOLDERS})


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


class UpgradeCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-index-upgrade-")
        self.path = os.path.join(self.dir, "idx.db")
        self.set_config(LIST_INDEX_FILE=self.path)
        list_index.reset_for_tests()
        self.addCleanup(list_index.reset_for_tests)
        self.held = {
            "boombox": held_list(self.dir, "BoomBox", "Enter Sandman.flac"),
            "otherbot": held_list(self.dir, "OtherBot", "Alabama Song.mp3")}
        self.set_config(fetched_bot_lists=self.held)
        # What an install upgrading from schema 1 has: both lists indexed.
        with contextlib.closing(old_schema(self.path)) as conn:
            for key, entry in self.held.items():
                old_index(conn, entry["bot"], list(list_mod.iter_filelist_rows(
                    entry["list_path"], entry["bot"])))

    def quiet(self, call, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = call(*args, **kwargs)
        return result, out.getvalue()

    def stored(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            return {name: sql for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master")}


class AnOldIndexIsRebuiltOnce(UpgradeCase):

    def test_the_old_file_really_is_schema_one(self):
        self.assertNotIn("prefix=", self.stored()["entries"])
        self.assertNotIn("folders", self.stored())

    def test_the_table_gets_the_prefix_index_and_folder_ids(self):
        self.quiet(list_index.search, ["sandman"], bots=["BoomBox"])

        stored = self.stored()
        self.assertIn("prefix='1 2 3 4'", stored["entries"])
        self.assertIn("columnsize=0", stored["entries"])
        self.assertNotIn("entries_docsize", stored,
                         "columnsize=0 has no per-row size table")
        self.assertIn("folders", stored)

    def test_the_first_filter_query_finds_the_held_lists_again(self):
        rows, log = self.quiet(list_index.search, ["sandman"], bots=["BoomBox", "OtherBot"])

        self.assertEqual([(r["filename"], r["folder"]) for r in rows],
                         [("Enter Sandman.flac", "D:\\MUSIC\\Some Folder\\")])
        self.assertIn("Rebuilding the search index once", log)
        (matched, empty), _log = self.quiet(
            list_index.bots_with_a_match, ["al"], ["BoomBox", "OtherBot"])
        self.assertEqual((matched, empty), ({"otherbot"}, {"boombox"}))

    def test_the_startup_backfill_restores_them(self):
        done, log = self.quiet(list_index.backfill_missing, self.held, log=print)

        self.assertEqual(done, 2)
        self.assertIn("Rebuilding the search index once", log)
        self.assertEqual(list_index.indexed_bots(), {"boombox", "otherbot"})

    def test_an_upgrade_opened_by_a_fetch_restores_the_other_lists(self):
        """No startup backfill ran: a fetch completing is what opened the old
        file. Its own list goes in; the other held list must be back before
        the sidebar is answered, not reported as holding nothing."""
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": "Enter Sandman.flac", "folder": "D:\\X\\", "size": "1MB"}],
            "BoomBox")
        self.quiet(list_index.index_bot_list, "BoomBox", rows)

        (matched, empty), _log = self.quiet(
            list_index.bots_with_a_match, ["alabama"], ["BoomBox", "OtherBot"])

        self.assertEqual((matched, empty), ({"otherbot"}, {"boombox"}))

    def test_a_restart_part_way_through_loses_nothing_for_good(self):
        """The rebuild is interrupted after one list - the flag that would have
        run the rest is gone with the process. The next start's backfill finds
        the list still missing and indexes it."""
        rows = list_mod.entries_to_filelist_rows(
            [{"filename": "Enter Sandman.flac", "folder": "D:\\X\\", "size": "1MB"}],
            "BoomBox")
        self.quiet(list_index.index_bot_list, "BoomBox", rows)
        list_index.reset_for_tests()  # the restart

        done, _log = self.quiet(list_index.backfill_missing, self.held, log=print)

        self.assertEqual(done, 1)
        self.assertEqual(list_index.indexed_bots(), {"boombox", "otherbot"})

    def test_it_is_rebuilt_once_and_not_on_every_open(self):
        self.quiet(list_index.backfill_missing, self.held, log=print)
        list_index.reset_for_tests()
        list_index.drop_bot("OtherBot")
        list_index.reset_for_tests()

        _rows, log = self.quiet(list_index.search, ["sandman"])

        self.assertEqual(log, "")
        self.assertEqual(list_index.indexed_bots(), {"boombox"},
                         "a reopened schema-2 index was dropped and rebuilt again")

    def test_a_rebuild_that_fails_half_way_leaves_the_old_table(self):
        """The DROP and the CREATE are one transaction. A CREATE that fails -
        a full disk, say - must not leave a file with no table at all and the
        old rows gone with nothing to put them back."""
        real = list_index.sqlite3.connect

        class CreateFails:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if sql.startswith("CREATE VIRTUAL TABLE"):
                    raise sqlite3.OperationalError("database or disk is full")
                return self.conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.conn, name)

        list_index.sqlite3.connect = lambda *a, **k: CreateFails(real(*a, **k))
        try:
            rows, log = self.quiet(list_index.search, ["sandman"])
        finally:
            list_index.sqlite3.connect = real

        self.assertEqual(rows, [])
        self.assertIn("database or disk is full", log)
        self.assertNotIn("prefix=", self.stored()["entries"])
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 2)

    def test_a_row_holding_heading_text_still_shows_it(self):
        """A table this version made, written by an older one after a
        downgrade: its rows hold the heading text, not an id. They show the
        text, as they always did, instead of losing the folder."""
        self.quiet(list_index.backfill_missing, self.held, log=print)
        list_index.reset_for_tests()
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("INSERT INTO entries (bot, filename, folder, size) "
                         "VALUES ('boombox', 'Wherever I May Roam.flac', "
                         "'D:\\OLD\\', '2MB')")
            conn.commit()

        rows = list_index.search(["roam"])

        self.assertEqual([r["folder"] for r in rows], ["D:\\OLD\\"])
