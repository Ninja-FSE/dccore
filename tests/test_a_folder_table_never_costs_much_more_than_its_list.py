"""A folder table never costs much more than the list it indexes (#1128).

The List Browser pages from a folder table (list.page_of_list_files()), built
once per version of a list. Its first version kept, for every run of rows
under one heading, a set and then a frozenset of the rows dedup drops, even
for runs no group shows. A fetched list is written by somebody else, and the
#1128 audit made one under the 128 MB text ceiling that the table could not
afford: two headings alternating with one row each, or millions of copies of
one row. 16.8 MB of list made a table that held 526 MB (995 MB at its peak)
and took 21 s to build, all of it under list_fetch's lock and kept in the
cache for as long as the list was. The whole-list parse it replaced held 2 MB.

So the dropped rows are kept as [start, stop) spans in one flat array, only
for the runs a group shows, and the build counts what it stores against a
budget proportional to the list's size, with a floor. Past it, the build
gives up and keeps nothing, and the list's pages are read whole, as they
were before there was a table - the same pages, only slower. Memory here is
measured by what the table stores (table.nbytes(), the items of its arrays),
never by the process's size, which is neither exact nor stable on a runner.
"""

import os
import random

from tests.support import DCCoreTestCase

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import webserver  # noqa: E402

RULE = "=" * 40
KW = {"max_rows": list_mod.FILELISTS_MAX_PAGE_ROWS}

# The audit's two shapes, by the line.
ALTERNATING = b"=\nA\n=\n!a x\n=\nB\n=\n!a y\n"
ONE_ROW = b"!a x\n"


def whole_list_page(path, offset, limit, source="SomeBot"):
    """The page as the whole-list parse gives it - the fallback, and the
    reference every table page must equal."""
    entries, _total = list_mod.find_matching_entries([], limit=None, list_path=path)
    rows = list_mod.entries_to_filelist_rows(entries, source)
    groups = list_mod.group_rows_by_folder(rows)
    return list_mod.page_folder_groups(groups, offset, limit, **KW)


def normal_list(rng, folders):
    """A list as a serving bot writes it: folders of a few to a few dozen
    rows, now and then a file listed twice."""
    out = ["List of many Files", ""]
    for n in range(folders):
        out += [RULE, "D:\\MUSIC\\Artist %d\\Album %d (%d)\\" % (n % 97, n, 1960 + n % 60), RULE]
        for track in range(rng.randint(3, 30)):
            out.append("!SomeBot Artist %d - Track %02d - Title %d.flac  ::INFO:: %d.%02dMB"
                       % (n % 97, track, rng.randint(1, 99999), rng.randint(1, 80),
                          rng.randint(0, 99)))
            if rng.random() < 0.02:
                out.append(out[-1])
    return ("\n".join(out) + "\n").encode("utf-8")


def duplicate_heavy_list(rng):
    """Few headings, repeated and in both cases, over rows that repeat in
    spans and one by one: every way the build can record a dropped row."""
    out = ["!SomeBot Loose.mp3  ::INFO:: 1.00MB"] * 3
    headings = ["D:\\A\\", "D:\\a\\", "D:\\B\\", "D:\\C\\", "D:\\c\\"]
    for _ in range(rng.randint(20, 60)):
        out += [RULE, rng.choice(headings), RULE]
        for _ in range(rng.randint(1, 12)):
            name = "Song %d" % rng.randint(1, 6)
            row = "!SomeBot %s.flac  ::INFO:: %dMB" % (
                rng.choice([name, name.upper()]), rng.randint(1, 2))
            out += [row] * rng.choice([1, 1, 2, 5])
    return ("\n".join(out) + "\n").encode("utf-8")


class TableCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.dir = self.make_temp_dir(prefix="dccore-table-budget-")
        list_mod.forget_folder_tables()
        self.addCleanup(list_mod.forget_folder_tables)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def build(self, path, budget=None):
        """The table of `path`, built here and not cached, or None."""
        signature = list_mod._list_signature([path])
        if budget is None:
            return list_mod._build_folder_table([path], signature)
        try:
            return list_mod._build_folder_table_within([path], signature, budget)
        except list_mod._OverBudget:
            return None

    def page(self, path, offset, limit):
        return list_mod.page_of_list_files([path], offset, limit, "SomeBot", **KW)

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

    def assert_every_page_is_the_whole_list_parse(self, path):
        total = whole_list_page(path, 0, 1)[1]
        for limit in (1, 3, 50, 0):
            step = limit or total or 1
            for offset in list(range(0, total + 2, step)):
                with self.subTest(offset=offset, limit=limit):
                    self.assertEqual(self.page(path, offset, limit),
                                     whole_list_page(path, offset, limit))


class TheBudgetIsTheListsSizeWithAFloor(TableCase):

    def test_half_the_size_and_never_under_the_floor(self):
        floor = list_mod._FOLDER_TABLE_BUDGET_FLOOR
        self.assertEqual(list_mod._folder_table_budget((("a", 1, 10),)), floor)
        big = 8 * floor
        self.assertEqual(list_mod._folder_table_budget((("a", 1, big),)), big // 2)
        self.assertEqual(list_mod._folder_table_budget(
            (("a", 1, big), ("b", 1, big))), big)

    def test_a_table_never_holds_more_than_the_build_counted(self):
        """The build's count bounds what the table keeps: with a budget one
        byte under what a table keeps, the same list gives no table."""
        rng = random.Random(1128)
        lists = [normal_list(rng, 40), duplicate_heavy_list(rng),
                 duplicate_heavy_list(rng), ALTERNATING * 50,
                 b"=\nA\n=\n" + ONE_ROW * 500 + b"=\na\n=\n" + ONE_ROW * 3 + b"!b y\n",
                 # Many spans in one run, from its own dedup and from the
                 # second one across a case twin.
                 b"=\nA\n=\n" + b"".join(b"!a %d\n!a %d\n" % (n, n) for n in range(300)),
                 b"=\nA\n=\n" + b"".join(b"!a %d\n" % n for n in range(0, 600, 2))
                 + b"=\na\n=\n" + b"".join(b"!a %d\n" % n for n in range(600))]
        for n, data in enumerate(lists):
            with self.subTest(list=n):
                path = self.write("list-%d.txt" % n, data)
                table = self.build(path, budget=1 << 40)
                self.assertGreater(table.nbytes(), 0)
                self.assertIsNone(self.build(path, budget=table.nbytes() - 1))


class TheAuditsListsAreReadWhole(TableCase):
    """Over the budget the build gives up, keeps nothing, and the pages are
    the whole-list parse's, as before the table."""

    def test_two_headings_alternating_give_no_table(self):
        path = self.write("alternating.txt", ALTERNATING * 50000)   # 1.2 MB
        rows = 2 * 50000
        split = self.spy("_split_entry_line")

        self.assertIsNone(self.build(path))

        # It gave up as soon as it was over, not at the end of the list.
        self.assertLess(len(split), rows // 2)

    def test_a_list_over_the_budget_is_paged_as_the_whole_list_parse(self):
        path = self.write("alternating.txt", ALTERNATING * 50000)
        builds = self.spy("_build_folder_table")
        for offset, limit in ((0, 200), (1, 1), (0, 0), (5, 3)):
            with self.subTest(offset=offset, limit=limit):
                self.assertIsNone(self.page(path, offset, limit))
        # Once: the None is kept for the list as it is, not built again for
        # every page, and nothing half-built is kept with it.
        self.assertEqual(len(builds), 1)
        self.assertEqual(list(list_mod._folder_tables.values()), [None])

    def test_alternating_headings_with_unique_rows_give_no_table(self):
        """The skeptic's variant: no duplicate at all, and still 4.5 times
        the list's size in runs and groups."""
        data = b"".join(b"=\nA\n=\n!a %d\n=\nB\n=\n!b %d\n" % (n, n) for n in range(50000))
        path = self.write("unique.txt", data)
        self.assertGreater(len(data), list_mod._FOLDER_TABLE_BUDGET_FLOOR)
        self.assertIsNone(self.build(path))

    def test_millions_of_copies_of_one_row_are_one_span(self):
        """One heading over copies of one row: a table of a few dozen bytes,
        where the set of their positions held 243 MB at 16.8 MB of list."""
        copies = 300000
        path = self.write("copies.txt", b"=\nA\n=\n" + ONE_ROW * copies)
        table = self.build(path)
        self.assertIsNotNone(table)
        self.assertEqual(list(table.dup_spans), [1, copies])
        self.assertLess(table.nbytes(), 100)
        self.assertEqual(self.page(path, 0, 5), whole_list_page(path, 0, 5))

    def test_a_run_no_group_shows_keeps_no_spans(self):
        """Every run after the first two is all duplicates of a run before
        it, so no group shows them and nothing of theirs is kept."""
        path = self.write("dead.txt", b"=\nA\n=\n!a x\n!a x\n=\nB\n=\n!a y\n" * 2000)
        table = self.build(path)
        self.assertIsNotNone(table)
        self.assertEqual(len(table.group_first), 2)
        self.assertEqual(list(table.dup_runs), [0])
        self.assertEqual(list(table.dup_spans), [1, 2])
        self.assert_every_page_is_the_whole_list_parse(path)

    def test_the_fetched_list_page_is_the_same(self):
        """Through get_fetched_bot_page(), as the List Browser asks for it."""
        self.set_config(FETCHED_FILES_DIR=self.dir, fetched_bot_lists={})
        extract = list_fetch.list_extract_dir("OtherBot")
        os.makedirs(extract)
        path = os.path.join(extract, "OtherBot-2026-10-04.txt")
        with open(path, "wb") as handle:
            handle.write(ALTERNATING * 50000)
        config.fetched_bot_lists["otherbot"] = {
            "bot": "OtherBot", "fetched_at": 1, "list_path": path, "entry_count": 0,
            "lists": {"": {"list_path": path, "entry_count": 0}}}
        answered = self.spy("page_of_list_files")
        got = webserver.build_fetched_bot_list_payload("OtherBot", 0, 200)
        real = list_mod.page_of_list_files
        list_mod.page_of_list_files = lambda *a, **k: None
        try:
            whole = webserver.build_fetched_bot_list_payload("OtherBot", 0, 200)
        finally:
            list_mod.page_of_list_files = real
        self.assertEqual(got, whole)
        self.assertEqual(got[0], 200)
        self.assertEqual(got[1]["total"], 2)
        self.assertEqual([result for _args, result in answered], [None])


class ANormalListStillGetsItsTable(TableCase):

    def test_a_normal_list_is_served_from_its_table(self):
        path = self.write("normal.txt", normal_list(random.Random(60), 2000))
        size = os.path.getsize(path)
        self.assertGreater(size, 2 * list_mod._FOLDER_TABLE_BUDGET_FLOOR,
                           "the share, not the floor, is the budget here")
        table = self.build(path)
        self.assertIsNotNone(table)
        self.assertEqual(len(table.group_first), 2000)
        # Far under its budget, so a normal list is nowhere near falling back.
        self.assertLess(table.nbytes(), size // 20)

        answered = self.spy("page_of_list_files")
        for offset, limit in ((0, 200), (1000, 200), (1990, 50)):
            with self.subTest(offset=offset):
                got = self.page(path, offset, limit)
                self.assertIsNotNone(got)
                self.assertEqual(got, whole_list_page(path, offset, limit))
        self.assertTrue(answered)
        self.assertNotIn(None, [result for _args, result in answered])

    def test_duplicate_heavy_lists_page_the_same(self):
        """Spans from a run's own dedup and from the second one across runs
        that share a lower-cased heading, contiguous and scattered."""
        rng = random.Random(1186)
        for n in range(6):
            with self.subTest(list=n):
                list_mod.forget_folder_tables()
                path = self.write("dups-%d.txt" % n, duplicate_heavy_list(rng))
                table = self.build(path)
                self.assertIsNotNone(table)
                self.assertTrue(table.dup_spans)
                self.assert_every_page_is_the_whole_list_parse(path)
