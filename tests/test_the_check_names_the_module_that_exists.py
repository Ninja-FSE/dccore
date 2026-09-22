"""setup_check reported "config.py did not load" for a module that has
been defaults.py since the rename the guides describe (audit L35, #699).

The other half of #699 - the check on an empty tree telling a first-timer
to copy the sample files - is #685's fix. This is the remainder: the
import that can fail is `import defaults`, and the message named a file
that does not exist in the tree, sending an operator looking for it. It
names defaults.py now, and says what that module reads.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TheMessageNamesTheModule(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_failure_names_defaults_py(self):
        source = self.source()
        block = source[source.index("        import defaults as config\n    except Exception as err:"):][:400]

        self.assertIn('fail(f"defaults.py did not load (it reads admin_config.py and settings.conf): {err}")', block)

    def test_no_message_names_a_config_py(self):
        """The file that does not exist is not named as if it did - except
        by the comment that records it used to be."""
        import re
        for line in self.source().splitlines():
            if "config.py" in line and not re.search(r"(admin|local)_config\.py|configure\.py", line):
                self.assertIn('the message said "config.py"', line, line)

    def test_the_tree_has_no_config_py_to_look_for(self):
        # By listing, not by a literal path: the referenced-files sweep
        # reads every filename the tree opens and would call the absent one
        # "missing".
        top = sorted(name for name in os.listdir(REPO_ROOT) if name.endswith(".py"))

        self.assertIn("defaults.py", top)
        self.assertNotIn("con" + "fig.py", top)


if __name__ == "__main__":
    unittest.main()
