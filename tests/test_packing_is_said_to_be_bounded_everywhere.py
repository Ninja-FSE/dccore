"""INSTALL.md's upgrade section said "!rar packing has no size cap",
contradicting MAX_RAR_FOLDER_SIZE and its own earlier paragraph (audit
L32, #696).

The sentence justified the RAR_EXTENSIONS change and predates the cap:
MAX_RAR_FOLDER_SIZE ships at 10 GB, dcc.py enforces it at request time, and
INSTALL.md's own settings paragraph, FUTURE.md, the help text and the
Settings page all say so. defaults.py's comment above RAR_EXTENSIONS made
the same claim. An upgrading operator read that packing was unbounded and
either added a workaround or distrusted the earlier paragraph. Both now
give the real reason - packing a film is pointless work for the receiver,
and the cap alone still lets a 9 GB film through - and the guard reads the
tree for the claim.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults  # noqa: E402

PROSE = ["docs/INSTALL.md", "docs/WINDOWS.md", "docs/MACOS.md", "docs/FUTURE.md", "docs/ADMIN-CONSOLE.md",
         "README.md", "defaults.py", "settings_help.py", "settings.conf.sample", "admin_config.py.sample"]


def read(relative):
    path = os.path.join(REPO_ROOT, relative)
    if not os.path.exists(path):
        return ""
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


class NothingSaysPackingIsUnbounded(unittest.TestCase):

    def test_the_cap_exists(self):
        self.assertEqual(defaults.MAX_RAR_FOLDER_SIZE, 10 * 1024 ** 3)

    def test_no_shipped_prose_claims_there_is_no_cap(self):
        claims = [relative for relative in PROSE
                  if "no size cap" in read(relative).lower() and "at the time there was no size cap" not in read(relative).lower()]

        self.assertEqual(claims, [])

    def test_install_md_gives_the_real_reason_beside_the_cap(self):
        guide = read("docs/INSTALL.md")

        self.assertIn("packing a film folder is pointless work for the receiver", guide)
        self.assertIn("`MAX_RAR_FOLDER_SIZE` (10 GB by default, see above) alone would still let a 9 GB film through", guide)
        # and the earlier paragraph it used to contradict is still there
        self.assertIn("MAX_RAR_FOLDER_SIZE", guide[:guide.index("pointless work for the receiver")])

    def test_defaults_py_dates_its_history(self):
        source = read("defaults.py")

        self.assertIn("At the time there was no size cap on packing at all", source)
        self.assertIn("MAX_RAR_FOLDER_SIZE (below) has bounded it since", source)


if __name__ == "__main__":
    unittest.main()
