"""#1345: the search index wrote several times its own size while filled.

FTS5 flushed its 1 MB in-memory hash as a new segment every few thousand
rows and kept merging them; a bot re-indexing 97 lists wrote about 36 GB for
a 5.2 GB index. The index now uses a 16 MB hash and merges every 8 segments
(measured: 13.1 GB -> 8.0 GB written, 272 s -> 195 s, on 6.78M real rows).

And every refresh deleted the old rows with `WHERE bot = ?`, which FTS5 can
only answer by reading every row of every list. The rows are found through
the index now, with `bot = ?` kept to make the delete exact.

Every bot and file name here is invented.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list_index  # noqa: E402
import runtime  # noqa: E402

from tests.test_crosslist_search import IndexCase  # noqa: E402


def config_of(conn, key):
    row = conn.execute("SELECT v FROM entries_config WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


class TheWriteSettings(IndexCase):

    def test_a_new_index_has_them(self):
        with runtime.list_index_lock:
            conn = list_index._connect()
            self.assertEqual(config_of(conn, "hashsize"), 16 * 1024 * 1024)
            self.assertEqual(config_of(conn, "automerge"), 8)

    def test_an_index_made_before_them_gets_them_on_open(self):
        with runtime.list_index_lock:
            conn = list_index._connect()
            conn.execute("DELETE FROM entries_config WHERE k IN ('hashsize', 'automerge')")
            conn.commit()
        list_index.close()

        with runtime.list_index_lock:
            conn = list_index._connect()
            self.assertEqual(config_of(conn, "hashsize"), 16 * 1024 * 1024)
            self.assertEqual(config_of(conn, "automerge"), 8)


class TheDeleteFindsTheRowsThroughTheIndex(IndexCase):

    def deletes_run(self, action):
        statements = []
        with runtime.list_index_lock:
            conn = list_index._connect()
            conn.set_trace_callback(statements.append)
        try:
            action()
        finally:
            with runtime.list_index_lock:
                list_index._connect().set_trace_callback(None)
        return [s for s in statements if s.lstrip().upper().startswith("DELETE FROM ENTRIES")]

    def test_a_refresh_asks_the_index(self):
        self.index("SomeBot", "01 - Old Song.flac")

        deletes = self.deletes_run(lambda: self.index("SomeBot", "01 - New Song.flac"))

        self.assertEqual(len(deletes), 1, deletes)
        self.assertIn("MATCH", deletes[0])
        self.assertEqual([r["filename"] for r in list_index.search(["song"])], ["01 - New Song.flac"])

    def test_dropping_a_list_asks_it_too(self):
        self.index("SomeBot", "01 - Old Song.flac")

        deletes = self.deletes_run(lambda: list_index.drop_bot("SomeBot"))

        self.assertIn("MATCH", deletes[0])
        self.assertEqual(list_index.search(["song"]), [])

    def test_a_list_whose_name_reads_as_the_same_phrase_is_left_alone(self):
        """some_bot and some-bot tokenise to the same phrase; only the exact
        name's rows go."""
        self.index("some_bot", "01 - First Song.flac")
        self.index("some-bot", "02 - Second Song.flac")

        self.index("some_bot", "03 - Third Song.flac")
        list_index.drop_bot("some_bot")

        self.assertEqual([r["filename"] for r in list_index.search(["song"])], ["02 - Second Song.flac"])

    def test_a_name_with_no_tokens_is_still_replaced_and_dropped(self):
        self.index("^_^", "01 - Old Song.flac")
        self.index("^_^", "01 - New Song.flac")
        self.assertEqual([r["filename"] for r in list_index.search(["song"])], ["01 - New Song.flac"])

        list_index.drop_bot("^_^")
        self.assertEqual(list_index.search(["song"]), [])


if __name__ == "__main__":
    unittest.main()
