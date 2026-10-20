"""#1438: two bots never share one per-bot fetch folder.

With FETCHED_FILES_PER_BOT on (#1423), dcc_fetch.bot_folder_name() cleaned
the nick the way a fetched file's name is cleaned, and that cleaning drops
the nick characters a file name cannot hold, []{}|^`. It is not one-to-one:
"[Alfa]" and "Alfa" both got the folder "Alfa", "Good|Bot" and "GoodBot"
both "GoodBot". One bot's files were filed beside another's.

The fix tags a name that is not its own proof of being unique with "-" and
list_fetch._name_digest() of the real nick, by list_fetch._needs_dir_tag() -
the rule the list directories already use, and their digest
(list_fetch._DIR_TAG_DIGITS hex digits).
_bot_folder_for_locked() reuses a folder another row has when the FOLDER
NAMES match under str.lower(), the fold the digest uses - casefold() went
further and gave "Strasse" the folder of the same name with an eszett.
Checked here on the real code:

  * a nick that cleans to itself keeps exactly its name;
  * nicks the cleaning used to merge get folders of their own, and the tag
    is the list directories' digest of the real nick;
  * two spellings of one nick that differ only in case share one folder;
    two nicks that are one only under casefold() do not;
  * a nick that copies another bot's tagged folder name letter for letter
    is tagged again, and a tagged name never reuses an untagged folder left
    from before the tag;
  * "lists" is never a folder, and is not "lists_"'s either; "CON" is not
    "CON_"'s;
  * a long nick fits one path component and keeps its tag;
  * end to end through handle_incoming_offer(): the files land in different
    folders, over real loopback sockets and with the transfer stood in for
    (so the check runs where sockets are not allowed too).
"""

import io
import os
import re
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402

TAG = re.compile(rf"-[0-9a-f]{{{list_fetch._DIR_TAG_DIGITS}}}\Z")


def folder(nick):
    return dcc_fetch.bot_folder_name(nick)


class ANickThatCleansToItselfKeepsItsName(unittest.TestCase):

    def test_exactly_its_name(self):
        for nick in ("Alfa", "GoodBot", "Good_Bot", "Some-Bot_2", "lists_", "CON_", "x" * 255):
            with self.subTest(nick=nick):
                self.assertEqual(folder(nick), nick)

    def test_whitespace_around_a_nick_is_not_part_of_it(self):
        """Stripped before anything else, as before #1438: no tag for it."""
        self.assertEqual(folder("  Alfa "), "Alfa")


class NicksTheCleaningMergedGetFoldersOfTheirOwn(unittest.TestCase):

    PAIRS = (("[Alfa]", "Alfa"), ("Good|Bot", "GoodBot"), ("{Alfa}", "[Alfa]"),
             ("Alfa^", "Alfa"), ("`Alfa", "Alfa"), ("Bot.", "Bot"), ("a/b", "a_b"),
             ("Some.Bot", "Some_Bot"))

    def test_two_nicks_two_folders(self):
        """Compared casefolded, the stricter side: the Windows and macOS
        filesystems do not tell case apart, so two names that differ only in
        case would be one folder there."""
        for one, other in self.PAIRS:
            with self.subTest(one=one, other=other):
                self.assertNotEqual(folder(one).casefold(), folder(other).casefold())

    def test_the_tag_is_the_list_directories_digest_of_the_real_nick(self):
        """One rule for list directories and bot folders: the digest is
        list_fetch._name_digest() of the nick itself, not of what cleaning
        left of it. A "." left by the cleaning is "_" in a folder name."""
        for nick in ("[Alfa]", "{Alfa}", "Good|Bot", "Alfa^", "Bot.", "a/b", "CON", "lists", "Some.Bot"):
            with self.subTest(nick=nick):
                self.assertEqual(folder(nick),
                                 dcc_fetch._sanitize_offer_filename(nick).replace(".", "_")
                                 + "-" + list_fetch._name_digest(nick))

    def test_the_documented_example(self):
        """docs/changes/1438.md: [Alfa] -> Alfa-1d8be4d71c298166. Pinned as a
        literal so a change to the digest itself (which list_fetch shares)
        shows here."""
        self.assertEqual(folder("[Alfa]"), "Alfa-1d8be4d71c298166")

    def test_a_nick_copying_a_tagged_folder_name_is_tagged_again(self):
        """Letter for letter, in any case."""
        for target in ("[Alfa]", "Good|Bot"):
            tagged = folder(target)
            for spelling in (tagged, tagged.upper(), tagged.lower()):
                with self.subTest(target=target, spelling=spelling):
                    self.assertNotEqual(folder(spelling).casefold(), tagged.casefold())
                    self.assertTrue(folder(spelling).endswith("-" + list_fetch._name_digest(spelling)))


class OneNickInTwoCasesIsOneFolder(unittest.TestCase):

    def test_the_same_digest_whatever_the_case(self):
        self.assertEqual(folder("[Alfa]").casefold(), folder("[ALFA]").casefold())
        self.assertEqual(folder("CON").casefold(), folder("Con").casefold())


class TheNamesDCCoreKeepsAreNeverABotsFolder(unittest.TestCase):

    def test_lists_is_tagged_and_is_not_lists_underscores_folder(self):
        self.assertEqual(folder("lists_"), "lists_")
        for nick in ("lists", "LISTS", "Lists"):
            with self.subTest(nick=nick):
                name = folder(nick)
                self.assertNotEqual(name.casefold(), "lists")
                self.assertNotEqual(name.casefold(), "lists_")
                self.assertTrue(name.endswith("-" + list_fetch._name_digest(nick)), name)

    def test_a_device_name_is_not_its_safe_spellings_folder(self):
        for nick in ("CON", "nul", "COM1"):
            with self.subTest(nick=nick):
                safe = dcc_fetch._sanitize_offer_filename(nick)
                self.assertNotEqual(safe.casefold(), nick.casefold())
                self.assertEqual(folder(safe), safe)
                self.assertNotEqual(folder(nick).casefold(), safe.casefold())


class ALongNickFitsAndKeepsItsTag(unittest.TestCase):

    LONG = ("x" * 256, "x" * 300, "x" * 300 + "a", "x" * 300 + "b",
            "\u00e9" * 200, "x" + "\u00e9" * 200, "Good|" + "x" * 300)

    def test_at_most_one_path_component_ending_in_its_tag(self):
        for nick in self.LONG:
            with self.subTest(nick=nick[:12], length=len(nick)):
                name = folder(nick)
                self.assertLessEqual(len(name.encode("utf-8")), dcc_fetch.MAX_NAME_BYTES)
                self.assertRegex(name, TAG)
                self.assertTrue(name.endswith("-" + list_fetch._name_digest(nick)), name[-12:])
                stem = name[:-(list_fetch._DIR_TAG_DIGITS + 1)]
                self.assertTrue(stem and dcc_fetch._sanitize_offer_filename(nick).startswith(stem),
                                stem[-12:])

    def test_two_long_nicks_with_one_prefix_are_two_folders(self):
        names = {folder(nick).casefold() for nick in self.LONG}
        self.assertEqual(len(names), len(self.LONG))


class NothingUsableIsStillNoFolder(unittest.TestCase):
    """The nick "fetched_file", which the cleaning cannot tell from nothing,
    gets "bot (<digest>)": tests/test_each_bot_gets_a_fetch_folder_of_its_own.py."""

    def test_none(self):
        for nick in ("", "   ", None, "\x03\x02"):
            with self.subTest(nick=nick):
                self.assertIsNone(folder(nick))


class WhichFolderARowGets(DCCoreTestCase):
    """_bot_folder_for_locked(): a folder another row already has is reused
    when the FOLDER NAMES match under str.lower() - the fold the digest uses,
    so one nick in two cases is one folder and nothing more is."""

    def setUp(self):
        super().setUp()
        self.set_config(FETCHED_FILES_PER_BOT=True, fetch_queue={})

    def folder_for(self, bot, queue):
        return dcc_fetch._bot_folder_for_locked(queue, {"bot": bot, "request_type": "file"})

    def test_one_nick_in_another_case_reuses_its_folder(self):
        queue = {"a": {"bot": "[ALFA]", "fetched_folder": folder("[ALFA]")}}
        self.assertEqual(self.folder_for("[Alfa]", queue), folder("[ALFA]"))
        queue = {"a": {"bot": "[Alfa]", "fetched_folder": folder("[Alfa]")}}
        self.assertEqual(self.folder_for("[ALFA]", queue), folder("[Alfa]"))

    def test_two_nicks_that_are_one_only_under_casefold_get_two_folders(self):
        """casefold() folds an eszett to "ss" and a long s (U+017F) to "s";
        str.lower() and the digest do not, and NTFS keeps those names apart
        too. Either way round, the second nick gets its own folder."""
        for queued, asking in (("Stra\u00dfe", "Strasse"), ("Strasse", "Stra\u00dfe"),
                               ("SomeBot", "\u017fomeBot"), ("\u017fomeBot", "SomeBot")):
            with self.subTest(queued=queued, asking=asking):
                self.assertEqual(queued.casefold(), asking.casefold())
                queue = {"a": {"bot": queued, "fetched_folder": folder(queued)}}
                self.assertEqual(self.folder_for(asking, queue), folder(asking))
                self.assertNotEqual(self.folder_for(asking, queue), folder(queued))

    def test_a_tagged_name_never_reuses_an_untagged_folder_from_before_the_tag(self):
        """A row fetched before #1438 has [Alfa]'s files in "Alfa", the folder
        it shared with Alfa. A new row for [Alfa] gets its tagged folder; it
        does not follow the old row's folder back into Alfa's."""
        queue = {"old": {"bot": "[Alfa]", "fetched_folder": "Alfa"}}
        self.assertEqual(self.folder_for("[Alfa]", queue), folder("[Alfa]"))
        self.assertEqual(self.folder_for("[ALFA]", queue), folder("[ALFA]"))

    def test_a_nick_the_cleaning_merged_does_not_reuse_the_others_folder(self):
        """[Alfa] and Alfa are two nicks, so two folders - by accident too:
        neither gets the other's through the reuse."""
        queue = {"a": {"bot": "Alfa", "fetched_folder": "Alfa"}}
        self.assertNotEqual(self.folder_for("[Alfa]", queue).casefold(), "alfa")
        queue = {"a": {"bot": "[Alfa]", "fetched_folder": folder("[Alfa]")}}
        self.assertNotEqual(self.folder_for(folder("[Alfa]"), queue).casefold(),
                            folder("[Alfa]").casefold())
        self.assertNotEqual(self.folder_for("Alfa", queue).casefold(), folder("[Alfa]").casefold())


class _OffersFromTwoBots(DCCoreTestCase):
    """handle_incoming_offer() for real, into a temp FETCHED_FILES_DIR."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(self.root, exist_ok=True)
        self.set_config(fetch_queue={}, fetch_feature_disabled=False, FETCHED_FILES_DIR=self.root,
                        FETCHED_FILES_PER_BOT=True)

    def fetch(self, bot, filename, payload):
        raise NotImplementedError

    def row(self, rid):
        return config.fetch_queue[rid]

    def landed(self, rid):
        """The file's bytes, read from the row's folder."""
        row = self.row(rid)
        with io.open(os.path.join(dcc_fetch.fetched_dir_of(row), row["stored_filename"]), "rb") as handle:
            return handle.read()

    def assert_two_folders(self, one, other):
        first = self.fetch(one, "Song.flac", b"from-the-first-bot")
        second = self.fetch(other, "Other.flac", b"from-the-second-bot")
        a, b = self.row(first), self.row(second)
        self.assertEqual(a["fetched_folder"], one)
        self.assertNotEqual(str(b["fetched_folder"]).casefold(), str(a["fetched_folder"]).casefold())
        self.assertEqual(self.landed(first), b"from-the-first-bot")
        self.assertEqual(self.landed(second), b"from-the-second-bot")
        return a, b

    def test_bracket_alfa_is_not_filed_under_alfa(self):
        self.assert_two_folders("Alfa", "[Alfa]")

    def test_good_pipe_bot_is_not_filed_under_goodbot(self):
        self.assert_two_folders("GoodBot", "Good|Bot")

    def test_nothing_is_created_at_lists(self):
        first = self.fetch("lists_", "Song.flac", b"from-lists-underscore")
        second = self.fetch("lists", "Other.flac", b"from-lists")
        third = self.fetch("LISTS", "Third.flac", b"from-lists-in-capitals")
        self.assertNotIn("lists", [entry.casefold() for entry in os.listdir(self.root)])
        self.assertEqual(self.row(first)["fetched_folder"], "lists_")
        self.assertNotEqual(self.row(second)["fetched_folder"].casefold(), "lists_")
        self.assertEqual(self.row(second)["fetched_folder"].casefold(),
                         self.row(third)["fetched_folder"].casefold())
        self.assertEqual(self.landed(first), b"from-lists-underscore")
        self.assertEqual(self.landed(second), b"from-lists")


class TheFilesLandInTheirOwnFolders(_OffersFromTwoBots):
    """Over real loopback sockets, like tests/test_dcc_fetch.py's
    ACompletedFetchDropsItsTemporaryId; TheOffersAreSentToTheirOwnFolders
    below is the same check where sockets are not allowed."""

    def setUp(self):
        super().setUp()
        try:
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(self.listener.close)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
        except OSError as exc:
            self.skipTest(f"no loopback sockets here: {exc}")
        self.port = self.listener.getsockname()[1]

    def fetch(self, bot, filename, payload):
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        self.assertIsNotNone(rid, bot)
        config.fetch_queue[rid].update(state="offered", offered_at=time.time())

        def serve():
            self.listener.settimeout(5.0)
            conn, _ = self.listener.accept()
            conn.settimeout(5.0)
            conn.sendall(payload)
            conn.close()

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        dcc_fetch.handle_incoming_offer(None, bot, f"DCC SEND {filename} 2130706433 {self.port} {len(payload)}")
        server.join(timeout=5.0)
        self.assertEqual(config.fetch_queue[rid]["state"], "complete", config.fetch_queue[rid].get("reason"))
        return rid

    def assert_two_folders(self, one, other):
        a, b = super().assert_two_folders(one, other)
        self.assertEqual(os.listdir(os.path.join(self.root, a["fetched_folder"])), ["Song.flac"])
        self.assertEqual(os.listdir(os.path.join(self.root, b["fetched_folder"])), ["Other.flac"])


class TheOffersAreSentToTheirOwnFolders(_OffersFromTwoBots):
    """The same offers with only the transfer stood in for: it writes the
    payload where it was told to, so this runs without sockets."""

    def setUp(self):
        super().setUp()
        self.payloads = {}
        real_run = dcc_fetch._run_transfer

        def stand_in(row, offer, dest_dir, stored_name, sock=None):
            os.makedirs(dest_dir, exist_ok=True)
            with io.open(os.path.join(dest_dir, stored_name), "wb") as handle:
                handle.write(self.payloads[row["bot"]])
            row.update(state="complete", stored_filename=stored_name)

        dcc_fetch._run_transfer = stand_in
        self.addCleanup(setattr, dcc_fetch, "_run_transfer", real_run)

    def fetch(self, bot, filename, payload):
        self.payloads[bot] = payload
        rid = dcc_fetch.enqueue_fetch(bot, filename)
        self.assertIsNotNone(rid, bot)
        config.fetch_queue[rid].update(state="offered", offered_at=time.time())
        dcc_fetch.handle_incoming_offer(None, bot, f"DCC SEND {filename} 2130706433 55000 {len(payload)}")
        self.assertEqual(config.fetch_queue[rid]["state"], "complete", config.fetch_queue[rid].get("reason"))
        return rid


del _OffersFromTwoBots


if __name__ == "__main__":
    unittest.main()
