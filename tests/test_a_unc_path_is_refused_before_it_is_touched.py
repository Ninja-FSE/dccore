r"""#578: a UNC filename made the Windows bot open an SMB connection.

os.path.join(base, r"\\host\share\x") returns the UNC path unchanged, and the
os.path.exists() and realpath() calls that come BEFORE the containment check
make Windows resolve the host and open an SMB session (NTLM) as the account
running the bot - from one line in the channel, and for the length of the SMB
timeout. The name is now refused on its text, before any file system call.

The behaviour is Windows', so what is tested here is the part that is ours and
runs everywhere: that the guard refuses those names, that a legitimate name
passes, and - by recording every file system call the handler makes - that
nothing looks at the path before the refusal.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
from tests.test_path_security import PathSecurityBase, quiet  # noqa: E402

UNC = "\\\\evil.example\\share\\a.mp3"
GUARD = dcc.names_a_remote_or_absolute_path


class TheGuard(unittest.TestCase):

    def test_a_unc_path_is_refused_on_every_platform(self):
        for windows in (True, False):
            self.assertTrue(GUARD(UNC, windows=windows), windows)

    def test_a_unc_path_written_with_forward_slashes_is_refused(self):
        """Windows reads a doubled slash of either kind as UNC."""
        self.assertTrue(GUARD("//evil.example/share/a.mp3", windows=False))
        self.assertTrue(GUARD("/\\evil.example\\share\\a.mp3", windows=False))

    def test_device_paths_are_refused(self):
        self.assertTrue(GUARD("\\\\?\\UNC\\evil.example\\share\\a", windows=False))
        self.assertTrue(GUARD("\\\\.\\PhysicalDrive0", windows=False))

    def test_a_drive_letter_is_refused_on_windows_only(self):
        self.assertTrue(GUARD("C:\\Windows\\win.ini", windows=True))
        self.assertTrue(GUARD("C:win.ini", windows=True), "drive-relative")
        self.assertFalse(GUARD("C:\\Windows\\win.ini", windows=False))

    def test_a_root_relative_name_is_refused_on_windows_only(self):
        self.assertTrue(GUARD("\\Windows\\win.ini", windows=True))
        self.assertFalse(GUARD("\\Windows\\win.ini", windows=False))

    def test_a_nul_byte_is_refused_everywhere(self):
        for windows in (True, False):
            self.assertTrue(GUARD("a\x00b.mp3", windows=windows))

    def test_ordinary_names_pass_on_both(self):
        for name in ("Song.flac", "Artist/Album (2024)/01 - Song.flac",
                     "Artist - Album [FLAC]/Track 1.flac", "Sigur R\u00f3s - \u00c1g\u00e6tis byrjun.flac",
                     "It's 5.30: A Song.mp3".replace(":", "")):
            for windows in (True, False):
                self.assertFalse(GUARD(name, windows=windows), (name, windows))

    def test_the_default_follows_the_platform(self):
        import platform_compat
        with mock.patch.object(platform_compat, "IS_WINDOWS", True):
            self.assertTrue(GUARD("C:\\x"))
        with mock.patch.object(platform_compat, "IS_WINDOWS", False):
            self.assertFalse(GUARD("C:\\x"))


class TheHandlerTouchesNothing(PathSecurityBase):
    """Records every os.path call the request makes and fails if any sees the
    remote name - which on Windows is the connection itself."""

    def request(self, name, user="dave"):
        seen = []
        real = {n: getattr(os.path, n) for n in ("exists", "isfile", "isdir", "realpath", "abspath", "getsize")}

        def spy(name_):
            def wrapper(path, *a, **k):
                if "evil.example" in str(path):
                    seen.append((name_, str(path)))
                return real[name_](path, *a, **k)
            return wrapper

        patches = [mock.patch.object(os.path, n, spy(n)) for n in real]
        for p in patches:
            p.start()
        try:
            with quiet():
                dcc.handle_download_request(self.sock, user, name, "#dccore-test")
        finally:
            for p in patches:
                p.stop()
        return seen

    def test_a_unc_file_request_is_refused_without_a_file_system_call(self):
        seen = self.request(UNC)
        self.assertEqual(seen, [])
        self.assertIn(("error", (mock.ANY, "invalid_path")), self.notices)
        self.assertEqual(config_queue(), {})

    def test_the_forward_slash_form_is_refused_too(self):
        seen = self.request("//evil.example/share/a.mp3")
        self.assertEqual(seen, [])
        self.assertIn(("error", (mock.ANY, "invalid_path")), self.notices)

    def test_a_unc_pack_request_is_refused_without_a_file_system_call(self):
        seen = self.request("!rar \\\\evil.example\\share\\Album")
        self.assertEqual(seen, [])
        self.assertIn("pack_error", [kind for kind, _a in self.notices])
        self.assertEqual(config_queue(), {})

    def test_a_pack_request_written_as_a_heading_still_gets_through_the_guard(self):
        r"""`D:\...` is how a heading is written; only the remote forms are
        refused for a pack request. Refused later, for its own reasons (no
        such folder) - the guard's own line is what must be absent."""
        with quiet() as printed:
            dcc.handle_download_request(self.sock, "dave",
                                        "!rar D:\\MEDIA\\nowhere\\Artist\\Album", "#dccore-test")
        self.assertNotIn("names a remote location", printed.getvalue())

    def test_an_ordinary_missing_file_is_still_reported_as_missing(self):
        self.request("No Such Song.flac")
        self.assertIn(("error", (mock.ANY, "file_not_found")), self.notices)


def config_queue():
    return config.dcc_queue


if __name__ == "__main__":
    unittest.main()
