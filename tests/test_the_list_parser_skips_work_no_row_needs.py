"""Two per-row costs in the shared list parser are gone, and the rows are the same.

WHAT WAS SLOW (#1136)

Every list the bot reads goes through find_matching_entries(),
strip_info_suffix() and entries_to_filelist_rows(): @find over our own list,
every List Browser page, every fetch install and every backfill. Two of their
per-row steps cost more than they had to:

- strip_info_suffix() split on r'\\s*::INFO::\\s*'. The leading \\s* is tried
  at every whitespace position of a row before the regex gives up on it, and
  both halves were stripped afterwards anyway. It now searches for the bare
  marker and slices around it.
- entries_to_filelist_rows() ran rar_folder_of()'s anchored "^!rar" regex on
  every title, although only a title starting with "!" can match it.

The third cost the audit named, the set() per line for the rule check, is the
same line #1126 changed, so it landed there.

WHY THESE TESTS

The rewrite is exact only because str.strip() removes precisely the
characters re's \\s matches, so that premise is checked over every code point
rather than assumed. Then the old functions, copied below as they were, are
compared with the new ones: on marker rows with every kind of whitespace
around the marker, mixed case, a double marker, a bare marker and the
dash-size suffix; on "!rar" titles with leading whitespace of every kind; and
on a whole list built to break the parser, row for row.
"""

import io
import os
import random
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


_OLD_INFO_MARKER_RE = re.compile(r'\s*::INFO::\s*', re.IGNORECASE)
_OLD_DASH_SIZE_SUFFIX_RE = re.compile(
    r'\s+-{2,}\s+([\d,]*\.?[\d,]+\s*[KMGTkmgt]?[Bb])\s*$')


def old_strip_info_suffix(rest):
    """strip_info_suffix() as it was before #1136."""
    parts = _OLD_INFO_MARKER_RE.split(rest, maxsplit=1)
    if len(parts) == 2:
        filename, size = parts
    else:
        dash_match = _OLD_DASH_SIZE_SUFFIX_RE.search(rest)
        if dash_match:
            filename, size = rest[:dash_match.start()], dash_match.group(1)
        else:
            filename, size = rest, ""
    return filename.strip(), size.strip()


def old_entries_to_filelist_rows(entries, source):
    """entries_to_filelist_rows() as it was before #1136."""
    seen = set()
    rows = []
    for entry in entries:
        filename = entry.get("filename", "?")
        size = entry.get("size", "")
        folder = entry.get("folder") or ""
        key = (folder.lower(), filename.lower(), size)
        if key in seen:
            continue
        seen.add(key)
        ext = os.path.splitext(filename)[1].lstrip(".").upper()
        rows.append({
            "title": filename,
            "size": size,
            "format": ext,
            "source": source,
            "folder": folder,
            "rar_folder": list_mod.rar_folder_of(filename),
            "mark": "",
        })
    return rows


def old_parse(path):
    """The whole old pipeline for one list: rows split the old way, shaped
    the old way. The scan itself is the real one; #1126 has its own test."""
    entries, total = list_mod.find_matching_entries([], limit=None, list_path=path)
    for entry in entries:
        _, _, rest = entry["line"].partition(" ")
        entry["filename"], entry["size"] = old_strip_info_suffix(rest)
    return old_entries_to_filelist_rows(entries, "SomeBot"), total


# Every whitespace character there is, found rather than listed.
WHITESPACE = [chr(c) for c in range(0x110000) if chr(c).isspace()]

MARKERS = ["::INFO::", "::info::", "::Info::", "::\u0130NFO::", "::\u0131NFO::",
           "::\u0131nfo::", "::INF0::", ":: INFO::", "::INFO:", "::INFO::::INFO::"]


def marker_rows():
    rows = []
    for ws in WHITESPACE + ["", "  ", " \u3000 ", "\t\u00a0"]:
        for marker in MARKERS:
            rows.append(f"Song.flac{ws}{marker}{ws}10MB")
            rows.append(f"{ws}Song.flac{ws}{marker}{ws}")
            rows.append(f"{marker}{ws}10MB{ws}")
    rows += [
        "Song.flac  ::INFO:: 1MB ::INFO:: 2MB",
        "Song.flac ::INFO::",
        "::INFO::",
        "   ::INFO::   ",
        "Track.mp3 ---- 18.8Mb",
        "Track.mp3 ---- 18.8Mb  ::INFO:: 3MB",
        "Track ::info:: 6.32Mb 4m30s 192/44.10/JS  SomeServe v2.60",
        "a\u2028::INFO::\u2028b",
        "\u0130stanbul.flac ::\u0130NFO:: 1MB",
        "\u212aelvin.flac ::INFO:: 1MB",
        "",
        "no marker at all",
    ]
    return rows


class TheMarkerSplitIsUnchanged(DCCoreTestCase):

    def test_strip_removes_exactly_what_backslash_s_matches(self):
        """The premise of the rewrite, over every code point."""
        space = re.compile(r"\s")
        for code in range(0x110000):
            ch = chr(code)
            if bool(space.fullmatch(ch)) != (ch.strip() == ""):
                self.fail(f"U+{code:04X}: \\s and str.strip() disagree")

    def test_every_marker_row_splits_as_before(self):
        rows = marker_rows()
        self.assertGreater(len(rows), 300)
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(list_mod.strip_info_suffix(row),
                                 old_strip_info_suffix(row))

    def test_the_marker_pattern_carries_no_whitespace_run(self):
        """The cost itself: the pattern is the marker, so nothing is retried
        at every space of a row before it is found or ruled out."""
        self.assertEqual(list_mod._INFO_MARKER_RE.pattern, "::INFO::")
        self.assertTrue(list_mod._INFO_MARKER_RE.flags & re.IGNORECASE)


class TheRarFieldIsUnchanged(DCCoreTestCase):

    TITLES = (["!rar Some Folder", "!RAR Some Folder", "!Rar  x ", "!rar",
               "!rar   ", "!rarely used.flac", "!rarities.zip", "the !rar song.flac",
               "Track01.flac", "", "!", "!!rar x", "rar x", "!rar\tTabbed Folder",
               "!rar\u00a0No Break"]
              + [ws + "!rar Padded Folder" for ws in WHITESPACE]
              + [ws + "Track.flac" for ws in WHITESPACE[:5]])

    def entries(self):
        return [{"filename": title, "size": "1MB", "folder": "F%d" % n}
                for n, title in enumerate(self.TITLES)]

    def test_every_title_gets_the_same_row(self):
        self.assertEqual(list_mod.entries_to_filelist_rows(self.entries(), "SomeBot"),
                         old_entries_to_filelist_rows(self.entries(), "SomeBot"))

    def test_the_rar_folder_is_still_read_where_there_is_one(self):
        """Control: the comparison above would pass if both sides said ""."""
        rows = list_mod.entries_to_filelist_rows(self.entries(), "SomeBot")
        folders = {row["rar_folder"] for row in rows}
        self.assertIn("Some Folder", folders)
        self.assertIn("Padded Folder", folders)
        self.assertIn("No Break", folders)

    def test_a_title_without_a_bang_is_never_asked(self):
        calls = []
        real = list_mod.rar_folder_of

        def counting(title):
            calls.append(title)
            return real(title)

        list_mod.rar_folder_of = counting
        try:
            list_mod.entries_to_filelist_rows(
                [{"filename": "Track %d.flac" % n, "size": "1MB", "folder": "F"}
                 for n in range(50)]
                + [{"filename": " !rar F", "size": "", "folder": "F"}],
                "SomeBot")
        finally:
            list_mod.rar_folder_of = real
        self.assertEqual(calls, [" !rar F"])


_EDGE_LINES = [
    b"=====", b"= = =", b"=\x00=", b"\x00\x00", b"=\xc2\xa0=", b"====\r", b"====",
    b"!", b"D:\\MEDIA\\Some Folder\\", b"Some Folder",
    b"!SomeBot Love Song.flac  ::INFO:: 10MB",
    b"!SomeBot Love Song.flac\xc2\xa0::info::\xe2\x80\xa810MB 4m31s 320/44.1/JS",
    b"!SomeBot \xce\xa3\xce\xbf\xcf\x82.mp3 ::INFO::::INFO:: 3MB",
    b"!SomeBot \xe2\x84\xaaelvin.flac\t::Info::\t1MB",
    b"!SomeBot \xc4\xb0stanbul.flac ::\xc4\xb0NFO:: 2MB",
    b"!SomeBot Bad \xff\xfe Bytes.mp3  ::INFO:: 4MB",
    b"!SomeBot vivaldi - winter.flac ---- 18.8Mb",
    b"!SomeBot !rar Some Folder",
    b"!SomeBot  !RAR Other Folder",
    b"!SomeBot !rarely.flac",
    b"!SomeBot ::INFO::",
    b"!SomeBot",
    b"!SomeBot \xe3\x80\x80::INFO::\xe3\x80\x80",
]


class TheWholeListParsesAsBefore(DCCoreTestCase):

    def test_an_adversarial_list_gives_the_same_rows(self):
        root = self.make_tree().root
        path = os.path.join(root, "adversarial.txt")
        rng = random.Random(1136)
        data = bytearray()
        for n in range(3000):
            line = rng.choice(_EDGE_LINES)
            if rng.random() < 0.2:
                line = b"!SomeBot Track %d.flac  ::INFO:: %dMB" % (n, n)
            data += line + rng.choice([b"\n", b"\r\n", b"\r"])
        with io.open(path, "wb") as handle:
            handle.write(bytes(data))

        new_entries, new_total = list_mod.find_matching_entries([], limit=None, list_path=path)
        new_rows = list_mod.entries_to_filelist_rows(new_entries, "SomeBot")
        old_rows, old_total = old_parse(path)

        self.assertEqual(new_total, old_total)
        self.assertGreater(len(new_rows), 500)
        self.assertEqual(new_rows, old_rows)
        self.assertTrue(any(row["rar_folder"] for row in new_rows))
        self.assertTrue(any(row["size"] for row in new_rows))


if __name__ == "__main__":
    unittest.main()
