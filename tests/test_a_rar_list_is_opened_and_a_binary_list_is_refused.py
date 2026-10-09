"""A list that arrives as a RAR archive is opened; any other binary is refused,
and the list already held for that bot survives the refusal (#1200).

WHAT WAS WRONG. list_fetch.py sent everything that was not a zip down the
plain-text route, and that route's only test - "does any line start with
'!'?" - read the head with str.splitlines(). That also breaks on \\x0b,
\\x0c, \\x1c-\\x1e and \\x85, so compressed data fell apart into thousands of
short "lines" and one of them nearly always started with "!": 64KB of random
bytes passed 50 times out of 50. A RAR or 7z list was installed as the bot's
list - zero rows, reported as arrived - in place of the good one already
held, and the automatic-grab record for that bot was reset. DCCore publishes
its own list as RAR when LIST_FORMAT = "rar", so two DCCore bots did this to
each other.

WHAT IS TESTED.
  * Binary never passes the plain-text route, and the held list - on disk and
    in fetched_bot_lists - is exactly as it was afterwards. Real text lists,
    including a legacy code-page one, still pass.
  * A RAR list is listed, checked under the zip route's guards and unpacked
    with rar. Those tests run against a stand-in rar (a small Python script
    behind list_fetch._rar_argv) so they run on every CI runner; the ones
    that need the real program skip without it and are paired with the
    stand-in versions of the same cases.
"""

import io
import json
import os
import random
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "PeerBot"
RULE = "=" * 40


def list_text(base, titles):
    """A list in the shape update_list.py writes, one folder of `titles`."""
    rows = "".join(f"!{base} {title}  ::INFO:: 4.00MB\n" for title in titles)
    return (f"List of {len(titles)} files\n\n{RULE}\nD:\\MEDIA\\Albums\\\n"
            f"{RULE}\n" + rows)


# A stand-in for the rar program, enough of it for what list_fetch.py asks:
# "lt" (the technical listing) and "p" (print one member). The "archive" is
# the RAR signature followed by JSON describing the entries, so every case
# below - a lying size, a link, a hang - can be written down directly.
FAKE_RAR = r'''
import json, sys, time

args = sys.argv[1:]
command = args[0]
rest = args[args.index("--") + 1:]
archive = rest[0]
with open(archive, "rb") as handle:
    spec = json.loads(handle.read()[8:].decode("utf-8"))
out = sys.stdout.buffer

if command == "lt":
    if spec.get("hang_listing"):
        time.sleep(120)
    if "listing_exit" in spec:
        sys.exit(spec["listing_exit"])
    out.write(b"\nRAR 7.00   Copyright (c) 1993-2026 Alexander Roshal\n\n")
    if "-c-" not in args and spec.get("comment"):
        out.write(("Archive comment:\n" + spec["comment"] + "\n\n").encode("utf-8"))
    out.write(("Archive: " + archive + "\nDetails: RAR 5\n\n").encode("utf-8"))
    if "listing" in spec:
        out.write(spec["listing"].encode("utf-8"))
        sys.exit(0)
    for entry in spec["entries"]:
        kind = entry.get("type", "File")
        lines = ["        Name: " + entry["name"], "        Type: " + kind]
        if kind == "File":
            size = entry.get("size", len(entry.get("data", "").encode("utf-8")))
            lines.append("        Size: " + str(size))
        lines += [" Packed size: 1", "    Modified: 2026-09-07 12:00:00,000",
                  "     Host OS: Windows", ""]
        out.write(("\n".join(lines) + "\n").encode("utf-8"))
    sys.exit(0)

if command == "p":
    for entry in spec["entries"]:
        if entry["name"] == rest[1]:
            if entry.get("flood"):
                while True:
                    out.write(b"!SomeBot x\n" * 4096)
            if entry.get("hang"):
                time.sleep(120)
            out.write(entry.get("data", "").encode("utf-8"))
            sys.exit(0)
    sys.exit(10)

sys.exit(7)
'''


class HeldListFixture(DCCoreTestCase):
    """A bot whose good list is already held, on disk and in the store."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FETCHED_FILES_DIR=os.path.join(self.tree.root, "fetched"),
                        fetched_bot_lists={}, RAR_BINARY=None)
        self.held_dir = list_fetch.list_extract_dir(BOT)
        os.makedirs(self.held_dir, exist_ok=True)
        self.held_file = os.path.join(self.held_dir, "PeerBot-2026-08-30.txt")
        with io.open(self.held_file, "w", encoding="utf-8") as handle:
            handle.write(list_text("PeerBot", ["Held Track.mp3"]))
        self.held_entry = {"bot": BOT, "fetched_at": 1.0,
                           "list_path": self.held_file, "entry_count": 1}
        config.fetched_bot_lists[BOT.lower()] = dict(self.held_entry)

    def incoming(self, payload, name="PeerBot-list.bin"):
        path = os.path.join(self.tree.root, name)
        with io.open(path, "wb") as handle:
            handle.write(payload)
        return path

    def fetch(self, path):
        return list_fetch.process_fetched_list_zip(BOT, path)

    def assert_held_list_untouched(self):
        self.assertTrue(os.path.exists(self.held_file),
                        "the list already held was lost while refusing its "
                        "replacement")
        with io.open(self.held_file, encoding="utf-8") as handle:
            self.assertIn("Held Track.mp3", handle.read())
        self.assertEqual(config.fetched_bot_lists.get(BOT.lower()), self.held_entry)
        self.assertFalse(os.path.exists(self.held_dir + ".previous"))

    def assert_installed(self, count):
        entry = config.fetched_bot_lists.get(BOT.lower())
        self.assertNotEqual(entry, self.held_entry)
        self.assertEqual(entry["entry_count"], count)
        self.assertTrue(os.path.exists(entry["list_path"]))
        self.assertFalse(os.path.exists(self.held_file),
                         "a good fetch replaces the held list")


# ---------------------------------------------------------------------------
# Part 1: binary never passes the plain-text route.
# ---------------------------------------------------------------------------

class BinaryNeverPassesThePlainTextRoute(HeldListFixture):

    def random_bytes(self):
        rng = random.Random(1200)
        return bytes(rng.getrandbits(8) for _ in range(64 * 1024))

    def test_the_sample_would_have_passed_the_old_check(self):
        """THE CONTROL for the test below: this seed's bytes are the kind the
        issue measured - the old splitlines() test finds a request line in
        them."""
        head = self.random_bytes().decode("utf-8", "replace")
        self.assertTrue(any(line.lstrip().startswith("!")
                            for line in head.splitlines()))

    def test_64kb_of_random_bytes_are_refused_and_the_held_list_survives(self):
        ok, reason = self.fetch(self.incoming(self.random_bytes()))

        self.assertFalse(ok)
        self.assertIn("binary data", reason)
        self.assert_held_list_untouched()

    def test_a_short_binary_with_no_nul_is_refused(self):
        """A NUL settles most binaries; this one has none, and is caught on
        its control characters."""
        rng = random.Random(1201)
        noise = bytes(rng.randrange(1, 256) for _ in range(2048))
        payload = noise + b"\n!PeerBot Track.mp3\n" + noise

        ok, reason = self.fetch(self.incoming(payload))

        self.assertFalse(ok)
        self.assertIn("binary data", reason)
        self.assert_held_list_untouched()

    def test_a_single_nul_is_enough(self):
        payload = list_text("PeerBot", ["New Track.mp3"]).encode("utf-8") + b"\x00"

        ok, reason = self.fetch(self.incoming(payload))

        self.assertFalse(ok)
        self.assertIn("binary data", reason)

    def test_a_7z_list_is_refused_by_name(self):
        payload = b"7z\xbc\xaf\x27\x1c\x00\x04" + list_text(
            "PeerBot", ["New Track.mp3"]).encode("utf-8")

        ok, reason = self.fetch(self.incoming(payload, "PeerBot.7z"))

        self.assertFalse(ok)
        self.assertIn("7z archive", reason)
        self.assert_held_list_untouched()

    def test_a_request_line_after_a_unicode_line_separator_is_not_one(self):
        """str.splitlines() ends a line at U+2028; the list parser does not,
        so the "!" below never starts a row and the file holds none."""
        payload = ("A banner\u2028!PeerBot Track.mp3  ::INFO:: 4.00MB\n"
                   ).encode("utf-8")

        ok, reason = self.fetch(self.incoming(payload, "PeerBot.txt"))

        self.assertFalse(ok)
        self.assertIn("no request lines", reason)
        self.assert_held_list_untouched()


class RealTextListsStillPass(HeldListFixture):
    """Every refusal above would also pass against a route that refused
    everything."""

    def fetch_text(self, payload):
        ok, reason = self.fetch(self.incoming(payload, "PeerBot.txt"))
        self.assertTrue(ok, reason)
        return config.fetched_bot_lists[BOT.lower()]["entry_count"]

    def test_a_utf8_list(self):
        text = list_text("PeerBot", ["One.mp3", "Two.mp3"])
        self.assertEqual(self.fetch_text(text.encode("utf-8")), 2)
        self.assert_installed(2)

    def test_a_legacy_code_page_list(self):
        """Nearly half of a Greek cp1253 list decodes to U+FFFD - as much as
        random bytes do - which is why that ratio is not the test."""
        text = list_text("PeerBot", ["01 Καλλιτέχνης - Τραγούδι.mp3",
                                     "02 Καλλιτέχνης - Τραγούδι.mp3"])
        self.assertEqual(self.fetch_text(text.encode("cp1253")), 2)

    def test_crlf_and_lone_cr_line_ends(self):
        text = list_text("PeerBot", ["One.mp3", "Two.mp3", "Three.mp3"])
        self.assertEqual(self.fetch_text(text.replace("\n", "\r\n").encode("utf-8")), 3)
        self.assertEqual(self.fetch_text(text.replace("\n", "\r").encode("utf-8")), 3)

    def test_irc_formatting_codes_in_the_banner(self):
        banner = "\x02\x0304Welcome\x0f to \x1fthe\x1f \x1dlist\x1d \x16!\x16\x1a\n"
        text = banner * 20 + list_text("PeerBot", ["One.mp3"])
        self.assertEqual(self.fetch_text(text.encode("utf-8")), 1)


class NoRarProgram(HeldListFixture):

    def test_a_rar_list_is_refused_with_the_reason(self):
        payload = b"Rar!\x1a\x07\x01\x00" + b"\x00\x11\x22" * 300
        with mock.patch.object(platform_compat, "rar_command",
                               lambda configured=None: None):
            ok, reason = self.fetch(self.incoming(payload, "PeerBot.rar"))

        self.assertFalse(ok)
        self.assertEqual(reason, "the list arrived as a RAR archive, and no rar "
                                 "program was found to open it - install rar, "
                                 "or set RAR_BINARY to where it is")
        self.assert_held_list_untouched()


# ---------------------------------------------------------------------------
# Part 2: RAR lists, against a stand-in rar that runs everywhere.
# ---------------------------------------------------------------------------

class StandInRar(HeldListFixture):

    def setUp(self):
        super().setUp()
        # No .py on the name: Python runs a script by any name, and this one
        # is created here, not a file of the repository expected to exist.
        self.fake = os.path.join(self.tree.root, "stand_in_rar")
        with io.open(self.fake, "w", encoding="utf-8") as handle:
            handle.write(FAKE_RAR)
        self.calls = []

        def argv(rar_bin, args):
            self.calls.append(list(args))
            return [sys.executable, self.fake] + list(args)

        # Every rar child started, so a test can ask that none is left
        # running - the watchdog and an early stop kill by the child's own
        # handle, and a child that outlived its fetch would be a leak.
        self.children = children = []

        class RecordedChild(list_fetch._RarChild):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                children.append(self)

        for patcher in (mock.patch.object(list_fetch, "_rar_argv", argv),
                        mock.patch.object(list_fetch, "_RarChild", RecordedChild),
                        mock.patch.object(platform_compat, "rar_command",
                                          lambda configured=None: "rar")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_no_rar_left_running(self):
        self.assertTrue(self.children)
        for child in self.children:
            self.assertIsNotNone(child.process.returncode,
                                 "a rar child outlived the fetch that started it")

    def rar(self, entries=(), **spec):
        spec["entries"] = list(entries)
        payload = b"Rar!\x1a\x07\x01\x00" + json.dumps(spec).encode("utf-8")
        return self.incoming(payload, "PeerBot.rar")

    def refused(self, path, *expected):
        ok, reason = self.fetch(path)
        self.assertFalse(ok)
        for text in expected:
            self.assertIn(text, reason)
        self.assert_held_list_untouched()
        return reason

    def test_a_rar_list_is_installed_with_its_rows(self):
        ok, reason = self.fetch(self.rar([
            {"name": "PeerBot-2026-09-07.txt",
             "data": list_text("PeerBot", ["A.mp3", "B.mp3", "C.mp3"])}]))

        self.assertTrue(ok, reason)
        self.assert_installed(3)

    def test_the_list_is_picked_as_from_a_zip(self):
        """The main list is the one _pick_list_file() chooses; the album list
        beside it is kept as its own tab, as with a zip; a non-.txt member is
        never unpacked."""
        ok, reason = self.fetch(self.rar([
            {"name": "Lists", "type": "Directory"},
            {"name": "Lists/PeerBot-RAR-2026-09-07.txt",
             "data": "List of Entire Album Folders (!rar)\n" + "=" * 90 + "\n"},
            {"name": "Lists/PeerBot-2026-09-07.txt",
             "data": list_text("PeerBot", ["A.mp3", "B.mp3"])},
            {"name": "cover.jpg", "data": "not an image"}]))

        self.assertTrue(ok, reason)
        entry = config.fetched_bot_lists[BOT.lower()]
        self.assertEqual(os.path.basename(entry["list_path"]), "PeerBot-2026-09-07.txt")
        self.assertEqual(entry["entry_count"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.held_dir, "cover.jpg")))
        asked = [call[-1] for call in self.calls if call[0] == "p"]
        self.assertNotIn("cover.jpg", asked)

    def test_every_member_is_listed_before_any_is_unpacked(self):
        self.fetch(self.rar([{"name": "PeerBot-2026-09-07.txt",
                              "data": list_text("PeerBot", ["A.mp3"])}]))

        self.assertEqual([call[0] for call in self.calls], ["lt", "p"])
        self.assertIn("-p-", self.calls[0])
        self.assertIn("-p-", self.calls[1])

    def test_a_traversing_name_is_refused(self):
        self.refused(self.rar([{"name": "../../evil.txt", "data": "!x y\n"}]),
                     "path traversal")
        self.assertFalse(os.path.exists(os.path.join(self.tree.root, "evil.txt")))

    def test_absolute_names_and_drive_letters_are_refused(self):
        for name in ("/etc/evil.txt", "C:/evil.txt", "C:\\evil.txt"):
            with self.subTest(name=name):
                self.refused(self.rar([{"name": name, "data": "!x y\n"}]),
                             "absolute path")

    def test_an_all_dots_component_is_refused(self):
        """Windows resolves "...." to the directory itself, so there the
        containment check is what refuses it; elsewhere the dots check."""
        self.refused(self.rar([{"name": "..../list.txt", "data": "!x y\n"}]),
                     "RAR entry '..../list.txt'")

    def test_too_many_entries_are_refused(self):
        """One past the most allowed. The listing stops reading there, rather
        than holding a hostile archive's whole listing first."""
        entries = [{"name": f"f{n}.txt", "data": "!x y\n"} for n in range(301)]
        self.refused(self.rar(entries), "more than 300 entries")
        self.assertEqual([call[0] for call in self.calls], ["lt"],
                         "nothing may be unpacked from a refused archive")

    def test_exactly_the_most_entries_allowed_is_not_refused_for_its_count(self):
        entries = [{"name": f"d{n}", "type": "Directory"} for n in range(299)]
        entries.append({"name": "PeerBot-2026-09-07.txt",
                        "data": list_text("PeerBot", ["A.mp3"])})
        ok, reason = self.fetch(self.rar(entries))
        self.assertTrue(ok, reason)

    def test_a_declared_total_over_the_budget_is_refused(self):
        self.set_config(MAX_FETCH_FILE_SIZE=1000)
        self.refused(self.rar([{"name": "a.txt", "data": "!x y\n", "size": 600},
                               {"name": "b.txt", "data": "!x y\n", "size": 600}]),
                     "zip-bomb guard")
        self.assertEqual([call[0] for call in self.calls], ["lt"])

    def test_a_member_that_unpacks_past_its_declared_size_is_stopped(self):
        """The stand-in never stops writing. Stopped by the size check, this
        is refused at once; left to the watchdog, it would be a timeout."""
        with mock.patch.object(list_fetch, "_RAR_LIST_TIMEOUT", 30):
            self.refused(self.rar([{"name": "PeerBot-2026-09-07.txt", "size": 100,
                                    "flood": True}]),
                         "past its declared size")
        self.assert_no_rar_left_running()

    def test_one_byte_past_the_declared_size_is_enough(self):
        self.refused(self.rar([{"name": "PeerBot-2026-09-07.txt", "size": 100,
                                "data": "!" * 101}]),
                     "past its declared size of 100 bytes")

    def test_a_member_rar_cannot_unpack_is_refused(self):
        listing = ("        Name: PeerBot-2026-09-07.txt\n"
                   "        Type: File\n"
                   "        Size: 5\n")
        self.refused(self.rar([], listing=listing),
                     "rar could not unpack 'PeerBot-2026-09-07.txt' (exit code 10)")

    def test_a_link_is_refused(self):
        self.refused(self.rar([{"name": "PeerBot-2026-09-07.txt",
                                "type": "Unix symbolic link"}]),
                     "not a file or a folder")

    def test_names_rar_would_read_as_a_pattern_or_a_switch_are_refused(self):
        for name in ("list*.txt", "li?t.txt", "@list.txt", "-list.txt", "li\tst.txt"):
            with self.subTest(name=name):
                self.refused(self.rar([{"name": name, "data": "!x y\n"}]),
                             "pattern or a switch")

    def test_a_name_twice_is_refused(self):
        self.refused(self.rar([{"name": "List.txt", "data": "!x y\n"},
                               {"name": "LIST.TXT", "data": "!x y\n"}]),
                     "twice")

    def test_a_listing_rar_fails_on_is_refused(self):
        self.refused(self.rar([], listing_exit=3), "exit code 3")

    def test_an_unreadable_listing_is_refused(self):
        listing = ("        Name: PeerBot-2026-09-07.txt\n"
                   "        Type: File\n"
                   "        Size: 5\n"
                   "        this line is not a field\n")
        self.refused(self.rar([], listing=listing), "could not be read")

    def test_an_overlong_listing_line_is_refused(self):
        listing = "        Name: " + "x" * 20000 + ".txt\n"
        self.refused(self.rar([], listing=listing), "too long")

    def test_a_size_that_is_not_a_number_is_refused(self):
        listing = ("        Name: PeerBot-2026-09-07.txt\n"
                   "        Type: File\n"
                   "        Size: 1,024\n")
        self.refused(self.rar([], listing=listing), "could not be read")

    def test_a_field_given_twice_is_refused(self):
        """A name holding a line break could otherwise smuggle in a second
        Size for its own entry."""
        listing = ("        Name: PeerBot-2026-09-07.txt\n"
                   "        Type: File\n"
                   "        Size: 5\n"
                   "        Size: 10\n")
        self.refused(self.rar([], listing=listing), "could not be read")

    def test_an_empty_archive_is_refused(self):
        self.refused(self.rar([]), "RAR archive is empty")

    def test_the_archive_comment_is_not_read_as_entries(self):
        """The sender writes the comment. Read as part of the listing, this
        one would add an entry that traverses."""
        comment = ("        Name: ../../evil.txt\n"
                   "        Type: File\n"
                   "        Size: 1\n")
        ok, reason = self.fetch(self.rar(
            [{"name": "PeerBot-2026-09-07.txt",
              "data": list_text("PeerBot", ["A.mp3"])}], comment=comment))
        self.assertTrue(ok, reason)

    def test_the_text_ceiling_still_applies(self):
        self.set_config(MAX_LIST_TEXT_SIZE=200)
        self.refused(self.rar([{"name": "PeerBot-2026-09-07.txt",
                                "data": list_text("PeerBot", ["A.mp3"] * 20)}]),
                     "MAX_LIST_TEXT_SIZE")

    def test_a_hung_rar_is_killed_by_its_own_handle(self):
        with mock.patch.object(list_fetch, "_RAR_LIST_TIMEOUT", 2):
            self.refused(self.rar([{"name": "PeerBot-2026-09-07.txt", "hang": True,
                                    "data": "!x y\n"}]),
                         "took more than 2 seconds")
        self.assert_no_rar_left_running()

    def test_a_hung_listing_is_killed_too(self):
        with mock.patch.object(list_fetch, "_RAR_LIST_TIMEOUT", 2):
            self.refused(self.rar([], hang_listing=True), "to list the archive")
        self.assert_no_rar_left_running()


# ---------------------------------------------------------------------------
# Part 2 against the real program. Skipped where rar is not installed; the
# stand-in cases above cover the same paths on every runner.
# ---------------------------------------------------------------------------

REAL_RAR = platform_compat.rar_command(None)


@unittest.skipUnless(REAL_RAR, "no rar program on this machine")
class RealRar(HeldListFixture):

    def pack(self, method, files):
        import subprocess
        source = os.path.join(self.tree.root, "pack-" + method)
        os.makedirs(source)
        names = []
        for name, text in files:
            with io.open(os.path.join(source, name), "w", encoding="utf-8") as handle:
                handle.write(text)
            names.append(os.path.join(source, name))
        archive = os.path.join(self.tree.root, f"PeerBot{method}.rar")
        done = subprocess.run([REAL_RAR, "a", method, "-ep", "-inul", "-cfg-",
                               archive] + names, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        return archive

    def test_a_store_mode_rar_list_is_installed(self):
        """Named outside ASCII, which rar prints in the console's code page
        unless asked for UTF-8."""
        titles = [f"Track {n:03d}.flac" for n in range(40)]
        archive = self.pack("-m0", [("PeerBot Λίστα-2026-09-07.txt",
                                     list_text("PeerBot", titles))])

        ok, reason = self.fetch(archive)

        self.assertTrue(ok, reason)
        self.assert_installed(40)

    def test_a_compressed_rar_list_is_installed(self):
        titles = [f"Track {n:03d}.flac" for n in range(500)]
        archive = self.pack("-m5", [
            ("PeerBot-2026-09-07.txt", list_text("PeerBot", titles)),
            ("PeerBot-RAR-2026-09-07.txt", "List of Entire Album Folders (!rar)\n")])

        ok, reason = self.fetch(archive)

        self.assertTrue(ok, reason)
        self.assert_installed(500)
        entry = config.fetched_bot_lists[BOT.lower()]
        self.assertEqual(os.path.basename(entry["list_path"]), "PeerBot-2026-09-07.txt")

    def test_a_damaged_rar_is_refused(self):
        ok, reason = self.fetch(self.incoming(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 400,
                                              "PeerBot.rar"))
        self.assertFalse(ok)
        self.assertIn("rar could not read the archive", reason)
        self.assert_held_list_untouched()


if __name__ == "__main__":
    unittest.main()
