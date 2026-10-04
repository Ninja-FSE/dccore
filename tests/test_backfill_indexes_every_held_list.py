"""Backfill indexes every list a held archive has, not just its main one (#1122).

A fetched archive can hold several lists - the main one, and others such as a
RAR list - and a fetch indexes each under its own name: the bare nick for the
main list, "<nick>/<marker>" for the rest. When the index was emptied (an
upgrade, a deleted file) or repaired (a damaged one moved aside),
backfill_missing() went through the main list alone. The others were never
indexed again, and the filter bar then showed a list that does match as
holding nothing - the false claim this module refuses to make everywhere else.
"""

import io
import os

from tests import support  # noqa: F401  (path setup)

import list_index  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_crosslist_search as crosslist  # noqa: E402


class EveryHeldList(crosslist.IndexCase):
    def list_file(self, name, nick, *filenames):
        path = os.path.join(self.index_dir, f"{name}.txt")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(f"List of {len(filenames)} Files\n\n" + "=" * 20 + "\n")
            handle.write("D:\\MUSIC\\Some Folder\\\n" + "=" * 20 + "\n")
            for filename in filenames:
                handle.write(f"!{nick} {filename}  ::INFO:: 4.00MB\n")
        return path

    def archive(self):
        """What list_fetch stores for a bot whose archive held two lists."""
        main = self.list_file("main", "PackBot", "Song One.flac")
        rar = self.list_file("rar", "PackBot", "Whole Album (2001)")
        return {"packbot": {"bot": "PackBot", "fetched_at": 1, "list_path": main,
                            "lists": {"": {"list_path": main, "entry_count": 1, "file_name": "main.txt"},
                                      "rar": {"list_path": rar, "entry_count": 1, "file_name": "rar.txt"}}}}

    def test_the_other_lists_are_indexed_too(self):
        done = list_index.backfill_missing(self.archive(), log=lambda _m: None)
        self.assertEqual(done, 2)
        matched, empty = list_index.bots_with_a_match(["album"], ["PackBot", "PackBot/rar"])
        self.assertEqual((matched, empty), ({"packbot/rar"}, {"packbot"}))
        matched, _empty = list_index.bots_with_a_match(["song"], ["PackBot", "PackBot/rar"])
        self.assertEqual(matched, {"packbot"})

    def test_a_list_already_indexed_is_not_read_again(self):
        held = self.archive()
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 2)
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 0)

    def test_only_the_missing_one_is_read(self):
        """After a fetch indexed the main list but the RAR list's rows were lost."""
        held = self.archive()
        list_index.backfill_missing(held, log=lambda _m: None)
        list_index.drop_bot("PackBot/rar")
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)
        self.assertEqual(list_index.bots_with_a_match(["album"], ["PackBot/rar"])[0], {"packbot/rar"})

    def test_an_entry_from_before_archives_held_several_lists_still_works(self):
        main = self.list_file("old", "OldBot", "Old Song.flac")
        held = {"oldbot": {"bot": "OldBot", "fetched_at": 1, "list_path": main}}
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)
        self.assertEqual(list_index.bots_with_a_match(["old"], ["OldBot"])[0], {"oldbot"})

    def test_a_list_whose_file_is_gone_is_skipped_and_the_rest_go_on(self):
        held = self.archive()
        os.remove(held["packbot"]["lists"]["rar"]["list_path"])
        self.assertEqual(list_index.backfill_missing(held, log=lambda _m: None), 1)
