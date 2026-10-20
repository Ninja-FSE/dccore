"""#1440: FETCHED_FILES_DIR may be a share root, with a folder for each bot.

ntpath.normpath(r"\\\\server\\share") has no trailing separator, but the
dirname of r"\\\\server\\share\\alfa" is r"\\\\server\\share\\", with one. Two
"directly inside the root" checks compared those two strings:

  * fetched_dir_of() returned None for every bot folder, so with
    FETCHED_FILES_PER_BOT on every fetch failed as "unsafe destination path";
  * _directory_folder_key() (#1439) returned None for a bot folder under a
    share root, and for the root itself written r"\\\\server\\share\\", so
    files_other_rows_hold_locked() saw no other row holding a file - the
    unsafe direction for a Delete and for the promotion to the plain name.

A share root is a Windows path, so it is checked with Windows' path rules in
place of `os` in dcc_fetch and dcc (tests.support.WindowsOs, shared with
#1439's tests), wherever the suite runs. dcc.is_safe_path() is the real one
under those rules: off Windows ntpath.realpath() is abspath(), which reads no
disk (TheStandInIsSound proves it), and on Windows the stand-in makes it so.
POSIX roots ("/" and an ordinary folder) are checked with the host's own os.

Checked on the real code:

  * fetched_dir_of() gives <root>\\alfa for a share root, a share root with a
    trailing separator, a drive root and a folder; a folder that climbs or
    nests is still refused;
  * _directory_folder_key(): the root, with or without a trailing separator,
    is ""; a bot folder is its key whatever its case or trailing separator;
    a nested folder, another drive or share, a sibling of the root and the
    root's parent are None;
  * end to end under a share root: an offer is saved into the bot's folder
    rather than failing, and another row in the same bot folder holds a file
    against a Delete and against the promotion.
"""

import ntpath
import os
import shutil
import sys
import threading
import types
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase, WindowsOs, silence_debug, windows_path_rules

import announce  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import web_fetch  # noqa: E402

BS = "\\"
SHARE = BS * 2 + "server" + BS + "share"

# Each Windows root as an operator may write it, with the folder
# fetched_files_root() makes of it (abspath keeps a root's separator).
WINDOWS_ROOTS = {
    "a share root": (SHARE, SHARE),
    "a share root with a trailing separator": (SHARE + BS, SHARE + BS),
    "a drive root": ("C:" + BS, "C:" + BS),
    "a folder on a drive": ("C:" + BS + "fetched", "C:" + BS + "fetched"),
}

# Elsewhere, for every Windows root: never the root and never a bot folder.
ELSEWHERE = (
    "D:" + BS + "alfa",
    "D:" + BS + "elsewhere" + BS + "alfa",
    BS * 2 + "server" + BS + "other" + BS + "alfa",
    BS * 2 + "server" + BS + "other",
    BS * 2 + "other" + BS + "share" + BS + "alfa",
)


def under(root, *parts):
    """`parts` inside a Windows `root`, written with one separator between."""
    return root.rstrip(BS) + BS + BS.join(parts)


class NothingOnTheShare:
    """Windows' path rules with nothing on the share: every question about
    a path is answered "not there" without asking, so no test reaches for
    a network share; the rest is `rules`."""

    def __init__(self, rules):
        self.rules = rules

    @staticmethod
    def exists(path):
        return False

    isdir = isfile = lexists = exists

    def __getattr__(self, name):
        return getattr(self.rules, name)


class ShareRootCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(FETCHED_FILES_PER_BOT=True, fetch_queue={})

    def windows(self, root):
        """FETCHED_FILES_DIR = `root`, and Windows' path rules in dcc_fetch
        and in dcc.is_safe_path() - that function alone, not all of dcc -
        for as long as the returned context is open."""
        self.set_config(FETCHED_FILES_DIR=root)
        return windows_path_rules(self, dcc_fetch, functions=[(dcc, "is_safe_path")])

    def posix(self, root):
        if os.sep != "/":
            self.skipTest("a POSIX root needs this host's os to be POSIX; "
                          "the Windows roots are checked here instead")
        self.set_config(FETCHED_FILES_DIR=root)

    def count_disk_calls(self):
        """Every call this thread makes that asks the disk about a path,
        until the test ends: os.stat(), lstat(), readlink(), listdir() and
        scandir(), and exists(), isdir(), isfile(), lexists() and samefile()
        of both ntpath and this host's os.path. The second group is counted
        on its own because it does not always go through the first: on
        Windows with Python 3.12 and later, ntpath's exists() and isdir()
        are nt._path_* built-ins that never call os.stat()."""
        me = threading.get_ident()
        calls = []
        owners = [(os, ("stat", "lstat", "readlink", "listdir", "scandir"))]
        for paths in {id(ntpath): ntpath, id(os.path): os.path}.values():
            owners.append((paths, ("exists", "isdir", "isfile", "lexists", "samefile")))
        for owner, attrs in owners:
            for attr in attrs:
                real = getattr(owner, attr, None)
                if real is None:
                    continue
                self.addCleanup(setattr, owner, attr, real)

                def wrapper(*args, _attr=attr, _real=real, **kwargs):
                    if threading.get_ident() == me:
                        calls.append((_attr, args[:1]))
                    return _real(*args, **kwargs)
                setattr(owner, attr, wrapper)
        return calls


class TheStandInIsSound(ShareRootCase):
    """Why the real dcc.is_safe_path() may run under WindowsOs: there its
    realpath() is abspath(), string work that reads no disk and no network."""

    def paths(self):
        for root, _ in WINDOWS_ROOTS.values():
            yield root
            yield under(root, "alfa")
            yield under(root, "alfa", "..", "..", "x")
        yield from ELSEWHERE

    def test_realpath_is_abspath_and_reads_no_disk(self):
        rules = WindowsOs().path
        calls = self.count_disk_calls()
        answers = {path: (rules.realpath(path), rules.abspath(path)) for path in self.paths()}
        self.assertEqual(calls, [])
        for path, (real, absolute) in answers.items():
            with self.subTest(path=path):
                self.assertEqual(real, absolute)
                self.assertEqual(absolute, ntpath.normpath(path), "an absolute Windows path is only normalised")

    def test_off_windows_it_is_ntpath_itself(self):
        """Not a copy of the rules: the module the stdlib ships, whose
        realpath() is abspath() where there is no `nt`."""
        if support._host_resolves_windows_paths():
            self.skipTest("on Windows ntpath.realpath() resolves on disk, so WindowsOs "
                          "makes it lexical; the test above checks that one")
        self.assertIs(WindowsOs().path, ntpath)
        for path in self.paths():
            with self.subTest(path=path):
                self.assertEqual(ntpath.realpath(path), ntpath.abspath(path))

    def test_the_real_is_safe_path_runs_with_windows_rules(self):
        """Control: these answers are Windows', not this host's. With POSIX
        rules r"\\\\server\\share\\alfa" is one relative file name, never
        inside r"\\\\server\\share"."""
        with self.windows(SHARE):
            answers = {
                "child": dcc.is_safe_path(SHARE, under(SHARE, "alfa")),
                "child of a root with a separator": dcc.is_safe_path(SHARE + BS, under(SHARE, "alfa")),
                "a share whose name begins the same": dcc.is_safe_path(SHARE, SHARE + "-x" + BS + "alfa"),
                "another share": dcc.is_safe_path(SHARE, ELSEWHERE[2]),
                "child of a folder on a drive": dcc.is_safe_path("C:" + BS + "fetched",
                                                                 "C:" + BS + "fetched" + BS + "alfa"),
                "its sibling": dcc.is_safe_path("C:" + BS + "fetched", "C:" + BS + "fetched-x" + BS + "alfa"),
            }
        self.assertEqual(answers, {
            "child": True, "child of a root with a separator": True,
            "a share whose name begins the same": False, "another share": False,
            "child of a folder on a drive": True, "its sibling": False})

    def test_restored_after_the_block(self):
        """Only dcc_fetch's `os` and dcc.is_safe_path() change: dcc's own
        `os`, which a send thread outliving the test reads, never does."""
        real_fetch, real_dcc, real_safe = dcc_fetch.os, dcc.os, dcc.is_safe_path
        with self.windows(SHARE) as stand_in:
            self.assertIs(dcc_fetch.os, stand_in)
            self.assertIs(dcc.os, real_dcc)
            self.assertIsNot(dcc.is_safe_path, real_safe)
            self.assertIs(dcc.is_safe_path.__code__, real_safe.__code__, "the real code, not a copy")
            self.assertIs(dcc.is_safe_path.__globals__["os"], stand_in)
        self.assertIs(dcc_fetch.os, real_fetch)
        self.assertIs(dcc.os, real_dcc)
        self.assertIs(dcc.is_safe_path, real_safe)
        self.assertIs(real_safe.__globals__["os"], real_dcc)


class TheBotsFolder(ShareRootCase):
    """fetched_dir_of(): the folder a row's file is in."""

    def test_a_bots_folder_under_each_windows_root(self):
        for label, (root, made) in WINDOWS_ROOTS.items():
            with self.subTest(root=label):
                with self.windows(root):
                    self.assertEqual(dcc_fetch.fetched_files_root(), made)
                    answers = {folder: dcc_fetch.fetched_dir_of({"fetched_folder": folder})
                               for folder in ("alfa", "Alfa" + BS, "a" + BS + ".." + BS + "alfa", None, "")}
                # The folder is joined normalised: no trailing separator, no
                # climb through "a".
                self.assertEqual(answers, {"alfa": under(root, "alfa"), "Alfa" + BS: under(root, "Alfa"),
                                           "a" + BS + ".." + BS + "alfa": under(root, "alfa"),
                                           None: made, "": made})

    def test_one_that_climbs_or_nests_is_still_refused(self):
        for label, (root, _) in WINDOWS_ROOTS.items():
            with self.subTest(root=label):
                with self.windows(root):
                    answers = {folder: dcc_fetch.fetched_dir_of({"fetched_folder": folder})
                               for folder in (".." + BS + "x", "..", ".", "a" + BS + "b", "a/b",
                                              under(root, "alfa"), "D:" + BS + "alfa", "C:alfa")}
                self.assertEqual(answers, dict.fromkeys(answers))

    def test_a_bots_folder_under_a_posix_root(self):
        for root, expected in (("/", "/alfa"), ("/srv/fetched", "/srv/fetched/alfa"),
                               ("/srv/fetched/", "/srv/fetched/alfa")):
            with self.subTest(root=root):
                self.posix(root)
                self.assertEqual(dcc_fetch.fetched_dir_of({"fetched_folder": "alfa"}), expected)
                self.assertIsNone(dcc_fetch.fetched_dir_of({"fetched_folder": "../x"}))
                self.assertIsNone(dcc_fetch.fetched_dir_of({"fetched_folder": "a/b"}))


class TheFolderIsJoinedNormalised(ShareRootCase):
    """fetched_dir_of() joins the folder as _fetched_folder_key() read it,
    normalised (#1440 review): no trailing separator, and no climb through a
    folder the name only passes on the way. The path it gives is the one
    _directory_folder_key() reads back as that same folder."""

    FOLDERS = ("a" + BS, "a/", "x" + BS + ".." + BS + "a", "a" + BS + ".", "." + BS + "a")

    def test_under_each_windows_root(self):
        roots = [root for root, _ in WINDOWS_ROOTS.values()]
        # Extended-length roots: Python 3.10's ntpath.normpath() leaves a
        # "\\?\" path as it is, so there a raw "a\" stayed "<root>\a\" and
        # the folder key did not know it. Asserted on every version.
        roots += [BS * 2 + "?" + BS + "C:" + BS + "f", BS * 2 + "?" + BS + "UNC" + BS + "server" + BS + "share"]
        for root in roots:
            for folder in self.FOLDERS:
                with self.subTest(root=root, folder=folder):
                    with self.windows(root):
                        path = dcc_fetch.fetched_dir_of({"fetched_folder": folder})
                        key = dcc_fetch._directory_folder_key(path) if path else "refused"
                    self.assertEqual(path, under(root, "a"))
                    self.assertEqual(key, "a")

    def test_under_this_hosts_own_root(self):
        root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(root)
        self.set_config(FETCHED_FILES_DIR=root)
        folders = ["bot" + os.sep, "a" + os.sep + os.pardir + os.sep + "bot", os.curdir + os.sep + "bot"]
        if os.altsep:
            folders.append("bot" + os.altsep)
        for folder in folders:
            with self.subTest(folder=folder):
                path = dcc_fetch.fetched_dir_of({"fetched_folder": folder})
                self.assertEqual(path, os.path.join(root, "bot"))
                self.assertEqual(dcc_fetch._directory_folder_key(path), "bot")

    def test_a_delete_finds_the_file_without_the_folder_it_climbs_from(self):
        """End to end: a row whose folder is "a/../bot", with no folder "a"
        on disk. Its Delete removes its own file in bot/. Joined raw, the
        path went through "a", which a POSIX system cannot open, and the
        file was silently left behind."""
        root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(os.path.join(root, "bot"))
        self.set_config(FETCHED_FILES_DIR=root)
        path = os.path.join(root, "bot", "cover.jpg")
        with open(path, "wb") as handle:
            handle.write(b"x")
        self.assertFalse(os.path.exists(os.path.join(root, "a")))
        config.fetch_queue["r1"] = {"bot": "bot", "state": "complete", "stored_filename": "cover.jpg",
                                    "filename": "cover.jpg", "request_type": "file",
                                    "fetched_folder": "a" + os.sep + os.pardir + os.sep + "bot"}
        self.assertEqual(web_fetch.build_fetch_delete_result("r1")[0], 200)
        self.assertFalse(os.path.exists(path))


class ABotFolderThatLinksOutsideIsRefused(ShareRootCase):
    """dcc.is_safe_path() is the only containment check left in
    fetched_dir_of() (#1440): a bot's folder that is a symlink to a folder
    outside FETCHED_FILES_DIR is refused, so nothing is written, read or
    deleted through it."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(os.path.join(self.root, "bravo"))
        self.outside = self.make_temp_dir()
        self.precious = os.path.join(self.outside, "cover.jpg")
        with open(self.precious, "wb") as handle:
            handle.write(b"not the bot's")
        link = os.path.join(self.root, "alfa")
        try:
            os.symlink(self.outside, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as err:
            self.skipTest("cannot create a directory symlink here (on Windows it needs "
                          "Developer Mode or elevation): %s" % err)
        self.addCleanup(self.unlink, link)
        self.set_config(FETCHED_FILES_DIR=self.root, FETCHED_FILES_PER_BOT=True)

    @staticmethod
    def unlink(link):
        """The link alone, never what it points at, if the tree's own
        removal has not taken it already."""
        if not os.path.lexists(link):
            return
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)  # a directory symlink on Windows

    def row(self, folder):
        return {"bot": folder, "state": "complete", "stored_filename": "cover.jpg",
                "filename": "cover.jpg", "request_type": "file", "fetched_folder": folder}

    def test_the_linked_folder_is_refused(self):
        self.assertIsNone(dcc_fetch.fetched_dir_of({"fetched_folder": "alfa"}))
        self.assertIsNone(dcc_fetch.fetched_dir_of({"fetched_folder": "ALFA" if os.path.normcase("A") == "a"
                                                    else "alfa" + os.sep}))

    def test_a_plain_folder_beside_it_is_not(self):
        """Control: the refusal is the link's, not the folder's."""
        self.assertEqual(dcc_fetch.fetched_dir_of({"fetched_folder": "bravo"}), os.path.join(self.root, "bravo"))

    def test_a_fetch_is_not_written_through_it(self):
        self.assertEqual(dcc_fetch._resolve_destination_path("abcdef012345", "Track.flac", "alfa"), (None, None))

    def test_a_dashboard_delete_leaves_the_file_outside(self):
        config.fetch_queue["r1"] = self.row("alfa")
        self.assertEqual(web_fetch.build_fetch_delete_result("r1")[0], 200)
        self.assertNotIn("r1", config.fetch_queue)
        self.assertTrue(os.path.exists(self.precious))

    def test_a_batch_delete_leaves_it_too(self):
        config.fetch_queue["r1"] = self.row("alfa")
        config.fetch_queue["r2"] = self.row("bravo")
        status, result = web_fetch.build_fetch_delete_many_result(["r1", "r2"])
        self.assertEqual((status, sorted(result["cancelled"])), (200, ["r1", "r2"]))
        self.assertTrue(os.path.exists(self.precious))


class TheDirectoryKey(ShareRootCase):
    """_directory_folder_key(): which folder a directory is, lexically."""

    def keys(self, root, directories):
        with self.windows(root):
            return {directory: dcc_fetch._directory_folder_key(directory) for directory in directories}

    def test_the_root_with_or_without_a_trailing_separator(self):
        for label, (root, _) in WINDOWS_ROOTS.items():
            with self.subTest(root=label):
                bare = root.rstrip(BS) if root != "C:" + BS else root
                directories = (root, root.rstrip(BS) + BS, bare, under(root, "alfa", ".."),
                               root.replace(BS, "/"))
                self.assertEqual(self.keys(root, directories), dict.fromkeys(directories, ""))

    def test_a_bots_folder_whatever_its_case_or_separator(self):
        for label, (root, _) in WINDOWS_ROOTS.items():
            with self.subTest(root=label):
                directories = (under(root, "alfa"), under(root, "ALFA") + BS, under(root, "Alfa") + "/",
                               under(root, "x", "..", "alfa"), under(root, "alfa").replace(BS, "/"),
                               under(root, "alfa").upper())
                self.assertEqual(self.keys(root, directories), dict.fromkeys(directories, "alfa"))

    def test_anywhere_else_is_none(self):
        for label, (root, _) in WINDOWS_ROOTS.items():
            with self.subTest(root=label):
                directories = (under(root, "alfa", "sub"), under(root, "alfa", "sub") + BS) + ELSEWHERE
                self.assertEqual(self.keys(root, directories), dict.fromkeys(directories))

    def test_a_sibling_and_the_parent_of_a_folder_root(self):
        root = "C:" + BS + "fetched"
        directories = ("C:" + BS, "C:" + BS + "fetched-x", "C:" + BS + "fetched-x" + BS + "alfa",
                       "C:" + BS + "alfa", SHARE, under(SHARE, "fetched"))
        self.assertEqual(self.keys(root, directories), dict.fromkeys(directories))

    def test_a_sibling_share_of_a_share_root(self):
        directories = (SHARE + "-x", under(SHARE + "-x", "alfa"), "C:" + BS + "alfa",
                       under("C:" + BS + "server" + BS + "share", "alfa"))
        for root in (SHARE, SHARE + BS):
            with self.subTest(root=root):
                self.assertEqual(self.keys(root, directories), dict.fromkeys(directories))

    def test_under_a_posix_root(self):
        cases = {
            "/": {"/": "", "/alfa": "alfa", "/ALFA/": "alfa", "/alfa/sub": None},
            "/srv/fetched": {"/srv/fetched": "", "/srv/fetched/": "", "/srv/fetched/BotA/": "bota",
                             "/srv/fetched/BotA/sub": None, "/srv/fetched-x": None,
                             "/srv/fetched-x/BotA": None, "/srv": None, "/srv/BotA": None, "/": None},
        }
        for root, expected in cases.items():
            with self.subTest(root=root):
                self.posix(root)
                self.assertEqual({directory: dcc_fetch._directory_folder_key(directory)
                                  for directory in expected}, expected)


class AFetchUnderAShareRoot(ShareRootCase):
    """End to end, with FETCHED_FILES_DIR a share root."""

    def test_the_destination_is_the_bots_folder(self):
        for root in (SHARE, SHARE + BS):
            with self.subTest(root=root):
                with self.windows(root):
                    row = {"bot": "Alfa", "request_type": "file"}
                    folder = dcc_fetch._bot_folder_for_locked({"r": row}, row)
                    resolved = dcc_fetch._resolve_destination_path("abcdef012345", "Track.flac", folder)
                self.assertEqual(folder, "Alfa")
                self.assertEqual(resolved, (under(SHARE, "Alfa"), "abcdef012345_Track.flac"))

    def test_a_name_that_climbs_is_still_kept_in_the_folder(self):
        with self.windows(SHARE):
            dest_dir, stored = dcc_fetch._resolve_destination_path(
                "abcdef012345", ".." + BS + ".." + BS + "evil.exe", "Alfa")
        self.assertEqual(dest_dir, under(SHARE, "Alfa"))
        self.assertNotIn(BS, stored)
        self.assertNotIn("/", stored)

    def test_an_offer_is_saved_into_the_bots_folder(self):
        """handle_incoming_offer() for real, as #1423's tests run it; only
        the transfer itself and the free-space query are stood in for. It
        failed as "unsafe destination path" before #1440."""
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
        ran = []
        real_run = dcc_fetch._run_transfer
        dcc_fetch._run_transfer = lambda row, offer, dest_dir, stored_name, sock=None: \
            ran.append((dest_dir, stored_name))
        self.addCleanup(setattr, dcc_fetch, "_run_transfer", real_run)

        rid = dcc_fetch.enqueue_fetch("ServerOne", "Track.flac")
        dcc_fetch.check_fetch_queue()
        calls = self.count_disk_calls()
        with self.windows(SHARE):
            dcc_fetch.handle_incoming_offer(None, "ServerOne", "DCC SEND Track.flac 2130706433 55000 64")
        # Nothing reached for the share: no test may wait on a network.
        self.assertEqual([call for call in calls if "server" in str(call[1]).lower()], [])
        row = config.fetch_queue[rid]
        self.assertNotEqual(row.get("state"), "failed", row.get("reason"))
        self.assertEqual(row["fetched_folder"], "ServerOne")
        self.assertEqual(ran, [(under(SHARE, "ServerOne"), row["stored_filename"])])
        self.assertTrue(row["stored_filename"].endswith("_Track.flac"), row["stored_filename"])


class AnotherRowHoldsTheFileUnderAShareRoot(ShareRootCase):
    """files_other_rows_hold_locked() under a share root: what a Delete and
    the promotion ask. Same names compare without the disk, and the disk
    calls are counted, so nothing here reaches for a share."""

    def row(self, folder, name="cover.jpg"):
        return {"stored_filename": name, "fetched_folder": folder}

    def held(self, root, queue, directory, row=None):
        calls = self.count_disk_calls()
        with self.windows(root), dcc_fetch._fetch_lock():
            held = dcc_fetch.files_other_rows_hold_locked(queue, directory, ["cover.jpg"], row=row)
            one = dcc_fetch.another_row_holds_file_locked(queue, directory, "cover.jpg", row=row)
        self.assertEqual(calls, [])
        self.assertEqual(bool(held), one)
        return held

    def test_a_row_in_the_same_bot_folder_holds_it(self):
        for root in (SHARE, SHARE + BS):
            for folder in ("Alfa", "alfa", "ALFA"):
                for directory in (under(SHARE, "alfa"), under(SHARE, "Alfa") + BS):
                    with self.subTest(root=root, folder=folder, directory=directory):
                        self.assertEqual(self.held(root, {"b": self.row(folder)}, directory), {"cover.jpg"})

    def test_a_root_row_holds_it_at_the_root(self):
        for root in (SHARE, SHARE + BS):
            for directory in (SHARE, SHARE + BS):
                for folder in (None, ""):
                    with self.subTest(root=root, directory=directory, folder=folder):
                        self.assertEqual(self.held(root, {"b": self.row(folder)}, directory), {"cover.jpg"})

    def test_another_folder_or_the_row_itself_does_not(self):
        """Control: the same name elsewhere is another file."""
        own = self.row("Alfa")
        for queue, directory, row in (({"b": self.row("Bravo")}, under(SHARE, "alfa"), None),
                                      ({"b": self.row(None)}, under(SHARE, "alfa"), None),
                                      ({"b": self.row("Alfa")}, SHARE, None),
                                      ({"b": self.row("Alfa")}, under(SHARE + "-x", "alfa"), None),
                                      ({"a": own}, under(SHARE, "alfa"), own)):
            with self.subTest(directory=directory, queue=queue):
                self.assertEqual(self.held(SHARE, queue, directory, row=row), set())

    def promote(self, holder_folder):
        """_promote_clean_filename() in the bot's folder under a share root,
        with the disk stood in: the plain name is free, and a rename is
        recorded, not made."""
        staged = "abcdef012345_cover.jpg"
        mine = {"stored_filename": staged, "fetched_folder": "Alfa"}
        queue = {"mine": mine, "old": self.row(holder_folder)}
        renamed = []
        with self.windows(SHARE) as stand_in, dcc_fetch._fetch_lock():
            rules = stand_in.path
            stand_in.path = NothingOnTheShare(rules)
            stand_in.rename = lambda old, new: renamed.append(new)
            try:
                result = dcc_fetch._promote_clean_filename(under(SHARE, "Alfa"), staged, queue=queue, row=mine)
            finally:
                stand_in.path = rules
                del stand_in.rename
        return result, renamed

    def test_promotion_leaves_a_name_a_row_in_the_same_bot_folder_holds(self):
        for holder in ("Alfa", "alfa"):
            with self.subTest(holder=holder):
                self.assertEqual(self.promote(holder), ("abcdef012345_cover.jpg", []))

    def test_promotion_takes_a_name_only_another_folder_holds(self):
        """Control: the stand-in lets a promotion through when nothing holds
        the name, so the test above is not passing on a refused rename."""
        for holder in ("Bravo", None):
            with self.subTest(holder=holder):
                result, renamed = self.promote(holder)
                self.assertEqual(result, "cover.jpg")
                self.assertEqual(len(renamed), 1)
                self.assertTrue(renamed[0].endswith(BS + "Alfa" + BS + "cover.jpg"), renamed)


if __name__ == "__main__":
    unittest.main()
