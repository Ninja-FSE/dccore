"""Two places the multi-list work threaded a list name through some calls and
not their neighbours.

The audit's completeness critic grouped these under one root cause: the `name`
argument reaches most helpers and is dropped at a few, and each drop silently
means "the primary list" rather than failing.

  * `get_file_count_date_size_and_raw_bytes(name)` passes `name` to
    `find_latest_list()` and `all_list_paths()` a few lines above, and calls
    `size_file_path()` and `rawbytes_file_path()` bare. So a channel bound to
    a second list advertised its own file count and list date beside the
    PRIMARY library's size and byte total.

  * `migrate_list_base_name()` only ever looked in `LOCAL_LIST_DIR`. A list's
    files live in its own directory and the marker recording what they are
    called is already per-directory, so renaming the bot orphaned every
    non-primary list's artifacts: they kept the old base name, nothing knew to
    look for it, and those channels advertised a library they no longer had a
    list for.

NOT CHANGED: the side-file migration (since removed)

It has the same primary-only shape and the critic flagged it alongside these,
but the consequence does not follow. The legacy side-file names it migrates
existed only in installs from before that rename - which predates multi-list
entirely, so those installs had exactly one list. A second list's directory is
created afterwards and can only ever hold the new names. Looping it would add
a code path for a state that cannot arise.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TwoListsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.music = os.path.join(self.tree.root, "Music")
        self.films = os.path.join(self.tree.root, "Films")
        for path in (self.music, self.films):
            os.makedirs(path, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="Muzik", NICKNAME="Muzik")
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=(),
                               folders=(library.Folder("Music", self.music),)),
            library.ServedList(name="Films", primary=False, channels=("#films",),
                               folders=(library.Folder("Films", self.films),)),
        ])
        self.films_dir = list_mod.list_dir("Films")
        os.makedirs(self.films_dir, exist_ok=True)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


class EachListAdvertisesItsOwnSize(TwoListsCase):

    def setUp(self):
        super().setUp()
        rows = ("List of files\n\n" + "=" * 40 + "\nD:\\MEDIA\\X\\\n"
                + "=" * 40 + "\n!Muzik One.mp3  ::INFO:: 1.00MB\n")
        self.write(os.path.join(self.tree.lists, "Muzik-2026-09-07.txt"), rows)
        self.write(os.path.join(self.films_dir, "Muzik-2026-09-07.txt"), rows)
        self.write(list_mod.size_file_path(), "1.21TB")
        self.write(list_mod.rawbytes_file_path(), "1330000000000")
        self.write(list_mod.size_file_path("Films"), "8.40TB")
        self.write(list_mod.rawbytes_file_path("Films"), "9240000000000")

    def test_the_second_list_publishes_its_own_size(self):
        _count, _date, size, raw = \
            list_mod.get_file_count_date_size_and_raw_bytes("Films")

        self.assertEqual(size, "8.40TB")
        self.assertEqual(raw, 9240000000000)

    def test_the_primary_still_publishes_its_own(self):
        _count, _date, size, raw = \
            list_mod.get_file_count_date_size_and_raw_bytes("Music")

        self.assertEqual(size, "1.21TB")
        self.assertEqual(raw, 1330000000000)

    def test_no_name_is_still_the_primary(self):
        """The default has to keep meaning what every existing caller relies
        on it meaning."""
        _count, _date, size, _raw = \
            list_mod.get_file_count_date_size_and_raw_bytes()

        self.assertEqual(size, "1.21TB")

    def test_a_missing_side_file_does_not_borrow_the_primary_s(self):
        """The failure that made this hard to see: reading the primary's file
        looks like a plausible answer rather than an error."""
        os.remove(list_mod.size_file_path("Films"))

        _count, _date, size, _raw = \
            list_mod.get_file_count_date_size_and_raw_bytes("Films")

        self.assertEqual(size, "0B")


class RenamingTheBotCarriesEveryList(TwoListsCase):

    def setUp(self):
        super().setUp()
        for directory in (self.tree.lists, self.films_dir):
            self.write(os.path.join(directory, "Muzik-2026-09-07.txt"), "rows")
            self.write(os.path.join(directory, "Muzik-RAR-2026-09-07.txt"), "rows")
            update_list.write_list_base_marker("Muzik", directory,
                                               log=lambda *a, **k: None)
        self.set_config(LIST_BASE_NAME="Muzik2", NICKNAME="Muzik2")

    def migrate(self):
        return update_list.migrate_list_base_name(log=lambda *a, **k: None)

    def test_the_second_list_s_files_are_renamed_too(self):
        self.migrate()

        listed = sorted(os.listdir(self.films_dir))
        self.assertIn("Muzik2-2026-09-07.txt", listed)
        self.assertIn("Muzik2-RAR-2026-09-07.txt", listed)
        self.assertNotIn("Muzik-2026-09-07.txt", listed)

    def test_the_primary_is_still_renamed(self):
        self.migrate()

        listed = sorted(os.listdir(self.tree.lists))
        self.assertIn("Muzik2-2026-09-07.txt", listed)

    def test_every_move_is_reported(self):
        """The return value feeds the startup log; a silent half-migration is
        how this went unnoticed."""
        moved = self.migrate()

        self.assertEqual(len(moved), 4)

    def test_each_directory_gets_its_own_marker(self):
        """The marker says what the files in THAT directory are called. A
        stale one sends the next startup looking for a name nothing has."""
        self.migrate()

        for directory in (self.tree.lists, self.films_dir):
            self.assertEqual(update_list.read_list_base_marker(directory),
                             "Muzik2", directory)

    def test_two_primaries_cannot_produce_a_shared_directory(self):
        """Why there is no de-duplication of directories in the migration.

        list_dir() answers LOCAL_LIST_DIR for any list marked primary, so two
        primaries would share one - but load_lists() normalises a hand-edited
        file down to exactly one primary before anything sees it. A dedup
        guard there could never be reached, so it is not there; this test is
        what keeps that true.
        """
        import json

        with io.open(library.lists_file(), "w", encoding="utf-8") as handle:
            json.dump([
                {"name": "Music", "primary": True, "channels": [],
                 "folders": [{"name": "Music", "path": self.music}]},
                {"name": "Also", "primary": True, "channels": ["#also"],
                 "folders": [{"name": "Music", "path": self.music}]},
            ], handle)

        directories = [list_mod.list_dir(served.name)
                       for served in library.lists()]

        self.assertEqual(len(directories), len(set(directories)))
        self.assertEqual(sum(1 for s in library.lists() if s.primary), 1)

    def test_a_failure_on_one_list_does_not_stop_the_others(self):
        """Same posture as a failure on one file: a daemon that will not start
        over a rename is worse than the rename not happening."""
        real_list_dir = list_mod.list_dir

        def explode_for_films(name=None):
            if name == "Films":
                raise OSError("unreadable")
            return real_list_dir(name)

        list_mod.list_dir = explode_for_films
        self.addCleanup(lambda: setattr(list_mod, "list_dir", real_list_dir))

        moved = self.migrate()

        self.assertTrue(any("Muzik2" in new for _old, new in moved))


if __name__ == "__main__":
    unittest.main()
