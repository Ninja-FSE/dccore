"""local_config.py doesn't follow config.py's rename to defaults.py.

#187's review, found on the real upgrade path: config.py ->
defaults.py is a TRACKED file, so git renames it on every operator's disk
automatically on pull. local_config.py -> admin_config.py is NOT - it is
gitignored, so it was never in the repository for git to rename. An operator
upgrading a real install keeps their old local_config.py, unchanged, sitting
right next to a defaults.py that no longer imports it - so
NICKNAME/CHANNEL/ADMIN_NICK read as blank and oserve.startup()'s REQUIRED
gate refuses to boot, even though every one of those settings is correctly
filled in, one file over.

Same shape as test_side_file_migration.py's coverage of
db.migrate_legacy_side_files() and test_list_base_name_migration.py's
coverage of update_list.migrate_list_base_name(): deliberately narrow, only
fires when the new name is not already there, moves rather than copies.
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402


class MigrationCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dccore-admin-config-migrate-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, body=""):
        with io.open(os.path.join(self.tmp, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def exists(self, name):
        return os.path.exists(os.path.join(self.tmp, name))

    def read(self, name):
        with io.open(os.path.join(self.tmp, name), encoding="utf-8") as handle:
            return handle.read()

    def migrate(self):
        return config._migrate_local_config_to_admin_config(
            directory=self.tmp, log=lambda message: None)


class AnInstallThatPredatesTheRename(MigrationCase):

    def test_local_config_carries_across(self):
        self.write("local_config.py", 'NICKNAME = "MyBot"\n')

        self.assertTrue(self.migrate())

        self.assertEqual(self.read("admin_config.py"), 'NICKNAME = "MyBot"\n')

    def test_the_old_file_is_gone_afterwards(self):
        """Moved, not copied - two files with the same settings is how they
        drift apart, and only one of them is ever read again."""
        self.write("local_config.py", 'NICKNAME = "MyBot"\n')

        self.migrate()

        self.assertFalse(self.exists("local_config.py"))

    def test_it_reports_true_when_it_moved_something(self):
        self.write("local_config.py", 'NICKNAME = "MyBot"\n')

        self.assertTrue(self.migrate())


class ItRefusesToGuess(MigrationCase):

    def test_a_second_run_does_nothing(self):
        self.write("admin_config.py", 'NICKNAME = "MyBot"\n')

        self.assertFalse(self.migrate())

    def test_an_existing_admin_config_wins_over_an_old_local_config(self):
        """An operator who has already started fresh under the new name -
        or already ran configure.py - must not have that overwritten by
        something left over from before."""
        self.write("local_config.py", 'NICKNAME = "OldBot"\n')
        self.write("admin_config.py", 'NICKNAME = "NewBot"\n')

        self.migrate()

        self.assertEqual(self.read("admin_config.py"), 'NICKNAME = "NewBot"\n')
        self.assertTrue(self.exists("local_config.py"),
                        "the untouched old file must survive when the new one already exists")

    def test_a_fresh_install_with_neither_file_is_a_no_op(self):
        self.assertFalse(self.migrate())

    def test_it_does_not_raise_when_the_directory_is_missing(self):
        """A daemon that will not start over a cosmetic rename is a worse
        outcome than the rename not happening."""
        missing = os.path.join(self.tmp, "not-there")

        self.assertFalse(
            config._migrate_local_config_to_admin_config(
                directory=missing, log=lambda message: None))


class ItRunsBeforeTheOverrideImport(unittest.TestCase):
    """The reason this cannot be db.migrate_legacy_side_files()'s shape,
    called from oserve.startup(): by then `from admin_config import *` has
    already run, and the override this migration exists to redirect would
    already have been skipped."""

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_migration_runs_before_the_admin_config_import(self):
        """Matched on the real statement, not a comment or docstring mentioning
        it - a bare substring search found this test's own first commented
        mention of "from admin_config import *" (line 490) before the real
        import statement, and passed vacuously against a migration call
        placed anywhere at all."""
        lines = self.source("defaults.py").splitlines()
        migrate_line = next(
            i for i, line in enumerate(lines)
            if line.strip() == "_migrate_local_config_to_admin_config()")
        import_line = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("from admin_config import"))

        self.assertLess(migrate_line, import_line,
                        "the migration must run before admin_config.py is imported, "
                        "or a real install's settings are already skipped by the time it fires")


class TheGitignoreCoversBothNames(unittest.TestCase):

    def test_both_names_stay_ignored(self):
        with io.open(os.path.join(REPO_ROOT, ".gitignore"), encoding="utf-8") as handle:
            body = handle.read()

        self.assertIn("admin_config.py", body)
        self.assertIn("local_config.py", body)


if __name__ == "__main__":
    unittest.main()
