"""Every atomic publish goes through the retry wrapper, not bare os.replace().

THE FINDING

db.py grew _replace_with_retry() for a measured Windows failure: os.replace()
raises PermissionError ([WinError 5]) when another handle has the destination
open at the instant of the rename, and under synthetic load 256 of 300 attempts
failed with a reader active throughout (#162 finding #25).

It was private to db.py. Seven other atomic publishes - five in update_list.py,
one in settings_file.py, one in defaults.py - called os.replace() bare and had
exactly the same hazard with none of the handling. So the fix existed, was
documented, was tested, and covered one of the eight places that needed it.

WHY THE LIST PUBLISH IS THE ONE THAT MATTERS

update_list.py's publish is what makes a freshly built master list the live
one. A PermissionError there takes the bot's list off the air until the next
successful rebuild - which is precisely the failure the atomic-publish rewrite
was written to prevent, arriving through the door that rewrite left open.

An antivirus scanner or a search indexer opening a newly written .txt is not an
exotic condition on Windows. It is the default configuration.

HOW THIS IS TESTED

Not by grepping for the call. The wrapper is replaced with a recorder and the
real publish is driven, so a caller that goes back to bare os.replace() fails
here even if the source still mentions the wrapper somewhere.

The retry BEHAVIOUR itself (backoff, bounded attempts, only PermissionError) is
covered by ReplaceWithRetryTests in tests/test_persistence.py, which moved with
the function.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import defaults as config  # noqa: E402
import platform_compat  # noqa: E402
import settings_file  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class RecordingCase(DCCoreTestCase):
    """Swap the wrapper for a recorder that still performs the replace."""

    def setUp(self):
        super().setUp()
        self.replacements = []
        real = platform_compat.replace_with_retry

        def recorder(src, dst, **kwargs):
            self.replacements.append((src, dst))
            return real(src, dst, **kwargs)

        platform_compat.replace_with_retry = recorder
        self.addCleanup(setattr, platform_compat, "replace_with_retry", real)

    def destinations(self):
        return [os.path.basename(dst) for _src, dst in self.replacements]


class TheListPublishRetries(RecordingCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest",
                        LIST_FORMAT="txt")

    def test_building_the_master_list_publishes_through_the_wrapper(self):
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            built = update_list.generate_master_list()

        self.assertTrue(built, buffer.getvalue())

        # Every one of the five, not just "at least one". update_list.py has
        # five publish sites and a mutation run showed that reverting ONE of
        # them left this test green - the other four still recorded, and the
        # assertion was only that something had.
        published = self.destinations()
        master = [n for n in published
                  if n.endswith(".txt") and "-RAR-" not in n and "-FULL-" not in n
                  and not n.startswith("dccore.")]

        self.assertTrue(master, f"the master list: {published}")
        self.assertTrue([n for n in published if "-RAR-" in n],
                        f"the album list: {published}")
        self.assertTrue([n for n in published if "-FULL-" in n],
                        f"the delivered list: {published}")
        self.assertIn("dccore.size.txt", published)
        self.assertIn("dccore.rawbytes.txt", published)


class TheSettingsWriteRetries(RecordingCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "settings.conf")
        self.set_config(NICKNAME="TestBot", CHANNEL="#test",
                        ADMIN_NICK="operator")

    def test_saving_a_setting_publishes_through_the_wrapper(self):
        settings_file.save(vars(config), {"NICKNAME": "Renamed"},
                           path=self.path, log=lambda *a, **k: None)

        self.assertIn("settings.conf", self.destinations())


class TheDatabaseWriteRetries(RecordingCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_an_atomic_write_publishes_through_the_wrapper(self):
        """The one call site that always had it. Pinned so the move out of
        db.py did not quietly drop the module that motivated the wrapper."""
        target = os.path.join(self.tree.root, "state.txt")

        db._atomic_write(target, "contents")

        self.assertIn("state.txt", self.destinations())
        with io.open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "contents")


class TheWrapperIsWhereItBelongs(unittest.TestCase):

    def test_it_is_public(self):
        """Six modules call it. Reaching through another module's underscore
        for a shared helper is what left seven call sites without it."""
        self.assertTrue(hasattr(platform_compat, "replace_with_retry"))
        self.assertFalse(hasattr(db, "_replace_with_retry"),
                         "two names for one function is how they drift apart")

    def test_it_still_replaces(self):
        """Control. A wrapper that never calls os.replace would satisfy every
        recorder assertion above."""
        import tempfile

        directory = tempfile.mkdtemp(prefix="dccore-replace-")
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        src = os.path.join(directory, "src")
        dst = os.path.join(directory, "dst")
        with io.open(src, "w", encoding="utf-8") as handle:
            handle.write("moved")

        platform_compat.replace_with_retry(src, dst)

        self.assertFalse(os.path.exists(src))
        with io.open(dst, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "moved")


if __name__ == "__main__":
    unittest.main()
