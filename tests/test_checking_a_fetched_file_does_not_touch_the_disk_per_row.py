"""#1439: "another row holds this file" never touches the disk per row.

Since #1423, files_other_rows_hold_locked() called fetched_dir_of() for
every history row with a stored file, and for a row with a folder that is
dcc.is_safe_path(): two os.path.realpath calls. It runs under _fetch_lock(),
which the IRC read thread also takes - on every completed fetch, every
dashboard Delete and once per folder in a batch delete. With 500 rows that
was 1000 realpath calls, far more on a NAS share.

Now the folder being checked is worked out once, lexically
(_directory_folder_key()), and each row's `fetched_folder` becomes a key from
the string alone (_fetched_folder_key()). Checked here on the real code, in a
temp FETCHED_FILES_DIR:

  * one check makes no filesystem call per row, however many rows;
  * which rows count as holding the file, for well-formed rows;
  * a hand-edited folder still matches nothing, and the two key functions
    on their own;
  * fetched_dir_of() refuses a folder exactly when the key does (#1439
    review): an absolute folder, even one inside FETCHED_FILES_DIR, and a
    drive-relative one on Windows are refused by both, and a Delete reads
    no file through them;
  * a dashboard delete, single and batch, and the promotion to the plain
    name still leave a file another row in the same bot folder names.
"""

import io
import os
import threading
import unittest
from collections import Counter

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase, windows_path_rules

import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import web_fetch  # noqa: E402


class FolderCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(self.root, exist_ok=True)
        self.set_config(FETCHED_FILES_DIR=self.root, FETCHED_FILES_PER_BOT=True, fetch_queue={})

    def at(self, folder):
        return os.path.join(self.root, folder) if folder else self.root

    def put(self, folder, name, body=b"x"):
        directory = self.at(folder)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with io.open(path, "wb") as handle:
            handle.write(body)
        return path

    def climbing_and_nesting(self):
        """Hand-edited folders, each with the directory it would have named."""
        absolute = os.path.abspath(os.sep + "abs")
        return {
            os.pardir + os.sep + "x": os.path.join(os.path.dirname(self.root), "x"),
            os.pardir: os.path.dirname(self.root),
            os.curdir: self.root,
            "a" + os.sep + "b": os.path.join(self.root, "a", "b"),
            "a/b": os.path.join(self.root, "a", "b"),
            absolute: absolute,
            # Absolute, but naming a folder that IS inside FETCHED_FILES_DIR.
            os.path.join(self.root, "BotA"): os.path.join(self.root, "BotA"),
        }

    def held(self, queue, folder_or_dir, names, row=None):
        directory = folder_or_dir if os.path.isabs(str(folder_or_dir or "")) else self.at(folder_or_dir)
        return dcc_fetch.files_other_rows_hold_locked(queue, directory, names, row=row)


class NoDiskCallPerRow(FolderCase):
    """The regression itself: every way a row's folder used to be resolved
    on disk is counted, for a small and a large history."""

    # Every call that asks the disk about a path, and the two functions
    # that used to do it per row. os.stat and os.lstat also catch an
    # exists()/isdir()/realpath() reached some other way.
    COUNTED = ((os, "stat"), (os, "lstat"), (os.path, "exists"), (os.path, "isdir"),
               (os.path, "realpath"), (os.path, "samefile"),
               (dcc, "is_safe_path"), (dcc_fetch, "fetched_dir_of"))

    def count_disk_calls(self, run):
        # Only this thread's calls: a thread another test left running must
        # not be counted against the check.
        me = threading.get_ident()
        calls = []
        originals = [(owner, attr, getattr(owner, attr)) for owner, attr in self.COUNTED]
        for owner, attr, real in originals:
            self.addCleanup(setattr, owner, attr, real)

            def wrapper(*args, _attr=attr, _real=real, **kwargs):
                if threading.get_ident() == me:
                    calls.append(_attr)
                return _real(*args, **kwargs)
            setattr(owner, attr, wrapper)
        try:
            result = run()
        finally:
            for owner, attr, real in originals:
                setattr(owner, attr, real)
        return result, calls

    def history(self, rows):
        # The folder is there, as it is on a real install: a disk call that
        # asked about it would find it and go on, and be counted.
        for folder in ("BotA", "BotB", "Other"):
            os.makedirs(self.at(folder), exist_ok=True)
        queue = {}
        folders = ("BotA", "BotB", None, "Other")
        for i in range(rows):
            queue[f"r{i}"] = {"stored_filename": f"f{i}.bin", "fetched_folder": folders[i % 4],
                              "state": "complete", "request_type": "file"}
        return queue

    def calls_for(self, rows):
        queue = self.history(rows)

        def check():
            with dcc_fetch._fetch_lock():
                return self.held(queue, "BotA", ["f0.bin", "f4.bin", "f1.bin", "missing.bin"])
        held, calls = self.count_disk_calls(check)
        self.assertEqual(held, {"f0.bin", "f4.bin"})
        return calls

    def test_the_calls_do_not_grow_with_the_rows(self):
        few = self.calls_for(20)
        many = self.calls_for(200)
        self.assertEqual(len(many), len(few), (Counter(few), Counter(many)))
        self.assertLessEqual(len(many), 2, Counter(many))

    def test_no_row_is_resolved_on_disk(self):
        calls = self.calls_for(200)
        for attr in ("stat", "lstat", "exists", "isdir", "realpath", "is_safe_path", "fetched_dir_of"):
            with self.subTest(call=attr):
                self.assertEqual(calls.count(attr), 0, Counter(calls))


class WhichRowsHoldTheFile(FolderCase):

    def row(self, folder, name="cover.jpg"):
        return {"stored_filename": name, "fetched_folder": folder}

    def test_a_row_in_the_same_bot_folder_holds_it(self):
        self.assertEqual(self.held({"b": self.row("BotA")}, "BotA", ["cover.jpg"]), {"cover.jpg"})

    def test_a_row_in_another_bots_folder_does_not(self):
        self.assertEqual(self.held({"b": self.row("BotB")}, "BotA", ["cover.jpg"]), set())

    def test_a_root_row_holds_it_at_the_root(self):
        for folder in (None, ""):
            with self.subTest(folder=folder):
                self.assertEqual(self.held({"b": self.row(folder)}, None, ["cover.jpg"]), {"cover.jpg"})

    def test_a_root_row_does_not_hold_it_in_a_bot_folder(self):
        self.assertEqual(self.held({"b": self.row(None)}, "BotA", ["cover.jpg"]), set())

    def test_a_bot_folder_row_does_not_hold_it_at_the_root(self):
        self.assertEqual(self.held({"b": self.row("BotA")}, None, ["cover.jpg"]), set())

    def test_the_row_itself_never_does(self):
        own = self.row("BotA")
        self.assertEqual(self.held({"a": own}, "BotA", ["cover.jpg"], row=own), set())
        own_root = self.row(None)
        self.assertEqual(self.held({"a": own_root}, None, ["cover.jpg"], row=own_root), set())

    def test_a_folder_differing_only_in_case_holds_it(self):
        """On purpose, on every platform: counting it can only keep a file."""
        self.assertEqual(self.held({"b": self.row("bota")}, "BotA", ["cover.jpg"]), {"cover.jpg"})
        self.assertEqual(self.held({"b": self.row("BOTA")}, "bota", ["cover.jpg"]), {"cover.jpg"})

    def test_a_row_with_no_stored_file_does_not(self):
        self.assertEqual(self.held({"b": self.row("BotA", name=None)}, "BotA", ["cover.jpg"]), set())

    def test_a_trailing_separator_or_an_unnormalised_directory_changes_nothing(self):
        queue = {"b": self.row("BotA")}
        for directory in (os.path.join(self.root, "BotA") + os.sep,
                          os.path.join(self.root, "x", os.pardir, "BotA"),
                          self.root + os.sep + os.curdir + os.sep + "BotA"):
            with self.subTest(directory=directory):
                self.assertEqual(self.held(queue, directory, ["cover.jpg"]), {"cover.jpg"})

    def test_a_folder_of_the_same_name_outside_fetched_files_dir_does_not(self):
        elsewhere = os.path.join(os.path.dirname(self.root), "elsewhere", "BotA")
        self.assertEqual(self.held({"b": self.row("BotA")}, elsewhere, ["cover.jpg"]), set())


class AHandEditedFolderMatchesNothing(FolderCase):
    """A history file edited by hand: a folder that climbs, nests, is
    absolute or is the root spelled "." never counts as the folder being
    checked - fetched_dir_of() returns None for it, so no reader ever finds
    its file there either."""

    def test_never_counts_as_the_folder_it_names(self):
        for folder, directory in self.climbing_and_nesting().items():
            with self.subTest(folder=folder):
                queue = {"b": {"stored_filename": "cover.jpg", "fetched_folder": folder}}
                self.assertEqual(self.held(queue, directory, ["cover.jpg"]), set())

    def test_never_counts_at_the_root_or_in_a_bot_folder_either(self):
        for folder in self.climbing_and_nesting():
            for where in (None, "BotA", "b", "x"):
                with self.subTest(folder=folder, where=where):
                    queue = {"b": {"stored_filename": "cover.jpg", "fetched_folder": folder}}
                    self.assertEqual(self.held(queue, where, ["cover.jpg"]), set())

    def test_one_that_climbs_back_in_is_the_folder_it_lands_in(self):
        folder = "a" + os.sep + os.pardir + os.sep + "bot"
        queue = {"b": {"stored_filename": "cover.jpg", "fetched_folder": folder}}
        self.assertEqual(self.held(queue, "bot", ["cover.jpg"]), {"cover.jpg"})
        self.assertEqual(self.held(queue, None, ["cover.jpg"]), set())
        # The same answer fetched_dir_of() gives that row.
        self.assertEqual(os.path.normpath(dcc_fetch.fetched_dir_of(queue["b"])),
                         os.path.join(self.root, "bot"))

    def test_a_dashboard_delete_through_an_absolute_folder_inside_removes_nothing(self):
        """The folder string is absolute but names a folder inside
        FETCHED_FILES_DIR: refused by the one lexical rule, so this row's
        Delete reads no file through it (#1439 review)."""
        path = self.put("BotA", "cover.jpg")
        config.fetch_queue["r1"] = {"bot": "BotA", "state": "complete", "stored_filename": "cover.jpg",
                                    "filename": "cover.jpg", "request_type": "file",
                                    "fetched_folder": os.path.join(self.root, "BotA")}
        web_fetch.build_fetch_delete_result("r1")
        self.assertNotIn("r1", config.fetch_queue)
        self.assertTrue(os.path.exists(path))


class OneRuleDecidesWhichFolderIsValid(FolderCase):
    """fetched_dir_of() - what every reader and every delete of a row's file
    goes through - refuses a folder exactly when _fetched_folder_key() does
    (#1439 review). Were the two to differ, a row could have a file the
    check never counts as held: its own Delete would remove a file another
    row names, or the other way round."""

    def folders(self):
        return (["BotA", "bota", "Bot A", "a" + os.sep + os.pardir + os.sep + "bot", "Bot" + os.sep,
                 "", None]
                + list(self.climbing_and_nesting()))

    def agree(self, folder):
        refused_by_reader = dcc_fetch.fetched_dir_of({"fetched_folder": folder}) is None
        refused_by_key = dcc_fetch._fetched_folder_key(folder) is None
        return refused_by_reader, refused_by_key

    def test_both_refuse_the_same_folders(self):
        for folder in self.folders():
            with self.subTest(folder=folder):
                refused_by_reader, refused_by_key = self.agree(folder)
                self.assertEqual(refused_by_reader, refused_by_key)

    def test_both_take_the_plain_ones(self):
        """Control: the two-way test above is not passing on None == None."""
        for folder in ("BotA", "a" + os.sep + os.pardir + os.sep + "bot", "", None):
            with self.subTest(folder=folder):
                self.assertEqual(self.agree(folder), (False, False))

    def test_and_under_windows_rules_wherever_the_suite_runs(self):
        """The same, with Windows' path rules (ntpath) in dcc_fetch's place:
        "C:bot" on a FETCHED_FILES_DIR on drive C: is the folder C:\\...\\bot
        to os.path.join, and absolute "C:\\t\\fetched\\BotA" is inside it.
        dcc.is_safe_path() is the real one, under the same rules
        (tests.support.windows_path_rules(), #1440): there its realpath() is
        abspath(), which reads no disk."""
        root = "C:\\t\\fetched"
        self.set_config(FETCHED_FILES_DIR=root)
        folders = ("C:bot", "C:", "D:bot", "C:\\t\\fetched\\BotA", "\\t\\fetched\\BotA",
                   "\\\\server\\share", "..\\x", "a\\b", "a/b", "a\\..\\Bot", "BotA", "", None)
        with windows_path_rules(self, dcc_fetch, functions=[(dcc, "is_safe_path")]):
            answers = {folder: self.agree(folder) for folder in folders}
            plain = dcc_fetch.fetched_dir_of({"fetched_folder": "BotA"})
        self.assertEqual(plain, root + "\\BotA", "the stand-in reaches the real join")
        for folder, (refused_by_reader, refused_by_key) in answers.items():
            with self.subTest(folder=folder):
                self.assertEqual(refused_by_reader, refused_by_key)
        self.assertEqual(answers["C:bot"], (True, True))
        self.assertEqual(answers["C:\\t\\fetched\\BotA"], (True, True))


class TheFolderKey(unittest.TestCase):
    """_fetched_folder_key(): a row's folder from the string alone."""

    def test_no_folder_is_the_root(self):
        for folder in ("", None):
            with self.subTest(folder=folder):
                self.assertEqual(dcc_fetch._fetched_folder_key(folder), "")

    def test_a_plain_folder_is_its_name_casefolded(self):
        self.assertEqual(dcc_fetch._fetched_folder_key("BotA"), "bota")
        self.assertEqual(dcc_fetch._fetched_folder_key("STRASSE"), dcc_fetch._fetched_folder_key("Straße"))

    def test_normalised_first(self):
        self.assertEqual(dcc_fetch._fetched_folder_key("a" + os.sep + os.pardir + os.sep + "Bot"), "bot")
        self.assertEqual(dcc_fetch._fetched_folder_key("Bot" + os.sep), "bot")
        self.assertEqual(dcc_fetch._fetched_folder_key(os.curdir + os.sep + "Bot"), "bot")

    def test_one_that_climbs_nests_or_is_absolute_is_none(self):
        for folder in (os.curdir, os.pardir, os.pardir + os.sep + "x", "a" + os.sep + "b", "a/b",
                       "a" + os.sep + "b" + os.sep + os.pardir + os.sep + os.pardir + os.sep + os.pardir,
                       os.path.abspath(os.sep + "abs"), os.sep + "abs"):
            with self.subTest(folder=folder):
                self.assertIsNone(dcc_fetch._fetched_folder_key(folder))

    def test_one_naming_a_drive_is_none_where_there_are_drives(self):
        """Where a name like "C:bot" is a drive (Windows) it is refused;
        elsewhere it is one plain folder name."""
        for folder in ("C:bot", "C:", "D:bot"):
            with self.subTest(folder=folder):
                if os.path.splitdrive(folder)[0]:
                    self.assertIsNone(dcc_fetch._fetched_folder_key(folder))
                else:
                    self.assertEqual(dcc_fetch._fetched_folder_key(folder), folder.casefold())

    def test_windows_rules_wherever_the_suite_runs(self):
        """The drive check bites only on Windows, so it is proven here with
        Windows' path rules (ntpath) in dcc_fetch's place for these calls only:
        on a POSIX host every absolute path already has a separator in it."""
        cases = {"C:bot": None, "C:": None, "C:\\bot": None, "\\bot": None, "/bot": None,
                 "\\\\server\\share": None, "a\\b": None, "a/b": None, "..\\x": None,
                 "a\\..\\Bot": "bot", "Bot\\": "bot", "Bot/": "bot", "BotA": "bota", "": ""}
        with windows_path_rules(self, dcc_fetch):
            answers = {folder: dcc_fetch._fetched_folder_key(folder) for folder in cases}
        self.assertEqual(answers, cases)


class TheDirectoryKey(FolderCase):
    """_directory_folder_key(): the folder being checked, lexically."""

    def test_the_root_is_the_empty_key(self):
        for directory in (self.root, self.root + os.sep,
                          os.path.join(self.root, "BotA", os.pardir)):
            with self.subTest(directory=directory):
                self.assertEqual(dcc_fetch._directory_folder_key(directory), "")

    def test_a_direct_child_is_its_key(self):
        for directory in (os.path.join(self.root, "BotA"),
                          os.path.join(self.root, "BotA") + os.sep,
                          os.path.join(self.root, "x", os.pardir, "BotA")):
            with self.subTest(directory=directory):
                self.assertEqual(dcc_fetch._directory_folder_key(directory), "bota")

    def test_a_mixed_separator_where_there_is_one(self):
        """Windows' "/" next to its "\\"; nowhere else has a second one."""
        for sep in (os.sep, os.path.altsep):
            if sep:
                with self.subTest(sep=sep):
                    self.assertEqual(dcc_fetch._directory_folder_key(self.root + sep + "BotA"), "bota")

    def test_anywhere_else_is_none(self):
        parent = os.path.dirname(self.root)
        for directory in (parent,
                          os.path.join(parent, "BotA"),
                          os.path.join(self.root, "BotA", "deeper"),
                          os.path.join(self.root, os.pardir, "x"),
                          self.root + "-backup",
                          "", None):
            with self.subTest(directory=directory):
                self.assertIsNone(dcc_fetch._directory_folder_key(directory))

    def test_touches_no_disk(self):
        """The folder being checked need not exist: it is never resolved."""
        missing = os.path.join(self.root, "NotMadeYet")
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(dcc_fetch._directory_folder_key(missing), "notmadeyet")


class TheCallersStillKeepAHeldFile(FolderCase):
    """End to end, in a bot's folder: #1269's guards per folder (#1423),
    through the check that no longer resolves rows (#1439)."""

    def row(self, folder, name="cover.jpg"):
        return {"bot": folder or "ServerOne", "state": "complete", "stored_filename": name,
                "filename": name, "fetched_folder": folder, "request_type": "file"}

    def test_a_delete_keeps_a_file_another_row_in_the_same_folder_names(self):
        path = self.put("BotA", "cover.jpg")
        config.fetch_queue["r1"] = self.row("BotA")
        config.fetch_queue["r2"] = self.row("BotA")
        self.assertEqual(web_fetch.build_fetch_delete_result("r1")[0], 200)
        self.assertTrue(os.path.exists(path))
        # The last row naming it takes it with it.
        self.assertEqual(web_fetch.build_fetch_delete_result("r2")[0], 200)
        self.assertFalse(os.path.exists(path))

    def test_a_delete_keeps_it_for_a_row_whose_folder_differs_in_case(self):
        path = self.put("BotA", "cover.jpg")
        config.fetch_queue["r1"] = self.row("BotA")
        config.fetch_queue["r2"] = self.row("bota")
        self.assertEqual(web_fetch.build_fetch_delete_result("r1")[0], 200)
        self.assertTrue(os.path.exists(path))

    def test_a_delete_removes_it_when_only_another_folders_row_names_it(self):
        """Control: the same name in another bot's folder is another file."""
        path = self.put("BotA", "cover.jpg")
        other = self.put("BotB", "cover.jpg")
        config.fetch_queue["r1"] = self.row("BotA")
        config.fetch_queue["r2"] = self.row("BotB")
        self.assertEqual(web_fetch.build_fetch_delete_result("r1")[0], 200)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(other))

    def test_a_batch_delete_keeps_it_too_unless_the_batch_holds_both(self):
        path = self.put("BotA", "cover.jpg")
        config.fetch_queue["r1"] = self.row("BotA")
        config.fetch_queue["r2"] = self.row("BotA")
        config.fetch_queue["r3"] = self.row("BotB", "other.jpg")
        status, result = web_fetch.build_fetch_delete_many_result(["r1", "r3"])
        self.assertEqual((status, sorted(result["cancelled"])), (200, ["r1", "r3"]))
        self.assertTrue(os.path.exists(path))
        web_fetch.build_fetch_delete_many_result(["r2"])
        self.assertFalse(os.path.exists(path))

    def promote(self, holder_folder):
        staged = "abcdef012345_cover.jpg"
        self.put("BotA", staged)
        mine = self.row("BotA", staged)
        queue = {"mine": mine}
        if holder_folder is not False:
            # Its own file was moved out of the folder: the plain name is free on disk.
            queue["old"] = self.row(holder_folder)
        with dcc_fetch._fetch_lock():
            return dcc_fetch._promote_clean_filename(self.at("BotA"), staged, queue=queue, row=mine)

    def test_promotion_leaves_a_name_a_row_in_the_same_folder_holds(self):
        self.assertEqual(self.promote("BotA"), "abcdef012345_cover.jpg")
        self.assertFalse(os.path.exists(os.path.join(self.at("BotA"), "cover.jpg")))

    def test_promotion_leaves_it_for_a_folder_differing_in_case(self):
        self.assertEqual(self.promote("BOTA"), "abcdef012345_cover.jpg")

    def test_promotion_takes_a_name_only_another_folder_holds(self):
        """Control: a row at the root or in another bot's folder holds
        another file."""
        for holder in ("BotB", None):
            with self.subTest(holder=holder):
                self.assertEqual(self.promote(holder), "cover.jpg")
                os.remove(os.path.join(self.at("BotA"), "cover.jpg"))

    def test_promotion_with_no_other_row(self):
        self.assertEqual(self.promote(False), "cover.jpg")


if __name__ == "__main__":
    unittest.main()
