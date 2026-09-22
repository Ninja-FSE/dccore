"""#590: after the browser setup, the daemon looked for the wrong list file.

apply_setup() makes the running process see the new settings through
settings_file.apply_to(), which assigns NICKNAME but never re-runs defaults.py's
derivation of LIST_BASE_NAME from it. The daemon kept the shipped "DCCore"; the
list rebuild (a fresh `python update_list.py`) imports defaults anew and wrote
"<nick>-<date>.zip". The daemon then globbed for DCCore-* and saw no list - the
advert said no list, `@<nick> list` failed - while the dashboard reported the
rebuild as done, until a restart or the next settings save (a rehash).
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

import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import GOOD  # noqa: E402


class TheDerivation(DCCoreTestCase):

    def test_an_untouched_name_follows_the_nickname(self):
        self.set_config(LIST_BASE_NAME="DCCore", NICKNAME="MusicBot")
        self.assertEqual(config.derive_list_base_name(), "MusicBot")
        self.assertEqual(config.LIST_BASE_NAME, "MusicBot")

    def test_a_name_the_operator_chose_is_left_alone(self):
        self.set_config(LIST_BASE_NAME="MyList", NICKNAME="MusicBot")
        self.assertEqual(config.derive_list_base_name(), "MyList")

    def test_it_is_sanitised_like_at_import(self):
        self.set_config(LIST_BASE_NAME="DCCore", NICKNAME="Bad/Nick*")
        self.assertEqual(config.derive_list_base_name(), "Bad_Nick_")

    def test_no_nickname_no_change(self):
        self.set_config(LIST_BASE_NAME="DCCore", NICKNAME=None)
        self.assertEqual(config.derive_list_base_name(), "DCCore")

    def test_import_still_derives_it(self):
        """The module-level call is what made it work before."""
        with io.open(os.path.join(REPO_ROOT, "defaults.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("\nderive_list_base_name()\n", source)


class AfterTheSetupPage(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-setup-list-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.set_config(NICKNAME=config.SHIPPED_DEFAULTS["NICKNAME"], ADMIN_PASSWORD_HASH="",
                        LIST_BASE_NAME="DCCore")

    def apply(self):
        changes, password_hash, _ = webserver.validate_setup_form(GOOD)
        webserver.apply_setup(changes, password_hash, log=lambda *_: None,
                              settings_path=os.path.join(self.tmp, "settings.conf"),
                              admin_path=os.path.join(self.tmp, "admin_config.py"))

    def test_the_running_process_looks_for_the_list_the_rebuild_will_write(self):
        self.apply()
        self.assertEqual(config.NICKNAME, "MusicBot")
        self.assertEqual(config.LIST_BASE_NAME, "MusicBot")

    def test_it_matches_what_a_fresh_import_derives(self):
        """update_list.py is a new process: it derives from the nickname at import."""
        self.apply()
        self.assertEqual(config.LIST_BASE_NAME, config._sanitize_list_base_name(config.NICKNAME))

    def test_a_list_base_name_in_the_setup_is_not_overridden(self):
        changes, password_hash, _ = webserver.validate_setup_form(GOOD)
        changes = dict(changes, LIST_BASE_NAME="Chosen")
        webserver.apply_setup(changes, password_hash, log=lambda *_: None,
                              settings_path=os.path.join(self.tmp, "settings.conf"),
                              admin_path=os.path.join(self.tmp, "admin_config.py"))
        self.assertEqual(config.LIST_BASE_NAME, "Chosen")


if __name__ == "__main__":
    unittest.main()
