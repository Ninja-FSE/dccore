"""Both sweeps read every tracked file that is not known binary (#1443).

The identifier sweep and the conflict-marker sweep used to read only files
whose suffix was on a list of TEXT suffixes, so a file type nobody had added
to it - the mIRC DLL's C sources, LICENSE, the dotfiles - was never read by
either, with both guards green. The rule is now the other way round: every
file is read unless its suffix is on BINARY_SUFFIXES, and a file that does not
decode is reported rather than passed.

These tests drive the real reader, and the real sweep test methods, against
temporary trees: a clean repository passes whatever the reader does, so the
repository alone cannot show that a C file or an undecodable file is caught.
Every name planted below is an invented placeholder; the one "forbidden" word
is denied only inside these tests, by patching FORBIDDEN.
"""

import codecs
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests import test_no_personal_identifiers_ship as sweep  # noqa: E402
from tests import test_no_conflict_marker_is_left_behind as conflicts  # noqa: E402

# An invented word, denied only inside the tests that patch FORBIDDEN.
PLANTED = "Zyxquorpheld"

# A suffix nobody will ever put on a list of text types. A file carrying it
# is read only if the reader really does read everything not known binary:
# a whitelist of the types this repository has today would skip it.
INVENTED_SUFFIX = ".zq1443"

# Built from parts, as the conflict sweep's own file does, so this file
# carries no marker line itself.
START = "<" * 7
SEPARATOR = "=" * 7
END = ">" * 7
A_CONFLICT = (START + " HEAD\nours\n" + SEPARATOR + "\ntheirs\n"
              + END + " other-branch\n")

# Latin-1 bytes that are not valid UTF-8: a text file in an encoding the
# sweep cannot read.
LATIN_1 = "caf\u00e9 menu\n".encode("latin-1")


def opening_a_directory_fails():
    """True if open(<a directory>, "rb") raises something other than
    FileNotFoundError here - IsADirectoryError on POSIX, PermissionError on
    Windows. A directory given a file's name is then a file that cannot be
    opened, on every platform, without touching permission bits."""
    probe = tempfile.mkdtemp()
    try:
        try:
            with io.open(probe, "rb"):
                return False
        except FileNotFoundError:
            return False
        except OSError:
            return True
    finally:
        os.rmdir(probe)


def remove_tree(root):
    """shutil.rmtree, after making every file writable: git writes its
    object files read-only, and on Windows rmtree cannot delete those.
    Errors are NOT ignored, so a tree this file leaks shows up as one."""
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    shutil.rmtree(root)


class TreeMixin:
    """A temporary repository the reader, or a sweep, is pointed at."""

    def make_tree(self, files):
        """`files` maps repository-relative names ("/"-separated) to bytes,
        or to None for a DIRECTORY with that name - a file that cannot be
        opened. Returns the root, which is removed when the test ends."""
        root = tempfile.mkdtemp(prefix="sweep-1443-")
        self.addCleanup(remove_tree, root)
        for name, data in files.items():
            path = os.path.join(root, *name.split("/"))
            if data is None:
                os.makedirs(path)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
        return root

    def point(self, module, name, value):
        """`module.name` is `value` until the test ends."""
        patcher = mock.patch.object(module, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


def failure_of(base, method):
    """The AssertionError message `method` of a copy of TestCase `base`
    fails with, or None if it passes.

    Run by hand rather than through a TestSuite: a suite run calls the
    process-wide module fixtures and cleanups too. A subclass, so the real
    class's cached setUpClass state is never overwritten."""
    probe = type("Probe", (base,), {})
    probe.setUpClass()
    try:
        case = probe(method)
        try:
            case.setUp()
            getattr(case, method)()
        except AssertionError as err:
            return str(err)
        except unittest.SkipTest as skip:
            # A probe that skipped proved nothing: never let it pass as the
            # outer test passing or skipping.
            raise RuntimeError(f"the probed sweep skipped: {skip}") from None
        return None
    finally:
        probe.tearDownClass()


class DecodeText(unittest.TestCase):

    def test_utf8_with_an_accent_is_text(self):
        text = "na\u00efve caf\u00e9\n"
        self.assertEqual(sweep.decode_text(text.encode("utf-8")), text)

    def test_utf8_with_a_byte_order_mark_is_text(self):
        data = codecs.BOM_UTF8 + ("x " + PLANTED + " y").encode("utf-8")

        text = sweep.decode_text(data)

        self.assertIsNotNone(text)
        self.assertIn(PLANTED, [word for word, _ in sweep.words(text)])

    def test_utf16_with_a_byte_order_mark_is_text_and_its_words_are_read(self):
        """How Visual Studio saves a resource script. A name in one must be
        found, so it is decoded, not reported."""
        source = "// by " + PLANTED + "\r\nIDI_ICON ICON \"a.ico\"\r\n"
        for label, data in (
                ("LE", codecs.BOM_UTF16_LE + source.encode("utf-16-le")),
                ("BE", codecs.BOM_UTF16_BE + source.encode("utf-16-be"))):
            with self.subTest(label):
                text = sweep.decode_text(data)
                self.assertEqual(text, source)
                self.assertIn(PLANTED, [word for word, _ in sweep.words(text)])

    def test_utf16_without_a_byte_order_mark_is_not_text(self):
        """Valid UTF-8 when its text is ASCII, with a NUL between letters -
        so read as text, every letter would be a word and a name would pass."""
        data = ("by " + PLANTED).encode("utf-16-le")
        self.assertEqual(data.decode("utf-8")[:2], "b\0",
                         "precondition: it really does decode as UTF-8")

        self.assertIsNone(sweep.decode_text(data))

    def test_latin1_is_not_text(self):
        self.assertIsNone(sweep.decode_text(LATIN_1))

    def test_utf8_with_a_nul_is_not_text(self):
        self.assertIsNone(sweep.decode_text(b"MZ\x90\x00\x03 header"))

    def test_truncated_utf16_is_not_text(self):
        """An odd number of bytes after the mark: not a half-read file
        counted as scanned."""
        self.assertIsNone(sweep.decode_text(codecs.BOM_UTF16_LE + b"a\x00b"))

    def test_an_empty_file_is_empty_text(self):
        """Read, with nothing in it - not undecodable."""
        self.assertEqual(sweep.decode_text(b""), "")


class IsBinaryName(unittest.TestCase):

    def test_a_binary_suffix_in_any_case(self):
        for name in ("bin/plugin.dll", "bin/PLUGIN.DLL", "art/Logo.PnG",
                     "tool.Exe", "fonts/face.woff2", "media/Clip.MP3"):
            with self.subTest(name=name):
                self.assertTrue(sweep.is_binary_name(name))

    def test_sources_dotfiles_and_extensionless_files_are_not_binary(self):
        for name in ("native/plugin.c", "native/plugin.h", "native/res.rc",
                     "LICENSE", ".gitattributes", ".gitignore",
                     "data/.gitkeep", "site/.nojekyll", "tools/runner",
                     "notes.dll.txt", "dll", "native/plugin" + INVENTED_SUFFIX):
            with self.subTest(name=name):
                self.assertFalse(sweep.is_binary_name(name))

    def test_every_suffix_on_the_list_can_match(self):
        """Compared against a lowercased name, so an entry with a capital
        letter, or without its dot, would never mean what it says."""
        for suffix in sweep.BINARY_SUFFIXES:
            with self.subTest(suffix=suffix):
                self.assertTrue(suffix.startswith("."))
                self.assertEqual(suffix, suffix.lower())


class ReadTextFiles(TreeMixin, unittest.TestCase):

    def test_every_kind_of_text_file_is_read(self):
        files = {
            "native/plugin.c": b"/* c */\n",
            "native/plugin.h": b"/* h */\n",
            "native/res.rc": codecs.BOM_UTF16_LE + "// rc\r\n".encode("utf-16-le"),
            "native/plugin" + INVENTED_SUFFIX: b"a type nobody listed\n",
            "LICENSE": b"licence text\n",
            ".gitattributes": b"* text=auto\n",
            "data/.gitkeep": b"",
            "tools/runner": b"#!/bin/sh\n",
            "src/module.py": b"x = 1\n",
        }
        root = self.make_tree(files)

        texts, unread = sweep.read_text_files(list(files), root)

        self.assertEqual(sorted(texts), sorted(files))
        self.assertEqual(texts["native/res.rc"], "// rc\r\n")
        self.assertEqual(texts["native/plugin" + INVENTED_SUFFIX], "a type nobody listed\n")
        self.assertEqual(texts["data/.gitkeep"], "")
        self.assertEqual(unread, [])

    def test_the_root_it_is_given_is_the_root_it_reads(self):
        """Not either module's REPO_ROOT: the caller passes the directory it
        asked git in, so the two can never disagree."""
        root = self.make_tree({"only/here.c": b"int here;\n"})

        texts, _unread = sweep.read_text_files(["only/here.c"], root)

        self.assertEqual(texts, {"only/here.c": "int here;\n"})
        self.assertNotEqual(os.path.normcase(root), os.path.normcase(sweep.REPO_ROOT))

    def test_a_binary_suffix_is_never_opened(self):
        """Each binary name is a DIRECTORY here, which cannot be opened as a
        file: reading one would raise, not return."""
        if not opening_a_directory_fails():
            self.skipTest("opening a directory as a file does not fail here")
        root = self.make_tree({"bin/plugin.dll": None, "bin/OTHER.DLL": None,
                               "art/logo.png": None, "native/plugin.c": b"int x;\n"})

        texts, unread = sweep.read_text_files(
            ["bin/plugin.dll", "bin/OTHER.DLL", "art/logo.png", "native/plugin.c"], root)

        self.assertEqual(list(texts), ["native/plugin.c"])
        self.assertEqual(unread, [])

    def test_a_binary_suffix_is_never_opened_counting_the_opens(self):
        """The same, with no reliance on the platform: every open is
        recorded, and no binary name is among them."""
        root = self.make_tree({"bin/plugin.dll": b"MZ\x00\x00", "bin/OTHER.DLL": b"MZ\x00",
                               "native/plugin.c": b"int x;\n"})
        opened = []

        def recording_open(path, *args, **kwargs):
            opened.append(os.path.basename(path))
            return io.open(path, *args, **kwargs)

        with mock.patch.object(sweep, "io", types.SimpleNamespace(open=recording_open)):
            texts, unread = sweep.read_text_files(
                ["bin/plugin.dll", "bin/OTHER.DLL", "native/plugin.c"], root)

        self.assertEqual(opened, ["plugin.c"])
        self.assertEqual(list(texts), ["native/plugin.c"])
        self.assertEqual(unread, [])

    def test_an_undecodable_file_without_a_binary_suffix_is_unread(self):
        root = self.make_tree({"notes/menu.txt": LATIN_1,
                               "native/wide.c": "int x;".encode("utf-16-le"),
                               "tools/blob": b"\x7fELF\x02\x01\x00\x00",
                               "native/plugin.c": b"int x;\n"})
        names = ["notes/menu.txt", "native/wide.c", "tools/blob", "native/plugin.c"]

        texts, unread = sweep.read_text_files(names, root)

        self.assertEqual(unread, ["notes/menu.txt", "native/wide.c", "tools/blob"])
        self.assertEqual(list(texts), ["native/plugin.c"])

    def test_undecodable_by_design_is_neither_read_nor_reported(self):
        root = self.make_tree({"tools/blob": b"\x7fELF\x02\x01\x00\x00",
                               "notes/menu.txt": LATIN_1})

        with mock.patch.object(sweep, "UNDECODABLE_BY_DESIGN",
                               frozenset({"tools/blob"})):
            texts, unread = sweep.read_text_files(["tools/blob", "notes/menu.txt"], root)

        self.assertEqual(texts, {})
        self.assertEqual(unread, ["notes/menu.txt"],
                         "only the listed path is excused, not every one")

    def test_a_tracked_file_deleted_from_the_working_tree_is_skipped(self):
        root = self.make_tree({"native/plugin.c": b"int x;\n"})

        texts, unread = sweep.read_text_files(["native/gone.c", "native/plugin.c"], root)

        self.assertEqual(list(texts), ["native/plugin.c"])
        self.assertEqual(unread, [])

    def test_a_name_git_could_not_decode_is_reported_not_skipped(self):
        """A tracked name in bytes that are not UTF-8 is decoded with
        "replace", and the path that makes does not exist. That is not a
        deleted file: whatever is under the real name was never read."""
        root = self.make_tree({"native/plugin.c": b"int x;\n"})
        mangled = "native/caf\ufffd.c"

        texts, unread = sweep.read_text_files([mangled, "native/plugin.c"], root)

        self.assertEqual(unread, [mangled])
        self.assertEqual(list(texts), ["native/plugin.c"])

    def test_a_file_that_cannot_be_opened_raises_a_directory(self):
        """Only a missing file is skipped. One that exists and cannot be read
        was not scanned, and must not pass silently as if it was gone."""
        if not opening_a_directory_fails():
            self.skipTest("opening a directory as a file does not fail here")
        root = self.make_tree({"native/locked.c": None})

        with self.assertRaises(OSError) as caught:
            sweep.read_text_files(["native/locked.c"], root)
        self.assertNotIsInstance(caught.exception, FileNotFoundError)

    def test_a_file_that_cannot_be_opened_raises_permission_bits(self):
        root = self.make_tree({"native/locked.c": b"int x;\n"})
        path = os.path.join(root, "native", "locked.c")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            with io.open(path, "rb"):
                pass
        except PermissionError:
            pass
        else:
            self.skipTest("a file with no permission bits is still readable "
                          "here (Windows, or running as root); "
                          "the patched-open test covers this everywhere")

        with self.assertRaises(PermissionError):
            sweep.read_text_files(["native/locked.c"], root)

    def test_a_file_that_cannot_be_opened_raises_patched_open(self):
        """The permission case on every platform: open raises as it would
        for a file this user may not read."""
        root = self.make_tree({"native/locked.c": b"int x;\n"})

        def refusing_open(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", path)

        with mock.patch.object(sweep, "io", types.SimpleNamespace(open=refusing_open)):
            with self.assertRaises(PermissionError):
                sweep.read_text_files(["native/locked.c"], root)

    def test_a_path_that_exists_but_opens_as_missing_raises(self):
        """FileNotFoundError is skipped only when nothing is at the path.
        If something is there, it was not read - so it is not skipped."""
        root = self.make_tree({"native/there.c": b"int x;\n"})

        def missing_open(path, *args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", path)

        with mock.patch.object(sweep, "io", types.SimpleNamespace(open=missing_open)):
            with self.assertRaises(FileNotFoundError):
                sweep.read_text_files(["native/there.c"], root)

    def test_only_not_found_counts_as_deleted(self):
        """Nothing is at the path, but the error is not "not found" - on
        POSIX, a parent of it that is a file raises NotADirectoryError. That
        is not a deleted file, and it is not skipped as one."""
        root = self.make_tree({})

        def odd_open(path, *args, **kwargs):
            raise NotADirectoryError(20, "Not a directory", path)

        with mock.patch.object(sweep, "io", types.SimpleNamespace(open=odd_open)):
            with self.assertRaises(NotADirectoryError):
                sweep.read_text_files(["LICENSE/inside.c"], root)


class ASymlinkIsReadAsWhatShips(TreeMixin, unittest.TestCase):
    """git stores a symlink as its link text and `git archive` ships that
    text, so that is what is scanned - never what the link points at."""

    def test_with_the_link_calls_faked_everywhere(self):
        """Runs on every platform. Each "link" is a path os.path.islink
        answers True for; one of them has a real file behind it, whose
        content must NOT be what is read."""
        root = self.make_tree({"links/to_file.c": b"the TARGET's content\n"})
        links = {
            "links/to_file.c": "../native/target.c",
            "links/dangling.c": "../native/" + PLANTED + ".c",
            "links/to_dir": "../native",
            "links/icon.png": "../art/real.png",
        }
        by_path = {os.path.normcase(os.path.normpath(os.path.join(root, name))): text
                   for name, text in links.items()}
        real_islink = os.path.islink

        def islink(path):
            return (os.path.normcase(os.path.normpath(path)) in by_path
                    or real_islink(path))

        def readlink(path, *args, **kwargs):
            return by_path[os.path.normcase(os.path.normpath(path))]

        with mock.patch.object(sweep.os.path, "islink", islink), \
                mock.patch.object(sweep.os, "readlink", readlink):
            texts, unread = sweep.read_text_files(list(links), root)

        self.assertEqual(texts, links)
        self.assertEqual(unread, [])
        self.assertIn(PLANTED, [w for w, _ in sweep.words(texts["links/dangling.c"])],
                      "a name in a link's text is a name that ships")

    def test_with_real_symlinks(self):
        root = self.make_tree({"native/target.c": b"the TARGET's content\n",
                               "native/inner/x.c": b"int x;\n"})
        made = {}
        try:
            for name, target, is_dir in (
                    ("links/to_file.c", os.path.join("..", "native", "target.c"), False),
                    ("links/to_dir", os.path.join("..", "native", "inner"), True),
                    ("links/dangling.c", os.path.join("..", "native", PLANTED + ".c"), False)):
                path = os.path.join(root, *name.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(target, path, target_is_directory=is_dir)
                made[name] = path
        except (OSError, NotImplementedError, AttributeError) as err:
            # The class name only: the message is localised and names a
            # temp path, so it would make every run's skip reason unique.
            self.skipTest(f"symlinks cannot be created here ({type(err).__name__}; "
                          "on Windows they need Developer Mode or admin); "
                          "the faked-link test covers this everywhere")

        texts, unread = sweep.read_text_files(list(made), root)

        self.assertEqual(texts, {name: os.readlink(path) for name, path in made.items()})
        self.assertNotIn("TARGET", texts["links/to_file.c"])
        self.assertIn(PLANTED, texts["links/dangling.c"])
        self.assertEqual(unread, [])


class TheRealTree(unittest.TestCase):
    """What the audit found unscanned is read now, and what is skipped by
    suffix really is binary."""

    def tracked(self):
        try:
            listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                                    check=True, capture_output=True, timeout=120)
        except Exception as err:  # noqa: BLE001 - no git, or not a checkout
            self.skipTest(f"could not list the tracked files ({err})")
        return [n for n in listed.stdout.decode("utf-8", "replace").split("\0") if n]

    def test_the_c_sources_dotfiles_and_licence_are_read(self):
        shipped, reason = sweep.shipped_paths()
        if reason:
            self.skipTest(reason)
        wanted = [name for name in shipped
                  if name.endswith((".c", ".h", ".rc"))
                  or os.path.basename(name).startswith(".")
                  or name == "LICENSE"]
        for must in ("LICENSE", ".gitattributes", ".gitignore"):
            self.assertIn(must, wanted)
        self.assertTrue([n for n in wanted if n.endswith(".c")],
                        "no C source ships any more - this test's premise changed")

        texts, unread = sweep.read_text_files(wanted, sweep.REPO_ROOT)

        self.assertEqual(sorted(texts), sorted(wanted))
        self.assertEqual(unread, [])

    def test_everything_skipped_by_suffix_really_is_binary(self):
        """The other way BINARY_SUFFIXES could fail open: a suffix on it that
        a text file carries. Every tracked file it skips must fail to decode."""
        skipped = [name for name in self.tracked() if sweep.is_binary_name(name)]
        self.assertTrue(skipped, "nothing tracked is skipped as binary - "
                                 "the DLLs and the preview image should be")

        decodable = []
        for name in skipped:
            try:
                with open(os.path.join(REPO_ROOT, name), "rb") as handle:
                    data = handle.read()
            except FileNotFoundError:
                continue
            if sweep.decode_text(data) is not None:
                decodable.append(name)

        self.assertEqual(decodable, [],
                         "these are skipped unread by suffix but decode as text")

    def test_every_binary_type_in_gitattributes_is_skipped_as_binary(self):
        """.gitattributes already says which file types are binary. A type
        it names and BINARY_SUFFIXES does not would be read, fail to decode
        and fail both sweeps the day such a file is first committed."""
        with open(os.path.join(REPO_ROOT, ".gitattributes"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        declared = [match.group(1).lower() for match in
                    (re.match(r"^\*(\.[^\s/*]+)\s+(?:\S+\s+)*binary(?:\s|$)", line)
                     for line in lines) if match]
        self.assertIn(".dll", declared, "the pattern no longer reads .gitattributes")

        missing = [suffix for suffix in declared if suffix not in sweep.BINARY_SUFFIXES]

        self.assertEqual(missing, [], "binary in .gitattributes, not on BINARY_SUFFIXES")


class TheIdentifierSweepOnATempTree(TreeMixin, unittest.TestCase):
    """The real NothingIdentifyingShips methods, pointed at a temp tree."""

    def point_sweep_at(self, files):
        # Only what the sweep itself uses: its REPO_ROOT (setUpClass passes
        # it to the reader), its file list and its denylist.
        self.point(sweep, "REPO_ROOT", self.make_tree(files))
        self.point(sweep, "shipped_paths", lambda: (list(files), None))
        self.point(sweep, "FORBIDDEN", {sweep.word_hash(PLANTED): "a planted placeholder"})

    def test_a_name_in_a_c_comment_is_found(self):
        self.point_sweep_at({"native/plugin.c": ("int x;\n/* thanks " + PLANTED
                                                 + " */\n").encode("utf-8"),
                             "LICENSE": b"clean\n"})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_no_forbidden_identifier_is_in_the_export")

        self.assertIsNotNone(failure, "a planted name in a .c file passed")
        self.assertIn("native/plugin.c:2", failure)

    def test_a_name_in_a_file_of_a_type_nobody_listed_is_found(self):
        name = "native/plugin" + INVENTED_SUFFIX
        self.point_sweep_at({name: ("x\ny\n" + PLANTED + "\n").encode("utf-8")})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_no_forbidden_identifier_is_in_the_export")

        self.assertIsNotNone(failure, "a file of an unlisted type was not read")
        self.assertIn(name + ":3", failure)

    def test_a_name_in_an_extensionless_file_and_a_dotfile_is_found(self):
        self.point_sweep_at({"LICENSE": ("Copyright " + PLANTED + "\n").encode("utf-8"),
                             ".gitignore": ("*.log\n# " + PLANTED + "\n").encode("utf-8")})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_no_forbidden_identifier_is_in_the_export")

        self.assertIsNotNone(failure)
        self.assertIn("LICENSE:1", failure)
        self.assertIn(".gitignore:2", failure)

    def test_a_name_in_a_utf16_resource_script_is_found(self):
        source = "// " + PLANTED + "\r\n"
        self.point_sweep_at({"native/res.rc": codecs.BOM_UTF16_LE + source.encode("utf-16-le")})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_no_forbidden_identifier_is_in_the_export")

        self.assertIsNotNone(failure)
        self.assertIn("native/res.rc:1", failure)

    def test_an_address_in_a_c_file_is_found(self):
        """The address check reads the same files, with or without the
        denylist. Built from parts: a benchmarking-range address (nobody's
        machine) that is not on the allowlist."""
        address = ".".join(["198", "18", "0", "1"])
        self.assertIsNone(sweep.ALLOWED_IP.match(address), "precondition")
        self.point_sweep_at({"native/plugin.c": ("// " + address + "\n").encode("utf-8")})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_no_real_ip_address_is_in_the_export")

        self.assertIsNotNone(failure)
        self.assertIn("native/plugin.c:1", failure)

    def test_a_clean_tree_passes(self):
        """The control: the failures above are the planted text, nothing
        else about the temp tree."""
        self.point_sweep_at({"native/plugin.c": b"int x;\n", "LICENSE": b"clean\n",
                             "bin/plugin.dll": b"MZ\x00\x00"})

        for method in ("test_no_forbidden_identifier_is_in_the_export",
                       "test_no_real_ip_address_is_in_the_export",
                       "test_every_shipped_file_is_read_or_known_binary"):
            with self.subTest(method):
                self.assertIsNone(failure_of(sweep.NothingIdentifyingShips, method))

    def test_an_undecodable_shipped_file_fails_the_sweep(self):
        self.point_sweep_at({"notes/menu.txt": LATIN_1, "native/plugin.c": b"int x;\n"})

        failure = failure_of(sweep.NothingIdentifyingShips,
                             "test_every_shipped_file_is_read_or_known_binary")

        self.assertIsNotNone(failure, "a file nobody read passed as scanned")
        self.assertIn("notes/menu.txt", failure)
        self.assertNotIn("native/plugin.c", failure)


# Two more invented words, for the sweep of every tracked file: one on a
# planted ALLOWED_OUTSIDE_THE_RELEASE, one not. Denied only inside the tests
# that patch FORBIDDEN, under labels that are planted too.
ALLOWED_PLANTED = "Qorvexallowedplaceholder"
OTHER_PLANTED = "Wumbridgelistedplaceholder"
ALLOWED_LABEL = "planted-label-allowed-outside-the-release"
OTHER_LABEL = "planted-label-allowed-nowhere"
TRACKED_SWEEP = "test_no_forbidden_identifier_is_tracked"


def outcome_of(base, method):
    """A unittest.TestResult for `method` of a copy of TestCase `base`, with
    its setUpClass run - so a skip is seen as one, where failure_of() would
    refuse it."""
    probe = type("Probe", (base,), {})
    probe.setUpClass()
    try:
        result = unittest.TestResult()
        probe(method).run(result)
        return result
    finally:
        probe.tearDownClass()


class TheTrackedSweepOnATempTree(TreeMixin, unittest.TestCase):
    """The real NothingIdentifyingIsTracked methods and tracked_hits(), on a
    temp tree with a planted denylist and a planted allowance: an allowed
    entry passes in an export-ignored file and nowhere else."""

    def setUp(self):
        self.point(sweep, "FORBIDDEN", {sweep.word_hash(ALLOWED_PLANTED): ALLOWED_LABEL,
                                        sweep.word_hash(OTHER_PLANTED): OTHER_LABEL})
        self.point(sweep, "ALLOWED_OUTSIDE_THE_RELEASE",
                   frozenset({sweep.word_hash(ALLOWED_PLANTED)}))
        # Labels or not is decided per test, never by the host this runs on.
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("GITHUB_ACTIONS", None)

    def point_sweep_at(self, files, ignored):
        """`files` ({name: text}) tracked, the names in `ignored` export-ignored."""
        self.point(sweep, "REPO_ROOT", self.make_tree(
            {name: text.encode("utf-8") for name, text in files.items()}))
        self.point(sweep, "tracked_paths", lambda: (list(files), set(ignored), None))

    def failure(self):
        return failure_of(sweep.NothingIdentifyingIsTracked, TRACKED_SWEEP)

    def test_an_allowed_entry_in_an_export_ignored_file_passes(self):
        self.point_sweep_at({"docs/INTERNAL.md": "about " + ALLOWED_PLANTED + "\n",
                             "src/clean.py": "x = 1\n"}, {"docs/INTERNAL.md"})

        self.assertIsNone(self.failure())

    def test_the_same_entry_in_a_file_that_ships_fails(self):
        self.point_sweep_at({"docs/INTERNAL.md": "about " + ALLOWED_PLANTED + "\n",
                             "src/shipped.py": "x = 1\n# " + ALLOWED_PLANTED + "\n"},
                            {"docs/INTERNAL.md"})

        failure = self.failure()

        self.assertIsNotNone(failure, "an allowed entry passed in a file that ships")
        self.assertIn("src/shipped.py:2", failure)
        self.assertNotIn("docs/INTERNAL.md", failure)

    def test_an_entry_not_allowed_fails_in_an_export_ignored_file(self):
        self.point_sweep_at({"docs/INTERNAL.md": "one\ntwo " + OTHER_PLANTED + "\n",
                             "src/clean.py": "x = 1\n"}, {"docs/INTERNAL.md"})

        failure = self.failure()

        self.assertIsNotNone(failure, "an entry not allowed passed in an export-ignored file")
        self.assertIn("docs/INTERNAL.md:2", failure)
        self.assertIn(OTHER_LABEL, failure)

    def test_on_github_actions_the_report_has_no_label(self):
        self.point_sweep_at({"docs/INTERNAL.md": "two " + OTHER_PLANTED + "\n",
                             "src/shipped.py": "# " + ALLOWED_PLANTED + "\n"},
                            {"docs/INTERNAL.md"})
        os.environ["GITHUB_ACTIONS"] = "true"

        failure = self.failure()

        self.assertIsNotNone(failure)
        self.assertIn("docs/INTERNAL.md:1", failure)
        self.assertIn("src/shipped.py:1", failure)
        self.assertIn("a listed identifier", failure)
        self.assertIn("which anyone can read", failure)
        self.assertNotIn(OTHER_LABEL, failure)
        self.assertNotIn(ALLOWED_LABEL, failure)

    def test_control_elsewhere_the_report_names_the_labels(self):
        self.point_sweep_at({"docs/INTERNAL.md": "two " + OTHER_PLANTED + "\n",
                             "src/shipped.py": "# " + ALLOWED_PLANTED + "\n"},
                            {"docs/INTERNAL.md"})

        failure = self.failure()

        self.assertIn(OTHER_LABEL, failure)
        self.assertIn(ALLOWED_LABEL, failure)

    def test_tracked_hits_on_its_own(self):
        texts = {"docs/a.md": ALLOWED_PLANTED + "\n",
                 "src/b.py": "\n" + ALLOWED_PLANTED + "\n",
                 "docs/c.md": OTHER_PLANTED + "\n",
                 "src/d.py": "clean\n"}

        found = sweep.tracked_hits(texts, {"docs/a.md", "docs/c.md"}, sweep.FORBIDDEN)

        self.assertEqual(found, {ALLOWED_LABEL: ["src/b.py:2"], OTHER_LABEL: ["docs/c.md:1"]})

    def test_it_skips_with_the_reason_when_git_cannot_list_the_files(self):
        def refusing_run(*args, **kwargs):
            raise OSError("planted: no git here")
        # The real tracked_paths(), with git unavailable to it.
        self.point(sweep, "subprocess", types.SimpleNamespace(run=refusing_run))

        for method in (TRACKED_SWEEP, "test_every_tracked_file_is_read_or_known_binary"):
            with self.subTest(method):
                result = outcome_of(sweep.NothingIdentifyingIsTracked, method)
                self.assertEqual((result.failures, result.errors), ([], []))
                self.assertEqual(len(result.skipped), 1)
                self.assertIn("planted: no git here", result.skipped[0][1])

    def test_it_skips_with_a_reason_when_git_lists_nothing(self):
        self.point(sweep, "tracked_paths", lambda: ([], set(), None))

        for method in (TRACKED_SWEEP, "test_every_tracked_file_is_read_or_known_binary"):
            with self.subTest(method):
                result = outcome_of(sweep.NothingIdentifyingIsTracked, method)
                self.assertEqual((result.failures, result.errors), ([], []))
                self.assertEqual(len(result.skipped), 1)
                self.assertIn("lists no tracked file", result.skipped[0][1])


class TheAllowanceIsPartOfTheDenylist(unittest.TestCase):
    """ALLOWED_OUTSIDE_THE_RELEASE only lets entries of the real denylist
    through: a hash on it that is no key of FORBIDDEN is a typo, or an entry
    since removed, and allows nothing it was meant to. Reported by the
    allowance's own hashes alone - nothing of the denylist is printed."""

    def test_every_allowed_hash_is_a_denylist_entry(self):
        if sweep.FORBIDDEN is None:
            self.skipTest(f"{sweep.DENYLIST} is absent or defines no FORBIDDEN here, "
                          "so there is no denylist to compare the allowance with")
        missing = sorted(key for key in sweep.ALLOWED_OUTSIDE_THE_RELEASE
                         if key not in sweep.FORBIDDEN)
        self.assertEqual(missing, [], "on ALLOWED_OUTSIDE_THE_RELEASE but not in the denylist")

    def test_every_allowed_hash_has_the_shape_of_one(self):
        width = len(sweep.word_hash("placeholder"))
        odd = sorted(key for key in sweep.ALLOWED_OUTSIDE_THE_RELEASE
                     if not re.fullmatch("[0-9a-f]{%d}" % width, key))
        self.assertTrue(sweep.ALLOWED_OUTSIDE_THE_RELEASE, "control: the allowance is not empty")
        self.assertEqual(odd, [])


class TheConflictSweepOnATempTree(TreeMixin, unittest.TestCase):
    """The real NothingTrackedCarriesOne method, pointed at a temp tree.

    It refuses a file list of 50 or fewer as "came back nearly empty", so
    every tree carries padding. Only the conflict module's REPO_ROOT is
    pointed at the tree: it is the root that sweep runs git in, and the
    root it must read from."""

    PADDING = {f"pad/file{n:02d}.txt": b"clean\n" for n in range(55)}

    def fake_git(self, names):
        """`git ls-files -z` answering `names`, for the conflict sweep only."""
        calls = self.git_calls = []

        def run(args, **kwargs):
            # No assertion in here: the sweep catches every exception from
            # this call and skips, which would hide it. Checked afterwards.
            calls.append((args, kwargs.get("cwd")))
            return types.SimpleNamespace(stdout="\0".join(names).encode("utf-8"))
        self.point(conflicts, "subprocess", types.SimpleNamespace(run=run))

    def sweep_tree(self, extra):
        files = dict(self.PADDING, **extra)
        self.root = self.make_tree(files)
        self.point(conflicts, "REPO_ROOT", self.root)
        self.fake_git(list(files))
        return failure_of(conflicts.NothingTrackedCarriesOne,
                          "test_no_tracked_text_file_has_a_conflict_marker")

    def test_a_marker_in_a_c_file_and_an_extensionless_file_is_caught(self):
        failure = self.sweep_tree({"native/plugin.c": A_CONFLICT.encode("utf-8"),
                                   "tools/runner": ("x\n" + A_CONFLICT).encode("utf-8")})
        self.assertEqual([(args[:2], cwd) for args, cwd in self.git_calls],
                         [(["git", "ls-files"], self.root)])

        self.assertIsNotNone(failure, "markers in a .c and an extensionless file passed")
        self.assertIn("native/plugin.c", failure)
        self.assertIn("tools/runner", failure)

    def test_a_marker_in_a_file_of_a_type_nobody_listed_is_caught(self):
        name = "native/plugin" + INVENTED_SUFFIX
        failure = self.sweep_tree({name: A_CONFLICT.encode("utf-8")})

        self.assertIsNotNone(failure, "a file of an unlisted type was not read")
        self.assertIn(name, failure)

    def test_an_undecodable_tracked_file_fails_the_sweep(self):
        failure = self.sweep_tree({"notes/menu.txt": LATIN_1})

        self.assertIsNotNone(failure, "a file nobody read passed as scanned")
        self.assertIn("notes/menu.txt", failure)

    def test_a_clean_tree_passes(self):
        self.assertIsNone(self.sweep_tree({"native/plugin.c": b"int x;\n",
                                           "bin/plugin.dll": b"MZ\x00\x00"}))

    def test_with_real_git_a_marker_in_a_c_file_is_caught(self):
        """The same against a real repository, for the git half too."""
        files = dict(self.PADDING, **{"native/plugin.c": A_CONFLICT.encode("utf-8")})
        root = self.make_tree(files)
        self.point(conflicts, "REPO_ROOT", root)
        # Without the caller's GIT_* variables, for this git and the sweep's
        # own: run from a git hook, GIT_DIR and GIT_INDEX_FILE would send
        # `git add -A` to the outer repository's index, and the sweep would list
        # that repository instead of this one.
        for name in [k for k in os.environ if k.startswith("GIT_")]:
            self.addCleanup(os.environ.__setitem__, name, os.environ.pop(name))
        try:
            for args in (["git", "init", "-q"], ["git", "add", "-A"]):
                subprocess.run(args, cwd=root, check=True, capture_output=True,
                               timeout=120)
        except Exception as err:  # noqa: BLE001 - no git here
            self.skipTest(f"git is not usable here ({err}); "
                          "the fake-git tests above cover the sweep")

        failure = failure_of(conflicts.NothingTrackedCarriesOne,
                             "test_no_tracked_text_file_has_a_conflict_marker")

        self.assertIsNotNone(failure)
        self.assertIn("native/plugin.c", failure)


if __name__ == "__main__":
    unittest.main()
