"""A List Browser page of our own list reads only its own folders (#1128).

Every GET /api/filelists re-read the whole of our own lists to return one
page of folders: a dict per row, a second dict per row, every row grouped,
and then 200 folders kept. 31 s and 2.1 GB per page at two million rows, paid
again on every page change. The first page now builds a table of where each
folder's rows are, how many survive dedup and which do not; a page seeks to
its own folders and parses only those.

The page must be IDENTICAL to the one the whole-list parse gives, and the
audit's skeptic listed why that is harder than it looks: dedup runs in file
order across every file, on (folder.lower(), filename.lower(), size), so two
headings that differ only in case share keys and a page cannot recompute it
from its own folders; a heading that appears twice is one group at its first
position; rows before any heading are the '' group; and our own list is the
master list and the video list read as one. So the reference here is the
whole-list parse itself, on a list built to hold all of that, page by page.
"""

import io
import os
import random

from tests import support
from tests.support import DCCoreTestCase

import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

RULE = "=" * 40


def adversarial_master(rng):
    """Text of a master list holding every shape the skeptic named, and more."""
    out = ["List of many Files (1.0GB) generated today", "", ""]
    # Rows before any heading: the '' group.
    out += ["!SomeBot Loose Track.mp3  ::INFO:: 1.00MB",
            "!SomeBot Loose Track.mp3  ::INFO:: 1.00MB",
            "!SomeBot loose track.MP3  ::INFO:: 1.00MB"]

    def heading(text, closing=True):
        out.append(RULE)
        out.append(text)
        if closing:
            out.append(RULE)

    names = ["Intro", "Song", "Ballad", "Café Noir", "Ωμέγα", "Track"]
    for i in range(60):
        kind = rng.random()
        if kind < 0.15:
            text = "D:\\MEDIA\\Abba\\Gold\\"            # repeated heading
        elif kind < 0.25:
            text = "D:\\MEDIA\\ABBA\\GOLD\\"            # its case twin
        elif kind < 0.30:
            text = "D:\\MEDIA\\Empty Folder %d\\" % i   # a heading with no rows
        else:
            text = "D:\\MEDIA\\Artist %d\\Album %d\\" % (i % 17, i)
        heading(text, closing=rng.random() > 0.05)
        if text.startswith("D:\\MEDIA\\Empty"):
            continue
        for n in range(rng.randint(1, 9)):
            name = rng.choice(names) + " %02d" % rng.randint(1, 6)
            name = rng.choice([name, name.upper(), name.lower()])
            size = rng.choice(["1.00MB", "2.00MB", "3.50MB"])
            out.append("!SomeBot %s.flac  ::INFO:: %s" % (name, size))
            if rng.random() < 0.1:
                out.append("")                          # blank lines
        if rng.random() < 0.05:
            out.append(RULE)
            out.append(RULE)                           # a doubled rule
            out.append("D:\\MEDIA\\After A Doubled Rule\\")
            out.append(RULE)
            out.append("!SomeBot Doubled.flac  ::INFO:: 1.00MB")
        if rng.random() < 0.05:
            out.append(RULE)                           # a file where a heading was due
            out.append("!SomeBot Resync.flac  ::INFO:: 1.00MB")
    # A folder larger than the page's row valve, so it is cut.
    heading("D:\\MEDIA\\Huge\\")
    out += ["!SomeBot Huge %05d.flac  ::INFO:: 1.00MB" % n
            for n in range(list_mod.FILELISTS_MAX_PAGE_ROWS + 40)]
    heading("D:\\MEDIA\\After Huge\\")
    out += ["!SomeBot After %02d.flac  ::INFO:: 1.00MB" % n for n in range(50)]
    heading("D:\\MEDIA\\Abba\\Gold\\")
    out += ["!SomeBot Last Word.flac  ::INFO:: 1.00MB",
            "!SomeBot Nul\x00Byte.flac  ::INFO:: 1.00MB",
            "!SomeBot Line\u2028Separator\x85Next\x0cFeed.flac  ::INFO:: 1.00MB"]
    return "\n".join(out)          # no newline at the end


def adversarial_video():
    """The video list: rows before a heading again, a heading the master list
    has too (one group across both files) and its case twin."""
    return "\r\n".join([
        "\ufeffList of Films",
        "!SomeBot Loose Film.mkv  ::INFO:: 9.00GB",
        "!SomeBot Loose Track.mp3  ::INFO:: 1.00MB",
        RULE, "D:\\MEDIA\\Abba\\Gold\\", RULE,
        "!SomeBot Song 01.flac  ::INFO:: 1.00MB",
        "!SomeBot Abba The Movie.mkv  ::INFO:: 4.00GB",
        RULE, "D:\\MEDIA\\abba\\gold\\", RULE,
        "!SomeBot ABBA THE MOVIE.mkv  ::INFO:: 4.00GB",
        "!SomeBot Only Here.mkv  ::INFO:: 4.00GB",
        RULE, "D:\\MEDIA\\Films\\", RULE,
        "!SomeBot Film One.mkv  ::INFO:: 1.00GB",
        RULE, "D:\\MEDIA\\FILMS\\", RULE,
        "!SomeBot film one.MKV  ::INFO:: 1.00GB",
    ]) + "\r\n"


class OwnListCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LIST_BASE_NAME="SomeBot", NICKNAME="SomeBot")
        list_mod.forget_folder_tables()
        self.addCleanup(list_mod.forget_folder_tables)
        self.master = os.path.join(self.tree.lists, "SomeBot-2026-08-25.txt")
        self.video = os.path.join(self.tree.lists, "SomeBot-VIDEO-2026-08-25.txt")
        self.write(self.master, adversarial_master(random.Random(1128)).encode("utf-8")
                   + b"\n!SomeBot Bad \xff\xfe Bytes \xe2\x82.flac  ::INFO:: 1.00MB")
        self.write(self.video, adversarial_video().encode("utf-8"))

    def write(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def whole_list_payload(self, *args, **kwargs):
        """build_filelists_payload() as it was: the whole list parsed."""
        real = list_mod.page_of_list_files
        list_mod.page_of_list_files = lambda *a, **k: None
        try:
            return webserver.build_filelists_payload(*args, **kwargs)
        finally:
            list_mod.page_of_list_files = real

    def spy(self, name):
        real = getattr(list_mod, name)
        calls = []

        def counted(*args, **kwargs):
            result = real(*args, **kwargs)
            calls.append((args, result))
            return result

        setattr(list_mod, name, counted)
        self.addCleanup(setattr, list_mod, name, real)
        return calls


class EveryPageIsTheSameAsTheWholeListParse(OwnListCase):

    def test_the_list_has_every_shape(self):
        """The fixture is what makes the comparison below worth anything."""
        groups, offset = [], 0
        while True:
            page = self.whole_list_payload(offset, 100000)
            if not page["folders"]:
                break
            groups += page["folders"]
            offset += page["returned"]
        folders = [g["folder"] for g in groups]
        self.assertEqual(len(folders), page["total"])
        self.assertIn("", folders)
        self.assertIn("D:\\MEDIA\\Abba\\Gold\\", folders)
        self.assertIn("D:\\MEDIA\\ABBA\\GOLD\\", folders)
        self.assertIn("D:\\MEDIA\\Films\\", folders, "the video list is read too")
        self.assertEqual(len(folders), len(set(folders)), "a repeated heading is one group")
        rows = sum(g["count"] for g in groups)
        self.assertEqual(rows, page["total_files"])
        raw = 0
        for path in (self.master, self.video):
            with open(path, encoding="utf-8", errors="replace") as handle:
                raw += sum(1 for line in handle
                           if line.replace("\x00", "").strip().startswith("!"))
        self.assertLess(rows, raw - 10, "dedup has duplicates to drop")
        self.assertTrue(any(g.get("truncated") for g in groups))

    def test_every_page_at_every_size(self):
        total = self.whole_list_payload()["total"]
        self.assertGreater(total, 40)
        answered = self.spy("page_of_list_files")
        for limit in (1, 2, 7, 50, 200, None, 0):
            step = limit or total
            for offset in list(range(0, total + 2, step)) + [total + 50]:
                with self.subTest(offset=offset, limit=limit):
                    self.assertEqual(webserver.build_filelists_payload(offset, limit),
                                     self.whole_list_payload(offset, limit))
        # Every one of them from the table, not from the whole-list parse it
        # falls back to - which would pass the comparison by being it.
        self.assertTrue(answered)
        self.assertNotIn(None, [result for _args, result in answered])

    def test_a_negative_offset_is_answered_the_same(self):
        self.assertEqual(webserver.build_filelists_payload(-3, 5),
                         self.whole_list_payload(-3, 5))

    def test_a_folder_whose_every_row_is_a_duplicate_is_no_group(self):
        """The video list's "D:\\MEDIA\\FILMS\\" holds only a row its
        case twin already listed, so dedup drops it and no group is made."""
        folders, offset = [], 0
        while True:
            page = webserver.build_filelists_payload(offset, 50)
            if not page["folders"]:
                break
            folders += [g["folder"] for g in page["folders"]]
            offset += page["returned"]
        self.assertIn("D:\\MEDIA\\Films\\", folders)
        self.assertNotIn("D:\\MEDIA\\FILMS\\", folders)


class APageParsesOnlyItsOwnFolders(OwnListCase):

    def test_the_table_is_built_once(self):
        builds = self.spy("_build_folder_table")
        for offset in (0, 5, 10, 0):
            webserver.build_filelists_payload(offset, 5)
        self.assertEqual(len(builds), 1)

    def test_a_page_reads_its_own_rows_and_no_others(self):
        webserver.build_filelists_payload(0, 1)
        split = self.spy("_split_entry_line")

        page = webserver.build_filelists_payload(5, 2)

        shown = sum(len(g["entries"]) for g in page["folders"])
        self.assertGreater(shown, 0)
        self.assertLess(len(split), shown + 20,
                        "the page parsed far more rows than it shows")
        self.assertLess(len(split), page["total_files"] // 10)

    def test_the_folder_after_the_page_is_not_read(self):
        """The row valve ends a page before an outsized folder; that folder's
        rows are not parsed only to be left out."""
        total = self.whole_list_payload()["total"]
        huge = None
        for offset in range(total):
            group = self.whole_list_payload(offset, 1)["folders"][0]
            if group["folder"] == "D:\\MEDIA\\Huge\\":
                huge = offset
                break
        self.assertIsNotNone(huge)
        webserver.build_filelists_payload(0, 1)
        split = self.spy("_split_entry_line")

        page = webserver.build_filelists_payload(huge - 1, 5)

        self.assertTrue(page["row_capped"])
        self.assertEqual(page["returned"], 1)
        self.assertLess(len(split), 100)

    def test_an_outsized_first_folder_ends_the_reading(self):
        """It is cut to the valve and the page ends there; the folders after
        it in the window are not read."""
        total = self.whole_list_payload()["total"]
        huge = [offset for offset in range(total)
                if self.whole_list_payload(offset, 1)["folders"][0]["folder"]
                == "D:\\MEDIA\\Huge\\"][0]
        webserver.build_filelists_payload(0, 1)
        split = self.spy("_split_entry_line")

        page = webserver.build_filelists_payload(huge, 5)

        self.assertTrue(page["folders"][0]["truncated"])
        self.assertEqual(page["returned"], 1)
        self.assertLessEqual(len(split), list_mod.FILELISTS_MAX_PAGE_ROWS + 45)

    def test_a_search_still_reads_the_whole_list(self):
        used = self.spy("page_of_list_files")
        payload = webserver.build_filelists_payload(0, 50, q="abba")
        self.assertEqual(used, [])
        self.assertEqual(payload, self.whole_list_payload(0, 50, q="abba"))


class AChangedListIsReadAgain(OwnListCase):

    def test_a_rebuilt_list_is_read_again(self):
        webserver.build_filelists_payload(0, 5)
        self.write(self.master, b"\n".join([
            RULE.encode(), b"D:\\MEDIA\\New\\", RULE.encode(),
            b"!SomeBot Brand New.flac  ::INFO:: 1.00MB"]) + b"\n")

        self.assertEqual(webserver.build_filelists_payload(0, 5),
                         self.whole_list_payload(0, 5))

    def test_a_rewrite_keeping_size_and_mtime_is_caught(self):
        """A coarse file-system clock can leave both the same. The table's
        offsets would then point into a different file; each run's checksum
        sees it and the page is read whole instead."""
        webserver.build_filelists_payload(0, 500)
        st = os.stat(self.master)
        with open(self.master, "rb") as handle:
            data = handle.read()
        self.write(self.master, data.replace(b"Song", b"Tune"))
        os.utime(self.master, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(self.master).st_size, st.st_size)

        for offset in (0, 3, 20):
            with self.subTest(offset=offset):
                self.assertEqual(webserver.build_filelists_payload(offset, 7),
                                 self.whole_list_payload(offset, 7))

    def test_forgetting_the_tables_reads_the_list_again(self):
        builds = self.spy("_build_folder_table")
        webserver.build_filelists_payload(0, 5)
        list_mod.forget_folder_tables()
        webserver.build_filelists_payload(0, 5)
        self.assertEqual(len(builds), 2)

    def test_a_finished_rebuild_forgets_them(self):
        """commands.py drops the tables when the rebuild's process returns."""
        import ast
        path = os.path.join(support.REPO_ROOT, "src", "commands.py")
        with io.open(path, encoding="utf-8") as handle:
            tree = support.parse_source(handle.read(), path)
        updater = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "async_list_updater"]
        self.assertEqual(len(updater), 1)
        calls = [node for node in ast.walk(updater[0])
                 if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Attribute)
                 and node.value.func.attr == "forget_folder_tables"]
        self.assertEqual(len(calls), 1)

    def test_a_lone_carriage_return_is_read_whole(self):
        """Text mode ends a line at a lone CR; the table's binary reader does
        not, so such a file is never answered from a table."""
        with open(self.master, "ab") as handle:
            handle.write(b"\n" + RULE.encode() + b"\rD:\\MEDIA\\Odd\\\r" + RULE.encode()
                         + b"\r!SomeBot Odd One.flac  ::INFO:: 1.00MB\n")
        self.assertIsNone(list_mod.page_of_list_files(
            list_mod.all_list_paths(), 0, 5, "SomeBot",
            max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS))
        total = self.whole_list_payload()["total"]
        self.assertEqual(webserver.build_filelists_payload(total - 3, 5),
                         self.whole_list_payload(total - 3, 5))


class AFileChangedBetweenTheStatAndTheRead(OwnListCase):
    """The signature is taken, then the file is opened: a rebuild can land in
    between. Each open checks the file it got against the signature."""

    def grow_the_list_behind_the_signature(self):
        old = list_mod._list_signature(list_mod.all_list_paths())
        with open(self.master, "ab") as handle:
            handle.write(b"\n!SomeBot Appended.flac  ::INFO:: 1.00MB\n")
        real = list_mod._list_signature
        list_mod._list_signature = lambda paths: old
        self.addCleanup(setattr, list_mod, "_list_signature", real)
        return old, real

    def test_a_page_read_from_a_grown_file_falls_back(self):
        """The first page's rows are untouched by the growth, so only the
        open's own check sees that the totals are not the file's any more."""
        webserver.build_filelists_payload(0, 1)
        _old, real = self.grow_the_list_behind_the_signature()

        got = webserver.build_filelists_payload(0, 1)

        list_mod._list_signature = real
        self.assertEqual(got, self.whole_list_payload(0, 1))


class TheTableIsSafeAcrossAReloadAndAMissingFile(OwnListCase):

    def test_the_lock_is_runtimes(self):
        self.assertIs(list_mod._folder_table_lock, runtime.list_folder_table_lock)

    def test_a_missing_list_is_answered_the_same(self):
        os.remove(self.video)
        self.assertEqual(webserver.build_filelists_payload(0, 9),
                         self.whole_list_payload(0, 9))
        os.remove(self.master)
        self.assertEqual(webserver.build_filelists_payload(0, 9),
                         self.whole_list_payload(0, 9))
