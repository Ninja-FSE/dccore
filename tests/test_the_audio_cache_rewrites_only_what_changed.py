"""#1137: publishing the audio cache rewrites only the rows that changed.

Every published rebuild with LIST_SHOW_AUDIO_INFO on deleted its whole scope
from the cache and inserted one row per audio file again - a million rows on
an unchanged library, which is the ordinary weekly run, while searches were
still held. Now only what changed is touched: rows this rebuild did not see
are deleted, rows whose read failed with an I/O error (#973) are deleted, and
rows that are new or differ from what is stored are written.

What must not change is what the table holds afterwards. The tests run the
same rebuild against two copies of one seeded cache - one published the new
way, one the old way, kept here verbatim - and compare the tables, with every
kind of row in play: unchanged, changed and read, changed with a failed read,
gone, new, new with a failed read, new with nothing to say, and left unread
when the time ran out. A row in another list's scope must survive both.

Separately, the list of files to read was meant to be released after reading,
but the line doing it sat after a return in rate() and never ran: on a cold
first run the whole library's (key, path, size) list stayed in memory.
"""

import errno
import os
import shutil
import sqlite3
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402

SCOPE = "SomeList"


def info(seconds):
    return {"seconds": seconds, "kbps": 320, "rate": 44100, "channels": "JS"}


def old_publish(cache):
    """Cache.publish() as it was before #1137."""
    with cache.conn:
        cache.conn.execute("DELETE FROM audio WHERE scope = ?", (cache.scope,))
        cache._save((key, (cache.sizes[key], suffix)) for key, suffix in cache.seen.items()
                    if key not in cache.unread)
    cache.published = True


class Clock:
    """The budget's clock, read once to set the deadline and once before
    each read is started: past the deadline after `reads` reads."""

    def __init__(self, reads):
        self.calls = 0
        self.reads = reads

    def __call__(self):
        self.calls += 1
        return 0 if self.calls <= self.reads + 1 else 10 ** 9


class SeededCache(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="dccore-audio-cache-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.seed = os.path.join(self.folder, "seed.db")
        conn = sqlite3.connect(self.seed)
        conn.execute("CREATE TABLE audio (scope TEXT, key TEXT, size INTEGER, "
                     "mtime INTEGER, suffix TEXT, run INTEGER, PRIMARY KEY (scope, key))")
        rows = [(SCOPE, f"same{n}", 1000 + n, 0, f"{n}m0s 320/44.1/JS", 0) for n in range(40)]
        rows += [(SCOPE, f"grown{n}", 500, 0, "1m0s 320/44.1/JS", 0) for n in range(5)]
        rows += [(SCOPE, f"grown-unreadable{n}", 600, 0, "2m0s 320/44.1/JS", 0) for n in range(5)]
        rows += [(SCOPE, f"gone{n}", 700, 0, "3m0s 320/44.1/JS", 0) for n in range(5)]
        rows += [(SCOPE, f"grown-late{n}", 800, 0, "4m0s 320/44.1/JS", 0) for n in range(3)]
        rows += [(SCOPE, "nothing-to-say", 900, 0, "", 0)]
        rows += [("Other", f"same{n}", 1000 + n, 0, "other", 0) for n in range(5)]
        conn.executemany("INSERT INTO audio VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    @staticmethod
    def reader(path, size=None):
        name = os.path.basename(path)
        if "unreadable" in name:
            raise OSError(errno.EIO, "Input/output error")
        if name.startswith("silent"):
            return None
        return info(60 + size % 60)

    def rebuild(self, name, publish):
        """One rebuild against a fresh copy of the seed; returns (cache, the
        scope's rows afterwards, every row afterwards, rows changed)."""
        path = os.path.join(self.folder, f"{name}.db")
        shutil.copyfile(self.seed, path)
        cache = audio_info.Cache.open(path=path, reader=self.reader, scope=SCOPE, log=lambda *_a: None)
        self.assertIsNotNone(cache)
        for n in range(40):
            cache.note(f"same{n}", f"same{n}.mp3", 1000 + n)
        for n in range(5):
            cache.note(f"grown{n}", f"grown{n}.mp3", 501)
            cache.note(f"grown-unreadable{n}", f"grown-unreadable{n}.mp3", 601)
            cache.note(f"new{n}", f"new{n}.mp3", 100 + n)
        for n in range(3):
            cache.note(f"new-unreadable{n}", f"new-unreadable{n}.mp3", 50)
            cache.note(f"silent{n}", f"silent{n}.mp3", 40)
        cache.note("nothing-to-say", "nothing-to-say.mp3", 900)
        # Read last, and the budget runs out before them: left for next time.
        for n in range(3):
            cache.note(f"grown-late{n}", f"grown-late{n}.mp3", 801)
        cache.read_pending(workers=1, budget=5, clock=Clock(len(cache.pending) - 3))
        before = cache.conn.total_changes
        publish(cache)
        changed = cache.conn.total_changes - before
        scope_rows = sorted(cache.conn.execute(
            "SELECT key, size, suffix FROM audio WHERE scope = ?", (SCOPE,)))
        every_row = sorted(cache.conn.execute("SELECT scope, key, size, suffix FROM audio"))
        cache.close()
        return cache, scope_rows, every_row, changed


class TheTableEndsUpTheSame(SeededCache):
    def test_the_table_is_what_the_old_publish_left(self):
        _new, new_rows, new_all, _changed = self.rebuild("new", audio_info.Cache.publish)
        _old, old_rows, old_all, _changed = self.rebuild("old", old_publish)
        self.assertEqual(new_all, old_all)
        self.assertEqual(new_rows, old_rows)

    def test_the_scenario_has_every_kind_of_row(self):
        """Guards the comparison above: a seed that exercised nothing would
        make it pass for free."""
        cache, rows, every, _changed = self.rebuild("new", audio_info.Cache.publish)
        keys = {key for key, _size, _suffix in rows}
        self.assertEqual(cache.left_count, 3)
        self.assertEqual(cache.unread, {f"grown-unreadable{n}" for n in range(5)}
                         | {f"new-unreadable{n}" for n in range(3)})
        self.assertTrue({"same0", "grown0", "new0", "silent0", "nothing-to-say"} <= keys)
        self.assertFalse(keys & {"gone0", "grown-unreadable0", "new-unreadable0", "grown-late0"})
        self.assertEqual(len([row for row in every if row[0] == "Other"]), 5)

    def test_a_row_whose_read_failed_loses_its_old_suffix(self):
        """The skeptic's case: cached, grown, and the re-read failed. Kept, its
        old suffix would be served without a read if the file went back to its
        old size."""
        self.rebuild("new", audio_info.Cache.publish)
        path = os.path.join(self.folder, "new.db")
        again = audio_info.Cache.open(path=path, reader=self.reader, scope=SCOPE, log=lambda *_a: None)
        self.addCleanup(again.close)
        again.note("grown-unreadable0", "grown-unreadable0.mp3", 600)
        self.assertEqual(len(again.pending), 1)
        self.assertEqual(again.suffix("grown-unreadable0"), "")


class OnlyWhatChangedIsWritten(SeededCache):
    def test_an_unchanged_row_is_not_rewritten(self):
        """Changes counted by SQLite: one per deleted row and one per row
        written. The old publish touched every row twice."""
        _cache, rows, _every, changed = self.rebuild("new", audio_info.Cache.publish)
        _old, _rows, _every, old_changed = self.rebuild("old", old_publish)
        deleted = 5 + 5 + 3      # gone, grown-unreadable, grown-late
        written = 5 + 5 + 3      # grown, new, silent
        self.assertEqual(changed, deleted + written)
        self.assertGreater(old_changed, 2 * 40)
        self.assertEqual(len(rows), 40 + 5 + 5 + 3 + 1)

    def test_a_second_rebuild_of_an_unchanged_library_writes_nothing(self):
        path = os.path.join(self.folder, "steady.db")
        shutil.copyfile(self.seed, path)
        cache = audio_info.Cache.open(path=path, reader=self.reader, scope=SCOPE, log=lambda *_a: None)
        for n in range(40):
            cache.note(f"same{n}", f"same{n}.mp3", 1000 + n)
        cache.publish()
        cache.close()

        cache = audio_info.Cache.open(path=path, reader=self.reader, scope=SCOPE, log=lambda *_a: None)
        self.addCleanup(cache.close)
        for n in range(40):
            cache.note(f"same{n}", f"same{n}.mp3", 1000 + n)
        self.assertEqual(cache.pending, [])
        before = cache.conn.total_changes
        cache.publish()
        self.assertEqual(cache.conn.total_changes - before, 0)
        self.assertEqual(cache.suffix("same7"), "7m0s 320/44.1/JS")


class ThePendingListIsReleased(unittest.TestCase):
    def test_reading_releases_the_list_of_files_to_read(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE audio (scope TEXT, key TEXT, size INTEGER, "
                     "mtime INTEGER, suffix TEXT, run INTEGER, PRIMARY KEY (scope, key))")
        cache = audio_info.Cache(conn, reader=lambda path, size=None: info(61))
        self.addCleanup(conn.close)
        for n in range(4):
            cache.note(f"k{n}", f"k{n}.mp3", n)
        self.assertEqual(len(cache.pending), 4)
        cache.read_pending(workers=2)
        self.assertEqual(cache.pending, [])
        self.assertEqual(cache.read_count, 4)
        self.assertEqual(cache.suffix("k3"), "1m1s 320/44.1/JS")


if __name__ == "__main__":
    unittest.main()
