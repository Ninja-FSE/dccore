"""#1273: a damaged audio_info.db stayed where it was for ever.

Cache.open() answered None on any sqlite3 error, so a cache file damaged by a
torn write or a bad sector ("file is not a database") made every rebuild from
then on write its list with sizes only, saying it could not open the cache,
until the operator found the file and deleted it. transfer_log.py,
list_index.py and the download counts move a damaged file aside and start a
new one; the audio cache now does the same. A fresh cache costs reading each
audio file once more. A locked file or a full disk is not damage, and leaves
the file where it is.
"""

import os
import sqlite3
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase  # noqa: E402

import audio_info  # noqa: E402


class TheAudioCache(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.folder = self.make_temp_dir()
        self.path = os.path.join(self.folder, "audio_info.db")
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE audio (scope TEXT, key TEXT, size INTEGER, mtime INTEGER, "
                     "suffix TEXT, run INTEGER, PRIMARY KEY (scope, key))")
        conn.executemany("INSERT INTO audio VALUES (?, ?, ?, 0, ?, 0)",
                         [("", "song-%d" % n, n, "3m0s") for n in range(500)])
        conn.commit()
        conn.close()
        self.said = []

    def open(self):
        cache = audio_info.Cache.open(self.path, log=self.said.append)
        if cache is not None:
            self.addCleanup(cache.conn.close)
        return cache

    def damage(self):
        # The header page overwritten, as a torn write or a bad sector leaves it.
        with open(self.path, "r+b") as handle:
            handle.write(b"\0" * 100)

    def moved(self):
        return sorted(name for name in os.listdir(self.folder) if ".corrupt-" in name)

    def test_a_healthy_cache_opens_as_it_is(self):
        cache = self.open()
        self.assertEqual(len(cache.known), 500)
        self.assertEqual(self.moved(), [])
        self.assertEqual(self.said, [])

    def test_a_damaged_cache_is_moved_aside_and_a_new_one_started(self):
        self.damage()
        cache = self.open()
        self.assertIsNotNone(cache, "a damaged cache must not mean sizes only for ever")
        self.assertEqual(cache.known, {})
        (aside,) = self.moved()
        self.assertTrue(aside.startswith("audio_info.db.corrupt-"), aside)
        self.assertEqual(len(self.said), 1, self.said)
        self.assertIn("damaged", self.said[0])
        self.assertIn(aside, self.said[0])

        # The next rebuild opens the new cache, and moves nothing else.
        cache.conn.close()
        self.said.clear()
        self.assertIsNotNone(self.open())
        self.assertEqual(self.moved(), [aside])
        self.assertEqual(self.said, [])

    def test_its_wal_and_shm_go_with_it(self):
        # sqlite3 removes them itself when the failed open's connection
        # closes, unless something else holds them; the move is for that case.
        for suffix in ("-wal", "-shm"):
            with open(self.path + suffix, "wb") as handle:
                handle.write(b"left over from the damaged file")
        aside = audio_info._move_aside(self.path)
        self.assertEqual(self.moved(), sorted(os.path.basename(aside) + suffix
                                              for suffix in ("", "-shm", "-wal")))
        self.assertFalse(os.path.exists(self.path + "-wal"))
        self.assertFalse(os.path.exists(self.path + "-shm"))

    def test_a_locked_file_is_not_damage_and_stays_where_it_is(self):
        real_open = audio_info.Cache._open

        def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        self.addCleanup(setattr, audio_info.Cache, "_open", real_open)
        audio_info.Cache._open = classmethod(locked)
        self.assertIsNone(self.open())
        self.assertEqual(self.moved(), [])
        self.assertTrue(os.path.isfile(self.path))
        self.assertIn("written with sizes only", self.said[-1])

    def test_a_damaged_file_that_cannot_be_moved_says_to_delete_it(self):
        self.damage()
        real_move = audio_info._move_aside

        def refused(path):
            raise PermissionError(13, "The process cannot access the file", path)

        self.addCleanup(setattr, audio_info, "_move_aside", real_move)
        audio_info._move_aside = refused
        self.assertIsNone(self.open())
        self.assertTrue(os.path.isfile(self.path))
        self.assertIn("Delete the file", self.said[-1])


if __name__ == "__main__":
    unittest.main()
