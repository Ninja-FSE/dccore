"""Stale phrasing and module names were left in operator-facing text
(audit L37, #701).

Remnants of earlier phases and of the config.py rename sat where a novice
reads them: ADMIN-CONSOLE.md's "Until phase 2 flips the switch" (the
switch is ADMIN_CHANNEL_COMMANDS; no phase numbering exists in the doc),
a dangling "Phase 4" bullet duplicating the prose above it, an example
banner from v1.10.0-RC1; "produced from config.py" in the sample and its
generator, "Every data path in config.py" in both launchers; the Windows
launcher telling a Windows user to type `python oserve.py` where every
guide says `py`; requirements.txt pointing at docs/README.md, which is at
the root; and FUTURE.md filing the finished multi-list work under
"Planned", right under the sentence that says everything there is not
working. Each is fixed; the guard reads for the class, not the instance.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults  # noqa: E402


def read(relative):
    with io.open(os.path.join(REPO_ROOT, relative), encoding="utf-8", errors="replace") as handle:
        return handle.read()


class TheConsoleGuide(unittest.TestCase):

    def test_no_phase_numbers(self):
        self.assertIsNone(re.search(r"\b[Pp]hase \d\b", read("docs/ADMIN-CONSOLE.md")))

    def test_the_lockout_names_the_switch(self):
        self.assertIn("While `ADMIN_CHANNEL_COMMANDS` is on (it\nships on), the channel commands still work",
                      read("docs/ADMIN-CONSOLE.md"))

    def test_the_example_banner_is_this_tree_s_version(self):
        version = defaults.SCRIPT_VERSION.split("v", 1)[1]
        self.assertIn("DCCore v%s - platform=posix" % version, read("docs/ADMIN-CONSOLE.md"))


class NothingNamesConfigPy(unittest.TestCase):
    """The module has been defaults.py since the rename; a bare config.py
    in operator-facing text sends a reader to a file that is not there."""

    FILES = ["settings.conf.sample", "scripts/gen_settings_sample.py",
             "scripts/linux/start-dccore.sh", "scripts/windows/start-dccore.bat",
             "docs/INSTALL.md", "docs/WINDOWS.md", "docs/ADMIN-CONSOLE.md", "README.md"]

    def test_no_bare_config_py(self):
        """Except where the rename itself is being described - a heading
        that says `config.py` became `defaults.py` is history, not a
        pointer."""
        bare = re.compile(r"(?<![\w./-])config\.py\b")
        hits = {}
        for relative in self.FILES:
            text = read(relative)
            for match in bare.finditer(text):
                context = text[match.start():match.start() + 40]
                if "defaults.py` rename" in context or "-> defaults.py" in context:
                    continue
                hits.setdefault(relative, []).append(text.count("\n", 0, match.start()) + 1)

        self.assertEqual(hits, {})


class TheWindowsLauncherSaysPy(unittest.TestCase):

    def test_the_first_run_hint_uses_py(self):
        bat = read("scripts/windows/start-dccore.bat")

        self.assertIn("echo       py oserve.py", bat)
        self.assertNotIn("python oserve.py", bat)


class RequirementsPointsAtTheReadme(unittest.TestCase):

    def test_the_readme_it_names_exists(self):
        text = read("requirements.txt")
        self.assertIn("See README.md.", text)
        self.assertNotIn("docs/README.md", text)
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "README.md")))


class TheRoadmapFilesDoneWorkAsDone(unittest.TestCase):

    def test_multi_list_is_under_implemented(self):
        roadmap = read("docs/FUTURE.md")
        implemented = roadmap.index("## Implemented")
        planned = roadmap.index("## Planned")
        section = roadmap.index("### Multiple lists, and multiple folders per list")

        self.assertLess(implemented, section)
        self.assertLess(section, planned)
        self.assertIn("**Stage 5 is in, and #26 is complete.**", roadmap[section:planned])

    def test_nothing_under_planned_says_it_is_in(self):
        """The rule the file states for itself: everything under Planned is
        not working. A stage reported "in" there contradicts it."""
        roadmap = read("docs/FUTURE.md")
        planned = roadmap[roadmap.index("## Planned"):roadmap.index("## Not planned")]

        self.assertIsNone(re.search(r"\*\*Stage \d is in", planned))
        self.assertNotIn("is complete.**", planned)


if __name__ == "__main__":
    unittest.main()
