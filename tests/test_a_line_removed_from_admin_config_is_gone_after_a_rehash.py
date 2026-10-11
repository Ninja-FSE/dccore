"""A line deleted from admin_config.py stayed live after !rehash.

importlib.reload() re-runs a module's file in the module's own namespace and
never removes a name the new text no longer defines. defaults.py's
`from admin_config import *` then copied that old value straight back over
the fresh default. So an operator who thought the password had leaked,
deleted the ADMIN_PASSWORD_HASH line and ran !rehash still had the old
password opening the console and the dashboard - and the same for an
ADMIN_NICK line, or any other - until a restart.
test_rehash_reloads_admin_config.py covers a line CHANGED; this is a line
REMOVED.

admin_config is now reloaded from scratch (commands.RELOAD_FROM_SCRATCH):
its names are cleared first, so it defines exactly what its file says. A
reload that fails puts them back, so a typo in the file leaves the last good
values, never none.

Driven through the real reload_modules_in_order() with a module pair of the
test's own shaped like the real one - a settings source, and a consumer that
sets its defaults and then does `from source import *` - registered as the
module to reload from scratch. The last class ties it to the real modules.
"""

import contextlib
import importlib
import importlib.util
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

SOURCE = "alfa_scratch_admin_config"
CONSUMER = "alfa_scratch_defaults"


class ModulePair(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir()
        sys.path.insert(0, self.dir)
        self.addCleanup(self._forget)
        self.write(CONSUMER,
                   "ADMIN_PASSWORD_HASH = ''\n"
                   "ADMIN_NICK = ''\n"
                   "ADMIN_HOSTMASKS = []\n"
                   f"from {SOURCE} import *  # noqa: F401,F403\n")
        self.write(SOURCE,
                   "ADMIN_PASSWORD_HASH = 'pbkdf2$old'\n"
                   "ADMIN_NICK = 'alfa,bravo'\n"
                   "ADMIN_HOSTMASKS = ['*!*@bravo.host.example']\n")
        importlib.import_module(SOURCE)
        importlib.import_module(CONSUMER)
        real = commands.RELOAD_FROM_SCRATCH
        commands.RELOAD_FROM_SCRATCH = (SOURCE,)
        self.addCleanup(setattr, commands, "RELOAD_FROM_SCRATCH", real)

    def _forget(self):
        for name in (SOURCE, CONSUMER):
            sys.modules.pop(name, None)
        if self.dir in sys.path:
            sys.path.remove(self.dir)

    def write(self, module, text):
        with io.open(os.path.join(self.dir, module + ".py"), "w", encoding="utf-8") as handle:
            handle.write(text)
        # Two writes inside one timestamp tick would serve the old .pyc.
        importlib.invalidate_caches()
        cached = importlib.util.cache_from_source(os.path.join(self.dir, module + ".py"))
        if os.path.exists(cached):
            os.remove(cached)

    def rehash(self):
        with contextlib.redirect_stdout(io.StringIO()):
            commands.reload_modules_in_order(modules=(SOURCE, CONSUMER), reload_self=False)

    def live(self, name):
        return getattr(sys.modules[CONSUMER], name)


class ARemovedLineIsGone(ModulePair):

    def test_a_deleted_password_hash_is_revoked(self):
        self.write(SOURCE,
                   "ADMIN_NICK = 'alfa,bravo'\n"
                   "ADMIN_HOSTMASKS = ['*!*@bravo.host.example']\n")
        self.rehash()

        self.assertEqual(self.live("ADMIN_PASSWORD_HASH"), "",
                         "a deleted ADMIN_PASSWORD_HASH line still opens the console")
        self.assertFalse(hasattr(sys.modules[SOURCE], "ADMIN_PASSWORD_HASH"))

    def test_a_commented_out_line_is_gone_and_the_rest_stays(self):
        self.write(SOURCE,
                   "ADMIN_PASSWORD_HASH = 'pbkdf2$old'\n"
                   "# ADMIN_NICK = 'alfa,bravo'\n"
                   "ADMIN_HOSTMASKS = ['*!*@bravo.host.example']\n")
        self.rehash()

        self.assertEqual(self.live("ADMIN_NICK"), "")
        self.assertEqual(self.live("ADMIN_PASSWORD_HASH"), "pbkdf2$old")
        self.assertEqual(self.live("ADMIN_HOSTMASKS"), ["*!*@bravo.host.example"])

    def test_a_file_that_fails_to_load_keeps_its_last_good_values(self):
        self.write(SOURCE, "ADMIN_NICK = 'alfa'\nADMIN_HOSTMASKS = (\n")

        with self.assertRaises(SyntaxError):
            self.rehash()

        source = sys.modules[SOURCE]
        self.assertEqual(source.ADMIN_PASSWORD_HASH, "pbkdf2$old")
        self.assertEqual(source.ADMIN_NICK, "alfa,bravo")
        self.assertEqual(source.ADMIN_HOSTMASKS, ["*!*@bravo.host.example"])


class TheRealModules(unittest.TestCase):

    def test_admin_config_is_reloaded_from_scratch(self):
        self.assertIn("admin_config", commands.RELOAD_FROM_SCRATCH)
        self.assertIn("admin_config", commands.CORE_MODULES)

    def test_defaults_is_not(self):
        """defaults.py holds runtime bindings and the last settings read; a
        reload of it must keep its namespace."""
        self.assertNotIn("defaults", commands.RELOAD_FROM_SCRATCH)


if __name__ == "__main__":
    unittest.main()
