"""#624: the setup wrote settings.conf before admin_config.py.

The two writes are not one transaction. Once settings.conf carries NICKNAME,
CHANNEL and ADMIN_NICK the REQUIRED gate is satisfied, so when the password
write then failed (admin_config.py held open by an editor or a scanner, a
disk that filled between the two writes) and the operator restarted instead
of retrying in the same page, the bot skipped the setup page, joined IRC and
webserver.start() refused the dashboard for the missing hash - with the
browser form never offered again. Writing admin_config.py first fails safe:
a hash on disk without settings.conf trips the gate as before, the page comes
back, and the next attempt replaces the line in place.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import configure  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import GOOD  # noqa: E402


class _Refused(PermissionError):
    """The write that did not happen."""


def _refuse(*_args, **_kwargs):
    raise _Refused("admin_config.py: held open by something else")


class TheBrowserSetup(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-order-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = os.path.join(self.tmp, "settings.conf")
        self.admin = os.path.join(self.tmp, "admin_config.py")
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"],
                        CHANNEL=config.SHIPPED_DEFAULTS["CHANNEL"],
                        ADMIN_NICK=config.SHIPPED_DEFAULTS["ADMIN_NICK"],
                        ADMIN_PASSWORD_HASH="")
        self.changes, self.password_hash, errors = webserver.validate_setup_form(GOOD)
        self.assertEqual(errors, [])

    def apply(self):
        webserver.apply_setup(self.changes, self.password_hash, log=lambda *_: None,
                              settings_path=self.settings, admin_path=self.admin)

    def patch_writer(self, name, replacement):
        real = getattr(configure, name)
        setattr(configure, name, replacement)
        return lambda: setattr(configure, name, real)

    def test_a_refused_password_write_leaves_no_settings_conf_behind(self):
        """The failure the issue describes: nothing on disk satisfies the gate."""
        self.addCleanup(self.patch_writer("write_admin_config_password", _refuse))
        with self.assertRaises(_Refused):
            self.apply()
        self.assertFalse(os.path.exists(self.settings),
                         "settings.conf was written before the password write failed")
        self.assertFalse(os.path.exists(self.admin))
        # the running process is untouched, so the gate still trips
        self.assertEqual(config.NICKNAME, config.SHIPPED_DEFAULTS["NICKNAME"])
        self.assertNotEqual(
            settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS), [])

    def test_the_password_write_happens_before_the_settings_write(self):
        order = []
        real_admin = configure.write_admin_config_password
        real_settings = configure.write_settings_conf

        def admin(*args, **kwargs):
            order.append("admin_config.py")
            return real_admin(*args, **kwargs)

        def settings(*args, **kwargs):
            order.append("settings.conf")
            return real_settings(*args, **kwargs)

        self.addCleanup(self.patch_writer("write_admin_config_password", admin))
        self.addCleanup(self.patch_writer("write_settings_conf", settings))
        self.apply()
        self.assertEqual(order, ["admin_config.py", "settings.conf"])

    def test_a_refused_settings_write_still_offers_the_page_again(self):
        """The other order of failure: a hash on disk, no settings.conf. The
        gate trips as before, and the retry replaces the line in place."""
        restore = self.patch_writer("write_settings_conf", _refuse)
        self.addCleanup(restore)
        with self.assertRaises(_Refused):
            self.apply()
        self.assertTrue(os.path.exists(self.admin))
        self.assertFalse(os.path.exists(self.settings))
        self.assertNotEqual(
            settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS), [])
        # the operator tries again, this time with the write going through
        restore()
        self.apply()
        with io.open(self.admin, encoding="utf-8") as handle:
            admin_text = handle.read()
        self.assertEqual(admin_text.count("ADMIN_PASSWORD_HASH ="), 1)
        self.assertIn(self.password_hash, admin_text)
        self.assertTrue(os.path.exists(self.settings))
        self.assertEqual(
            settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS), [])


class TheTerminalSetup(unittest.TestCase):
    """configure.py's main() runs the same two writers; same order."""

    def patch(self, name, replacement):
        real = getattr(configure, name)
        setattr(configure, name, replacement)
        self.addCleanup(setattr, configure, name, real)

    def test_a_refused_password_write_never_reaches_settings_conf(self):
        written = []
        self.patch("collect_answers", lambda: ({"NICKNAME": "MusicBot"}, "hash"))
        self.patch("write_admin_config_password", _refuse)
        self.patch("write_settings_conf", lambda changes, path=None: written.append(changes))
        self.patch("offer_to_generate_master_list", lambda *_: None)
        self.patch("offer_to_import_omenserve_stats", lambda *_: None)
        with self.assertRaises(_Refused):
            configure.main()
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
