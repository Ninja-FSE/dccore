"""#623: the setup seeded admin_config.py from the sample, and a line there
that settings.conf overrode was never reported at boot.

A new admin_config.py used to be admin_config.py.sample with the password
filled in. The sample's ACTIVE lines - WEBUI_ENABLED = True, WEBUI_HOST,
WEBUI_PORT, ADMIN_CHAT_MODE = "listen", DEBUG_TO_CHANNEL/CONSOLE - then sat
in the operator's own file from birth. Where the setup had just written the
same name to settings.conf (WEBUI_ENABLED, WEBUI_HOST) they were dead, since
defaults.py applies settings.conf second; where it had not, they silently
diverged from defaults.py (every install ran ADMIN_CHAT_MODE = "listen"
under a comment naming "auto" as the default). And the only shadow check in
the codebase ran at dashboard-save time, so an operator who later followed
the file's own comment ("set this to 0.0.0.0 here") and restarted got a
dashboard that ignored the edit and a console that said nothing about why.

Two changes: a new admin_config.py carries a header and the password line,
nothing else; and settings_file.apply_to() names every setting it applied
that admin_config.py had also set, to a different value.
"""

import ast
import io
import os
import shutil
import sys
import tempfile
import types
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


def _active_assignments(text):
    """{name: value} for every live (uncommented) assignment in `text`."""
    found = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = ast.literal_eval(node.value)
    return found


class ANewAdminConfigCarriesOnlyThePassword(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-seed-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings = os.path.join(self.tmp, "settings.conf")
        self.admin = os.path.join(self.tmp, "admin_config.py")

    def read_admin(self):
        with io.open(self.admin, encoding="utf-8") as handle:
            return handle.read()

    def test_the_terminal_path_seeds_no_live_line_but_the_hash(self):
        configure.write_admin_config_password("HASH1", path=self.admin)

        self.assertEqual(_active_assignments(self.read_admin()),
                         {"ADMIN_PASSWORD_HASH": "HASH1"})

    def test_the_browser_path_seeds_no_live_line_but_the_hash(self):
        """The failure the issue describes: an install that declined the
        dashboard, or chose loopback, must not carry WEBUI_ENABLED = True or
        a WEBUI_HOST line that settings.conf then overrides for ever."""
        form = dict(GOOD)
        form.pop("WEBUI_ENABLED", None)
        changes, password_hash, errors = webserver.validate_setup_form(form)
        self.assertEqual(errors, [])
        self.assertFalse(changes["WEBUI_ENABLED"])
        # apply_setup() writes these into the live config module; put every
        # one back afterwards so SERVER does not leak into the next module.
        restore = list(changes) + ["ADMIN_PASSWORD_HASH", "LIST_BASE_NAME"]
        self.set_config(**{name: getattr(config, name) for name in restore})

        webserver.apply_setup(changes, password_hash, log=lambda *_: None,
                              settings_path=self.settings, admin_path=self.admin)

        self.assertEqual(_active_assignments(self.read_admin()),
                         {"ADMIN_PASSWORD_HASH": password_hash})

    def test_the_new_file_says_where_the_other_settings_went(self):
        """The header is the only documentation the operator's own file now
        carries; it has to point at settings.conf and at the sample."""
        configure.write_admin_config_password("HASH1", path=self.admin)
        text = self.read_admin()
        self.assertIn("settings.conf", text)
        self.assertIn("admin_config.py.sample", text)

    def test_the_sample_itself_is_untouched_documentation(self):
        """Not seeded from any more, but still the file a hand setup copies:
        its own comments and live example lines stay readable."""
        with io.open(os.path.join(REPO_ROOT, "admin_config.py.sample"),
                     encoding="utf-8") as handle:
            sample = handle.read()
        self.assertIn("WEBUI_HOST", _active_assignments(sample))
        self.assertNotIn(configure.NEW_ADMIN_CONFIG_HEADER.splitlines()[0], sample)


class AShadowedLineIsReportedAtBoot(unittest.TestCase):
    """settings_file.apply_to() against a planted admin_config module: the
    module THIS process applied is what sys.modules holds, and that is what
    the report compares against."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dccore-shadow-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "settings.conf")
        self.logged = []
        self.had_module = "admin_config" in sys.modules
        self.real_module = sys.modules.get("admin_config")
        self.addCleanup(self._restore_module)

    def _restore_module(self):
        if self.had_module:
            sys.modules["admin_config"] = self.real_module
        else:
            sys.modules.pop("admin_config", None)

    def plant(self, **values):
        module = types.ModuleType("admin_config")
        for name, value in values.items():
            setattr(module, name, value)
        sys.modules["admin_config"] = module

    def apply(self, text, namespace):
        with io.open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return settings_file.apply_to(namespace, path=self.path,
                                      log=self.logged.append)

    def test_a_different_value_in_admin_config_is_named(self):
        """The issue's scenario: WEBUI_HOST edited to 0.0.0.0 in
        admin_config.py, settings.conf still says 127.0.0.1."""
        self.plant(WEBUI_HOST="0.0.0.0")
        namespace = {"WEBUI_HOST": "0.0.0.0", "WEBUI_PORT": 8420}

        report = self.apply("WEBUI_HOST = 127.0.0.1\n", namespace)

        self.assertEqual(namespace["WEBUI_HOST"], "127.0.0.1",
                         "settings.conf still wins; only the silence changed")
        self.assertEqual(report["shadowed"], [("WEBUI_HOST", "0.0.0.0")])
        line = [text for text in self.logged if "WEBUI_HOST" in text]
        self.assertEqual(len(line), 1, self.logged)
        self.assertIn("admin_config.py", line[0])
        self.assertIn("'0.0.0.0'", line[0])
        self.assertIn("settings.conf", line[0])

    def test_the_same_value_in_both_files_is_not_reported(self):
        """Nothing is lost, so nothing is said - a warning that fires on
        every boot for a harmless line is one nobody reads when it counts."""
        self.plant(WEBUI_HOST="127.0.0.1", WEBUI_ENABLED=True)
        namespace = {"WEBUI_HOST": "127.0.0.1", "WEBUI_ENABLED": True}

        report = self.apply("WEBUI_HOST = 127.0.0.1\nWEBUI_ENABLED = true\n",
                            namespace)

        self.assertEqual(report["shadowed"], [])
        self.assertFalse(any("admin_config.py" in text for text in self.logged),
                         self.logged)

    def test_a_name_only_settings_conf_sets_is_not_reported(self):
        self.plant(ADMIN_PASSWORD_HASH="abc")
        namespace = {"WEBUI_HOST": "127.0.0.1", "ADMIN_PASSWORD_HASH": "abc"}

        report = self.apply("WEBUI_HOST = 0.0.0.0\n", namespace)

        self.assertEqual(report["shadowed"], [])
        self.assertFalse(any("admin_config.py" in text for text in self.logged),
                         self.logged)

    def test_no_admin_config_module_means_nothing_to_compare(self):
        """The common install has no admin_config.py at all; and a process
        that never imported one must not go and import it from disk here."""
        sys.modules.pop("admin_config", None)
        namespace = {"WEBUI_HOST": "127.0.0.1"}

        report = self.apply("WEBUI_HOST = 0.0.0.0\n", namespace)

        self.assertEqual(report["shadowed"], [])
        self.assertNotIn("admin_config", sys.modules)

    def test_a_missing_settings_conf_reports_an_empty_list(self):
        self.plant(WEBUI_HOST="0.0.0.0")
        report = settings_file.apply_to({"WEBUI_HOST": "0.0.0.0"},
                                        path=os.path.join(self.tmp, "nope.conf"),
                                        log=self.logged.append)
        self.assertEqual(report["shadowed"], [])
        self.assertEqual(self.logged, [])


if __name__ == "__main__":
    unittest.main()
