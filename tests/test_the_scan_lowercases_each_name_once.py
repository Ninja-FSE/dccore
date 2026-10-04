"""#1138: the scan lower-cases each name once and slices each folder's path.

Three per-file and per-folder costs in the list rebuild's scan loop:

- is_listed_file(), is_packable_file(), belongs_in_video_list() and
  audio_info.is_audio() each lower-cased the same name again, up to five
  times a file, and rebuilt a tuple of the extensions each time. The loop now
  lowers each name once and checks it against the tuples resolved once per
  scan, and marks a folder packable once instead of once per packable file.
- os.path.relpath() made every directory absolute and normalised it, about
  10-20 us a directory on Windows, for a path the walk had just built by
  appending names to the scan root. relative_folder() slices it off instead,
  wherever relpath() could not answer differently.
- The full path of every audio file was joined before the cache even looked
  at it, and on an unchanged library almost every one is a cache hit. It now
  goes as (folder, name) and is joined only for a file that is read.

None of it may change what the scan decides. The tests compare the rebuild
with the old loop, kept here verbatim and built on the public helpers it
used, on names in every case and extension shape, under each extension
setting left empty, and with the video split on and off; and they compare
relative_folder() with relpath() under both Windows and POSIX path rules.

The global sort (SORT-1 in the issue) is deliberately left alone: the skeptic
re-measured it as small, and grouping the rows changes the data the writer
reads.
"""

import contextlib
import io
import ntpath
import os
import posixpath
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import audio_info  # noqa: E402
import library  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BACKSLASH = chr(92)


def counting(paths):
    """`paths` (ntpath or posixpath) with relpath() counted."""
    calls = []

    def relpath(path, start):
        calls.append(path)
        return paths.relpath(path, start)

    return types.SimpleNamespace(sep=paths.sep, altsep=paths.altsep, relpath=relpath), calls


def as_the_walk_builds_it(paths, scan_root, rest):
    """scandir's entry.path: no separator added after one already there, or
    after a bare drive on Windows."""
    if not rest:
        return scan_root
    ends = (paths.sep, paths.altsep) if paths.altsep else (paths.sep,)
    if paths.altsep and scan_root.endswith(":"):
        ends += (":",)
    glue = "" if scan_root.endswith(ends) else paths.sep
    return scan_root + glue + rest.replace("/", paths.sep)


WINDOWS_ROOTS = [
    BACKSLASH * 2 + "?" + BACKSLASH + "C:" + BACKSLASH + "Lib" + BACKSLASH + "music",
    BACKSLASH * 2 + "?" + BACKSLASH + "D:" + BACKSLASH,
    BACKSLASH * 2 + "?" + BACKSLASH + "UNC" + BACKSLASH + "server" + BACKSLASH + "share",
    "C:" + BACKSLASH + "music", "C:/music", "C:/music/", "C:", "music", ".",
]
POSIX_ROOTS = ["/srv/music", "/srv/music/", "/", "//", "/srv//music", "/a/../music", "music", "."]
PLAIN = ["Artist", "Artist/Album (1997)", "Jóga/Ünïcödé Séries", "🎵/東京", " lead", ".hidden",
         "x..y", "Artist/Album (1997)/CD1"]
ODD = ["Album.", "Album ", "Artist/Album./CD1", "b /c", "x..", "...", "a:b"]


def outcome(call, *args):
    """What `call` answers, or the kind of error it raises: relpath() raises
    for some shapes, and relative_folder() must then raise the same."""
    try:
        return call(*args)
    except ValueError as err:
        return type(err)


class RelativeFolderIsRelpath(unittest.TestCase):
    def check(self, paths, roots, rests):
        for scan_root in roots:
            for rest in rests:
                root = as_the_walk_builds_it(paths, scan_root, rest)
                with self.subTest(scan_root=scan_root, rest=rest):
                    self.assertEqual(outcome(update_list.relative_folder, root, scan_root, paths),
                                     outcome(paths.relpath, root, scan_root))

    def test_windows_rules(self):
        self.check(ntpath, WINDOWS_ROOTS, [""] + PLAIN + ODD)

    def test_posix_rules(self):
        self.check(posixpath, POSIX_ROOTS,
                   [""] + PLAIN + ODD + ["Rock" + BACKSLASH + "Metal", "evil\nname", "Bj\udcf6rk"])

    def test_a_root_from_somewhere_else_is_asked_of_relpath(self):
        for paths, scan_root, root in (
                (ntpath, "C:" + BACKSLASH + "music", "C:" + BACKSLASH + "musicals" + BACKSLASH + "x"),
                (ntpath, "C:" + BACKSLASH + "music", "c:" + BACKSLASH + "MUSIC" + BACKSLASH + "x"),
                (posixpath, "/srv/music", "/srv/musicals/x"),
                (posixpath, "/srv/music", "/srv/music//x")):
            with self.subTest(root=root):
                self.assertEqual(update_list.relative_folder(root, scan_root, paths),
                                 paths.relpath(root, scan_root))

    def test_plain_names_are_sliced_without_asking_relpath(self):
        """Guards the comparison above: a relative_folder() that always asked
        relpath() would pass it and save nothing."""
        for real, roots in ((ntpath, WINDOWS_ROOTS[:3] + ["C:" + BACKSLASH + "music"]),
                            (posixpath, ["/srv/music", "/srv/music/", "/"])):
            paths, calls = counting(real)
            for scan_root in roots:
                for rest in PLAIN:
                    root = as_the_walk_builds_it(real, scan_root, rest)
                    with self.subTest(scan_root=scan_root, rest=rest):
                        self.assertEqual(update_list.relative_folder(root, scan_root, paths),
                                         rest.replace("/", real.sep))
            self.assertEqual(calls, [])

    def test_odd_names_are_asked_of_relpath(self):
        for real, scan_root, rests in (
                (ntpath, WINDOWS_ROOTS[0], ODD + ["a/b"]),
                (posixpath, "/srv/music", ["a//b", "a/./b", "a/../b", "a/"])):
            for rest in rests:
                paths, calls = counting(real)
                root = scan_root + real.sep + rest
                with self.subTest(rest=rest):
                    self.assertEqual(update_list.relative_folder(root, scan_root, paths),
                                     real.relpath(root, scan_root))
                    self.assertEqual(len(calls), 1)


# (folder below the scan root, [(name, size)]) as the walk hands it over.
TREE = [
    ("", [("loose.FLAC", 10), ("desktop.ini", 1), ("Thumbs.db", 2)]),
    ("Artist/Album (1997)", [("01 - One.flac", 100), ("02 - Two.MP3", 200), ("cover.jpg", 3),
                             ("album.NFO", 4), ("info.txt", 5), ("broken.flac", None)]),
    ("Artist/Album (1997)/Scans", [("front.jpg", 6), ("back.PNG", 7)]),
    ("Films/Feature (2020)", [("Feature.MKV", 5000), ("Feature.srt", 8), ("feature.NFO", 9),
                              ("sample.mp3", 11), ("Feature.SFV", 12)]),
    ("Films/Loose Subs", [("only.srt", 13), ("only.nfo", 14)]),
    ("Mixed", [("clip.mp4", 700), ("song.flac", 15), ("partial.part", 16), ("ignored.DB", 17)]),
    ("Odd/Record.", [("x.flac", 18)]),
    ("Case/MiXeD.Ext", [("track.FlAc", 19), ("noext", 20), (".hidden", 21), ("TRAILER.Mp4", 22)]),
    ("Ünïcödé/Jóga", [("Jóga.flac", 23), ("ÆØÅ.OGG", 24), ("Ŝtrange.MKV", 25)]),
]

SETTINGS = [
    {},
    {"SEPARATE_VIDEO_LIST": False},
    {"LIST_IGNORED_EXTENSIONS": []},
    {"RAR_EXTENSIONS": []},
    {"LIST_VIDEO_EXTENSIONS": []},
    {"LIST_VIDEO_COMPANION_EXTENSIONS": []},
    {"LIST_IGNORED_EXTENSIONS": "INI, txt", "RAR_EXTENSIONS": "FLAC", "LIST_VIDEO_EXTENSIONS": "mkv"},
]


def old_scan(walked, scan_root, label):
    """The scan loop before #1138, verbatim apart from its outputs: (music
    rows, video rows, packable folders, total bytes, audio paths noted)."""
    ignored = update_list.ignored_extensions()
    video_exts = update_list.video_extensions()
    companion_exts = update_list.video_companion_extensions()
    packable_exts = update_list.rar_extensions()
    split_video = bool(getattr(update_list.config, "SEPARATE_VIDEO_LIST", True))
    all_files_data, video_files_data, packable_folders, noted = [], [], set(), []
    total_bytes = 0
    for root, files in walked:
        rel_dir = os.path.relpath(root, scan_root)
        if rel_dir == ".":
            rel_dir = ""
        rel_dir = os.path.join(label, rel_dir) if rel_dir else label
        folder_has_video = split_video and any(
            update_list.is_video_file(name, video_exts) for name, _bytes in files)
        for file, file_bytes in files:
            if update_list.is_listed_file(file, ignored):
                if file_bytes is None:
                    continue
                total_bytes += file_bytes
                if update_list.is_packable_file(file, packable_exts):
                    packable_folders.add(rel_dir)
                if split_video and update_list.belongs_in_video_list(
                        file, folder_has_video, video_exts, companion_exts):
                    video_files_data.append((rel_dir, file, file_bytes))
                else:
                    all_files_data.append((rel_dir, file, file_bytes))
                    if audio_info.is_audio(file):
                        noted.append(os.path.join(root, file))
    key = lambda x: (str(x[0]).lower(), str(x[1]).lower())  # noqa: E731
    return sorted(all_files_data, key=key), sorted(video_files_data, key=key), \
        packable_folders, total_bytes, sorted(noted)


class TheScanDecidesAsBefore(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="SomeBot", NICKNAME="SomeBot", ORIGINAL_NICK="SomeBot",
                        RAR_ENABLED=True, LIST_SHOW_AUDIO_INFO=True)
        self.label = library.folders()[0].name
        self.scan_root = platform_compat.long_path(library.folders()[0].path)

        def fake_walk(top, onerror=None, workers=None):
            for folder, files in TREE:
                yield (os.path.join(top, *folder.split("/")) if folder else top), list(files)

        self.walk = fake_walk
        patcher = mock.patch.object(update_list, "walk_with_sizes", fake_walk)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, name):
        """One rebuild; (music rows, video rows, packable folders, total
        bytes, audio paths read), caught where the rebuild hands them on."""
        totals_of = []
        real_totals = update_list.folder_totals

        def folder_totals(rows):
            totals_of.append(list(rows))
            return real_totals(rows)

        reads = []

        def reader(path, size=None):
            reads.append(path)
            return None

        lists = os.path.join(self.tree.root, name)
        os.makedirs(lists)
        # A cache of its own, so every audio file is read and seen here.
        self.set_config(LOCAL_LIST_DIR=lists, LIST_AUDIO_INFO_CACHE=os.path.join(lists, "audio.db"))
        out = io.StringIO()
        # The audio files are read after the list is published (#1182): the
        # reading the rebuild hands on is run here, so `reads` is still every
        # audio file the scan noted.
        jobs = []
        with mock.patch.object(update_list, "folder_totals", folder_totals), \
                mock.patch.object(audio_info, "read", reader), contextlib.redirect_stdout(out):
            self.assertTrue(update_list.generate_master_list(reading_jobs=jobs), out.getvalue())
            update_list.read_audio_info(jobs)
        rar = [entry for entry in os.listdir(lists) if "-RAR-" in entry]
        packable = set()
        if rar:
            prefix = "!SomeBot !rar "
            with open(os.path.join(lists, rar[0]), encoding="utf-8") as handle:
                packable = {line.rstrip("\n") for line in handle
                            if line.startswith(prefix) and not line.rstrip("\n").endswith("Album" + BACKSLASH)}
        with open(os.path.join(lists, update_list.config.LIST_RAWBYTES_FILE), encoding="utf-8") as handle:
            total = int(handle.read().strip())
        music = totals_of[0]
        video = totals_of[1] if len(totals_of) > 1 else []
        return music, video, packable, total, sorted(reads)

    def rar_rows(self, folders):
        import list as list_mod
        return {"!SomeBot !rar " + (list_mod.LIST_FOLDER_PREFIX + folder + BACKSLASH).replace("/", BACKSLASH)
                for folder in folders}

    def test_every_setting_classifies_every_file_as_the_old_loop_did(self):
        for number, settings in enumerate(SETTINGS):
            with self.subTest(settings=settings):
                self.set_config(**settings)
                music, video, packable, total, reads = self.build(f"lists{number}")
                old_music, old_video, old_packable, old_total, old_noted = old_scan(
                    self.walk(self.scan_root), self.scan_root, self.label)
                self.assertEqual(music, old_music)
                self.assertEqual(video, old_video)
                self.assertEqual(packable, self.rar_rows(old_packable))
                self.assertEqual(total, old_total)
                self.assertEqual(reads, old_noted)

    def test_the_scenario_reaches_every_branch(self):
        """Guards the comparison above against a tree that exercised little."""
        music, video, packable, _total, reads = self.build("built")
        names = {name for _folder, name, _size in music}
        self.assertIn("album.NFO", names, "a companion without a video stays with the music")
        self.assertIn("sample.mp3", names, "audio beside a video is music")
        self.assertNotIn("desktop.ini", names)
        self.assertNotIn("broken.flac", names)
        self.assertEqual({name for _folder, name, _size in video},
                         {"Feature.MKV", "Feature.srt", "feature.NFO", "Feature.SFV", "clip.mp4",
                          "TRAILER.Mp4", "Ŝtrange.MKV"})
        self.assertNotIn(self.rar_rows([os.path.join(self.label, "Artist", "Album (1997)", "Scans")]).pop(),
                         packable)
        self.assertIn(self.rar_rows([os.path.join(self.label, "Artist", "Album (1997)")]).pop(), packable)
        self.assertEqual(len(reads), 8)
        self.assertTrue(all(isinstance(path, str) for path in reads))


if __name__ == "__main__":
    unittest.main()
