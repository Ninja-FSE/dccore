"""A List Browser page of a FETCHED list reads only its own folders (#1128).

The other half of #1128 (the audit's FETCH-PAGE). get_fetched_bot_page()
re-parsed the whole of another bot's list for every page: 11 s and 412 MB per
page at 378k rows, and all of it under list_fetch's lock, so a fetch landing
meanwhile waited too. It now pages from the same folder table as our own
list (test_a_list_browser_page_reads_only_its_own_folders), built under that
lock.

A fetched list is written by somebody else's software, so the fixture here is
what ours never contains: ::INFO:: in other bots' spellings and with branding
after the size, the dash-size suffix with no marker at all, rows with no size,
a folder named "====" and the parser's resync, "!rar" request rows, a
marker list beside the main one. Every page must equal the whole-file parse.
A refetch and a purge must drop that bot's tables, and only that bot's.
"""

import io
import os
import random
import zipfile

from tests.support import DCCoreTestCase

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import webserver  # noqa: E402

RULE = "=" * 53


def foreign_list(rng, nick="OtherBot"):
    """Bytes of a list in other bots' formats."""
    out = ["\ufeffList of many Files (c) SomeServe v2.60", "", "",
           "!%s Before Any Heading.mp3  ::INFO:: 2.00MB" % nick,
           "!%s before any heading.MP3  ::INFO:: 2.00MB" % nick]
    tails = ["  ::INFO:: {s}", " ::INFO:: {s} (c) SomeServe v2.60 (c)",
             "::INFO::{s} 4m30s 192/44.10/JS  SomeServe v2.71",
             " ::info:: {s} : SomeServe v2.71 :", " ---- {s}", "  -- {s}",
             "", " no size here at all"]
    names = ["Intro", "Love Song", "Ballad", "Café Noir", "Ωμέγα", "Track - Mix"]
    for i in range(70):
        kind = rng.random()
        if kind < 0.12:
            heading = "D:\\MUSIC\\Shared\\Album\\"
        elif kind < 0.2:
            heading = "D:\\MUSIC\\SHARED\\ALBUM\\"
        elif kind < 0.24:
            heading = "===="                      # a folder named only "="
        elif kind < 0.28:
            heading = "Bare Folder Name %d" % i   # no drive prefix
        else:
            heading = "E:\\Stuff\\Artist %d\\Album %d (%d)\\" % (i % 13, i, 1970 + i)
        out += [RULE, heading, RULE]
        for _ in range(rng.randint(1, 10)):
            name = rng.choice(names) + " %02d" % rng.randint(1, 5)
            name = rng.choice([name, name.upper(), name.lower()])
            size = rng.choice(["18.8Mb", "6.32Mb", "153.03MB", "1,024KB"])
            ext = rng.choice([".flac", ".mp3", ""])
            out.append("!%s %s%s%s" % (nick, name, ext,
                                       rng.choice(tails).format(s=size)))
        if rng.random() < 0.1:
            out.append("!%s !rar D:\\MUSIC\\Shared\\Album\\" % nick)
        if rng.random() < 0.05:
            out.append(RULE)                      # resync: a file where a heading was due
            out.append("!%s Resync.flac  ::INFO:: 1.00MB" % nick)
    out += [RULE, "E:\\Stuff\\Huge\\", RULE]
    out += ["!%s Huge %05d.flac  ::INFO:: 1.00MB" % (nick, n)
            for n in range(list_mod.FILELISTS_MAX_PAGE_ROWS + 30)]
    out += [RULE, "E:\\Stuff\\After Huge\\", RULE]
    out += ["!%s After %02d.flac ---- 2.0Mb" % (nick, n) for n in range(40)]
    out += [RULE, "D:\\MUSIC\\Shared\\Album\\", RULE,
            "!%s Nul\x00Byte.flac  ::INFO:: 1.00MB" % nick,
            "!%s Line\u2028Sep\x85Next\x0cFeed.flac  ::INFO:: 1.00MB" % nick]
    return ("\r\n".join(out)).encode("utf-8") + (
        b"\r\n!" + nick.encode() + b" Bad \xff\xfe \xe2\x82.flac  ::INFO:: 1.00MB")


def rar_list(nick="OtherBot"):
    """A marker list: every row is the request line to type."""
    lines = ["List of albums", RULE, "D:\\MUSIC\\", RULE]
    lines += ["!%s !rar D:\\MUSIC\\Album %d\\" % (nick, n) for n in range(30)]
    lines += ["!%s !RAR D:\\MUSIC\\album 3\\" % nick]
    return "\n".join(lines).encode("utf-8") + b"\n"


class FetchedListCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-fetched-pages-")
        self.set_config(FETCHED_FILES_DIR=self.dir, fetched_bot_lists={})
        list_mod.forget_folder_tables()
        self.addCleanup(list_mod.forget_folder_tables)
        extract = list_fetch.list_extract_dir("OtherBot")
        os.makedirs(extract)
        self.main = os.path.join(extract, "OtherBot-2026-10-01.txt")
        self.rar = os.path.join(extract, "OtherBot-RAR-2026-10-01.txt")
        self.write(self.main, foreign_list(random.Random(1128)))
        self.write(self.rar, rar_list())
        self.entry = {"bot": "OtherBot", "fetched_at": 1, "list_path": self.main,
                      "entry_count": 0,
                      "lists": {"": {"list_path": self.main, "entry_count": 0},
                                "rar": {"list_path": self.rar, "entry_count": 0}}}
        config.fetched_bot_lists["otherbot"] = self.entry

    def write(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def page(self, offset, limit, marker="", q=""):
        return webserver.build_fetched_bot_list_payload(
            "OtherBot", offset, limit, list_marker=marker, q=q)

    def whole_file_page(self, *args, **kwargs):
        """The page as it was built before: the whole file parsed."""
        real = list_mod.page_of_list_files
        list_mod.page_of_list_files = lambda *a, **k: None
        try:
            return self.page(*args, **kwargs)
        finally:
            list_mod.page_of_list_files = real

    def spy(self, module, name):
        real = getattr(module, name)
        calls = []

        def counted(*args, **kwargs):
            result = real(*args, **kwargs)
            calls.append((args, result))
            return result

        setattr(module, name, counted)
        self.addCleanup(setattr, module, name, real)
        return calls

    def tables_under(self, bot):
        # With the separator: "otherbot" must not claim "otherbot2"'s files.
        prefix = os.path.normcase(os.path.abspath(list_fetch.list_extract_dir(bot))) + os.sep
        return [sig for sig in list_mod._folder_tables
                if any(os.path.normcase(os.path.abspath(
                    path.replace("\\\\?\\", ""))).startswith(prefix)
                    for path, _m, _s in sig)]


class EveryPageIsTheSameAsTheWholeFileParse(FetchedListCase):

    def test_the_fixture_has_the_foreign_shapes(self):
        status, whole = self.whole_file_page(0, 100000)
        self.assertEqual(status, 200)
        rows = [r for g in whole["folders"] for r in g["entries"]]
        sizes = {r["size"] for r in rows}
        self.assertIn("18.8Mb", sizes, "the dash-size suffix")
        self.assertIn("", sizes, "a row with no size")
        self.assertTrue(any(r["rar_folder"] for r in rows), "a !rar row")
        folders = [g["folder"] for g in whole["folders"]]
        self.assertIn("", folders)
        self.assertIn("D:\\MUSIC\\SHARED\\ALBUM\\", folders)
        self.assertEqual(len(folders), len(set(folders)))

    def test_every_page_of_the_main_list(self):
        answered = self.spy(list_mod, "page_of_list_files")
        total = self.whole_file_page(0, 1)[1]["total"]
        self.assertGreater(total, 40)
        for limit in (1, 3, 25, 200, 0):
            step = limit or total
            for offset in list(range(0, total + 2, step)) + [total + 9]:
                with self.subTest(offset=offset, limit=limit):
                    self.assertEqual(self.page(offset, limit),
                                     self.whole_file_page(offset, limit))
        self.assertTrue(answered)
        self.assertNotIn(None, [result for _a, result in answered])

    def test_every_page_of_the_marker_list(self):
        for offset in range(0, 4):
            for limit in (1, 2, 200):
                with self.subTest(offset=offset, limit=limit):
                    self.assertEqual(self.page(offset, limit, marker="rar"),
                                     self.whole_file_page(offset, limit, marker="rar"))

    def test_a_search_reads_the_whole_file(self):
        answered = self.spy(list_mod, "page_of_list_files")
        self.assertEqual(self.page(0, 50, q="love"),
                         self.whole_file_page(0, 50, q="love"))
        self.assertEqual(answered, [])

    def test_a_page_parses_only_its_own_rows(self):
        self.page(0, 1)
        split = self.spy(list_mod, "_split_entry_line")
        status, page = self.page(4, 3)
        shown = sum(len(g["entries"]) for g in page["folders"])
        self.assertEqual(status, 200)
        self.assertLess(len(split), shown + 30)

    def test_a_missing_file_is_the_same_error(self):
        os.remove(self.main)
        self.assertEqual(self.page(0, 5), self.whole_file_page(0, 5))
        self.assertEqual(self.page(0, 5)[0], 502)


class AnyDoubtFallsBackToTheWholeFile(FetchedListCase):

    def test_a_lone_carriage_return(self):
        with open(self.main, "ab") as handle:
            handle.write(b"\r\n" + RULE.encode() + b"\rD:\\Odd\\\r" + RULE.encode()
                         + b"\r!OtherBot Odd.flac  ::INFO:: 1.00MB\r\n")
        self.assertIsNone(list_mod.page_of_list_files(
            [self.main], 0, 5, "OtherBot", max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS))
        total = self.whole_file_page(0, 1)[1]["total"]
        self.assertEqual(self.page(total - 2, 5), self.whole_file_page(total - 2, 5))

    def test_a_same_size_rewrite_inside_one_tick(self):
        self.page(0, 500)
        st = os.stat(self.main)
        with open(self.main, "rb") as handle:
            data = handle.read()
        self.write(self.main, data.replace(b"Ballad", b"Sonata"))
        os.utime(self.main, ns=(st.st_atime_ns, st.st_mtime_ns))
        for offset in (0, 5, 30):
            with self.subTest(offset=offset):
                self.assertEqual(self.page(offset, 9), self.whole_file_page(offset, 9))


class TheTableIsBuiltUnderTheFetchLock(FetchedListCase):

    def test_the_build_holds_the_fetch_lock(self):
        held = []
        real = list_mod._build_folder_table

        def watching(*args, **kwargs):
            held.append(list_fetch._lock().locked())
            return real(*args, **kwargs)

        list_mod._build_folder_table = watching
        self.addCleanup(setattr, list_mod, "_build_folder_table", real)
        self.page(0, 5)
        self.assertEqual(held, [True])


class RefetchAndPurgeDropTheBotsTables(FetchedListCase):

    def other_bots_table(self):
        extract = list_fetch.list_extract_dir("OtherBot2")
        os.makedirs(extract)
        path = os.path.join(extract, "OtherBot2-2026-10-01.txt")
        self.write(path, foreign_list(random.Random(7), "OtherBot2"))
        self.assertIsNotNone(list_mod.page_of_list_files(
            [path], 0, 5, "OtherBot2", max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS))
        return path

    def test_a_refetch_drops_them(self):
        self.page(0, 5)
        self.page(0, 5, marker="rar")
        self.other_bots_table()
        self.assertEqual(len(self.tables_under("OtherBot")), 2)
        zip_path = os.path.join(self.dir, "incoming.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("OtherBot-2026-10-02.txt", foreign_list(random.Random(9)))

        ok, reason = list_fetch.process_fetched_list_zip("OtherBot", zip_path)

        self.assertTrue(ok, reason)
        self.assertEqual(self.tables_under("OtherBot"), [])
        self.assertEqual(len(self.tables_under("OtherBot2")), 1)
        self.assertEqual(self.page(3, 7), self.whole_file_page(3, 7))

    def test_a_rejected_refetch_drops_them_too(self):
        """The held list is put back by a rename, which is safe to keep a
        table for - but dropping it costs one rebuild and needs no proof."""
        self.page(0, 5)
        zip_path = os.path.join(self.dir, "incoming.zip")
        self.write(zip_path, b"not a zip at all")
        list_fetch.process_fetched_list_zip("OtherBot", zip_path)
        self.assertEqual(self.tables_under("OtherBot"), [])

    def test_a_purge_drops_them(self):
        self.page(0, 5)
        self.other_bots_table()
        self.assertTrue(list_fetch.forget_bot("OtherBot"))
        self.assertEqual(self.tables_under("OtherBot"), [])
        self.assertEqual(len(self.tables_under("OtherBot2")), 1)


class TheTablesAreCapped(FetchedListCase):

    def small_list(self, n):
        path = os.path.join(self.dir, "small-%02d.txt" % n)
        self.write(path, ("%s\nD:\\F%d\\\n%s\n!Bot A%d.flac  ::INFO:: 1MB\n"
                          % (RULE, n, RULE, n)).encode())
        return path

    def test_at_most_the_cap_and_the_least_recent_goes(self):
        paths = [self.small_list(n) for n in range(list_mod._FOLDER_TABLES_KEPT + 3)]
        kw = {"max_rows": list_mod.FILELISTS_MAX_PAGE_ROWS}
        list_mod.page_of_list_files([paths[0]], 0, 1, "Bot", **kw)
        for path in paths[1:list_mod._FOLDER_TABLES_KEPT]:
            list_mod.page_of_list_files([path], 0, 1, "Bot", **kw)
        # The first is used again, so the second is now the oldest.
        list_mod.page_of_list_files([paths[0]], 0, 1, "Bot", **kw)
        list_mod.page_of_list_files([paths[-1]], 0, 1, "Bot", **kw)

        held = {sig[0][0] for sig in list_mod._folder_tables}
        self.assertEqual(len(list_mod._folder_tables), list_mod._FOLDER_TABLES_KEPT)
        self.assertIn(paths[0], held)
        self.assertNotIn(paths[1], held)
        self.assertIn(paths[-1], held)
