"""A bot whose nick is only symbols is found in the search index (#1091).

The index tokenises with unicode61, which keeps letters and digits and splits
on everything else. A nick made only of IRC's special characters - ^_^, [_],
|-| - is then no phrase at all, so `bot:"^_^"` matched no row whether the bot
had rows or not. Startup took such a list as missing and read it from disk and
wrote it into the index again on every start, and the filter bar greyed it out
as holding no match. Those names are asked by the stored name alone now.
"""

import io
import os

from tests import support  # noqa: F401  (path setup)

import list_index  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402

NL = chr(10)
BS = chr(92)
SYMBOL_NICKS = ("^_^", "[_]", "|-|", "[]", "_")


class TheStartupCheck(crosslist.IndexCase):
    def held_list(self, bot, *filenames):
        """As the cross-list tests write one, under a file name any system
        takes: | and the like are not allowed in one on Windows."""
        path = os.path.join(self.index_dir, f"held-{len(os.listdir(self.index_dir))}-list.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(f"List of {len(filenames)} Files" + NL + NL)
            handle.write("=" * 20 + NL + "D:" + BS + "MUSIC" + BS + "Some Folder" + BS + NL + "=" * 20 + NL)
            for name in filenames:
                handle.write(f"!{bot} {name}  ::INFO:: 4.00MB" + NL)
        return {"bot": bot, "fetched_at": 1, "entry_count": len(filenames), "list_path": path}

    def test_an_indexed_symbol_nick_is_not_read_again(self):
        for nick in SYMBOL_NICKS:
            with self.subTest(nick=nick):
                self.index(nick, "Enter Sandman.flac")
                held = {nick: self.held_list(nick, "Enter Sandman.flac")}
                self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 0)

    def test_one_not_indexed_yet_still_is(self):
        self.index("[_]", "Enter Sandman.flac")
        held = {"^_^": self.held_list("^_^", "A Song.flac")}
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)

    def test_names_with_letters_still_ask_the_index(self):
        self.index("Dude^", "Enter Sandman.flac")
        conn = list_index._connect()
        seen = []
        conn.set_trace_callback(seen.append)
        try:
            with list_index._conn_lock:
                self.assertTrue(list_index._holds_rows_for(conn, "Dude^"))
        finally:
            conn.set_trace_callback(None)
        asked = [s for s in seen if not s.startswith("--")]   # FTS5's own reads
        self.assertTrue(asked and all("MATCH" in s.upper() for s in asked), seen)


class TheFilterBar(crosslist.IndexCase):
    def test_a_symbol_nick_with_a_match_is_not_greyed_out(self):
        self.index("^_^", "Enter Sandman.flac")
        self.index("[_]", "Something Else.flac")
        matched, empty = list_index.bots_with_a_match(["sandman"], ["^_^", "[_]"])
        self.assertEqual((matched, empty), ({"^_^"}, {"[_]"}))

    def test_another_symbol_nick_with_the_file_does_not_count_for_it(self):
        """With no bot phrase the equality alone decides."""
        self.index("|-|", "Enter Sandman.flac")
        matched, empty = list_index.bots_with_a_match(["sandman"], ["^_^"])
        self.assertEqual((matched, empty), (set(), {"^_^"}))


class TheTokenRule(support.DCCoreTestCase):
    def test_letters_or_digits_make_a_token(self):
        for name in ("dude", "^dude^", "[7]", "Ünal", "ボット"):
            with self.subTest(name=name):
                self.assertTrue(list_index._has_tokens(name))
        for name in SYMBOL_NICKS + ("`{}", "\\"):
            with self.subTest(name=name):
                self.assertFalse(list_index._has_tokens(name))
