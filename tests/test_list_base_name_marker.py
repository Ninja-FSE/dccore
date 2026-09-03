"""Renaming the bot carries its list artifacts across, every time.

THE DEFECT (#213)

`migrate_list_base_name()` could only move files across from the shipped
default, because that literal was the only "old" name it knew:

    old_prefix = _SHIPPED_LIST_BASE_NAME + "-"      # "DCCore-"

So it worked exactly once. Rename the bot a second time - or from any name that
was never "DCCore" - and the artifacts on disk keep the old name while the bot
looks for the new one. A restart does not recover it. The list is on disk,
complete and invisible, and the advert publishes 0 files.

WHERE THE PREVIOUS NAME IS REMEMBERED

A marker file in the lists directory, not settings.conf or admin_config.py.

The config can be hand-edited, replaced wholesale on an upgrade, or restored
from a backup taken before the rename - and each of those silently orphans the
lists again, which is the exact failure being closed. A file in the directory
cannot drift from the directory it describes, because it is in it.

Absent means an install from before the marker existed, which is precisely when
falling back to the shipped default is the right guess - so the old behaviour
is what an un-migrated install still gets.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class MarkerCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists)

    def artifact(self, base, suffix="-2026-01-01.txt", body="List of 3 Files\n"):
        path = os.path.join(self.tree.lists, f"{base}{suffix}")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return os.path.basename(path)

    def names(self):
        return sorted(f for f in os.listdir(self.tree.lists)
                      if not f.startswith("."))

    def migrate(self, to_name):
        self.set_config(LIST_BASE_NAME=to_name)
        return update_list.migrate_list_base_name(log=lambda *a, **k: None)


class TheSecondRenameAlsoWorks(MarkerCase):
    """The defect, stated as the sequence that reproduces it."""

    def test_a_rename_from_the_shipped_default_still_works(self):
        """Control: the behaviour that already existed must be unchanged."""
        self.artifact("DCCore")

        self.migrate("FirstName")

        self.assertEqual(self.names(), ["FirstName-2026-01-01.txt"])

    def test_and_then_a_second_rename_works_too(self):
        """This is the one that used to fail. After the first rename the files
        are called FirstName-*, which the old code had no way to know."""
        self.artifact("DCCore")
        self.migrate("FirstName")

        self.migrate("SecondName")

        self.assertEqual(self.names(), ["SecondName-2026-01-01.txt"])

    def test_a_third_rename_works_as_well(self):
        """Not a one-off improvement from one to two."""
        self.artifact("DCCore")
        for name in ("One", "Two", "Three"):
            self.migrate(name)

        self.assertEqual(self.names(), ["Three-2026-01-01.txt"])

    def test_a_rename_from_a_name_that_was_never_the_default(self):
        """An install whose LIST_BASE_NAME has never been "DCCore" at any
        point - the old code could not migrate these at all."""
        update_list.write_list_base_marker("Custom", self.tree.lists,
                                           log=lambda *a, **k: None)
        self.artifact("Custom")

        self.migrate("Renamed")

        self.assertEqual(self.names(), ["Renamed-2026-01-01.txt"])

    def test_every_artifact_kind_moves_together(self):
        """The master list, the album list and the download artifact. Moving
        only some of them is how the advert and @find disagree."""
        self.artifact("DCCore")
        self.artifact("DCCore", suffix="-RAR-2026-01-01.txt")
        self.artifact("DCCore", suffix="-2026-01-01.zip", body="PK")
        self.migrate("FirstName")

        self.migrate("SecondName")

        self.assertEqual(self.names(), ["SecondName-2026-01-01.txt",
                                        "SecondName-2026-01-01.zip",
                                        "SecondName-RAR-2026-01-01.txt"])

    def test_the_list_is_findable_afterwards(self):
        """What the operator actually cares about: the advert stops saying 0."""
        self.artifact("DCCore")
        self.migrate("FirstName")
        self.migrate("SecondName")

        found = list_mod.find_latest_list()

        self.assertIsNotNone(found, "the list is on disk but invisible")
        self.assertIn("SecondName", os.path.basename(found))


class TheMarkerItself(MarkerCase):

    def test_it_is_absent_before_anything_writes_it(self):
        self.assertIsNone(update_list.read_list_base_marker(self.tree.lists))

    def test_it_round_trips(self):
        update_list.write_list_base_marker("Whatever", self.tree.lists,
                                           log=lambda *a, **k: None)

        self.assertEqual(update_list.read_list_base_marker(self.tree.lists),
                         "Whatever")

    def test_a_migration_records_the_new_name(self):
        self.artifact("DCCore")

        self.migrate("FirstName")

        self.assertEqual(update_list.read_list_base_marker(self.tree.lists),
                         "FirstName")

    def test_an_install_that_was_never_renamed_still_gets_a_marker(self):
        """So its FIRST rename migrates from the right name rather than from
        the shipped guess - which matters for an install whose name has always
        been something else."""
        self.set_config(LIST_BASE_NAME="AlwaysThis")

        update_list.migrate_list_base_name(log=lambda *a, **k: None)

        self.assertEqual(update_list.read_list_base_marker(self.tree.lists),
                         "AlwaysThis")

    def test_an_unreadable_marker_falls_back_rather_than_raising(self):
        """Startup calls this. A marker that cannot be read must degrade to the
        old guess, never take the boot down."""
        path = update_list.list_base_marker_path(self.tree.lists)
        os.makedirs(path, exist_ok=True)   # a directory where a file should be

        self.assertIsNone(update_list.read_list_base_marker(self.tree.lists))

    def test_it_is_not_mistaken_for_a_list_artifact(self):
        """A leading dot and no .txt/.zip/.rar suffix, so every artifact scan
        and the glob in find_latest_list() step over it."""
        self.set_config(LIST_BASE_NAME="Bot")
        update_list.write_list_base_marker("Bot", self.tree.lists,
                                           log=lambda *a, **k: None)

        self.assertIsNone(list_mod.find_latest_list())
        self.assertIsNone(list_mod.find_latest_list_file())


class ItSurvivesAFailedRename(MarkerCase):

    def test_the_marker_is_not_advanced_when_nothing_moved(self):
        """If the renames fail, the marker must still name what is actually on
        disk - otherwise the next startup looks for files under a name nothing
        has, and the recovery path is gone too."""
        self.artifact("DCCore")
        update_list.write_list_base_marker("DCCore", self.tree.lists,
                                           log=lambda *a, **k: None)

        # Only the ARTIFACT renames fail. The first version of this failed
        # every replace_with_retry, which also broke write_list_base_marker
        # (db._atomic_write goes through the same helper) - so the marker stayed
        # put for the wrong reason and the test passed against a mutant that
        # advanced it unconditionally.
        real = update_list.platform_compat.replace_with_retry
        marker_name = os.path.basename(
            update_list.list_base_marker_path(self.tree.lists))

        def fail_artifact_renames(src, dst, **kwargs):
            if os.path.basename(dst) == marker_name:
                return real(src, dst, **kwargs)
            raise OSError("denied")

        update_list.platform_compat.replace_with_retry = fail_artifact_renames
        self.addCleanup(setattr, update_list.platform_compat,
                        "replace_with_retry", real)

        self.migrate("NewName")

        self.assertEqual(update_list.read_list_base_marker(self.tree.lists),
                         "DCCore", "the marker moved on without the files")
        self.assertEqual(self.names(), ["DCCore-2026-01-01.txt"])


class APublishRecordsTheName(MarkerCase):
    """generate_master_list() writes the marker too, not just the migration.

    Without it a rename BETWEEN two rebuilds is migrated from whatever name the
    last migration recorded, which by then is stale - the rebuild has already
    republished everything under the current name.
    """

    def setUp(self):
        super().setUp()
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="Publisher", NICKNAME="Publisher",
                        LIST_FORMAT="txt")

    def build(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            return update_list.generate_master_list()

    def test_a_successful_build_records_the_current_name(self):
        self.assertTrue(self.build())

        self.assertEqual(update_list.read_list_base_marker(self.tree.lists),
                         "Publisher")

    def test_a_rename_after_a_build_migrates_from_the_built_name(self):
        """The sequence the marker exists for: build, rename, restart."""
        self.assertTrue(self.build())

        self.migrate("Renamed")

        self.assertTrue(any(f.startswith("Renamed-") for f in self.names()),
                        f"artifacts were not carried across: {self.names()}")
        self.assertFalse(any(f.startswith("Publisher-") for f in self.names()))


if __name__ == "__main__":
    unittest.main()
