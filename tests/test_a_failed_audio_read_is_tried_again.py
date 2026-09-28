"""An audio file that could not be read is read again next time (#973).

A read that failed with an I/O error - a network mount's EIO or timeout, a
sharing violation on Windows while another program holds the file - came back
as "nothing to say", was stored like a real answer, and every later rebuild
reused it: the file showed no length or quality until its size changed. Now
it shows its size alone this time and is not remembered, so the next rebuild
reads it again. A file that makes no sense is still an answer, remembered.
"""

import errno
import os
import unittest

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_list_says_how_long_and_how_good as long_and_good  # noqa: E402

SUFFIX = "0m26s 128/44.1/JS"


class ReadAgain(long_and_good.FileCase):
    def setUp(self):
        super().setUp()
        self.path = self.write("a.mp3", long_and_good.frames(1000))
        self.reads = []
        self.failing = True

    def reader(self, path, size=None):
        self.reads.append(os.path.basename(path))
        if self.failing:
            raise OSError(errno.EIO, "Input/output error")
        return audio_info.read(path, size)

    def build(self, publish=True):
        cache = audio_info.Cache.open(reader=self.reader)
        self.addCleanup(cache.close)
        cache.note("k", self.path, os.path.getsize(self.path))
        cache.read_pending(workers=1)
        if publish:
            cache.publish()
        cache.close()
        return cache

    def test_a_read_that_failed_is_read_again(self):
        first = self.build()
        self.assertEqual(first.suffix("k"), "", "size alone this time")
        self.assertEqual(first.unread, {"k"})

        self.failing = False
        second = self.build()
        self.assertEqual(self.reads, ["a.mp3", "a.mp3"])
        self.assertEqual(second.suffix("k"), SUFFIX)

        third = self.build()
        self.assertEqual(len(self.reads), 2, "and remembered once it was read")
        self.assertEqual(third.suffix("k"), SUFFIX)

    def test_also_after_a_rebuild_that_did_not_publish(self):
        self.build(publish=False)
        self.failing = False
        self.assertEqual(self.build().suffix("k"), SUFFIX)
        self.assertEqual(len(self.reads), 2)

    def test_a_file_that_makes_no_sense_is_still_an_answer(self):
        self.path = self.write("a.mp3", b"not audio at all" * 100)
        self.failing = False
        self.build()
        self.build()
        self.assertEqual(self.reads, ["a.mp3"], "read once, remembered as nothing")


class WhatReadRaises(long_and_good.FileCase):
    def test_an_io_error_is_raised_not_swallowed(self):
        path = self.write("a.mp3", long_and_good.frames(10))

        def unreadable(*args, **kwargs):
            raise PermissionError(errno.EACCES, "The process cannot access the file")

        audio_info.open = unreadable
        self.addCleanup(delattr, audio_info, "open")
        with self.assertRaises(OSError):
            audio_info.read(path)

    def test_a_file_gone_since_the_walk_is_none(self):
        self.assertIsNone(audio_info.read(os.path.join(self.tree.music, "gone.mp3")))


if __name__ == "__main__":
    unittest.main()
