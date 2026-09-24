"""#567: duration and quality after the size, on the list's MP3 and FLAC rows.

    !DCCore Artist - Album - 01 - Track.mp3  ::INFO:: 10.3MB 4m31s 320/44.1/JS

Every audio file here is built byte by byte in the test - frame headers, a
Xing / Info / VBRI header, a FLAC STREAMINFO block - so the expected numbers
come from the formats' own arithmetic, not from a sample file nobody can
regenerate. Names are invented.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import audio_info  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

# MPEG-1 Layer III, 128 kbps, 44.1 kHz, no CRC, no padding. The last byte is
# the channel mode: 0x00 stereo, 0x40 joint stereo, 0x80 dual, 0xC0 mono.
MPEG1_128 = b"\xff\xfb\x90"
FRAME_128 = 417  # 144 * 128000 / 44100, rounded down
# MPEG-2 Layer III, 64 kbps, 22.05 kHz: 72 * 64000 / 22050 -> 208 bytes.
MPEG2_64 = b"\xff\xf3\x80"
FRAME_64 = 208


def frames(count, header=MPEG1_128, mode=0x40, length=FRAME_128):
    one = header + bytes([mode]) + b"\x00" * (length - 4)
    return one * count


def id3v2(body_size=1000):
    size = bytes([(body_size >> 21) & 0x7F, (body_size >> 14) & 0x7F,
                  (body_size >> 7) & 0x7F, body_size & 0x7F])
    # A frame-sync pattern inside the tag, which must be skipped with it.
    body = (b"\xff\xfb\x90\x40" * 8).ljust(body_size, b"\x00")
    return b"ID3\x03\x00\x00" + size + body


def id3v1():
    return b"TAG" + b"\x00" * 125


def xing_frame(tag, frame_count, byte_count, mode=0x40, side=32):
    frame = bytearray(frames(1, mode=mode))
    at = 4 + side
    frame[at:at + 16] = (tag + (3).to_bytes(4, "big") + frame_count.to_bytes(4, "big")
                         + byte_count.to_bytes(4, "big"))
    return bytes(frame)


def vbri_frame(frame_count, byte_count):
    frame = bytearray(frames(1))
    frame[36:54] = (b"VBRI" + (1).to_bytes(2, "big") + b"\x00\x00" + b"\x00\x4b"
                    + byte_count.to_bytes(4, "big") + frame_count.to_bytes(4, "big"))
    return bytes(frame)


def flac(rate=44100, channels=2, bits=16, samples=44100 * 2, audio_bytes=278750, padding=100,
         picture=0):
    """A FLAC: STREAMINFO, then a PICTURE block of `picture` bytes if asked
    for, then a last PADDING block, then the audio."""
    word = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | samples
    streaminfo = (b"\x10\x00\x10\x00" + b"\x00" * 6 + word.to_bytes(8, "big") + b"\x00" * 16)
    blocks = bytes([0x00]) + len(streaminfo).to_bytes(3, "big") + streaminfo
    if picture:
        blocks += bytes([0x06]) + picture.to_bytes(3, "big") + b"\x00" * picture
    blocks += bytes([0x81]) + padding.to_bytes(3, "big") + b"\x00" * padding
    return b"fLaC" + blocks + b"\x55" * audio_bytes


class FileCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def write(self, name, data, folder=None):
        directory = os.path.join(self.tree.music, folder) if folder else self.tree.music
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def described(self, name, data):
        return audio_info.describe(audio_info.read(self.write(name, data)))


class ReadingMp3(FileCase):
    def test_cbr_from_its_bytes_and_bitrate(self):
        # 1000 frames of 417 bytes at 128 kbps: 417000 * 8 / 128000 = 26.06 s.
        self.assertEqual(self.described("cbr.mp3", frames(1000)), "0m26s 128/44.1/JS")

    def test_a_leading_tag_is_not_audio_and_a_trailing_one_changes_nothing(self):
        # An ID3v1 tag at the end is no longer looked for (#914): a request of
        # its own, for 128 bytes - 8 ms of a 128 kbps file.
        data = id3v2() + frames(1000) + id3v1()
        self.assertEqual(self.described("tagged.mp3", data), "0m26s 128/44.1/JS")

    def test_a_tag_bigger_than_the_search_window_is_skipped(self):
        """Embedded cover art makes ID3v2 tags of hundreds of KB; the frame
        search only looks MP3_SYNC_WINDOW past where the tag ends."""
        data = id3v2(body_size=audio_info.MP3_SYNC_WINDOW * 2) + frames(1000)
        self.assertEqual(self.described("cover-art.mp3", data), "0m26s 128/44.1/JS")

    def test_a_false_sync_before_the_audio_is_passed_over(self):
        """A header-shaped pattern whose "next frame" is not there."""
        junk = bytes([0xFF, 0xFB, 0xE0, 0x40]) + bytes(10)  # claims 320 kbps
        info = audio_info.read(self.write("junk.mp3", junk + frames(1000)))
        self.assertEqual(audio_info.describe(info), "0m26s 128/44.1/JS")

    def test_the_channel_modes(self):
        for mode, word in ((0x00, "S"), (0x40, "JS"), (0x80, "DC"), (0xC0, "M")):
            self.assertTrue(self.described(f"mode{mode}.mp3", frames(1000, mode=mode)).endswith("/" + word))

    def test_vbr_is_the_xing_frame_count_and_an_average(self):
        # 2297 frames * 1152 / 44100 = 60.003 s; 1837592 bytes over that = 245 kbps.
        data = xing_frame(b"Xing", 2297, 1837592) + frames(5)
        self.assertEqual(self.described("vbr.mp3", data), "1m0s ~245/44.1/JS")

    def test_a_mono_xing_header_sits_closer(self):
        data = xing_frame(b"Xing", 2297, 1837592, mode=0xC0, side=17) + frames(5, mode=0xC0)
        self.assertEqual(self.described("vbr-mono.mp3", data), "1m0s ~245/44.1/M")

    def test_an_info_header_is_cbr_with_an_exact_length(self):
        data = xing_frame(b"Info", 2297, 0) + frames(5)
        self.assertEqual(self.described("info.mp3", data), "1m0s 128/44.1/JS")

    def test_vbri(self):
        data = vbri_frame(2297, 1837592) + frames(5)
        self.assertEqual(self.described("vbri.mp3", data), "1m0s ~245/44.1/JS")

    def test_mpeg2_rates(self):
        # 1000 frames of 208 bytes at 64 kbps: 208000 * 8 / 64000 = 26 s.
        data = frames(1000, header=MPEG2_64, mode=0x00, length=FRAME_64)
        self.assertEqual(self.described("low.mp3", data), "0m26s 64/22.05/S")


class ReadingFlac(FileCase):
    def test_duration_and_real_bitrate(self):
        # 88200 samples at 44.1 kHz = 2 s; 278750 audio bytes * 8 / 2 = 1115 kbps.
        self.assertEqual(self.described("track.flac", flac()), "0m2s 1115/44.1/S")

    def test_channels_and_rates(self):
        self.assertEqual(self.described("mono.flac", flac(channels=1)), "0m2s 1115/44.1/M")
        six = self.described("six.flac", flac(rate=96000, channels=6, samples=96000 * 2))
        self.assertEqual(six, "0m2s 1115/96/6ch")

    def test_a_big_picture_block_is_skipped_not_read(self):
        data = flac(padding=3 * 1024 * 1024)
        self.assertEqual(self.described("art.flac", data), "0m2s 1115/44.1/S")


class NothingItCannotReadIsGuessed(FileCase):
    def test_every_bad_file_is_none(self):
        cases = {
            "empty.mp3": b"",
            "noise.mp3": bytes(range(256)) * 40,
            "cut.flac": flac()[:20],
            "notflac.flac": b"OggS" + b"\x00" * 100,
            "zero-samples.flac": flac(samples=0),
            "tag-only.mp3": id3v2(),
        }
        for name, data in cases.items():
            self.assertIsNone(audio_info.read(self.write(name, data)), name)

    def test_other_files_are_never_opened(self):
        self.assertIsNone(audio_info.read(self.write("cover.jpg", frames(10))))
        self.assertFalse(audio_info.is_audio("cover.jpg"))
        self.assertTrue(audio_info.is_audio("Loud.MP3"))

    def test_a_missing_file_is_none_not_an_exception(self):
        self.assertIsNone(audio_info.read(os.path.join(self.tree.music, "gone.mp3")))

    def test_describe_of_nothing_is_empty(self):
        self.assertEqual(audio_info.describe(None), "")


class CountingOpen:
    """audio_info's open(), counting the reads each file costs - on a network
    mount every one is a round trip (#914)."""

    def __init__(self, case):
        self.reads = []
        real = open

        class Counted:
            def __init__(inner, path, *args, **kwargs):
                inner.file = real(path, *args, **kwargs)
                self.reads.append(0)

            def read(inner, *args):
                self.reads[-1] += 1
                return inner.file.read(*args)

            def seek(inner, *args):
                return inner.file.seek(*args)

            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                inner.file.close()

        audio_info.open = Counted
        case.addCleanup(delattr, audio_info, "open")


class FewRequestsPerFile(FileCase):
    def test_an_ordinary_mp3_or_flac_is_one_read(self):
        counted = CountingOpen(self)
        self.described("cbr.mp3", id3v2() + frames(1000))
        self.described("vbr.mp3", xing_frame(b"Xing", 2297, 1837592) + frames(5))
        self.described("track.flac", flac())
        self.assertEqual(counted.reads, [1, 1, 1])

    def test_cover_art_costs_one_more(self):
        counted = CountingOpen(self)
        self.assertEqual(self.described("art.mp3", id3v2(body_size=200 * 1024) + frames(1000)),
                         "0m26s 128/44.1/JS")
        # The picture is stepped over: the block header after it is fetched
        # where it is, not read up to.
        self.assertEqual(self.described("art.flac", flac(picture=3 * 1024 * 1024)), "0m2s 1115/44.1/S")
        # A big LAST block needs nothing after it: the audio starts there.
        self.assertEqual(self.described("pad.flac", flac(padding=3 * 1024 * 1024)), "0m2s 1115/44.1/S")
        self.assertEqual(counted.reads, [2, 2, 1])


class TheCache(FileCase):
    def counting(self):
        self.reads = []

        def reader(path, size=None):
            self.reads.append(os.path.basename(path))
            return audio_info.read(path, size)
        return reader

    def open(self, scope=""):
        cache = audio_info.Cache.open(reader=self.reader, scope=scope)
        self.addCleanup(cache.close)
        return cache

    def build(self, scope="", keys=("k",), publish=True):
        """One rebuild's worth: note, read, publish, close."""
        cache = self.open(scope)
        for key in keys:
            cache.note(key, self.path, os.path.getsize(self.path))
        cache.read_pending(workers=2)
        if publish:
            cache.publish()
        cache.close()
        return cache

    def setUp(self):
        super().setUp()
        self.reader = self.counting()
        self.path = self.write("a.mp3", frames(1000))

    def test_an_unchanged_file_is_not_read_again(self):
        self.build()
        second = self.build()
        self.assertEqual(self.reads, ["a.mp3"])
        self.assertEqual(second.suffix("k"), "0m26s 128/44.1/JS")
        self.assertEqual((second.read_count, second.reused_count), (0, 1))

    def test_note_makes_no_request_at_all(self):
        """The whole point on a network mount: an unchanged file costs no
        stat and no read - its size came from the directory listing."""
        self.build()
        calls = []
        real_stat = os.stat
        cache = self.open()
        size = os.path.getsize(self.path)

        def counting_stat(*args, **kwargs):
            calls.append(args[0])
            return real_stat(*args, **kwargs)

        os.stat = counting_stat
        try:
            cache.note("k", self.path, size)
        finally:
            os.stat = real_stat
        self.assertEqual(calls, [])
        self.assertEqual(cache.pending, [])

    def test_a_changed_size_is_read_again(self):
        self.build()
        self.write("a.mp3", frames(2000))
        second = self.build()
        self.assertEqual(self.reads, ["a.mp3", "a.mp3"])
        self.assertEqual(second.suffix("k"), "0m52s 128/44.1/JS")

    def test_a_published_rebuild_forgets_what_it_did_not_see(self):
        self.build(keys=("gone", "kept"))
        self.build(keys=("kept",))
        self.build(keys=("gone", "kept"))
        self.assertEqual(self.reads.count("a.mp3"), 3, "gone and kept once, then gone again")

    def test_a_stopped_rebuild_keeps_what_it_read_and_forgets_nothing(self):
        self.build(keys=("old",))
        self.build(keys=("new",), publish=False)
        self.build(keys=("old", "new"))
        self.assertEqual(len(self.reads), 2, "neither was read twice")

    def test_a_rebuild_that_did_not_see_a_file_has_no_suffix_for_it(self):
        self.build()
        self.assertEqual(self.open().suffix("k"), "")

    def test_one_list_never_prunes_another(self):
        self.build(scope="other")
        self.build(scope="", keys=())
        self.build(scope="other")
        self.assertEqual(self.reads, ["a.mp3"], "the other list's row survived")

    def test_a_cache_that_cannot_open_says_so(self):
        blocker = os.path.join(self.tree.root, "not-a-dir")
        with open(blocker, "w") as handle:
            handle.write("x")
        said = []
        self.assertIsNone(audio_info.Cache.open(path=os.path.join(blocker, "audio.db"), log=said.append))
        self.assertIn("written with sizes only", said[0])


class ReadingManyAtOnce(FileCase):
    def pending(self, count):
        cache = audio_info.Cache.open(reader=self.reader)
        self.addCleanup(cache.close)
        for n in range(count):
            path = self.write(f"t{n}.mp3", frames(10))
            cache.note(f"k{n}", path, os.path.getsize(path))
        return cache

    def test_the_reads_really_overlap(self):
        """Four reads that can only finish if four are in flight together:
        a barrier of four, which a one-at-a-time reader never passes."""
        import threading
        barrier = threading.Barrier(4, timeout=10)

        def reader(path, size=None):
            barrier.wait()
            return audio_info.read(path, size)

        self.reader = reader
        cache = self.pending(4)
        cache.read_pending(workers=4)
        self.assertEqual(cache.read_count, 4)
        self.assertTrue(all(cache.suffix(f"k{n}") for n in range(4)))

    def test_past_the_time_limit_no_read_is_started(self):
        """A clock that says the budget ran out after two reads were started:
        those finish, nothing else starts, and the rest are counted as left."""
        started = []
        ticks = iter([0, 0, 0, 999] + [999] * 50)

        def reader(path, size=None):
            started.append(path)
            return audio_info.read(path, size)

        self.reader = reader
        cache = self.pending(6)
        cache.read_pending(workers=1, budget=60, clock=lambda: next(ticks))
        self.assertEqual(len(started), 2)
        self.assertEqual((cache.read_count, cache.left_count), (2, 4))
        self.assertEqual(cache.suffix("k5"), "")

    def test_a_reader_that_raises_costs_only_its_file(self):
        def reader(path, size=None):
            if path.endswith("t1.mp3"):
                raise OSError("stale NFS handle")
            return audio_info.read(path, size)

        self.reader = reader
        cache = self.pending(3)
        cache.read_pending(workers=2)
        self.assertEqual(cache.suffix("k1"), "")
        self.assertTrue(cache.suffix("k0") and cache.suffix("k2"))

    def test_the_rate_is_measured_on_the_real_clock(self):
        """Files per second of reading - what an operator compares to pick
        LIST_AUDIO_INFO_THREADS - and not taken from the budget's clock,
        which a test (or a frozen clock) can stop."""
        self.reader = audio_info.read
        cache = self.pending(4)
        self.assertIsNone(cache.rate(), "nothing read yet")
        cache.read_pending(workers=2, clock=lambda: 0)
        self.assertGreater(cache.rate(), 0)
        self.assertEqual(cache.workers, 2)

    def test_the_thread_count_is_clamped_to_1_to_128(self):
        """What update_list hands read_pending(): the setting, held to a range
        a typo cannot turn into ten thousand threads or none."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"), encoding="utf-8") as handle:
            code = handle.read()
        self.assertIn('workers = max(1, min(128, int(getattr(config, "LIST_AUDIO_INFO_THREADS", 64) or 1)))', code)
        import defaults
        self.assertEqual(defaults.LIST_AUDIO_INFO_THREADS, 64)

    def test_progress_is_reported(self):
        self.reader = audio_info.read
        cache = self.pending(3)
        seen = []
        cache.read_pending(workers=2, progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (3, 3))


class TheList(FileCase):
    def setUp(self):
        super().setUp()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", RAR_ENABLED=False, LIST_FORMAT="txt")
        self.write("Example Artist - 01 - Opening.mp3", frames(1000), folder="Album")
        self.write("Example Artist - 02 - Closing.flac", flac(), folder="Album")
        self.write("Front.jpg", b"\xff\xd8" + b"\x00" * 500, folder="Album")
        self.write("Broken.mp3", b"not audio at all", folder="Album")

    def rows(self, _clock=None, **overrides):
        self.set_config(**overrides)
        buffer = io.StringIO()
        real = audio_info.Cache.read_pending
        if _clock is not None:
            ticks = iter(_clock)

            def with_clock(cache, **kwargs):
                return real(cache, clock=lambda: next(ticks), **kwargs)
            audio_info.Cache.read_pending = with_clock
        try:
            with redirect_stdout(buffer):
                built = update_list.generate_master_list()
        finally:
            audio_info.Cache.read_pending = real
        self.assertTrue(built, buffer.getvalue())
        path = list_mod.find_latest_list()
        with open(path, encoding="utf-8") as handle:
            return {line.split("  ::INFO:: ")[0].split(" ", 1)[1]: line.rstrip("\n").split("  ::INFO:: ")[1]
                    for line in handle if line.startswith("!")}, buffer.getvalue()

    def test_on_the_audio_rows_carry_it_and_nothing_else_does(self):
        rows, said = self.rows(LIST_SHOW_AUDIO_INFO=True)
        self.assertRegex(rows["Example Artist - 01 - Opening.mp3"], r"^\S+ 0m26s 128/44\.1/JS$")
        self.assertRegex(rows["Example Artist - 02 - Closing.flac"], r"^\S+ 0m2s 1115/44\.1/S$")
        self.assertNotIn(" ", rows["Front.jpg"])
        self.assertNotIn(" ", rows["Broken.mp3"], "unreadable: size only")
        # make_tree() ships a few audio files of its own; every one is read.
        self.assertRegex(said, r"\[LIST-GEN\] Audio info: [1-9]\d* file\(s\) read, 0 unchanged")
        self.assertRegex(said, r"Read at [\d,]+ files a second, 64 at a time\.")
        self.assertRegex(said, r"Reading the length and quality of [1-9]\d* new or changed audio file\(s\), 64 at a time, for at most 5 minute\(s\)")

    def test_off_nothing_is_opened_and_the_rows_are_as_before(self):
        rows, said = self.rows(LIST_SHOW_AUDIO_INFO=False)
        self.assertTrue(all(" " not in tail for tail in rows.values()), rows)
        self.assertNotIn("Audio info", said)
        self.assertFalse(os.path.exists(audio_info.cache_path()))

    def test_the_second_rebuild_reads_nothing(self):
        self.rows(LIST_SHOW_AUDIO_INFO=True)
        rows, said = self.rows(LIST_SHOW_AUDIO_INFO=True)
        self.assertRegex(said, r"\[LIST-GEN\] Audio info: 0 file\(s\) read, [1-9]\d* unchanged")
        self.assertNotIn("files a second", said, "no rate when nothing was read")
        self.assertRegex(rows["Example Artist - 01 - Opening.mp3"], r" 0m26s 128/44\.1/JS$")

    def test_a_published_rebuild_forgets_a_removed_file(self):
        import sqlite3
        self.rows(LIST_SHOW_AUDIO_INFO=True)
        os.remove(os.path.join(self.tree.music, "Album", "Example Artist - 02 - Closing.flac"))
        self.rows(LIST_SHOW_AUDIO_INFO=True)
        conn = sqlite3.connect(audio_info.cache_path())
        self.addCleanup(conn.close)
        keys = [row[0] for row in conn.execute("SELECT key FROM audio")]
        self.assertTrue(any(key.endswith("Opening.mp3") for key in keys), keys)
        self.assertFalse(any(key.endswith("Closing.flac") for key in keys), keys)

    def test_a_rebuild_out_of_time_publishes_and_the_next_one_finishes(self):
        rows, said = self.rows(LIST_SHOW_AUDIO_INFO=True, LIST_AUDIO_INFO_THREADS=1,
                               LIST_AUDIO_INFO_MINUTES=1, _clock=[0, 0] + [999] * 200)
        # One read started before the clock ran out; which file it was is the
        # walk's order, and make_tree() has audio files of its own.
        self.assertIn("not read within LIST_AUDIO_INFO_MINUTES = 1", said)
        self.assertRegex(said, r"Audio info: 1 file\(s\) read, 0 unchanged since the last "
                               r"rebuild, [1-9]\d* left for the next one\.")
        self.assertLessEqual(len([tail for tail in rows.values() if " " in tail]), 1)
        rows, said = self.rows(LIST_SHOW_AUDIO_INFO=True, LIST_AUDIO_INFO_MINUTES=0)
        self.assertRegex(rows["Example Artist - 01 - Opening.mp3"], r" 0m26s 128/44\.1/JS$")
        self.assertRegex(rows["Example Artist - 02 - Closing.flac"], r" 0m2s 1115/44\.1/S$")

    def test_our_own_parser_reads_the_row_back(self):
        self.rows(LIST_SHOW_AUDIO_INFO=True)
        entries, _total = list_mod.find_matching_entries(["opening"])
        self.assertEqual([e["filename"] for e in entries], ["Example Artist - 01 - Opening.mp3"])


class Search(DCCoreTestCase):
    """The name comes before the audio tail when a result must be cut."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.long_name = "Example Artist - " + "Very Long Title " * 19 + ".mp3"  # fits only without its tail
        write_master_list(self.tree.lists, "DCCoreTest", [(None, [
            ("Short Song.mp3", "4.1MB 4m31s 320/44.1/JS"),
            (self.long_name, "9.9MB 7m2s ~245/44.1/JS"),
        ])])
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest", CHANNEL="#chan",
                        search_inprogress=False, update_inprogress=False)

    def find(self, term):
        self.oserve.queued.clear()
        list_mod.execute_search(RecordingSocket(), "dave", term, "#chan")
        return [m for _u, m, *_ in self.oserve.queued if "::INFO::" in m]

    def test_a_row_that_fits_keeps_its_tail(self):
        rows = self.find("short song")
        self.assertEqual(len(rows), 1)
        self.assertIn("Short Song.mp3  ::INFO:: 4.1MB 4m31s 320/44.1/JS", rows[0])

    def test_a_row_that_does_not_loses_the_tail_before_the_name(self):
        rows = self.find("very long title")
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0].encode("utf-8")), announce.IRC_LINE_BUDGET)
        self.assertIn(self.long_name + "  ::INFO:: 9.9MB", rows[0])
        self.assertNotIn("7m2s", rows[0])

    def test_the_tail_pattern_touches_only_a_tail(self):
        row = "!Bot 4m31s 320/44.1/JS.mp3  ::INFO:: 4.1MB 4m31s 320/44.1/JS"
        self.assertEqual(list_mod.without_audio_info(row), "!Bot 4m31s 320/44.1/JS.mp3  ::INFO:: 4.1MB")
        self.assertEqual(list_mod.without_audio_info("!Bot a.mp3  ::INFO:: 4.1MB"), "!Bot a.mp3  ::INFO:: 4.1MB")


if __name__ == "__main__":
    unittest.main()
