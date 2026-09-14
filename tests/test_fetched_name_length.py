"""The offering bot chooses the filename, and 255 was still the wall.

WHAT WENT WRONG

A fetched file is written as "<request-id>_<cleaned offer name>" under
FETCHED_FILES_DIR. _sanitize_offer_filename() removes separators, `..`, nulls
and anything outside the charset whitelist - but it does not TRUNCATE, and the
comment at the open() call said that was fine:

    # _sanitize_offer_filename() does not truncate, so the length of this
    # name is entirely the offering bot's choice - wrap it like dcc.py
    # wraps every path it touches.

    handle = open(platform_compat.long_path(dest_path), "wb")

There are two different limits and long_path() only lifts one of them. The
`\\?\` prefix removes the 260-character limit on the TOTAL PATH. The limit on
one path COMPONENT - 255 characters on NTFS, 255 bytes on ext4 - is a
filesystem rule underneath that prefix and does not move. Measured against a
real filesystem by binary search: 255, with the wrap in place.

So an offer whose name was too long failed at open() with

    [Errno 22] Invalid argument

caught by the transfer's own `except Exception` and reported as
"transfer error: ...", which names neither the length nor the name. Nothing
crashed; the fetch simply never worked, every time, for that file.

AND THE PREFIX COUNTS. The request id is uuid4().hex[:12] plus an underscore,
so the budget for the offered name was 242 characters, not 255. That is short
enough to reach with a real filename - a long classical or live-recording
title - rather than only with a hostile one.
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import dcc_fetch  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

LIMIT = dcc_fetch.MAX_NAME_BYTES


class ANameIsCutToOneComponent(unittest.TestCase):

    def test_a_normal_name_is_untouched(self):
        """Control, and the overwhelmingly common case."""
        name = "Artist - Album - 01 Song.flac"

        self.assertEqual(dcc_fetch._fit_name_component(name), name)

    def test_a_name_at_the_limit_is_untouched(self):
        name = "a" * (LIMIT - 5) + ".mp3"
        self.assertEqual(len(name.encode("utf-8")), LIMIT - 1)

        self.assertEqual(dcc_fetch._fit_name_component(name), name)

    def test_a_long_name_is_cut_to_the_limit(self):
        result = dcc_fetch._fit_name_component("a" * 500 + ".mp3")

        self.assertLessEqual(len(result.encode("utf-8")), LIMIT)

    def test_the_extension_survives(self):
        """The stem shrinks, not the tail - the same choice
        announce.fit_irc_filename() makes, and for the same reason: the
        extension is how the file is recognised and opened."""
        result = dcc_fetch._fit_name_component("a" * 500 + ".flac")

        self.assertTrue(result.endswith(".flac"))

    def test_multibyte_characters_are_not_split(self):
        """A byte budget cut blindly lands inside a character. The result has
        to survive a round trip, because it is a str the moment it is used."""
        result = dcc_fetch._fit_name_component("é" * 300 + ".flac")

        self.assertLessEqual(len(result.encode("utf-8")), LIMIT)
        self.assertEqual(result, result.encode("utf-8").decode("utf-8"))
        self.assertTrue(result.endswith(".flac"))

    def test_bytes_not_characters(self):
        """Two bytes each, so 200 accented characters is 400 bytes - under a
        character limit and over a byte one. Linux counts bytes."""
        result = dcc_fetch._fit_name_component("é" * 200 + ".mp3")

        self.assertLessEqual(len(result.encode("utf-8")), LIMIT)
        self.assertLess(len(result), 200)

    def test_an_extension_longer_than_the_budget(self):
        """"x." followed by 400 characters is not an extension worth keeping,
        and preserving it would leave no room for anything else."""
        result = dcc_fetch._fit_name_component("x." + "y" * 400)

        self.assertLessEqual(len(result.encode("utf-8")), LIMIT)

    def test_a_name_with_no_extension(self):
        result = dcc_fetch._fit_name_component("z" * 400)

        self.assertLessEqual(len(result.encode("utf-8")), LIMIT)


class TruncatingUtf8(unittest.TestCase):

    def test_it_never_returns_a_partial_character(self):
        for limit in range(1, 12):
            with self.subTest(limit=limit):
                result = dcc_fetch._truncate_utf8("éééé", limit)

                self.assertEqual(result, result.encode("utf-8").decode("utf-8"))
                self.assertLessEqual(len(result.encode("utf-8")), limit)

    def test_ascii_is_cut_exactly(self):
        self.assertEqual(dcc_fetch._truncate_utf8("abcdef", 3), "abc")

    def test_a_four_byte_character_is_dropped_whole(self):
        """An emoji in a filename is four bytes; three of them is not a
        character."""
        result = dcc_fetch._truncate_utf8("\U0001f600ab", 3)

        self.assertEqual(result, "")


class TheStoredNameFitsIncludingThePrefix(DCCoreTestCase):
    """The request id is part of what has to fit, and was not counted."""

    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(self.dest, exist_ok=True)
        self.set_config(FETCHED_FILES_DIR=self.dest)

    def test_the_offered_name_that_used_to_be_just_over(self):
        """243 characters plus a 13-character prefix. Reachable with a real
        filename, which is what makes this worth fixing rather than only
        worth refusing."""
        _dir, stored = dcc_fetch._resolve_destination_path(
            "abcdef123456", "b" * 243 + ".mp3")

        self.assertLessEqual(len(stored.encode("utf-8")), LIMIT)
        self.assertTrue(stored.startswith("abcdef123456_"))
        self.assertTrue(stored.endswith(".mp3"))

    def test_a_hostile_length(self):
        _dir, stored = dcc_fetch._resolve_destination_path(
            "abcdef123456", "a" * 5000 + ".mp3")

        self.assertLessEqual(len(stored.encode("utf-8")), LIMIT)

    def test_containment_still_holds_after_truncation(self):
        """Truncation happens before the is_safe_path() check, so it must not
        be able to produce a name that escapes - and a name cut to "." or ".."
        would be exactly that."""
        for raw in ("../../../evil.txt", "." * 400, "a" * 400 + "/../../x"):
            with self.subTest(raw=raw):
                dest_dir, stored = dcc_fetch._resolve_destination_path(
                    "abcdef123456", raw)

                self.assertIsNotNone(dest_dir)
                final = os.path.abspath(os.path.join(dest_dir, stored))
                self.assertTrue(final.startswith(os.path.abspath(self.dest) + os.sep))

    def test_the_result_can_actually_be_opened(self):
        """The assertion the byte arithmetic exists for. Skipped rather than
        assumed if this filesystem cannot hold a name at the limit at all -
        the daemon runs on ext4, NTFS and whatever a container mounts."""
        probe = os.path.join(self.dest, "p" * 200 + ".tmp")
        try:
            with io.open(platform_compat.long_path(probe), "wb"):
                pass
            os.unlink(platform_compat.long_path(probe))
        except OSError:
            self.skipTest("this filesystem refuses a 204-byte name; the limit "
                          "here is not the one under test")

        dest_dir, stored = dcc_fetch._resolve_destination_path(
            "abcdef123456", "a" * 5000 + ".mp3")
        target = platform_compat.long_path(os.path.join(dest_dir, stored))

        with io.open(target, "wb") as handle:
            handle.write(b"payload")

        self.assertTrue(os.path.isfile(target))


class TheOpenSiteNoLongerClaimsLongPathCoversIt(unittest.TestCase):

    def test_the_comment_does_not_say_the_wrap_makes_the_length_safe(self):
        """The defect was documented as handled. A future reader following that
        comment would remove the fit as redundant."""
        with io.open(os.path.join(REPO_ROOT, "dcc_fetch.py"),
                     encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn("does not truncate, so the length of this", source,
                         "the open() site still claims long_path() makes the "
                         "offered name's length safe - it lifts the total-path "
                         "limit, not the per-component one")


if __name__ == "__main__":
    unittest.main()
