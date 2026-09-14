"""The master index has a grammar, and three things wrote into it without one.

Found by the full-program audit.

1. THE PRUNE STEP ATE THE SIDE FILES IT HAD JUST WRITTEN.

   _prune_superseded_lists() matched artifacts with a bare
   `item.startswith(config.LIST_BASE_NAME)`. Every generated list is actually
   f"{LIST_BASE_NAME}-{today}.txt", so the separator is always there - but the
   side files (dccore.size.txt, dccore.rawbytes.txt) live in the same
   directory, and with LIST_BASE_NAME derived from a nickname like "dccore" or
   "dcc" they matched too.

   So every rebuild wrote the size files and then deleted them. The library's
   total size disappeared from every public surface permanently: the advert
   published "Files (0B)" and the CTCP SLOTS payload published 0 raw bytes, on
   every interval, for ever. The log line was "[LIST-CLEAN] Removed 2
   superseded list(s)", which reads like housekeeping working correctly.

2. A BANNER LINE OF ONLY "=" SHIFTED EVERY FOLDER HEADING BY ONE.

   The operator's banner is written into the body of the index, and a line
   that is entirely "=" is a folder RULE to every reader. An odd number of
   them leaves find_matching_entries()'s rule/heading/rule machine mid-block,
   so the next banner line is taken as a folder heading and the REAL heading
   after it is swallowed. Measured: a file in "D:\\MUSIC\\Flac\\Artist\\" was
   reported as living in "MY BOT - EST 2009".

   That value is not cosmetic. build_verify_list_payload() resolves it to tell
   the operator where duplicate names live, and the File Lists view groups by
   it.

3. A BANNER LINE BEGINNING WITH "!" WAS SERVED AS A FILE.

   A request row is identified, everywhere in the project, as "the line starts
   with !". A banner line like "!!! NEW RELEASES WEEKLY !!!" was counted in the
   advert's file count and in the CTCP SLOTS payload, and @find returned it as
   a genuine match - so a user could request it and receive nothing.

WHY THE FIX IS IN read_operator_header()

_split_master_list() removes the banner from each split part by matching the
exact text read_operator_header() returned. Neutralising anywhere else would
leave the two sides looking at different text, and the banner would survive
into every part after the first.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

QUIET = lambda *_a, **_k: None  # noqa: E731


class ThePruneStepKeepsWhatItJustWrote(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.lists = os.path.join(self.make_tree().root, "lists")
        os.makedirs(self.lists, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.lists,
                        LIST_SIZE_FILE="dccore.size.txt",
                        LIST_RAWBYTES_FILE="dccore.rawbytes.txt")

    def touch(self, *names):
        for name in names:
            with io.open(os.path.join(self.lists, name), "w",
                         encoding="utf-8") as handle:
                handle.write("x")

    def remaining(self):
        return sorted(os.listdir(self.lists))

    def test_the_side_files_survive_the_base_name_that_ate_them(self):
        """"dccore.size.txt".startswith("dccore") is True, and that was the
        whole test."""
        self.set_config(LIST_BASE_NAME="dccore")
        self.touch("dccore.size.txt", "dccore.rawbytes.txt",
                   "dccore-2026-09-01.txt")

        update_list._prune_superseded_lists(keep=set())

        self.assertIn("dccore.size.txt", self.remaining())
        self.assertIn("dccore.rawbytes.txt", self.remaining())

    def test_a_shorter_base_name_does_not_reach_them_either(self):
        self.set_config(LIST_BASE_NAME="dcc")
        self.touch("dccore.size.txt", "dccore.rawbytes.txt")

        update_list._prune_superseded_lists(keep=set())

        self.assertEqual(self.remaining(),
                         ["dccore.rawbytes.txt", "dccore.size.txt"])

    def test_superseded_lists_are_still_removed(self):
        """Control - the prune must still do its job."""
        self.set_config(LIST_BASE_NAME="dccore")
        self.touch("dccore-2026-09-01.txt", "dccore-2026-09-05.txt",
                   "dccore-RAR-2026-09-05.txt")

        update_list._prune_superseded_lists(
            keep={"dccore-2026-09-05.txt", "dccore-RAR-2026-09-05.txt"})

        self.assertNotIn("dccore-2026-09-01.txt", self.remaining())
        self.assertIn("dccore-2026-09-05.txt", self.remaining())

    def test_the_separator_is_what_narrows_the_match(self):
        """A file that begins with the base name but is not a generated list -
        the side files are only the case that bit. Nothing else protects this
        one, so it pins the hyphen specifically."""
        self.set_config(LIST_BASE_NAME="dccore")
        self.touch("dccore_backup_before_upgrade.txt")

        update_list._prune_superseded_lists(keep=set())

        self.assertIn("dccore_backup_before_upgrade.txt", self.remaining())

    def test_a_side_file_named_like_a_list_is_still_protected(self):
        """The side-file names are SETTINGS. An operator who sets
        LIST_SIZE_FILE to something that does match base + "-" needs the
        explicit exclusion, which is the only thing covering this."""
        self.set_config(LIST_BASE_NAME="dccore",
                        LIST_SIZE_FILE="dccore-size.txt",
                        LIST_RAWBYTES_FILE="dccore-rawbytes.txt")
        self.touch("dccore-size.txt", "dccore-rawbytes.txt")

        update_list._prune_superseded_lists(keep=set())

        self.assertEqual(self.remaining(),
                         ["dccore-rawbytes.txt", "dccore-size.txt"])

    def test_an_unrelated_file_is_left_alone(self):
        self.set_config(LIST_BASE_NAME="dccore")
        self.touch("unrelated.txt", "notes.zip")

        update_list._prune_superseded_lists(keep=set())

        self.assertEqual(self.remaining(), ["notes.zip", "unrelated.txt"])


class TheBannerCannotBeReadAsStructure(DCCoreTestCase):

    def banner(self, text):
        path = os.path.join(self.make_tree().root, "list_header.txt")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        self.set_config(LIST_HEADER_FILE=path)
        return update_list.read_operator_header(log=QUIET)

    def test_a_rule_line_is_rewritten_to_the_same_width(self):
        """The box still looks like a box - the operator's art survives, it
        just stops being grammar."""
        out = self.banner("========\nMY BOT\n========\n")

        self.assertEqual(out.split("\n")[0], "-" * 8)
        self.assertEqual(out.split("\n")[2], "-" * 8)

    def test_a_row_line_is_dropped(self):
        """No edit keeps such a line looking like itself while stopping it
        being read as a row, so it goes."""
        out = self.banner("Welcome\n!!! NEW RELEASES !!!\nEnjoy\n")

        self.assertEqual(out.split("\n"), ["Welcome", "Enjoy"])

    def test_an_indented_row_line_is_dropped_too(self):
        """Every reader strips before testing, so leading space is no
        defence."""
        out = self.banner("Welcome\n    !bot something\nEnjoy\n")

        self.assertNotIn("!bot", out)

    def test_an_ordinary_banner_is_untouched(self):
        """The overwhelmingly common case must pass through exactly."""
        text = "Welcome to my bot\n  ~ FLAC since 2009 ~\nAsk in #channel\n"
        out = self.banner(text)

        self.assertEqual(out, text.rstrip("\n"))

    def test_a_line_merely_containing_equals_is_untouched(self):
        out = self.banner("Ratio = 1:3\n")

        self.assertEqual(out, "Ratio = 1:3")

    def test_a_line_of_dashes_is_untouched(self):
        """Only "=" is grammar. A banner already drawn in "-" was always
        safe and must stay unchanged."""
        out = self.banner("--------\nMY BOT\n--------\n")

        self.assertEqual(out.split("\n")[0], "-" * 8)

    def test_no_banner_at_all(self):
        self.set_config(LIST_HEADER_FILE=os.path.join(
            self.make_tree().root, "absent.txt"))

        self.assertEqual(update_list.read_operator_header(log=QUIET), "")


class ThePublishedListParsesBackCorrectly(DCCoreTestCase):
    """The end-to-end property: whatever the operator wrote, the index that
    goes out is read the way it was meant."""

    def build(self, banner_text):
        root = self.make_tree().root
        header = os.path.join(root, "list_header.txt")
        with io.open(header, "w", encoding="utf-8", newline="") as handle:
            handle.write(banner_text)
        self.set_config(LIST_HEADER_FILE=header)

        banner = update_list.read_operator_header(log=QUIET)
        path = os.path.join(root, "list.txt")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(banner + "\n"
                         "=====\n"
                         "D:\\MUSIC\\Flac\\Artist\\\n"
                         "=====\n"
                         "!bot Real Song.flac 10M\n")
        return path

    def test_the_real_folder_heading_survives_a_rule_shaped_banner(self):
        path = self.build("========================\n   MY BOT - EST 2009\n")

        entries, total = list_mod.find_matching_entries([], list_path=path)

        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["folder"], "D:\\MUSIC\\Flac\\Artist\\")

    def test_a_banner_line_is_never_a_search_hit(self):
        path = self.build("!!! NEW RELEASES WEEKLY !!!\n")

        _hits, count = list_mod.find_matching_entries(["releases"],
                                                      list_path=path)

        self.assertEqual(count, 0)

    def test_the_file_count_counts_only_files(self):
        path = self.build("!!! NEW RELEASES !!!\n=========\nMY BOT\n=========\n")

        _entries, total = list_mod.find_matching_entries([], list_path=path)

        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
