"""A directory name that had to be cleaned carries a sixteen-digit digest of
the real name (#1423).

list_fetch tags a bot's or a channel's directory with a digest of the real
name whenever the cleaning changed it ("alfa|x" -> "alfa_x-<digest>"), so two
names that clean to the same stem get two directories. The digest was six hex
digits; it is sixteen now. A name that already looks tagged, in the new form
or the older six-digit one, is tagged again, so no nick spells out another
bot's directory under either name. A list held under a six-digit directory is
moved to the longer one once at startup (migrate_held_list_directories()),
its secondary channels with it, and an old lists/_channels/<bot>/ folder left
empty by that is removed.

Checked on the real code, in a temp FETCHED_FILES_DIR. Where two names must
agree in their first six digest digits, the digest is stood in for, rather
than searched for.
"""

import hashlib
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402

from tests.test_a_peer_name_cannot_reach_another_lists_folder import HeldListsCase, plain  # noqa: E402

HEX16 = r"[0-9a-f]{16}"


def six_digit_tag(real):
    """The tag a directory carried before: six hex digits of the lower-cased
    real name. Worked out here, not read from list_fetch."""
    return hashlib.sha1(real.strip().lower().encode("utf-8")).hexdigest()[:6]


def silent(_line):
    pass


class TheDigest(unittest.TestCase):

    def test_it_is_sixteen_hex_digits(self):
        for text in ("alfa|x", "#a|b", "[]", ""):
            with self.subTest(text=text):
                self.assertRegex(list_fetch._name_digest(text), "^" + HEX16 + "$")

    def test_one_name_in_any_case_is_one_digest(self):
        self.assertEqual(list_fetch._name_digest("ALFA|X"), list_fetch._name_digest("alfa|x"))

    def test_a_cleaned_bot_directory_carries_it(self):
        self.assertRegex(list_fetch._sanitize_bot_dir_name("alfa|x"), "^alfa_x-" + HEX16 + "$")

    def test_a_cleaned_channel_directory_carries_it(self):
        self.assertRegex(list_fetch._channel_dir_name("#a|b"), "^a_b-" + HEX16 + "$")


class ANameThatLooksTagged(unittest.TestCase):
    """Tagged again, in either form, so that it is never the directory of
    the name whose tag it spells."""

    def test_the_older_six_digit_form_is_tagged_again(self):
        for nick in ("Alfa-1d8be4", "alfa-ABCDEF"):
            with self.subTest(nick=nick):
                self.assertRegex(list_fetch._sanitize_bot_dir_name(nick),
                                 "^" + nick + "-" + HEX16 + "$")

    def test_the_sixteen_digit_form_is_tagged_again(self):
        for nick in ("Alfa-0123456789abcdef", "Alfa-0123456789ABCDEF"):
            with self.subTest(nick=nick):
                self.assertRegex(list_fetch._sanitize_bot_dir_name(nick),
                                 "^" + nick + "-" + HEX16 + "$")

    def test_other_lengths_are_left_as_they_are(self):
        for nick in ("Alfa-0123456789abcde", "Alfa-12345", "Alfa-1234567"):
            with self.subTest(nick=nick):
                self.assertEqual(list_fetch._sanitize_bot_dir_name(nick), nick)

    def test_a_channel_that_looks_tagged_is_tagged_again(self):
        for channel in ("#video-1d8be4", "#video-0123456789abcdef"):
            with self.subTest(channel=channel):
                self.assertRegex(list_fetch._channel_dir_name(channel),
                                 "^" + channel[1:] + "-" + HEX16 + "$")

    def test_a_nick_spelling_another_bots_directory_does_not_get_it(self):
        current = list_fetch.list_extract_dir("alfa|x")
        older = os.path.join(os.path.dirname(current), "alfa_x-" + six_digit_tag("alfa|x"))
        for nick in (os.path.basename(current), os.path.basename(older)):
            with self.subTest(nick=nick):
                mine = os.path.normcase(list_fetch.list_extract_dir(nick))
                self.assertNotEqual(mine, os.path.normcase(current))
                self.assertNotEqual(mine, os.path.normcase(older))


class _StandInDigests:
    """hashlib for list_fetch, with chosen digests for some names: two names
    that agree in their first six hex digits and differ after them."""

    def __init__(self, chosen):
        self.chosen = {key.encode("utf-8"): value for key, value in chosen.items()}

    def sha1(self, data=b""):
        value = self.chosen.get(bytes(data))
        if value is None:
            return hashlib.sha1(data)
        return types.SimpleNamespace(hexdigest=lambda: value)


class TwoNamesWhoseDigestsStartAlike(unittest.TestCase):
    """Two names that clean to the same stem, with digests equal in their
    first six digits: they still get two directories."""

    def stand_in(self, **pairs):
        real = list_fetch.hashlib
        list_fetch.hashlib = _StandInDigests(pairs)
        self.addCleanup(setattr, list_fetch, "hashlib", real)

    def two(self, first, second):
        self.stand_in(**{first.lower(): "abcdef" + "0" * 34, second.lower(): "abcdef" + "1" * 34})

    def test_two_bots_get_two_list_directories(self):
        self.two("alfa|x", "alfa\\x")
        self.assertEqual(list_fetch._sanitize_bot_dir_name("alfa|x").split("-")[0],
                         list_fetch._sanitize_bot_dir_name("alfa\\x").split("-")[0])
        self.assertNotEqual(os.path.normcase(list_fetch.list_extract_dir("alfa|x")),
                            os.path.normcase(list_fetch.list_extract_dir("alfa\\x")))

    def test_two_channels_get_two_directories(self):
        self.two("#a|b", "#a\\b")
        self.assertNotEqual(
            os.path.normcase(list_fetch.secondary_channel_extract_dir("bravo", "#a|b")),
            os.path.normcase(list_fetch.secondary_channel_extract_dir("bravo", "#a\\b")))

    def test_two_nicks_with_no_usable_name_get_two_fetch_folders(self):
        self.two("[]", "^|`")
        self.assertNotEqual(dcc_fetch.bot_folder_name("[]").casefold(),
                            dcc_fetch.bot_folder_name("^|`").casefold())


class AHeldListUnderTheOlderTag(HeldListsCase):
    """A list held from before, under a six-digit directory. Made here by
    fetching under the current names and putting each folder back where the
    six-digit tag had it."""

    BOT = "alfa|x"

    def setUp(self):
        super().setUp()
        self.lists = os.path.join(os.path.abspath(self.tmp), "lists")
        self.channels = os.path.join(self.lists, "_channels")

    def old_bot(self, bot=None):
        bot = bot or self.BOT
        cleaned = list_fetch._sanitize_bot_dir_name(bot).rsplit("-", 1)[0]
        return f"{cleaned}-{six_digit_tag(bot)}".lower()

    def old_main_dir(self, bot=None):
        return os.path.join(self.lists, self.old_bot(bot))

    def old_channel_dir(self, channel, bot=None):
        bot = bot or self.BOT
        new_bot_dir = list_fetch.list_extract_dir(bot)
        bot_part = self.old_bot(bot) if new_bot_dir != os.path.join(self.lists, bot.lower()) \
            else bot.lower()
        cleaned = list_fetch._channel_dir_name(channel).rsplit("-", 1)[0]
        return os.path.join(self.channels, bot_part, f"{cleaned}-{six_digit_tag(channel)}".lower())

    def put_back(self, key, new_dir, old_dir):
        os.makedirs(os.path.dirname(old_dir), exist_ok=True)
        os.rename(new_dir, old_dir)
        entry = config.fetched_bot_lists[key]
        for record in [entry] + list(entry["lists"].values()):
            path = plain(record["list_path"])
            if os.path.normcase(path).startswith(os.path.normcase(new_dir) + os.sep):
                record["list_path"] = old_dir + path[len(new_dir):]
        parent = os.path.dirname(new_dir)
        if os.path.normcase(parent) != os.path.normcase(os.path.dirname(old_dir)) \
                and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)

    def hold(self, channels=("#a|b", "#c|d")):
        """The bot's main list and one list per secondary channel, all under
        six-digit directories. Returns {channel: (old, new)} with None for the
        main list."""
        self.fetch(self.BOT, title="Main.flac")
        for channel in channels:
            self.fetch(self.BOT, channel=channel, secondary=True, title=f"{channel[1:]}.mkv")
        moves = {None: (self.old_main_dir(), list_fetch.list_extract_dir(self.BOT))}
        for channel in channels:
            moves[channel] = (self.old_channel_dir(channel),
                              list_fetch.secondary_channel_extract_dir(self.BOT, channel))
        for old, new in moves.values():
            self.put_back(self.BOT, new, old)
        self.assertFalse(os.path.exists(os.path.join(self.channels, list_fetch._sanitize_bot_dir_name(self.BOT).lower())))
        return moves

    def migrate_watching(self):
        """migrate_held_list_directories(), noting every folder it renames
        or asks to remove: ("rename", path) for both ends, ("rmdir", path)."""
        touched = []
        real_rename, real_rmdir = os.rename, os.rmdir

        def rename(src, dst, *args, **kwargs):
            touched.append(("rename", src))
            touched.append(("rename", dst))
            return real_rename(src, dst, *args, **kwargs)

        def rmdir(path, *args, **kwargs):
            touched.append(("rmdir", path))
            return real_rmdir(path, *args, **kwargs)

        os.rename, os.rmdir = rename, rmdir
        try:
            moved = list_fetch.migrate_held_list_directories(log=silent)
        finally:
            os.rename, os.rmdir = real_rename, real_rmdir
        return moved, touched

    def held_paths(self):
        entry = config.fetched_bot_lists[self.BOT]
        return [entry["list_path"]] + sorted(str(info["list_path"]) for info in entry["lists"].values())

    def markers(self):
        lists = config.fetched_bot_lists[self.BOT]["lists"]
        return {info.get("channel"): marker for marker, info in lists.items()}

    def assert_every_path_under_new(self, moves):
        entry = config.fetched_bot_lists[self.BOT]
        news = [os.path.normcase(new) + os.sep for _old, new in moves.values()]
        for record in [entry] + list(entry["lists"].values()):
            path = os.path.normcase(plain(record["list_path"]))
            self.assertTrue(any(path.startswith(new) for new in news), path)
            self.assertTrue(os.path.exists(plain(record["list_path"])), path)

    def test_the_main_list_and_its_channels_move_to_the_longer_tag(self):
        moves = self.hold()

        self.assertEqual(list_fetch.migrate_held_list_directories(log=silent), 3)

        for old, new in moves.values():
            self.assertFalse(os.path.exists(old), old)
            self.assertTrue(os.path.isdir(new), new)
        self.assert_every_path_under_new(moves)
        self.assertEqual(self.titles(config.fetched_bot_lists[self.BOT]), (["Main.flac"], None))
        for channel in ("#a|b", "#c|d"):
            self.assertEqual(self.titles(config.fetched_bot_lists[self.BOT], self.markers()[channel]),
                             ([f"{channel[1:]}.mkv"], None))

    def test_a_second_run_moves_nothing(self):
        self.hold()
        list_fetch.migrate_held_list_directories(log=silent)
        before = self.held_paths()

        self.assertEqual(list_fetch.migrate_held_list_directories(log=silent), 0)

        self.assertEqual(self.held_paths(), before)

    def test_the_move_is_saved(self):
        import db
        self.hold()

        list_fetch.migrate_held_list_directories(log=silent)

        saved = db.load_fetched_bot_lists()[self.BOT]
        for record in [saved] + list(saved["lists"].values()):
            self.assertTrue(os.path.exists(plain(record["list_path"])), record["list_path"])

    def test_the_old_channels_folder_left_empty_is_removed(self):
        moves = self.hold()
        old_parent = os.path.dirname(moves["#a|b"][0])
        self.assertTrue(os.path.isdir(old_parent))

        list_fetch.migrate_held_list_directories(log=silent)

        self.assertFalse(os.path.exists(old_parent))
        self.assertTrue(os.path.isdir(self.channels))
        self.assertTrue(os.path.isdir(os.path.dirname(moves["#a|b"][1])))

    def test_an_old_channels_folder_with_anything_else_in_it_is_kept(self):
        moves = self.hold()
        old_parent = os.path.dirname(moves["#a|b"][0])
        stray = os.path.join(old_parent, "notes.txt")
        with open(stray, "wb") as handle:
            handle.write(b"kept")

        self.assertEqual(list_fetch.migrate_held_list_directories(log=silent), 3)

        with open(stray, "rb") as handle:
            self.assertEqual(handle.read(), b"kept")
        self.assertEqual(os.listdir(old_parent), ["notes.txt"])

    def test_a_bot_whose_own_name_needs_no_tag_keeps_its_channels_folder(self):
        self.fetch("bravo", title="Main.flac")
        self.fetch("bravo", channel="#a|b", secondary=True, title="Pipe.mkv")
        new = list_fetch.secondary_channel_extract_dir("bravo", "#a|b")
        old = self.old_channel_dir("#a|b", bot="bravo")
        self.assertEqual(os.path.dirname(old), os.path.dirname(new))
        self.put_back("bravo", new, old)

        moved, touched = self.migrate_watching()

        self.assertEqual(moved, 1)
        # Its channels folder is where the list moved to: never one to remove.
        self.assertNotIn(os.path.normcase(os.path.dirname(new)),
                         [os.path.normcase(plain(path)) for action, path in touched if action == "rmdir"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))
        marker = {info.get("channel"): m for m, info in config.fetched_bot_lists["bravo"]["lists"].items()}["#a|b"]
        self.assertEqual(self.titles(config.fetched_bot_lists["bravo"], marker), (["Pipe.mkv"], None))

    def test_a_list_whose_new_directory_exists_stays_where_it_is(self):
        moves = self.hold(channels=("#a|b",))
        for old, new in moves.values():
            os.makedirs(new)

        self.assertEqual(list_fetch.migrate_held_list_directories(log=silent), 0)

        for old, new in moves.values():
            self.assertTrue(os.path.isdir(old), old)
            self.assertEqual(os.listdir(new), [])
        self.assertEqual(self.titles(config.fetched_bot_lists[self.BOT]), (["Main.flac"], None))

    def test_a_directory_two_bots_share_stays_where_it_is(self):
        moves = self.hold(channels=())
        old = moves[None][0]
        self.fetch("alfa\\x", title="Other.flac")
        other = config.fetched_bot_lists["alfa\\x"]
        for record in [other] + list(other["lists"].values()):
            record["list_path"] = os.path.join(old, os.path.basename(plain(record["list_path"])))
        held = sorted(os.listdir(old))

        self.assertEqual(list_fetch.migrate_held_list_directories(log=silent), 0)

        self.assertEqual(sorted(os.listdir(old)), held)
        self.assertFalse(os.path.exists(moves[None][1]))

    def test_nothing_outside_lists_is_touched(self):
        """Folders at the top of FETCHED_FILES_DIR named like the old and new
        directories, and an empty one shaped like an old channels folder: the
        move renames and removes only inside lists/."""
        moves = self.hold()
        root = os.path.abspath(self.tmp)
        for name in (self.old_bot(), os.path.basename(moves[None][1]),
                     os.path.join("_channels", self.old_bot())):
            os.makedirs(os.path.join(root, name), exist_ok=True)
        with open(os.path.join(root, self.old_bot(), "Track.flac"), "wb") as handle:
            handle.write(b"x")

        def outside_lists():
            found = []
            for here, dirs, files in os.walk(root):
                if os.path.normcase(here) == os.path.normcase(self.lists):
                    dirs[:] = []
                    continue
                found.extend(os.path.join(here, name) for name in dirs + files)
            return sorted(found)

        before = outside_lists()

        moved, touched = self.migrate_watching()

        self.assertEqual(moved, 3)
        self.assertEqual(outside_lists(), before)
        self.assertTrue(touched)
        for action, path in touched:
            with self.subTest(action=action, path=path):
                path = os.path.normcase(plain(path))
                self.assertTrue(path.startswith(os.path.normcase(self.lists) + os.sep), path)
                if action == "rmdir":
                    # Only an old bot folder under _channels/, never _channels itself.
                    self.assertTrue(path.startswith(os.path.normcase(self.channels) + os.sep), path)


if __name__ == "__main__":
    unittest.main()
