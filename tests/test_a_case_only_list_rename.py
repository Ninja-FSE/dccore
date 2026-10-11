"""#1272: a case-only change of LIST_BASE_NAME left the bot on its old list.

"samplebot" -> "SampleBot" (the operator recapitalised the nick, and the
list name follows it). On NTFS and APFS the new name "exists" because it is
the same file, so the startup migration skipped every file and kept the old
marker; the prune matched names case-sensitively and never removed the old
lists; and find_latest_list() - whose glob ignores case on Windows - took
sorted(...)[-1], where lowercase sorts last. Yesterday's list under the old
spelling won after every rebuild, for @find and the advert alike.

Each case-sensitivity test that needs the real thing probes the filesystem
it runs on and skips where it does not apply - and each is paired with a
fake that runs everywhere, so a case-sensitive CI runner still exercises
the same branch.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

OLD, NEW = "samplebot", "SampleBot"


def touch(path, text="list\n"):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def filesystem_ignores_case(directory):
    """Probed directly, not through the function under test."""
    probe = os.path.join(directory, "CaseProbe.tmp")
    touch(probe)
    try:
        return os.path.exists(os.path.join(directory, "cASEpROBE.TMP"))
    finally:
        os.remove(probe)


class ListsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.lists = self.make_temp_dir(prefix="dccore-case-lists-")
        self.set_config(LOCAL_LIST_DIR=self.lists)

    def names(self):
        return sorted(name for name in os.listdir(self.lists) if not name.startswith("."))

    def quiet(self, *_args, **_kwargs):
        pass


class TheMigrationRenamesTheCase(ListsCase):

    def test_on_this_filesystem_if_it_ignores_case(self):
        if not filesystem_ignores_case(self.lists):
            self.skipTest("case-sensitive filesystem: the fake below covers this branch")
        touch(os.path.join(self.lists, f"{OLD}-2026-10-09.txt"))
        touch(os.path.join(self.lists, f"{OLD}-2026-10-09.zip"))
        update_list.write_list_base_marker(OLD, self.lists, log=self.quiet)
        self.set_config(LIST_BASE_NAME=NEW)

        moved = update_list._migrate_one_list_directory(self.lists, log=self.quiet)

        self.assertEqual(sorted(new for _old, new in moved),
                         [f"{NEW}-2026-10-09.txt", f"{NEW}-2026-10-09.zip"])
        self.assertEqual(self.names(), [f"{NEW}-2026-10-09.txt", f"{NEW}-2026-10-09.zip"])
        self.assertEqual(update_list.read_list_base_marker(self.lists), NEW)

    def test_the_same_file_under_a_new_name_everywhere(self):
        """The fake: a hard link IS what a case-insensitive filesystem shows -
        one file answering to two names - and works on NTFS, ext4 and APFS."""
        old_path = os.path.join(self.lists, "oldname-2026-10-09.txt")
        new_path = os.path.join(self.lists, "newname-2026-10-09.txt")
        touch(old_path, "the old list\n")
        try:
            os.link(old_path, new_path)
        except (OSError, AttributeError, NotImplementedError) as err:
            self.skipTest(f"no hard links on this filesystem ({err})")
        update_list.write_list_base_marker("oldname", self.lists, log=self.quiet)
        self.set_config(LIST_BASE_NAME="newname")

        moved = update_list._migrate_one_list_directory(self.lists, log=self.quiet)

        self.assertEqual(moved, [("oldname-2026-10-09.txt", "newname-2026-10-09.txt")])
        self.assertEqual(self.names(), ["newname-2026-10-09.txt"])
        with open(new_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "the old list\n")
        self.assertEqual(update_list.read_list_base_marker(self.lists), "newname")

    def test_a_different_file_at_the_new_name_still_wins(self):
        """The rule that was right all along: a rebuild already published
        under the new name is not overwritten."""
        touch(os.path.join(self.lists, "oldname-2026-10-09.txt"), "old\n")
        touch(os.path.join(self.lists, "newname-2026-10-09.txt"), "rebuilt\n")
        update_list.write_list_base_marker("oldname", self.lists, log=self.quiet)
        self.set_config(LIST_BASE_NAME="newname")

        moved = update_list._migrate_one_list_directory(self.lists, log=self.quiet)

        self.assertEqual(moved, [])
        with open(os.path.join(self.lists, "newname-2026-10-09.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "rebuilt\n")


class ThePruneMatchesTheFilesystem(ListsCase):

    def setUp(self):
        super().setUp()
        self.set_config(LIST_BASE_NAME=NEW)
        self.today = f"{NEW}-2026-10-10.txt"
        touch(os.path.join(self.lists, self.today))
        touch(os.path.join(self.lists, f"{OLD}-2026-10-09.txt"))
        touch(os.path.join(self.lists, f"{OLD}-2026-10-09.zip"))

    def prune(self):
        update_list._prune_superseded_lists(keep={self.today}, directory=self.lists)

    def test_on_this_filesystem_if_it_ignores_case(self):
        if not filesystem_ignores_case(self.lists):
            self.skipTest("case-sensitive filesystem: the fake below covers this branch")
        self.prune()
        self.assertEqual(self.names(), [self.today])

    def test_old_spellings_go_where_the_filesystem_ignores_case(self):
        """The fake: the probe answers "ignores case" on any filesystem."""
        self.patch_probe(True)
        self.prune()
        self.assertEqual(self.names(), [self.today])

    def test_a_case_sensitive_filesystem_keeps_another_spelling(self):
        """There the two spellings really are different names."""
        self.patch_probe(False)
        self.prune()
        self.assertEqual(self.names(), [self.today, f"{OLD}-2026-10-09.txt",
                                        f"{OLD}-2026-10-09.zip"])

    def test_side_files_survive_either_way(self):
        side = os.path.join(self.lists, os.path.basename(config.LIST_SIZE_FILE))
        touch(side, "12345\n")
        self.patch_probe(True)
        self.prune()
        self.assertTrue(os.path.exists(side))

    def patch_probe(self, answer):
        self.addCleanup(setattr, platform_compat, "ignores_case", platform_compat.ignores_case)
        platform_compat.ignores_case = lambda directory: answer


class TheProbe(unittest.TestCase):

    def test_it_agrees_with_a_direct_probe(self):
        home = tempfile.mkdtemp(prefix="dccore-case-probe-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        touch(os.path.join(home, "Sample-List.txt"))
        expected = os.path.exists(os.path.join(home, "sAMPLE-lIST.TXT"))
        self.assertEqual(platform_compat.ignores_case(home), expected)

    def test_a_directory_with_nothing_to_try_is_case_sensitive(self):
        home = tempfile.mkdtemp(prefix="dccore-case-probe-")
        self.addCleanup(os.rmdir, home)
        self.assertFalse(platform_compat.ignores_case(home))


class TheNewestListIsTheNewestDate(ListsCase):

    def test_by_date_not_by_code_point(self):
        paths = [os.path.join("lists", f"{NEW}-2026-10-10.txt"),
                 os.path.join("lists", f"{OLD}-2026-10-09.txt")]
        self.assertEqual(sorted(paths)[-1], paths[1], "the old rule picked the old spelling")
        self.assertEqual(list_mod.newest_by_date(paths, f"{NEW}-"), paths[0])

    def test_an_undated_name_ranks_last(self):
        paths = [f"{NEW}-2026-10-10.txt", f"{NEW}-zzz.txt"]
        self.assertEqual(list_mod.newest_by_date(paths, f"{NEW}-"), paths[0])

    def test_find_latest_list_on_disk(self):
        """On Windows the glob matches both spellings; everywhere the newest
        date must win."""
        self.set_config(LIST_BASE_NAME=NEW)
        touch(os.path.join(self.lists, f"{NEW}-2026-10-10.txt"))
        touch(os.path.join(self.lists, f"{OLD}-2026-10-09.txt"))
        found = list_mod.find_latest_list()
        self.assertEqual(os.path.basename(found), f"{NEW}-2026-10-10.txt")

    def test_the_film_list_too(self):
        marker = list_mod.VIDEO_LIST_MARKER
        paths = [f"{NEW}-{marker}-2026-10-10.txt", f"{OLD}-{marker}-2026-10-09.txt"]
        self.assertEqual(list_mod.newest_by_date(paths, f"{NEW}-{marker}-"), paths[0])


if __name__ == "__main__":
    unittest.main()
