"""A peer's archive can hold more than one list, and all of them are kept.

Until now exactly one survived: _pick_list_file() skipped anything matching
the "-rar-"/"-video-" naming conventions and took the largest of what
remained. So a bot offering its albums as a separate RAR list, or its films as
a separate video list, had that half silently discarded on the way in - and an
operator whose peer keeps everything in the second file saw an empty catalogue
for a bot that plainly advertises thousands.

    @SomeBot  ->  SomeBot-Default(2026-01-02)-OS.txt   kept
                  SomeBot-rar(2026-01-02)-OS.txt       thrown away

KEYED ON A MARKER, NEVER THE FILENAME. A peer's list file carries a date, so a
filename key would make every re-fetch a NEW list: the old one orphaned, the
sidebar growing forever, and the freshness LED with nothing stable to compare.
The marker is what is left after the shared prefix, the date and a trailing
"-OS" come off - "rar", "VIDEO", "Default" - and the archive's main list keeps
the EMPTY marker, which is what every reader written before this already
means.

THE MAIN LIST IS UNCHANGED, deliberately. It is chosen by the rule that always
chose it, it is still mirrored in list_path and entry_count at the top of the
entry, and it is still what a bare "@<nick>" is understood to be offering. So
nothing migrates, and nothing that reads a stored entry today has to learn
about any of this.
"""

import io
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
import list_fetch  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "someotherbot"


def list_text(base, count, folder):
    lines = ["Header line\n", "=" * 30 + "\n", folder + "\n", "=" * 30 + "\n"]
    for index in range(count):
        lines.append(f"!{base} Track{index:02d}.flac  ::INFO:: 5000000\n")
    return "".join(lines)


class DerivingTheMarker(unittest.TestCase):
    """The identity half. Pure string work, so it is pinned on its own before
    anything depends on it."""

    def marker(self, names, wanted):
        stems = [os.path.splitext(n)[0] for n in names]
        prefix = list_fetch._shared_list_prefix(stems)
        return list_fetch.list_marker(wanted, prefix)

    def test_our_own_naming_convention(self):
        names = ["SomeBot-2026-09-07.txt", "SomeBot-RAR-2026-09-07.txt",
                 "SomeBot-VIDEO-2026-09-07.txt"]

        self.assertEqual(self.marker(names, names[0]), "")
        self.assertEqual(self.marker(names, names[1]), "RAR")
        self.assertEqual(self.marker(names, names[2]), "VIDEO")

    def test_the_omenserve_naming_convention(self):
        """Dated in parentheses, with a "-OS" trailer that says who built the
        list rather than which list it is."""
        names = ["SomeBot-Default(2026-01-02)-OS.txt",
                 "SomeBot-rar(2026-01-02)-OS.txt"]

        self.assertEqual(self.marker(names, names[0]), "Default")
        self.assertEqual(self.marker(names, names[1]), "rar")

    def test_a_nick_containing_a_hyphen(self):
        """Splitting on the first separator would cut such a name in half,
        which is why the prefix is derived from the files rather than assumed
        from the nick."""
        names = ["Some-Bot-2026-09-07.txt", "Some-Bot-RAR-2026-09-07.txt"]

        self.assertEqual(self.marker(names, names[1]), "RAR")

    def test_the_prefix_is_cut_at_a_separator(self):
        """With "-RAR-" and "-README-" the raw common prefix is "...-R", and
        the markers would come out "AR" and "EADME"."""
        names = ["SomeBot-RAR-2026-09-07.txt", "SomeBot-README-2026-09-07.txt"]

        self.assertEqual(self.marker(names, names[0]), "RAR")
        self.assertEqual(self.marker(names, names[1]), "README")

    def test_names_with_nothing_in_common(self):
        names = ["Music-2026-09-07.txt", "Films-2026-09-07.txt"]

        self.assertEqual(self.marker(names, names[1]), "Films")

    def test_the_date_is_never_part_of_the_identity(self):
        """The whole reason this is not the filename: two rebuilds of the same
        list must be the same list."""
        january = self.marker(["SomeBot-2026-01-02.txt",
                               "SomeBot-rar-2026-01-02.txt"],
                              "SomeBot-rar-2026-01-02.txt")
        september = self.marker(["SomeBot-2026-09-07.txt",
                                 "SomeBot-rar-2026-09-07.txt"],
                                "SomeBot-rar-2026-09-07.txt")

        self.assertEqual(january, september)


class TheArchiveIsKeptWhole(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-archive-")
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

    def two_list_archive(self):
        return self.fetch([
            ("SomeBot-Default(2026-01-02)-OS.txt",
             list_text("SomeBot", 6, "D:\\Music\\Album A")),
            ("SomeBot-rar(2026-01-02)-OS.txt",
             list_text("SomeBot", 3, "D:\\Music\\Album B")),
        ])

    def test_both_lists_are_kept(self):
        """The defect in one assertion: the second used to be discarded."""
        entry = self.two_list_archive()

        self.assertEqual(sorted(entry["lists"]), ["", "rar"])

    def test_each_keeps_its_own_count(self):
        entry = self.two_list_archive()

        self.assertEqual(entry["lists"][""]["entry_count"], 6)
        self.assertEqual(entry["lists"]["rar"]["entry_count"], 3)

    def test_the_main_list_is_still_where_it_always_was(self):
        """Every reader written before an archive could hold more than one
        reads these two fields, so nothing migrates."""
        entry = self.two_list_archive()

        self.assertEqual(entry["entry_count"], 6)
        self.assertEqual(entry["list_path"], entry["lists"][""]["list_path"])

    def test_a_single_list_archive_is_unchanged(self):
        """The ordinary case. Deriving a marker from one filename would find
        one - there is nothing to contrast it against, so the date reads as
        distinguishing - and invent a sub-list the archive does not have."""
        entry = self.fetch([("SomeBot-2026-09-07.txt",
                             list_text("SomeBot", 4, "D:\\Music\\Album A"))])

        self.assertEqual(list(entry["lists"]), [""])
        self.assertEqual(entry["entry_count"], 4)

    def test_a_second_file_with_no_entries_is_not_a_list(self):
        """A .txt with no request lines is a readme, a banner or a header. A
        sidebar row for one opens on nothing, which is the noise this change
        is otherwise removing.

        The MAIN list is exempt - it is the archive's identity, and an empty
        one is a fact about that bot worth seeing rather than a file to
        ignore."""
        entry = self.fetch([
            ("SomeBot-2026-09-07.txt", list_text("SomeBot", 5, "D:\\A")),
            ("SomeBot-rar-2026-09-07.txt", "just a banner, no entries\n"),
        ])

        self.assertEqual(list(entry["lists"]), [""])
        self.assertEqual(entry["entry_count"], 5)

    def test_a_second_list_that_cannot_be_read_costs_only_itself(self):
        """The main list is parsed, counted and indexed before this runs.
        Failing the whole fetch would throw away a list that is sitting there,
        correct."""
        real_getsize = os.path.getsize

        def explode_on_the_second(path):
            if "rar" in os.path.basename(path).lower():
                raise OSError("gone since the fetch")
            return real_getsize(path)

        entry = self.fetch([
            ("SomeBot-2026-09-07.txt", list_text("SomeBot", 5, "D:\\A")),
            ("SomeBot-rar-2026-09-07.txt", list_text("SomeBot", 2, "D:\\B")),
        ])
        self.assertEqual(sorted(entry["lists"]), ["", "rar"])

        os.path.getsize = explode_on_the_second
        self.addCleanup(setattr, os.path, "getsize", real_getsize)
        entry = self.fetch([
            ("SomeBot-2026-09-08.txt", list_text("SomeBot", 5, "D:\\A")),
            ("SomeBot-rar-2026-09-08.txt", list_text("SomeBot", 2, "D:\\B")),
        ])

        self.assertEqual(list(entry["lists"]), [""],
                         "an unreadable second list must cost only itself")
        self.assertEqual(entry["entry_count"], 5)

    def test_a_refetch_replaces_the_same_lists_rather_than_adding_to_them(self):
        """The marker is stable across rebuilds, so the January archive and
        the September one are the same two lists."""
        self.two_list_archive()
        entry = self.fetch([
            ("SomeBot-Default(2026-09-07)-OS.txt",
             list_text("SomeBot", 9, "D:\\Music\\Album A")),
            ("SomeBot-rar(2026-09-07)-OS.txt",
             list_text("SomeBot", 4, "D:\\Music\\Album B")),
        ])

        self.assertEqual(sorted(entry["lists"]), ["", "rar"])
        self.assertEqual(entry["lists"]["rar"]["entry_count"], 4)


class TheArchiveIsBounded(DCCoreTestCase):
    """A peer's zip is untrusted, and "keep exactly one" is what bounded this
    before. Without a ceiling, an archive of hundreds of small .txt files is
    hundreds of parses, sidebar rows and index writes - all comfortably under
    the existing byte cap."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-archive-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp

    def test_only_so_many_lists_are_kept(self):
        path = os.path.join(self.tmp, "many.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for index in range(40):
                archive.writestr(f"SomeBot-part{index:02d}-2026-09-07.txt",
                                 list_text("SomeBot", 2, "D:\\A"))

        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)

        self.assertTrue(ok, reason)
        kept = config.fetched_bot_lists[BOT]["lists"]
        self.assertLessEqual(len(kept), list_fetch.MAX_LISTS_PER_ARCHIVE)
        self.assertGreater(len(kept), 1, "the cap must not collapse to one")


class TheIndexNamesTheList(unittest.TestCase):
    """Re-fetching must replace one list's rows, not the whole bot's, and the
    cross-list filter should be able to say which list a match came from."""

    def test_the_main_list_keeps_the_bare_nick(self):
        """The name it has always had, so an index written before archives
        could hold more than one still resolves."""
        self.assertEqual(list_fetch.index_key("SomeBot", ""), "SomeBot")

    def test_another_list_is_named_under_it(self):
        self.assertEqual(list_fetch.index_key("SomeBot", "rar"), "SomeBot/rar")

    def test_the_key_splits_back(self):
        self.assertEqual(list_fetch.split_index_key("SomeBot/rar"),
                         ("SomeBot", "rar"))
        self.assertEqual(list_fetch.split_index_key("SomeBot"),
                         ("SomeBot", ""))


class TheSidebarShowsEachOne(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-archive-rows-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        config.FETCHED_FILES_DIR = self.tmp
        path = os.path.join(self.tmp, "incoming.zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("SomeBot-Default(2026-01-02)-OS.txt",
                             list_text("SomeBot", 6, "D:\\Music\\Album A"))
            archive.writestr("SomeBot-rar(2026-01-02)-OS.txt",
                             list_text("SomeBot", 3, "D:\\Music\\Album B"))
        ok, reason = list_fetch.process_fetched_list_zip(BOT, path)
        self.assertTrue(ok, reason)

    def held_rows(self):
        return [row for row in webserver.build_fetched_bot_list_summaries()
                if row.get("held")]

    def test_one_row_per_list(self):
        self.assertEqual([row["bot"] for row in self.held_rows()],
                         [BOT, f"{BOT}/rar"])

    def test_the_main_list_keeps_the_bare_nick_as_its_row(self):
        """So a source stored before this - a bookmark, a selected row - still
        resolves to the same list."""
        self.assertEqual(self.held_rows()[0]["bot"], BOT)

    def test_every_row_carries_the_nick_for_acting_on_the_bot(self):
        """Re-fetching, packing a folder and the fetch box all address the
        BOT. Sending "<nick>/<marker>" to any of them names a bot that does
        not exist."""
        for row in self.held_rows():
            self.assertEqual(row["nick"], BOT)

    def test_each_row_carries_its_own_count(self):
        counts = {row["bot"]: row["count"] for row in self.held_rows()}

        self.assertEqual(counts[BOT], 6)
        self.assertEqual(counts[f"{BOT}/rar"], 3)

    def test_freshness_is_per_bot_not_per_list(self):
        """One advert covers the archive and one @<nick> fetches all of it, so
        every list a bot published is exactly as fresh as the fetch."""
        verdicts = {row["freshness"] for row in self.held_rows()}

        self.assertEqual(len(verdicts), 1)

    def test_browsing_the_second_list_returns_its_own_files(self):
        status, payload = webserver.build_fetched_bot_list_payload(
            BOT, list_marker="rar")
        rows = [row for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 3)

    def test_browsing_without_a_marker_returns_the_main_list(self):
        status, payload = webserver.build_fetched_bot_list_payload(BOT)
        rows = [row for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

        self.assertEqual(status, 200)
        self.assertEqual(len(rows), 6)

    def test_an_unknown_marker_falls_back_to_the_main_list(self):
        """The sidebar is polled continuously and a peer's next archive need
        not carry the same lists as the last."""
        _status, payload = webserver.build_fetched_bot_list_payload(
            BOT, list_marker="no-such-list")
        rows = [row for group in (payload.get("folders") or [])
                for row in group.get("entries", [])]

        self.assertEqual(len(rows), 6)


class AnEntryStoredBeforeThisStillWorks(DCCoreTestCase):
    """No migration: an entry written by an older build has no "lists" key at
    all, and must still render and browse."""

    def test_it_gets_the_single_row_it_always_had(self):
        config.fetched_bot_lists[BOT] = {
            "bot": BOT, "fetched_at": 1, "list_path": "nowhere.txt",
            "entry_count": 12, "source_zip": "old.zip",
            "advert_when_fetched": {},
        }

        rows = [row for row in webserver.build_fetched_bot_list_summaries()
                if row.get("held")]

        self.assertEqual([row["bot"] for row in rows], [BOT])
        self.assertEqual(rows[0]["count"], 12)


if __name__ == "__main__":
    unittest.main()
