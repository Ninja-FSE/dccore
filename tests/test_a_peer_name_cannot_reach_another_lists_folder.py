"""A peer's nick, a channel name, or a file name a peer offers never lands
on a directory that belongs to somebody else.

Every fetched list is extracted under <FETCHED_FILES_DIR>/lists/, into a
directory named after the bot (and, for a secondary channel's list, after the
channel under lists/_channels/<bot>/). The names come from other people, and
three ways for them to reach the wrong directory were found together:

- A bot nicked "_channels" was given lists/_channels as its own directory -
  the folder every bot's secondary-channel lists live in. Fetching its list
  held that folder aside and deleted it on success; forgetting the bot
  deleted it outright.
- Cleaning a name for the file system is not one-to-one. "alfa|x" and
  "alfa_x" are two nicks and both cleaned to "alfa_x", one directory between
  them, so a fetch from either replaced the other's list. Two channels did
  the same under one bot ("#a|b", "#a_b").
- A file offered as "lists" by a bot we had asked for a folder became the
  plain file FETCHED_FILES_DIR/lists on an install with no list fetched yet,
  and every list fetch after it failed.

A name that needed changing, that DCCore uses itself, or that already looks
like a tagged name now carries a short digest of the real one. An ordinary
nick keeps exactly the directory it had, and a held list whose directory name
changed is moved once at startup (migrate_held_list_directories()).
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402

BS = chr(92)
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_list_fetch import _list_txt, _write_zip  # noqa: E402


class HeldListsCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = self.make_temp_dir(prefix="dccore-peer-dirs-")
        self.set_config(FETCHED_FILES_DIR=self.tmp)
        self.zips = 0

    def fetch(self, bot, channel="#music", secondary=False, title="Song.flac",
              list_name=None):
        self.zips += 1
        path = os.path.join(self.tmp, "upload-%d.zip" % self.zips)
        _write_zip(path, [(list_name or "alfa-2026-10-01.txt",
                           _list_txt(base_name="alfa", files=((title, "10.0MB"),)))])
        ok, reason = list_fetch.process_fetched_list_zip(bot, path, channel=channel,
                                                         secondary=secondary)
        self.assertTrue(ok, reason)

    @staticmethod
    def titles(entry, marker=None):
        if marker is not None:
            entry = dict(entry, **entry["lists"][marker])
        groups, _folders, _rows, _capped, error = list_fetch.get_fetched_bot_page(entry, 0, 50)
        return [row["title"] for group in groups for row in group["entries"]], error


class ABotNamedLikeTheSecondaryFolder(HeldListsCase):

    def setUp(self):
        super().setUp()
        self.fetch("alfa")
        self.fetch("alfa", channel="#video", secondary=True, title="Clip.mkv")
        self.video = config.fetched_bot_lists["alfa"]["lists"]["video"]["list_path"]
        self.assertTrue(os.path.exists(self.video))

    def test_its_own_directory_is_not_the_secondary_folder(self):
        channels_root = os.path.join(os.path.abspath(self.tmp), "lists", "_channels")
        mine = list_fetch.list_extract_dir("_channels")

        self.assertNotEqual(os.path.normcase(mine), os.path.normcase(channels_root))
        self.assertFalse(os.path.normcase(channels_root).startswith(
            os.path.normcase(mine) + os.sep))

    def test_fetching_its_list_leaves_every_other_bots_secondary_lists(self):
        self.fetch("_channels")

        self.assertTrue(os.path.exists(self.video))
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa"], "video"),
                         (["Clip.mkv"], None))

    def test_forgetting_it_leaves_them_too(self):
        self.fetch("_channels")

        self.assertTrue(list_fetch.forget_bot("_channels"))

        self.assertTrue(os.path.exists(self.video))

    def test_any_spelling_of_it_is_kept_out(self):
        channels_root = os.path.normcase(os.path.join(os.path.abspath(self.tmp), "lists", "_channels"))
        for nick in ("_Channels", "_CHANNELS"):
            with self.subTest(nick=nick):
                self.assertNotEqual(os.path.normcase(list_fetch.list_extract_dir(nick)),
                                    channels_root)


class ANameEndingLikeAHeldCopy(HeldListsCase):

    def test_it_is_never_another_directorys_held_copy(self):
        """_hold_existing_list() sets "<dir>" aside as "<dir>.previous"."""
        for bot in ("alfa", "bravo"):
            with self.subTest(bot=bot):
                held = list_fetch.list_extract_dir(bot) + ".previous"
                self.assertNotEqual(os.path.normcase(list_fetch.list_extract_dir(bot + ".previous")),
                                    os.path.normcase(held))
        self.assertNotEqual(
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "#video.previous")),
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "#video") + ".previous"))


class TwoNicksThatCleanAlike(HeldListsCase):

    def test_they_get_two_directories(self):
        self.assertNotEqual(os.path.normcase(list_fetch.list_extract_dir("alfa|x")),
                            os.path.normcase(list_fetch.list_extract_dir("alfa_x")))

    def test_a_fetch_from_one_leaves_the_others_list(self):
        self.fetch("alfa|x", title="Real.flac")
        self.fetch("alfa_x", title="Other.flac")

        self.assertEqual(self.titles(config.fetched_bot_lists["alfa|x"]), (["Real.flac"], None))
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa_x"]), (["Other.flac"], None))

    def test_a_nick_spelled_like_a_tagged_directory_cannot_take_it(self):
        tagged = list_fetch._sanitize_bot_dir_name("alfa|x")
        self.assertNotEqual(tagged.lower(), "alfa_x")

        self.assertNotEqual(list_fetch._sanitize_bot_dir_name(tagged).lower(), tagged.lower())
        self.assertNotEqual(list_fetch._sanitize_bot_dir_name(tagged.upper()).lower(),
                            tagged.lower())

    def test_one_nick_in_any_case_is_still_one_directory(self):
        self.assertEqual(os.path.normcase(list_fetch.list_extract_dir("ALFA|X")),
                         os.path.normcase(list_fetch.list_extract_dir("alfa|x")))

    def test_an_ordinary_nick_keeps_its_directory(self):
        for nick in ("alfa", "Bravo", "Bot[EU]", "alfa-2", "zulu^`{x}"):
            with self.subTest(nick=nick):
                self.assertEqual(os.path.basename(list_fetch.list_extract_dir(nick)),
                                 nick.lower())


class TwoChannelsThatCleanAlike(HeldListsCase):

    def test_they_get_two_directories(self):
        self.assertNotEqual(
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "#a|b")),
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "#a_b")))
        self.assertNotEqual(
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "##a")),
            os.path.normcase(list_fetch.secondary_channel_extract_dir("alfa", "#a")))

    def test_each_marker_reads_its_own_channels_list(self):
        self.fetch("alfa")
        self.fetch("alfa", channel="#a|b", secondary=True, title="FromPipe.mkv")
        self.fetch("alfa", channel="#a_b", secondary=True, title="FromUnderscore.mkv")

        lists = config.fetched_bot_lists["alfa"]["lists"]
        by_channel = {info["channel"]: marker for marker, info in lists.items()}
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa"], by_channel["#a|b"]),
                         (["FromPipe.mkv"], None))
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa"], by_channel["#a_b"]),
                         (["FromUnderscore.mkv"], None))

    def test_an_ordinary_channel_keeps_its_directory(self):
        self.assertEqual(os.path.basename(list_fetch.secondary_channel_extract_dir("alfa", "#Video")),
                         "video")


def plain(path):
    """A stored list_path without the Windows long-path prefix it can carry."""
    text = str(path)
    prefix = BS * 2 + "?" + BS
    return text[len(prefix):] if text.startswith(prefix) else text


class AHeldListMovesToItsNewName(HeldListsCase):
    """A list held from before keeps its files under the old name until
    startup moves them. Made here by fetching under the new name and putting
    the folder back where the old naming had it."""

    def back_to_old_name(self, key, old_dir, new_dir):
        os.makedirs(os.path.dirname(old_dir), exist_ok=True)
        os.rename(new_dir, old_dir)
        entry = config.fetched_bot_lists[key]
        for record in [entry] + list(entry["lists"].values()):
            path = plain(record["list_path"])
            if os.path.normcase(path).startswith(os.path.normcase(new_dir) + os.sep):
                record["list_path"] = old_dir + path[len(new_dir):]

    def lists_root(self, *parts):
        return os.path.join(os.path.abspath(self.tmp), "lists", *parts)

    def test_a_primary_list_is_moved_and_still_reads(self):
        self.fetch("alfa|x", title="Real.flac")
        new_dir = list_fetch.list_extract_dir("alfa|x")
        old_dir = self.lists_root("alfa_x")
        self.back_to_old_name("alfa|x", old_dir, new_dir)

        self.assertEqual(list_fetch.migrate_held_list_directories(log=lambda _line: None), 1)

        self.assertFalse(os.path.exists(old_dir))
        entry = config.fetched_bot_lists["alfa|x"]
        self.assertTrue(os.path.normcase(plain(entry["list_path"])).startswith(
            os.path.normcase(new_dir) + os.sep))
        self.assertEqual(self.titles(entry), (["Real.flac"], None))
        # And a purge now reaches it, which it could not from the old name.
        self.assertTrue(list_fetch.forget_bot("alfa|x"))
        self.assertFalse(os.path.exists(new_dir))

    def test_a_secondary_list_is_moved_and_still_reads(self):
        self.fetch("alfa")
        self.fetch("alfa", channel="#a|b", secondary=True, title="FromPipe.mkv")
        new_dir = list_fetch.secondary_channel_extract_dir("alfa", "#a|b")
        old_dir = self.lists_root("_channels", "alfa", "a_b")
        self.back_to_old_name("alfa", old_dir, new_dir)

        self.assertEqual(list_fetch.migrate_held_list_directories(log=lambda _line: None), 1)

        self.assertFalse(os.path.exists(old_dir))
        self.assertTrue(os.path.isdir(new_dir))
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa"], "a_b"),
                         (["FromPipe.mkv"], None))
        # The primary list was never touched.
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa"]), (["Song.flac"], None))

    def test_the_move_is_saved(self):
        import db
        self.fetch("alfa|x")
        self.back_to_old_name("alfa|x", self.lists_root("alfa_x"),
                              list_fetch.list_extract_dir("alfa|x"))

        list_fetch.migrate_held_list_directories(log=lambda _line: None)

        stored = db.load_fetched_bot_lists()["alfa|x"]["list_path"]
        self.assertTrue(os.path.exists(stored), stored)

    def test_an_ordinary_nick_moves_nothing(self):
        self.fetch("alfa")
        before = config.fetched_bot_lists["alfa"]["list_path"]

        self.assertEqual(list_fetch.migrate_held_list_directories(log=lambda _line: None), 0)

        self.assertEqual(config.fetched_bot_lists["alfa"]["list_path"], before)
        self.assertTrue(os.path.exists(before))

    def test_a_directory_two_bots_shared_is_left_where_it_is(self):
        """The case the change is for: it cannot belong to both. The bot that
        owns it under the new names keeps it; the other is fetched into its own."""
        self.fetch("alfa_x", title="Other.flac")
        self.fetch("alfa|x", title="Real.flac")
        self.share_one_directory()

    def test_it_is_left_whichever_bot_was_held_first(self):
        self.fetch("alfa|x", title="Real.flac")
        self.fetch("alfa_x", title="Other.flac")
        self.share_one_directory()

    def share_one_directory(self):
        old_dir = self.lists_root("alfa_x")
        self.back_to_old_name("alfa|x", old_dir + "-shared", list_fetch.list_extract_dir("alfa|x"))
        # Both now point into the one directory, as the old naming had them.
        entry = config.fetched_bot_lists["alfa|x"]
        for record in [entry] + list(entry["lists"].values()):
            record["list_path"] = os.path.join(old_dir, os.path.basename(record["list_path"]))
        held = sorted(os.listdir(old_dir))

        self.assertEqual(list_fetch.migrate_held_list_directories(log=lambda _line: None), 0)

        self.assertEqual(sorted(os.listdir(old_dir)), held)
        self.assertEqual(self.titles(config.fetched_bot_lists["alfa_x"]), (["Other.flac"], None))

    def test_the_secondary_folder_is_never_moved_as_one_bots_own(self):
        self.fetch("alfa")
        self.fetch("alfa", channel="#video", secondary=True, title="Clip.mkv")
        video = config.fetched_bot_lists["alfa"]["lists"]["video"]["list_path"]
        # A "_channels" bot held from before, its list sitting in that folder.
        self.fetch("_channels", title="Mine.flac")
        own = list_fetch.list_extract_dir("_channels")
        old_file = self.lists_root("_channels", "list.txt")
        os.rename(config.fetched_bot_lists["_channels"]["list_path"], old_file)
        os.rmdir(own)
        entry = config.fetched_bot_lists["_channels"]
        for record in [entry] + list(entry["lists"].values()):
            record["list_path"] = old_file

        self.assertEqual(list_fetch.migrate_held_list_directories(log=lambda _line: None), 0)

        self.assertTrue(os.path.exists(video))
        self.assertTrue(os.path.exists(old_file))
        self.assertFalse(os.path.exists(own))


class AFileOfferedUnderAReservedName(HeldListsCase):

    def promote(self, offered):
        stored = "0123456789ab_" + offered
        with open(os.path.join(self.tmp, stored), "wb") as handle:
            handle.write(b"x" * 64)
        return dcc_fetch._promote_clean_filename(os.path.abspath(self.tmp), stored)

    def test_it_keeps_its_id_and_list_fetches_still_work(self):
        for offered in ("lists", "LISTS", "Lists."):
            with self.subTest(offered=offered):
                kept = self.promote(offered)

                self.assertTrue(kept.startswith("0123456789ab_"), kept)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "lists")))

        self.fetch("bravo", title="Song.flac")
        self.assertIn("bravo", config.fetched_bot_lists)

    def test_an_ordinary_name_still_loses_its_id(self):
        self.assertEqual(self.promote("Album.zip"), "Album.zip")
        self.assertEqual(self.promote("lists.zip"), "lists.zip")


if __name__ == "__main__":
    import unittest
    unittest.main()
