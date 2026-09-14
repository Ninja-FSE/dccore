"""Two ways `configure.py` could leave an operator worse off than before.

CHANGING THE PASSWORD MIGHT DO NOTHING

defaults.py applies `admin_config.py` first and `settings.conf` second, and
says so: "settings.conf is applied SECOND and therefore wins". The dashboard's
own change-password control writes to settings.conf.

So on any install whose password has ever been changed from the dashboard,
running `configure.py` to rotate the credential wrote a new hash into
admin_config.py where nothing would ever read it - and the operator went on
believing they had replaced a password that still worked.

settings_file.shadowed_by_admin_config() warns about exactly this collision
from the other direction, on the settings.conf write path. The reverse
direction had no check at all.

A HALF-WRITTEN admin_config.py BRICKS THE INSTALL

`open(path, "w")` truncates before it writes. A full disk, a killed process or
a power cut there leaves a partial Python file - and a partial Python file is
a SyntaxError, which defaults.py's `except ImportError` around
`from admin_config import *` does not catch.

Verified: `import defaults` then fails outright. configure.py imports defaults
itself, so the one tool that could repair the file will not start either. The
install is unbootable and un-reconfigurable, from a routine password change.

settings_file._atomic_write() already does this correctly for the other config
file.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import configure  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class WritingTheHashIsAtomic(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "admin_config.py")
        self.sample = os.path.join(self.tree.root, "admin_config.py.sample")
        with io.open(self.sample, "w", encoding="utf-8") as handle:
            handle.write('ADMIN_HOSTMASKS = ["*!*@example.invalid"]\n')

    def test_a_failed_write_leaves_the_original_intact(self):
        """The whole point of atomicity. Before this, the file was truncated
        first and the failure left a Python file that will not parse."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write('ADMIN_PASSWORD_HASH = "OLD"\n'
                         'ADMIN_HOSTMASKS = ["*!*@example.invalid"]\n')

        real_write = settings_file._atomic_write

        def exploding(path, text):
            raise OSError(28, "No space left on device")

        settings_file._atomic_write = exploding
        self.addCleanup(lambda: setattr(settings_file, "_atomic_write", real_write))

        with self.assertRaises(OSError):
            configure.write_admin_config_password("NEW", path=self.path,
                                                  sample_path=self.sample)

        with io.open(self.path, encoding="utf-8") as handle:
            left = handle.read()
        self.assertIn("OLD", left)
        self.assertIn("ADMIN_HOSTMASKS", left)

    def test_what_it_leaves_behind_still_parses_as_python(self):
        """The failure mode that mattered: a SyntaxError here is not caught by
        defaults.py's `except ImportError`, so the daemon will not boot - and
        configure.py imports defaults, so it will not run to repair it."""
        import ast

        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write('ADMIN_PASSWORD_HASH = "OLD"\n')

        configure.write_admin_config_password("NEW", path=self.path,
                                              sample_path=self.sample)

        with io.open(self.path, encoding="utf-8") as handle:
            ast.parse(handle.read())

    def test_the_new_hash_is_actually_written(self):
        """Control: atomicity is worthless if it writes nothing."""
        configure.write_admin_config_password("BRANDNEW", path=self.path,
                                              sample_path=self.sample)

        with io.open(self.path, encoding="utf-8") as handle:
            self.assertIn("BRANDNEW", handle.read())

    def test_it_still_keeps_the_rest_of_the_file(self):
        """Unchanged behaviour, and the reason the function exists: never
        overwrite what a hand-edited admin_config.py already has."""
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write('ADMIN_HOSTMASKS = ["*!*@keepme.invalid"]\n'
                         'ADMIN_PASSWORD_HASH = "OLD"\n')

        configure.write_admin_config_password("NEW", path=self.path,
                                              sample_path=self.sample)

        with io.open(self.path, encoding="utf-8") as handle:
            left = handle.read()
        self.assertIn("keepme.invalid", left)
        self.assertNotIn("OLD", left)


class AShadowedPasswordIsReported(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "admin_config.py")
        self.sample = os.path.join(self.tree.root, "admin_config.py.sample")
        with io.open(self.sample, "w", encoding="utf-8") as handle:
            handle.write("# sample\n")
        self.conf = os.environ["DCCORE_SETTINGS_FILE"]

    def write_settings(self, text):
        with io.open(self.conf, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_it_notices_settings_conf_setting_the_same_name(self):
        self.write_settings("ADMIN_PASSWORD_HASH = pbkdf2_sha256$1$aa$bb\n")

        self.assertIsNotNone(configure.settings_conf_shadows_password())

    def test_it_says_nothing_when_settings_conf_does_not_set_it(self):
        self.write_settings("NICKNAME = SomeBot\n")

        self.assertIsNone(configure.settings_conf_shadows_password())

    def test_a_missing_settings_conf_is_not_a_shadow(self):
        if os.path.exists(self.conf):
            os.remove(self.conf)

        self.assertIsNone(configure.settings_conf_shadows_password())

    def test_an_unreadable_settings_conf_does_not_stop_the_password_change(self):
        """A broken settings.conf is a fault the daemon reports at startup on
        its own. It must not be a reason to refuse to set a password."""
        real_parse = settings_file.parse

        def exploding(_text):
            raise ValueError("unparseable")

        settings_file.parse = exploding
        self.addCleanup(lambda: setattr(settings_file, "parse", real_parse))
        self.write_settings("whatever\n")

        self.assertIsNone(configure.settings_conf_shadows_password())

    def test_the_operator_is_warned_rather_than_left_guessing(self):
        self.write_settings("ADMIN_PASSWORD_HASH = pbkdf2_sha256$1$aa$bb\n")

        shadow = configure.write_admin_config_password(
            "NEW", path=self.path, sample_path=self.sample)

        self.assertIsNotNone(
            shadow,
            "the hash was written where nothing will read it, and the "
            "operator was told the password had been set")

    def test_no_warning_when_nothing_shadows_it(self):
        self.write_settings("NICKNAME = SomeBot\n")

        shadow = configure.write_admin_config_password(
            "NEW", path=self.path, sample_path=self.sample)

        self.assertIsNone(shadow)


if __name__ == "__main__":
    unittest.main()
