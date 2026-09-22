"""WINDOWS.md's "Did it actually start?" referred to "Step 7" and "step 4"
two sections before those steps were introduced (audit L31, #695).

The section was written under a since-removed "The seven steps" list. Read
top to bottom, a first-timer met "Step 7 prints a lot" right after "The two
steps" (which has steps 1 and 2) and "That is step 4" with no referent - the
list meant is "Before you start", fifty lines further down - and either
hunted through the guide or assumed they had skipped something. The Setup
section's "skipped step 4" had the same problem: its own list has three
steps. The three say what they mean now: the launcher's window, the
optional Flask install (with the command and where the item lives), and
"Flask was never installed".

The guard is the property, not the wording: every "step N" in the guide is
preceded by a numbered item N somewhere above it.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def guide():
    with io.open(os.path.join(REPO_ROOT, "docs", "WINDOWS.md"), encoding="utf-8") as handle:
        return handle.read()


def step_mentions_without_a_referent(text):
    """Every "step N" whose numbered item N has not appeared above it."""
    dangling = []
    for match in re.finditer(r"\b[Ss]tep (\d+)\b", text):
        number = match.group(1)
        before = text[:match.start()]
        if not re.search(r"^\s*%s\. " % re.escape(number), before, re.M):
            line = before.count("\n") + 1
            dangling.append("line %d: %s" % (line, match.group(0)))
    return dangling


class EveryStepHasAReferent(unittest.TestCase):

    def test_no_step_is_named_before_its_list(self):
        self.assertEqual(step_mentions_without_a_referent(guide()), [])

    def test_the_guard_reads_the_old_text_as_dangling(self):
        """The audit's three, on a model of the old order."""
        old = "## Did it actually start?\n\nStep 7 prints a lot.\n\nThat is step 4.\n\n## Before you start\n\n1. x\n4. y\n7. z\n"

        self.assertEqual(step_mentions_without_a_referent(old), ["line 3: Step 7", "line 5: step 4"])

    def test_and_a_step_named_after_its_list_as_fine(self):
        self.assertEqual(step_mentions_without_a_referent("1. a\n2. b\n\nThen step 2.\n"), [])


class TheThreePlacesSayWhatTheyMean(unittest.TestCase):

    def test_did_it_actually_start(self):
        text = guide()
        section = text[text.index("## Did it actually start?"):text.index("## Before you start")]

        self.assertIn("The launcher's window prints a lot.", section)
        self.assertIn("That is the optional Flask install", section)
        self.assertIn("py -3 -m pip install -r requirements-web.txt", section)
        self.assertIn('item 4 under "Before\nyou start", below', section)

    def test_the_setup_sections_start_it(self):
        text = guide()
        section = text[text.index("### 3. Start it"):text.index("## Why there is a launcher at all")]

        self.assertIn("which of them means Flask was never installed", section)
        self.assertNotIn("step 4", section)


if __name__ == "__main__":
    unittest.main()
