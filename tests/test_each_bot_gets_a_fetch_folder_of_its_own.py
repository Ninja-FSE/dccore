"""#1423: each bot's fetched files in a folder named after it.

An operator's wish, from mIRC: "If I download from DCCore, it places the
files in MircDownload\\DCCore; from Balte, in MircDownload\\Balte." With
FETCHED_FILES_PER_BOT on, a file or a packed folder fetched from SomeBot is
saved in FETCHED_FILES_DIR/SomeBot/. Off - the default - nothing changes.

Checked here on the real code, in a temp FETCHED_FILES_DIR:

  * the folder name: the nick, cleaned as a fetched file's name is, never a
    separator, "..", a Windows device name or "lists" - and tagged with a
    digest of the nick when cleaning changed it (#1438); a nick that cleans
    to nothing gets "bot (<16 hex digits>)", a name no nick cleans to;
  * which folder a row gets: none with the setting off or for a list; one
    bot, one folder, whatever the case of its nick;
  * a row's folder is never outside FETCHED_FILES_DIR, whatever the history
    file says;
  * an offer is saved into the bot's folder, through handle_incoming_offer();
  * "another row holds this file" (#1269) is per folder;
  * the dashboard's delete and download find the file in its row's folder.
"""

import io
import os
import re
import shutil
import string
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import web_fetch  # noqa: E402
from tests.support import DCCoreTestCase, silence_debug  # noqa: E402


class FolderCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(self.root, exist_ok=True)
        self.set_config(FETCHED_FILES_DIR=self.root, FETCHED_FILES_PER_BOT=True, fetch_queue={})

    def put(self, folder, name, body=b"x"):
        directory = os.path.join(self.root, folder) if folder else self.root
        os.makedirs(directory, exist_ok=True)
        with io.open(os.path.join(directory, name), "wb") as handle:
            handle.write(body)
        return os.path.join(directory, name)


class TheFolderName(unittest.TestCase):

    def test_an_ordinary_nick_is_the_folder(self):
        self.assertEqual(dcc_fetch.bot_folder_name("SomeBot"), "SomeBot")
        self.assertEqual(dcc_fetch.bot_folder_name("Some-Bot_2"), "Some-Bot_2")

    def test_cleaned_as_a_fetched_files_name_is_and_then_tagged(self):
        """What a fetched file's name may hold, a folder name may hold -
        brackets go, as they do from a file name. But a nick the cleaning
        changed also carries "-" and list_fetch._name_digest() of the real
        nick (#1438).

        This test used to assert bot_folder_name(nick) ==
        _sanitize_offer_filename(nick), and that equality WAS the defect:
        "Some[Bot]", "Some|Bot", "Some{Bot}" and "SomeBot" all got the one
        folder "SomeBot". tests/test_two_bots_never_share_a_fetch_folder.py
        covers the rule in full."""
        import list_fetch
        for nick in ("Some[Bot]", "Some|Bot", "Some{Bot}"):
            with self.subTest(nick=nick):
                self.assertEqual(dcc_fetch.bot_folder_name(nick),
                                 dcc_fetch._sanitize_offer_filename(nick)
                                 + "-" + list_fetch._name_digest(nick))
        names = {dcc_fetch.bot_folder_name(nick).casefold()
                 for nick in ("Some[Bot]", "Some|Bot", "Some{Bot}", "SomeBot")}
        self.assertEqual(len(names), 4, names)

    def test_no_separator_and_no_climb_survive(self):
        for nick in ("../evil", "..\\evil", "a/b", "a\\b", ".."):
            with self.subTest(nick=nick):
                name = dcc_fetch.bot_folder_name(nick)
                self.assertTrue(name is None or ("/" not in name and "\\" not in name
                                                 and name not in (".", "..")), name)

    def test_a_windows_device_name_is_made_safe(self):
        for nick in ("CON", "nul", "COM1"):
            with self.subTest(nick=nick):
                self.assertNotEqual(dcc_fetch.bot_folder_name(nick).upper(), nick.upper())

    def test_never_the_folder_the_lists_are_in(self):
        for nick in ("lists", "LISTS"):
            with self.subTest(nick=nick):
                self.assertNotEqual(dcc_fetch.bot_folder_name(nick).casefold(), "lists")

    def test_nothing_usable_is_no_folder(self):
        for nick in ("", "   ", None, "\x03\x02"):
            with self.subTest(nick=nick):
                self.assertIsNone(dcc_fetch.bot_folder_name(nick))


class NoFolderNameHasADot(unittest.TestCase):
    """A fetched file is promoted to its plain name at the top of
    FETCHED_FILES_DIR only when that name has a "." (_promote_clean_filename()),
    so a bot's folder name never has one, whatever the nick."""

    DOTTED = ("a.b", "a.", "a..", "a...", "a...b", ".a", ".a.", "a.b.c.",
              "x . .", "Alfa.", "Alfa..", "[Alfa].", "aux.txt", "lists.",
              "fetched.file", "a." * 200, "\x02a.b\x02")

    def test_a_dot_in_the_nick_never_reaches_the_folder_name(self):
        for nick in self.DOTTED:
            with self.subTest(nick=nick[:24]):
                name = dcc_fetch.bot_folder_name(nick)
                self.assertIsNotNone(name)
                self.assertNotIn(".", name)

    def test_a_dot_inside_the_nick_becomes_an_underscore(self):
        """And the name is then not the nick's own, so it carries the tag
        (#1438): "a.b" never gets the folder of the nick "a_b"."""
        import list_fetch
        self.assertEqual(dcc_fetch.bot_folder_name("a.b"), "a_b-" + list_fetch._name_digest("a.b"))
        self.assertEqual(dcc_fetch.bot_folder_name("x.y.z."), "x_y_z-" + list_fetch._name_digest("x.y.z."))
        self.assertEqual(dcc_fetch.bot_folder_name("a_b"), "a_b")
        self.assertNotEqual(dcc_fetch.bot_folder_name("a.b").casefold(),
                            dcc_fetch.bot_folder_name("a_b").casefold())

    def test_the_folder_is_still_one_inside_the_fetched_files_folder(self):
        for nick in self.DOTTED:
            with self.subTest(nick=nick[:24]):
                name = dcc_fetch.bot_folder_name(nick)
                self.assertNotIn(os.sep, name)
                self.assertLessEqual(len(name.encode("utf-8")), dcc_fetch.MAX_NAME_BYTES)
                self.assertNotIn(name.casefold(), dcc_fetch._RESERVED_FETCHED_NAMES)


# A folder name for a nick that cleans to nothing: "bot (" sixteen hex digits ")".
HASHED_FOLDER = re.compile(r"bot \(([0-9a-f]{16})\)")

# What an IRC nick is made of (RFC 2812: letters, digits, "-" and the
# specials []\`_^{|}).
NICK_CHARS = string.ascii_letters + string.digits + "-[]\\`_^{|}"


class ANickWithNoUsableName(FolderCase):
    """A nick that cleans to nothing gets a folder of its own, "bot (<digest>)",
    rather than the top of FETCHED_FILES_DIR."""

    def test_it_gets_a_hashed_folder(self):
        for nick in ("[]", "^|`", "fetched_file", "FETCHED_FILE", "Fetched_File"):
            with self.subTest(nick=nick):
                self.assertRegex(dcc_fetch.bot_folder_name(nick), HASHED_FOLDER)
                self.assertIsNotNone(HASHED_FOLDER.fullmatch(dcc_fetch.bot_folder_name(nick)))

    def test_one_nick_in_either_case_is_one_folder(self):
        self.assertEqual(dcc_fetch.bot_folder_name("fetched_file"),
                         dcc_fetch.bot_folder_name("FETCHED_FILE"))

    def test_two_such_nicks_get_two_folders(self):
        names = {dcc_fetch.bot_folder_name(nick) for nick in ("[]", "^|`", "fetched_file", "{}|")}
        self.assertEqual(len(names), 4, names)

    def test_no_nick_at_all_is_still_no_folder(self):
        for nick in ("", None, "   ", "\x03\x02", "\x0304,05"):
            with self.subTest(nick=nick):
                self.assertIsNone(dcc_fetch.bot_folder_name(nick))

    def test_its_folder_is_one_inside_the_fetched_files_folder(self):
        for nick in ("[]", "FETCHED_FILE"):
            with self.subTest(nick=nick):
                name = dcc_fetch.bot_folder_name(nick)
                self.assertEqual(dcc_fetch.fetched_dir_of({"fetched_folder": name}),
                                 os.path.join(self.root, name))

    def test_a_row_from_it_gets_that_folder(self):
        row = {"bot": "[]", "request_type": "file"}
        self.assertEqual(dcc_fetch._bot_folder_for_locked({}, row), dcc_fetch.bot_folder_name("[]"))

    def test_no_nick_shaped_string_gives_that_folder(self):
        hashed = dcc_fetch.bot_folder_name("[]")
        digest = HASHED_FOLDER.fullmatch(hashed).group(1)
        spellings = ["bot-" + digest, "bot_" + digest + "_", "bot[" + digest + "]",
                     "bot{" + digest + "}", "bot" + digest, "Bot|" + digest,
                     "bot_" + digest.upper() + "_"]
        spellings += ["x" + char + "y" for char in NICK_CHARS]
        spellings += [char * 3 for char in NICK_CHARS]
        for nick in spellings:
            with self.subTest(nick=nick):
                name = dcc_fetch.bot_folder_name(nick)
                if HASHED_FOLDER.fullmatch(name):
                    # Only a nick that cleans to nothing gets one, and then its own.
                    self.assertNotEqual(name.casefold(), hashed.casefold())
                    continue
                self.assertNotEqual(name.casefold(), hashed.casefold())
                for char in " ()":
                    self.assertNotIn(char, name)

    def test_its_folder_has_no_dot_so_no_file_is_promoted_to_it(self):
        self.assertNotIn(".", dcc_fetch.bot_folder_name("[]"))


class OffUnlessTurnedOn(unittest.TestCase):

    def test_the_shipped_default_is_off(self):
        """Read from a fresh copy of defaults.py: an operator who never turns
        it on sees nothing change."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "shipped_defaults_1423", os.path.join(REPO_ROOT, "src", "defaults.py"))
        shipped = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shipped)
        self.assertIs(shipped.FETCHED_FILES_PER_BOT, False)


class WhichFolderARowGets(FolderCase):

    def folder(self, bot="SomeBot", request_type="file", queue=None):
        row = dcc_fetch.new_fetch_row(bot, "Track.flac", request_type=request_type) \
            if hasattr(dcc_fetch, "new_fetch_row") else {"bot": bot, "request_type": request_type}
        return dcc_fetch._bot_folder_for_locked(queue if queue is not None else {}, row)

    def test_off_by_default_and_then_none(self):
        self.set_config(FETCHED_FILES_PER_BOT=False)
        self.assertIsNone(self.folder())

    def test_on_the_bots_own(self):
        self.assertEqual(self.folder(), "SomeBot")

    def test_a_packed_folder_too(self):
        self.assertEqual(self.folder(request_type="folder"), "SomeBot")

    def test_never_a_list(self):
        """Fetched lists have their own place, lists/<bot>/."""
        self.assertIsNone(self.folder(request_type="list"))

    def test_one_bot_one_folder_whatever_the_case(self):
        queue = {"a": {"bot": "SomeBot", "fetched_folder": "SomeBot"}}
        self.assertEqual(self.folder(bot="somebot", queue=queue), "SomeBot")


class ARowsFolderStaysInside(FolderCase):

    def test_no_folder_is_the_fetched_files_folder(self):
        self.assertEqual(dcc_fetch.fetched_dir_of({}), self.root)
        self.assertEqual(dcc_fetch.fetched_dir_of({"fetched_folder": None}), self.root)

    def test_a_bots_folder_is_one_level_down(self):
        self.assertEqual(dcc_fetch.fetched_dir_of({"fetched_folder": "SomeBot"}),
                         os.path.join(self.root, "SomeBot"))

    def test_a_hand_edited_folder_that_climbs_or_nests_is_refused(self):
        for folder in ("../elsewhere", "..", "a/b", os.path.abspath(os.sep + "tmp")):
            with self.subTest(folder=folder):
                self.assertIsNone(dcc_fetch.fetched_dir_of({"fetched_folder": folder}))

    def test_the_destination_is_in_the_bots_folder(self):
        dest_dir, stored = dcc_fetch._resolve_destination_path("abcdef123456", "Track.flac", "SomeBot")
        self.assertEqual(dest_dir, os.path.join(self.root, "SomeBot"))
        self.assertEqual(stored, "abcdef123456_Track.flac")


class AnOfferIsSavedInTheBotsFolder(FolderCase):
    """handle_incoming_offer() for real; only the transfer itself is stood in
    for, and says where it was told to write."""

    def setUp(self):
        super().setUp()
        self.set_config(MAX_FETCH_SLOTS=10, fetch_feature_disabled=False, CHANNEL="#chan",
                        FETCH_MAX_PER_BOT=0, bot_joined_channel=True)
        config.channel_users["#chan"] = {"serverone"}
        silence_debug(announce)
        stand_in = types.ModuleType("oserve")
        stand_in.queue_message = lambda target, line, is_vip=False: None
        real = sys.modules.get("oserve")
        sys.modules["oserve"] = stand_in
        self.addCleanup(lambda: sys.modules.__setitem__("oserve", real) if real
                        else sys.modules.pop("oserve", None))
        real_usage = shutil.disk_usage
        shutil.disk_usage = lambda path: shutil._ntuple_diskusage(10 ** 13, 0, 10 ** 12)
        self.addCleanup(setattr, shutil, "disk_usage", real_usage)
        self.ran = []
        real_run = dcc_fetch._run_transfer
        dcc_fetch._run_transfer = lambda row, offer, dest_dir, stored_name, sock=None: \
            self.ran.append((dest_dir, stored_name, row.get("fetched_folder")))
        self.addCleanup(setattr, dcc_fetch, "_run_transfer", real_run)

    def offer(self, setting=True):
        self.set_config(FETCHED_FILES_PER_BOT=setting)
        rid = dcc_fetch.enqueue_fetch("ServerOne", "Track.flac")
        dcc_fetch.check_fetch_queue()
        dcc_fetch.handle_incoming_offer(None, "ServerOne", "DCC SEND Track.flac 2130706433 55000 64")
        return config.fetch_queue[rid]

    def test_on_it_goes_into_the_bots_folder(self):
        row = self.offer()
        (dest_dir, stored, folder), = self.ran
        self.assertEqual(dest_dir, os.path.join(self.root, "ServerOne"))
        self.assertEqual(folder, "ServerOne")
        self.assertEqual(row["fetched_folder"], "ServerOne")

    def test_off_it_goes_where_it_always_has(self):
        row = self.offer(setting=False)
        (dest_dir, stored, folder), = self.ran
        self.assertEqual(dest_dir, self.root)
        self.assertIsNone(row["fetched_folder"])


    def test_a_row_resumed_after_a_restart_keeps_its_folder(self):
        """Part of the file is already in the folder the first attempt used;
        the setting changed since. The resumed transfer writes there."""
        self.set_config(FETCHED_FILES_PER_BOT=True)
        rid = dcc_fetch.enqueue_fetch("ServerOne", "Track.flac")
        dcc_fetch.check_fetch_queue()
        config.fetch_queue[rid].update(stored_filename=rid + "_Track.flac", fetched_folder="ServerOne")
        self.set_config(FETCHED_FILES_PER_BOT=False)
        dcc_fetch.handle_incoming_offer(None, "ServerOne", "DCC SEND Track.flac 2130706433 55000 64")
        (dest_dir, stored, folder), = self.ran
        self.assertEqual(dest_dir, os.path.join(self.root, "ServerOne"))


class AnotherRowsFileIsPerFolder(FolderCase):

    def test_the_same_name_in_two_bots_folders_is_two_files(self):
        self.put("BotA", "cover.jpg")
        self.put("BotB", "cover.jpg")
        queue = {"b": {"stored_filename": "cover.jpg", "fetched_folder": "BotB"}}
        held = dcc_fetch.files_other_rows_hold_locked(queue, os.path.join(self.root, "BotA"), ["cover.jpg"])
        self.assertEqual(held, set())

    def test_the_same_name_in_one_folder_is_still_held(self):
        self.put("BotA", "cover.jpg")
        queue = {"b": {"stored_filename": "cover.jpg", "fetched_folder": "BotA"}}
        held = dcc_fetch.files_other_rows_hold_locked(queue, os.path.join(self.root, "BotA"), ["cover.jpg"])
        self.assertEqual(held, {"cover.jpg"})


class TheDashboardFindsTheFile(FolderCase):

    def row(self, folder, name="Track.flac", state="complete"):
        return {"bot": folder or "ServerOne", "state": state, "stored_filename": name,
                "filename": name, "fetched_folder": folder, "request_type": "file"}

    def test_a_delete_removes_the_file_in_its_bots_folder_and_no_other(self):
        in_folder = self.put("BotA", "Track.flac")
        at_the_top = self.put(None, "Track.flac")
        config.fetch_queue["r1"] = self.row("BotA")
        status, _ = web_fetch.build_fetch_delete_result("r1")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(in_folder))
        self.assertTrue(os.path.exists(at_the_top))

    def test_a_batch_delete_keeps_a_file_another_bots_row_still_names(self):
        a = self.put("BotA", "cover.jpg")
        b = self.put("BotB", "cover.jpg")
        config.fetch_queue["r1"] = self.row("BotA", "cover.jpg")
        config.fetch_queue["r2"] = self.row("BotB", "cover.jpg")
        status, result = web_fetch.build_fetch_delete_many_result(["r1"])
        self.assertEqual((status, result["cancelled"]), (200, ["r1"]))
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.exists(b))

    def test_a_row_from_before_the_setting_still_finds_its_file(self):
        at_the_top = self.put(None, "Old.flac")
        config.fetch_queue["r1"] = {"bot": "ServerOne", "state": "complete",
                                    "stored_filename": "Old.flac", "request_type": "file"}
        web_fetch.build_fetch_delete_result("r1")
        self.assertFalse(os.path.exists(at_the_top))

    def test_a_row_whose_folder_climbs_deletes_nothing(self):
        outside = os.path.join(os.path.dirname(self.root), "keep.flac")
        with io.open(outside, "wb") as handle:
            handle.write(b"x")
        config.fetch_queue["r1"] = self.row("..", "keep.flac")
        web_fetch.build_fetch_delete_result("r1")
        self.assertTrue(os.path.exists(outside))

    def test_the_download_route_serves_the_file_from_its_folder(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("flask is not installed in this environment")
        import adminchat
        import webserver
        self.put("BotA", "Track.flac", b"from-bot-a")
        self.put(None, "Track.flac", b"from-the-top")
        config.fetch_queue["r1"] = self.row("BotA")
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash("pw"))
        client = webserver.create_app().test_client()
        client.post("/login", data={"password": "pw"})
        response = client.get("/api/fetch/r1/download")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b"from-bot-a")


if __name__ == "__main__":
    unittest.main()
