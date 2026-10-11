"""#1272: a multi-list install refused to boot when the PRIMARY list's
folders were unavailable, although another list's folders were fine.

startup() and the setup check both asked library.folders(), which is the
primary list alone. With the primary's drive unplugged at boot - or a
mapped drive not reconnected yet at logon - the daemon exited with "None of
the configured music folders exist" and the launcher refused too, while the
second list sat there readable and its channel went unserved. The rule the
code's own comment states is to refuse only a library with NOTHING readable.
"""

import contextlib
import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for entry in (REPO_ROOT, os.path.join(REPO_ROOT, "scripts"), os.path.join(REPO_ROOT, "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import defaults as config  # noqa: E402
import library  # noqa: E402
import setup_check  # noqa: E402

from tests.test_startup import BootCase  # noqa: E402
from tests.test_the_setup_check_asks_the_library import Recorder  # noqa: E402


class TwoLists:
    """lists.json with a primary "Music" list and a second "Films" list."""

    def write_lists(self, music, films):
        entries = [
            {"name": "Music", "primary": True, "channels": ["#example-music"],
             "folders": [{"name": "Music", "path": music}]},
            {"name": "Films", "primary": False, "channels": ["#example-films"],
             "folders": [{"name": "Films", "path": films}]},
        ]
        with open(library.lists_file(), "w", encoding="utf-8") as handle:
            json.dump(entries, handle)

    def folder(self, name):
        path = os.path.join(self.tree.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def gone(self, name):
        return os.path.join(self.tree.root, "unplugged-drive", name)


class TheDaemonStarts(TwoLists, BootCase):

    def setUp(self):
        super().setUp()
        config.FILE_DIRECTORY = ""

    def test_an_unplugged_primary_does_not_stop_the_other_list(self):
        self.write_lists(self.gone("Music"), self.folder("Films"))
        output = self.boot()        # must not raise SystemExit
        self.assertNotIn("[CRITICAL] None of the configured music folders exist", output)
        self.assertIn(config.SCRIPT_VERSION, output)

    def test_the_list_with_nothing_readable_is_named(self):
        self.write_lists(self.gone("Music"), self.folder("Films"))
        output = self.boot()
        self.assertIn("[WARNING] None of list 'Music'", output)
        self.assertIn("#example-music", output)
        self.assertNotIn("list 'Films'", output)

    def test_every_list_gone_still_refuses(self):
        self.write_lists(self.gone("Music"), self.gone("Films"))
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(io.StringIO()):
            self.oserve.startup()
        self.assertEqual(caught.exception.code, 1)


class TheSetupCheckAgrees(TwoLists, BootCase):

    def setUp(self):
        super().setUp()
        config.FILE_DIRECTORY = ""
        self.rec = Recorder()

    def run_report(self):
        setup_check.library_report(config, *self.rec.reporters())
        return self.rec

    def test_an_unplugged_primary_is_a_warning_not_a_failure(self):
        self.write_lists(self.gone("Music"), self.folder("Films"))
        rec = self.run_report()
        self.assertEqual(rec.fail, [], "the launcher would refuse a bot that can serve #example-films")
        self.assertTrue(any("list 'Music'" in line for line in rec.warn), rec.warn)

    def test_both_lists_folders_are_listed(self):
        films = self.folder("Films")
        self.write_lists(self.gone("Music"), films)
        rec = self.run_report()
        self.assertTrue(any(f"Films -> {films}" in line for line in rec.detail), rec.detail)
        self.assertIn("lists.json", rec.all_text)

    def test_every_list_gone_is_still_a_failure(self):
        self.write_lists(self.gone("Music"), self.gone("Films"))
        rec = self.run_report()
        self.assertEqual(len(rec.fail), 1, rec.fail)


if __name__ == "__main__":
    unittest.main()
