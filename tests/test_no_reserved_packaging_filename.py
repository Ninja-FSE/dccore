"""Nothing at the repository root occupies a filename pip treats as packaging.

WHAT WENT WRONG

DCCore's guided first-run wizard was called setup.py. That is Python's reserved
packaging filename: `pip install .` and `pip install -e .` execute it as a
build script. On a public repository, a contributor who types the most obvious
command in the ecosystem would have had an interactive configuration wizard
start prompting them for a nickname and an IRC server, in the middle of what
they expected to be an install.

Renamed to configure.py, which also describes it better - the README says it is
safe to run again later, so "setup" as in first-run was never the whole story.

WHY NOT ADD A pyproject.toml INSTEAD

That would make `pip install .` do something rather than fail. But DCCore is not
a package: it is a daemon you clone and run in place, with no third-party
dependencies and no importable public API. Shipping packaging metadata would
advertise an installation path nobody maintains. `pip install .` failing with
"no pyproject.toml or setup.py" is the accurate answer.
"""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files pip and setuptools treat as a build definition at the root of a source
# tree. setup.cfg is included because it is read WITH the others rather than
# executed alone, so an accidental one is still a packaging claim.
RESERVED = ("setup.py", "setup.cfg", "pyproject.toml")


class TheRootHasNoPackagingFiles(unittest.TestCase):

    def test_no_reserved_filename_is_taken(self):
        for name in RESERVED:
            with self.subTest(filename=name):
                self.assertFalse(
                    os.path.exists(os.path.join(REPO_ROOT, name)),
                    f"{name} at the repository root makes `pip install .` do "
                    f"something. If that is intended, this test should change "
                    f"deliberately rather than by a file appearing.")

    def test_the_wizard_is_still_there_under_its_own_name(self):
        """The rename must not have simply deleted it."""
        self.assertTrue(os.path.exists(os.path.join(REPO_ROOT, "configure.py")))

    def test_it_is_still_importable_and_runnable(self):
        """A hyphen in the name would have been the other obvious choice and
        would have made it unimportable, taking its own tests with it."""
        import configure

        self.assertTrue(hasattr(configure, "main"))


class TheDocsPointAtTheRightFile(unittest.TestCase):
    """A rename that leaves the instructions behind is worse than no rename:
    the reader runs a command that does not exist."""

    DOCS = ("README.md", "docs/INSTALL.md", "docs/WINDOWS.md",
            "docs/ADMIN-CONSOLE.md", "docs/FUTURE.md")

    def read(self, name):
        import io
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_no_document_still_tells_the_reader_to_run_setup_py(self):
        for name in self.DOCS:
            with self.subTest(document=name):
                self.assertNotIn("setup.py", self.read(name),
                                 "this document names a file that no longer exists")

    def test_the_install_guide_names_the_wizard(self):
        """Control: a rename that removed the instruction entirely would pass
        the test above."""
        self.assertIn("configure.py", self.read("docs/INSTALL.md"))


if __name__ == "__main__":
    unittest.main()
