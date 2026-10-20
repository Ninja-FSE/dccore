"""A !rar archive holds what the list lists, and nothing it leaves out (#1376).

The list is what the bot serves: a file type it leaves out - LIST_IGNORED_EXTENSIONS,
or a list's own exclusions (#1295) - is not served by name. A pack follows the
same rule:

  * A folder gets a !rar row only when it holds a packable file the list lists,
    and the request gate - update_list.folder_holds_packable() - asks the same
    question with the list's left-out types. A folder whose only packable files
    are left out has no row and is not packed.
  * The archive holds the files update_list.pack_members() finds - the scan's
    own rules. rar is given the folder, as it always was, and an -n@ list file
    of those files' absolute paths, which keeps only them; a path rar would
    not read as that one exact name is left out. Once it is made, the archive
    is listed, and one holding a name nobody asked for is not sent.
  * A finished archive another request is waiting on is handed to a new one
    only when it was packed leaving out at least what the new one's list leaves
    out. Otherwise the new one is packed beside it, at a path of its own.

Driven through the real list build and dcc.handle_download_request() for the
rows, and through dcc.pack_folder_for_request() with a stand-in rar that reads
the list file it is given the way rar 7.22 does. Where a real rar is found the
same packs are made with it and its archive listed.

Every nick, channel and folder here is invented.
"""

import fnmatch
import io
import json
import os
import subprocess
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402
from tests import support  # noqa: E402
from tests import test_path_security as security  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_album_row_is_one_the_bot_packs import RowCase  # noqa: E402
from tests.test_startup import BootCase  # noqa: E402

CHANNEL = "#dccore-test"


def write(path, size=64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as handle:
        handle.write(b"\x00" * size)


def exact_path(path):
    """`path` as the file system is to take it: on Windows behind \\?\\ with
    nothing normalised, so a name may end in a space or a dot."""
    return "\\\\?\\" + os.path.abspath(path.rstrip(" .")) + path[len(path.rstrip(" .")):] if os.name == "nt" else path


def write_exact(test, path):
    """write() at exact_path(path), and remove it again when `test` ends.

    Before the temp folder goes: on Windows a name that ends in a space or a
    dot can only be removed behind \\?\\, so the folder's own removal
    would leave it - and the folder - behind (#1149)."""
    write(exact_path(path))

    def remove():
        try:
            os.remove(exact_path(path))
        except FileNotFoundError:
            pass
    test.addCleanup(remove)


class _TimeWithoutSleep:
    """dcc's `time`, but sleep() returns at once (the packer's 2 s settle)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def sleep(_seconds):
        return None


# Windows will not start a process in a folder whose path is this long
# (CreateProcess: "the directory name is invalid"), with or without \\?\ in
# front - measured with rar 7.22. The stand-in refuses it on every platform,
# so a pack that needs it fails everywhere, not only on Windows.
LONGEST_CWD = 259


class StandInRar:
    """rar as the packer sees it: `rar a [switches] <archive> <folder>` and
    `rar lb [-scfr] <archive>`, read as text, as the packer reads its pipe.

    Packing walks the folder and keeps the files an -n@ list file (UTF-16, as
    -scul says) takes, read the way rar 7.22 reads it: the end of each line
    is trimmed of spaces and tabs, and of dots on Windows; a line with no
    wildcard takes that file, or everything in that folder; * and ? match
    within a name, in that folder and every folder below it; case aside on
    Windows. With no -n@ it keeps every file. Names are kept from the
    folder's own name down (-ep1), and `extra` ones are added. The "archive"
    is the names, as JSON, and `rar lb` lists them. A start in a folder whose
    path is longer than LONGEST_CWD is refused, as Windows refuses it."""

    def __init__(self, test):
        self.calls = []
        self.listings = []
        self.extra = []
        self.leave_out = []
        self.kw = []
        real = dcc.subprocess.Popen

        def popen(cmd, *a, **kw):
            self.kw.append(kw)
            cwd = kw.get("cwd")
            if cwd is not None and len(os.path.abspath(cwd)) > LONGEST_CWD:
                raise NotADirectoryError(267, "The directory name is invalid", cwd)
            return support.FakeRarProcess(cmd, lambda c, timeout=None: self.run(c, cwd))

        dcc.subprocess.Popen = popen
        test.addCleanup(setattr, dcc.subprocess, "Popen", real)

    @staticmethod
    def takes(mask, full):
        mask = mask.rstrip(" \t." if os.name == "nt" else " \t")
        if os.name == "nt":
            mask, full = mask.lower(), full.lower()
        if "*" not in mask and "?" not in mask:
            return full == mask or full.startswith(mask + os.sep)
        folder, name = os.path.split(mask)
        here, base = os.path.split(full)
        return (here == folder or here.startswith(folder + os.sep)) and fnmatch.fnmatchcase(base, name)

    def run(self, cmd, cwd):
        if cmd[1] == "lb":
            return self.list(cmd)
        switches = [part for part in cmd[2:] if part.startswith("-")]
        named = [part for part in cmd[2:] if not part.startswith("-")]
        archive, sources = named[0], named[1:]
        masks, list_files = None, []
        for switch in switches:
            if switch.startswith("-n@"):
                list_files.append(switch[3:])
                with io.open(switch[3:], "rb") as handle:
                    # At line ends alone, as rar reads it: a name may hold U+2028.
                    lines = [line[:-1] if line.endswith("\r") else line
                             for line in handle.read().decode("utf-8" if "-scfl" in switches else "utf-16")
                             .split("\n")]
                    lines = [line for line in lines if line]
                masks = (masks or []) + lines
        names, used = [], set()
        for source in sources:
            base = os.path.dirname(os.path.abspath(source))
            for here, _dirs, files in os.walk(platform_compat.long_path(source)):
                for name in files:
                    full = os.path.join(here, name)
                    if full.startswith("\\\\?\\"):
                        full = full[4:]
                    taking = [mask for mask in masks or () if self.takes(mask, full)]
                    if masks is not None and not taking:
                        continue
                    used.update(taking)
                    names.append(os.path.relpath(full, base))
        names = sorted([name for name in names if name not in self.leave_out] + list(self.extra))
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "names": names, "sources": sources,
                           "unmatched": sorted(set(masks or ()) - used), "list_files": list_files})
        with io.open(archive, "w", encoding="utf-8") as handle:
            json.dump(names, handle)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def list(self, cmd):
        archive = cmd[-1]
        with io.open(archive, encoding="utf-8") as handle:
            names = json.load(handle)
        self.listings.append(archive)
        text = "\r\n".join(names) + "\r\n"
        out = text
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class PackCase(security.PathSecurityBase):
    """A library with one album, a stand-in rar, three slots and room to pack."""

    def setUp(self):
        super().setUp()
        config.channel_users = {CHANNEL: {"dave", "erin", "frank"}}
        config.bot_joined_channel = True
        self.set_config(MAX_DCC_SLOTS=3, rar_inprogress=False, LARGE_FILE_MB=0,
                        LIST_IGNORED_EXTENSIONS=[".db", ".tmp"])

        real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(
            self.tree.root, "rar.exe" if os.name == "nt" else "rar")
        self.addCleanup(setattr, platform_compat, "rar_command", real_rar_command)
        real_time = dcc.time
        dcc.time = _TimeWithoutSleep(real_time)
        self.addCleanup(setattr, dcc, "time", real_time)
        self.rar = StandInRar(self)

        self.artist = os.path.join(self.tree.music, "Some Artist")
        self.album = os.path.join(self.artist, "Some Album")
        for rel in ("01 - One.flac", "02 - Two.mp3", os.path.join("CD1", "03 - Three.FLAC"),
                    os.path.join("CD1", "04 - Four.mp3"), "cover.jpg", "Thumbs.db", "part.TMP",
                    ".hidden.mp3", os.path.join("Scans", "back.jpg")):
            write(os.path.join(self.album, rel))
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)

    def lists_leaving_out(self, *types, root=None):
        """One primary list, "Music", over the library, leaving out `types`."""
        library.save_lists([library.ServedList("Music", True, [CHANNEL],
                                               [library.Folder("music", root or self.tree.music)], {}, types)])

    def row(self, nick="dave", served_list="Music"):
        row = {"file": "Some_Album.rar", "path": self.album, "channel": CHANNEL, "user_raw": nick,
               "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        if served_list is not None:
            row[dcc.PACK_LIST_KEY] = served_list
        config.dcc_queue.setdefault(nick, []).append(row)
        return row

    def pack(self, row, owner="dave"):
        with security.quiet() as out:
            result = dcc.pack_folder_for_request(self.sock, row, lambda: owner, CHANNEL, None)
        return result, out.getvalue()

    def in_album(self, *rels):
        return sorted(os.path.join("Some Album", rel) for rel in rels)

    def sends(self):
        return [tuple(args[1:4]) for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]

    def base_archive(self):
        return os.path.normpath(os.path.join(config.TMP_ZIP_DIR, dcc._rar_archive_disk_name(self.album)))


def assert_told_once_the_fixed_line(test, nick):
    """`nick` was told exactly once, with dcc.PACK_FAILED_TO_USER, and nothing
    of this machine: no TMP_ZIP_DIR, no path, no size."""
    told = [message for who, message, *_ in test.oserve.queued if who == nick]
    test.assertEqual(len(told), 1, told)
    (message,) = told
    test.assertIn(dcc.PACK_FAILED_TO_USER, message)
    for detail in (os.path.abspath(config.TMP_ZIP_DIR), config.TMP_ZIP_DIR, test.tree.root,
                   os.path.abspath(test.tree.root), " MB", ":\\", "/"):
        test.assertNotIn(detail, message.replace("NOTICE " + nick, ""), message)
    test.assertEqual([kind for kind, _args in test.notices if kind == "pack_error"], [],
                     "the notice for an artist root folder went out too")


class TheArchiveHoldsTheListedFiles(PackCase):

    def test_the_global_left_out_types_stay_out(self):
        self.lists_leaving_out()
        row = self.row()

        self.pack(row)

        (call,) = self.rar.calls
        self.assertEqual(call["unmatched"], [])
        self.assertEqual(call["names"], self.in_album(
            ".hidden.mp3", "01 - One.flac", "02 - Two.mp3", os.path.join("CD1", "03 - Three.FLAC"),
            os.path.join("CD1", "04 - Four.mp3"), "cover.jpg", os.path.join("Scans", "back.jpg")))

    def test_a_lists_own_left_out_types_stay_out_whatever_their_case(self):
        self.lists_leaving_out(".flac", ".jpg")
        row = self.row()

        self.pack(row)

        (call,) = self.rar.calls
        self.assertEqual(call["names"], self.in_album(
            ".hidden.mp3", "02 - Two.mp3", os.path.join("CD1", "04 - Four.mp3")))
        with io.open(row["path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), call["names"], "the archive is not what rar took")

    def test_rar_is_given_the_folder_and_the_names_to_keep(self):
        self.lists_leaving_out(".flac")
        row = self.row()

        self.pack(row)

        (call,) = self.rar.calls
        cmd = call["cmd"]
        self.assertEqual(call["sources"], [os.path.abspath(self.album)])
        self.assertIn("-ep1", cmd)
        self.assertIn("-scul" if os.name == "nt" else "-scfl", cmd)
        self.assertEqual(len(call["list_files"]), 1, cmd)
        self.assertIsNone(call["cwd"], "rar is started where the bot runs")
        self.assertEqual(call["unmatched"], [])
        self.assertFalse(os.path.exists(call["list_files"][0]), "the list file was left behind")

    def test_the_row_records_what_its_archive_was_packed_without(self):
        self.lists_leaving_out(".flac")
        row = self.row()

        self.pack(row)

        self.assertFalse(row["is_unpacked_rar_folder"])
        self.assertEqual(sorted(row[dcc.PACKED_WITHOUT_KEY]), [".db", ".flac", ".tmp"])

    def test_a_folder_the_list_now_lists_nothing_of_is_not_packed(self):
        """Left-out types changed after the row was queued."""
        self.lists_leaving_out(".flac", ".mp3", ".jpg")
        row = self.row()

        with self.assertRaises(dcc.PackRefused):
            self.pack(row)

        self.assertEqual(self.rar.calls, [])
        self.assertFalse(os.path.exists(self.base_archive()))

    def test_through_the_dispatcher_that_pack_is_refused_once_and_the_interlocks_released(self):
        self.lists_leaving_out(".flac", ".mp3", ".jpg")
        row = self.row()

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.rar.calls, [])
        self.assertNotIn("send_fails", row, "a refusal used up a retry")
        self.assertNotIn("dave", config.dcc_queue, "the refused row was kept to be tried again")
        assert_told_once_the_fixed_line(self, "dave")

    def test_an_error_that_names_a_path_reaches_the_log_and_not_the_user(self):
        self.lists_leaving_out()
        self.set_config(MAX_SEND_FAILS=1)
        secret = os.path.join(self.tree.root, "somewhere", "private")
        real_members = dcc.update_list.pack_members

        def denied(path, ignored=None):
            raise PermissionError(13, "Permission denied", secret)
        dcc.update_list.pack_members = denied
        self.addCleanup(setattr, dcc.update_list, "pack_members", real_members)
        self.row()

        with security.quiet() as out:
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertIn("Permission denied", out.getvalue(), "the detail is not in the log")
        self.assertIn("private", out.getvalue())
        self.assertNotIn("dave", config.dcc_queue)
        assert_told_once_the_fixed_line(self, "dave")
        self.assertFalse(config.rar_inprogress)
        self.assertNotIn("dave", config.user_processing_lock)

    def test_a_row_without_its_list_follows_every_lists_rule(self):
        """Queued before rows recorded their list, or its list is gone."""
        library.save_lists([
            library.ServedList("Music", True, [CHANNEL], [library.Folder("music", self.tree.music)], {}, (".flac",)),
            library.ServedList("Other", False, ["#dccore-other"], [library.Folder("music", self.tree.music)],
                               {}, (".jpg",))])
        for served in (None, "Gone"):
            with self.subTest(served_list=served):
                self.assertEqual(sorted(dcc.pack_leaves_out(self.row(served_list=served))),
                                 [".db", ".flac", ".jpg", ".tmp"])
        self.assertEqual(sorted(dcc.pack_leaves_out(self.row(served_list="Other"))), [".db", ".jpg", ".tmp"])
        self.assertEqual(sorted(dcc.pack_leaves_out(self.row(served_list="music"))), [".db", ".flac", ".tmp"],
                         "a list is found by its name in any case")


class AWaitingArchiveIsHandedOnOnlyUnderTheSameRule(PackCase):
    """erin's finished archive of the album waits to be sent; dave asks for it."""

    def erin_waits(self, packed_without):
        target = self.base_archive()
        write(target, 32)
        row = {"file": "Some_Album.rar", "path": target, "source_path": self.album, "channel": CHANNEL,
               "user_raw": "erin", "is_unpacked_rar_folder": False, "is_temporary_zip": True}
        if packed_without is not None:
            row[dcc.PACKED_WITHOUT_KEY] = list(packed_without)
        config.dcc_queue["erin"] = [row]
        return row

    def test_packed_leaving_out_at_least_as_much_it_is_reused(self):
        self.lists_leaving_out(".flac")
        self.erin_waits([".db", ".flac", ".tmp", ".jpg"])
        row = self.row()

        result, log = self.pack(row)

        self.assertIn("is already packed", log)
        self.assertEqual(self.rar.calls, [])
        self.assertEqual(row["path"], self.base_archive())

    def assert_packed_beside_it(self, erin):
        row = self.row()

        result, log = self.pack(row)

        self.assertNotIn("is already packed", log)
        self.assertEqual(len(self.rar.calls), 1)
        self.assertNotEqual(os.path.normcase(row["path"]), os.path.normcase(self.base_archive()))
        self.assertEqual(os.path.normcase(os.path.dirname(row["path"])),
                         os.path.normcase(os.path.dirname(self.base_archive())))
        self.assertTrue(os.path.exists(row["path"]))
        self.assertEqual(erin["path"], self.base_archive())
        with io.open(self.base_archive(), "rb") as handle:
            self.assertEqual(handle.read(), b"\x00" * 32, "erin's archive was packed over")
        return row

    def test_packed_leaving_out_less_it_is_not_reused(self):
        self.lists_leaving_out(".flac")
        erin = self.erin_waits([".db", ".tmp"])

        row = self.assert_packed_beside_it(erin)

        self.assertNotIn(os.path.join("Some Album", "01 - One.flac"), self.rar.calls[0]["names"])
        # frank asks under dave's rule: dave's archive is the one he is handed.
        frank = self.row("frank")
        result, log = self.pack(frank, owner="frank")
        self.assertIn("is already packed", log)
        self.assertEqual(len(self.rar.calls), 1, "packed a third time")
        self.assertEqual(frank["path"], row["path"])

    def test_an_archive_no_row_says_what_it_left_out_is_not_reused(self):
        self.lists_leaving_out()
        erin = self.erin_waits(None)

        self.assert_packed_beside_it(erin)

    def test_with_both_paths_wanted_under_other_rules_it_is_refused_once(self):
        self.lists_leaving_out(".flac")
        # No folder on erin's row, so the sweep after the refusal leaves it be.
        self.erin_waits(None).pop("source_path")
        beside = dcc._archive_path_leaving_out(self.base_archive(), dcc.pack_leaves_out(self.row("frank")))
        write(beside, 16)
        config.dcc_queue["gina"] = [{"file": "Some_Album.rar", "path": beside, "channel": CHANNEL,
                                     "user_raw": "gina", "is_unpacked_rar_folder": False,
                                     "is_temporary_zip": True}]
        config.dcc_queue.pop("frank")
        row = self.row()

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.rar.calls, [])
        self.assertNotIn("dave", config.dcc_queue)
        self.assertNotIn("send_fails", row)
        assert_told_once_the_fixed_line(self, "dave")
        for path in (self.base_archive(), beside):
            self.assertTrue(os.path.exists(path), "an archive another row waits on went")


class TheListFileLines(unittest.TestCase):
    """dcc._rar_list_file_lines(): a line rar would not read as that one exact name is left out."""

    def lines(self, members, separator="/", parent="/srv/lib/Artist", windows=False):
        return dcc._rar_list_file_lines(parent, "Album", members, separator=separator, windows=windows)

    def test_each_line_is_a_files_absolute_path(self):
        self.assertEqual(self.lines(["01.mp3", "CD1/02.mp3"]),
                         (["/srv/lib/Artist/Album/01.mp3", "/srv/lib/Artist/Album/CD1/02.mp3"], 0))
        self.assertEqual(self.lines(["CD1\\02.mp3"], separator="\\", parent="D:\\Lib\\Artist", windows=True),
                         (["D:\\Lib\\Artist\\Album\\CD1\\02.mp3"], 0))
        self.assertEqual(self.lines(["01.mp3"], separator="\\", parent="D:\\", windows=True),
                         (["D:\\Album\\01.mp3"], 0))

    def test_a_wildcard_in_any_part_is_left_out(self):
        """In a name it would match files in every folder below too."""
        for member, parent in (("Why?.mp3", "/srv/lib/Artist"), ("x*", "/srv/lib/Artist"),
                               ("CD?/01.mp3", "/srv/lib/Artist"), ("01.mp3", "/srv/li?/Artist"),
                               ("01.mp3", "/srv/lib/Art*")):
            with self.subTest(member=member, parent=parent):
                kept = self.lines([member], parent=parent)
                self.assertEqual(kept, ([], 1))

    def test_a_part_rar_would_trim_is_left_out(self):
        """The end of a line is trimmed: `keep.flac ` takes `keep.flac`, a file
        `Sub ` the whole folder `Sub`, and on Windows `keep.flac.` takes `keep.flac`."""
        for member in ("keep.flac ", "keep.flac\t", "Sub ", "Sub /x.mp3", "Sub\t/x.mp3"):
            with self.subTest(member=repr(member)):
                self.assertEqual(self.lines([member, "fine.mp3"]), (["/srv/lib/Artist/Album/fine.mp3"], 1))
        self.assertEqual(self.lines(["keep.flac."]), (["/srv/lib/Artist/Album/keep.flac."], 0),
                         "a trailing dot is an ordinary name off Windows")
        for member in ("keep.flac.", "Sub./x.mp3"):
            with self.subTest(member=member, windows=True):
                self.assertEqual(self.lines([member], windows=True), ([], 1))
        self.assertEqual(self.lines(["01.mp3"], parent="/srv/lib /Artist"), ([], 1))
        self.assertEqual(self.lines([" lead.mp3"]), (["/srv/lib/Artist/Album/ lead.mp3"], 0),
                         "a leading space is read as it is")

    def test_names_rar_cannot_be_given_exactly_are_left_out(self):
        for name in ("two\nlines.mp3", "car\rriage.mp3", "back\\slash.mp3", "bad\udcff.mp3",
                     "tab\x0bbed.mp3", "form\x0cfeed.mp3", "group\x1cseparator.mp3", "del\x7fete.mp3"):
            with self.subTest(name=repr(name)):
                self.assertEqual(self.lines([name, "fine.mp3"]), (["/srv/lib/Artist/Album/fine.mp3"], 1))

    def test_line_separators_rar_reads_as_one_name_are_kept(self):
        """rar 7.22 takes a name holding U+0085, U+2028 or U+2029 as it is."""
        for name in ("a\u0085b.mp3", "a\u2028b.mp3", "a\u2029b.mp3"):
            with self.subTest(name=repr(name)):
                self.assertEqual(self.lines([name]), (["/srv/lib/Artist/Album/" + name], 0))

    def test_a_backslash_is_a_separator_where_it_is_one(self):
        self.assertEqual(self.lines(["CD1\\02.mp3"], separator="\\", parent="D:\\Lib", windows=True)[1], 0)


class NamesRarWouldReadAsMore(PackCase):
    """Files on disk whose names rar would not take as they are: left out of
    the archive, and so is what they would have taken."""

    def make(self, rel):
        write_exact(self, os.path.join(self.album, rel))

    def test_a_trailing_space_takes_nothing_left_out(self):
        self.lists_leaving_out(".flac", ".jpg")
        self.make("keep.flac ")
        self.make("Sub ")
        self.make(os.path.join("Sub", "x.flac"))
        if os.name == "nt":
            self.make("keep.flac.")
        row = self.row()

        result, log = self.pack(row)

        (call,) = self.rar.calls
        self.assertEqual(call["names"], self.in_album(
            ".hidden.mp3", "02 - Two.mp3", os.path.join("CD1", "04 - Four.mp3")))
        self.assertIn("left out of its archive", log)
        self.assertFalse(row["is_unpacked_rar_folder"], log)

    @unittest.skipIf(os.name == "nt", "* is not allowed in a Windows file name")
    def test_a_wildcard_name_takes_nothing_from_the_folders_below(self):
        self.lists_leaving_out(".flac", ".jpg")
        self.make("x*")
        self.make(os.path.join("Sub", "xy.flac"))
        row = self.row()

        self.pack(row)

        (call,) = self.rar.calls
        self.assertNotIn(os.path.join("Some Album", "Sub", "xy.flac"), call["names"])
        self.assertNotIn(os.path.join("Some Album", "x*"), call["names"])


class TheArchiveIsListedBeforeItIsSent(PackCase):

    def assert_refused_once(self, row, says):
        with security.quiet() as out:
            dcc.check_queue_and_send(self.sock, "dave")
        log = out.getvalue()
        self.assertEqual(len(self.rar.listings), 1)
        self.assertIn("is not what was asked for, so it is not sent: " + says, log)
        self.assertFalse(os.path.exists(self.base_archive()), "the archive was kept")
        self.assertNotIn("dave", config.dcc_queue, "packed again and again for the same answer")
        self.assertNotIn("send_fails", row)
        self.assertEqual(self.sends(), [])
        assert_told_once_the_fixed_line(self, "dave")

    def test_an_archive_with_a_name_nobody_asked_for_is_refused_once(self):
        self.lists_leaving_out(".flac")
        self.rar.extra = [os.path.join("Some Album", "01 - One.flac")]
        row = self.row()

        self.assert_refused_once(row, "1 name(s) nobody asked for ['Some Album/01 - One.flac']")

    def test_an_archive_short_of_an_asked_for_file_is_sent_and_said_so(self):
        self.lists_leaving_out(".flac")
        self.rar.leave_out = [os.path.join("Some Album", "02 - Two.mp3")]
        row = self.row()

        result, log = self.pack(row)

        self.assertIs(result, True, log)
        self.assertIn("[LINEAR RAR ERROR] The archive of Some Album lacks 1 of the 5 file(s) asked for, "
                      "and is sent without them: ['Some Album/02 - Two.mp3']", log)
        self.assertEqual([path for _nick, path, _name in self.sends()], [self.base_archive()])

    def test_one_with_a_stray_name_as_well_is_refused(self):
        self.lists_leaving_out(".flac")
        self.rar.leave_out = [os.path.join("Some Album", "02 - Two.mp3")]
        self.rar.extra = [os.path.join("Some Album", "01 - One.flac")]
        row = self.row()

        self.assert_refused_once(row, "1 name(s) nobody asked for ['Some Album/01 - One.flac'], "
                                      "1 asked-for file(s) missing ['Some Album/02 - Two.mp3']")

    def test_an_empty_listing_is_refused(self):
        self.lists_leaving_out(".flac")
        real_list = self.rar.list
        self.rar.list = lambda cmd: (self.rar.listings.append(cmd[-1]),
                                     types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
        self.addCleanup(setattr, self.rar, "list", real_list)
        row = self.row()

        self.assert_refused_once(row, "0 name(s) nobody asked for [], 5 asked-for file(s) missing")

    def test_a_name_holding_a_line_separator_is_one_name(self):
        self.lists_leaving_out(".flac", ".jpg")
        write(os.path.join(self.album, "a\u2028b.mp3"))
        row = self.row()

        result, log = self.pack(row)

        self.assertIs(result, True, log)
        self.assertIn(os.path.join("Some Album", "a\u2028b.mp3"), self.rar.calls[0]["names"])

    def test_an_archive_of_what_was_asked_for_is_sent(self):
        self.lists_leaving_out(".flac")
        row = self.row()

        result, log = self.pack(row)

        self.assertIs(result, True, log)
        self.assertEqual(len(self.rar.listings), 1)
        self.assertEqual([path for _nick, path, _name in self.sends()], [self.base_archive()])

    def test_one_that_cannot_be_listed_is_tried_again(self):
        self.lists_leaving_out(".flac")
        real_list = self.rar.list
        self.rar.list = lambda cmd: types.SimpleNamespace(returncode=10, stdout="", stderr="ERROR: Bad archive")
        self.addCleanup(setattr, self.rar, "list", real_list)
        row = self.row()

        result, log = self.pack(row)

        self.assertIs(result, False)
        self.assertIn("could not be listed, so it is not sent: rar lb exited 10: ERROR: Bad archive", log)
        self.assertFalse(os.path.exists(self.base_archive()))
        self.assertEqual(row.get("send_fails"), 1, "a listing that failed was not tried again")
        self.assertEqual(config.dcc_queue["dave"], [row])


class TheListFileIsWrittenAsRarReadsIt(unittest.TestCase):
    """UTF-16 with -scul on Windows (rar 7.22), UTF-8 with -scfl elsewhere:
    rar 6.23 on Linux leaves out a name above U+FFFF from a UTF-16 list."""

    NAMES = ["/srv/lib/Album/plain.mp3", "/srv/lib/Album/\U0001F3B5.mp3", "/srv/lib/Album/a\u2028b.mp3"]

    def test_off_windows(self):
        switch, data = dcc._rar_list_file_bytes(self.NAMES, windows=False)
        self.assertEqual(switch, "-scfl")
        self.assertEqual(data, "".join(name + "\n" for name in self.NAMES).encode("utf-8"))

    def test_on_windows(self):
        switch, data = dcc._rar_list_file_bytes(self.NAMES, windows=True)
        self.assertEqual(switch, "-scul")
        self.assertEqual(data, ("\ufeff" + "".join(name + "\r\n" for name in self.NAMES)).encode("utf-16-le"))


class AListFileInUtf8(PackCase):
    """The pack as it runs off Windows, on any platform."""

    def test_names_above_the_bmp_are_packed(self):
        self.lists_leaving_out(".flac", ".jpg")
        emoji = "\U0001F3B5 tune.mp3"
        write(os.path.join(self.album, emoji))
        real_bytes = dcc._rar_list_file_bytes
        dcc._rar_list_file_bytes = lambda lines, windows=None: real_bytes(lines, windows=False)
        self.addCleanup(setattr, dcc, "_rar_list_file_bytes", real_bytes)
        row = self.row()

        result, log = self.pack(row)

        (call,) = self.rar.calls
        self.assertIn("-scfl", call["cmd"])
        self.assertIn(os.path.join("Some Album", emoji), call["names"])
        self.assertIs(result, True, log)


class TheListingIsReadAsRarWritesIt(unittest.TestCase):

    def test_split_at_line_ends_only(self):
        self.assertEqual(dcc._listed_names("a\u2028b.mp3\r\nc\u0085d.mp3\ne\u2029f.mp3\r\n\n"),
                         ["a\u2028b.mp3", "c\u0085d.mp3", "e\u2029f.mp3"])
        self.assertEqual(dcc._listed_names("\ufeffone.mp3\r\n"), ["one.mp3"])
        self.assertEqual(dcc._listed_names(""), [])

    def test_both_ways(self):
        expected = {"Album/CD1/01.mp3", "Album/02.mp3"}
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album/CD1/01.mp3", "Album/02.mp3"], expected), ([], []))
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album/CD1/01.mp3", "Album/02.mp3", "Album/x.db"],
                                                         expected), (["Album/x.db"], []))
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album/02.mp3"], expected), ([], ["Album/CD1/01.mp3"]))
        self.assertEqual(dcc._what_the_archive_got_wrong([], expected), ([], sorted(expected)))

    def test_a_folder_above_an_asked_for_file_is_not_stray(self):
        """rar off Windows may store the folders as entries of their own."""
        expected = {"Album/CD1/01.mp3"}
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album", "Album/CD1", "Album/CD1/01.mp3"], expected),
                         ([], []))
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album", "Album/CD2", "Album/CD1/01.mp3"], expected),
                         (["Album/CD2"], []))
        self.assertEqual(dcc._what_the_archive_got_wrong(["Album/CD1/01.mp3/x", "Album/CD1/01.mp3"], expected),
                         (["Album/CD1/01.mp3/x"], []))

    def environment_with(self, **locale):
        """_rar_environment() off Windows, with this process's locale
        variables replaced by `locale` for the call."""
        saved = {k: os.environ.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")}

        def put_back():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(put_back)
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(locale)
        return dcc._rar_environment(windows=False, darwin=False), dcc._rar_environment(windows=False, darwin=True)

    def test_rar_runs_in_a_utf8_locale_off_windows(self):
        linux, mac = self.environment_with(LANG="C")
        self.assertEqual(linux["LC_ALL"], "C.UTF-8")
        self.assertEqual(mac["LC_ALL"], "en_US.UTF-8", "macOS has no C.UTF-8 on every release")
        self.assertEqual({k: v for k, v in linux.items() if k != "LC_ALL"},
                         {k: v for k, v in os.environ.items() if k != "LC_ALL"}, "the rest of the environment changed")
        self.assertIsNone(dcc._rar_environment(windows=True))

    def test_a_locale_that_is_utf8_already_is_left_alone(self):
        for locale in ({"LANG": "en_GB.UTF-8"}, {"LC_CTYPE": "el_GR.utf8"}, {"LC_ALL": "C.UTF-8"}):
            with self.subTest(locale=locale):
                linux, mac = self.environment_with(**locale)
                for env in (linux, mac):
                    self.assertEqual(env.get("LC_ALL"), locale.get("LC_ALL"))

    def test_lc_all_decides_before_lang(self):
        linux, _mac = self.environment_with(LC_ALL="C", LANG="en_GB.UTF-8")
        self.assertEqual(linux["LC_ALL"], "C.UTF-8")


class BothRarRunsGetTheEnvironment(PackCase):

    def test_the_pack_and_the_listing(self):
        self.lists_leaving_out(".flac")
        marker = {"LC_ALL": "C.UTF-8", "MARK": "for the test"}
        real_env = dcc._rar_environment
        dcc._rar_environment = lambda windows=None: marker
        self.addCleanup(setattr, dcc, "_rar_environment", real_env)

        self.pack(self.row())

        self.assertEqual(len(self.rar.kw), 2)
        self.assertEqual([kw.get("env") for kw in self.rar.kw], [marker, marker])

    def sends(self):
        return [tuple(args[1:4]) for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]


class AnArchivePackedBeforeTheListsRule(PackCase):
    """A packed archive's row from an earlier version, restored from
    dcc_queue.txt: it says nothing of what it was packed without."""

    def setUp(self):
        super().setUp()
        self.lists_leaving_out(".flac")
        self.old = self.base_archive()
        write(self.old, 99)
        self.row_ = {"file": "Some_Album.rar", "path": self.old, "source_path": self.album, "channel": CHANNEL,
                     "user_raw": "dave", "is_unpacked_rar_folder": False, "is_temporary_zip": True,
                     dcc.QUEUED_SIZE_KEY: 99}
        config.dcc_queue["dave"] = [self.row_]

    def sends(self):
        return [tuple(args[1:4]) for name, args in security.InlineThread.dispatched if name == "start_dcc_send"]

    def test_at_its_turn_the_folder_is_packed_again(self):
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(len(self.rar.calls), 1, "the earlier archive was sent as it is")
        self.assertNotIn(os.path.join("Some Album", "01 - One.flac"), self.rar.calls[0]["names"])
        self.assertEqual(self.row_[dcc.PACKED_WITHOUT_KEY], [".db", ".tmp", ".flac"])
        self.assertEqual([path for _nick, path, _name in self.sends()], [self.old])
        with io.open(self.old, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self.rar.calls[0]["names"], "the archive sent is the earlier one")

    def test_the_sweep_turns_it_back_too(self):
        with security.quiet():
            dcc.check_queue_and_send(self.sock, "system_next_trigger_fallback")

        self.assertTrue(self.row_["is_unpacked_rar_folder"])
        self.assertEqual(self.row_["path"], self.album)
        self.assertNotIn(dcc.QUEUED_SIZE_KEY, self.row_)
        self.assertFalse(os.path.exists(self.old), "the earlier archive was left on disk")
        self.assertEqual(self.sends(), [])
        self.assertIn(("check_queue_and_send", (self.sock, "dave")), security.InlineThread.dispatched)

    def test_every_one_in_a_loaded_queue_is_turned_back(self):
        """Not only the head: one behind it would otherwise wait behind its own
        pack at the waiting-archive cap."""
        other_archive = os.path.join(config.TMP_ZIP_DIR, "Other.rar")
        second = dict(self.row_, file="Other.rar", path=other_archive)
        write(other_archive, 9)
        config.dcc_queue["dave"].append(second)

        with security.quiet():
            self.assertEqual(dcc.pack_again_every_row_packed_before(), 2)

        self.assertTrue(all(row["is_unpacked_rar_folder"] for row in config.dcc_queue["dave"]))
        self.assertFalse(os.path.exists(other_archive), "the second earlier archive was left on disk")
        self.assertEqual(second["path"], self.album)

    def test_one_being_sent_is_left_alone(self):
        config.active_transfers.append({"user": "dave", "file": "Some_Album.rar", "bytes_sent": 0,
                                        "queue_row": self.row_})
        with dcc.queue_lock:
            self.assertFalse(dcc._pack_again_if_packed_before(self.row_))
        self.assertEqual(self.row_["path"], self.old)

    def test_one_that_says_what_it_was_packed_without_is_sent_as_it_is(self):
        self.row_[dcc.PACKED_WITHOUT_KEY] = [".db", ".tmp", ".flac"]

        with security.quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.rar.calls, [])
        self.assertEqual([path for _nick, path, _name in self.sends()], [self.old])


class AFileLinkOutOfTheLibrary(PackCase):

    def test_a_link_that_resolves_outside_is_left_out(self):
        """The decision, with the link and its target given: runs everywhere."""
        members = [("02 - Two.mp3", 64), ("link.mp3", 9), ("inner.mp3", 9)]
        links = {os.path.join(self.album, "link.mp3"), os.path.join(self.album, "inner.mp3")}
        outside = os.path.join(self.album, "link.mp3")

        def islink(path):
            return any(path.endswith(link) for link in links)

        def safe(root, path):
            return path != outside and dcc.is_safe_path(root, path)

        kept, left_out = dcc._members_inside_the_library(self.album, members, islink=islink, safe=safe)

        self.assertEqual(kept, [("02 - Two.mp3", 64), ("inner.mp3", 9)], "a link inside the library is packed")
        self.assertEqual(left_out, [("link.mp3", 9)])

    def test_a_real_link_is_left_out_of_the_archive(self):
        self.lists_leaving_out(".flac", ".jpg")
        secret = os.path.join(self.tree.root, "secret.txt")
        write(secret, 9)
        link = os.path.join(self.album, "link.mp3")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError) as err:
            self.skipTest(f"no file links here ({type(err).__name__}); the decision is tested above")
        self.assertTrue(update_list.is_listed_file("link.mp3", update_list.ignored_extensions("Music")))
        row = self.row()

        result, log = self.pack(row)

        (call,) = self.rar.calls
        self.assertNotIn(os.path.join("Some Album", "link.mp3"), call["names"])
        self.assertIn("[SECURITY] 1 file(s) in Some Album are links to somewhere outside the library", log)


class TheBootTurnsBackALoadedQueue(BootCase):
    """oserve.startup() itself, as tests/test_startup.py drives it: a saved
    queue holding archives an earlier version packed - one at a head, one
    behind a folder pack - is loaded and every one of them turned back."""

    def old_archive_row(self, nick, archive, album):
        return {"file": os.path.basename(album) + ".rar", "path": archive, "source_path": album,
                "channel": "#test-channel", "user_raw": nick,
                "is_unpacked_rar_folder": False, "is_temporary_zip": True}

    def test_every_earlier_archive_row_is_folder_row_again_after_the_boot(self):
        album = os.path.join(self.tree.music, "Some Artist", "Old Album")
        write(os.path.join(album, "01.mp3"))
        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)
        at_the_head = os.path.join(config.TMP_ZIP_DIR, "At_The_Head.rar")
        behind_a_pack = os.path.join(config.TMP_ZIP_DIR, "Behind_A_Pack.rar")
        for archive in (at_the_head, behind_a_pack):
            write(archive, 9)
        pack = {"file": "Old_Album.rar", "path": album, "channel": "#test-channel", "user_raw": "erin",
                "is_unpacked_rar_folder": True, "is_temporary_zip": True}
        saved = {"dave": [self.old_archive_row("dave", at_the_head, album)],
                 "erin": [pack, self.old_archive_row("erin", behind_a_pack, album)]}
        with io.open(db.DCC_QUEUE_FILE, "w", encoding="utf-8") as handle:
            json.dump(saved, handle)

        output = self.boot()

        rows = [row for key in ("dave", "erin") for row in config.dcc_queue.get(key, [])]
        self.assertEqual(len(rows), 3, output)
        for row in rows:
            with self.subTest(row=row["file"]):
                self.assertTrue(row["is_unpacked_rar_folder"], "an earlier archive is still queued as one")
                self.assertEqual(row["path"], album)
        for archive in (at_the_head, behind_a_pack):
            self.assertFalse(os.path.exists(archive), "an earlier archive was left on disk")
        self.assertIn("2 queued archive(s) packed by an earlier version will be packed again", output)


class TheMembers(DCCoreTestCase):
    """update_list.pack_members() lists by the scan's rules."""

    def setUp(self):
        super().setUp()
        self.root = self.make_temp_dir()
        self.folder = os.path.join(self.root, "Album")
        for rel, size in (("01.flac", 10), ("02.MP3", 20), (os.path.join("CD1", "03.flac"), 30),
                          (os.path.join("CD1", "Thumbs.DB"), 40), ("notes.txt", 50)):
            write(os.path.join(self.folder, rel), size)

    def test_sorted_relative_with_sizes_less_the_left_out(self):
        self.assertEqual(update_list.pack_members(self.folder, (".db", ".txt")),
                         [("01.flac", 10), ("02.MP3", 20), (os.path.join("CD1", "03.flac"), 30)])

    def test_nothing_left_out_is_everything(self):
        self.assertEqual(len(update_list.pack_members(self.folder, ())), 5)

    def test_an_unreadable_folder_raises(self):
        with self.assertRaises(OSError):
            update_list.pack_members(os.path.join(self.root, "missing"), ())


class TheGateAsksTheList(DCCoreTestCase):
    """update_list.folder_holds_packable() with the list's left-out types."""

    @unittest.skipIf(os.name == "nt", "a backslash is the separator on Windows")
    def test_a_disc_folder_the_scan_does_not_list_does_not_count(self):
        root = self.make_temp_dir()
        write(os.path.join(root, "album", "CD\\1", "01.flac"))
        self.assertFalse(update_list.folder_holds_packable(os.path.join(root, "album"), (".flac",), ()))
        self.assertEqual(update_list.pack_members(os.path.join(root, "album"), ()), [])

    def test_a_left_out_packable_type_does_not_count(self):
        root = self.make_temp_dir()
        write(os.path.join(root, "flac", "01.FLAC"))
        write(os.path.join(root, "flac", "cover.jpg"))
        write(os.path.join(root, "mixed", "01.flac"))
        write(os.path.join(root, "mixed", "02.mp3"))
        write(os.path.join(root, "discs", "CD1", "01.flac"))
        packable = (".flac", ".mp3")
        for folder, ignored, expected in (("flac", (".flac",), False), ("flac", (), True),
                                          ("mixed", (".flac",), True), ("discs", (".flac",), False),
                                          ("discs", (".mp3",), True)):
            with self.subTest(folder=folder, ignored=ignored):
                self.assertEqual(update_list.folder_holds_packable(
                    os.path.join(root, folder), packable, ignored), expected)


class TheRowsAndTheGateAgree(RowCase):
    """Every folder the list writes a !rar row for is packed, and every one it
    writes none for is refused - with the left-out types set for every list
    and for the list alone."""

    def setUp(self):
        super().setUp()
        self.add("Artist A", "Lossless Only", "01 - Track.flac")
        self.add("Artist A", "Lossless Only", "cover.jpg")
        self.add("Artist A", "Lossless Discs", "CD1", "01 - Track.FLAC")
        self.add("Artist A", "Mixed", "01 - Track.flac")
        self.add("Artist A", "Mixed", "02 - Track.mp3")
        self.add("Artist A", "Lossy", "01 - Track.mp3")
        self.folders = ("Lossless Only", "Lossless Discs", "Mixed", "Lossy")

    def assert_rows_are_what_is_packed(self, expected):
        rows = self.build()
        self.assertEqual(sorted(rows), sorted(self.row_for("Artist A", name) for name in expected))
        for name in self.folders:
            with self.subTest(folder=name):
                pasted = self.row_for("Artist A", name)
                answer = self.answer(pasted)
                self.assertEqual(answer, ["send_dcc_queue_notice"] if name in expected
                                 else ["send_pack_error_notice"], pasted)
        return rows

    def test_left_out_for_every_list(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=[".flac"])
        self.assert_rows_are_what_is_packed(("Mixed", "Lossy"))
        self.assertEqual(set(config.dcc_queue), {"bravo"})
        self.assertEqual(len(config.dcc_queue["bravo"]), 2)

    def test_left_out_by_the_list_alone(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        library.save_lists([library.ServedList("Music", True, ["#alfa-test"],
                                               [library.Folder("library", self.music)], {}, (".flac",))])
        self.assert_rows_are_what_is_packed(("Mixed", "Lossy"))
        self.assertEqual([row[dcc.PACK_LIST_KEY] for row in config.dcc_queue["bravo"]], ["Music", "Music"])

    def answer_in(self, row, channel, nick):
        self.notices.clear()
        with security.quiet():
            dcc.handle_download_request(support.RecordingSocket(), nick, row[len("!alfa "):], channel)
        return list(self.notices)

    def test_each_list_is_asked_by_its_own_rule(self):
        """Two lists over one folder: the one bound to the channel decides,
        not the primary."""
        self.set_config(LIST_IGNORED_EXTENSIONS=[], CHANNEL="#alfa-test,#alfa-lossy")
        library.save_lists([
            library.ServedList("Music", True, ["#alfa-test"], [library.Folder("library", self.music)], {}, ()),
            library.ServedList("Lossy", False, ["#alfa-lossy"], [library.Folder("library", self.music)],
                               {}, (".flac",))])
        for name, in_lossy in (("Lossless Only", False), ("Lossless Discs", False), ("Mixed", True),
                               ("Lossy", True)):
            pasted = self.row_for("Artist A", name)
            with self.subTest(folder=name):
                self.assertEqual(self.answer_in(pasted, "#alfa-lossy", "bravo"),
                                 ["send_dcc_queue_notice"] if in_lossy else ["send_pack_error_notice"])
                self.assertEqual(self.answer_in(pasted, "#alfa-test", "charlie"), ["send_dcc_queue_notice"])
        self.assertEqual(sorted(row[dcc.PACK_LIST_KEY] for row in config.dcc_queue.get("bravo", [])),
                         ["Lossy", "Lossy"])
        self.assertEqual([row[dcc.PACK_LIST_KEY] for row in config.dcc_queue["charlie"]], ["Music"] * 4)

    def test_with_nothing_left_out_every_folder_is_packed(self):
        """The control: the folders refused above are refused for what is left out."""
        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        self.assert_rows_are_what_is_packed(self.folders)


def deep_library(test):
    """A library folder whose albums' parent paths are longer than Windows'
    260 characters, with Some Artist/Some Album in it; the album's path."""
    deep = test.tree.root
    while len(deep) < 300:
        deep = os.path.join(deep, "a-rather-long-folder-name-for-this-test")
    root = os.path.join(deep, "library")
    album = os.path.join(root, "Some Artist", "Some Album")
    for rel in ("01 - One.flac", "02 - Two.mp3", os.path.join("CD1", "03 - Three.mp3"), "cover.jpg"):
        write(platform_compat.long_path(os.path.join(album, rel)))
    test.set_config(FILE_DIRECTORY=root)
    test.lists_leaving_out(".flac", root=root)
    return album


class AFolderDeeperThanWindowsLongestPath(PackCase):

    def setUp(self):
        super().setUp()
        self.deep_album = deep_library(self)
        self.assertGreater(len(os.path.dirname(self.deep_album)), LONGEST_CWD)

    def test_it_is_packed_as_a_short_one_is(self):
        row = self.row()
        row["path"] = self.deep_album

        result, log = self.pack(row)

        (call,) = self.rar.calls
        self.assertIsNone(call["cwd"])
        self.assertEqual(call["sources"], [os.path.abspath(self.deep_album)])
        self.assertEqual(call["unmatched"], [])
        self.assertEqual(call["names"], self.in_album(
            "02 - Two.mp3", os.path.join("CD1", "03 - Three.mp3"), "cover.jpg"))
        self.assertFalse(row["is_unpacked_rar_folder"], log)
        self.assertTrue(os.path.exists(row["path"]))


def _real_rar():
    found = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
    return found if found and os.path.isfile(found) else None


class WithTheRealRar(PackCase):
    """The same pack with the rar binary itself, where there is one. Its
    stand-in above runs everywhere; this checks the stand-in reads the list
    file the way rar does."""

    def setUp(self):
        self.real = _real_rar()
        if self.real is None:
            self.skipTest("no rar binary here")
        real_popen = subprocess.Popen
        super().setUp()
        # dcc.subprocess is this subprocess: the stand-in's own cleanup puts
        # the real Popen back afterwards.
        platform_compat.rar_command = lambda configured=None: self.real
        dcc.subprocess.Popen = real_popen

    def listing(self, archive):
        recorded = dcc.threading.Thread
        dcc.threading.Thread = self._real_thread
        try:
            if os.name == "nt":
                done = subprocess.run([self.real, "lb", "-scur", archive], capture_output=True, timeout=120)
                text = done.stdout.decode("utf-16-le")
            else:
                done = subprocess.run([self.real, "lb", archive], capture_output=True, timeout=120)
                text = done.stdout.decode("utf-8", "replace")
        finally:
            dcc.threading.Thread = recorded
        self.assertEqual(done.returncode, 0, text)
        # Split at line ends alone: a name may hold U+2028, which splitlines() splits at.
        lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
        return sorted(line.replace("\\", "/") for line in lines if line.strip())

    def test_the_archive_holds_the_listed_files_and_the_folder_by_name(self):
        self.lists_leaving_out(".flac", ".jpg")
        # No slot, so the archive waits rather than going to a send; and real
        # threads while rar runs, which Popen.communicate() needs on Windows.
        self.set_config(MAX_DCC_SLOTS=0)
        # A name no code page holds: the listing after the pack must read it
        # as it is, or the archive is taken for one that holds something else.
        unicode_name = "\u00dcn\u00efc\u00f8d\u00e9 \u03b1\u03b2\u03b3 \U0001F3B5.mp3"
        write(os.path.join(self.album, unicode_name))
        # And one str.splitlines() would split: rar lists it as one name.
        separated = "a\u2028b.mp3"
        write(os.path.join(self.album, separated))
        row = self.row()
        recorded = dcc.threading.Thread
        dcc.threading.Thread = self._real_thread
        try:
            result, log = self.pack(row)
        finally:
            dcc.threading.Thread = recorded

        self.assertFalse(row["is_unpacked_rar_folder"], log)
        self.assertEqual(self.listing(row["path"]), sorted(
            "Some Album/" + rel for rel in (".hidden.mp3", "02 - Two.mp3", "CD1/04 - Four.mp3", unicode_name,
                                            separated)))

    def real_pack(self, row):
        self.set_config(MAX_DCC_SLOTS=0)
        recorded = dcc.threading.Thread
        dcc.threading.Thread = self._real_thread
        try:
            return self.pack(row)
        finally:
            dcc.threading.Thread = recorded

    def test_names_it_would_trim_take_nothing_left_out(self):
        self.lists_leaving_out(".flac", ".jpg")
        for rel in ("keep.flac ", "Sub ", os.path.join("Sub", "x.flac")) + (("keep.flac.",) if os.name == "nt" else ()):
            write_exact(self, os.path.join(self.album, rel))
        row = self.row()

        result, log = self.real_pack(row)

        self.assertFalse(row["is_unpacked_rar_folder"], log)
        self.assertEqual(self.listing(row["path"]), sorted(
            "Some Album/" + rel for rel in (".hidden.mp3", "02 - Two.mp3", "CD1/04 - Four.mp3")))

    def test_an_archive_with_a_name_nobody_asked_for_is_not_sent(self):
        """A folder named to -n takes all of it: the listing after the pack
        is what catches it."""
        self.lists_leaving_out(".flac", ".jpg")
        real_lines = dcc._rar_list_file_lines

        def and_a_folder(parent, leaf, members, **kw):
            lines, left_out = real_lines(parent, leaf, members, **kw)
            return lines + [os.path.join(parent, leaf, "CD1")], left_out
        dcc._rar_list_file_lines = and_a_folder
        self.addCleanup(setattr, dcc, "_rar_list_file_lines", real_lines)
        row = self.row()

        with self.assertRaises(dcc.PackRefused):
            self.real_pack(row)

        self.assertTrue(row["is_unpacked_rar_folder"])
        self.assertFalse(os.path.exists(self.base_archive()))

    def test_a_folder_deeper_than_windows_longest_path(self):
        """The same names as `rar a -ep1 <folder>` gives, less what the list leaves out."""
        album = deep_library(self)
        row = self.row()
        row["path"] = album

        result, log = self.real_pack(row)

        self.assertFalse(row["is_unpacked_rar_folder"], log)
        self.assertEqual(self.listing(row["path"]), sorted(
            "Some Album/" + rel for rel in ("02 - Two.mp3", "CD1/03 - Three.mp3", "cover.jpg")))


if __name__ == "__main__":
    unittest.main()
