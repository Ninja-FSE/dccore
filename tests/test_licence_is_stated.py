"""A public repository has to say what its licence is, and who holds copyright.

Without a LICENSE file the copyright default is all-rights-reserved: the code
would be readable but nobody could legally run, modify or redistribute it,
which defeats the point of publishing. #194 added GPLv3.

Two things it did not add, and this file pins both:

  * a copyright notice for DCCore itself. LICENSE carries the FSF's copyright,
    which covers the licence TEXT, not the software. Without a line naming the
    holder, a downstream user cannot tell who to attribute, and nobody can tell
    who would have to agree to a future relicence. GPLv3's own "How to Apply
    These Terms" appendix - already at the bottom of LICENSE - asks for exactly
    this.
  * an accurate summary. The README said "forks and modified versions must stay
    open source". Only DISTRIBUTED ones must; an operator who modifies DCCore
    and runs it privately owes nothing, which is most of this audience most of
    the time. Overstating a licence is the kind of error that gets quoted back.

Not a style check. A licence file that is deleted, truncated, or swapped for a
different one is a real event, and nothing else in the suite would notice.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class TheLicenceFileIsIntact(unittest.TestCase):

    def test_it_exists(self):
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "LICENSE")),
                        "no LICENSE file: the repo would be all-rights-reserved")

    def test_it_is_the_gpl_version_3(self):
        head = read("LICENSE")[:400]

        self.assertIn("GNU GENERAL PUBLIC LICENSE", head)
        self.assertIn("Version 3", head)

    def test_it_is_the_whole_text_and_not_a_summary(self):
        """A truncated licence is worse than none: it looks authoritative and
        grants nothing. Every operative section has to be present."""
        body = read("LICENSE")

        for section in ("TERMS AND CONDITIONS", "0. Definitions",
                        "15. Disclaimer of Warranty", "16. Limitation of Liability",
                        "17. Interpretation", "END OF TERMS AND CONDITIONS"):
            with self.subTest(section=section):
                self.assertIn(section, body)

    def test_nothing_project_specific_was_spliced_into_it(self):
        """The licence text is the FSF's and must be verbatim. A project's own
        copyright goes alongside it, never inside it.

        Whole words only. The first version of this test looked for the
        substring "irc" and failed on the FSF's own "circumvention" - a
        reminder that a substring check finds things nobody put there."""
        body = read("LICENSE").lower()

        for word in ("dccore", "undernet", "omenserve", "irc"):
            with self.subTest(word=word):
                self.assertIsNone(re.search(r"\b" + word + r"\b", body),
                                  f"{word!r} was spliced into the licence text")


class TheReadmeStatesTheLicenceAndTheCopyright(unittest.TestCase):

    def section(self):
        body = read("README.md")
        start = body.index("## License")
        return body[start:]

    def test_the_readme_points_at_the_licence(self):
        self.assertIn("LICENSE", self.section())

    def test_a_copyright_holder_is_named(self):
        """LICENSE's own copyright line is the FSF's and covers the licence
        text. Nothing there says who holds copyright in DCCore."""
        self.assertRegex(self.section(), r"Copyright \(C\) \d{4} ")

    def test_the_holder_is_not_an_individual_identity(self):
        """Consistent with the rest of the pre-publication work: the published
        code does not tie itself to one person's handle or nick."""
        section = self.section().lower()

        for handle in ("chchatzop", "ninja-fse", "flac", "samoth"):
            with self.subTest(handle=handle):
                self.assertNotIn(handle, section)

    def test_the_summary_does_not_overstate_the_licence(self):
        """GPLv3's obligations attach to DISTRIBUTION, not to use. Saying that
        any modified version must be open-sourced is wrong, and wrong in the
        direction that would put people off."""
        section = self.section()

        claim = re.search(r"forks and modified versions must stay open source", section)
        if claim:
            before = section[max(0, claim.start() - 120):claim.start()].lower()

            self.assertIn("distributed", before,
                          "the summary claims every modified version must be "
                          "open-sourced; only distributed ones must")

    def test_the_warranty_disclaimer_is_carried_across(self):
        """GPLv3's appendix asks for it alongside the grant, and it is the part
        an operator running a file server actually wants to have said."""
        self.assertIn("WITHOUT ANY", self.section())


if __name__ == "__main__":
    unittest.main()
