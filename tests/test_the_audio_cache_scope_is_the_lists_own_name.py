"""The audio-info cache is kept under the list's own name (#979).

Each list keeps its own rows in the cache, under a scope. A lone list was built
with no name - scope "" - and each of several by its name, so adding a second
list moved the primary's scope from "" to its name: its whole cache missed at
once, most files lost their length and quality for rebuilds while it was read
again within LIST_AUDIO_INFO_MINUTES, and the "" rows were never pruned.
Removing the list flipped it back. Now the scope is the list's name however the
rebuild was asked for, and the primary takes the old "" rows over once.
"""

import io
import os
import sqlite3
import unittest
from contextlib import redirect_stdout

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402
import library  # noqa: E402
import update_list  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_list_says_how_long_and_how_good as long_and_good  # noqa: E402


class TheRebuildAsksForTheName(long_and_good.FileCase):
    def setUp(self):
        super().setUp()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", RAR_ENABLED=False, LIST_FORMAT="txt",
                        LIST_SHOW_AUDIO_INFO=True)
        self.write("Example Artist - 01 - Opening.mp3", long_and_good.frames(100), folder="Album")
        self.scopes = []
        real_open = audio_info.Cache.open.__func__

        def recording_open(cls, *args, **kwargs):
            self.scopes.append((kwargs.get("scope"), kwargs.get("formerly")))
            return real_open(cls, *args, **kwargs)

        audio_info.Cache.open = classmethod(recording_open)
        self.addCleanup(setattr, audio_info.Cache, "open", classmethod(real_open))

    def build(self, *name):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(update_list.generate_master_list(*name))

    def test_a_lone_list_uses_its_name_and_takes_the_old_rows_over(self):
        self.assertTrue(library.primary_list().name, "a list always has a name")
        self.build()
        self.assertEqual(self.scopes, [(library.primary_list().name, "")])

    def test_built_by_its_name_it_is_the_same_scope(self):
        """How a rebuild of several lists asks for the primary."""
        self.build(library.primary_list().name)
        self.assertEqual(self.scopes, [(library.primary_list().name, "")])


class TheOldRowsAreTakenOver(long_and_good.FileCase):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.tree.root, "audio.db")

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return sorted(conn.execute("SELECT scope, key, suffix FROM audio"))
        finally:
            conn.close()

    def seed(self, *rows):
        cache = audio_info.Cache.open(path=self.db)
        cache.conn.executemany(
            "INSERT INTO audio (scope, key, size, mtime, suffix, run) VALUES (?, ?, 1, 0, ?, 0)", rows)
        cache.conn.commit()
        cache.close()

    def test_moved_to_the_name_where_the_name_has_none(self):
        self.seed(("", "a", "old a"), ("", "b", "old b"), ("music", "b", "new b"), ("video", "c", "film"))
        audio_info.Cache.open(path=self.db, scope="music", formerly="").close()
        self.assertEqual(self.rows(), [("music", "a", "old a"), ("music", "b", "new b"), ("video", "c", "film")])

    def test_nothing_moves_without_it(self):
        self.seed(("", "a", "old a"))
        audio_info.Cache.open(path=self.db, scope="video").close()
        self.assertEqual(self.rows(), [("", "a", "old a")])


if __name__ == "__main__":
    unittest.main()
