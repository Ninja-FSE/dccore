"""#1272: one malformed line in settings.conf made a configured install look
like a first run - and the setup page then replaced the admin password
before its save failed.

apply_to() skips the whole file when it cannot parse it, so NICKNAME,
CHANNEL and ADMIN_NICK - which configure.py and the setup page put there -
read as blank. startup() said "Nothing is configured yet" and opened the
first-run page. Filling it wrote admin_config.py first (the order that is
right for a real first run, #624), then failed to save settings.conf because
the broken line was still there: the page showed a write error, the bot
exited, and the password had changed anyway. configure.py did the same and
died with a SettingsWriteError traceback.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import configure  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import GOOD  # noqa: E402
from tests.test_startup import BootCase  # noqa: E402

CONFIGURED = ("# my settings\n"
              "NICKNAME = SampleBot\n"
              "CHANNEL = #example-room\n"
              "ADMIN_NICK = SampleAdmin\n")
BROKEN = CONFIGURED + "MAX_DCC_SLOTS 5\n"          # line 5: no "="
OLD_ADMIN_CONFIG = 'ADMIN_PASSWORD_HASH = "pbkdf2$sample-old-hash"\n'


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class StartupSaysWhichLine(BootCase):

    def setUp(self):
        super().setUp()
        # What the daemon sees after apply_to() skipped the file: the three
        # REQUIRED names at their shipped (blank) values.
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"],
                        CHANNEL=config.SHIPPED_DEFAULTS["CHANNEL"],
                        ADMIN_NICK=config.SHIPPED_DEFAULTS["ADMIN_NICK"])
        self.settings = settings_file.settings_path()
        self.served = []

    def page(self):
        self.served.append(1)

    def refused(self):
        buffer = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(buffer):
            self.oserve.startup(setup_page=self.page)
        return caught.exception.code, buffer.getvalue()

    def test_the_setup_page_is_not_offered(self):
        write(self.settings, BROKEN)
        code, output = self.refused()
        self.assertEqual(self.served, [], "the first-run page was opened on a configured install")
        self.assertEqual(code, 1)
        self.assertNotIn("Nothing is configured yet", output)

    def test_the_file_and_its_line_are_named(self):
        write(self.settings, BROKEN)
        _code, output = self.refused()
        self.assertIn(self.settings, output)
        self.assertIn("line 5 ", output)
        self.assertIn("MAX_DCC_SLOTS 5", output)

    def test_a_refused_required_value_is_not_a_first_run_either(self):
        """The same shape through a value: a CHANNEL settings_file refuses
        reads as unconfigured, and the page would have been offered for it."""
        write(self.settings, CONFIGURED.replace("#example-room", "example-room"))
        code, output = self.refused()
        self.assertEqual(self.served, [])
        self.assertEqual(code, 1)
        self.assertIn("CHANNEL", output)
        self.assertIn("does not start with #", output)

    def test_a_real_first_run_still_gets_the_page(self):
        """No file at all is a first run, and nothing here may change that."""
        if os.path.exists(self.settings):
            os.remove(self.settings)
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            self.oserve.startup(setup_page=self.page)
        self.assertEqual(self.served, [1])


class TheSetupPageChecksBeforeItWrites(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="dccore-broken-conf-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.settings = os.path.join(self.home, "settings.conf")
        self.admin = os.path.join(self.home, "admin_config.py")
        self.keep_every_setting()
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"],
                        CHANNEL=config.SHIPPED_DEFAULTS["CHANNEL"],
                        ADMIN_NICK=config.SHIPPED_DEFAULTS["ADMIN_NICK"],
                        ADMIN_PASSWORD_HASH="")
        self.changes, self.password_hash, errors = webserver.validate_setup_form(GOOD)
        self.assertEqual(errors, [])

    def test_the_password_is_not_replaced_when_the_save_would_fail(self):
        write(self.settings, BROKEN)
        write(self.admin, OLD_ADMIN_CONFIG)
        with self.assertRaises(settings_file.SettingsWriteError), \
                contextlib.redirect_stdout(io.StringIO()):
            webserver.apply_setup(self.changes, self.password_hash, log=lambda *_: None,
                                  settings_path=self.settings, admin_path=self.admin)
        self.assertEqual(read(self.admin), OLD_ADMIN_CONFIG)
        self.assertEqual(read(self.settings), BROKEN)

    def test_check_save_writes_nothing(self):
        report = settings_file.check_save(vars(config), {"NICKNAME": "SampleBot"},
                                          path=self.settings)
        self.assertEqual(report["checked"], ["NICKNAME"])
        self.assertFalse(os.path.exists(self.settings))

    def test_check_save_refuses_what_save_refuses(self):
        write(self.settings, BROKEN)
        with self.assertRaises(settings_file.SettingsWriteError):
            settings_file.check_save(vars(config), {"NICKNAME": "SampleBot"},
                                     path=self.settings)


class ConfigureSaysItInsteadOfATraceback(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.settings = settings_file.settings_path()
        self.written = []
        for name in ("write_admin_config_password", "collect_answers"):
            self.addCleanup(setattr, configure, name, getattr(configure, name))
        configure.write_admin_config_password = lambda *a, **k: self.written.append(a)

    def run_main(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = configure.main()
        return code, buffer.getvalue()

    def test_a_broken_file_is_named_before_any_question(self):
        write(self.settings, BROKEN)
        asked = []
        configure.collect_answers = lambda: asked.append(1) or ({}, "hash")
        code, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(asked, [], "the questions were asked of a file that cannot take the answers")
        self.assertEqual(self.written, [], "the password was written")
        self.assertIn("line 5 ", output)

    def test_answers_the_file_cannot_take_leave_the_password_alone(self):
        write(self.settings, CONFIGURED)
        configure.collect_answers = lambda: ({"NICKNAME": "not a nick"}, "hash")
        code, output = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(self.written, [])
        self.assertIn("cannot be saved", output)
        self.assertEqual(read(self.settings), CONFIGURED)


if __name__ == "__main__":
    unittest.main()
