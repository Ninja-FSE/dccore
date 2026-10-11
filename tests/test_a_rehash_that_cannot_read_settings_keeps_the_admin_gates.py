"""A rehash that could not read settings.conf made admin nick-only.

settings_file.apply_to() keeps the shipped defaults when settings.conf
cannot be read - right at startup, where the REQUIRED gate refuses to boot
and says why. During a !rehash (or a dashboard Settings save) the reload's
backstop then put back only the three REQUIRED settings, ADMIN_NICK among
them, and ADMIN_HOSTMASKS came back as none: is_admin() checks the nick
alone when there are no hostmasks, so anyone who took the (unregistered)
admin nick had every admin command, and the rehash still said it completed.
A hand edit saved as ANSI with one non-ASCII letter is enough to do it.

Now an unreadable settings.conf during a reload applies again what the file
said at its last good read, and the backstop never keeps ADMIN_NICK without
the hostmasks that narrowed it. A file that is read and says less is still
applied as it says: neither is a ratchet.

The real defaults.py reloaded by the real reload_modules_in_order(), against
a settings file of the test's own.
"""

import contextlib
import importlib
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

GOOD = ("NICKNAME = alfabot\n"
        "CHANNEL = #music\n"
        "ADMIN_NICK = alfa\n"
        "ADMIN_HOSTMASKS = *@alfa.host.example\n"
        "MAX_DCC_SLOTS = 9\n")


class ReloadCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.keep_every_setting()
        self.set_config(ADMIN_HOSTMASKS=getattr(config, "ADMIN_HOSTMASKS", []),
                        _SETTINGS_REPORT=getattr(config, "_SETTINGS_REPORT", None))
        self.dir = self.make_temp_dir()
        self.path = os.path.join(self.dir, "settings.conf")
        real = os.environ.get("DCCORE_SETTINGS_FILE")
        os.environ["DCCORE_SETTINGS_FILE"] = self.path
        self.addCleanup(
            lambda: os.environ.__setitem__("DCCORE_SETTINGS_FILE", real)
            if real is not None else os.environ.pop("DCCORE_SETTINGS_FILE", None))
        self.out = io.StringIO()

    def write(self, data):
        with io.open(self.path, "wb") as handle:
            handle.write(data)

    def rehash(self):
        with contextlib.redirect_stdout(self.out):
            commands.reload_modules_in_order(modules=("defaults",), reload_self=False)

    def a_stranger_holding_the_nick_is_admin(self):
        return commands.is_admin("alfa", "x@stranger.host.example")


class AnUnreadableFileKeepsWhatItSaid(ReloadCase):

    def test_the_audits_case(self):
        self.write(GOOD.encode("utf-8"))
        self.rehash()
        self.assertEqual(config.ADMIN_HOSTMASKS, ["*@alfa.host.example"])
        self.assertFalse(self.a_stranger_holding_the_nick_is_admin())

        # A comment with one letter saved in an ANSI code page: not UTF-8.
        self.write(GOOD.encode("utf-8") + "# r\xe4ttad\n".encode("cp1252"))
        self.rehash()

        self.assertIn("Could not read settings.conf", self.out.getvalue())
        self.assertEqual(config.ADMIN_NICK, "alfa")
        self.assertEqual(config.ADMIN_HOSTMASKS, ["*@alfa.host.example"],
                         "an unreadable settings.conf dropped the admin hostmasks")
        self.assertFalse(self.a_stranger_holding_the_nick_is_admin(),
                         "a failed read made admin nick-only")
        self.assertEqual(config.MAX_DCC_SLOTS, 9, "the other settings went back to defaults")
        self.assertIn("Kept the 5 setting(s)", self.out.getvalue())

    def test_twice_in_a_row_still_keeps_them(self):
        self.write(GOOD.encode("utf-8"))
        self.rehash()
        self.write(b"\xff\xfe broken")
        self.rehash()
        self.rehash()

        self.assertEqual(config.ADMIN_HOSTMASKS, ["*@alfa.host.example"])

    def test_once_readable_again_the_file_wins(self):
        self.write(GOOD.encode("utf-8"))
        self.rehash()
        self.write(b"\xff\xfe broken")
        self.rehash()
        self.write(GOOD.replace("MAX_DCC_SLOTS = 9", "MAX_DCC_SLOTS = 4").encode("utf-8"))
        self.rehash()

        self.assertEqual(config.MAX_DCC_SLOTS, 4)


class AReadableFileIsAppliedAsItSays(ReloadCase):

    def test_hostmasks_taken_out_of_a_readable_file_are_gone(self):
        """Control: no ratchet. The operator removed the line on purpose."""
        self.write(GOOD.encode("utf-8"))
        self.rehash()
        self.write(GOOD.replace("ADMIN_HOSTMASKS = *@alfa.host.example\n", "").encode("utf-8"))
        self.rehash()

        self.assertEqual(config.ADMIN_HOSTMASKS, [])

    def test_another_files_last_read_is_not_used(self):
        """The last good read belongs to its own path: a different file that
        cannot be read is not given another file's settings."""
        self.write(GOOD.encode("utf-8"))
        self.rehash()
        other = os.path.join(self.dir, "other.conf")
        with io.open(other, "wb") as handle:
            handle.write(b"\xff\xfe broken")
        os.environ["DCCORE_SETTINGS_FILE"] = other
        self.rehash()

        self.assertNotEqual(config.MAX_DCC_SLOTS, 9)


class TheBackstopKeepsTheNickWithItsHostmasks(DCCoreTestCase):
    """reload_modules_in_order()'s REQUIRED backstop, with the reload replaced
    by one that does what a missing or unreadable file does: blank."""

    def run_reload(self, blank):
        def blanking_reload(module):
            for name, value in blank.items():
                setattr(config, name, value)
            return module
        real_reload = importlib.reload
        importlib.reload = blanking_reload
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                commands.reload_modules_in_order(modules=("runtime",), reload_self=False)
        finally:
            importlib.reload = real_reload

    def test_a_kept_nick_keeps_its_hostmasks(self):
        self.set_config(ADMIN_NICK="alfa", ADMIN_HOSTMASKS=["*@alfa.host.example"])

        self.run_reload({"ADMIN_NICK": "", "ADMIN_HOSTMASKS": []})

        self.assertEqual(config.ADMIN_NICK, "alfa")
        self.assertEqual(config.ADMIN_HOSTMASKS, ["*@alfa.host.example"],
                         "the backstop kept the admin nick without its hostmasks")

    def test_hostmasks_removed_while_the_nick_stays_are_removed(self):
        """No ratchet: the nick came back, so nothing was kept, and the
        hostmasks are what the configuration now says."""
        self.set_config(ADMIN_NICK="alfa", ADMIN_HOSTMASKS=["*@alfa.host.example"])

        self.run_reload({"ADMIN_HOSTMASKS": []})

        self.assertEqual(config.ADMIN_HOSTMASKS, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
