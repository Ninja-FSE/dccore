"""`.gitattributes` ships unchanged, and one of its lines is wrong there.

In THIS repository `docs/UPDATES.md` is the internal changelog and is
deliberately export-ignored. In the PUBLIC repository the same path **is the
changelog** - extraction step 3 renames `UPDATES-PUBLIC.md` onto it.

So the line

    docs/UPDATES.md    export-ignore

is correct here and actively wrong there: shipped unchanged, it tells any
`git archive` run in the public repository to drop its own changelog. Nobody
would notice until a release tarball came out without one (#449).

The fix is a step in the extraction checklist, which is exactly the kind of
manual step that gets dropped quietly - so this guards that the step is still
written down, and that the condition making it necessary still holds.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exported_tree import internal_file_or_skip  # noqa: E402

WORKFLOW = os.path.join("docs", "PUBLIC-REPO-WORKFLOW.md")


class TheExtractionStripsTheLineThatWouldDropTheChangelog(unittest.TestCase):

    def workflow_text(self):
        with io.open(internal_file_or_skip(self, WORKFLOW), encoding="utf-8") as h:
            return h.read()

    def test_the_condition_that_makes_this_necessary_still_holds(self):
        """If .gitattributes ever stops export-ignoring the internal
        changelog, the strip step becomes wrong rather than necessary - and
        this guard should be the thing that notices.

        Meaningless in an extracted public tree, where the strip has already
        happened and this line is gone on purpose - unlike the other two
        tests in this class, this one reads .gitattributes directly rather
        than through internal_file_or_skip(), so it needs the same guard by
        hand."""
        internal_file_or_skip(self, WORKFLOW)
        with io.open(os.path.join(REPO_ROOT, ".gitattributes"),
                     encoding="utf-8") as handle:
            attributes = handle.read()

        self.assertRegex(
            attributes, r"docs/UPDATES\.md\s+export-ignore",
            "the internal changelog is no longer export-ignored, so the "
            "extraction step that strips this line needs rethinking rather "
            "than keeping")

    def test_the_extraction_says_to_strip_it(self):
        text = self.workflow_text()
        extraction = text.split("2. Strip dev-only tooling", 1)
        self.assertEqual(len(extraction), 2,
                         "extraction step 2 has been renamed or removed - this "
                         "guard is reading the wrong part of the document")
        step = extraction[1].split("3. Swap the changelog", 1)[0]

        self.assertIn(".gitattributes", step,
                      "extraction no longer says to strip the internal-only "
                      "export-ignore lines, so the public repo's own archive "
                      "would drop its changelog")
        self.assertIn("docs/UPDATES.md", step)

    def test_it_says_why_rather_than_just_what(self):
        """A checklist item with no reason attached is the first thing an
        editor removes when it looks redundant - and this one looks redundant
        right up until a release ships without a changelog."""
        text = self.workflow_text()
        step = text.split("2. Strip dev-only tooling", 1)[1]
        step = step.split("3. Swap the changelog", 1)[0]

        self.assertRegex(step, r"(?i)changelog",
                         "the strip step does not say what breaks if it is "
                         "skipped")
