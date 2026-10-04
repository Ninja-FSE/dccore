"""A short read gives the same length and quality as a full one (#1189).

audio_info reads each file unbuffered, so every read is one request - and a
raw read() may return FEWER bytes than it asked for. A network mount with cold
caches does exactly that. _Window took one read as the whole answer, so the
parser saw a truncated header: an MP3 whose Xing header it then missed was
taken for CBR, and its quality lost the leading "~" - one byte in the list.
A test bot's cold rebuild of a 62,837-file NFS library came out one byte
shorter than the warm ones, from the same files.

The window now reads until it has what it asked for or the file ends, and
knows the file's size, so the ordinary case is still one request.
"""

import contextlib

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402
from tests.test_the_list_says_how_long_and_how_good import (  # noqa: E402
    CountingOpen, FileCase, flac, frames, id3v2, vbri_frame, xing_frame)


@contextlib.contextmanager
def short_reads(most):
    """audio_info's open(), answering every read with at most `most` bytes,
    the way a network mount under load may."""
    real = open

    class Short:
        def __init__(inner, path, *args, **kwargs):
            inner.file = real(path, *args, **kwargs)

        def read(inner, size=-1):
            if size is None or size < 0:
                return inner.file.read(most)
            return inner.file.read(min(size, most))

        def seek(inner, *args):
            return inner.file.seek(*args)

        def __enter__(inner):
            return inner

        def __exit__(inner, *exc):
            inner.file.close()

    audio_info.open = Short
    try:
        yield
    finally:
        del audio_info.open


FILES = {
    "vbr.mp3": lambda: xing_frame(b"Xing", 2297, 1837592) + frames(5),
    "vbri.mp3": lambda: vbri_frame(2297, 1837592) + frames(5),
    "tagged.mp3": lambda: id3v2() + frames(1000),
    "art.mp3": lambda: id3v2(body_size=200 * 1024) + frames(1000),
    "track.flac": flac,
    "art.flac": lambda: flac(picture=3 * 1024 * 1024),
}


class AShortReadIsReadAgain(FileCase):

    def full(self):
        return {name: self.described(name, make()) for name, make in FILES.items()}

    def test_every_answer_is_the_same_whatever_size_the_reads_come_back(self):
        expected = self.full()
        self.assertTrue(all(expected.values()), expected)
        self.assertTrue(expected["vbr.mp3"].split(" ")[1].startswith("~"), expected["vbr.mp3"])
        for most in (7, 512, 4096, 10000):
            with self.subTest(most=most), short_reads(most):
                got = {name: self.described(name, make()) for name, make in FILES.items()}
                self.assertEqual(got, expected)

    def test_an_ordinary_file_is_still_one_request(self):
        counted = CountingOpen(self)
        self.described("vbr.mp3", FILES["vbr.mp3"]())
        self.described("track.flac", flac())
        self.assertEqual(counted.reads, [1, 1])

    def test_a_file_shorter_than_the_first_read_costs_no_empty_read(self):
        """Knowing the size, the window stops at the end of the file instead
        of asking again to be told so."""
        counted = CountingOpen(self)
        small = xing_frame(b"Xing", 2297, 1837592) + frames(1)
        self.assertLess(len(small), audio_info.FIRST_READ)
        self.described("small.mp3", small)
        self.assertEqual(counted.reads, [1])
