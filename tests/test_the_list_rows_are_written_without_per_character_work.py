"""#1125: the list rows are written without a per-character pass over each one.

Writing the list cost four times what it needed. _one_line() ran a Python
generator step over every character of every file name and folder heading -
about 15 us for a 60-character track name, the largest single cost of writing
a million-row list - although almost no name holds anything it changes.
list_nick() was asked again for every row, and every row was its own write().

Now one regex search decides whether a name needs flattening at all, the nick
is resolved once per list, and each folder's heading and rows go out in one
write. None of it may change a byte: the list is what every user downloads and
what other bots and AutoQ parse. So these tests compare against the old
implementation, kept here verbatim, on names with accents, emoji, odd
punctuation, control characters, a newline and a lone surrogate - and against
a rendering of the rows built independently of the writer.
"""

import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import PurePosixPath
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BACKSLASH = chr(92)


def old_one_line(text):
    """_one_line() as it was before #1125, character for character."""
    text = str(text).encode("utf-8", "replace").decode("utf-8")
    return "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)


# Names the old and new _one_line() must agree on. Every control character,
# DEL, the C1 range that is NOT flattened, accents, emoji, a lone surrogate of
# each half, and names that mix several of them.
AWKWARD_NAMES = [
    "", " ", "Enter Sandman.flac", "Jóga.flac", "Sigur Rós - ( ).flac",
    "Ünïcödé Séries", "東京 - 青い歌.mp3", "🎵 Untitled #1.flac",
    "evil\nname.flac", "cr\rname.flac", "crlf\r\nname.flac", "tab\there.flac",
    "nul\x00name.flac", "del\x7fname.flac", "c1\x85\x9fname.flac",
    "Bj\udcf6rk.flac", "lone\ud800high.mp3", "\udfff", "\ud83c\udfb5 pair",
    "mixed\n\udcf6\tJóga\x7f🎵.flac", "[Weird] {Chars} & 100% ; ' \" !.flac",
    "trailing space .flac", "  leading.flac", "back" + BACKSLASH + "slash.flac",
]


class OneLineIsUnchanged(unittest.TestCase):
    def test_every_awkward_name_comes_out_as_it_did(self):
        for name in AWKWARD_NAMES:
            with self.subTest(name=repr(name)):
                self.assertEqual(update_list._one_line(name), old_one_line(name))

    def test_every_code_point_alone_and_inside_a_name(self):
        """Every code point up to U+3000, every surrogate, and a sample of
        the rest - alone and between ordinary characters."""
        points = list(range(0x3000)) + list(range(0xD800, 0xE000)) + list(range(0x3000, 0x110000, 97))
        for code in points:
            ch = chr(code)
            for name in (ch, f"Track {ch} name.flac"):
                new, old = update_list._one_line(name), old_one_line(name)
                if new != old:
                    self.fail(f"U+{code:04X} in {name!r}: {new!r} != {old!r}")

    def test_what_is_not_a_string_is_turned_into_one_as_before(self):
        for value in (12, 3.5, None, PurePosixPath("Artist/Album")):
            with self.subTest(value=value):
                self.assertEqual(update_list._one_line(value), old_one_line(value))

    def test_a_clean_name_is_handed_back_untouched(self):
        """The fast path: a name with nothing to flatten is the same object,
        not a copy rebuilt a character at a time."""
        for name in ("Enter Sandman.flac", "Jóga.flac", "🎵 Untitled #1.flac"):
            with self.subTest(name=name):
                self.assertIs(update_list._one_line(name), name)


# A library, as the walk would hand it over: (folder below the scan root,
# [(name, size)]). Faked rather than created, so a newline or a lone surrogate
# in a name works on every platform - neither can be created on Windows.
MUSIC = [
    ("", [("loose track.flac", 1000)]),
    ("Artist A/Album One", [(f"{n:02d} - Track {n}.flac", 1000 + n * 37) for n in range(1, 13)]),
    ("Björk/Homogenic (1997)", [("Jóga.flac", 31_000_000), ("Bachelorette.flac", 29_000_000),
                                ("Hunter.mp3", 9_000_000), ("notes.txt", 0)]),
    ("Sigur Rós/( )", [("🎵 Untitled #1.flac", 4096), ("東京 - 青い歌.mp3", 5_000_000_000)]),
    ("Odd/Line\nBreak", [("evil\nname.flac", 2048), ("tab\there.flac", 2048),
                         ("del\x7fname.mp3", 2048), ("nul\x00name.flac", 2048)]),
    ("Odd/Bj\udcf6rk", [("Bj\udcf6rk.flac", 2048), ("lone\ud800high.mp3", 2048)]),
    ("Misc/[Weird] {Chars} & 100% ; ' !", [("!bang.flac", 1), ("#hash.mp3", 10),
                                          ("  leading spaces.flac", 100),
                                          ("trailing space .flac", 1000)]),
    ("Only Text", [("readme.txt", 12), ("lyrics.txt", 34)]),
    ("Case/ABC", [("b.flac", 1), ("D.flac", 2)]),
    ("Case/abc", [("a.flac", 3), ("C.flac", 4)]),
]
FILMS = [
    ("Films/Some Film (2021)", [("Some.Film.2021.mkv", 7_000_000_000),
                                ("Some.Film.2021.srt", 70_000), ("Some.Film.2021.nfo", 700)]),
    ("Films/Ünïcödé Séries/Season 1", [("S01E01.mkv", 1_000_000_000),
                                       ("ep\nnewline.mkv", 900_000_000)]),
]


def human(b):
    """update_list's format_size_human(), which is local to the rebuild."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024.0:
            return f"{b:.2f}{unit}"
        b /= 1024.0
    return f"{b:.2f}PB"


def more_rows(tree, times):
    """The same folders, each with `times` times as many files."""
    grown = []
    for folder, files in tree:
        extra = [(f"copy {n} {name}", size) for n in range(1, times) for name, size in files]
        grown.append((folder, files + extra))
    return grown


def more_folders(tree, times):
    """The same files in `times` times as many folders."""
    return tree + [(f"{folder or 'Loose'} {n}", files) for n in range(2, times + 1) for folder, files in tree]


class CountingFile:
    """A list file being written, counting its write() calls."""

    def __init__(self, inner, counts, key):
        self._inner = inner
        self._counts = counts
        self._key = key

    def write(self, text):
        self._counts[self._key] = self._counts.get(self._key, 0) + 1
        return self._inner.write(text)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TheWrittenList(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="SomeBot", NICKNAME="SomeBot", ORIGINAL_NICK="SomeBot",
                        RAR_ENABLED=True, SEPARATE_VIDEO_LIST=True, LIST_SHOW_AUDIO_INFO=False)
        self.label = library.folders()[0].name
        self.library(MUSIC + FILMS)

    def library(self, tree):
        def fake_walk(top, onerror=None, workers=None):
            for folder, files in tree:
                yield (os.path.join(top, *folder.split("/")) if folder else top), list(files)

        patcher = mock.patch.object(update_list, "walk_with_sizes", fake_walk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def generate(self, name):
        """Build the lists into their own folder; (music, rar, film) bytes."""
        folder = os.path.join(self.tree.root, name)
        os.makedirs(folder)
        self.set_config(LOCAL_LIST_DIR=folder)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(), out.getvalue())
        found = {}
        for entry in os.listdir(folder):
            if not (entry.startswith("SomeBot-") and entry.endswith(".txt")):
                continue
            kind = ("rar" if "-RAR-" in entry
                    else "film" if f"-{list_mod.VIDEO_LIST_MARKER}-" in entry else "music")
            with open(os.path.join(folder, entry), "rb") as handle:
                found[kind] = handle.read()
        self.assertEqual(sorted(found), ["film", "music", "rar"])
        return found

    @staticmethod
    def without_timing(data):
        """The first line says how long the scan took, which differs between
        two runs; everything else must not."""
        return re.sub(rb" in \d\d:\d\d:\d\d \( [\d,]+ Files Per Second \)", b"", data, count=1)

    def rows(self, tree):
        """(folder, name, size) as the rebuild keys them, in list order."""
        rows = [(os.path.join(self.label, *folder.split("/")) if folder else self.label, name, size)
                for folder, files in tree for name, size in files]
        return sorted(rows, key=lambda r: (r[0].lower(), r[1].lower()))

    def rendered(self, rows):
        """Headings, summaries and rows, rendered with the OLD _one_line()."""
        totals = update_list.folder_totals(rows)
        out = []
        current = None
        for folder, name, size in rows:
            if folder != current:
                current = folder
                line = old_one_line(f"{list_mod.LIST_FOLDER_PREFIX}{folder}{BACKSLASH}".replace("/", BACKSLASH))
                rule = "=" * len(line)
                out.append(f"\n{rule}\n{line}\n{rule}\n")
                out.append(update_list.folder_summary_line(*totals[folder], human) + "\n")
            out.append(f"!SomeBot {old_one_line(name)}  ::INFO:: {human(size)}\n")
        return "".join(out)

    @staticmethod
    def body(data):
        """Everything from the first folder heading on."""
        text = data.decode("utf-8").replace("\r\n", "\n")
        return text[text.index("\n\n=") + 1:]

    def test_the_lists_are_byte_for_byte_what_the_old_flattening_wrote(self):
        new = self.generate("new")
        with mock.patch.object(update_list, "_one_line", old_one_line):
            old = self.generate("old")
        for kind in ("music", "rar", "film"):
            with self.subTest(kind=kind):
                self.assertEqual(self.without_timing(new[kind]), self.without_timing(old[kind]))

    def test_every_folder_and_row_is_written_once_in_order(self):
        written = self.generate("built")
        self.assertEqual(self.body(written["music"]), self.rendered(self.rows(MUSIC)))
        self.assertEqual(self.body(written["film"]), self.rendered(self.rows(FILMS)))

    def test_the_album_list_names_every_packable_folder_once(self):
        written = self.generate("built")
        packable = tuple(config.RAR_EXTENSIONS)
        folders = []
        for folder, name, _size in self.rows(MUSIC):
            if name.lower().endswith(packable) and folder not in folders:
                folders.append(folder)
        expected = [f"!SomeBot !rar "
                    + old_one_line(f"{list_mod.LIST_FOLDER_PREFIX}{folder}{BACKSLASH}".replace("/", BACKSLASH))
                    for folder in folders]
        text = written["rar"].decode("utf-8").replace("\r\n", "\n")
        self.assertEqual([line for line in text.split("\n") if line.startswith("!SomeBot !rar ")
                          and not line.endswith("Album" + BACKSLASH)], expected)

    def counted(self, tree, name):
        """(list_nick() calls, write() calls per list) for one build of `tree`."""
        self.library(tree)
        calls = []
        real_nick = update_list.list_nick

        def counting_nick():
            calls.append(1)
            return real_nick()

        writes = {}
        real_open = open

        def counting_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            entry = os.path.basename(os.fspath(path))
            if "w" not in mode or not entry.startswith("SomeBot-"):
                return handle
            kind = ("rar" if "-RAR-" in entry
                    else "film" if f"-{list_mod.VIDEO_LIST_MARKER}-" in entry else "music")
            return CountingFile(handle, writes, kind)

        with mock.patch.object(update_list, "list_nick", counting_nick), \
                mock.patch.object(update_list, "open", counting_open, create=True):
            self.generate(name)
        return len(calls), writes

    def test_the_nick_is_asked_per_list_not_per_row_or_folder(self):
        nick_small, _writes = self.counted(MUSIC + FILMS, "small")
        nick_rows, _writes = self.counted(more_rows(MUSIC + FILMS, 4), "rows")
        nick_folders, _writes = self.counted(more_folders(MUSIC + FILMS, 3), "folders")
        self.assertEqual(nick_rows, nick_small)
        self.assertEqual(nick_folders, nick_small)

    def test_each_folder_is_one_write_however_many_rows_it_holds(self):
        """Without the case twins: two folders differing only in case share
        one place in the sort, so their rows interleave by name and each
        change of folder repeats a heading - as it always has. That makes
        their heading count grow with their rows."""
        tree = [entry for entry in MUSIC + FILMS if not entry[0].startswith("Case/")]
        _nick, small = self.counted(tree, "small")
        _nick, large = self.counted(more_rows(tree, 4), "large")
        self.assertEqual(small["music"], large["music"])
        self.assertEqual(small["film"], large["film"])
        # A few header writes, then one per folder.
        self.assertLessEqual(small["music"], 8 + len([e for e in tree if e in MUSIC]))
        self.assertLessEqual(small["film"], 8 + len(FILMS))

    def test_the_list_is_not_held_in_memory_whole(self):
        """One folder at a time: more folders, more writes."""
        tree = [entry for entry in MUSIC + FILMS if not entry[0].startswith("Case/")]
        _nick, small = self.counted(tree, "small")
        _nick, large = self.counted(more_folders(tree, 3), "large")
        self.assertGreaterEqual(large["music"] - small["music"], 2 * len([e for e in tree if e in MUSIC]))
        self.assertGreaterEqual(large["film"] - small["film"], 2 * len(FILMS))

if __name__ == "__main__":
    unittest.main()
