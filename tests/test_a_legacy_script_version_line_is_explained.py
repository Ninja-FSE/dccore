"""A legacy SCRIPT_VERSION line in settings.conf was reported as a
misspelled unknown setting (audit L24, #688).

An older dashboard's Settings page offered SCRIPT_VERSION and wrote it into
settings.conf; nothing ever removed the line. NOT_SETTINGS rightly keeps
the name out of the overridable set, so the line is ignored - but the
explanation table (RUNTIME_ASSIGNED, #465) covered only MY_IP_OR_DOCK and
ORIGINAL_NICK, so every boot and every !rehash said "not a setting this
version recognises. Check the spelling against settings.conf.sample" of a
name DCCore itself had written, spelled perfectly. The table now covers it:
"the code's own version, which an older Settings page wrote here; it is no
longer configurable. Delete this line."
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402


class AnUpgradedInstall(unittest.TestCase):

    def apply(self, text):
        path = os.path.join(tempfile.mkdtemp(prefix="dccore-legacy-"), "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        said = []
        namespace = {"SCRIPT_VERSION": "DCCore v9.9.9", "DEBUG_CHANNEL": ""}
        report = settings_file.apply_to(namespace, path=path, log=said.append)
        return namespace, report, said

    def test_the_line_is_still_ignored(self):
        """The control: NOT_SETTINGS still keeps the version the code's."""
        namespace, report, _said = self.apply("SCRIPT_VERSION = DCCore v1.0\nDEBUG_CHANNEL = #x\n")

        self.assertEqual(namespace["SCRIPT_VERSION"], "DCCore v9.9.9")
        self.assertIn("SCRIPT_VERSION", report["unknown"])
        self.assertEqual(namespace["DEBUG_CHANNEL"], "#x")

    def test_and_the_operator_is_told_what_it_is_and_what_to_do(self):
        _ns, _report, said = self.apply("SCRIPT_VERSION = DCCore v1.0\n")
        line = [l for l in said if "'SCRIPT_VERSION'" in l]

        self.assertEqual(len(line), 1, said)
        self.assertIn("the code's own version, which an older Settings page wrote here", line[0])
        self.assertIn("Delete this line", line[0])
        self.assertNotIn("Check the spelling", line[0])

    def test_a_real_misspelling_is_still_sent_to_the_sample(self):
        """The audit's contrast, the other way round: a name nobody wrote
        for the operator still gets the spelling hint."""
        _ns, _report, said = self.apply("DEBUG_CHANEL = #x\n")

        self.assertTrue(any("'DEBUG_CHANEL'" in l and "Check the spelling" in l for l in said), said)


class TheTableCoversTheNonSetting(unittest.TestCase):

    def test_every_name_in_not_settings_has_an_explanation(self):
        """NOT_SETTINGS grows by one line; without this, its next member
        would get the same wrong advice."""
        for name in settings_file.NOT_SETTINGS:
            self.assertIn(name, settings_file.RUNTIME_ASSIGNED, name)


if __name__ == "__main__":
    unittest.main()
