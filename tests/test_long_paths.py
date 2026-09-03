"""Library files past Windows' 260-character MAX_PATH must still be servable.

WHY THIS MATTERS HERE AND NOT IN THEORY

Measured against the operator's real library (47,420 files on a NAS share):
the longest path is 259 characters. The limit is 260. One character of
headroom - so renaming an album slightly longer, or nesting one level deeper,
silently makes those files unservable.

It gets worse the moment the daemon runs unattended. The library is reached
through a mapped drive (Z:), and mapped drives are per-user-session: a Windows
service, or a scheduled task set to "run whether user is logged on or not",
sees no Z: at all and has to address the share by UNC instead. That respelling
adds 17 characters, which puts 49 real files over the limit. Opening them by
plain UNC succeeds 0 times out of 49; through long_path() it succeeds 49 out
of 49.

The failure is an ordinary FileNotFoundError on a file that is plainly there,
which is about the least helpful thing Windows can say.

platform_compat.long_path() existed for exactly this and was called by nothing
until now. These tests pin the sites that call it.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support  # noqa: E402

import defaults as config  # noqa: E402
import platform_compat  # noqa: E402
import dcc  # noqa: E402
import update_list  # noqa: E402

from tests.test_path_security import PathSecurityBase, quiet  # noqa: E402

WINDOWS_ONLY = "MAX_PATH is a Windows limit; POSIX allows 4096"


def _deep_dir(root, target_len=250):
    """A directory path at least `target_len` characters long."""
    path = root
    while len(path) < target_len:
        path = os.path.join(path, "a-fairly-long-directory-segment")
    return path


def _make(path, payload):
    os.makedirs(platform_compat.long_path(os.path.dirname(path)), exist_ok=True)
    with open(platform_compat.long_path(path), "wb") as handle:
        handle.write(payload)
    return path


def _max_path_is_enforced():
    """Does this machine actually refuse a path longer than 260 characters?

    Windows 10 1607+ can opt out of MAX_PATH entirely via the LongPathsEnabled
    registry value, and GitHub's windows-latest images ship with it ON. On such
    a machine a 300-character path simply works, so the control assertions
    below - which assert that plain access FAILS - are asserting something
    untrue of that machine rather than something untrue of the code.

    That is the same trap the loopback tests fell into: an environment-
    dependent precondition asserted as universal. Reported as a FAILURE it
    reads as "the codebase is broken" when the honest answer is "not here".

    The behaviour under test is still worth pinning on such a machine - the
    positive tests all run everywhere - but a control that cannot reproduce
    the hazard proves nothing and must say so.

    The operator's own box enforces it: measured, 0 of 49 real library files
    opened by plain UNC and 49 of 49 through long_path(). That is why this
    matters at all.
    """
    if not platform_compat.IS_WINDOWS:
        return False
    probe_root = tempfile.mkdtemp(prefix="dccore-maxpath-probe-")
    try:
        deep = _deep_dir(probe_root, 250)
        target = os.path.join(deep, "probe-file-with-a-long-enough-name.txt")
        if len(target) <= 260:
            return False
        os.makedirs(platform_compat.long_path(deep), exist_ok=True)
        with open(platform_compat.long_path(target), "wb") as handle:
            handle.write(b"x")
        # If the plain spelling can see it, this machine does not enforce it.
        return not os.path.exists(target)
    except OSError:
        return True
    finally:
        shutil.rmtree(platform_compat.long_path(probe_root), ignore_errors=True)


MAX_PATH_ENFORCED = _max_path_is_enforced()
NEEDS_ENFORCEMENT = ("this machine does not enforce MAX_PATH "
                     "(LongPathsEnabled), so the hazard cannot be reproduced here")


class LongPathSpelling(unittest.TestCase):
    """The prefix rules. These run everywhere; on POSIX it is an identity."""

    def test_it_is_an_identity_off_windows(self):
        if platform_compat.IS_WINDOWS:
            self.skipTest("Windows applies a prefix; checked below")
        self.assertEqual(platform_compat.long_path("/srv/library/x.flac"),
                         "/srv/library/x.flac")

    @unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
    def test_a_drive_path_gets_the_extended_prefix(self):
        got = platform_compat.long_path("Z:\\1 Metal\\x.flac")
        self.assertEqual(got, "\\\\?\\Z:\\1 Metal\\x.flac")

    @unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
    def test_a_unc_path_gets_the_unc_form_not_the_plain_one(self):
        """\\\\?\\\\server\\share is NOT valid - UNC needs \\\\?\\UNC\\server\\share.

        This is the spelling the service deployment depends on, so getting it
        wrong would break exactly the case the change exists for.
        """
        got = platform_compat.long_path("\\\\MEDIASERVER\\Music\\1 Metal\\x.flac")
        self.assertEqual(got, "\\\\?\\UNC\\MEDIASERVER\\Music\\1 Metal\\x.flac")

    @unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
    def test_an_already_prefixed_path_is_left_alone(self):
        already = "\\\\?\\Z:\\1 Metal\\x.flac"
        self.assertEqual(platform_compat.long_path(already), already)

    @unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
    def test_a_relative_path_is_absolutised_first(self):
        # The extended-length API rejects relative paths outright.
        got = platform_compat.long_path("data\\tmp_zips\\x.rar")
        self.assertTrue(got.startswith("\\\\?\\"), got)
        self.assertNotIn("\\\\?\\data", got)

    def test_empty_and_none_survive(self):
        self.assertEqual(platform_compat.long_path(""), "")
        self.assertIsNone(platform_compat.long_path(None))


@unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
class AFileBeyondMaxPathIsReachable(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dccore-longpath-")
        self.addCleanup(shutil.rmtree,
                        platform_compat.long_path(self.root), True)
        self.payload = b"\x00" * 5000
        self.track = _make(
            os.path.join(_deep_dir(self.root),
                         "01 - A Track With A Reasonably Long Title Here.flac"),
            self.payload)
        self.assertGreater(len(self.track), 260,
                           "the fixture must actually exceed MAX_PATH")

    @unittest.skipUnless(MAX_PATH_ENFORCED, NEEDS_ENFORCEMENT)
    def test_control_plain_access_really_does_fail(self):
        """If this ever stops failing on a machine that DOES enforce MAX_PATH,
        the tests below prove nothing."""
        self.assertFalse(os.path.exists(self.track))
        with self.assertRaises(OSError):
            open(self.track, "rb").close()

    def test_exists_through_long_path(self):
        self.assertTrue(os.path.exists(platform_compat.long_path(self.track)))

    def test_getsize_returns_the_real_size_not_zero(self):
        """A zero here would be advertised in the DCC offer, and a receiver
        told to expect 0 bytes closes immediately."""
        self.assertEqual(
            os.path.getsize(platform_compat.long_path(self.track)),
            len(self.payload))

    def test_the_bytes_come_back_intact(self):
        with open(platform_compat.long_path(self.track), "rb") as handle:
            self.assertEqual(handle.read(), self.payload)


@unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
class TheLibraryScanSeesDeepFiles(support.DCCoreTestCase):
    """update_list.py walks the library and records each file's size.

    Before this change the getsize() there was a plain call, so a deep track
    raised inside the per-file try/except and was recorded as 0 bytes - present
    in the list, but advertised as empty.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.deep_payload = b"\x00" * 7777
        self.deep_track = _make(
            os.path.join(_deep_dir(self.tree.music, 240),
                         "99 - A Deep Track With A Long Name.flac"),
            self.deep_payload)
        self.assertGreater(len(self.deep_track), 260)

    def test_the_deep_track_is_counted_with_its_real_size(self):
        self.assertTrue(update_list.generate_master_list())

        import list as list_mod
        count, _date, _human, raw_bytes = \
            list_mod.get_file_count_date_size_and_raw_bytes()

        # The fixture's two shallow tracks are 4096 each, plus the deep one.
        self.assertEqual(count, 3, "the deep track was dropped from the list")
        self.assertEqual(raw_bytes, 4096 + 4096 + len(self.deep_payload),
                         "the deep track was counted as 0 bytes")

    def test_the_deep_track_appears_in_the_text_list(self):
        self.assertTrue(update_list.generate_master_list())
        import list as list_mod
        path = list_mod.find_latest_list()
        self.assertIsNotNone(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        self.assertIn("99 - A Deep Track With A Long Name.flac", body)


@unittest.skipUnless(platform_compat.IS_WINDOWS, WINDOWS_ONLY)
class ADeepLibraryFileIsNotReportedMissing(PathSecurityBase):
    """The gates in dcc.handle_download_request decide whether a request is
    honoured at all.

    They are plain os.path.exists() calls on a path built from the library root
    plus the requested name. For a file past MAX_PATH the answer is False even
    though the file is plainly there, so the daemon answers "no such file" -
    the most consequential place this bug can land, and the one an operator
    would report as "the bot is lying about my library".

    The assertion is deliberately "was not refused" rather than "was sent":
    whether a request dispatches immediately or lands in the queue depends on
    whether a slot happens to be free, which is not what these tests are about.
    A refusal, on the other hand, is unambiguous.
    """

    DEEP_NAME = "99 - A Deep Track With A Long Name.flac"

    def setUp(self):
        super().setUp()
        deep_dir = self.tree.music
        while len(deep_dir) < 240:
            deep_dir = os.path.join(deep_dir, "a-fairly-long-directory-segment")
        self.deep_track = os.path.join(deep_dir, self.DEEP_NAME)
        os.makedirs(platform_compat.long_path(deep_dir), exist_ok=True)
        with open(platform_compat.long_path(self.deep_track), "wb") as handle:
            handle.write(b"\x00" * 6543)
        self.assertGreater(len(self.deep_track), 260,
                           "fixture must exceed MAX_PATH")
        if MAX_PATH_ENFORCED:
            # Only true where the machine enforces the limit. Where it does
            # not, the file is plainly reachable and these tests still pass -
            # they just are not proving anything the plain spelling could not
            # have done on its own.
            self.assertFalse(os.path.exists(self.deep_track),
                             "fixture invariant: plain access must fail")

    def _request(self, name):
        self.notices.clear()
        with quiet():
            dcc.handle_download_request(self.sock, "dave", name, "#dccore-test")
        return [kind for kind, _args in self.notices]

    def test_a_genuinely_missing_file_IS_refused(self):
        """The discriminator. Without this, "not refused" could pass for a
        build that never refuses anything."""
        self.assertIn("error", self._request("No Such Track At All.flac"))

    def test_a_shallow_file_is_not_refused(self):
        """Control: the same call path works for an ordinary file, so a failure
        below is about the depth and not the plumbing."""
        self.assertNotIn("error",
                         self._request(os.path.basename(self.tree.tracks[0])))

    def test_the_deep_file_is_not_refused(self):
        kinds = self._request(self.DEEP_NAME)
        self.assertNotIn(
            "error", kinds,
            "a file past MAX_PATH was refused as though it did not exist")
        self.assertTrue(
            {"sending", "queue"} & set(kinds),
            f"expected the deep file to be sent or queued, got {kinds}")


if __name__ == "__main__":
    unittest.main()
