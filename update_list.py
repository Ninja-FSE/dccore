# update_list.py - OmenServe-style layout, generating both lists (part 1 of 2)
import os
import io
import re
import sys
import shutil
import datetime
import subprocess
import tempfile
import zipfile
import time
import json
import defaults as config
import library
import platform_compat

# BEFORE ANYTHING PRINTS A FILENAME. This runs as its own process - the daemon
# starts it with subprocess.run() and configure.py runs it directly - so
# oserve.py's guard does nothing for it, and every line it writes is a path off
# somebody's disk.
#
# On a console whose code page cannot represent a character in one of those
# paths, print() raises UnicodeEncodeError and the scan dies where it stood.
# Reported from a live Greek-Windows install: "External update_list.py failed
# (Exit Code 1): Unknown script error", with the parent's own reader thread
# then dying on the bytes that had made it out - so the operator got a failure
# with no cause, for a library containing an accented filename.
platform_compat.install_console_encoding_guard()

# Multi-disc/box-set container names the !rar album list truncates at - see
# generate_master_list()'s own comment on the box-word block for why this
# has to match a whole PATH SEGMENT, not a substring anywhere in the path.
# An optional trailing number covers "CD1", "Disc 2", "Volume III" (digits
# only - "III" survives as part of the folder name, same as before this fix).
_BOX_WORD_RE = re.compile(
    r'^(cd|disc|disk|volume|digital media|media)\s*\d*$', re.IGNORECASE)

def format_size_human(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PB"

def format_total_size(bytes_size):
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PiB"

def _extension_set(setting_name):
    """One setting's extensions, normalised: lower-case and dot-leading.

    Normalised HERE rather than trusted from the setting, because these are
    reached from three directions - settings.conf, admin_config.py and the
    dashboard's Settings page - and only one of them goes anywhere near a
    validator. An operator writing "DB, .ini, tmp" means the obvious thing,
    and so does "MKV,.mp4 , avi".

    The leading dot is not cosmetic. `"Thumbs.db".endswith("db")` is already
    true, so it is not what makes a file match; it is what stops an extension
    matching the END OF A NAME. Without it, ignoring "ts" also hides every
    file called `credits` or `highlights`.

    An empty result is a real answer for every one of these sets, so there is
    no fallback anywhere: skip nothing, no video, nothing packable.
    """
    raw = getattr(config, setting_name, None)
    if isinstance(raw, str):
        raw = raw.split(",")
    cleaned = []
    for item in (raw or []):
        text = str(item).strip().lower()
        if not text:
            continue
        cleaned.append(text if text.startswith(".") else "." + text)
    return tuple(dict.fromkeys(cleaned))


def ignored_extensions():
    """Extensions left out of every list."""
    return _extension_set("LIST_IGNORED_EXTENSIONS")


def video_extensions():
    """Extensions routed to the film and series list rather than the music one."""
    return _extension_set("LIST_VIDEO_EXTENSIONS")


def rar_extensions():
    """Extensions that make a folder packable with !rar."""
    return _extension_set("RAR_EXTENSIONS")


def pack_size_over(path, cap):
    """Is packing `path` going to exceed `cap` bytes? Returns (over, measured).

    RECURSIVE, because the pack is: `rar a <dir>` takes the directory and
    everything under it, so a check that only looked at the top level would
    measure something other than what gets packed.

    STOPS AS SOON AS THE CAP IS PASSED. The answer wanted here is a yes or no,
    not a total, and the folder this is most useful on is the one that is
    enormous - so walking all of it to produce a number nobody reads is the
    one cost worth avoiding. A folder a hundred times over the cap is refused
    after a few thousand entries instead of after all of them.

    `cap` of 0 or less means no limit, and returns (False, 0) without touching
    the disk at all: an operator who has not set one pays nothing for this.

    An unreadable entry is skipped rather than counted or raised on. This runs
    on the REQUEST path, where the alternative to an answer is a user who gets
    no reply - and the pack that follows would meet the same unreadable file
    and report it properly.
    """
    if not cap or cap <= 0:
        return False, 0
    measured = 0
    for root, _dirs, files in os.walk(platform_compat.long_path(path)):
        for name in files:
            try:
                measured += os.path.getsize(
                    platform_compat.long_path(os.path.join(root, name)))
            except OSError:
                continue
            if measured > cap:
                return True, measured
    return False, measured


def walk_with_sizes(top, onerror=None):
    """Every file under `top`, with the size the directory entry already knew.

    WHY THIS EXISTS. os.walk is built on os.scandir, which gets each entry's
    size from the directory enumeration itself - and then throws it away,
    because os.walk's contract is names only. The caller then asks
    os.path.getsize() for a number the filesystem has just finished telling
    us. One redundant syscall per file, and on a network share one redundant
    ROUND TRIP per file.

    Measured on 20,000 files, local SSD, warm cache, both producing the same
    answer:

        os.walk + getsize    0.356s   (17.8 us/file)
        os.scandir + cached  0.095s   ( 4.7 us/file)   3.8x

    That is the WALK. A whole rebuild, timed the same way over 30,000 files
    and producing byte-identical lists, goes 2.51s -> 1.56s: 1.61x, because
    writing and packing are the rest of the job and this does not touch them.
    The walk's share is what grows on a network drive, where the second ask is
    a round trip rather than a cached answer.

    Local disk is the BEST case for the old shape. The library this was
    written for is 799,438 files on a mapped network drive, where a rebuild
    takes fifteen and a half minutes.

    Yields (dirpath, [(name, size), ...]), one tuple per directory, so the
    caller's loop keeps its shape.

    A file whose size cannot be read yields a size of None rather than being
    dropped here. That decision belongs to the caller, which already logs the
    path and excludes it - see the [LIST-GEN ERROR] branch - and a helper that
    silently skipped it would take that log line away.

    `entry.stat()` FOLLOWS symlinks, exactly as os.path.getsize() did, so a
    symlinked track still reports its target's size. On Windows that costs a
    syscall only when the entry really is a symlink; an ordinary file is
    answered from what the enumeration already returned.

    A symlinked DIRECTORY is classified as a directory - `entry.is_dir()`,
    following, exactly as os.walk does when deciding what goes in `dirs` - and
    then not descended into, which is os.walk's followlinks=False default. Two
    decisions, made separately, because collapsing them into
    is_dir(follow_symlinks=False) answers False for a symlinked directory and
    hands it back as a file.

    `onerror` is called with the OSError, matching os.walk's parameter of the
    same name, so an unreadable subtree is reported the way it always was
    rather than ending the scan.
    """
    pending = [top]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scanning:
                entries = list(scanning)
        except OSError as err:
            if onerror is not None:
                onerror(err)
            continue

        files = []
        for entry in entries:
            try:
                # CLASSIFYING AND DESCENDING ARE TWO DECISIONS, and os.walk
                # makes them separately. A symlink to a directory IS a
                # directory - os.walk puts it in `dirs`, so it never reaches a
                # caller as a file - and with followlinks=False it simply is
                # not descended into.
                #
                # Doing both with is_dir(follow_symlinks=False) collapses them
                # and gets the first one wrong: that answers False for a
                # symlinked directory, which made this walk treat it as a FILE
                # and stat it, and would have published a directory as a
                # downloadable entry in the list.
                #
                # Caught by CI on Linux, where a symlink can be created without
                # elevation, while the same test skipped on the Windows box
                # that wrote it.
                if entry.is_dir():
                    if not entry.is_symlink():
                        pending.append(entry.path)
                    continue
            except OSError as err:
                # A directory entry that cannot even be classified. Report it
                # like an unreadable subtree - it is one - rather than
                # guessing it is a file and failing again on the stat.
                if onerror is not None:
                    onerror(err)
                continue
            try:
                files.append((entry.name, entry.stat().st_size))
            except OSError:
                files.append((entry.name, None))
        yield current, files


def _has_extension(name, extensions):
    return bool(extensions) and str(name).lower().endswith(tuple(extensions))


def is_video_file(name, video=None):
    """Does this file belong in the video list rather than the music one?"""
    return _has_extension(name, video_extensions() if video is None else video)


def is_packable_file(name, packable=None):
    """Does this file make its folder requestable with !rar?

    Its own set, deliberately not "anything the scan indexed" - see
    RAR_EXTENSIONS in defaults.py for what that cost.
    """
    return _has_extension(name, rar_extensions() if packable is None else packable)


def has_backslash_component(relative_path, separator=None):
    """Does any component of `relative_path` carry a literal backslash?

    The list writes folder headings with backslashes between the components
    and list.list_heading_parts() reads them back by splitting on both
    separators, so a backslash INSIDE a name is indistinguishable from the
    separator between two - and the folder a heading resolves to is then a
    different one, or none (#464).

    THE ANSWER DEPENDS ON WHAT THE SEPARATOR IS, which is the whole of the
    platform question here. On Linux the separator is "/" and a backslash is
    an ordinary filename character, so "Rock\\Metal" is ONE folder with an
    awkward name. On Windows the backslash IS the separator, so the same text
    is two folders and no name can contain one - this returns False for every
    path a Windows install can produce, correctly.

    `separator` is for the tests, the way irc.resolve_dcc_address()'s
    `lookup` is: the hazard exists on one platform, and a test that could only
    run there would be a hole on the other. Production passes nothing and gets
    os.sep, which is what generate_master_list() builds rel_dir with.
    """
    sep = os.sep if separator is None else separator
    flattened = str(relative_path).replace(sep, "/")
    return any("\\" in part for part in flattened.split("/"))


def is_listed_file(name, ignored=None):
    """Does this file go into the list? Everything does, unless it is skipped.

    `ignored` is the hot-path argument: the scan resolves the setting ONCE and
    passes the result down, because this is asked of every file in the library
    and a 719k-file library would otherwise rebuild the tuple 719,000 times.
    Callers with one file to check can leave it out.

    A file with no extension is listed. So is a dotfile, and so is anything
    else the operator has not named - "every file" is the rule, and the
    setting is the only exception to it.
    """
    if ignored is None:
        ignored = ignored_extensions()
    if not ignored:
        return True
    return not str(name).lower().endswith(ignored)


def _one_line(text):
    """Flatten anything that would break the one-entry-per-line format.

    POSIX filenames may contain newlines and other control characters - only "/"
    and NUL are forbidden - so a track called "evil\nname.flac" is perfectly
    legal on the Linux box this daemon runs on. Written straight into the list it
    splits one request entry into two lines, leaving a truncated entry and an
    orphan fragment, and the file every user downloads is malformed from there
    down. Windows refuses such names at creation, which is why CI caught this on
    the ubuntu jobs only.

    Nobody can plant one remotely - the library is the operator's own mount - so
    this is robustness rather than a security boundary. Replacing with a space
    keeps the entry visible and the file structurally sound; such a track is
    already unrequestable, because the request parser splits on whitespace too.

    Also sanitises non-UTF-8 bytes. os.walk() on POSIX decodes filenames with
    the "surrogateescape" error handler by default, so a name with bytes that
    are not valid UTF-8 (a CP1252 rip, a bad extraction, a FAT copy) comes back
    as a lone surrogate codepoint - not itself a control character, but not
    valid UTF-8 either, and this text is about to be written with a strict
    UTF-8 encoder. Sanitised here rather than left to fail at the write: one
    bad name in a library of thousands now costs a mangled-but-valid name in
    the list, not the entire rebuild.
    """
    text = str(text).encode("utf-8", "replace").decode("utf-8")
    return "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)


def _discard_temp_lists(*paths):
    """Remove half-written temporary lists so they cannot be mistaken for real ones."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as err:
            print(f"[LIST-CLEAN ERROR] Could not remove {path}: {err}")


def _publish_artifacts(swaps):
    """Move every (temporary, destination) pair into place, or none of them.

    THE PUBLISH USED TO BE FIVE INDEPENDENT REPLACEMENTS, so a failure partway
    through left the bot advertising one scan and handing out another. The
    ordinary way to reach it, on Windows: os.replace onto a file another
    handle has open raises PermissionError, and dcc.py holds the published
    artifact open for the whole duration of a DCC send. PAUSE_ON_UPDATE only
    refuses NEW requests, so a transfer already in flight keeps that handle -
    and somebody downloading the list when the scheduled rebuild lands is not
    an edge case on a bot with several slots.

    What that produced: the master index replaced, so @find, the advert count
    and commands.count_from_master_list() all reported the new scan - while
    the archive users actually received was the previous one, the size side
    files still carried the previous numbers, the base-name marker was not
    updated, and the prune never ran. Nothing recovered it; the artifact
    stayed stale until some later rebuild happened to run with no transfer in
    progress. And the failure branch then printed "The previous list was left
    untouched and is still in use", which by then was false.

    Each destination is moved ASIDE before its replacement lands, so a failure
    can put back exactly what was there. That also makes the locked case fail
    at the safest possible moment: renaming a file another process holds open
    fails on Windows too, so the lock is discovered while moving the old file
    out of the way - before anything observable has changed.

    Raises whatever the underlying replace raised, after rolling back. The
    caller's message about the previous list still being in use is then true
    again, which is the point.
    """
    done = []
    try:
        for temporary, destination in swaps:
            backup = None
            if os.path.exists(platform_compat.long_path(destination)):
                backup = destination + ".previous"
                platform_compat.replace_with_retry(destination, backup)
            platform_compat.replace_with_retry(temporary, destination)
            done.append((destination, backup))
    except Exception:
        # Reverse order because that is the convention for undoing a
        # sequence, not because it is required here: each swap touches only
        # its own destination and that destination's .previous, so no two of
        # them can collide. A mutation run flipped the order and nothing
        # failed, which is the honest reading - the comment that used to sit
        # here claimed a necessity there is not one of.
        for destination, backup in reversed(done):
            try:
                if backup:
                    platform_compat.replace_with_retry(backup, destination)
                else:
                    # There was nothing here before; leaving the new file
                    # would publish half a rebuild.
                    os.remove(platform_compat.long_path(destination))
            except OSError as undo_err:
                print(f"[LIST-GEN ERROR] Could not roll {destination} back: "
                      f"{undo_err}")
        raise

    for _destination, backup in done:
        if not backup:
            continue
        try:
            os.remove(platform_compat.long_path(backup))
        except OSError:
            # A leftover .previous is clutter, not a failure - the publish
            # itself succeeded and that is what the caller is waiting on.
            pass
    return True


def _prune_superseded_lists(keep, directory=None):
    """Delete older generated lists once the new ones are safely in place.

    Runs AFTER the swap, never before: the previous index has to stay usable for the whole
    scan, which can take minutes on a large NFS mount.
    """
    # The list's OWN directory. Every list prunes only what it wrote: with
    # more than one, a prune reaching across them would delete another list's
    # current index the moment their base names matched, which they always do.
    directory = directory or config.LOCAL_LIST_DIR
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError as err:
        print(f"[LIST-CLEAN ERROR] Could not read {directory}: {err}")
        return

    for item in entries:
        if item in keep:
            continue
        # The HYPHEN is the point. Every generated list is
        # f"{LIST_BASE_NAME}-{today}.txt" or f"{LIST_BASE_NAME}-RAR-{today}.txt",
        # so the separator is always there - and matching on the bare prefix
        # also matched the side files, which live in this same directory.
        #
        # With LIST_BASE_NAME derived from a nickname like "dccore" or "dcc",
        # the size side file starts with that prefix too, so every rebuild
        # wrote the side files and then deleted them again. The library's size
        # then disappeared from every public surface permanently: the advert
        # published "Files (0B)" and the CTCP SLOTS payload published 0 raw
        # bytes, on every interval, for ever - and the log line for it read
        # "[LIST-CLEAN] Removed 2 superseded list(s)", which sounds like
        # housekeeping working. Found by audit.
        if not item.startswith(config.LIST_BASE_NAME + "-"):
            continue

        # Belt and braces: never remove a file this run just wrote, whatever
        # the name matching decides.
        if item in (os.path.basename(str(getattr(config, "LIST_SIZE_FILE", ""))),
                    os.path.basename(str(getattr(config, "LIST_RAWBYTES_FILE", "")))):
            continue
        # ".rar" is here because LIST_FORMAT can publish one. Only names that
        # also start with LIST_BASE_NAME are considered, and this is the lists
        # directory, so no album archive a user is waiting on is in reach.
        if not item.endswith((".txt", ".zip", ".rar")):
            continue
        try:
            os.remove(os.path.join(directory, item))
            removed += 1
        except OSError as err:
            print(f"[LIST-CLEAN ERROR] Could not remove {item}: {err}")

    if removed:
        print(f"[LIST-CLEAN] Removed {removed} superseded list(s).")


# The literal shipped default defaults.py derives LIST_BASE_NAME away FROM -
# see its own comment on why the derivation compares against this same
# literal rather than a shared constant (a snapshot taken before overrides
# apply, the way SHIPPED_DEFAULTS does for settings_file.REQUIRED, would
# need LIST_BASE_NAME added to REQUIRED just to get one, which it deliberately
# is not).
_SHIPPED_LIST_BASE_NAME = "DCCore"


# The name the artifacts in LOCAL_LIST_DIR were last published under.
#
# #213: the migration below could only ever carry files across from the shipped
# default, so it worked exactly once. Rename the bot a second time - or from any
# value that was never "DCCore" - and the artifacts on disk keep the old name
# while the bot looks for the new one. A restart does not recover it: the list
# is right there and invisible, and the advert says 0 files.
#
# The previous name has to be remembered somewhere. A marker file in the lists
# directory rather than settings.conf/admin_config.py, because it travels with
# the thing it describes: a value in the config can be hand-edited, replaced
# wholesale on an upgrade, or restored from a backup taken before the rename,
# and each of those silently orphans the lists again - which is the exact
# failure being closed. A file in the directory cannot drift from the directory
# it names, because it is in it.
#
# Absent means an install from before this existed, which is precisely when
# falling back to _SHIPPED_LIST_BASE_NAME is the right guess.
#
# The leading dot and the lack of a .txt/.zip/.rar suffix keep it out of every
# artifact scan in list.py and out of the glob in find_latest_list().
_LIST_BASE_MARKER = ".dccore-list-base"


def list_base_marker_path(directory=None):
    directory = directory or getattr(config, "LOCAL_LIST_DIR", "./lists")
    return os.path.join(directory, _LIST_BASE_MARKER)


def read_list_base_marker(directory=None):
    """The LIST_BASE_NAME the artifacts on disk were published under, or None.

    None on any read problem, deliberately: an unreadable marker must fall back
    to the shipped-default guess, never raise into startup.
    """
    try:
        with io.open(list_base_marker_path(directory), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def write_list_base_marker(name=None, directory=None, log=print):
    """Record the name the artifacts are now published under. Returns True on
    success.

    Written through db._atomic_write, like the advert side files: a marker
    truncated by a crash mid-write would read as absent, sending the next
    migration back to the shipped-default guess and stranding the artifacts
    this call exists to keep findable.
    """
    import db

    name = config.LIST_BASE_NAME if name is None else name
    try:
        db._atomic_write(list_base_marker_path(directory), str(name) + "\n")
        return True
    except Exception as err:
        log(f"[MIGRATE] Could not record the list base name: {err}. "
            f"A future rename may not find these files.")
        return False

def migrate_list_base_name(log=print):
    """Carry existing list files across when LIST_BASE_NAME changed out from
    under them - #184's review: defaults.py's LIST_BASE_NAME
    derivation (an untouched value takes NICKNAME's own value once NICKNAME
    is set) means every existing install's list files, generated before that
    derivation existed, are sitting on disk as "DCCore-<date>.*" while
    LIST_BASE_NAME now resolves to the operator's nickname instead.

    Without this, find_latest_list() globs for the NEW base name, finds
    nothing, and the daemon boots, joins its channels and advertises with no
    list at all - not because there is no list, but because the one on disk
    is filed under a name nothing is looking for any more. It stays that way
    until the next successful !update, which on a weekly rebuild schedule is
    up to a week of a bot that looks healthy and answers every request with
    "not found". The side-file migration's own docstring described
    this exact failure shape for the side-file rename; this is the same
    problem, for a prefix rather than a single filename.

    Deliberately narrow, matching that function's safety properties:

      * only when LIST_BASE_NAME no longer equals what defaults.py ships -
        an install that never had any "DCCore-*" files (fresh, or one that
        chose its own LIST_BASE_NAME from the very first run) has nothing to
        move, and this is a no-op for it.
      * only a file whose new name does not already exist is moved - a
        rebuild that has already happened under the new name wins over
        anything left behind from the old one.
      * os.replace, so an interrupted run leaves one intact file rather than
        two halves; a failure is logged and swallowed per file, because a
        daemon that will not start over a rename is a worse outcome than the
        rename not happening for one file.

    Returns the list of (old, new) basenames actually moved, for the tests
    and for the startup log.
    """
    # EVERY list's directory, not only the primary's. A list's files live in
    # its own directory - list.list_dir(name), with the primary keeping
    # LOCAL_LIST_DIR itself - and the marker that records what they are called
    # is already per-directory. This function simply never looked anywhere but
    # the primary, so renaming the bot orphaned every other list's artifacts:
    # they kept the old base name, nothing on the next startup knew to look
    # for it, and each of those channels advertised a library it no longer had
    # a list for.
    #
    # A failure in one directory must not stop the others, for the same reason
    # a failure on one file does not stop the rest of that directory: a daemon
    # that will not start over a rename is worse than the rename not
    # happening.
    import list as list_mod
    import library

    # No de-duplication of directories. It was written, and then deleted for
    # being unreachable: list_dir() answers LOCAL_LIST_DIR for any list marked
    # primary, so two primaries would share a directory - but load_lists()
    # normalises a hand-edited file down to exactly one primary before this
    # ever sees it. A guard that cannot be reached is a guard no test can
    # falsify, and this file has deleted two of those already.
    moved = []
    for served in library.lists():
        try:
            directory = list_mod.list_dir(served.name)
        except Exception as err:  # a malformed lists.json is not fatal here
            log(f"[MIGRATE] Could not resolve the directory for list "
                f"{served.name!r}: {err}")
            continue
        moved.extend(_migrate_one_list_directory(directory, log=log))
    return moved


def _migrate_one_list_directory(directory, log=print):
    """migrate_list_base_name() for ONE list's directory. Returns the
    (old, new) basenames actually moved."""

    # What the files on disk are actually called, not what they were called
    # when the bot shipped. Absent means an install from before the marker
    # existed, where the shipped default is the right guess (#213).
    previous = read_list_base_marker(directory) or _SHIPPED_LIST_BASE_NAME

    if previous == config.LIST_BASE_NAME:
        # Nothing to move. Still record it: an install that has never been
        # renamed has no marker, and writing one now means its FIRST rename is
        # migrated from the right name rather than from the shipped guess.
        write_list_base_marker(config.LIST_BASE_NAME, directory, log=log)
        return []

    try:
        entries = os.listdir(directory)
    except OSError as err:
        log(f"[MIGRATE] Could not read {directory}: {err}")
        return []

    # The "-" is required, not just the bare prefix: every real artifact is
    # named "DCCore-<date>.ext" or "DCCore-RAR-<date>.ext", always with a
    # hyphen immediately after the base name. A bare startswith("DCCore")
    # would also match a file already renamed to the NEW LIST_BASE_NAME when
    # that name itself happens to start with "DCCore" - e.g. "DCCoreTest" -
    # corrupting an already-correct file instead of leaving it alone.
    old_prefix = previous + "-"

    moved = []
    for item in entries:
        if not item.startswith(old_prefix):
            continue
        if not item.endswith((".txt", ".zip", ".rar")):
            continue

        new_name = config.LIST_BASE_NAME + item[len(previous):]
        old_path = os.path.join(directory, item)
        new_path = os.path.join(directory, new_name)
        if os.path.exists(new_path):
            continue
        try:
            platform_compat.replace_with_retry(old_path, new_path)
            moved.append((item, new_name))
        except OSError as err:
            log(f"[MIGRATE] Could not rename {item} to {new_name}: {err}. "
                f"It will not be found until the next successful !update.")

    for old_name, new_name in moved:
        log(f"[MIGRATE] Renamed {old_name} to {new_name}.")

    # After the move, not before: if the renames failed the marker must still
    # say what the files on disk are really called, or the next startup would
    # look for them under a name nothing has.
    if moved or not read_list_base_marker(directory):
        write_list_base_marker(config.LIST_BASE_NAME, directory, log=log)
    return moved


def _artifact_paths(fmt, date_str, directory=None):
    """Where the download artifact for `fmt` is published, and staged."""
    import list as list_mod
    final = os.path.join(directory or config.LOCAL_LIST_DIR,
                         list_mod.list_artifact_name(fmt, date_str))
    return final, final + ".new"


def _write_text_artifact(tmp_path, members):
    """Both lists as one text file.

    A plain .txt can only be one file where the .zip is two, so the album
    section is appended to the file list rather than dropped. The format is a
    choice about packaging; it is not a request to hand out less. This is a
    copy and not the master index itself precisely because the two must not be
    the same file - see list.FULL_LIST_MARKER.

    THE OPERATOR'S BANNER APPEARS ONCE, NOT ONCE PER SECTION

    Each source list carries its own banner, which is right when they are
    downloaded separately - as .zip and .rar hand them out, and as the !rar
    list is served on its own. Concatenated, that put the operator's ASCII art
    in the middle of the file as well as at the top, which reads as a bug
    rather than as a design.

    The identity line deliberately still repeats. It is a section header - the
    album half of this file should say what is serving it too, and one line is
    not noise. The banner can be any height, which is the difference.

    Removed by matching the exact text read_operator_header() returned rather
    than by recognising a banner in the output, because only the first is
    knowable: the banner is free-form and could otherwise be anything,
    including something that looks like a folder heading.
    """
    banner = read_operator_header()
    # Exactly as generate_master_list() wrote it: a leading blank line, the
    # banner, then the newline ending its last line.
    banner_block = "\n" + banner + "\n" if banner else ""

    with io.open(tmp_path, "w", encoding="utf-8", newline="\n") as out:
        for index, (source, _name) in enumerate(members):
            if index:
                out.write("\n\n")
            _copy_removing_first(source, out,
                                 banner_block if index else "")


# A megabyte at a time. Large enough that the read count is irrelevant next to
# the work of writing, small enough to be noise beside what the scan is
# already holding.
_COPY_CHUNK_CHARS = 1 << 20


def _copy_removing_first(source, out, strip=""):
    """Copy `source` into `out`, removing the FIRST occurrence of `strip`.

    In chunks (#463). This was `handle.read()` followed by
    `text.replace(strip, "", 1)`, and the file it reads is a list - several
    hundred megabytes on a library big enough for the operator to have chosen
    the txt format in the first place, held whole in memory on top of
    everything the rebuild is already holding.

    THE OVERLAP IS WHAT KEEPS IT FAITHFUL. A chunk boundary can fall inside
    the text being removed, so the last len(strip) - 1 characters of each
    chunk are held back and carried into the next rather than written out.
    Without that, a banner straddling a boundary would be written through -
    and the result would be a list with the operator's banner repeated in the
    middle of it, which is the exact thing this removal exists to prevent.
    """
    pending = ""
    with io.open(source, encoding="utf-8") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_CHARS)
            if not chunk:
                break
            if not strip:
                out.write(chunk)
                continue
            pending += chunk
            at = pending.find(strip)
            if at >= 0:
                out.write(pending[:at] + pending[at + len(strip):])
                pending = ""
                strip = ""
                continue
            carry = len(strip) - 1
            if carry:
                out.write(pending[:-carry])
                pending = pending[-carry:]
            else:
                out.write(pending)
                pending = ""
    if pending:
        out.write(pending)


def _write_zip_artifact(tmp_path, members):
    """Store the temp files under their FINAL names, so the archive users
    download is identical to what it always was."""
    # One more heartbeat before the longest silent step in the run.
    # Deflating a list this size is minutes on a machine that has just
    # walked 80 TB, and the daemon's stall watch has nothing else to go
    # on until it finishes - see commands.run_watching_for_a_stall().
    write_progress("packing", force=True)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for source, name in members:
            zipf.write(source, arcname=name)


_STAGING_PREFIX = ".listpack-"


# How often a running scan may write its progress file. A 719k-file library
# would otherwise spend a meaningful part of the run serialising JSON nobody
# read - the dashboard polls every couple of seconds, so anything finer is
# work done for no reader.
PROGRESS_WRITE_SECONDS = 0.5

_progress_last_write = [0.0]

# WHEN THIS RUN BEGAN, stamped once and repeated in every write.
#
# The dashboard cannot work it out for itself: update_list.py is a SUBPROCESS,
# so the only thing the two share is this file, and the daemon may have been
# restarted - or the page opened - long after the rebuild started. Deriving it
# from when the file first appeared would be wrong for the same reason, since
# the file survives the run that wrote it.
#
# Module scope, not per call: a rebuild is one process, so "when did this
# process start" is the honest answer to "how long has this been running", and
# it cannot drift as the run proceeds.
_started_at = time.time()


def progress_path():
    """Where a rebuild reports what it is doing. Resolved per call, like every
    other configurable path here - !rehash reloads config."""
    return getattr(config, "LIST_PROGRESS_FILE", os.path.join("data", "list_progress.json"))


def write_progress(phase, folder="", folder_index=0, folder_count=0,
                   files=0, force=False):
    """Report the current step, for the dashboard to poll.

    NEVER RAISES AND NEVER BLOCKS THE SCAN. This is a progress bar: a full
    disk, a read-only data/ or a permissions problem must cost the operator
    the bar, not the list rebuild they actually asked for.

    Throttled to PROGRESS_WRITE_SECONDS, except when `force` marks a step an
    operator would notice missing - the start, each folder, and the end.
    """
    now = time.time()
    if not force and (now - _progress_last_write[0]) < PROGRESS_WRITE_SECONDS:
        return
    _progress_last_write[0] = now
    payload = {"phase": phase, "folder": folder, "folder_index": folder_index,
               "folder_count": folder_count, "files": files, "at": now,
               "started_at": _started_at}
    try:
        path = progress_path()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(platform_compat.long_path(directory), exist_ok=True)
        # Written whole and renamed into place: the dashboard polls this
        # while it is being written, and half a JSON object is a parse error
        # every couple of seconds rather than a progress bar.
        temp = path + ".new"
        with io.open(platform_compat.long_path(temp), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        platform_compat.replace_with_retry(temp, path)
    except Exception:
        pass


def clear_progress():
    """Drop the progress file once a run is over.

    A leftover from a killed process would otherwise read as a rebuild that
    is still going, and the dashboard would show a bar that never moves.
    """
    try:
        os.remove(platform_compat.long_path(progress_path()))
    except OSError:
        pass


def _discard_stale_temps(directory=None):
    """Remove ".new" staging files a previous run was killed in the middle of.

    Only names the builder itself stages: "<LIST_BASE_NAME>-...new", in the
    lists directory. Nothing reads these - find_latest_list() globs "*.txt"
    and is_list_artifact() ends in the real extension, so a leftover has never
    been served or counted - which is exactly why nothing removed them either.

    Called at the START of a build, where this run has staged nothing yet, so
    the only file it can reach belongs to a run that is no longer alive. (Two
    builds running at once already write the SAME temp paths as each other, so
    this adds no hazard that concurrency does not already have.)
    """
    directory = directory or config.LOCAL_LIST_DIR
    base = str(getattr(config, "LIST_BASE_NAME", "") or "")
    if not base:
        return
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    removed = 0
    for name in entries:
        if not (name.startswith(base + "-") and name.endswith(".new")):
            continue
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as err:
            print(f"[LIST-CLEAN ERROR] Could not remove {name}: {err}")
    if removed:
        print(f"[LIST-CLEAN] Removed {removed} leftover staging file(s) "
              f"from an interrupted run.")


def _discard_stale_staging(directory=None):
    """Remove staging directories a previous run was killed in the middle of."""
    directory = directory or config.LOCAL_LIST_DIR
    try:
        entries = os.listdir(directory)
    except OSError:
        return
    for name in entries:
        if not name.startswith(_STAGING_PREFIX):
            continue
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _write_rar_artifact(tmp_path, members, directory=None):
    """Pack with the rar binary. False if it could not be done, with the reason.

    rar has no equivalent of zipfile's arcname, so the members are copied to
    their published names in a scratch directory first - packing the temporary
    files directly would put "DCCore-2026-08-30.txt.new" inside the archive and
    every reader would see the name the build happened to use that run.

    The scratch directory sits inside LOCAL_LIST_DIR so the finished archive
    lands on the same filesystem: os.replace is only atomic within one.
    """
    rar_bin = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
    if not rar_bin:
        print("[LIST-GEN] LIST_FORMAT is 'rar' but no rar binary was found - "
              "set RAR_BINARY or put rar on PATH. Packing a .zip instead.")
        return False

    # The finally below removes this run's directory. A daemon killed between
    # mkdtemp and that finally leaves one behind, and nothing else in lists/
    # ever looks at directories, so it would sit there until somebody noticed.
    _discard_stale_staging(directory)
    directory = directory or config.LOCAL_LIST_DIR
    staging = tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=directory)
    try:
        staged = []
        for source, name in members:
            target = os.path.join(staging, name)
            shutil.copyfile(source, target)
            staged.append(os.path.abspath(target))

        # rar names the archive itself when the given name has no extension,
        # so it is built inside the staging directory under a name it cannot
        # reinterpret and moved into place afterwards.
        built = os.path.join(staging, "list.rar")
        # -ep stores the names with no path at all, which is the arcname
        # behaviour above. A list of arguments and never a shell, and a
        # timeout, both for the same reasons dcc.py packs albums that way: a
        # hung rar here would wedge every list rebuild after it.
        cmd = [rar_bin, "a", "-ep", "-w" + os.path.abspath(directory),
               os.path.abspath(built)] + staged
        # utf-8/replace for the same reason as dcc.py's rar call.
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=getattr(config, "RAR_TIMEOUT", 1800))
        if result.returncode == 0 and os.path.exists(built):
            platform_compat.replace_with_retry(built, tmp_path)
            return True
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"[LIST-GEN] rar exited {result.returncode} packing the list - "
              f"packing a .zip instead. {detail[-1] if detail else ''}")
        return False
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        print(f"[LIST-GEN] Could not pack the list with rar ({err}) - "
              f"packing a .zip instead.")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_list_artifact(fmt, members, date_str, directory=None):
    """Build what a user downloads. Returns (fmt_used, tmp_path, final_path).

    `fmt_used` is not always `fmt`: "rar" falls back to "zip" when the binary
    is missing or the pack fails. That is deliberate. The list is the whole
    point of the bot, and refusing to publish one over a packaging preference
    would take it off the air; the fallback is loud, and the operator still
    has something to serve while they sort the binary out.
    """
    final, tmp = _artifact_paths(fmt, date_str, directory)
    if fmt == "rar":
        if _write_rar_artifact(tmp, members, directory):
            return fmt, tmp, final
        fmt = "zip"
        final, tmp = _artifact_paths(fmt, date_str, directory)

    if fmt == "txt":
        _write_text_artifact(tmp, members)
    else:
        _write_zip_artifact(tmp, members)
    return fmt, tmp, final


def list_identity_line(nickname=None):
    """"Served by <nick> - <version> - <url>", with absent parts left out.

    Shared by the .txt and the !rar list so the two cannot drift, and built by
    joining on what is actually present: an operator who blanks SCRIPT_VERSION
    or PROJECT_URL should get a shorter line, never a dangling " - " with
    nothing after it.

    The fallbacks here are empty strings rather than a literal like "DCCore".
    A non-empty fallback would be a second opinion about a value config.py
    already declares, and which one won would depend on how the value happened
    to be read - see tests/test_config_fallbacks.py, which enforces exactly
    that. An empty fallback declines to have an opinion instead.
    """
    if nickname is None:
        nickname = getattr(config, "NICKNAME", "")

    parts = [str(getattr(config, "SCRIPT_VERSION", "") or "").strip(),
             str(getattr(config, "PROJECT_URL", "") or "").strip()]
    detail = " - ".join(part for part in parts if part)

    return f"Served by {nickname} - {detail}" if detail else f"Served by {nickname}"


def read_operator_header(path=None, max_bytes=None, log=print):
    """The operator's own banner for the top of the list, or "" if there is none.

    Free-form: several lines, ASCII art, a channel name - whatever should greet
    whoever opens the file. Everything else written into the list goes through
    _one_line(), which flattens control characters; doing that here would
    destroy the art this exists to carry, so the banner deliberately bypasses
    it.

    TWO LINE SHAPES ARE NEUTRALISED, because the banner is written into the
    body of the master index and the index has a grammar of its own. Found by
    audit, both with the banner working exactly as documented:

      A line that is entirely "=" is a folder RULE to every reader. A banner
      with an odd number of them leaves list.find_matching_entries()'s
      rule/heading/rule machine mid-block, so the next banner line is taken as
      a folder heading and the REAL heading after it is swallowed - every file
      in the first folder is then attributed to a folder that does not exist.
      That value is not cosmetic: the dashboard's duplicate-finder and the
      File Lists view both resolve it. Rewritten to the same width in "-", so
      the box still looks like a box.

      A line beginning with "!" is a request ROW to every reader - the count in
      the advert, the CTCP SLOTS payload, and @find. A banner line like
      "!!! NEW RELEASES !!!" was counted as a file and returned as a genuine
      search hit, so a user could request it and receive nothing. Dropped
      rather than rewritten: there is no edit that keeps such a line looking
      like itself while stopping it being read as a row.

    Both are reported with the offending line, so the operator can change the
    banner rather than wonder what happened to it. Neutralised HERE rather
    than at the point of writing because _split_master_list() removes the
    banner from each split part by matching the exact text this function
    returned - so both sides have to be the same text.

    "" on anything unreadable, matching read_list_base_marker()'s reasoning: a
    banner is decoration, and no decoration should ever stop a list being
    built. A missing file is the normal state, not an error.

    Line endings are normalised to "\\n" because the list is opened without
    newline="", so the writer translates "\\n" to os.linesep itself. A CRLF
    banner written through that untouched would come out "\\r\\r\\n" on Windows.
    """
    if path is None:
        path = getattr(config, "LIST_HEADER_FILE", "./data/list_header.txt")
    if max_bytes is None:
        max_bytes = getattr(config, "LIST_HEADER_MAX_BYTES", 8192)

    try:
        with io.open(platform_compat.long_path(path), "rb") as handle:
            raw = handle.read(int(max_bytes) + 1)
    except (OSError, ValueError, TypeError):
        return ""

    if not raw:
        return ""

    truncated = len(raw) > int(max_bytes)
    if truncated:
        raw = raw[:int(max_bytes)]
        print(f"[LIST-GEN] {path} is larger than {max_bytes} bytes; "
              f"the banner was truncated.")

    # errors="replace": this is operator-supplied and may be in any encoding.
    # A banner in the wrong code page should come out looking odd, not abort
    # the list build.
    text = raw.decode("utf-8", errors="replace")

    # Normalise line endings, drop trailing blank lines so this function owns
    # the spacing around the banner rather than inheriting whatever the file
    # happened to end with.
    text = "\n".join(text.splitlines()).rstrip("\n")
    return _neutralise_banner(text, log=log)


def _neutralise_banner(text, log=print):
    """The banner with any line the index's own grammar would misread made
    safe. See read_operator_header()'s docstring for what and why.

    Returns the text unchanged when there is nothing to do, which is the
    overwhelmingly common case - most banners are prose and ASCII art that
    happens not to be a bare rule.
    """
    if not text:
        return text

    safe = []
    for line in text.split("\n"):
        stripped = line.strip()

        # A row, to every reader in the project.
        if stripped.startswith("!"):
            log(f"[LIST-GEN] A banner line starting with '!' was left out of "
                f"the list: every reader counts it as a file, and @find would "
                f"return it as a match nobody can download. The line was: "
                f"{stripped[:60]!r}")
            continue

        # A folder rule, to every reader in the project.
        if stripped and set(stripped) == {"="}:
            log(f"[LIST-GEN] A banner line of only '=' was rewritten with '-': "
                f"it reads as a folder rule and would shift every folder "
                f"heading after it by one. The line was {len(stripped)} "
                f"characters wide.")
            safe.append(line.replace("=", "-"))
            continue

        safe.append(line)

    return "\n".join(safe).rstrip("\n")


def generate_master_list(list_name=None):
    """Scan one list's folders, clear its old files first, and build its lists.

    `list_name` picks which list to build; without one this is the primary,
    which on a single-list install is the only one there is - so every existing
    caller, and the whole of this function's previous behaviour, is unchanged.

    EVERY PATH BELOW COMES FROM `directory`, not from config.LOCAL_LIST_DIR.
    That is the single change that makes more than one list possible: for the
    primary the two are the same string, and for any other list it is a
    subdirectory. See list.list_dir() for why the list is in the path and not
    in the filename.
    """
    import os
    import sys
    import time
    import datetime
    import zipfile
    import re
    import defaults as config
    import db
    import library
    import list as list_mod

    directory = list_mod.list_dir(list_name)
    if not os.path.exists(directory):
        os.makedirs(directory)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    txt_filename = f"{config.LIST_BASE_NAME}-{today}.txt"
    rar_filename = f"{config.LIST_BASE_NAME}-RAR-{today}.txt"
    # Same shape as the album list's name, and it has to be: list.py's
    # find_latest_list() globs "<base>-*.txt" and takes the LAST one, so this
    # file sorts after the master list and would silently become "the" list
    # that @find searches and the advert counts. VIDEO_LIST_MARKER is how that
    # function knows to leave it alone - the same guard "-RAR-" already needs.
    video_filename = f"{config.LIST_BASE_NAME}-{list_mod.VIDEO_LIST_MARKER}-{today}.txt"

    txt_path = os.path.join(directory, txt_filename)
    rar_path = os.path.join(directory, rar_filename)
    video_path = os.path.join(directory, video_filename)
    
    # config.RAR_ENABLED (#140) refuses every !rar request. Building an album
    # list anyway, and shipping it inside the zip every user downloads, hands
    # people a file whose every line is an instruction to use a command this
    # bot will refuse - they paste one, get "Folder packing is disabled", and
    # reasonably conclude the bot is broken rather than that the feature is
    # off. Raised on #140 and left as a follow-up there.
    serve_albums = bool(getattr(config, "RAR_ENABLED", True))
    if not serve_albums:
        print("[LIST-GEN] RAR_ENABLED is off - skipping the album list entirely.")

    SIZE_FILE_PATH = os.path.join(directory, config.LIST_SIZE_FILE)
    RAWBYTES_FILE_PATH = os.path.join(directory, config.LIST_RAWBYTES_FILE)

    scan_start = time.time()

    # The old lists are NOT deleted here any more. This ran before the scan, so an NFS mount
    # that went away, a disk that filled, or any exception below left the daemon with no index
    # at all: find_latest_list() returns None, @find answers "No MasterList found" and the
    # advert publicly announces 0 files, until someone notices and runs a successful !update.
    #
    # The new lists are written to temporary names alongside the real ones and swapped in only
    # once the whole generation has succeeded (see finalise below). A failed run now leaves the
    # previous index exactly as it was.
    tmp_txt_path = txt_path + ".new"
    tmp_rar_path = rar_path + ".new"
    tmp_video_path = video_path + ".new"
    # Every format's temporary, not just the configured one: a run that asked
    # for .rar and fell back to .zip has staged two, and the guards below have
    # no way of knowing which without repeating the decision.
    tmp_index_paths = (tmp_txt_path, tmp_rar_path, tmp_video_path)
    tmp_artifact_paths = tuple(_artifact_paths(f, today, directory)[1]
                               for f in list_mod.LIST_FORMATS)
    tmp_all_paths = tmp_index_paths + tmp_artifact_paths
    # Before anything of THIS run exists. A run that fails discards its own
    # temporaries; a run that is KILLED - the machine goes down mid-scan,
    # which on a large library is a window of minutes - cannot, and those
    # files carry the date they were staged on, so the next day's run stages
    # different names and never touches them again. They accumulate one set
    # per killed run, for ever, in the directory the operator looks at to see
    # whether their lists are being built.
    _discard_stale_temps(directory)

    scan_folders = library.folders(list_name)
    print("[LIST-GEN] Scanning the library in "
          + ", ".join(f"{f.path} ({f.name})" for f in scan_folders) + "...")

    all_files_data = []
    # The music list, the film-and-series list, and which folders !rar may
    # pack. All three filled by the one walk below - the !rar album list has
    # been built from the same pass since long before this.
    video_files_data = []
    packable_folders = set()
    total_bytes = 0

    # A single-folder leftover used to sit here: `scan_root` built from
    # config.FILE_DIRECTORY and then immediately overwritten inside the
    # per-folder loop below. Dead since #164, and reading a setting that is
    # only the FALLBACK for an install with no folder list - the same
    # confusion an operator raised about it still sitting on the Settings page.
    # The real scan root, and why it is long_path()-wrapped, are in the
    # loop.

    # Every subtree os.walk() could not read (a stale NFS handle, EIO, a
    # revoked ACL) used to be skipped in total silence under the default
    # onerror=None - total_files_count came out non-zero, so the zero-files
    # guard further down never caught it, and a truncated index was
    # published over the previous good one. Collected here and checked
    # once the walk finishes, so one bad subtree costs the whole run
    # (keeping the previous index) rather than a silent partial one.
    walk_errors = []
    denied_dirs = []
    # Folders left out because their own name cannot survive the list format
    # (#464) - see has_backslash_component() at the point it is used.
    unlistable_dirs = []

    def _on_walk_error(err):
        # TWO DIFFERENT FAILURES, AND ONLY ONE OF THEM IS WORTH ABORTING FOR
        # (#441).
        #
        # A directory that is PERMANENTLY unreadable - a Windows volume root's
        # System Volume Information, a POSIX lost+found, anything whose ACL
        # excludes the account the daemon runs as - fails identically on every
        # future scan. Treating it as "keep the previous index" meant the list
        # could never be rebuilt again on that install: every !update ran the
        # whole scan and then threw the result away, for ever, and the log line
        # said only that part of the library could not be read.
        #
        # A directory that has VANISHED mid-scan is the opposite: it means the
        # library changed underneath us, the scan we just did is of a state
        # that no longer exists, and publishing it would be publishing a
        # snapshot known to be wrong. That one still aborts.
        #
        # The test is whether the path is still there. Permission denied on a
        # directory that exists is a fact about the ACL, not about the scan.
        denied = isinstance(err, PermissionError)
        if denied:
            try:
                denied = os.path.isdir(err.filename)
            except Exception:
                denied = False

        if denied:
            denied_dirs.append(err.filename)
            print(f"[LIST-GEN] Skipping {err.filename!r}: permission denied, and "
                  f"it is still there - excluded from the list rather than "
                  f"failing the whole rebuild.")
            return

        walk_errors.append(err)
        print(f"[LIST-GEN ERROR] Could not read {err.filename!r} during the scan: {err}")

    # Once, here - see is_listed_file()'s note. Resolving these inside the
    # walk asked the question per file rather than per scan, and a 719k-file
    # library would have rebuilt each tuple 719,000 times.
    ignored = ignored_extensions()
    video_exts = video_extensions()
    packable_exts = rar_extensions()
    split_video = bool(getattr(config, "SEPARATE_VIDEO_LIST", True))

    print("[LIST-GEN] Indexing every file"
          + (f", except {len(ignored)} type(s): {', '.join(ignored)}"
             if ignored else " - no extensions are being skipped")
          + ".")
    if split_video:
        print(f"[LIST-GEN] Film and series go in their own list "
              f"({len(video_exts)} extension(s)).")
    else:
        print("[LIST-GEN] One combined list - SEPARATE_VIDEO_LIST is off.")

    write_progress("scanning", folder_count=len(scan_folders), force=True)

    for folder_number, scan_folder in enumerate(scan_folders, start=1):
        # Reported per folder because the folder COUNT is the one total known
        # before the walk starts - a file total would need a full pass to
        # find, which is the work being measured. So the bar advances a
        # folder at a time and the file counter shows life in between.
        write_progress("scanning", folder=scan_folder.name,
                       folder_index=folder_number,
                       folder_count=len(scan_folders),
                       files=len(all_files_data) + len(video_files_data),
                       force=True)

        # A folder that is not there RIGHT NOW is skipped, not fatal: an
        # unplugged drive or an unmounted share should cost its own contents,
        # not take the whole list - and the bot - off the air. Deliberately
        # different from walk_errors below, which is a subtree going unreadable
        # DURING a walk of a folder that was present: that is a systemic
        # failure of a library we are meant to be reading, and it keeps the
        # previous index rather than publishing a truncated one.
        #
        # Every folder missing is still caught, further down: the zero-files
        # guard refuses to publish an empty list.
        if not os.path.isdir(platform_compat.long_path(scan_folder.path)):
            print(f"[LIST-GEN] Skipping {scan_folder.name} - "
                  f"{scan_folder.path} is not available right now. The list "
                  f"is being built without it.")
            continue

        # long_path()-wrapped, and the SAME wrapped value is relpath()'s base
        # below - see the note above scan_root's original single-folder form.
        scan_root = platform_compat.long_path(scan_folder.path)

        # walk_with_sizes(), not os.walk: the size comes back with the name
        # rather than being asked for again per file. See its docstring for
        # the measurement. Traversal ORDER differs from os.walk's and cannot
        # matter - all_files_data is sorted by (folder, filename) below before
        # anything is written.
        for root, files in walk_with_sizes(scan_root, onerror=_on_walk_error):
            # Throttled inside write_progress(), so this costs a clock read
            # per directory rather than a file write.
            write_progress("scanning", folder=scan_folder.name,
                           folder_index=folder_number,
                           folder_count=len(scan_folders),
                           files=len(all_files_data) + len(video_files_data))

            # ONCE PER DIRECTORY, NOT ONCE PER FILE (#463).
            #
            # rel_dir is a property of the directory - it was computed inside
            # the per-file loop below, so a folder of 20 tracks paid 20
            # relpath() calls and 20 join() calls for the same answer, and
            # stored 20 separate equal strings in all_files_data. Hoisted, the
            # rows share one object: 149.9 B/file against 206.9 B/file
            # measured, about 0.29 GiB at 5.4M files, and the calls go from
            # one per file to one per directory.
            #
            # The folder's LABEL leads every path (#164). Two folders can hold
            # the same relative path - the same album in flac and in mp3 is
            # the ordinary case, not a corner one - and without the label
            # their headings would be identical text that resolution could not
            # tell apart.
            #
            # Written for a single folder too. Labelling only at two or more
            # would mean an operator who adds a second folder after weeks of
            # serving changes every path anyone already saved; doing it once,
            # at the upgrade, is one break instead of two.
            rel_dir = os.path.relpath(root, scan_root)
            if rel_dir == ".":
                rel_dir = ""
            rel_dir = (os.path.join(scan_folder.name, rel_dir)
                       if rel_dir else scan_folder.name)

            # A LITERAL BACKSLASH IN A FOLDER NAME IS NOT A SEPARATOR (#464).
            #
            # On the Linux box this daemon runs on, a backslash is an ordinary
            # filename character, and unzipping a Windows-made archive
            # produces one routinely. The list writes its headings with
            # backslashes as separators, and list.list_heading_parts() reads
            # them back by splitting on both separators - so a directory named
            # "Rock\Metal" is written as ...\music\Rock\Metal\ and read back as
            # ["music", "Rock", "Metal"]. If music/Rock/Metal exists, every
            # file under it - and its !rar row - resolves into that unrelated
            # folder and the requester silently gets the wrong album. If it
            # does not, nothing under the real folder can ever be served.
            #
            # Excluded rather than escaped. The list format is read by other
            # bots and by AutoQ, so an escape convention would have to be
            # understood by readers that already exist and never will be.
            # library.problems() already refuses a backslash in a folder LABEL
            # for exactly this reason ("the label travels further than the
            # machine that made it"); this is the same rule applied to the
            # components below it. A folder that cannot be named in the list
            # cannot be served from it, so listing it would advertise
            # something unreachable.
            if has_backslash_component(rel_dir):
                # No dedup needed: the walk visits each directory once, so
                # rel_dir is new every time it gets here.
                unlistable_dirs.append(rel_dir)
                continue

            # Keep every track under its exact, complete path on disk
            for file, file_bytes in files:
                if is_listed_file(file, ignored):
                    if file_bytes is None:
                        full_file_path = os.path.join(root, file)
                        # #228: a bare `except: pass` left file_bytes at 0 and
                        # the entry was published anyway - permission denied, a
                        # dangling symlink, or a file removed mid-scan all read
                        # as a legitimate 0-byte track. The list is the thing
                        # the bot HANDS OUT: publishing it meant offering a
                        # download that can only ever fail once someone
                        # actually requests it, with the library's reported
                        # total size quietly short and no log line saying why.
                        # Excluded and logged instead - one bad file costs
                        # itself, not the whole scan (walk_errors above is for
                        # a whole SUBTREE going unreadable, a more systemic
                        # failure than one file).
                        print(f"[LIST-GEN ERROR] Skipping {full_file_path!r}, "
                              f"its size could not be read.")
                        continue
                    total_bytes += file_bytes

                    # WHICH folder may be packed, decided per FILE and
                    # remembered per folder. A folder earns its !rar row from
                    # holding something worth packing, not from holding
                    # anything at all - see RAR_EXTENSIONS.
                    if is_packable_file(file, packable_exts):
                        packable_folders.add(rel_dir)

                    # WHICH list the row goes in. With the split off, video
                    # lands in the same list as everything else, which is the
                    # behaviour this had before the setting existed.
                    if split_video and is_video_file(file, video_exts):
                        video_files_data.append((rel_dir, file, file_bytes))
                    else:
                        all_files_data.append((rel_dir, file, file_bytes))

    if walk_errors:
        print(f"[LIST-GEN ERROR] {len(walk_errors)} part(s) of the library could not be "
              "read - keeping the previous index rather than publishing a truncated one.")
        return False

    if denied_dirs:
        # Said once, with the count, because it is a standing condition rather
        # than an event: these folders will be missing from every list until
        # the operator changes their permissions, and an operator comparing
        # the file count against their own expectation deserves to know why
        # it is short.
        print(f"[LIST-GEN] {len(denied_dirs)} folder(s) were excluded because "
              f"the daemon cannot read them. They are not in this list and "
              f"will not be in the next one until their permissions change: "
              f"{', '.join(repr(p) for p in denied_dirs[:5])}"
              f"{' ...' if len(denied_dirs) > 5 else ''}")

    if unlistable_dirs:
        # Same shape and same reasoning as denied_dirs above: a standing
        # condition rather than an event, and an operator comparing the file
        # count against their own expectation deserves to know why it is
        # short.
        print(f"[LIST-GEN] {len(unlistable_dirs)} folder(s) were excluded "
              f"because a literal backslash in the name cannot be told from a "
              f"path separator once the list is written - anything under them "
              f"would resolve to a different folder or to nothing. Rename "
              f"them to list them: "
              f"{', '.join(repr(p) for p in unlistable_dirs[:5])}"
              f"{' ...' if len(unlistable_dirs) > 5 else ''}")

    # Sort by the real folder and file names
    all_files_data.sort(key=lambda x: (str(x[0]).lower(), str(x[1]).lower()))
    # THE SAME ORDER, FOR THE SAME REASON (#443). all_files_data has been
    # sorted here since the beginning; video_files_data is built by the same
    # walk and was only ever appended to, so the film and series list came out
    # in whatever order the filesystem handed the directories over - which is
    # reverse-ish on NTFS and arbitrary elsewhere. A reader scanning for a
    # title has no way to guess where it is.
    video_files_data.sort(key=lambda x: (str(x[0]).lower(), str(x[1]).lower()))

    total_files_count = len(all_files_data)
    scan_end = time.time()
    elapsed_seconds = scan_end - scan_start
    if elapsed_seconds <= 0:
        elapsed_seconds = 0.1

    files_per_second = int((total_files_count + len(video_files_data))
                           / elapsed_seconds)
    def format_total_size(b):
        for unit in ['B','KB','MB','GB','TB']:
            if b < 1024.0: return f"{b:.2f}{unit}"
            b /= 1024.0
        return f"{b:.2f}PB"
    def format_size_human(b):
        return format_total_size(b)

    # The WHOLE library, which is what the side files and the advert want.
    formatted_size = format_total_size(total_bytes)
    # This list's own share of it. The header below describes this file, and
    # a music list announcing a size that includes films it does not contain
    # is a number no reader can reconcile with what they are looking at.
    music_bytes = sum(size for _folder, _name, size in all_files_data)
    formatted_music_size = format_total_size(music_bytes)
    
    time_struct = time.gmtime(elapsed_seconds)
    duration_str = time.strftime("%H:%M:%S", time_struct)

    day = datetime.datetime.now().day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    date_header_str = datetime.datetime.now().strftime(f"{day}{suffix} %b %Y")
    
    # A LIST THAT CANNOT PRODUCE AN ALBUM DOES NOT GET AN ALBUM SECTION.
    #
    # serve_albums is one global read of RAR_ENABLED, applied to every list a
    # build produces. That is right for the switch itself - an operator turning
    # packing off means everywhere - and wrong as the only question asked,
    # because a list can be incapable of packing anything regardless of the
    # switch.
    #
    # A video-only list is the case that showed it. Since #288, a folder earns
    # its !rar row by holding a file in RAR_EXTENSIONS, and those are the audio
    # formats; LIST_VIDEO_EXTENSIONS and RAR_EXTENSIONS share not one entry. So
    # its album list is not "usually empty" - it can never hold a row, for any
    # library, under any configuration.
    #
    # It was still built, and still shipped: the masthead makes the file
    # non-empty, so the `getsize > 0` test below let it into the archive every
    # user downloads. Four dead lines and a heading with nothing behind it,
    # once per download, forever.
    #
    # DECIDED AFTER THE SCAN, not from the extension sets. What actually
    # matters is whether this list produced a packable folder, and the scan has
    # just finished answering that exactly - where reasoning from the
    # configured extensions would be predicting it, and would be wrong for a
    # list whose folders simply hold no albums today and might tomorrow.
    #
    # Turning serve_albums off HERE rather than adding a second flag is what
    # makes this small: every consequence already exists below - the masthead
    # is skipped, no rows are written, the empty temp file fails the size test
    # for the archive, and the `if not serve_albums:` branch removes it instead
    # of publishing it.
    if serve_albums and not packable_folders:
        serve_albums = False
        print("[LIST-GEN] No folder in this list can be packed - skipping the "
              "album list rather than shipping an empty one.")

    # Set the moment the swap completes, and read by the handler at the
    # bottom - which has to tell a failure BEFORE the swap from one
    # after it. Declared out here so the early failures do not meet an
    # unbound name in the very handler meant to report them.
    published = False
    try:
        with open(tmp_txt_path, "w", encoding="utf-8") as f, \
             open(tmp_rar_path, "w", encoding="utf-8") as f_rar:
                 
            f.write(f"List of {total_files_count:,} Files ({formatted_music_size}) generated on {date_header_str} in {duration_str} ( {files_per_second:,} Files Per Second )\n")
            f.write(f"To request a file, copy/paste to the channel... !{config.NICKNAME} FILENAME eg. !{config.NICKNAME} Songname.flac\n")

            # The operator's banner and the bot's identity go BELOW the two
            # lines above and above the first folder - not at the very top.
            # commands.count_from_master_list() reads one readline() and pulls
            # "List of N Files" out of it, so anything inserted above that line
            # silently zeroes the count feeding !update and the channel advert:
            # no exception, no empty file, just a list that advertises nothing.
            #
            # The identity line exists because this file travels. It gets sent
            # to strangers over DCC and reopened weeks later in a text editor
            # with no memory of which bot produced it.
            # The identity line sits directly under the two functional lines,
            # ABOVE the operator's banner. The banner is free-form and can be
            # any height, so putting it first would push the attribution off
            # the reader's first screen exactly on the installs that decorate
            # the most. Fixed position, always visible.
            #
            # Still not line 1: commands.count_from_master_list() does one
            # readline() and regexes "List of N Files" out of it, so anything
            # above that line silently zeroes the count feeding !update and the
            # channel advert - no exception, no empty file, just a list that
            # advertises nothing.
            operator_header = read_operator_header()
            f.write(list_identity_line() + "\n")
            if operator_header:
                f.write("\n" + operator_header + "\n")
            f.write("\n")

            if serve_albums:
                f_rar.write(f"List of Entire Album Folders (!rar) for !{config.NICKNAME} generated on {date_header_str}\n")
                f_rar.write(f"To request an entire album, copy/paste the line... eg. "
                            f"!{config.NICKNAME} !rar {list_mod.LIST_FOLDER_PREFIX}Album\\\n")
                # Same order as the .txt above, and for the same reason. The
                # !rar list is a separate download that travels on its own, so
                # it carries its own copy rather than inheriting one.
                f_rar.write(list_identity_line() + "\n")
                if operator_header:
                    f_rar.write("\n" + operator_header + "\n")
                f_rar.write("="*90 + "\n\n")

            current_folder = None
            written_rar_folders = set()  # Keeps the !rar list free of duplicate rows

            for folder, filename, bytes_size in all_files_data:
                if folder != current_folder:
                    current_folder = folder
                    
                    # The text list gets the complete subfolder (e.g. \Digital Media 1\)
                    raw_folder_str = (f"{list_mod.LIST_FOLDER_PREFIX}{folder}\\"
                                      if folder else list_mod.LIST_FOLDER_PREFIX)
                    display_folder = raw_folder_str.replace("/", "\\")
                    
                    # The rule is drawn to the width of the folder line it wraps, not
                    # to a fixed 53 characters. Against the real library every one of
                    # the 4,107 folder headers is wider than 53 - they run 54 to 136,
                    # averaging 80 - so the fixed rule never once matched the line it
                    # was framing.
                    #
                    # Measured on _one_line()'s output rather than the raw string:
                    # that is what actually gets written, and it flattens control
                    # characters, which changes the length.
                    folder_line = _one_line(display_folder)
                    folder_rule = "=" * len(folder_line)
                    f.write(f"\n{folder_rule}\n")
                    f.write(f"{folder_line}\n")
                    f.write(f"{folder_rule}\n")
                    
                    # Strip multi-disc suffixes, for the !rar album list ONLY.
                    #
                    # Matched as a whole PATH SEGMENT (split on the same
                    # separators the folder can carry), not a substring
                    # anywhere in the path - a substring match let "\disc"
                    # fire on "\Discography" and "\media" fire on "\Media
                    # Markt Hits", collapsing a real album folder to the
                    # ARTIST root, which dcc.py refuses outright ("Artist
                    # root folders cannot be requested"). The album then had
                    # no requestable row at all: written_rar_folders
                    # deduplicates on the truncated (wrong) string.
                    #
                    # Also requires the truncation to leave at least two
                    # segments below FILE_DIRECTORY - i.e. never collapse to
                    # the artist root - and finds the EARLIEST matching
                    # segment by walking the path in order, rather than the
                    # old "first box word in LIST order" behaviour, which
                    # made the truncation point depend on the order this
                    # list happened to be written in.
                    # A folder earns a !rar row from holding something
                    # worth PACKING, not from holding anything at all. Before
                    # this the only conditions were "has a folder" and
                    # RAR_ENABLED, which read as "album folders" while the
                    # scan took .mp3 and .flac and nothing else. Once the scan
                    # took every file, every folder in the library became
                    # packable - a season of a series, or a folder holding one
                    # text note - with no size cap anywhere behind a line
                    # anybody in the channel can paste. See RAR_EXTENSIONS.
                    if folder and serve_albums and folder in packable_folders:
                        folder_segments = re.split(r'[\\/]', folder)
                        truncate_at = None
                        for seg_index, segment in enumerate(folder_segments):
                            if _BOX_WORD_RE.match(segment.strip()):
                                truncate_at = seg_index
                                break
                        # Three, not two. The threshold means "leave at least
                        # two segments below the FOLDER" - artist and album -
                        # and rel_dir now begins with the folder's label
                        # (#164), so every index shifted by one. Left at 2, a
                        # library shaped <label>/<artist>/<disc 1> would
                        # truncate to <label>/<artist>: the artist root, which
                        # dcc.py refuses outright, leaving that album with no
                        # requestable row at all - the exact failure the
                        # threshold was added to prevent.
                        if truncate_at is not None and truncate_at >= 3:
                            rar_folder_clean = "/".join(folder_segments[:truncate_at])
                        else:
                            # Truncating here would collapse to the artist
                            # root (or nothing) - offering the untruncated
                            # real path, box word and all, is still a
                            # request dcc.py will actually serve; the
                            # refused artist root is not.
                            rar_folder_clean = folder

                        raw_rar_str = f"{list_mod.LIST_FOLDER_PREFIX}{rar_folder_clean}\\"
                        display_rar_folder = raw_rar_str.replace("/", "\\")
                        
                        # Write the row exactly once per album to the .rar text file.
                        #
                        # NOTHING MAY BE APPENDED TO THIS LINE.
                        #
                        # It is not a display row, it is a command. AutoQ.mrc -
                        # the mIRC script people use to auto-download files and
                        # folders - copies this line out of the list and sends it
                        # verbatim, so anything after the folder stops it matching.
                        #
                        # The exact mechanism, read out of AutoQ.mrc's own
                        # `on *:filercvd:*:` handler rather than inferred:
                        #
                        #   $left($trunc($nopath($filename)),-4)
                        #       == $nopath($left($trunc($3-),-1))
                        #
                        # `$3-` is "token 3 to END OF LINE" - greedy, not just
                        # the folder token. Clean row: $3- is the folder,
                        # $left(...,-1) drops the trailing backslash, $nopath()
                        # takes "Album", and it matches the received filename
                        # minus ".rar", so the row is dequeued. Append anything
                        # and $3- swallows it too; $trunc() strips the spaces,
                        # so the character $left(...,-1) removes is the last
                        # letter of what was appended rather than the
                        # backslash, and $nopath() then extracts a substring
                        # that can never match. filercvd never fires, and the
                        # album stays queued in AutoQ forever despite having
                        # actually arrived. Verified against the script and
                        # reproduced (#256).
                        #
                        # Updating AutoQ is not a way out: people install old
                        # copies from mirrors, so the deployed versions cannot
                        # be assumed current.
                        #
                        # A folder size here is the obvious and recurring idea, and
                        # it was wanted. dcc.py already cites AutoQ compatibility
                        # for archive and filename shape, but nothing said it
                        # constrained this line, so the objection had to be
                        # rediscovered.
                        #
                        # The size belongs on the MAIN list's per-folder heading
                        # instead - see the folder_line write below. That heading
                        # is framed decoration, not a row AutoQ imports, so it can
                        # carry anything; and putting it there leaves this file at
                        # exactly one line per album, which a second ::INFO:: line
                        # per row would not. Tracked in #69.
                        #
                        # THE REASON, corrected against AutoQ.mrc itself rather
                        # than the second-hand version this comment used to give.
                        # A !rar row takes AutoQ's verbatim branch -
                        # `if (($2 == !rar) && ($3 != $null)) { aline -n $1- }` -
                        # the whole line, untouched. So anything appended here is
                        # sent as part of the request. That is absolute and has
                        # nothing to do with extensions.
                        #
                        # The file rows below end "::INFO:: <size>", and AutoQ does
                        # not merely cope with that: its file branch truncates at
                        # the end of the extension, so it never sees the tail at
                        # all. A further trailing field on a FILE row is therefore
                        # safe for AutoQ - the constraint there is its accept list
                        # (*.mp3 and *.rar), not the tail. See defaults.py's note
                        # above LIST_IGNORED_EXTENSIONS for the quoted source.
                        if display_rar_folder not in written_rar_folders:
                            f_rar.write(f"!{config.NICKNAME} !rar {_one_line(display_rar_folder)}\n")
                            written_rar_folders.add(display_rar_folder)
                single_file_size = format_size_human(bytes_size)
                f.write(f"!{config.NICKNAME} {_one_line(filename)}  ::INFO:: {single_file_size}\n")

        # The film and series list. Written after the music one and from the
        # same walk, exactly as the album list is - a separate file with its
        # own header, travelling in the same archive.
        #
        # Only written when there is something to put in it: a music-only
        # library should not gain an empty file it has no use for, and every
        # reader below treats "no video list" as the ordinary case rather than
        # as a fault.
        # The one decision the three sites below all ask about: did THIS run
        # publish a film list? Asking the filesystem instead let a stale file
        # from an earlier rebuild answer for a run that produced nothing.
        wrote_video_list = bool(video_files_data)
        if wrote_video_list:
            video_bytes = sum(size for _f, _n, size in video_files_data)
            with open(tmp_video_path, "w", encoding="utf-8") as f_video:
                f_video.write(
                    f"List of {len(video_files_data):,} Films & Series "
                    f"({format_total_size(video_bytes)}) generated on "
                    f"{date_header_str}\n")
                f_video.write(
                    f"To request one, copy/paste to the channel... "
                    f"!{config.NICKNAME} FILENAME eg. !{config.NICKNAME} "
                    f"Some.Film.2021.mkv\n")
                # Same order as the .txt above, and for the same reason: this
                # file travels on its own once it is out of the archive, so it
                # carries its own attribution rather than inheriting one.
                f_video.write(list_identity_line() + "\n")
                if operator_header:
                    f_video.write("\n" + operator_header + "\n")
                f_video.write("\n")

                video_folder = None
                for folder, filename, bytes_size in video_files_data:
                    if folder != video_folder:
                        video_folder = folder
                        raw = (f"{list_mod.LIST_FOLDER_PREFIX}{folder}\\"
                               if folder else list_mod.LIST_FOLDER_PREFIX)
                        line = _one_line(raw.replace("/", "\\"))
                        rule = "=" * len(line)
                        f_video.write(f"\n{rule}\n{line}\n{rule}\n")
                    f_video.write(
                        f"!{config.NICKNAME} {_one_line(filename)}"
                        f"  ::INFO:: {format_size_human(bytes_size)}\n")
            print(f"[LIST-GEN] Film & series list created: {tmp_video_path}")

        print(f"[LIST-GEN] Text list created: {tmp_txt_path}")
        # The walk is over by here, so the folder bar has nothing left to say.
        # The phase does: writing and packing a 719k-row list is not instant,
        # and a bar that sat at 100% through it would look stalled.
        write_progress("writing", files=len(all_files_data) + len(video_files_data),
                       force=True)
        if serve_albums:
            print(f"[LIST-GEN] RAR album list created: {tmp_rar_path}")
        
        # The size and rawbytes side files used to be published here. They are now
        # written with the lists in the finalise section below - see the comment
        # there for why writing them ahead of the guards was half a rollback.
            
        # A scan that found NOTHING is almost always an unavailable mount, not an empty
        # library - and publishing it would replace a good index with an empty one, which is
        # exactly the failure this rewrite exists to prevent. Size cannot be the test: the
        # file always carries two header lines, so it is never zero bytes.
        #
        # Accept an empty result only when there is no working index to lose, so a genuine
        # first run on an empty library still succeeds.
        if total_files_count == 0 and not video_files_data:
            try:
                existing = [f for f in os.listdir(directory)
                            if f.startswith(config.LIST_BASE_NAME)
                            and f.endswith(".txt") and "-RAR-" not in f]
            except OSError:
                existing = []
            if existing:
                print("[LIST-GEN ERROR] Scan found 0 files but an index already exists "
                      "(mount unavailable?). Keeping the previous index.")
                _discard_temp_lists(*tmp_all_paths)
                return False
            print("[LIST-GEN] Scan found 0 files and there is no previous index; "
                  "publishing an empty list.")

        if not os.path.exists(tmp_txt_path):
            print("[LIST-GEN ERROR] No list was written. Keeping the previous index.")
            _discard_temp_lists(*tmp_all_paths)
            return False

        # ---- what a user actually downloads -----------------------------
        # config.LIST_FORMAT decides how the two text lists are handed over:
        # as one plain .txt, packed into a .zip, or packed into a .rar.
        # OmenServe has offered the same three for years, and which of them a
        # given person's client opens without complaint still differs.
        #
        # This is deliberately NOT tied to RAR_ENABLED. That switch governs
        # whether the bot will pack an ALBUM FOLDER for a stranger on demand -
        # minutes of CPU and a large temporary file, once per request. Packing
        # two text files once per rebuild, on the operator's own schedule, is
        # a different job and does not belong behind the same switch.
        members = [(tmp_txt_path, os.path.basename(txt_path))]
        # The film and series list rides in the same archive, on the same
        # terms as the album list: present when it has content, absent when
        # the library has no video, and never a reason to fail the build.
        if wrote_video_list:
            members.append((tmp_video_path, os.path.basename(video_path)))
        if os.path.exists(tmp_rar_path) and os.path.getsize(tmp_rar_path) > 0:
            members.append((tmp_rar_path, os.path.basename(rar_path)))

        wanted_format = list_mod.list_format()
        print(f"[LIST-GEN] Packing the list for download as .{wanted_format}...")
        artifact_format, tmp_artifact_path, artifact_path = build_list_artifact(
            wanted_format, members, today, directory)

        # Everything generated cleanly. Swap the new files in, THEN remove the superseded
        # ones. os.replace overwrites atomically on both POSIX and Windows, where os.rename
        # would raise because the destination already exists.
        #
        if not serve_albums:
            # Not published, and not left behind either: an empty album list
            # in lists/ reads as "this bot offers no albums" to anything
            # counting the file, which is a different claim from "it does
            # not offer them at all".
            _discard_temp_lists(tmp_rar_path)

        # THE DOWNLOAD ARTIFACT FIRST, because it is the one a DCC send holds
        # open - so the likeliest failure is discovered before anything else
        # has moved. All of them go together or none of them do; see
        # _publish_artifacts().
        swaps = [(tmp_artifact_path, artifact_path), (tmp_txt_path, txt_path)]
        # Only when it was written. A library with no video has no video list,
        # and the publish must not try to move a file that was never staged.
        if wrote_video_list:
            swaps.append((tmp_video_path, video_path))
        if serve_albums:
            swaps.append((tmp_rar_path, rar_path))
        _publish_artifacts(swaps)

        # THE POINT OF NO RETURN (#442). Everything from here to the except is
        # tail work - side files, cleanup, bookkeeping - and any of it can
        # raise. The swap has already happened, so the NEW list is the one in
        # use from this line onward, and the handler must not go on claiming
        # the old one was kept.
        published = True

        # The two side files are published HERE, AFTER every swap above has
        # already succeeded, and atomically. They used to be written before
        # the swaps - so a failure partway through (a Windows os.replace can
        # raise PermissionError if something else has the destination open)
        # rolled the list itself back to the previous index while the size
        # and byte count it wears had already been overwritten with the new
        # scan's numbers: an old index publishing a new scan's size. And a
        # plain open(..., "w") truncates first, so an interruption left a
        # readable but EMPTY file, which is unparseable and used to cost the
        # caller the file count and the list date as well as the size.
        db._atomic_write(SIZE_FILE_PATH, formatted_size)
        db._atomic_write(RAWBYTES_FILE_PATH, str(total_bytes))
        # #213: the artifacts just published are named after the CURRENT
        # LIST_BASE_NAME, so this is the moment that fact becomes true. Written
        # here rather than only in the migration, so a rename between two
        # rebuilds is still migrated from the right name.
        write_list_base_marker(config.LIST_BASE_NAME, directory=directory,
                               log=print)

        # DUPLICATE FILENAMES, SAID OUT LOUD AT BUILD TIME.
        #
        # The dashboard has answered this since #164 - Tools > Verify list,
        # build_verify_list_payload() - but only when somebody goes and looks,
        # and the operators most likely to have collisions are the ones running
        # several folders without watching a web page. The scan is holding the
        # data already, so saying it here costs nothing and no second walk.
        #
        # Why it matters: a request names a FILE, not a path, because a bare
        # filename is all the list gives a requester to copy.
        # dcc.handle_download_request() resolves that name against the list and
        # serves the FIRST folder it finds it under - so every later copy is
        # listed, looks requestable, and can never be sent. The requester does
        # not get an error either; they get the other file.
        #
        # Both lists, in the order all_list_paths() hands them to the resolver:
        # a name in the music list and the film list resolves to the music one.
        # Counting them separately would miss exactly the collisions the split
        # introduced.
        #
        # The COUNT only. The detail belongs to the view that already presents
        # it properly, resolved to paths this machine has; printing folder
        # headings here would be a second presentation to keep in step.
        # THE COUNT, NOT THE ANSWER (#463). This used to build a second full
        # copy of the library - one dict per file - and hand it to
        # find_duplicate_filenames(), whose result was passed to len() and
        # dropped. At 5.4M files that copy measured 2.2 GiB and took the peak
        # for the whole rebuild to 3.5 GiB, on top of the sort's own peak.
        # count_duplicate_filenames() holds one entry per distinct NAME and
        # answers the same question by the same definition.
        def _every_listed_row():
            # A GENERATOR, not a list. count_duplicate_filenames() reads its
            # input once and keeps one entry per distinct NAME, so handing it
            # a materialised copy of the library would put back a smaller
            # version of the very thing this change removes.
            for folder, name, _size in all_files_data:
                yield folder, name
            for folder, name, _size in video_files_data:
                yield folder, name

        duplicate_count = list_mod.count_duplicate_filenames(_every_listed_row())
        if duplicate_count:
            print(f"[LIST-GEN] WARNING: {duplicate_count} filename(s) appear "
                  f"under more than one folder. A request names a file, not a "
                  f"path, so only the first copy of each can ever be sent. "
                  f"Dashboard: Tools > Verify list.")

        print(f"[LIST-GEN] New lists activated: {os.path.basename(txt_path)} "
              f"(download: {os.path.basename(artifact_path)})")

        keep = {os.path.basename(txt_path), os.path.basename(artifact_path)}
        # The film list too, when this run published one. It is named
        # "<base>-VIDEO-<date>.txt", so the prefix match below picks it up as
        # a generated list - correctly - and without this it would be swapped
        # in and deleted again in the same run, every run. The only trace was
        # a "[LIST-CLEAN] Removed 1 superseded list(s)" line that reads like
        # housekeeping working, which is exactly how the side files were lost
        # once already (see _prune_superseded_lists).
        # THIS RUN's decision, not "a file is sitting at that path". video_path
        # carries today's date, so on a second rebuild the same day that file
        # is the EARLIER run's output - and a run that found no video would
        # have kept it, leaving a film list describing films that are gone:
        # still searchable, still counted by the advert, and absent from the
        # archive users actually download. It self-corrected across a date
        # boundary, which is why a single-build test never saw it.
        if wrote_video_list:
            keep.add(os.path.basename(video_path))
        if serve_albums:
            keep.add(os.path.basename(rar_path))
        # Yesterday's artifact in another format goes with the rest. It is not
        # only clutter: find_latest_list_file() falls back to whatever HAS been
        # built when the configured format has not been yet, so a stale .zip
        # left beside a fresh .rar would go on being handed out to somebody the
        # day the operator switched formats and the build failed.
        _prune_superseded_lists(keep=keep, directory=directory)
        return True
            
    except Exception as e:
        print(f"[LIST-GEN ERROR] Failed to generate the lists: {e}")
        if published:
            # THE SWAP ALREADY HAPPENED. Saying the old list was kept would be
            # exactly backwards: the new list is live and serving, and only
            # the tail work after the swap failed. An operator told "the
            # previous list is still in use" reasonably concludes nothing
            # changed and that the rebuild can simply be retried - while the
            # bot is already serving the new one.
            print("[LIST-GEN] The new list WAS published and is in use - the "
                  "failure above happened afterwards, in the tail work. Do "
                  "not treat this as 'nothing changed'.")
        else:
            print("[LIST-GEN] The previous list was left untouched and is still in use.")
        _discard_temp_lists(*tmp_all_paths)
        return False

def generate_all_lists(log=print):
    """Build every configured list. True only if every one of them succeeded.

    On a single-list install this is one call to generate_master_list() with no
    name - byte for byte what running this script has always done. The loop is
    what a second list turns on.

    EACH LIST IS BUILT INDEPENDENTLY, and one failing does not stop the rest. A
    list whose folder is on an unavailable mount must not take down the list
    whose folder is on a local disk: generate_master_list() already refuses to
    publish an empty scan over a working index, so a failed list goes on serving
    what it last built while its neighbours move on. Returning False is what
    tells the operator - and !update - that something needs looking at.

    The failures are NAMED, not counted. "1 of 3 lists failed" sends somebody to
    read a log they already have open; naming it tells them which folder to go
    and look at.
    """
    import library

    # The progress file is dropped HERE, not only in __main__, so every entry
    # point clears it and the behaviour is reachable from a test. A leftover
    # from a finished run reads as a rebuild still in progress, and the
    # dashboard would show a bar that never moves.
    try:
        return _generate_all_lists(log)
    finally:
        clear_progress()


def _generate_all_lists(log):
    import library

    every = library.lists()
    if len(every) == 1:
        return generate_master_list()

    failed = []
    for entry in every:
        log(f"[LIST-GEN] Building {entry.name!r} ({len(entry.folders)} folder(s))...")
        try:
            if not generate_master_list(entry.name):
                failed.append(entry.name)
        except Exception as err:
            # One list's unexpected failure is not every list's. The scan
            # handles the failures it can predict; this is the backstop for the
            # ones it cannot, and catching it here is the difference between
            # "two of your three lists rebuilt" and "the update crashed".
            log(f"[LIST-GEN ERROR] {entry.name!r} failed: {err}")
            failed.append(entry.name)

    if failed:
        log("[LIST-GEN ERROR] These lists were not rebuilt and are still serving "
            "what they last built: " + ", ".join(repr(n) for n in failed))
        return False
    log(f"[LIST-GEN] All {len(every)} lists rebuilt.")
    return True


if __name__ == "__main__":
    print("--- Starting the scheduled weekly file-list update ---")
    # FILE_DIRECTORY is not in settings_file.REQUIRED (see its own comment) -
    # a blank value is a supported "not chosen yet" state the daemon itself
    # boots fine with, so os.path.exists(None) here (a TypeError, not a
    # clean failure) must be guarded against explicitly rather than assuming
    # a real string ever reaches this point.
    # Across EVERY list, not just the primary. The two guards below ask
    # "is there anything to build at all", and with several lists the
    # answer is yes if any one of them has somewhere to look - a single
    # list pointed at an unplugged drive is generate_all_lists()'s
    # problem to report, not a reason to refuse the whole run.
    configured = [folder for entry in library.lists() for folder in entry.folders]
    if not configured:
        print("[CRITICAL] No music directory configured yet - set FILE_DIRECTORY "
              "from the web dashboard's Settings page, settings.conf, or "
              "admin_config.py before running this.")
        sys.exit(1)
    # Every one missing, not any one: a single unavailable folder is skipped
    # during the scan with a warning, and only a library with nothing readable
    # in it at all is worth refusing to run for.
    if not any(os.path.isdir(platform_compat.long_path(f.path)) for f in configured):
        print("[CRITICAL] None of the configured music folders exist: "
              + ", ".join(f.path for f in configured))
        sys.exit(1)
        
    # The file is removed whichever way the run ends, including the failure
    # branch: a leftover from a crashed or killed process reads as a rebuild
    # still in progress, and the dashboard would show a bar that never moves.
    # generate_all_lists() clears the progress file in its own finally, so a
    # crash inside it still leaves nothing behind for the dashboard to read.
    success = generate_all_lists()
    if success:
        print("--- The list was updated successfully. ---")
        sys.exit(0)
    else:
        print("--- ERROR: could not generate the list. ---")
        sys.exit(1)
