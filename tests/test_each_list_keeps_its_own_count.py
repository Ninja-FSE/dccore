"""With two lists, each keeps its own cached count, and -que/-stats report it (#1270).

THE CACHE. count_request_lines() reads every published list end to end - 2.4 s
on a 5.4M-file library - so the count is cached against the files' path,
mtime and size (#457/#1123). It held ONE entry and cleared it before storing
the next. With two lists the advert asks for each in turn, so Music evicted
Films and Films evicted Music: every advert cycle and every Stats poll read
every list again, on any install with more than one.

THE COMMANDS. -que (with nothing queued) and -stats asked for the count with
no list name, which is the primary. In a channel bound to another list the
advert reported that list's figures and the commands the primary's, so one
bot answered two library sizes in one channel.
"""

import builtins
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

MUSIC_ROWS = 31
FILM_ROWS = 7


class TwoLists(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        music = os.path.join(self.tree.root, "Music")
        films = os.path.join(self.tree.root, "Films")
        for path in (music, films):
            os.makedirs(path, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="alfa",
                        NICKNAME="alfa", ORIGINAL_NICK="alfa",
                        CHANNEL="#alfa-music,#alfa-films",
                        LISTS_FILE=os.path.join(self.tree.root, "lists.json"))
        library.save_lists([
            library.ServedList(name="Music", primary=True, channels=("#alfa-music",),
                               folders=(library.Folder("Music", music),)),
            library.ServedList(name="Films", primary=False, channels=("#alfa-films",),
                               folders=(library.Folder("Films", films),)),
        ])
        films_dir = list_mod.list_dir("Films")
        os.makedirs(films_dir, exist_ok=True)
        self.list_files = []
        for directory, rows in ((self.tree.lists, MUSIC_ROWS), (films_dir, FILM_ROWS)):
            path = os.path.join(directory, "alfa-2026-10-01.txt")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("List of x Files\n\n" + "=" * 20 + "\nD:\\MEDIA\\X\\\n"
                             + "=" * 20 + "\n")
                for index in range(rows):
                    handle.write(f"!alfa track{index}.flac  ::INFO:: 1.0MB\n")
            self.list_files.append(os.path.abspath(path))
        list_mod._count_cache.clear()
        self.addCleanup(list_mod._count_cache.clear)


class TheCountIsCachedPerList(TwoLists):

    def count_reads(self):
        """Every open of a published list, while the test runs."""
        reads = []
        real = builtins.open

        def spy(path, *args, **kwargs):
            if os.path.abspath(str(path)) in self.list_files:
                reads.append(path)
            return real(path, *args, **kwargs)

        builtins.open = spy
        self.addCleanup(setattr, builtins, "open", real)
        return reads

    def test_alternating_lists_read_each_once(self):
        reads = self.count_reads()

        # What the advert loop does each cycle: one call per served channel.
        for _cycle in range(5):
            self.assertEqual(list_mod.get_file_count_date_size_and_raw_bytes("Music")[0],
                             MUSIC_ROWS)
            self.assertEqual(list_mod.get_file_count_date_size_and_raw_bytes("Films")[0],
                             FILM_ROWS)

        self.assertEqual(len(reads), 2, "the lists evict each other's count")

    def test_a_changed_list_is_recounted_and_the_other_is_not(self):
        list_mod.get_file_count_date_size_and_raw_bytes("Music")
        list_mod.get_file_count_date_size_and_raw_bytes("Films")
        with io.open(self.list_files[1], "a", encoding="utf-8") as handle:
            handle.write("!alfa extra.flac  ::INFO:: 1.0MB\n")
        reads = self.count_reads()

        self.assertEqual(list_mod.get_file_count_date_size_and_raw_bytes("Films")[0],
                         FILM_ROWS + 1)
        self.assertEqual(list_mod.get_file_count_date_size_and_raw_bytes("Music")[0],
                         MUSIC_ROWS)
        self.assertEqual(len(reads), 1)

    def test_the_cache_is_bounded(self):
        """A caller passing a new path set each time must not grow it for ever."""
        for index in range(list_mod._COUNT_SLOTS_KEPT + 10):
            path = os.path.join(self.tree.root, f"extra{index}.txt")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("!alfa one.flac\n")
            list_mod.count_request_lines([path])

        self.assertLessEqual(len(list_mod._count_cache), list_mod._COUNT_SLOTS_KEPT)


class TheCommandsReportTheChannelsList(TwoLists):

    def told(self, handler, target):
        self.oserve.queued.clear()
        handler(None, "bravo", target)
        codes = re.compile("[" + chr(2) + chr(15) + "]|" + chr(3) + r"\d{0,2}(,\d{1,2})?")
        return " ".join(codes.sub("", message) for _user, message, _vip in self.oserve.queued)

    def test_stats_in_the_films_channel(self):
        text = self.told(commands.handle_stats_request, "#alfa-films")

        self.assertRegex(text, rf"\b{FILM_ROWS}\b")
        self.assertNotRegex(text, rf"\b{MUSIC_ROWS}\b")

    def test_stats_in_the_music_channel_and_in_private(self):
        for target in ("#alfa-music", "alfa"):
            with self.subTest(target=target):
                text = self.told(commands.handle_stats_request, target)
                self.assertRegex(text, rf"\b{MUSIC_ROWS}\b")

    def test_que_with_nothing_queued_in_the_films_channel(self):
        text = self.told(commands.handle_queue_check, "#alfa-films")

        self.assertRegex(text, rf"\b{FILM_ROWS}\b")
        self.assertNotRegex(text, rf"\b{MUSIC_ROWS}\b")

    def test_que_in_the_music_channel(self):
        text = self.told(commands.handle_queue_check, "#alfa-music")

        self.assertRegex(text, rf"\b{MUSIC_ROWS}\b")


if __name__ == "__main__":
    unittest.main()
