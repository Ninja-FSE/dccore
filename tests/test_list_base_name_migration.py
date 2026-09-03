"""LIST_BASE_NAME's NICKNAME derivation orphans existing list files.

#184's review: defaults.py's DERIVED VALUES section makes an
untouched LIST_BASE_NAME take NICKNAME's own value once NICKNAME is set
(#170's RFC predicted this, see its own comment). Every install that never
set LIST_BASE_NAME explicitly has its list files on disk as
"DCCore-<date>.*" - generated before that derivation existed - and after it
lands, LIST_BASE_NAME resolves to the operator's nickname instead.

Renaming the setting alone is the wrong change on its own, the same way it
was for LIST_SIZE_FILE/LIST_RAWBYTES_FILE (see test_side_file_migration.py):
find_latest_list() globs for the NEW base name, finds nothing, and the
daemon boots, joins its channels and advertises with no list at all - not
because there is no list, but because the one on disk is filed under a name
nothing is looking for any more. It stays that way until the next
successful !update, which on a weekly rebuild schedule is up to a week of a
bot that looks healthy and answers every request with "not found".

So the derivation comes with a migration, deliberately narrow the same way
db.migrate_legacy_side_files() is: it only fires when LIST_BASE_NAME no
longer equals what defaults.py ships, only a file whose new name does not
already exist is moved, and it moves rather than copies.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class MigrationCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists)

    def write(self, name, body=""):
        with io.open(os.path.join(self.tree.lists, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def exists(self, name):
        return os.path.exists(os.path.join(self.tree.lists, name))

    def migrate(self):
        return update_list.migrate_list_base_name(log=lambda message: None)


class AnInstallThatPredatesTheDerivation(MigrationCase):

    def setUp(self):
        super().setUp()
        self.set_config(LIST_BASE_NAME="DCCoreTest")

    def test_the_master_list_carries_across(self):
        self.write("DCCore-2026-09-01.txt", "List of 1 Files\n!x Song.flac\n")

        self.migrate()

        self.assertTrue(self.exists("DCCoreTest-2026-09-01.txt"))
        self.assertFalse(self.exists("DCCore-2026-09-01.txt"))

    def test_the_rar_variant_carries_across_too(self):
        self.write("DCCore-RAR-2026-09-01.txt", "header\n")

        self.migrate()

        self.assertTrue(self.exists("DCCoreTest-RAR-2026-09-01.txt"))

    def test_a_zip_archive_carries_across(self):
        self.write("DCCore-2026-09-01.zip", "not a real zip, just bytes")

        self.migrate()

        self.assertTrue(self.exists("DCCoreTest-2026-09-01.zip"))

    def test_every_matching_file_moves_in_one_pass(self):
        self.write("DCCore-2026-09-01.txt")
        self.write("DCCore-RAR-2026-09-01.txt")
        self.write("DCCore-2026-09-01.zip")

        moved = self.migrate()

        self.assertEqual(len(moved), 3)
        self.assertFalse(self.exists("DCCore-2026-09-01.txt"))
        self.assertFalse(self.exists("DCCore-RAR-2026-09-01.txt"))
        self.assertFalse(self.exists("DCCore-2026-09-01.zip"))

    def test_it_reports_what_it_moved(self):
        self.write("DCCore-2026-09-01.txt")

        self.assertEqual(
            self.migrate(),
            [("DCCore-2026-09-01.txt", "DCCoreTest-2026-09-01.txt")])

    def test_the_daemon_can_find_the_list_afterwards(self):
        """The point of migrating at all. Not "a file exists" - find_latest_list()
        globbing the CURRENT LIST_BASE_NAME has to actually reach it."""
        import list as list_mod
        self.write("DCCore-2026-09-01.txt", "List of 1 Files\n!x Song.flac\n")

        self.migrate()

        self.assertIsNotNone(list_mod.find_latest_list())

    def test_an_unrelated_file_is_left_alone(self):
        self.write("readme.txt", "not a list")

        self.migrate()

        self.assertTrue(self.exists("readme.txt"))


class ItRefusesToGuess(MigrationCase):

    def test_a_second_run_moves_nothing(self):
        self.set_config(LIST_BASE_NAME="DCCoreTest")
        self.write("DCCoreTest-2026-09-01.txt")

        self.assertEqual(self.migrate(), [])

    def test_an_existing_new_file_wins_over_an_old_one(self):
        """A rebuild that has already happened under the new name is more
        current than anything left on disk from before it."""
        self.set_config(LIST_BASE_NAME="DCCoreTest")
        self.write("DCCore-2026-09-01.txt", "OLD")
        self.write("DCCoreTest-2026-09-01.txt", "NEW")

        self.migrate()

        with io.open(os.path.join(self.tree.lists, "DCCoreTest-2026-09-01.txt"),
                     encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "NEW")
        self.assertTrue(self.exists("DCCore-2026-09-01.txt"),
                        "the untouched old file must survive when the new "
                        "one already exists")

    def test_list_base_name_still_at_the_shipped_default_is_a_no_op(self):
        """The condition this whole migration hinges on: LIST_BASE_NAME must
        actually have changed away from "DCCore" for there to be anything to
        migrate at all."""
        self.set_config(LIST_BASE_NAME="DCCore")
        self.write("DCCore-2026-09-01.txt")

        self.assertEqual(self.migrate(), [])
        self.assertTrue(self.exists("DCCore-2026-09-01.txt"))

    def test_a_fresh_install_with_no_old_files_is_a_no_op(self):
        self.set_config(LIST_BASE_NAME="DCCoreTest")

        self.assertEqual(self.migrate(), [])

    def test_it_does_not_raise_when_the_directory_is_missing(self):
        """Startup calls this before anything has checked lists/ exists. A
        daemon that will not start over a rename is a worse outcome than the
        rename not happening."""
        self.set_config(LIST_BASE_NAME="DCCoreTest",
                        LOCAL_LIST_DIR=os.path.join(self.tree.root, "not-there"))

        self.assertEqual(self.migrate(), [])


class TheMigrationIsWiredIntoStartup(unittest.TestCase):

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_oserve_calls_the_migration(self):
        """A migration nothing calls is a derivation with no migration."""
        calls = [line for line in self.source("oserve.py").splitlines()
                 if "migrate_list_base_name" in line
                 and not line.strip().startswith("#")]

        self.assertTrue(calls, "nothing calls the migration, so nobody is migrated")

    def test_it_runs_before_find_latest_list(self):
        """Order matters: the migration must land the files under their new
        name before anything looks for them there."""
        lines = self.source("oserve.py").splitlines()
        migrate_line = next(i for i, line in enumerate(lines)
                            if "migrate_list_base_name()" in line)
        find_line = next(i for i, line in enumerate(lines)
                         if "list.find_latest_list()" in line)

        self.assertLess(migrate_line, find_line,
                        "migrate_list_base_name() must run before "
                        "list.find_latest_list(), or the list it just moved "
                        "is not found on this same boot")


if __name__ == "__main__":
    unittest.main()
