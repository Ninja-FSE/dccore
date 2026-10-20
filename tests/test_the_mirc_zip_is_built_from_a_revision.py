"""#1442: dccore-mirc.zip is built from a git revision, not from the disk.

scripts/build_mirc_zip.py used to walk scripts/mirc/ in the working tree, so
the release asset carried whatever happened to be there - a .DS_Store, a log,
a locally rebuilt DLL, an uncommitted dccore.mrc edit - and could differ from
the tree CI tested. Now --rev (default HEAD) is resolved once to a sha and the
members come from `git archive` of scripts/mirc at that sha.

Checked here in throwaway git repositories under the temp folder, laid out
like scripts/mirc/ with invented contents, so every case is deterministic:

  * the member rule and the bytes come from the commit, with its
    .gitattributes line endings (dccore.mrc CRLF, other text LF, binaries
    untouched), whatever the builder's own core.autocrlf says;
  * nothing in the working tree changes the zip: untracked files, a modified
    DLL, an uncommitted or a staged edit, an uncommitted .gitattributes;
  * --rev packs that commit, and the README names that commit's version;
  * the revision is resolved once: a branch moving mid-build does not mix
    two commits;
  * the archive must hold exactly the commit's files (`git ls-tree`), so a
    machine-local export-ignore is a refusal, not a smaller zip, and a name
    outside ASCII matches whatever the file system encoding is;
  * NotACheckout (what the zip tests skip on) only where the folder has no
    .git of its own - git is not even asked - or git cannot start; a .git
    git refuses, a toplevel elsewhere, a timeout or a bad revision is a
    failed build, and a worktree (whose .git is a file) builds;
  * a revision that looks like an option never reaches git;
  * a missing piece, or a symlink or other non-regular entry, is a failed
    build with no zip left behind;
  * main() prints the sha it packed and writes --out.
"""

import contextlib
import functools
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
try:
    import build_mirc_zip  # noqa: E402
finally:
    sys.path.remove(os.path.join(REPO_ROOT, "scripts"))

from tests.support import remove_tree, temp_dir  # noqa: E402

README = build_mirc_zip.README_NAME
SHA = re.compile(r"[0-9a-f]{40}")

# Identity and signing for the commits these tests make; nothing global is read.
GIT_ID = ("-c", "user.name=x", "-c", "user.email=x@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")

ATTRIBUTES = (b"* text=auto eol=lf\n"
              b"*.mrc text eol=crlf\n"
              b"*.dll binary\n"
              b"*.png binary\n")

# Bytes a newline conversion would change: CRLF, a lone LF, a lone CR, a NUL.
DCX_DLL = bytes(range(256)) + b"\r\n\n\r\x00"
UI_DLL = b"MZ\x00\x01\n\r\n" * 8


def script(version):
    """A dccore.mrc as the commit stores it: LF lines."""
    return (b"; placeholder script\n"
            b"alias dccore.ver { return " + version.encode("ascii") + b" }\n"
            b"alias placeholder.other { echo -a hello }\n")


def crlf(blob):
    return blob.replace(b"\n", b"\r\n")


def tree(version="9.8.7"):
    """{path in the repo: bytes} for one commit."""
    return {
        ".gitattributes": ATTRIBUTES,
        "README.md": b"top of the repository, not shipped\n",
        "scripts/mirc-extra/dcx/stray.txt": b"beside scripts/mirc, not in it\n",
        "scripts/mirc/dccore.mrc": script(version),
        "scripts/mirc/build_settings_window.py": b"# builds a window, not shipped\n",
        "scripts/mirc/dcx/dcx.dll": DCX_DLL,
        "scripts/mirc/dcx/LICENSE-DCX.txt": b"licence line one\nlicence line two\n",
        "scripts/mirc/dcx/__pycache__/stale.cpython-310.pyc": b"\x00not shipped",
        "scripts/mirc/dll/dccore_ui.dll": UI_DLL,
        "scripts/mirc/dll/README.md": b"about\nthe dlls\n",
        "scripts/mirc/dll/srcmap.txt": b"named like src, not in src/\n",
        "scripts/mirc/dll/icons-preview.png": b"\x89PNG\r\n\x1a\n",
        "scripts/mirc/dll/src/placeholder.c": b"int placeholder;\n",
    }


# What the zip of tree() holds, each with the bytes a checkout has.
def shipped(version="9.8.7"):
    return {
        "dccore.mrc": crlf(script(version)),
        "dcx/dcx.dll": DCX_DLL,
        "dcx/LICENSE-DCX.txt": b"licence line one\nlicence line two\n",
        "dll/dccore_ui.dll": UI_DLL,
        "dll/README.md": b"about\nthe dlls\n",
        "dll/srcmap.txt": b"named like src, not in src/\n",
    }


def _writable_and_again(func, path, *_):
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    func(path)


def remove_repo(path):
    """Remove a temp repository. git writes its objects read-only, which
    shutil.rmtree cannot delete on Windows - so clear the bit and retry the
    one that failed; if even that fails, the suite's own remover decides
    (a cleanup must not fail a test that passed)."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_writable_and_again)
        else:
            shutil.rmtree(path, onerror=_writable_and_again)
    except Exception:  # noqa: BLE001 - remove_tree is the last word
        remove_tree(path)


@contextlib.contextmanager
def recording():
    """Every argv the builder hands subprocess.run, while still running it."""
    real_run = subprocess.run
    calls = []

    def proxy(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    with mock.patch.object(build_mirc_zip.subprocess, "run", proxy):
        yield calls


def zip_members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class TempRepoCase(unittest.TestCase):
    """A fresh repository under the temp folder, and git kept off everything
    else: no GIT_* variable from the caller (a hook's GIT_DIR would point
    `git init` at the real repository), no global or system config, and no
    fsmonitor daemon left running on the temp folder."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("no git on PATH, and the zip is built from git")
        self.root = tempfile.mkdtemp(prefix="dccore-mirczip-git-")
        self.addCleanup(remove_repo, self.root)
        empty = os.path.join(self.root, "empty-global.gitconfig")
        open(empty, "wb").close()
        self.repo = os.path.join(self.root, "repo")
        os.mkdir(self.repo)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env["GIT_CONFIG_GLOBAL"] = empty
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        # Also for the builder's own git calls, which take no -c.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
        env["GIT_CONFIG_VALUE_0"] = "false"
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            self.git("init", "-q", "--template=")
            self.git("config", "core.autocrlf", "false")
            top = self.git("rev-parse", "--show-toplevel").decode("utf-8").strip()
        except (OSError, subprocess.SubprocessError) as err:
            self.skipTest(f"git cannot make a repository in the temp folder here: {err}")
        try:
            same = os.path.samefile(top, self.repo)
        except OSError as err:
            # An MSYS-style /c/... answer from a git this Python cannot follow.
            self.skipTest(f"git names the temp repository {top!r}, a path Python cannot open: {err}")
        if not same:
            self.skipTest(f"git does not see the temp repository as its own checkout ({top})")
        self.out = os.path.join(self.root, "out", "dccore-mirc.zip")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *GIT_ID, *args], cwd=cwd or self.repo, check=True,
                              capture_output=True, timeout=120).stdout

    def write(self, files):
        for path, data in files.items():
            full = os.path.join(self.repo, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(data)

    def commit(self, files=None, message="placeholder commit"):
        if files is not None:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").decode("ascii").strip()

    def build(self, rev="HEAD", out=None):
        return build_mirc_zip.build(out or self.out, rev, self.repo)

    def assertNoZip(self, out=None):
        out = out or self.out
        self.assertFalse(os.path.exists(out), out)
        self.assertFalse(os.path.exists(out + ".tmp"), out + ".tmp")


class TheMembersComeFromTheCommit(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.sha = self.commit(tree())

    def test_the_rule_and_nothing_else(self):
        """dccore.mrc and dcx/ and dll/, without dll/src/, the icon preview,
        __pycache__, the builder beside them or anything outside scripts/mirc."""
        self.build()
        self.assertEqual(set(zip_members(self.out)), set(shipped()) | {README})

    def test_each_member_is_the_committed_bytes_with_its_line_ending(self):
        """The blob of dccore.mrc is LF; the zip has it CRLF, as eol=crlf
        checks it out everywhere. Other text stays LF; DLLs are untouched."""
        self.build()
        found = zip_members(self.out)
        for name, data in shipped().items():
            with self.subTest(name=name):
                self.assertEqual(found[name], data)
        self.assertNotIn(b"\n", found["dccore.mrc"].replace(b"\r\n", b""))

    def test_the_builders_own_line_ending_config_does_not_change_it(self):
        """The release is built on whatever machine; core.autocrlf=true (the
        Git for Windows default) must not turn the LF text into CRLF."""
        self.build()
        with open(self.out, "rb") as handle:
            plain = handle.read()
        self.git("config", "core.autocrlf", "true")
        self.git("config", "core.eol", "crlf")
        other = os.path.join(self.root, "other", "dccore-mirc.zip")
        self.build(out=other)
        with open(other, "rb") as handle:
            self.assertEqual(handle.read(), plain)

    def test_files_at_reads_scripts_mirc_and_only_it(self):
        listed = self.git("ls-tree", "-r", "--name-only", self.sha, "--", "scripts/mirc")
        expected = {path[len("scripts/mirc/"):] for path in listed.decode("utf-8").splitlines()}
        self.assertIn("dll/src/placeholder.c", expected)
        self.assertEqual(set(build_mirc_zip.files_at(self.sha, self.repo)), expected)

    def test_the_readme_names_the_committed_version(self):
        self.build()
        first = zip_members(self.out)[README].decode("utf-8").splitlines()[0]
        self.assertIn("dccore.mrc 9.8.7", first)


class ADirtyWorkingTreeDoesNotShip(TempRepoCase):

    def test_nothing_on_disk_changes_the_zip(self):
        self.commit(tree())
        self.build()
        with open(self.out, "rb") as handle:
            clean = handle.read()
        mirc = os.path.join(self.repo, "scripts", "mirc")
        dirt = [
            ("an untracked .DS_Store in dcx/", lambda: self.write({
                "scripts/mirc/dcx/.DS_Store": b"\x00\x00\x00\x01Bud1"})),
            ("an untracked log in dll/", lambda: self.write({
                "scripts/mirc/dll/build.log": b"placeholder log line\n"})),
            ("a locally rebuilt DLL", lambda: self.write({
                "scripts/mirc/dll/dccore_ui.dll": b"MZ rebuilt here, never committed"})),
            ("an uncommitted dccore.mrc edit", lambda: self.write({
                "scripts/mirc/dccore.mrc": crlf(script("9.9.9"))})),
            ("a staged, uncommitted licence edit", lambda: (
                self.write({"scripts/mirc/dcx/LICENSE-DCX.txt": b"staged only\n"}),
                self.git("add", "--", "scripts/mirc/dcx/LICENSE-DCX.txt"))),
            ("a tracked DLL deleted from disk", lambda: os.remove(
                os.path.join(mirc, "dcx", "dcx.dll"))),
            # The commit's attributes decide, not the working tree's: with
            # `git archive --worktree-attributes` the first would make every
            # member CRLF and the second drop a file.
            ("an uncommitted .gitattributes making everything CRLF", lambda: self.write({
                ".gitattributes": b"* text eol=crlf\n"})),
            ("and in it an export-ignore under scripts/mirc/dcx/", lambda: self.write({
                ".gitattributes": b"* text eol=crlf\n"
                                  b"scripts/mirc/dcx/LICENSE-DCX.txt export-ignore\n"})),
        ]
        for n, (what, make_dirty) in enumerate(dirt):
            with self.subTest(dirt=what):
                make_dirty()
                out = os.path.join(self.root, f"dirty-{n}", "dccore-mirc.zip")
                self.build(out=out)
                with open(out, "rb") as handle:
                    self.assertEqual(handle.read(), clean)


class TheRevisionIsPacked(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.first = self.commit(tree("9.8.7"))
        self.git("tag", "placeholder-first")
        changed = dict(tree("9.8.8"))
        changed["scripts/mirc/dcx/dcx.dll"] = b"MZ second build"
        self.second = self.commit(changed)

    def test_rev_packs_that_commit_not_the_checked_out_one(self):
        sha, version, found = build_mirc_zip.contents("placeholder-first", self.repo)
        self.assertEqual(sha, self.first)
        self.assertEqual(version, "9.8.7")
        self.assertEqual(found["dccore.mrc"], crlf(script("9.8.7")))
        self.assertEqual(found["dcx/dcx.dll"], DCX_DLL)
        self.build("placeholder-first")
        self.assertIn("dccore.mrc 9.8.7", zip_members(self.out)[README].decode("utf-8"))

    def test_head_is_the_default_and_is_the_newer_commit(self):
        build_mirc_zip.build(self.out, repo=self.repo)
        found = zip_members(self.out)
        self.assertEqual(found["dccore.mrc"], crlf(script("9.8.8")))
        self.assertEqual(found["dcx/dcx.dll"], b"MZ second build")
        self.assertIn("dccore.mrc 9.8.8", found[README].decode("utf-8"))

    def test_a_sha_names_its_commit(self):
        _sha, version, _found = build_mirc_zip.contents(self.first, self.repo)
        self.assertEqual(version, "9.8.7")


class ItIsResolvedOnce(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.first = self.commit(tree("9.8.7"))
        self.second = self.commit(tree("9.8.8"))
        self.git("update-ref", "HEAD", self.first)
        self.calls = []
        real_run = subprocess.run

        def proxy(argv, *args, **kwargs):
            self.calls.append(list(argv))
            result = real_run(argv, *args, **kwargs)
            if self.move_after_resolving and argv[1:3] == ["rev-parse", "--verify"]:
                # The branch moves between resolving and reading.
                real_run(["git", "update-ref", "HEAD", self.second], cwd=self.repo,
                         check=True, capture_output=True, timeout=120)
            return result

        self.move_after_resolving = False
        patcher = mock.patch.object(build_mirc_zip.subprocess, "run", proxy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_contents_returns_the_full_sha_and_its_version(self):
        sha, version, _found = build_mirc_zip.contents("HEAD", self.repo)
        self.assertTrue(SHA.fullmatch(sha), sha)
        self.assertEqual(sha, self.first)
        self.assertEqual(version, "9.8.7")

    def test_git_archive_is_given_the_sha_not_the_rev(self):
        sha, _version, _found = build_mirc_zip.contents("HEAD", self.repo)
        archives = [argv for argv in self.calls if "archive" in argv]
        self.assertEqual(len(archives), 1, self.calls)
        self.assertIn(sha, archives[0])
        self.assertNotIn("HEAD", archives[0])
        verifies = [argv for argv in self.calls if argv[1:3] == ["rev-parse", "--verify"]]
        self.assertEqual(len(verifies), 1, self.calls)

    def test_a_branch_moving_mid_build_does_not_mix_two_commits(self):
        self.move_after_resolving = True
        sha, version, found = build_mirc_zip.contents("HEAD", self.repo)
        self.assertEqual(self.git("rev-parse", "HEAD").decode("ascii").strip(), self.second,
                         "the branch did not move: this test proved nothing")
        self.assertEqual(sha, self.first)
        self.assertEqual(version, "9.8.7")
        self.assertEqual(found["dccore.mrc"], crlf(script("9.8.7")))
        self.assertIn("dccore.mrc 9.8.7", found[README].decode("utf-8"))


class NotACheckoutIsWhatTheZipTestsSkipOn(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.commit(tree())

    def test_it_is_a_system_exit(self):
        """A command-line run still stops with the message."""
        self.assertTrue(issubclass(build_mirc_zip.NotACheckout, SystemExit))

    def test_no_git(self):
        with mock.patch.object(build_mirc_zip.subprocess, "run",
                               side_effect=FileNotFoundError(2, "placeholder: no git")):
            with self.assertRaises(build_mirc_zip.NotACheckout):
                build_mirc_zip.resolve("HEAD", self.repo)
            with self.assertRaises(build_mirc_zip.NotACheckout):
                self.build()
        self.assertNoZip()

    def test_a_copy_with_no_git_of_its_own_inside_another_checkout(self):
        """An extracted tree inside a repository would read that repository's
        scripts/mirc - here a different version - and must not."""
        copy = os.path.join(self.repo, "extracted-copy")
        for path, data in tree("1.0.0-placeholder").items():
            full = os.path.join(copy, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(data)
        # A subfolder of the checkout, like scripts/, has no .git entry either.
        for folder in (copy, os.path.join(self.repo, "scripts")):
            with self.subTest(folder=os.path.relpath(folder, self.repo)):
                with recording() as calls:
                    with self.assertRaises(build_mirc_zip.NotACheckout):
                        build_mirc_zip.resolve("HEAD", folder)
                    with self.assertRaises(build_mirc_zip.NotACheckout):
                        build_mirc_zip.build(self.out, "HEAD", folder)
                # Not asked: inside this checkout git would answer for it.
                self.assertEqual(calls, [])
                self.assertNoZip()

    def test_a_folder_that_is_no_checkout_at_all(self):
        # Wherever the temp folder is, this folder has no .git of its own.
        plain = temp_dir(self, prefix="dccore-mirczip-plain-")
        with recording() as calls:
            with self.assertRaises(build_mirc_zip.NotACheckout):
                build_mirc_zip.resolve("HEAD", plain)
        self.assertEqual(calls, [])

    def test_a_timeout_is_a_failure_not_a_skip(self):
        """git hanging - on the checkout question itself, the revision or the
        archive - is a failed build, not 'no checkout here': a NotACheckout
        would turn the zip tests into skips."""
        real_run = subprocess.run
        for hangs in ("--show-toplevel", "--verify", "archive"):
            with self.subTest(hangs=hangs):
                def slow(argv, *args, **kwargs):
                    if hangs in argv:
                        raise subprocess.TimeoutExpired(argv, build_mirc_zip.GIT_TIMEOUT)
                    return real_run(argv, *args, **kwargs)

                with mock.patch.object(build_mirc_zip.subprocess, "run", slow):
                    with self.assertRaises(SystemExit) as caught:
                        self.build()
                self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
                self.assertIn(f"took over {build_mirc_zip.GIT_TIMEOUT}s", str(caught.exception))
                self.assertNoZip()


class ABadRevisionIsRefused(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.commit(tree())

    def test_an_unknown_revision_says_what_git_said(self):
        with self.assertRaises(SystemExit) as caught:
            self.build("placeholder-no-such-rev")
        self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
        self.assertIn("placeholder-no-such-rev", str(caught.exception))
        self.assertNoZip()

    def test_a_tree_is_not_a_commit(self):
        with self.assertRaises(SystemExit):
            self.build("HEAD^{tree}")
        self.assertNoZip()

    def test_a_revision_that_looks_like_an_option_never_reaches_git(self):
        real_run = subprocess.run
        calls = []

        def proxy(argv, *args, **kwargs):
            calls.append(list(argv))
            return real_run(argv, *args, **kwargs)

        for rev in ("", "-", "--all", "--output=placeholder.txt", "-q"):
            with self.subTest(rev=rev):
                del calls[:]
                with mock.patch.object(build_mirc_zip.subprocess, "run", proxy):
                    with self.assertRaises(SystemExit) as caught:
                        self.build(rev)
                self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
                self.assertEqual(calls, [])
                self.assertNoZip()


class AMissingPieceStopsIt(TempRepoCase):

    def test_each_missing_piece_is_a_failed_build(self):
        whole = tree()
        cases = {
            "no dccore.mrc": [p for p in whole if p == "scripts/mirc/dccore.mrc"],
            "no dcx/": [p for p in whole if p.startswith("scripts/mirc/dcx/")],
            "no dll/": [p for p in whole if p.startswith("scripts/mirc/dll/")],
            "no scripts/mirc": [p for p in whole if p.startswith("scripts/mirc/")],
        }
        for what, dropped in cases.items():
            with self.subTest(missing=what):
                self.assertTrue(dropped)
                for path in whole:
                    full = os.path.join(self.repo, *path.split("/"))
                    if os.path.exists(full):
                        os.remove(full)
                self.write({p: d for p, d in whole.items() if p not in dropped})
                sha = self.commit(message=what)
                with self.assertRaises(SystemExit):
                    self.build(sha)
                self.assertNoZip()

    def test_a_script_without_dccore_ver(self):
        files = tree()
        files["scripts/mirc/dccore.mrc"] = b"; placeholder with no version alias\n"
        self.commit(files)
        with self.assertRaises(SystemExit):
            self.build()
        self.assertNoZip()


class ANonRegularEntryIsRefused(TempRepoCase):

    def test_a_committed_symlink(self):
        """Committed through the index, so it is a real git symlink on every
        system, core.symlinks or not."""
        self.write(tree())
        self.git("add", "-A")
        target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.repo,
                                input=b"dccore_ui.dll", check=True, capture_output=True,
                                timeout=120).stdout.decode("ascii").strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{target},scripts/mirc/dll/link.dll")
        self.git("commit", "-q", "-m", "a symlink")
        listed = self.git("ls-tree", "HEAD", "--", "scripts/mirc/dll/link.dll").decode("utf-8")
        self.assertTrue(listed.startswith("120000 "), listed)
        with self.assertRaises(SystemExit) as caught:
            self.build()
        self.assertIn("dll/link.dll", str(caught.exception))
        self.assertIn("not a regular file", str(caught.exception))
        self.assertNoZip()


SCRIPT_BYTES = b"alias dccore.ver { return 9.8.7 }\r\n"


def tar_of(regular, odd=None):
    """A tar as `git archive` writes one: the regular files {name: bytes},
    and optionally one entry ("name", tarfile type) that is not a file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in regular.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if odd is not None:
            entry = tarfile.TarInfo(odd[0])
            entry.type = odd[1]
            if odd[1] in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = "scripts/mirc/dccore.mrc"
            tar.addfile(entry)
    return buffer.getvalue()


def faked_git(archive, listed):
    """_git answering `archive` and `ls-tree -z --name-only` from the two given."""
    def fake(repo, *args):
        if args[0] == "archive":
            return archive
        if args[0] == "ls-tree":
            return b"".join(name.encode("utf-8") + b"\0" for name in listed)
        raise AssertionError(f"unexpected git {args}")
    return mock.patch.object(build_mirc_zip, "_git", fake)


class ANonRegularTarEntryIsRefusedWithoutGit(unittest.TestCase):
    """The same refusal on a hand-made archive, so it runs where git does not."""

    def test_links_and_devices(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                data = tar_of({"scripts/mirc/dccore.mrc": SCRIPT_BYTES},
                              ("scripts/mirc/dll/odd.dll", kind))
                with faked_git(data, ["scripts/mirc/dccore.mrc", "scripts/mirc/dll/odd.dll"]):
                    with self.assertRaises(SystemExit) as caught:
                        build_mirc_zip.files_at("0" * 40, "placeholder-repo")
                self.assertIn("dll/odd.dll", str(caught.exception))

    def test_a_plain_archive_still_reads(self):
        data = tar_of({"scripts/mirc/dccore.mrc": SCRIPT_BYTES}, ("scripts/mirc/dll", tarfile.DIRTYPE))
        with faked_git(data, ["scripts/mirc/dccore.mrc"]):
            found = build_mirc_zip.files_at("0" * 40, "placeholder-repo")
        self.assertEqual(found, {"dccore.mrc": SCRIPT_BYTES})


class TheArchiveMustHoldTheCommitsFiles(unittest.TestCase):
    """files_at() against `git ls-tree`, on hand-made answers: any difference
    in either direction is a refusal - not just a different count."""

    ARCHIVED = {"scripts/mirc/dccore.mrc": SCRIPT_BYTES, "scripts/mirc/dcx/a.txt": b"a\n"}

    def test_each_kind_of_difference(self):
        cases = {
            "one missing": (["scripts/mirc/dccore.mrc", "scripts/mirc/dcx/a.txt",
                             "scripts/mirc/dcx/dropped.txt"], ["dcx/dropped.txt"]),
            "one extra": (["scripts/mirc/dccore.mrc"], ["dcx/a.txt"]),
            "the same count, another name": (["scripts/mirc/dccore.mrc", "scripts/mirc/dcx/b.txt"],
                                             ["dcx/a.txt", "dcx/b.txt"]),
        }
        for what, (listed, named) in cases.items():
            with self.subTest(what):
                with faked_git(tar_of(self.ARCHIVED), listed):
                    with self.assertRaises(SystemExit) as caught:
                        build_mirc_zip.files_at("0" * 40, "placeholder-repo")
                self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
                for name in named:
                    self.assertIn(name, str(caught.exception))

    def test_the_same_files_pass(self):
        with faked_git(tar_of(self.ARCHIVED), list(self.ARCHIVED)):
            found = build_mirc_zip.files_at("0" * 40, "placeholder-repo")
        self.assertEqual(set(found), {"dccore.mrc", "dcx/a.txt"})


class TheFolderOnDiskIsTheFolderInGit(unittest.TestCase):

    def test_source_is_source_in_git_under_the_repo(self):
        self.assertEqual(build_mirc_zip.SOURCE,
                         os.path.join(build_mirc_zip.REPO_ROOT, *build_mirc_zip.SOURCE_IN_GIT.split("/")))
        self.assertTrue(os.path.isfile(os.path.join(build_mirc_zip.SOURCE, build_mirc_zip.SCRIPT)))


class ALocalAttributeCannotDropAFile(TempRepoCase):
    """.git/info/attributes is this machine's, not the commit's, and
    `git archive` obeys it: an export-ignore there would ship a smaller zip."""

    def test_a_local_export_ignore_is_refused(self):
        self.commit(tree())
        info = os.path.join(self.repo, ".git", "info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "attributes"), "wb") as handle:
            handle.write(b"scripts/mirc/dcx/LICENSE-DCX.txt export-ignore\n")
        with self.assertRaises(SystemExit) as caught:
            self.build()
        self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
        self.assertIn("dcx/LICENSE-DCX.txt", str(caught.exception))
        self.assertNoZip()

    OUTSIDE_ASCII = "dcx/LICENSE-éß.txt"

    def commit_a_name_outside_ascii(self):
        self.write(tree())
        self.git("add", "-A")
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.repo,
                              input=b"placeholder licence\n", check=True, capture_output=True,
                              timeout=120).stdout.decode("ascii").strip()
        # Through the index, so no file system has to hold the name.
        name = "scripts/mirc/" + self.OUTSIDE_ASCII
        self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        self.git("commit", "-q", "-m", "a name outside ascii")

    def test_a_name_outside_ascii_still_matches(self):
        """ls-tree quotes such a name unless -z; the archive does not."""
        self.commit_a_name_outside_ascii()
        self.build()
        self.assertEqual(zip_members(self.out)[self.OUTSIDE_ASCII], b"placeholder licence\n")

    def test_a_name_outside_ascii_whatever_the_file_system_encoding(self):
        """tarfile decodes names with the file system encoding unless told
        otherwise, and ls-tree's are read as UTF-8: where that encoding is
        ASCII (a C locale on Linux), the two names must still be one."""
        self.commit_a_name_outside_ascii()
        # What tarfile takes from sys.getfilesystemencoding() at import.
        with mock.patch.object(tarfile.TarFile, "encoding", "ascii"):
            self.build()
        self.assertEqual(zip_members(self.out)[self.OUTSIDE_ASCII], b"placeholder licence\n")

    def test_a_name_outside_ascii_in_a_python_whose_file_system_encoding_is_ascii(self):
        """The same in a real Python started under the C locale, with neither
        UTF-8 mode nor locale coercion to rescue it."""
        if sys.platform in ("win32", "darwin"):
            self.skipTest("Python's file system encoding is always UTF-8 on Windows and macOS")
        self.commit_a_name_outside_ascii()
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
        env.pop("PYTHONIOENCODING", None)
        child = ("import sys, tarfile\n"
                 "sys.path.insert(0, sys.argv[1])\n"
                 "import build_mirc_zip\n"
                 "print(sys.getfilesystemencoding(), tarfile.TarFile.encoding)\n"
                 "build_mirc_zip.build(sys.argv[2], 'HEAD', sys.argv[3])\n")
        ran = subprocess.run([sys.executable, "-X", "utf8=0", "-c", child,
                              os.path.join(REPO_ROOT, "scripts"), self.out, self.repo],
                             env=env, capture_output=True, timeout=120)
        said = ran.stdout.decode("ascii", "replace").split()
        if ran.returncode == 0 and said[:2] != ["ascii", "ascii"]:
            self.skipTest(f"Python here would not start with an ASCII file system encoding ({said})")
        self.assertEqual(ran.returncode, 0, ran.stderr.decode("utf-8", "replace"))
        self.assertEqual(said[:2], ["ascii", "ascii"])
        self.assertEqual(zip_members(self.out)[self.OUTSIDE_ASCII], b"placeholder licence\n")


class TheCheckoutBoundary(TempRepoCase):
    """NotACheckout only where there is no .git; past that, git failing or
    disagreeing is a failed build - the zip tests fail instead of skipping."""

    def setUp(self):
        super().setUp()
        self.commit(tree())

    def copy_of_the_tree(self, folder):
        for path, data in tree().items():
            full = os.path.join(folder, *path.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(data)

    def test_a_worktree_whose_git_is_a_file_builds(self):
        worktree = os.path.join(self.root, "worktree")
        self.git("worktree", "add", "-q", "--detach", worktree, "HEAD")
        self.assertTrue(os.path.isfile(os.path.join(worktree, ".git")))
        self.build()
        with open(self.out, "rb") as handle:
            main = handle.read()
        other = os.path.join(self.root, "out-worktree", "dccore-mirc.zip")
        build_mirc_zip.build(other, "HEAD", worktree)
        with open(other, "rb") as handle:
            self.assertEqual(handle.read(), main)

    def test_a_git_file_pointing_nowhere_is_a_failed_build(self):
        broken = os.path.join(self.root, "broken")
        self.copy_of_the_tree(broken)
        with open(os.path.join(broken, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: " + os.path.join(self.root, "placeholder-missing-gitdir") + "\n")
        with self.assertRaises(SystemExit) as caught:
            build_mirc_zip.build(self.out, "HEAD", broken)
        self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
        self.assertIn("--show-toplevel", str(caught.exception))
        self.assertNoZip()

    def test_a_git_that_is_not_its_own_is_a_failed_build(self):
        """An empty .git folder is no repository to git, which walks up and
        answers for the checkout around it."""
        inner = os.path.join(self.repo, "inner")
        self.copy_of_the_tree(inner)
        os.mkdir(os.path.join(inner, ".git"))
        with self.assertRaises(SystemExit) as caught:
            build_mirc_zip.build(self.out, "HEAD", inner)
        self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
        self.assertIn("top at", str(caught.exception))
        self.assertNoZip()

    def test_a_toplevel_elsewhere_is_named(self):
        elsewhere = os.path.join(self.root, "placeholder-elsewhere")
        os.mkdir(elsewhere)
        real_run = subprocess.run

        def answer(argv, *args, **kwargs):
            if "--show-toplevel" in argv:
                return subprocess.CompletedProcess(argv, 0, elsewhere.encode("utf-8") + b"\n", b"")
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(build_mirc_zip.subprocess, "run", answer):
            with self.assertRaises(SystemExit) as caught:
                self.build()
        self.assertNotIsInstance(caught.exception, build_mirc_zip.NotACheckout)
        self.assertIn(elsewhere, str(caught.exception))
        self.assertNoZip()


class TheCommandLine(TempRepoCase):

    def setUp(self):
        super().setUp()
        self.first = self.commit(tree("9.8.7"))
        self.commit(tree("9.8.8"))
        # main() packs REPO_ROOT; point it at the temp repository.
        patcher = mock.patch.object(build_mirc_zip, "contents",
                                    functools.partial(build_mirc_zip.contents, repo=self.repo))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_writes_out_and_prints_the_sha_it_packed(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = build_mirc_zip.main(["--rev", self.first[:12], "--out", self.out])
        self.assertEqual(code, 0)
        with open(self.out, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        said = printed.getvalue()
        self.assertIn(f"at {self.first}", said)
        self.assertIn(f"sha256 {digest}", said)
        self.assertIn("dccore.mrc 9.8.7", said)
        self.assertIn("dccore.mrc 9.8.7", zip_members(self.out)[README].decode("utf-8"))

    def test_a_bad_rev_writes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_mirc_zip.main(["--rev", "placeholder-no-such-rev", "--out", self.out])
        self.assertNoZip()


class TheRealRepositoryAtHead(unittest.TestCase):
    """main() as the release runs it, on this checkout."""

    def test_main_prints_heads_sha(self):
        out = os.path.join(temp_dir(self, prefix="dccore-mirczip-main-"), "dccore-mirc.zip")
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                build_mirc_zip.main(["--out", out])
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
                                  capture_output=True, timeout=120).stdout.decode("ascii").strip()
        except build_mirc_zip.NotACheckout as err:
            self.skipTest(f"the zip is built from git, and git cannot read this tree: {err}")
        self.assertIn(f"at {head}", printed.getvalue())
        self.assertTrue(os.path.isfile(out))


if __name__ == "__main__":
    unittest.main()
