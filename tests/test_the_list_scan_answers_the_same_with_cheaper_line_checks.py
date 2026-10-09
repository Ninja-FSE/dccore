"""The list scan asks two per-line questions more cheaply, and answers the same.

WHAT WAS SLOW (#1126)

find_matching_entries() is the scan behind @find over our own lists, the
dashboard's searches, fetched-list pages and installs. For every line it did:

    is_rule = set(line_strip) == {"="}

building a set of the line's characters only to ask whether they were all
"=", and for every file line it tested the search words with
all(<generator>), a fresh generator object per row. The performance audit
measured the scan 2.4-3.4x faster with the rule check rewritten as
`not line_strip.strip("=")` and the generators replaced by plain loops, at two
million rows. The NUL guard the audit also proposed measured as noise, so it
is not part of the change.

WHY THESE TESTS

A rewrite like this is only worth having if the answers are exactly the same,
so the first test compares the new scan with the old one, copied below as it
was, on a list built to break it: NULs inside rules and rows, rules with
spaces or no-break spaces between the "=", U+2028 and ideographic whitespace,
lone CRs, folders named "====", Greek final sigma, the Kelvin sign, U+0130
(whose lower case is two characters), invalid UTF-8, and every limit from 0 to
unlimited. Same entries, same order, same total_matches.

The second test pins the cost itself without a clock: a scan of many lines
calls neither set() nor all(), which is what the fix removed from the loop.
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


_OLD_PHRASE_GAP = r"[ _.*\-]+"


def old_find_matching_entries(search_words, limit, list_path):
    """find_matching_entries() for one list_path, as it was before #1126.

    Copied, not imported, so the comparison keeps meaning something after the
    real function changes. It splits rows with the module's own
    _split_entry_line(), which #1126 did not touch.
    """
    entries = []
    total_matches = 0
    plain_words = [item for item in search_words if not isinstance(item, tuple)]
    phrase_patterns = [re.compile(_OLD_PHRASE_GAP.join(re.escape(word) for word in item))
                       for item in search_words if isinstance(item, tuple)]
    if not list_path or not os.path.exists(list_path):
        return entries, total_matches
    current_folder = None
    state = "none"
    with open(list_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_strip = line.replace('\x00', '').strip()
            if not line_strip:
                continue
            is_rule = set(line_strip) == {"="}
            if state == "none":
                if is_rule:
                    state = "open"
                    continue
            elif state == "open":
                if is_rule:
                    continue
                if line_strip.startswith("!"):
                    state = "none"
                else:
                    current_folder = line_strip
                    state = "folder_seen"
                    continue
            elif state == "folder_seen":
                state = "none"
                if is_rule:
                    continue
            if not line_strip.startswith("!"):
                continue
            # The words are matched against the filename alone (#1199), cut
            # out the plain way: the spec the fast cut is held to.
            line_lower = list_mod.strip_info_suffix(line_strip.partition(" ")[2])[0].lower()
            if plain_words and not all(word in line_lower for word in plain_words):
                continue
            if phrase_patterns and not all(pattern.search(line_lower) for pattern in phrase_patterns):
                continue
            total_matches += 1
            if limit is None or len(entries) < limit:
                filename, size = list_mod._split_entry_line(line_strip)
                entries.append({
                    "line": line_strip,
                    "folder": current_folder,
                    "filename": filename,
                    "size": size,
                })
    return entries, total_matches


# Lines that each probe one edge of the two rewritten checks. Bytes, so the
# list can carry what a text writer would refuse: invalid UTF-8.
_EDGE_LINES = [
    b"=====",
    b"=",
    b"= = =",
    b"=\x00=\x00=",
    b"\x00\x00\x00",
    b"=\xc2\xa0=",               # a no-break space between rules
    b"\xe2\x80\xa8=====\xe2\x80\xa8",  # U+2028 around a rule
    b"=\xe3\x80\x80=",           # an ideographic space between rules
    b"\xc2\xa0====\xc2\xa0",     # a rule padded with no-break spaces
    b"====\r",
    b"\r",
    b"==x==",
    b"x=====",
    b"=====x",
    b"====",
    b"!",
    b"!=====",
    b"D:\\MEDIA\\Some Folder\\",
    b"Some Folder",
    b"!SomeBot Love Song.flac  ::INFO:: 10MB",
    b"!SomeBot LOVE \xce\xa3\xce\xbf\xcf\x82.mp3  ::INFO:: 3MB",   # Greek, final sigma
    b"!SomeBot \xe2\x84\xaaelvin Track.flac  ::INFO:: 1MB",       # the Kelvin sign
    b"!SomeBot \xc4\xb0stanbul Live.flac  ::INFO:: 2MB",          # U+0130
    b"!SomeBot Bad \xff\xfe Bytes.mp3  ::INFO:: 4MB",             # invalid UTF-8
    b"!SomeBot Metal Church - Dark.flac ::info:: 9MB",
    b"!SomeBot metal_church_dark.flac",
    b"!SomeBot Metal\x00Church.flac  ::INFO:: 5MB",
    b"  !SomeBot Padded Love.flac  ::INFO:: 6MB  ",
    b"!SomeBot !rar Some Folder",
    b"!SomeBot vivaldi - winter.flac ---- 18.8Mb",
    b"",
    b"   ",
    b"\t\x0b\x0c",
]

_WORD_POOL = ["love", "metal", "church", "dark", "vivaldi", "winter", "Song",
              "Σ", "ς", "K", "İ", "x"]


def build_adversarial_list(path, seed=1126, lines=3000):
    """The edge lines above in a well-formed frame, then shuffled at random."""
    rng = random.Random(seed)
    out = []
    out += [b"=" * 20, b"D:\\MEDIA\\First\\", b"=" * 20]
    out += _EDGE_LINES
    for _ in range(lines):
        roll = rng.random()
        if roll < 0.55:
            out.append(rng.choice(_EDGE_LINES))
        elif roll < 0.7:
            out.append(b"=" * rng.randint(1, 30))
        elif roll < 0.8:
            out.append(("Folder %d" % rng.randint(0, 50)).encode())
        else:
            words = " ".join(rng.choice(_WORD_POOL) for _ in range(rng.randint(1, 4)))
            out.append(b"!SomeBot " + words.encode("utf-8") + b".flac  ::INFO:: 1MB")
    # Mixed line endings, a lone CR among them.
    data = bytearray()
    for line in out:
        data += line + rng.choice([b"\n", b"\r\n", b"\r"])
    with io.open(path, "wb") as handle:
        handle.write(bytes(data))
    return path


SEARCHES = [
    [],
    ["love"],
    ["metal", "church"],
    [("metal", "church")],
    ["dark", ("metal", "church")],
    ["\u03c3"],          # small sigma
    ["\u03c2"],          # final sigma
    ["\u03c3\u03bf\u03c2"],
    ["k"],               # the Kelvin sign lower-cases to it
    ["kelvin"],
    ["i\u0307stanbul"],  # what U+0130 lower-cases to
    ["istanbul"],
    ["\ufffd"],          # what invalid UTF-8 decodes to
    ["="],
    ["!"],
    ["\x00"],
    ["nothing-like-this"],
    ["love", "nothing-like-this"],
    [("vivaldi", "winter")],
    [("nothing", "here")],
]

LIMITS = [None, 0, 1, 2, 3, 5, 50, 10 ** 6]


class TheScanAnswersExactlyAsBefore(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root
        self.path = build_adversarial_list(os.path.join(self.root, "adversarial.txt"))

    def test_every_search_and_limit_is_unchanged(self):
        compared = 0
        for words in SEARCHES:
            for limit in LIMITS:
                with self.subTest(words=words, limit=limit):
                    self.assertEqual(
                        list_mod.find_matching_entries(words, limit=limit, list_path=self.path),
                        old_find_matching_entries(words, limit, self.path))
                    compared += 1
        self.assertEqual(compared, len(SEARCHES) * len(LIMITS))

    def test_the_list_really_has_rules_folders_and_matches(self):
        """Control: an equality over a list the parser reads as nothing would
        prove nothing. This one has folders, unfoldered rows and hits."""
        entries, total = old_find_matching_entries([], None, self.path)
        folders = {e["folder"] for e in entries}
        self.assertGreater(total, 500)
        self.assertGreater(len(folders), 10)
        for words in (["love"], ["\u03c3"], ["k"], ["\ufffd"], [("metal", "church")]):
            with self.subTest(words=words):
                self.assertGreater(old_find_matching_entries(words, None, self.path)[1], 0)

    def test_a_rule_is_only_ever_a_line_of_equals_signs(self):
        """The rewritten rule check, line by line, against the set it replaced."""
        samples = [b.decode("utf-8", "replace").replace("\x00", "").strip()
                   for b in _EDGE_LINES]
        samples += ["=" * n for n in range(1, 8)] + ["=a", "a=", "= =", "=\u3000="]
        for text in samples:
            if not text:
                continue
            with self.subTest(text=text):
                path = os.path.join(self.root, "one.txt")
                with io.open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text + "\nFolder\n" + text + "\n!SomeBot Row.flac\n")
                self.assertEqual(
                    list_mod.find_matching_entries([], list_path=path),
                    old_find_matching_entries([], None, path))


class _Counting:
    """A stand-in for a builtin that counts its calls."""

    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.real(*args, **kwargs)


class TheScanBuildsNothingPerLine(DCCoreTestCase):
    """The cost, pinned without a clock. A module global shadows a builtin,
    so these stand-ins see every set() and all() the scan makes."""

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root
        self.path = os.path.join(self.root, "plain.txt")
        with io.open(self.path, "w", encoding="utf-8", newline="") as handle:
            for n in range(200):
                handle.write("=" * 20 + "\nD:\\MEDIA\\Folder %d\\\n" % n + "=" * 20 + "\n")
                handle.write("!SomeBot Love Song %d.flac  ::INFO:: 1MB\n" % n)

    def _count(self, name, words):
        import builtins
        counter = _Counting(getattr(builtins, name))
        setattr(list_mod, name, counter)
        try:
            _entries, total = list_mod.find_matching_entries(
                words, limit=5, list_path=self.path)
        finally:
            delattr(list_mod, name)
        self.assertEqual(total, 200 if words != ["nothing-like-this"] else 0)
        return counter.calls

    def test_no_set_is_built_per_line(self):
        for words in ([], ["love"], ["love", "song"]):
            with self.subTest(words=words):
                self.assertEqual(self._count("set", words), 0)

    def test_no_all_is_called_per_row(self):
        for words in (["love"], ["love", "song"], [("love", "song")],
                      ["nothing-like-this"]):
            with self.subTest(words=words):
                self.assertEqual(self._count("all", words), 0)


if __name__ == "__main__":
    unittest.main()
