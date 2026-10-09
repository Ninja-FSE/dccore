"""A fetched list archive's member named after a Windows device is written
under a name Windows can create.

WHAT WAS WRONG. list_fetch.py unpacked a zip or RAR list under the member
names the peer chose. _validate_zip_members() refused traversal, absolute
paths and all-dots components, but not device names. On Windows,
os.path.abspath() turns ".../lists/<bot>/NUL" into "\\\\.\\NUL" (and, before
Windows 11, "COM1" or "con.txt" into their devices too), which long_path()
then makes a UNC path: the member was written to a device instead of the list
folder, and an open on a serial port could hold the fetch thread.

WHAT IS TESTED. Every component goes through windows_safe_name(), as a peer's
name does everywhere else, on every platform: "NUL.txt" is unpacked as
"NUL_.txt", a "CON" folder as "CON_". The tests read the names that reach the
disk, so they run the same on every runner, with no device opened anywhere.
The list itself is still found and installed, since _pick_list_file() looks
at what is in the folder.
"""

import io
import json
import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402

from unittest import mock  # noqa: E402

import platform_compat  # noqa: E402

# The fixture and the stand-in rar only: importing a class with tests of its
# own would run those tests again under this module.
from tests.test_a_rar_list_is_opened_and_a_binary_list_is_refused import (  # noqa: E402
    BOT, FAKE_RAR, HeldListFixture, list_text)

DEVICE_NAMES = ("NUL.txt", "nul.txt", "COM1.txt", "con.txt", "AUX .txt", "LPT9.txt")


def written_names(root):
    """Every file and folder name under `root`, relative, with "/"."""
    found = []
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            rel = os.path.relpath(os.path.join(folder, name), root)
            found.append(rel.replace(os.sep, "/"))
    return sorted(found)


class TheComponentsAMemberIsWrittenUnder(HeldListFixture):

    def test_a_device_name_gets_an_underscore_after_its_base(self):
        self.assertEqual(list_fetch._member_parts("NUL.txt"), ["NUL_.txt"])
        self.assertEqual(list_fetch._member_parts("Lists\\con\\COM1.txt"),
                         ["Lists", "con_", "COM1_.txt"])

    def test_an_ordinary_name_is_unchanged(self):
        self.assertEqual(list_fetch._member_parts("./Lists//PeerBot-2026-10-09.txt"),
                         ["Lists", "PeerBot-2026-10-09.txt"])
        self.assertEqual(list_fetch._member_parts("CONCERT.txt"), ["CONCERT.txt"])


class AZipList(HeldListFixture):

    def zip_list(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, data in members:
                zf.writestr(name, data)
        return self.incoming(buffer.getvalue(), "PeerBot.zip")

    def test_every_device_name_reaches_the_disk_renamed(self):
        for name in DEVICE_NAMES:
            with self.subTest(name=name):
                ok, reason = self.fetch(self.zip_list(
                    [(name, list_text("PeerBot", ["A.mp3", "B.mp3"]))]))

                self.assertTrue(ok, reason)
                on_disk = written_names(list_fetch.list_extract_dir(BOT))
                base, dot, rest = name.partition(".")
                self.assertEqual(on_disk, [base.rstrip(" ") + "_" + dot + rest])
                entry = config.fetched_bot_lists[BOT.lower()]
                self.assertEqual(entry["entry_count"], 2)

    def test_a_device_named_folder_is_renamed_too(self):
        ok, reason = self.fetch(self.zip_list(
            [("CON/AUX/PeerBot-2026-10-09.txt", list_text("PeerBot", ["A.mp3"]))]))

        self.assertTrue(ok, reason)
        self.assertEqual(written_names(list_fetch.list_extract_dir(BOT)),
                         ["CON_", "CON_/AUX_", "CON_/AUX_/PeerBot-2026-10-09.txt"])

    def test_an_all_dots_component_is_still_refused(self):
        """The dots check reads the name as sent; renaming must not empty a
        "...." component into something that passes."""
        ok, reason = self.fetch(self.zip_list([("..../..../x.txt", "junk")]))

        self.assertFalse(ok)
        self.assert_held_list_untouched()


class ARarList(HeldListFixture):

    def setUp(self):
        super().setUp()
        fake = os.path.join(self.tree.root, "stand_in_rar")
        with io.open(fake, "w", encoding="utf-8") as handle:
            handle.write(FAKE_RAR)
        self.calls = []

        def argv(rar_bin, args):
            self.calls.append(list(args))
            return [sys.executable, fake] + list(args)

        for patcher in (mock.patch.object(list_fetch, "_rar_argv", argv),
                        mock.patch.object(platform_compat, "rar_command",
                                          lambda configured=None: "rar")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def rar(self, entries):
        payload = b"Rar!\x1a\x07\x01\x00" + json.dumps({"entries": list(entries)}).encode("utf-8")
        return self.incoming(payload, "PeerBot.rar")

    def test_every_device_name_reaches_the_disk_renamed(self):
        for name in DEVICE_NAMES:
            with self.subTest(name=name):
                ok, reason = self.fetch(self.rar([
                    {"name": name, "data": list_text("PeerBot", ["A.mp3", "B.mp3"])}]))

                self.assertTrue(ok, reason)
                on_disk = written_names(list_fetch.list_extract_dir(BOT))
                base, dot, rest = name.partition(".")
                self.assertEqual(on_disk, [base.rstrip(" ") + "_" + dot + rest])
                self.assertEqual(config.fetched_bot_lists[BOT.lower()]["entry_count"], 2)

    def test_rar_is_still_asked_for_the_member_by_its_own_name(self):
        """Only where it is written changes: rar knows the member as "NUL.txt"."""
        ok, reason = self.fetch(self.rar([
            {"name": "Lists", "type": "Directory"},
            {"name": "Lists/NUL.txt", "data": list_text("PeerBot", ["A.mp3"])}]))

        self.assertTrue(ok, reason)
        printed = [call[-1] for call in self.calls if call and call[0] == "p"]
        self.assertEqual(printed, ["Lists/NUL.txt"])
        self.assertIn("Lists/NUL_.txt", written_names(list_fetch.list_extract_dir(BOT)))
