"""admin_config.py.sample disagreed with defaults.py on two live lines (audit
M34, #636).

INSTALL.md and WINDOWS.md both say "copy admin_config.py.sample to
admin_config.py and fill it in", and the sample's live lines are what that
install then runs with. Two of them were not the defaults: ADMIN_CHAT_MODE =
"listen" sat under a comment naming "auto" the default and "right for most
setups", so a hand-made install never dialled the operator's client and the
console guide's table sent them debugging a choice they never made; and
WEBUI_ENABLED = True opened a listener defaults.py keeps off, from a file the
docs describe as opt-in. (#623 stopped the SETUP seeding from the sample; the
hand-copy path still read it.)

Every live line in the sample now carries defaults.py's own value, and this
keeps it that way.
"""

import ast
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def live_assignments(path):
    """{name: value} for every uncommented `NAME = <literal>` in `path`."""
    with io.open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = {}
    for node in tree.body:
        targets = getattr(node, "targets", None) or ([node.target] if hasattr(node, "target") else [])
        value = getattr(node, "value", None)
        if len(targets) == 1 and isinstance(targets[0], ast.Name) and value is not None:
            try:
                found[targets[0].id] = ast.literal_eval(value)
            except ValueError:
                continue
    return found


class TheSampleCarriesTheDefaults(unittest.TestCase):

    def setUp(self):
        self.sample = live_assignments("admin_config.py.sample")
        self.defaults = live_assignments("defaults.py")

    def test_the_console_dials_by_default_as_the_comment_above_it_says(self):
        self.assertEqual(self.sample["ADMIN_CHAT_MODE"], "auto")
        self.assertEqual(self.sample["ADMIN_CHAT_MODE"], self.defaults["ADMIN_CHAT_MODE"])

    def test_the_dashboard_is_opt_in_as_the_docs_say(self):
        self.assertIs(self.sample["WEBUI_ENABLED"], False)
        self.assertEqual(self.sample["WEBUI_ENABLED"], self.defaults["WEBUI_ENABLED"])

    def test_every_live_line_that_defaults_py_also_has_agrees_with_it(self):
        """The general rule the two above are instances of. The password hash
        and the hostmask list are the sample's own placeholders and have no
        default worth the name; everything else a copied sample sets must be
        what an untouched install already runs with."""
        placeholders = {"ADMIN_PASSWORD_HASH", "ADMIN_HOSTMASKS"}
        differing = {name: (value, self.defaults[name])
                     for name, value in self.sample.items()
                     if name in self.defaults and name not in placeholders
                     and value != self.defaults[name]}

        self.assertEqual(differing, {}, "sample line != defaults.py: {sample, default}")

    def test_the_sweep_saw_the_live_lines(self):
        """Guard on the guard: an AST walk that found nothing would pass."""
        self.assertGreaterEqual(len(self.sample), 8, sorted(self.sample))
        self.assertIn("WEBUI_HOST", self.sample)


if __name__ == "__main__":
    unittest.main()
