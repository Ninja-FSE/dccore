"""The list side files stop being named after one operator's server.

flac-serv-size.txt and flac-serv-rawbytes.txt hold the human-readable total
size and the raw byte count. The channel advert and @<nick>-que read them back,
so they are two of the numbers this bot publishes about itself.

Renaming the settings ALONE would have been the wrong change, and config.py
said so: update_list.py would start writing the new names, list.py would start
reading them, and until the next SUCCESSFUL list rebuild neither file exists.
On a bot whose list is rebuilt weekly that is a week of the advert publishing
"0B" in public, for a cosmetic change.

So the rename comes with a migration, and the migration is deliberately narrow.
A migration that guesses is worse than no migration: it only fires when the
setting still holds the shipped default, only when the new file is not already
there, and it moves rather than copies.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import db  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

LEGACY_SIZE = "flac-serv-size.txt"
LEGACY_RAW = "flac-serv-rawbytes.txt"


class MigrationCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        LIST_SIZE_FILE="dccore.size.txt",
                        LIST_RAWBYTES_FILE="dccore.rawbytes.txt")

    def write(self, name, body):
        with io.open(os.path.join(self.tree.lists, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def read(self, name):
        with io.open(os.path.join(self.tree.lists, name), encoding="utf-8") as handle:
            return handle.read()

    def exists(self, name):
        return os.path.exists(os.path.join(self.tree.lists, name))

    def migrate(self):
        return db.migrate_legacy_side_files(log=lambda message: None)


class AnInstallThatPredatesTheRename(MigrationCase):

    def test_both_files_carry_across(self):
        self.write(LEGACY_SIZE, "1.21TB")
        self.write(LEGACY_RAW, "1330000000000")

        self.migrate()

        self.assertEqual(self.read("dccore.size.txt"), "1.21TB")
        self.assertEqual(self.read("dccore.rawbytes.txt"), "1330000000000")

    def test_the_old_names_are_gone_afterwards(self):
        """Moved, not copied. Two files holding the same figure is how they
        drift apart, and the loser is whichever one nobody remembers reads."""
        self.write(LEGACY_SIZE, "1.21TB")

        self.migrate()

        self.assertFalse(self.exists(LEGACY_SIZE))

    def test_it_reports_what_it_moved(self):
        self.write(LEGACY_SIZE, "1.21TB")

        self.assertEqual(self.migrate(), [(LEGACY_SIZE, "dccore.size.txt")])

    def test_the_advert_can_read_the_figure_afterwards(self):
        """The point of migrating at all. Not "a file exists" - the number the
        bot publishes about itself is still the right number."""
        import list as list_mod
        self.write(LEGACY_SIZE, "1.21TB")
        self.write(LEGACY_RAW, "1330000000000")
        self.write(f"{config.LIST_BASE_NAME}-2026-08-27.txt",
                   "header\n!x Song.flac\n")

        self.migrate()
        _count, _date, size, raw = list_mod.get_file_count_date_size_and_raw_bytes()

        self.assertEqual(size, "1.21TB")
        self.assertEqual(raw, 1330000000000)


class ItRefusesToGuess(MigrationCase):

    def test_a_second_run_moves_nothing(self):
        self.write("dccore.size.txt", "1.21TB")

        self.assertEqual(self.migrate(), [])

    def test_an_existing_new_file_wins_over_an_old_one(self):
        """A rebuild that has already happened is more current than anything
        left on disk from before it."""
        self.write(LEGACY_SIZE, "OLD")
        self.write("dccore.size.txt", "NEW")

        self.migrate()

        self.assertEqual(self.read("dccore.size.txt"), "NEW")

    def test_an_operator_who_chose_their_own_name_is_left_alone(self):
        """Their file is not "the old one", it is theirs. Migrating on top of
        a chosen name would be the migration inventing an intent."""
        self.set_config(LIST_SIZE_FILE="my-own-size.txt")
        self.write(LEGACY_SIZE, "1.21TB")

        self.migrate()

        self.assertTrue(self.exists(LEGACY_SIZE))
        self.assertFalse(self.exists("my-own-size.txt"))

    def test_a_fresh_install_with_neither_file_is_a_no_op(self):
        self.assertEqual(self.migrate(), [])

    def test_it_does_not_raise_when_the_directory_is_missing(self):
        """Startup calls this before anything has checked lists/ exists. A
        daemon that will not start over a cosmetic rename is a worse outcome
        than the rename not happening."""
        self.set_config(LOCAL_LIST_DIR=os.path.join(self.tree.root, "not-there"))

        self.assertEqual(self.migrate(), [])


class TheNameIsGoneFromTheTree(unittest.TestCase):
    """The actual question: do we need it in the code at all. The settings
    are the last place the old name was load-bearing."""

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_shipped_defaults_are_named_after_the_program(self):
        self.assertEqual(config.LIST_SIZE_FILE, "dccore.size.txt")
        self.assertEqual(config.LIST_RAWBYTES_FILE, "dccore.rawbytes.txt")

    def test_the_shipped_debug_channel_is_not_one_operators_own(self):
        """A default is what an install that never touches it joins. Shipping
        "#example-serv" pointed every new bot's debug output at somebody else's
        channel."""
        self.assertNotIn("flac", config.DEBUG_CHANNEL.lower())

    def test_no_module_still_carries_the_old_side_file_names(self):
        """list.py had LIST_FILE_PATH = .../"flac-serv.txt" - defined, read by
        nothing, and the last hardcoded copy of the name.

        db.py is exempt by definition: LEGACY_SIDE_FILES is the one place the
        old names are supposed to appear, because moving them is what it is
        for. config.py is exempt because its comment explains the history.
        """
        names = ("flac-serv-size", "flac-serv-rawbytes", "flac-serv.txt")
        offenders = []
        for name in sorted(os.listdir(REPO_ROOT)):
            if not name.endswith(".py") or name in ("admin_config.py", "defaults.py", "db.py"):
                continue
            for number, line in enumerate(self.source(name).splitlines(), 1):
                if any(old in line for old in names) and not line.lstrip().startswith("#"):
                    offenders.append(f"{name}:{number}: {line.strip()[:60]}")

        self.assertEqual(offenders, [],
                         "the old side-file name is still a literal in: "
                         + ", ".join(offenders))

    def test_the_exemption_is_not_hiding_the_whole_repo(self):
        """Control for the exemption above: db.py really does still name them,
        so a scan that found nothing anywhere would be broken rather than
        reassuring."""
        body = self.source("db.py")

        self.assertIn("flac-serv-size.txt", body)
        self.assertIn("flac-serv-rawbytes.txt", body)

    def test_the_migration_is_wired_into_startup(self):
        """A migration nothing calls is a rename with no migration."""
        calls = [line for line in self.source("oserve.py").splitlines()
                 if "migrate_legacy_side_files" in line
                 and not line.strip().startswith("#")]

        self.assertTrue(calls, "nothing calls the migration, so nobody is migrated")


if __name__ == "__main__":
    unittest.main()
