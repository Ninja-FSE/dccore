"""An mxrarserver list is named, chosen and read the way its bot meant (#1209).

Three things about the lists an mxrarserver bot sends, which arrive as
"<name>-Files(<x>)-MX.txt" and "<name>-Folders(<x>)-MX.txt", alone or
together in one "Complete" archive:

  * THE MARKER. A list in an archive is kept under a short, stable marker,
    so that the next fetch replaces it rather than adding a second one. The
    "(<x>)" changes from one build to the next and "-MX" only says who built
    it; both stayed in, so "Folders(56)-MX" became a new list on every fetch.
    It is now "Folders", and "Files".

  * THE PACK LIST. Its Folders list is the list of folders it packs into a
    RAR on request - what DCCore's "-RAR-" list is. In a Complete archive the
    Files list is now the main one, as DCCore's music list is beside its RAR
    list, and a Folders list held, main or not, says the bot packs - which
    decides how long a folder request is waited for.

  * THE BANNER. Its list opens with its operator's banner - blocks of text
    between "=" rules - and its rows have no folder headings. The parser
    took the banner's last block for a heading and filed every row in the
    list under it: "> Overview", or the bot's own name. In an mxrarserver
    list nothing above the first row is a heading. Every other list is read
    as before: DCCore's own put a summary line under each heading, so "text
    after a heading" could not be the test, and the list's name is.

Every nick, trigger, path and banner line here is invented.
"""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "someserver"
TRIGGER = "SomeTrigger"
RULE = "=" * 57

# The shape of mxrarserver's banner: a title block, a subtitle block, a
# credit block and a long overview block, each between rules, then the rows.
BANNER = "\n".join([
    RULE, "                  some server", "=" * 43,
    "A file and folder server for a channel", "=" * 43, "",
    "                   Run by: SomeOperator", "                 October 2026", "",
    RULE, "> Overview", "> Files and folders, sent on request.", "",
    "> Requests", "> Type the line exactly as shown.", "", RULE, "",
])

FILE_ROWS = "\n".join([
    f"!{TRIGGER} Some Track.mp3 ::INFO:: 4.5 MB",
    f"!{TRIGGER} Other Track.flac ::INFO:: 30.1 MB",
]) + "\n"

FOLDER_ROWS = "\n".join([
    f"!{TRIGGER} E:\\Music\\Some Artist\\Some Album.rar",
    f"!{TRIGGER} E:\\Music\\Some Artist\\Other Album\\CD1.rar",
]) + "\n"


class TheMarker(unittest.TestCase):

    def markers(self, names):
        stems = [os.path.splitext(n)[0] for n in names]
        prefix = list_fetch._shared_list_prefix(stems)
        return [list_fetch.list_marker(n, prefix) for n in names]

    def test_files_and_folders(self):
        self.assertEqual(self.markers(["SomeServer-Files(1,234)-MX.txt",
                                       "SomeServer-Folders(56)-MX.txt"]),
                         ["Files", "Folders"])

    def test_the_same_list_from_another_build_is_the_same_list(self):
        self.assertEqual(self.markers(["SomeServer-Files(1,234)-MX.txt",
                                       "SomeServer-Folders(56)-MX.txt"]),
                         self.markers(["SomeServer-Files(9,999)-MX.txt",
                                       "SomeServer-Folders(77)-MX.txt"]))

    def test_without_a_parenthesis(self):
        self.assertEqual(list_fetch.list_marker("SomeServer-Folders-MX.txt", "SomeServer-"),
                         "Folders")

    def test_mx_inside_a_word_stays(self):
        self.assertEqual(list_fetch.list_marker("SomeBot-TOPMX.txt", "SomeBot-"), "TOPMX")

    def test_the_other_conventions_are_unchanged(self):
        self.assertEqual(self.markers(["SomeBot-Default(2026-01-02)-OS.txt",
                                       "SomeBot-rar(2026-01-02)-OS.txt"]), ["Default", "rar"])
        self.assertEqual(self.markers(["SomeBot-2026-09-07.txt", "SomeBot-RAR-2026-09-07.txt"]),
                         ["", "RAR"])


class TheArchive(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-mxlist-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp

    def fetch(self, members):
        path = os.path.join(self.tmp, "incoming.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in members:
                archive.writestr(name, text)
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)
        return config.fetched_bot_lists[BOT]

    def test_a_complete_archive_keeps_files_as_the_main_list(self):
        # The Folders list is the bigger file here, which is what decided the
        # main list before.
        entry = self.fetch([
            ("SomeServer-Files(2)-MX.txt", BANNER + FILE_ROWS),
            ("SomeServer-Folders(40)-MX.txt", BANNER + FOLDER_ROWS * 20),
        ])

        self.assertEqual(entry["lists"][""]["file_name"], "SomeServer-Files(2)-MX.txt")
        self.assertEqual(sorted(entry["lists"]), ["", "Folders"])
        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_folders_only_archive_says_it_packs(self):
        entry = self.fetch([("SomeServer-Folders(2)-MX.txt", BANNER + FOLDER_ROWS)])

        self.assertEqual(list(entry["lists"]), [""])
        self.assertTrue(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_a_files_only_archive_does_not(self):
        self.fetch([("SomeServer-Files(2)-MX.txt", BANNER + FILE_ROWS)])

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))

    def test_another_bot_s_folders_list_is_not_taken_for_one(self):
        """Only mxrarserver's own name says it: a "Folders" list from
        anyone else is whatever that bot meant by it."""
        self.fetch([("SomeServer-2026-09-07.txt", FILE_ROWS),
                    ("SomeServer-Folders-2026-09-07.txt", FILE_ROWS)])

        self.assertFalse(list_fetch.bot_publishes_a_rar_list(BOT))


class TheBanner(unittest.TestCase):

    def write(self, name, text):
        folder = tempfile.mkdtemp(prefix="dccore-banner-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.addCleanup(list_mod.forget_folder_tables)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def folders(self, path):
        return [row["folder"] for row in list_mod.iter_filelist_rows(path, BOT)]

    def test_no_row_is_filed_under_the_banner(self):
        path = self.write("SomeServer-Files(2)-MX.txt", BANNER + FILE_ROWS)

        self.assertEqual(self.folders(path), ["", ""])

    def test_nor_under_a_three_line_one(self):
        path = self.write("SomeServer-MX.txt",
                          f"{RULE}\n  mxrarserver v2.1.5 - some server\n{RULE}\n\n" + FOLDER_ROWS)

        self.assertEqual(self.folders(path), ["", ""])

    def test_a_search_says_the_same(self):
        path = self.write("SomeServer-Files(2)-MX.txt", BANNER + FILE_ROWS)

        found, total = list_mod.find_matching_entries(["track"], list_path=path)

        self.assertEqual(total, 2)
        self.assertEqual([entry["folder"] for entry in found], [None, None])

    def test_a_heading_after_the_first_row_is_still_one(self):
        path = self.write("SomeServer-Files(4)-MX.txt",
                          BANNER + FILE_ROWS + f"{RULE}\nE:\\Music\\Later\n{RULE}\n" + FILE_ROWS)

        self.assertEqual(self.folders(path), ["", "", "E:\\Music\\Later", "E:\\Music\\Later"])

    def test_the_folder_table_reads_it_the_same(self):
        """The List Browser pages a list through its folder table (#1128),
        which starts the parser part-way through the file; it must agree
        with reading the list whole."""
        path = self.write("SomeServer-Files(6)-MX.txt",
                          BANNER + FILE_ROWS + f"{RULE}\nE:\\Music\\Later\n{RULE}\n" + FILE_ROWS
                          + f"{RULE}\nE:\\Music\\Last\n{RULE}\n" + FILE_ROWS)
        whole = list_mod.page_folder_groups(
            list_mod.group_rows_by_folder(list_mod.iter_filelist_rows(path, BOT)), 0, 50)

        paged = list_mod.page_of_list_files([path], 0, 50, BOT)

        self.assertIsNotNone(paged)
        self.assertEqual([(g["folder"], g["count"]) for g in paged[0]],
                         [(g["folder"], g["count"]) for g in whole[0]])
        self.assertEqual([g["folder"] for g in paged[0]], ["", "E:\\Music\\Later", "E:\\Music\\Last"])
        # And a second page read from the table it built.
        again = list_mod.page_of_list_files([path], 1, 50, BOT)
        self.assertEqual([g["folder"] for g in again[0]], ["E:\\Music\\Later", "E:\\Music\\Last"])

    def test_any_other_list_reads_as_before(self):
        """The same banner in a list that is not mxrarserver's: its last
        block is a heading, as it always was."""
        path = self.write("SomeServer-2026-09-07.txt", BANNER + FILE_ROWS)

        self.assertEqual(self.folders(path), ["> Overview", "> Overview"])

    def test_dccore_s_summary_line_under_a_heading_is_not_a_banner(self):
        path = self.write("SomeServer-Files(2)-MX.txt",
                          f"{RULE}\nD:\\MEDIA\\Some Album\\\n{RULE}\n2 files, 34.6 MB\n" + FILE_ROWS)

        # In an mxrarserver list, the block above the first row is banner...
        self.assertEqual(self.folders(path), ["", ""])
        # ...and in DCCore's own, a heading with its summary line.
        path = self.write("SomeServer-2026-09-07.txt",
                          f"{RULE}\nD:\\MEDIA\\Some Album\\\n{RULE}\n2 files, 34.6 MB\n" + FILE_ROWS)
        self.assertEqual(self.folders(path), ["D:\\MEDIA\\Some Album\\"] * 2)


if __name__ == "__main__":
    unittest.main()
