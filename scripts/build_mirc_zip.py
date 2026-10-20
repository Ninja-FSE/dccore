#!/usr/bin/env python3
"""Build dccore-mirc.zip: the mIRC script and the folders it loads (#1412).

Installing the script used to mean finding scripts/mirc/ in the repository and
copying dccore.mrc with its dcx and dll folders - and not the build files that
sit beside them. This packs exactly what goes into mIRC, and is attached to
each GitHub release under one fixed name, so

    https://github.com/Ninja-FSE/dccore/releases/latest/download/dccore-mirc.zip

is always the newest script (docs/PUBLIC-REPO-WORKFLOW.md, the release steps).

WHAT GOES IN is a rule, not a list: dccore.mrc, and everything under dcx/ and
dll/ except the dll sources and the icon preview (SKIPPED). A file added to one
of those folders ships without anyone remembering to add it here.

NO TOP FOLDER. The script finds dcx\\ and dll\\ beside itself ($scriptdir), and
keeps its settings and token in dccore.ini beside itself too. Extracted where
dccore.mrc already is, an update overwrites the script and its folders and
leaves dccore.ini alone; a first install extracts into the mIRC folder.

THE SAME BYTES EVERY TIME: members in sorted order, one fixed timestamp and
mode, so a rebuild of the same tree gives the same file and the same SHA-256.

FROM A REVISION, NOT FROM THE DISK (#1442). The members and their bytes come
from git at one commit (--rev, default HEAD, resolved once to a sha), so the
zip is the committed tree CI tested: a .DS_Store, a log, a locally rebuilt DLL
or an uncommitted dccore.mrc edit in the working tree does not ship. The bytes
are what `git archive` gives for that commit, which applies the commit's own
.gitattributes line endings - dccore.mrc CRLF (eol=crlf), as a checkout on any
system has it - so the zip is byte for byte what a clean checkout built.

That last holds only without machine-local attribute rules: `git archive` also
reads .git/info/attributes, core.attributesFile and the system attributes. A
local export-ignore would silently drop a file, so the archive's file names are
checked against the commit's own list (`git ls-tree`) and a difference refuses
the build. A local export-subst, filter or eol rule that rewrites bytes is not
caught here and stays a latent risk: build releases from a checkout without them.

Usage:
    python3 scripts/build_mirc_zip.py [--rev HEAD] [--out dccore-mirc.zip]
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The folder that is packed, as a path inside a commit, and on disk.
SOURCE_IN_GIT = "scripts/mirc"
SOURCE = os.path.join(REPO_ROOT, *SOURCE_IN_GIT.split("/"))
DEFAULT_REV = "HEAD"
GIT_TIMEOUT = 120
ZIP_NAME = "dccore-mirc.zip"
README_NAME = "dccore-mirc-README.txt"

# What is installed: the script, and the two folders it loads from.
SCRIPT = "dccore.mrc"
FOLDERS = ("dcx", "dll")
# Inside those folders, what is for building them, not for running them.
SKIPPED = ("dll/src/", "dll/icons-preview.png")

# One timestamp for every member (the earliest a zip can hold), so the bytes
# do not depend on when the files were checked out.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644


class NotACheckout(SystemExit):
    """No checkout to build from here: `repo` has no .git of its own, or git
    cannot be started at all. Anything else that goes wrong - a .git that git
    refuses, a hang, a bad revision - is a plain SystemExit, a failed build."""


def version_in(script, where=SCRIPT):
    """dccore.ver in the bytes of a dccore.mrc: what the README says it is."""
    # Line by line as a text-mode read splits them (CRLF, LF or CR).
    for line in io.StringIO(script.decode("latin-1"), newline=None):
        found = re.match(r"\s*alias\s+dccore\.ver\s*\{\s*return\s+(\S+)\s*\}", line)
        if found:
            return found.group(1)
    raise SystemExit(f"build_mirc_zip: no dccore.ver in {where}")


def script_version(source=SOURCE):
    """dccore.ver of the script on disk in `source` (the working tree)."""
    path = os.path.join(source, SCRIPT)
    with open(path, "rb") as handle:
        return version_in(handle.read(), path)


def _git(repo, *args):
    """stdout of one git command in `repo`, as bytes; SystemExit when it fails."""
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=GIT_TIMEOUT)
    except OSError as err:
        raise NotACheckout(f"build_mirc_zip: git could not run: {err}")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"build_mirc_zip: git {args[0]} took over {GIT_TIMEOUT}s")
    if result.returncode != 0:
        said = result.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"build_mirc_zip: git {' '.join(args)} failed: {said}")
    return result.stdout


def resolve(rev=DEFAULT_REV, repo=REPO_ROOT):
    """The commit sha `rev` names in `repo` - resolved once, so every read is one tree."""
    if not rev or rev.startswith("-"):
        raise SystemExit(f"build_mirc_zip: {rev!r} is not a revision")
    # A tree with no .git of its own (an extracted copy) is no checkout, and
    # git is not asked: inside another checkout it would answer for that one,
    # and read that repository's scripts/mirc.
    if not os.path.lexists(os.path.join(repo, ".git")):
        raise NotACheckout(f"build_mirc_zip: {repo} has no .git, so it is not a git checkout")
    # From here a .git is there, so git failing on it (dubious ownership, a
    # broken worktree link, a corrupt repository) is a failed build, not a skip.
    top = _git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    if not (os.path.isdir(top) and os.path.samefile(top, repo)):
        raise SystemExit(f"build_mirc_zip: git says the checkout at {repo} has its top at {top}")
    return _git(repo, "rev-parse", "--verify", rev + "^{commit}").decode("utf-8").strip()


def files_at(sha, repo=REPO_ROOT):
    """{path inside scripts/mirc: bytes} at commit `sha`, as `git archive`
    writes them - so with that commit's .gitattributes line endings applied.
    Refuses when the archive does not hold exactly the commit's files (a
    machine-local export-ignore drops one silently otherwise)."""
    data = _git(repo, "archive", "--format=tar", sha, "--", SOURCE_IN_GIT)
    prefix = SOURCE_IN_GIT + "/"
    found = {}
    # UTF-8, as the ls-tree names below are read: tarfile would otherwise
    # decode the names git wrote with the file system encoding, and on a
    # machine where that is not UTF-8 a name outside ASCII would not match.
    with tarfile.open(fileobj=io.BytesIO(data), encoding="utf-8") as archive:
        for entry in archive.getmembers():
            if entry.isdir() or not entry.name.startswith(prefix):
                continue
            if not entry.isfile():
                raise SystemExit(f"build_mirc_zip: {entry.name} at {sha} is not a regular file")
            found[entry.name[len(prefix):]] = archive.extractfile(entry).read()
    listed = _git(repo, "ls-tree", "-r", "-z", "--name-only", sha, "--", SOURCE_IN_GIT)
    committed = {name[len(prefix):] for name in listed.decode("utf-8").split("\0")
                 if name.startswith(prefix)}
    if committed != set(found):
        dropped = sorted(committed - set(found))
        added = sorted(set(found) - committed)
        raise SystemExit(f"build_mirc_zip: git archive of {sha} is not the commit's {SOURCE_IN_GIT} "
                         f"(local attributes, e.g. export-ignore?): missing {dropped}, extra {added}")
    return found


def members(files):
    """{name in the zip: bytes}, by the rule above, from files_at()'s map."""
    if SCRIPT not in files:
        raise SystemExit(f"build_mirc_zip: {SOURCE_IN_GIT}/{SCRIPT} is missing")
    found = {SCRIPT: files[SCRIPT]}
    for folder in FOLDERS:
        inside_folder = [name for name in files if name.startswith(folder + "/")]
        if not inside_folder:
            raise SystemExit(f"build_mirc_zip: {SOURCE_IN_GIT}/{folder} is missing")
        for inside in inside_folder:
            if "__pycache__" in inside.split("/"):
                continue
            if inside.startswith(SKIPPED) or inside in SKIPPED:
                continue
            found[inside] = files[inside]
    return found


def contents(rev=DEFAULT_REV, repo=REPO_ROOT):
    """(sha, dccore.ver, {name in the zip: bytes}) for `rev`, all from that one commit."""
    sha = resolve(rev, repo)
    found = members(files_at(sha, repo))
    version = version_in(found[SCRIPT], f"{SCRIPT} at {sha}")
    found[README_NAME] = readme(version)
    return sha, version, found


def readme(version):
    """The few lines a person needs, CRLF so Notepad shows them as lines."""
    lines = [
        f"DCCore - the mIRC window script, dccore.mrc {version}",
        "",
        "FIRST TIME",
        "  1. Extract everything here into your mIRC folder (in mIRC: //echo -a $mircdir).",
        "     dccore.mrc goes in it, with the dcx and dll folders beside it.",
        "  2. In mIRC:  /load -rs dccore.mrc",
        "  3. Then:     /dccore pair <your bot's nick>",
        "     and type the bot's admin password in the window, once.",
        "",
        "UPDATING",
        "  Extract everything over the old files, where dccore.mrc already is, then in mIRC:",
        "     /reload -rs dccore.mrc",
        "  Your settings and the bot's token are in dccore.ini beside the script;",
        "  this zip has no dccore.ini, so they are kept.",
        "",
        "WHAT IS IN HERE",
        "  dccore.mrc   the script",
        "  dcx\\         DCX, which draws the control panel (licence inside)",
        "  dll\\         the dark theme and the menu icons",
        "  Without the two folders the script still works, with plain windows.",
        "",
        "Everything else: https://ninja-fse.github.io/dccore/mirc.html",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _info(name):
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = FILE_MODE << 16
    info.create_system = 0
    return info


def _write(out, found):
    """Write {name: bytes} to `out` as the zip; returns its sha256 hex."""
    folder = os.path.dirname(os.path.abspath(out))
    os.makedirs(folder, exist_ok=True)
    temp = out + ".tmp"
    with zipfile.ZipFile(temp, "w") as archive:
        for name in sorted(found):
            archive.writestr(_info(name), found[name])
    os.replace(temp, out)
    with open(out, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def build(out, rev=DEFAULT_REV, repo=REPO_ROOT):
    """Write the zip of `rev` to `out`; returns (sha256 hex, number of members)."""
    _sha, _version, found = contents(rev, repo)
    return _write(out, found), len(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build dccore-mirc.zip, the mIRC script to install.")
    parser.add_argument("--rev", default=DEFAULT_REV,
                        help="the commit to pack, e.g. the one the release extracts (default HEAD)")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "dist", ZIP_NAME), help="where to write the zip")
    args = parser.parse_args(argv)
    sha, version, found = contents(args.rev)
    digest = _write(args.out, found)
    print(f"build_mirc_zip: {args.out} - dccore.mrc {version} at {sha}, "
          f"{len(found)} files, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
