"""The setup check's library verdict must be the daemon's own.

scripts/setup_check.py used to read FILE_DIRECTORY and nothing else. That
setting is only the fallback for an install with no folder list, so an operator
who had configured folders on the dashboard - twenty of them, across five
drives - and left FILE_DIRECTORY blank was told at every start:

    WARN   FILE_DIRECTORY is not set yet - the daemon will start, but cannot
           search or serve anything until it is set ...

which was simply untrue. oserve.py's own startup check received exactly this
correction (its comment above `configured = library.folders()` tells the
story); the setup check kept the old rule, and it is the thing the operator
reads first.

These call library_report() directly with a fake config and recording
reporters, so every branch is exercised on every platform. A child process
against the developer's own checkout - the only way the check was tested
before - could reach one branch: whichever the checkout happened to be in.
"""

import io
import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for entry in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts"), os.path.join(REPO_ROOT, "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import defaults as config  # noqa: E402
import setup_check  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class Recorder:
    """ok/warn/fail/detail as the check hands them to library_report(), captured."""

    def __init__(self):
        self.ok, self.warn, self.fail, self.detail = [], [], [], []

    def reporters(self):
        return self.ok.append, self.warn.append, self.fail.append, self.detail.append

    @property
    def all_text(self):
        return "\n".join(self.ok + self.warn + self.fail + self.detail)


class LibraryReportBase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.folders_json = os.path.join(self.tree.root, "library_folders.json")
        config.LIBRARY_FOLDERS_FILE = self.folders_json
        config.LISTS_FILE = os.path.join(self.tree.root, "lists.json")
        config.FILE_DIRECTORY = ""
        self.rec = Recorder()

    def folder(self, name):
        path = os.path.join(self.tree.root, name)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "track.flac"), "wb") as handle:
            handle.write(b"\x00" * 64)
        return path

    def write_folders(self, entries):
        with open(self.folders_json, "w", encoding="utf-8") as handle:
            json.dump([{"name": n, "path": p} for n, p in entries], handle)

    def run_report(self):
        setup_check.library_report(config, *self.rec.reporters())
        return self.rec


class NothingConfiguredYet(LibraryReportBase):
    """Not chosen is not misconfigured: a WARN, and the daemon still boots."""

    def test_no_folder_file_and_no_file_directory_is_a_warning(self):
        rec = self.run_report()
        self.assertEqual(rec.fail, [], "an unmade choice must not refuse to start")
        self.assertEqual(len(rec.warn), 1)
        self.assertIn("configured yet", rec.warn[0])

    def test_the_warning_points_at_the_library_page_not_only_file_directory(self):
        """Folders are added on the dashboard now; the old text only knew one way."""
        rec = self.run_report()
        self.assertIn("Library page", rec.warn[0])


class ConfiguredFoldersAreWhatIsChecked(LibraryReportBase):
    """The bug. FILE_DIRECTORY blank, folders configured: the check must say so."""

    def test_folders_with_a_blank_file_directory_are_not_a_warning(self):
        self.write_folders([("A", self.folder("a")), ("B", self.folder("b"))])
        rec = self.run_report()
        self.assertEqual(rec.warn, [],
                         "twenty configured folders were reported as 'cannot serve anything'")
        self.assertEqual(rec.fail, [])
        self.assertTrue(any("2 folder(s)" in line for line in rec.ok), rec.ok)

    def test_the_report_names_the_folder_file_as_the_source(self):
        self.write_folders([("A", self.folder("a")), ("B", self.folder("b"))])
        rec = self.run_report()
        self.assertIn("library_folders.json", rec.all_text)

    def test_every_folder_is_listed_by_name_and_path(self):
        a, b = self.folder("a"), self.folder("b")
        self.write_folders([("Scene", a), ("Sorted", b)])
        rec = self.run_report()
        for name, path in (("Scene", a), ("Sorted", b)):
            with self.subTest(name=name):
                self.assertTrue(any(f"{name} -> {path}" in line for line in rec.detail),
                                rec.detail)

    def test_the_file_count_covers_every_folder_not_just_the_first(self):
        self.write_folders([("A", self.folder("a")), ("B", self.folder("b")),
                            ("C", self.folder("c"))])
        rec = self.run_report()
        self.assertTrue(any(re.search(r"\b3 file\(s\) would be listed", line)
                            for line in rec.ok), rec.ok)

    def test_a_stale_file_directory_does_not_override_the_folder_list(self):
        """The daemon's other complaint: a FILE_DIRECTORY nothing reads any more
        used to be able to stop the bot starting. Same rule here."""
        self.write_folders([("A", self.folder("a"))])
        config.FILE_DIRECTORY = os.path.join(self.tree.root, "gone-drive")
        rec = self.run_report()
        self.assertEqual(rec.fail, [], "the folder list is the truth, not the fallback")
        self.assertEqual(rec.warn, [])


class TheSingleFolderPathStillWorks(LibraryReportBase):
    """An install with only FILE_DIRECTORY set must read exactly as before."""

    def test_a_good_file_directory_is_ok(self):
        config.FILE_DIRECTORY = self.folder("music")
        rec = self.run_report()
        self.assertEqual(rec.warn, [])
        self.assertEqual(rec.fail, [])
        self.assertTrue(any("music directory" in line for line in rec.ok), rec.ok)

    def test_a_missing_file_directory_is_a_failure(self):
        """Set but wrong is a real misconfiguration; the daemon exits on it."""
        config.FILE_DIRECTORY = os.path.join(self.tree.root, "not-there")
        rec = self.run_report()
        self.assertEqual(len(rec.fail), 1, rec.fail)
        self.assertIn("FILE_DIRECTORY does not exist", rec.fail[0],
                      "the single-folder path keeps its original, more specific wording")


class MissingFoldersFollowTheDaemonsRule(LibraryReportBase):
    """Every one missing refuses; some missing warns. Never the other way round."""

    def test_all_folders_missing_is_a_failure(self):
        self.write_folders([("A", os.path.join(self.tree.root, "x")),
                            ("B", os.path.join(self.tree.root, "y"))])
        rec = self.run_report()
        self.assertEqual(len(rec.fail), 1, rec.fail)
        self.assertIn("none of the configured", rec.fail[0])

    def test_one_folder_missing_is_a_warning_not_a_failure(self):
        """A single unavailable drive is a scan-time condition the build skips.
        Refusing to start for it would be stricter than the daemon itself."""
        self.write_folders([("A", self.folder("a")),
                            ("B", os.path.join(self.tree.root, "unplugged"))])
        rec = self.run_report()
        self.assertEqual(rec.fail, [], "the daemon starts with one drive gone; so must the check")
        self.assertEqual(len(rec.warn), 1, rec.warn)
        self.assertIn("1 of 2", rec.warn[0])

    def test_the_missing_folder_is_marked_in_the_listing(self):
        gone = os.path.join(self.tree.root, "unplugged")
        self.write_folders([("A", self.folder("a")), ("Gone", gone)])
        rec = self.run_report()
        self.assertIn(f"MISSING  Gone -> {gone}", rec.detail)


class MainUsesItAndNothingElse(unittest.TestCase):
    """main() must reach the library through library_report, not its own copy."""

    def setUp(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"),
                     encoding="utf-8") as handle:
            self.source = handle.read()
        self.main_body = self.source[self.source.index("def main(platform):"):]

    def test_main_calls_the_report(self):
        self.assertIn("library_report(config, ok, warn, fail, detail)", self.main_body)

    def test_main_no_longer_reads_file_directory_for_the_verdict(self):
        """The statement that WAS the bug, not merely its identifier."""
        self.assertNotIn('music = getattr(config, "FILE_DIRECTORY", "")', self.main_body)
        self.assertNotIn("FILE_DIRECTORY is not set yet", self.source)

    def test_the_report_asks_the_library_module(self):
        body = self.source[self.source.index("def library_report("):
                           self.source.index("def main(platform):")]
        self.assertIn("library.folders()", body)

    def test_the_report_prints_nothing_itself(self):
        """It sits above main()'s console-encoding guard in the file. A print()
        of its own would be the first print in the file, ahead of the guard -
        which is exactly what tests/test_a_filename_your_code_page_cannot_spell.py
        forbids, and it did trip on the first draft of this function."""
        body = self.source[self.source.index("def library_report("):
                           self.source.index("def main(platform):")]
        statements = re.findall(r"^\s*print\(", body, re.MULTILINE)
        self.assertEqual(statements, [],
                         "a print statement, not the word in a docstring, is what the "
                         "guard-ordering test measures")


if __name__ == "__main__":
    unittest.main()
