# =====================================================================
# DCC.PY - THE TRANSFER ENGINE
# =====================================================================
import select
import socket
import struct
import threading
import time
import os
import sys
import re
import subprocess

import defaults as config
import platform_compat
import update_list
import list as list_mod
import announce
import db
import library
import runtime

# THE queue lock: bound to runtime.py's object, not constructed here - dcc.py is
# reloaded by !rehash (commands.CORE_MODULES), and importlib.reload() re-executing
# `queue_lock = threading.Lock()` would rebind this name to a brand-new lock every
# time, exactly the bug runtime.py's own module docstring exists to remove for
# containers. A thread already inside `with queue_lock:` when that happened would
# go on holding the old, now-invisible object while the next caller acquired the
# fresh one - two threads in the critical section at once. Binding to runtime.py's
# object instead means a reload re-runs this same line and picks the identical live
# lock back up, so `with queue_lock:` throughout this file always resolves to the
# one object every caller has ever held, and is what db.py's and announce.py's own
# comments mean when they say "queue_lock".
queue_lock = runtime.queue_lock

def names_a_remote_or_absolute_path(name, windows=None):
    """True for a requested name that points somewhere the library is not (#578).

    A request is a name inside the library, never a location. On Windows,
    os.path.join(base, "\\\\host\\share\\x") returns the UNC path unchanged, and
    the os.path.exists()/realpath() calls that come BEFORE the containment check
    then make Windows resolve the host and open an SMB session as the account
    running the bot - an outbound connection, and an NTLM handshake, that anybody
    in the channel could start with one line. Refused here, on the text, before
    anything touches the file system.

    UNC and device paths (`\\\\host\\share`, `\\\\?\\`, `//host/share`: Windows reads a
    doubled slash of either kind the same way) are refused everywhere - no real
    library name starts that way. A drive letter (`C:\\x`), a drive-relative one
    (`C:x`) or a root-relative one (`\\x`) can only mean something on Windows, so
    they are refused only there (`windows` overrides, for tests). A NUL byte is
    refused everywhere: os.path calls raise on it.
    """
    text = str(name)
    if "\x00" in text:
        return True
    if text.replace("/", "\\").startswith("\\\\"):
        return True
    if windows is None:
        windows = platform_compat.IS_WINDOWS
    if windows:
        import ntpath
        if ntpath.splitdrive(text)[0] or text.startswith("\\"):
            return True
    return False


# A LIBRARY-WIDE LOOKUP IS THE EXPENSIVE THING A REQUEST CAN ASK FOR (#580).
# A name that is not in the first folder's root makes handle_download_request()
# read every published list and then walk every configured folder; each request
# runs on its own thread and the flood gate allows ten a nick per five seconds.
# Two bounds, both cheap: only a few scans at a time (the rest are told the bot
# is busy, at once, without touching the disk), and a name that just missed is
# answered "not found" from memory for a minute - the same stale row pasted ten
# times costs one scan, not ten.
MAX_CONCURRENT_LIBRARY_SCANS = runtime.MAX_CONCURRENT_LIBRARY_SCANS
LOOKUP_MISS_TTL_SECONDS = 60.0
LOOKUP_MISS_MEMORY = 512
# WHAT A PASTE OF NINE ROWS COSTS (#886). Only misses were remembered, so
# every request for a file that exists paid the scan again - and a user
# pasting nine rows out of the list, which is how these lists are meant to
# be used, started nine scans, of which MAX_CONCURRENT_LIBRARY_SCANS - 1
# ran and the rest were refused outright with "busy ... try again in a
# moment", advice that points a novice straight at the flood gate.
#
# Three memories now, each cheap and each verified before it is trusted:
# the name that was found (repeats), the FOLDERS recent lookups resolved
# into (a batch is nearly always siblings in one album, so the second row
# onwards is an os.path.exists rather than a scan), and a short wait for a
# scan slot instead of an instant refusal. Nothing here decides what may
# be sent: is_safe_path() still checks the resolved path against every
# configured root afterwards, unchanged.
LOOKUP_HIT_TTL_SECONDS = 300.0
LOOKUP_HIT_MEMORY = 512
LOOKUP_FOLDER_MEMORY = 32
LOOKUP_SCAN_WAIT_SECONDS = 5.0
# Owned by runtime.py, which nothing reloads (#749) - bound by name, the way
# queue_lock is, so a !rehash re-runs these lines and picks the same live
# objects back up. The memories themselves are this module's own cache.
_library_scans = runtime.library_scans
_lookup_misses = globals().get("_lookup_misses") or {}
_lookup_misses_lock = runtime.lookup_memory_lock
_lookup_hits = globals().get("_lookup_hits") or {}
_lookup_folders = globals().get("_lookup_folders") or {}


def _lookup_missed_recently(key):
    with _lookup_misses_lock:
        when = _lookup_misses.get(key)
        if when is None:
            return False
        if time.monotonic() - when >= LOOKUP_MISS_TTL_SECONDS:
            _lookup_misses.pop(key, None)
            return False
        return True


def _note_lookup_miss(key):
    with _lookup_misses_lock:
        _lookup_misses.pop(key, None)          # re-noted: newest again
        _lookup_misses[key] = time.monotonic()
        excess = len(_lookup_misses) - LOOKUP_MISS_MEMORY
        if excess > 0:
            for stale in list(_lookup_misses)[:excess]:
                _lookup_misses.pop(stale, None)


def _remembered_path(key):
    """The path a recent lookup resolved `key` to, if it is still there.

    A hint, never an authority: the entry is dropped and None returned when
    the file has gone, so a library reorganised between two requests costs
    one stale check rather than a wrong answer. The caller puts the result
    through is_safe_path() exactly as it does a freshly scanned one.
    """
    with _lookup_misses_lock:
        remembered = _lookup_hits.get(key)
        if remembered is None:
            return None
        path, when = remembered
        if time.monotonic() - when >= LOOKUP_HIT_TTL_SECONDS:
            _lookup_hits.pop(key, None)
            return None
    if not os.path.exists(platform_compat.long_path(path)):
        with _lookup_misses_lock:
            _lookup_hits.pop(key, None)
        return None
    return path


def _note_lookup_hit(key, path):
    """Remember where a scan found this name, and the folder it was in."""
    folder = os.path.dirname(path)
    with _lookup_misses_lock:
        _lookup_hits.pop(key, None)            # re-noted: newest again
        _lookup_hits[key] = (path, time.monotonic())
        excess = len(_lookup_hits) - LOOKUP_HIT_MEMORY
        if excess > 0:
            for stale in list(_lookup_hits)[:excess]:
                _lookup_hits.pop(stale, None)
        if folder:
            folders = _lookup_folders.setdefault(key[0], [])
            if folder in folders:
                folders.remove(folder)
            folders.append(folder)             # newest last
            del folders[:-LOOKUP_FOLDER_MEMORY]


def _in_a_recent_folder(list_name, file_name):
    """`<folder>/<name>` for the folders recent lookups resolved into.

    The reported case (#886): a user pastes nine rows from one album. The
    first costs a scan and names the folder; the other eight are one
    os.path.exists each, in the folder their sibling was just found in -
    newest folder first, because a batch arrives together.
    """
    with _lookup_misses_lock:
        folders = list(reversed(_lookup_folders.get(list_name, [])))
    for folder in folders:
        candidate = os.path.join(folder, file_name)
        if os.path.exists(platform_compat.long_path(candidate)):
            return candidate
    return None


def forget_library_lookups():
    """Drop every remembered lookup - for a rebuild, and for the tests."""
    with _lookup_misses_lock:
        _lookup_hits.clear()
        _lookup_folders.clear()
        _lookup_misses.clear()




def is_safe_path(base_dir, path, follow_symlinks=True):
    """Safety filter: prevents directory traversal attacks.

    A falsy base_dir (#184's review: FILE_DIRECTORY is
    deliberately not in settings_file.REQUIRED any more, so callers that
    used to be able to assume it was always set can no longer do so) refuses
    rather than raising: os.path.realpath(None) is a TypeError, and every
    caller here is a security boundary, where "there is no base to check
    against" must read as "nothing is safe", not as an unhandled exception a
    caller's own except Exception can turn into a silent, unexplained
    failure.
    """
    if not base_dir:
        return False

    if follow_symlinks:
        matchpath = os.path.realpath(path)
    else:
        matchpath = os.path.abspath(path)

    base = os.path.realpath(base_dir)

    # Compares per directory step, rather than a plain startswith. With
    # startswith alone, "/srv/library-backup" would wrongly be accepted as
    # part of "/srv/library", because the string happens to begin the same way.
    #
    # The separator is appended only when the base does not already end in
    # one, which a DRIVE ROOT always does: os.path.realpath("D:\\") is
    # "D:\\", so `base + os.sep` built "D:\\\\" - a doubled separator no real
    # path can start with - and this returned False for every file on the
    # drive. Found by audit, and reachable the moment an operator serves a
    # whole drive: the master list advertises every file on it and every
    # request for one is refused as a path violation, with the refusal logged
    # as a security event rather than as the configuration problem it is.
    # "/" on POSIX is the same shape.
    boundary = base if base.endswith(os.sep) else base + os.sep
    return matchpath == base or matchpath.startswith(boundary)

def default_announce_channel():
    """The one channel to announce in when a queue entry does not name one.

    ONE, not the list. The value this replaces was `config.CHANNEL.split(',')`,
    used as the default of `next_file.get('channel', ...)` - so an entry
    without a 'channel' key handed a LIST to start_dcc_send(), which passes it
    to announce.send_transfer_complete(), which builds

        f"PRIVMSG {channel} :"

    The line that went out was `PRIVMSG ['#one', '#two'] :Sent ...` - a
    malformed target the server answers with a numeric nothing reads, so the
    "Sent" announcement was simply lost. Nothing raised, and the transfer
    itself had already succeeded.

    Every entry the request path creates does set 'channel' (handle_download_request
    and the two paths around it), which is why this stayed latent - it needs an
    entry from somewhere else, or one restored from a queue written before that
    key existed.

    Deferred import: irc.py imports THIS module at load time, so reaching for
    it at the top would close a cycle. The same shape as the `import dcc_fetch`
    calls inside irc.py's own handlers.
    """
    import irc
    channels = irc.configured_channels()
    return channels[0] if channels else ""


def announce_channel_for(next_file):
    """The channel one queue entry's completion should be announced in.

    Lifted out of the queue-completion path so it can be tested at all: the
    caller needs a socket, a live queue and a channel-user map, and the rule
    itself is four lines. The same reason resolve_dcc_address() and
    take_complete_lines() were lifted out of irc_loop().

    `or`, not get()'s default: an entry carrying an EMPTY channel is as
    unusable as one carrying none, and only the second case was handled
    before.
    """
    if isinstance(next_file, dict):
        named = next_file.get('channel')
        # A STRING, specifically. `if named:` accepted a list, which is the
        # very thing this function exists to stop reaching
        # `f"PRIVMSG {channel} :"` - it fixed the DEFAULT and let a stored one
        # straight through. A queue entry whose channel is a list is malformed
        # either way; falling back to the configured channel is predictable,
        # where picking one of its entries would be a guess.
        if isinstance(named, str) and named.strip() and is_channel_name(named):
            return named.strip()
    return default_announce_channel()


def is_channel_name(target):
    """Does this PRIVMSG target name a channel rather than a nick?

    RFC 2812 gives channels four prefixes; a nick can start with none of them.
    A request made by PRIVATE MESSAGE records the bot's OWN NICK as the row's
    channel (it is the wire target of that message - see
    handle_download_request), and until #530 announce_channel_for() handed it
    straight back, so the "Sent:" line for a PM request went out as
    `PRIVMSG <our nick> :Sent ...` - the bot telling itself. A nick is not a
    place to announce; the default channel is.
    """
    text = str(target or "").strip()
    return bool(text) and text[0] in "#&+!"


def download_count_identity(file_path, file_name):
    """(key, display name, kind) for one completed send, for db.record_download().

    Split out of start_dcc_send() so it can be tested without a socket: the
    rules below are the whole reason the "most downloaded" tables mean
    anything, and they were previously buried in the middle of a transfer.

    An ALBUM goes out as a packed archive from TMP_ZIP_DIR whose name this
    module built from the folder, so the archive's own name is already the
    readable one and identifies the album on its own.

    A FILE is keyed by its path relative to the library and only DISPLAYED by
    its basename. Two albums can hold a track with the same filename - #110 is
    the bug where exactly that ambiguity sent the wrong file - so keying on the
    basename would credit one track with another's downloads and report a
    popularity that belongs to neither.
    """
    if _is_temp_zip_cache_file(file_path):
        name = file_name[:-4] if str(file_name).lower().endswith(".rar") else file_name
        return file_name, name, "album"

    # THE LIST ITSELF IS NOT A DOWNLOAD. It is how somebody finds out what the
    # downloads ARE, so counting it answers a question nobody asked: it is
    # sent to everyone who has ever typed the nickname, which makes it the
    # most-requested item on every bot, forever, in a table whose whole job is
    # to say which of the FILES people want.
    #
    # Worse than one wrong row. The artifact's name carries the build date, so
    # every rebuild starts a new key - the table slowly fills with dated
    # copies of the same list and pushes real files out of the top ten. And
    # because the list lives in LOCAL_LIST_DIR rather than under any library
    # folder, library_count_key() cannot make it relative to anything and
    # falls back to the ABSOLUTE path, which is the one form #151 made these
    # keys relative to avoid.
    #
    # A None key is the "do not count this" signal - see db.record_download(),
    # which returns on it. The rule stays here, with the other two, rather
    # than becoming a second condition at the call site.
    #
    # Checked AFTER the album branch on purpose: an album is identified by
    # where it is (TMP_ZIP_DIR, which this module wrote), and that is
    # unambiguous. A folder packed as "<base name>-<date>.rar" would otherwise
    # match the list naming rule and go uncounted.
    if list_mod.is_list_artifact_name(file_name):
        return None, file_name, "list"

    return library_count_key(file_path), file_name, "file"


def library_count_key(file_path):
    """The download-counter key for a served file: label, then the path
    beneath that folder.

    THE LABEL IS PART OF THE IDENTITY, and used not to be. The key was

        os.path.relpath(file_path, config.FILE_DIRECTORY)

    which is one root, and #164's own cost table listed this as one of the
    places that assumed there was only one. With several folders configured it
    went wrong two different ways, neither of them loudly:

      * FILE_DIRECTORY unset - which is now ordinary, since the dashboard can
        write data/library_folders.json and never touch FILE_DIRECTORY at all -
        makes relpath() measure from the CURRENT WORKING DIRECTORY, so the key
        described where the daemon was started from rather than the library.
      * FILE_DIRECTORY set to the first folder keys a file in the second as
        "..\\Second\\Artist\\Album\\track.flac", and raises ValueError
        outright when the two are on different drives - falling back to the
        ABSOLUTE path, which is the one thing #151 made these keys relative to
        avoid, because it breaks every counter the moment a library moves.

    Keyed on the label rather than the folder's path for that same reason: an
    operator who moves D:\\Flac to E:\\Flac and updates the folder list keeps
    their history, because the label did not change.

    library.is_inside() rather than dcc.is_safe_path(): this is attribution,
    not the request-time security gate, and it runs after the transfer has
    already completed. is_safe_path() stays the gate on the request path.

    A file under none of the configured folders keeps its absolute path as the
    key. That is a temp archive or a folder removed from the list mid-session;
    it is not a reason to lose the row.

    EVERY list's folders, not the primary's. This runs after the transfer, by
    which point the list that served the file is no longer in hand - and
    folders() with no name means the PRIMARY list, so on a multi-list install
    every file served from any other list fell through to the absolute-path
    branch. That is a counter key holding a drive letter, which does not match
    the same file counted from anywhere else and would ship a real path into a
    stats table.
    """
    for folder in library.every_folder():
        if library.is_inside(folder.path, file_path):
            try:
                relative = os.path.relpath(file_path, folder.path)
            except ValueError:
                break
            return os.path.join(folder.name, relative)
    return file_path


def path_is_in_our_library(path):
    """Is this path inside a folder some configured list is built from?

    The pack-time question, and deliberately not the same one the request
    path asks. A request knows which list it is being served from and is
    resolved against THAT list's folders, which is the stronger check. By the
    time a packed row comes back here the list name is gone, and a queued row
    can legitimately have come from any list.

    A named function rather than the inline `any(...)` it replaces, because
    the inline form could only be tested by a test that rewrote it - and a
    test that reimplements the check it is guarding passes just as happily
    against the broken version. This one is callable.

    Still is_safe_path() per folder, so each comparison resolves symlinks and
    compares per separator: widening this from one list to all lists must not
    widen it to the filesystem.
    """
    return any(is_safe_path(folder.path, path)
               for folder in library.every_folder())


def _sanitize_rar_leaf_name(folder_leaf):
    """The single, shared sanitiser for a packed album's LEAF folder name -
    used both to build the queue row's visible filename (what a user's
    client sees, and what AutoQ.mrc's own reconciliation matches) and the
    packer's own archive name. #162 finding #7: these used to be two
    independent regexes that disagreed - one deleted a disallowed
    character outright, the other replaced it with "_"; one kept square
    brackets, the other stripped them; one was ASCII-only (a-zA-Z0-9),
    silently mangling a non-ASCII album name the other left alone. The
    packer had grown a special-case "does the real folder name have an
    apostrophe the cleaned name lost? re-derive from scratch" workaround
    for exactly this kind of divergence - one shared function makes that
    workaround unnecessary, because the two call sites can no longer
    disagree in the first place.

    Preserves parentheses, brackets and apostrophes - real album tags use
    all three ("[WEB] [192K]", "A Winter's Tale") - and \\w (Unicode-aware
    in Python 3, unlike a literal a-zA-Z0-9 class) keeps real non-ASCII
    library names intact. Everything else, spaces included, becomes "_".
    """
    cleaned = re.sub(r"[^\w\-\.\(\)\[\]']", "_", str(folder_leaf))
    return cleaned.replace(" ", "_")


def _rar_archive_disk_name(source_dir):
    """Where a packed album's archive actually lives in TMP_ZIP_DIR - built
    from the path RELATIVE TO FILE_DIRECTORY (sanitised), not just the leaf
    folder name. #162 finding #7: two different artists' albums that
    happen to share a leaf name ("Greatest Hits") used to collide into the
    SAME disk filename - and `rar a` ADDS to an existing archive rather
    than replacing it, so the second requester silently received both
    albums packed together.

    Deliberately NOT the same name a user's client sees over DCC (see
    _sanitize_rar_leaf_name() for that, built from the leaf alone and
    unchanged by this fix) - AutoQ.mrc's own reconciliation compares the
    received (de-underscored) name against the QUEUED FOLDER'S OWN
    BASENAME, so changing what the user is offered would silently break
    that matching for every existing deployment. Only WHERE the bytes
    live on disk changes; what a user is offered does not.
    """
    try:
        rel = os.path.relpath(source_dir, config.FILE_DIRECTORY)
    except ValueError:
        rel = os.path.basename(str(source_dir).rstrip("/\\"))
    rel = rel.replace("\\", "/").strip("/")
    segments = [_sanitize_rar_leaf_name(part) for part in rel.split("/") if part]
    return f"{'_'.join(segments) or 'album'}.rar"


def _is_temp_zip_cache_file(path):
    """Is `path` one of the packed .rar archives in TMP_ZIP_DIR - eligible for
    the "delete once nothing else needs it" cleanup in start_dcc_send()?

    Checked against config.TMP_ZIP_DIR's actual configured value, not a
    hardcoded directory name: a literal "tmp_zips" substring check against
    the shipped default meant an operator who renamed the setting (via
    admin_config.py or settings.conf, both documented override points)
    would have every packed .rar sent successfully and never cleaned up
    afterward - this would simply never be true again, silently.

    The ".zip" exclusion is unchanged from before this fix: TMP_ZIP_DIR also
    holds the master list's own zip, which this cleanup must never touch.
    """
    path = str(path)
    return is_safe_path(config.TMP_ZIP_DIR, path) and ".zip" not in path


def user_is_present_in_ram(user_key):
    """Is this user still in ANY of the bot's live channel lists (synced from 353/JOIN)?"""
    u = str(user_key).lower()
    with runtime.channel_users_lock():
        for users_set in getattr(config, 'channel_users', {}).values():
            for known_user in users_set:
                if str(known_user).lower() == u:
                    return True
    return False


def channel_containing_user(user_key):
    """WHICH of our own channels `user_key` is in right now, or None if none
    of them - the answer user_is_present_in_ram() above deliberately throws
    away by collapsing it to a bool.

    Reported live: a fetch for a bot only in one of several configured
    channels was sent into a different one instead - dcc_fetch.
    check_fetch_queue() dispatched every request into one fixed channel
    (BROADCAST_SEARCH_CHANNEL, or else config.CHANNEL's first entry)
    regardless of where the target bot actually was, so the bot never saw it
    and every request failed with "no response". Not a guess about who
    would answer - the same observation bot_not_here_error() already makes
    at enqueue time (webserver.py), just never carried through to the
    PRIVMSG dispatch actually sends.

    config.channel_users is keyed and valued in lower case for matching (see
    irc.py's 353/JOIN handlers); this returns the channel in the CASE THE
    OPERATOR CONFIGURED, which is what an outbound PRIVMSG should use, by
    matching against irc.configured_channels() rather than the mirror's own
    keys. Checked in that configured order, so a bot present in more than
    one of our channels gets a stable, predictable answer rather than
    whichever channel's 353 happened to arrive first.

    Deferred import: irc.py imports THIS module at load time, so reaching
    for it at the top would close a cycle. The same shape as
    announce_channel_for()'s own deferred `import irc`, earlier in this file.
    """
    import irc
    u = str(user_key).lower()
    with runtime.channel_users_lock():
        users_by_chan = {chan: set(users) for chan, users in
                         (getattr(config, "channel_users", {}) or {}).items()}
    for chan in irc.configured_channels():
        if u in users_by_chan.get(chan.lower(), ()):
            return chan
    return None


def discard_orphaned_temp_archives(user_key):
    """Delete the temp .rar files that only `user_key`'s queue rows still name.

    MUST be called with queue_lock held and BEFORE the rows are dropped: those
    rows are the only record that the archives exist, so once they are gone the
    files sit in TMP_ZIP_DIR forever.

    Four paths delete a whole queue - the freeze-timeout sweep, the per-user
    freeze timer, !clearqueue, and the user's own @<nick>-remove / CTCP REMOVE.
    Only the first three cleaned up; REMOVE, the one users actually type, did
    not. This is that cleanup, in one place.

    Two rows are deliberately skipped:

    * is_unpacked_rar_folder rows, whose "path" is the source album directory in
      the music library, not a temp file. Deleting that would delete the music.
    * archives another queue still points at, or that a transfer is streaming
      right now. Archive names come from the FOLDER, not the user
      (dcc.py builds "{clean_folder_name}.rar"), so two people who queued the
      same album share one file on disk.

    Returns the paths actually removed, for the caller to log.
    """
    removed = []
    queue = getattr(config, 'dcc_queue', {})
    if user_key not in queue:
        return removed

    for f_obj in queue[user_key]:
        if not isinstance(f_obj, dict):
            continue
        if f_obj.get('is_temporary_zip') is not True or f_obj.get('is_unpacked_rar_folder'):
            continue
        temp_path = f_obj.get('path')
        if not temp_path or not os.path.exists(temp_path):
            continue

        still_needed = any(
            isinstance(other, dict) and other.get('path') == temp_path
            for other_key, files in queue.items() if other_key != user_key
            for other in files
        )
        if not still_needed:
            still_needed = any(tx.get('file') == f_obj.get('file')
                               for tx in getattr(config, 'active_transfers', []))
        if still_needed:
            continue

        try:
            os.remove(temp_path)
            removed.append(temp_path)
        except OSError as rm_err:
            print(f"[TEMP CLEANUP] Could not remove {temp_path}: {rm_err}")

    return removed


def _report_transfer_failure(user, file_name, reason, acked=None, total=None, channel=None):
    """Say a transfer failed everywhere a completed one is said to succeed.

    The console log line, AND the debug channel / admin console through
    send_debug() with the FAIL category - the same route
    announce.send_transfer_complete() takes for "Sent:". Before #526 the
    failures were print() only, so the two places an operator actually
    watches showed every success and no failure.
    """
    print(f"[DCC-FAIL] {file_name} for {user}: {reason}")
    try:
        # acked/total feed the structured FAIL event (#550) when the caller
        # knows them; the prose carries them in words either way.
        announce.feed_event("FAIL", f"Failed: \"{file_name}\" to {user} - {reason}",
                            nick=user, channel=channel, acked=acked, total=total,
                            name=file_name, reason=reason)
    except Exception as debug_err:
        print(f"[DEBUG-FAIL ERROR] Could not report the failed transfer: {debug_err}")


def accept_timeout():
    """Seconds the listener waits for the receiver after the offer (#879).

    DCC_ACCEPT_TIMEOUT, floored at one second so a zero or a typo cannot
    make every offer fail the instant it is made.
    """
    try:
        seconds = float(getattr(config, "DCC_ACCEPT_TIMEOUT", 30))
    except (TypeError, ValueError):
        seconds = 30.0
    return max(1.0, seconds)


def never_connected_advice():
    """What the person downloading can actually do about it.

    The operator's line says the receiver never connected (#879). The
    person on the other end was told only "transfer did not complete",
    which names no cause and suggests no action - and the two things that
    cause this are both theirs to fix: a DCC prompt nobody answered, and a
    client set to ignore the file's type. Reported live by an operator
    whose user could take a .jpg and never a .nfo and wrote "it says
    active transfer started then gives error".
    """
    return ("your client never accepted it. Look for a DCC prompt and accept "
            "it, and check your client is not set to ignore this kind of file")


def never_connected_reason(seconds):
    """What a failed accept() is: nobody came, not a link that stopped.

    Before #879 the accept() timeout fell into the send loop's own
    `except socket.timeout` and was reported as "the send blocked for the
    whole socket timeout with the receiver not draining it (0B of 0B)" -
    which sends an operator looking for a network fault when the receiver
    simply never clicked Accept, or their client's DCC filter dropped the
    file type.
    """
    return (f"the receiver never connected within {seconds:.0f}s - the offer "
            f"was not accepted, or their client ignored it.")


class _ReceiverGone(Exception):
    """The peer closed the data connection with bytes still unacknowledged."""

    def __init__(self, acked):
        super().__init__(acked)
        self.acked = acked


class _ReceiverStalled(Exception):
    """No ack progress for ACK_STALL_SECONDS with bytes still outstanding."""

    def __init__(self, acked):
        super().__init__(acked)
        self.acked = acked


class _ShortSend(Exception):
    """Raised to skip the completion bookkeeping when a send ended early.

    Not an error condition - the caller already printed [DCC-FAIL] and the
    queue row is settled by release_queue_entry() either way. This exists so
    the skip is one branch rather than a condition repeated around every
    statement in the block, and so it cannot be swallowed by the broad
    `except Exception` that guards the database writes: a deliberate skip
    reported as "[DB ERROR] Could not increment the sharing statistics" would
    send the next reader looking for a database fault that never happened.
    """


def release_queue_entry(user, next_file, delivered, reason=""):
    """Settle the queue row for a finished attempt. Returns True if the row was kept.

    Removal is by IDENTITY, never by position. start_dcc_send's finally used to do
    config.dcc_queue[u_key].pop(0), which removes whatever is first at that instant rather
    than the entry actually sent. Two ways that lost files:

      * The direct-send fast path builds a synthetic next_file never inserted into the
        queue, so the row at position 0 on completion is by construction a DIFFERENT,
        unsent file. It was deleted anyway.
      * The queue can be appended to or promoted while a transfer runs.

    Identity removal fixes both and needs no special case: a synthetic entry is not in the
    list, so nothing is removed and the user's real queue is left intact.

    A failed attempt no longer consumes the row, but it must not retry forever, so the
    attempt count lives ON THE ROW as 'send_fails' and is capped at config.MAX_SEND_FAILS.
    Keeping it on the row rather than in a side dict means its lifetime is exactly the
    row's: it cannot leak, cannot collide between users or same-named files, and cannot be
    inherited by a later request for the same filename.

    Two kinds of row are deliberately NOT retryable, because retrying them can only fail:
      * a temporary archive that is GONE from disk - every retry would abort immediately
        on file_size == 0 and emit a misleading error. While the archive is still there
        a packed row is as retryable as a plain file (#657, audit M55): the cleanup used
        to delete the .rar first and this function then found it "consumed" - a circular
        reason, and a 3 GB album that took ten minutes to pack got exactly one 30-second
        accept window before the user had to !rar it again. The finally now settles the
        row BEFORE the cleanup and keeps the archive for a row it kept;
      * a legacy non-dict row, which has nowhere to store a counter.
    Both are settled on their first failure.
    """
    import defaults as config
    import db

    u_key = str(user).lower()

    def _put_rows(key, kept):
        # An emptied list leaves with its key, HERE, under queue_lock (#606).
        # db.save_dcc_queue() used to pop it instead - after this function's
        # lock block had closed - which raced next_waiting_pack_owner()'s and
        # the temp-archive cleanup's live `config.dcc_queue.items()` walks
        # and raised in their thread, out of start_dcc_send's finally.
        if kept:
            config.dcc_queue[key] = kept
        else:
            config.dcc_queue.pop(key, None)

    def _remove_by_identity():
        # THE NICK MAY HAVE MOVED WHILE THE FILE WAS SENDING (#455).
        #
        # `user` is whoever the send STARTED as. A transfer takes minutes, and
        # irc.note_nick_change() carries dcc_queue to the new nick's key the
        # moment the server says so - so by the time the row is settled, the
        # queue can be filed under a key this function has never heard of.
        # The keyed lookup then finds nothing, the delivered row is never
        # removed, and the same file is handed out again on the next trigger.
        #
        # The row object is the identity, not the key it happens to sit under.
        # Try the key first because it is right almost always and costs one
        # lookup; fall back to scanning for the object itself, which is what
        # "remove THIS row" actually means.
        rows = config.dcc_queue.get(u_key)
        if rows:
            kept = [row for row in rows if row is not next_file]
            removed = len(rows) - len(kept)
            if removed:
                _put_rows(u_key, kept)
                return removed

        # dict() first: the scan walks every queue, and another thread
        # adding or removing a user mid-walk would otherwise raise - the same
        # reason #432 and #452 take a copy rather than the lock, which cannot
        # be taken here either.
        for other_key, other_rows in dict(config.dcc_queue).items():
            if not other_rows:
                continue
            kept = [row for row in other_rows if row is not next_file]
            removed = len(other_rows) - len(kept)
            if removed:
                _put_rows(other_key, kept)
                if other_key != u_key:
                    print(f"[DCC QUEUE] Settled {user}'s row under {other_key!r} "
                          f"- they renamed while it was sending.")
                return removed
        return 0

    is_row = isinstance(next_file, dict)
    packed = bool(is_row and next_file.get("is_temporary_zip")
                  and not next_file.get("is_unpacked_rar_folder"))
    # Consumed means the archive is not there to send again (#657) - not
    # merely that this was an archive.
    consumed_temp = packed and not os.path.exists(
        platform_compat.long_path(str(next_file.get("path") or "")))
    # A row that is not in any queue is the direct-send fast path's synthetic
    # one (see above): nothing will ever pick it up again, so "kept for retry"
    # was a claim about a retry that could not happen - and, being "kept", it
    # sent the user nothing, so the most common request (a free slot, no
    # queue) got one attempt and silence when it failed (#599).
    in_a_queue = False
    if is_row:
        with queue_lock:
            in_a_queue = any(row is next_file
                             for rows in list(config.dcc_queue.values()) if rows
                             for row in rows)
    retryable = is_row and in_a_queue and not consumed_temp

    retained = False
    gave_up = False
    budget = getattr(config, "MAX_SEND_FAILS", 3)

    with queue_lock:
        if delivered:
            removed = _remove_by_identity()
            outcome = "delivered, " + str(removed) + " row(s) removed"
        elif not retryable:
            removed = _remove_by_identity()
            if consumed_temp:
                why = "temporary archive is gone from disk"
            elif is_row and not in_a_queue:
                why = "sent directly, not queued - nothing to retry"
            else:
                why = "row is not retryable"
            outcome = "failed (" + why + "), " + str(removed) + " row(s) removed"
        else:
            attempts = int(next_file.get("send_fails", 0)) + 1
            next_file["send_fails"] = attempts
            if attempts >= budget:
                removed = _remove_by_identity()
                gave_up = True
                outcome = "failed " + str(attempts) + "/" + str(budget) + " - giving up, " + str(removed) + " row(s) removed"
            else:
                retained = True
                outcome = "failed " + str(attempts) + "/" + str(budget) + " - kept for retry"

    try:
        db.save_dcc_queue()
    except Exception as save_err:
        print("[DCC QUEUE ERROR] Could not persist the queue: " + str(save_err))

    if gave_up or (not delivered and not retained):
        # Tell the user their file was dropped. Silently discarding it is how the old
        # positional pop hid this class of failure in the first place.
        try:
            oserve_mod = sys.modules.get("oserve")
            dropped = next_file.get("file", "your file") if is_row else str(next_file)
            # "Removed from your queue" is only true of a row that was in one.
            tail = "Removed from your queue." if (in_a_queue or not is_row) else "Ask for it again when you are ready."
            # Through fit_irc_line like the other notices that carry a
            # filename (#162 finding #31): the name comes off the operator's
            # own disk and a long one - plus a reason that is now a sentence
            # of advice - pushes this past 512 bytes, where the server's cut
            # lands mid-colour-code and the reader loses the tail that says
            # what to do.
            def _build(shown_name):
                return ("NOTICE " + str(user) + " :" + config.C_BOLD + "Error" + config.C_RESET +
                        ": Could not send " + shown_name + " - " + str(reason) + ". " + tail + "\r\n")

            if oserve_mod:
                import announce as announce_mod
                oserve_mod.queue_message(user, announce_mod.fit_irc_line(_build, str(dropped)))
        except Exception as notify_err:
            print("[DCC QUEUE] Could not notify " + str(user) + ": " + str(notify_err))

    print("[DCC QUEUE] " + str(user) + ": " + outcome + ((" (" + reason + ")") if reason else "") + ".")
    return retained


def get_total_queued_count():
    """The total number of files sitting in every personal queue right now.

    #432: iterates a SNAPSHOT of the values, not the live dict. Every writer
    that adds or removes a key does so under `queue_lock` (dcc.py's own
    request/transfer path, commands.py, db.py), but this reader took none -
    a key added or removed at the exact microsecond this loop was mid-scan
    raised "dictionary changed size during iteration" and escaped all the way
    up through announce_worker(), aborting the whole advert cycle for every
    channel not yet reached.

    Taking queue_lock HERE would be worse, not better: dcc.py's own two
    request-path call sites call this function while already holding that
    lock, and queue_lock is a plain threading.Lock - not reentrant - so
    locking inside would deadlock every file and pack request. list() over
    the dict's values is what makes this reader safe without needing the
    lock at all: it copies the reference list under the GIL in one step, so
    a concurrent add or remove during the copy can only leave this total off
    by the one entry racing it, never raise.
    """
    total = 0
    for files in list(config.dcc_queue.values()):
        total += len(files)
    return total

def get_public_ip_long():
    """Convert config.MY_IP_OR_DOCK into the mIRC-compatible long format, or 0
    if it is blank or not a dotted-quad at all.

    Deliberately a pure converter, and it stays one. An earlier version of this
    fix folded the "is this address any use to a remote client?" question in
    here, which broke the admin console: adminchat.py's DCC CHAT listen-back
    uses the same value, and an operator connecting from the same machine or
    the same LAN has a loopback or private address that is entirely correct for
    that purpose. Only the file-transfer path needs the stricter rule, so the
    stricter rule lives there - see is_offerable_to_strangers() below.
    """
    text = str(getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    if not text:
        return 0
    try:
        parts = text.split('.')
        if len(parts) == 4:
            return (int(parts[0]) << 24) + (int(parts[1]) << 16) + (int(parts[2]) << 8) + int(parts[3])
    except Exception as e:
        print(f"[DCC IP ERROR] Could not convert the IP to long form: {e}")
    return 0


def is_offerable_to_strangers(ip_text=None):
    """Can a stranger on a public IRC network actually dial this address?

    #162 follow-up, found by the pre-publication audit. irc.py's connect used to
    fall back to "127.0.0.1" whenever the ipify lookup failed, and the only
    guard on the send path was `if ip_long == 0`. 127.0.0.1 converts to
    2130706433, which is not 0, so nothing caught it: the bot accepted every
    request, told each user "Active Transfer Started", held a DCC slot, and sent
    every leecher to their own loopback. One warning at boot was the only clue,
    and the queue drained into failed transfers.

    Loopback, private, link-local, multicast, reserved and unspecified are all
    unreachable from the far side of a public network, so an offer carrying one
    is an offer to nobody. Refusing with a message the operator can act on beats
    a transfer that silently never happens.

    An operator serving a LAN pins MY_IP_OR_DOCK, and irc.py now uses a pinned
    value verbatim without the lookup - but the offer still has to be dialable
    by whoever receives it, so this applies either way.
    """
    import ipaddress

    text = str(ip_text if ip_text is not None
               else getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    if not text:
        return False
    try:
        address = ipaddress.IPv4Address(text)
    except Exception:
        return False
    return not (address.is_loopback or address.is_private or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified)


def next_waiting_pack_owner(exclude_user=None):
    """The user whose queue HEAD is a folder pack, in arrival order, or None.

    check_queue_and_send() only ever looks at the queue of the user handed to
    it, and every caller hands it the user whose transfer just finished. A
    second user turned away at the [RAR-HOLD] branch - because someone else's
    pack held config.rar_inprogress - was therefore never revisited: their row
    stayed queued until they happened to complete some unrelated transfer of
    their own, which on a bot serving one album at a time may be never.

    Arrival order is dict insertion order on config.dcc_queue. Queue entries
    carry no timestamp, and insertion order is what the rest of the queue
    already treats as arrival order, so this matches rather than invents.

    HEAD only, not "anywhere in their queue": check_queue_and_send() dispatches
    entries[0] and nothing else, so a pack sitting behind a plain file is not
    dispatchable yet and waking that user would be a no-op that looks like a
    fix.
    """
    exclude = (exclude_user or "").lower()
    with queue_lock:
        for user_key, entries in config.dcc_queue.items():
            if user_key == exclude or not entries:
                continue
            head = entries[0]
            if isinstance(head, dict) and head.get('is_unpacked_rar_folder') is True:
                return head.get('user_raw') or user_key
    return None


def redispatch_waiting_pack(irc_sock, just_finished=None):
    """Wake the next queued folder pack, once rar_inprogress has been released.

    Called from every path that clears rar_inprogress. Returns the user woken,
    or None - which the tests assert on, and which is also the honest answer
    when there was nothing to wake.

    Re-checks rar_inprogress first: these callers clear it a line or two
    earlier, but a concurrent trigger may already have claimed it, and starting
    a second packer would defeat the interlock this whole branch exists to
    hold.

    Dispatched on a thread rather than called inline. check_queue_and_send()
    takes queue_lock, and threading.Lock is not reentrant - the scan above
    releases it before returning, but the send path this leads into is long
    (it packs an album), and running it inline would also make one completion
    recurse into the next.
    """
    if getattr(config, 'rar_inprogress', False):
        return None
    owner = next_waiting_pack_owner(exclude_user=just_finished)
    if not owner:
        return None
    print(f"[RAR-WAKE] A pack for {owner} was waiting on the packer lock; dispatching it now.")
    threading.Thread(target=check_queue_and_send, args=(irc_sock, owner),
                     daemon=True).start()
    return owner

FREEZE_TIMEOUT = 300.0   # seconds an absent user's queue is kept, counted only while the bot is online


def pause_freeze_clock(now=None):
    """The bot's link is gone: stop the freeze box's clock (#652, audit M50).

    Called from the disconnect epilogue. Idempotent - a second call while
    already paused keeps the earlier moment, which is the one the outage
    started at.
    """
    if runtime.freeze_clock_paused_at is None:
        runtime.freeze_clock_paused_at = time.time() if now is None else now


def resume_freeze_clock(now=None, log=print):
    """The bot is channel-synced again: move every frozen timestamp forward by
    the outage, so the seconds it was away count for nobody. Returns the
    seconds skipped, 0.0 if the clock was not paused.

    ONE CLOCK. The per-user timer thread already refused to count the bot's
    own downtime ("the bot's own downtime must NEVER count against a user's
    queue"), while the sweep in check_queue_and_send() compared the frozen
    timestamp with wall time - so as soon as bot_joined_channel came back, the
    sweep deleted queues the timer said had sixty seconds on them. Rebasing
    the timestamp makes the sweep, the timer and the console's "seconds left"
    read the same figure: time the bot has been ONLINE since the freeze.
    """
    paused_at = runtime.freeze_clock_paused_at
    if paused_at is None:
        return 0.0
    runtime.freeze_clock_paused_at = None
    skipped = max(0.0, (time.time() if now is None else now) - paused_at)
    frozen = getattr(config, "frozen_queues", None)
    if skipped and isinstance(frozen, dict) and frozen:
        with queue_lock:
            for key in list(frozen):
                try:
                    frozen[key] = float(frozen[key]) + skipped
                except (TypeError, ValueError):
                    pass
        log(f"[DCC FREEZE] The bot was away {int(skipped)}s; that time counts "
            f"against none of the {len(frozen)} frozen queue(s).")
    return skipped


def frozen_users_channel_is_synced(user_key):
    """Can the bot see the channel this user's queue belongs to?

    The freeze means "not in any channel we share", and that is only an
    observation once the channel's NAMES has arrived on this connection -
    channel_users is cleared on every disconnect and rebuilt per channel.
    Until then the bot is not in a position to say the user is gone, so
    the deletion waits (#652). A queue with no channel on its rows is
    judged as before.
    """
    rows = getattr(config, "dcc_queue", {}).get(user_key) or []
    chan = announce_channel_for(rows[0]) if rows else None
    if not chan:
        return True
    with runtime.channel_users_lock():
        return str(chan).lower() in getattr(config, "channel_users", {})


def freeze_absent_user(irc_sock, user, target_chan):
    """Start the five-minute countdown for a queued user who is not in any
    of our channels. Idempotent: a user already counting down is left alone,
    and nothing is frozen while the bot itself is not channel-synced.

    Lifted out of check_queue_and_send()'s specific-user branch so the global
    sweep (section B) can apply the SAME policy. Until #530 the sweep could
    not: it `continue`d past an absent user without a word, so a queue that
    only the sweep ever looked at was never frozen, never expired, and was
    never retried - 65 rows sat QUEUED for days on a live bot while other
    users were served around them.
    """
    import announce as announce_mod
    import threading
    import time
    import defaults as config
    import db

    user_key = str(user).lower()

    # NEVER freeze a queue while the bot itself is off the network.
    # On a netsplit or reconnect channel_users is empty or half-synced, so we do
    # not KNOW whether the user left. Leave the queue alone until NAMES has synced.
    if not getattr(config, 'bot_joined_channel', False) or not getattr(config, 'channel_users', None):
        print(f"[DCC FREEZE-SKIP] The bot is not channel-synced yet. Leaving {user}'s queue untouched.")
        return

    # A user may have exactly ONE countdown running at a time.
    with queue_lock:
        if user_key in getattr(config, 'frozen_queues', {}):
            print(f"[DCC FREEZE-HOLD] {user} already has a countdown running. Not starting another.")
            return
        config.frozen_queues[user_key] = time.time()
    print(f"[DCC REACTIVE FREEZE] {user} really has left {target_chan}. Starting the timer...")
    announce_mod.send_debug(f"DCC reactive freeze triggered for {user} in {target_chan}. Initiating 5-minute cooldown timer.", category="QUIT")

    def user_queue_timer(sock, target_user, original_chan):
        """A verifying countdown, replacing the old blind 300-second sleep.
        The clock pauses entirely while the bot is disconnected - the bot's own
        downtime must NEVER count against a user's queue - and the countdown
        aborts as soon as the user reappears via JOIN or a NAMES sync."""
        t_key = target_user.lower()
        elapsed = 0

        # ONE CLOCK (#652): `elapsed` is read from the frozen timestamp, the
        # same figure the sweep in check_queue_and_send() and the console's
        # seconds-left use - not counted here on its own. The bot's outage
        # is taken out of that timestamp by resume_freeze_clock() when the
        # link is back, so the pause below is what it always was, and the
        # sweep can no longer delete what this countdown says has a minute
        # left.
        while elapsed < FREEZE_TIMEOUT:
            time.sleep(10)

            # A) Something else already thawed the queue (JOIN / NAMES / !rehash)
            if t_key not in getattr(config, 'frozen_queues', {}):
                print(f"[DCC FREEZE-ABORT] {target_user} is already thawed. The countdown stops; the queue is safe.")
                return

            # B) The bot itself is offline - freeze the clock, do NOT advance elapsed
            if not getattr(config, 'bot_joined_channel', False):
                print(f"[DCC FREEZE-PAUSE] The bot is off the network. Pausing {target_user}'s countdown at {elapsed}s.")
                continue

            # C) The bot is back online - check against the fresh channel list
            if user_is_present_in_ram(t_key):
                with queue_lock:
                    config.frozen_queues.pop(t_key, None)
                print(f"[DCC FREEZE-ABORT] {target_user} was found in the channel list. The queue is kept and woken.")
                announce_mod.send_debug(f"Queue for {config.C_BOLD}{target_user}{config.C_RESET} preserved - user verified back in channel before timeout.", category="JOIN")
                threading.Thread(target=check_queue_and_send, args=(sock, target_user), daemon=True).start()
                return

            # D) The bot is up but has no member list for this user's channel
            # yet (its NAMES has not arrived, or the rejoin was refused): it
            # cannot say the user is gone. Wait, without counting.
            if not frozen_users_channel_is_synced(t_key):
                continue

            try:
                elapsed = time.time() - float(config.frozen_queues.get(t_key) or time.time())
            except (TypeError, ValueError):
                elapsed += 10

        # THE FREEZE IS TESTED AND TAKEN UNDER THE LOCK, IN ONE MOVE (#659,
        # audit M57). This used to test `t_key in frozen_queues` outside
        # queue_lock and then, inside it, delete the queue and `del` the key
        # without looking again. The JOIN thaw and the sweep thaw both remove
        # that key; one landing in the gap meant the timer erased the queue
        # of a user who was verifiably back and then died on the KeyError -
        # the freezer destroying the queue it exists to preserve, and no
        # "Timer expired" line to say so. pop() under the lock answers
        # "was it still frozen" and takes it in the same step; only a real
        # answer erases anything.
        still_frozen = False
        with queue_lock:
            frozen = getattr(config, 'frozen_queues', None)
            if isinstance(frozen, dict):
                still_frozen = frozen.pop(t_key, None) is not None
            if still_frozen and t_key in config.dcc_queue:
                for f_obj in config.dcc_queue[t_key]:
                    if isinstance(f_obj, dict) and f_obj.get('is_temporary_zip') is True and os.path.exists(f_obj['path']) and not f_obj.get('is_unpacked_rar_folder'):
                        try: os.remove(f_obj['path'])
                        except: pass
                del config.dcc_queue[t_key]
                db.save_dcc_queue()
        if still_frozen:
            announce_mod.send_debug(f"Timer expired for {target_user} in {original_chan}. Personal queue has been erased.", category="PART")
        else:
            print(f"[DCC FREEZE-ABORT] {target_user} was thawed as the countdown ended. The queue is safe.")

    threading.Thread(target=user_queue_timer, args=(irc_sock, user, target_chan), daemon=True).start()


def wake_restored_queues(irc_sock):
    """One look at every queue once the bot is channel-synced.

    A queue restored from dcc_queue.txt at start-up has no trigger of its own:
    the request that created it fired years ago in process terms, a JOIN only
    wakes users who are FROZEN (frozen_queues is in-memory and empty after a
    restart), and the global sweep otherwise runs only when some OTHER
    transfer completes. On a quiet bot that is never. So the sweep is run
    once here, on activation - once per slot, because a single pass dispatches
    at most one user and then breaks.
    """
    import defaults as config
    slots = max(1, int(config.MAX_DCC_SLOTS or 1))
    for _ in range(slots):
        check_queue_and_send(irc_sock, "system_next_trigger_fallback")


def check_queue_and_send(irc_sock, completed_user):
    """Check the queues and run RAR packing one at a time, without flooding the server."""
    import announce as announce_mod
    import subprocess
    import threading
    import socket
    import sys
    import os
    import re
    import time
    import defaults as config
    import db
    
    user_key = completed_user.lower()
    oserve = sys.modules.get('oserve')

    # 0. THE QUIESCE GATE.
    #
    # wait_for_transfers_to_finish() sets config.transfers_paused and then
    # waits for config.active_transfers to empty, and its own log line
    # promises "No new sends will start". Until this check existed that was
    # not true: the flag had exactly ONE reader, in handle_download_request(),
    # which turns away a NEW request from a user. Nothing stopped THIS
    # function - the dispatcher that actually claims a slot and starts a send
    # - from promoting the rows already queued.
    #
    # So the wait could not converge on a busy bot. Every completing transfer
    # re-arms delayed_queue_trigger_fallback (see start_dcc_send's finally),
    # that fallback calls straight back into here, and the freed slot is
    # refilled inside the very wait that was supposed to be draining it.
    # active_transfers never empties, the wait burns REHASH_TRANSFER_WAIT
    # (120s by default) refusing every user request with "the bot is
    # reloading", and then reloads under live transfers anyway - which is the
    # exact outcome #310 added the quiesce to prevent.
    #
    # The rehash's own wake path already assumes this gate is here: it calls
    # resume_transfers() BEFORE waking the queue, commented "waking it while
    # still paused would have every dispatch refused by the gate the wait put
    # up". That gate is this one.
    #
    # Checked before the freeze sweep runs, not after: the sweep deletes queue
    # rows for users gone over five minutes, and a rehash is not a reason to
    # start throwing away queues.
    if transfers_are_paused():
        return

    # 1. Sweep away frozen queues older than five minutes
    # The sweep may ONLY run once the bot itself is fully channel-synced.
    # During a reconnect channel_users is empty, and the old sweep then deleted queues
    # belonging to users who had never left the channel.
    if getattr(config, 'bot_joined_channel', False):
        with queue_lock:
            current_time = time.time()
            for f_user, freeze_timestamp in list(config.frozen_queues.items()):
                # THAW: the user is back in memory - release the freeze instead of deleting
                if user_is_present_in_ram(f_user):
                    del config.frozen_queues[f_user]
                    print(f"[DCC FREEZE-THAW] {f_user} is back in the channel list. Their queue was saved.")
                    continue
                # The same clock as the timer thread (#652): the timestamp
                # has the bot's own downtime taken out of it, and a channel
                # the bot has no member list for yet is not evidence of
                # absence - see frozen_users_channel_is_synced().
                if (current_time - freeze_timestamp) > FREEZE_TIMEOUT and frozen_users_channel_is_synced(f_user):
                    if f_user in config.dcc_queue:
                        for f_obj in config.dcc_queue[f_user]:
                            if isinstance(f_obj, dict) and f_obj.get('is_temporary_zip') is True and os.path.exists(f_obj['path']) and not f_obj.get('is_unpacked_rar_folder'):
                                try: os.remove(f_obj['path'])
                                except: pass
                        del config.dcc_queue[f_user]
                        db.save_dcc_queue()
                    if f_user in config.frozen_queues:
                        del config.frozen_queues[f_user]
                    print(f"[DCC QUEUE_CLEAN] {f_user} was frozen for over "
                          f"five minutes and never came back. Queue dropped.")

    if user_key == "system_next_trigger_fallback":
        user_key = ""

    next_file = None
    with queue_lock:
        if user_key and user_key in config.dcc_queue and config.dcc_queue[user_key]:
            if user_key not in config.frozen_queues:
                next_file = config.dcc_queue[user_key][0]  # FIXED: takes the top entry


    if next_file:
        target_chan = announce_channel_for(next_file)
        
        user_is_actively_in_channel = False
        
        # Case-folding and system-trigger bypass:
        # if completed_user is the system trigger, OR the user was just rehashed, 
        # open the gate fully, to clear any silent case-sensitivity blocks.
        if "system_next_trigger_fallback" in [str(completed_user).lower(), str(user_key)]:
            user_is_actively_in_channel = True
        else:
            with runtime.channel_users_lock():
                if hasattr(config, 'channel_users'):
                    for chan_name, users_set in config.channel_users.items():
                        lowered_channel_users = [u.lower() for u in users_set]
                        if user_key in lowered_channel_users or str(completed_user).lower() in lowered_channel_users:
                            user_is_actively_in_channel = True
                            break
            
        if user_is_actively_in_channel is True:
            # ---------------------------------------------------------------------
            # The folder packer runs strictly one at a time, inside the send gate
            # ---------------------------------------------------------------------
            if isinstance(next_file, dict) and next_file.get('is_unpacked_rar_folder') is True:
                # FIXED (issue #27, RAR branch): this check-then-set had the same gap as
                # section A's plain-file dispatch before that fix - the user_processing_lock
                # check and the rar_inprogress check both ran, then BOTH interlocks got set,
                # all outside queue_lock. Two overlapping triggers could each read both flags
                # as clear before either claimed them, both start packing/sending the same
                # folder for the same user. Mirror the plain-file fix: check and claim both
                # interlocks atomically under queue_lock.
                with queue_lock:
                    # #162 finding #23: this branch had no capacity check at
                    # all, unlike the plain-file branch a few lines below,
                    # which already re-checks capacity inside this same
                    # lock. rar_inprogress bounds concurrent PACKS to one,
                    # but a pack's own SEND afterwards is a normal DCC slot
                    # like any other - with no check here, that one send
                    # could still push active_transfers one past
                    # MAX_DCC_SLOTS. Same check, same message, as the sibling
                    # plain-file branch below already has.
                    if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                        print(f"[DCC-BLOCK] {completed_user}: all {config.MAX_DCC_SLOTS} slot(s) busy, leaving queued for the next trigger.")
                        return

                    user_already_locked = (
                        hasattr(config, 'user_processing_lock')
                        and completed_user.lower() in config.user_processing_lock
                    )
                    pack_in_progress = getattr(config, 'rar_inprogress', False)

                    if user_already_locked:
                        print(f"[RAR-BLOCK] {completed_user} is already locked in memory; blocking a stale thread.")
                        return
                    if pack_in_progress:
                        print(f"[RAR-HOLD] {completed_user} waits in the queue while another packing run is in progress...")
                        return

                    config.rar_inprogress = True
                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(completed_user.lower())

                def inline_rar_packer(sock):
                    # This runs with config.rar_inprogress already True and the user held in
                    # config.user_processing_lock. Both are PROCESS-WIDE interlocks, and
                    # nothing here released them on an unexpected failure - subprocess.run
                    # raising (rar missing, disk full, timeout), os.makedirs failing, or
                    # getsize on a vanished archive would all leave rar_inprogress latched
                    # True, silently disabling folder packing for every user until the
                    # daemon was restarted. Not even !rehash cleared it.
                    # handed_off: the body returns True once it has transferred ownership of
                    # config.rar_inprogress / user_processing_lock to another thread. Releasing
                    # them here in that case would un-serialise packing from sending - the very
                    # thing the interlocks exist to guarantee - so the finally only releases
                    # what this call still owns.
                    handed_off = False
                    runtime.packer_thread = threading.current_thread()
                    try:
                        handed_off = _inline_rar_packer_body(sock)
                    except Exception as packer_err:
                        print("[LINJAR RAR ERROR] Packing failed for " + str(completed_user) + ": " + str(packer_err))
                        try:
                            announce_mod.send_pack_error_notice(sock, completed_user)
                        except Exception:
                            pass
                        release_queue_entry(completed_user, next_file, delivered=False,
                                            reason="pack failed: " + str(packer_err))
                    finally:
                        # The pack itself is over either way (#651): what is
                        # handed off is the SEND, which active_transfers
                        # already counts.
                        if runtime.packer_thread is threading.current_thread():
                            runtime.packer_thread = None
                        if not handed_off:
                            config.rar_inprogress = False
                            # #215: this release is the only moment another user's held pack can
                            # start. Nothing else revisits them - every check_queue_and_send()
                            # caller passes the user who just finished, never the one turned
                            # away at [RAR-HOLD].
                            redispatch_waiting_pack(irc_sock, just_finished=completed_user)
                            if hasattr(config, 'user_processing_lock'):
                                config.user_processing_lock.discard(completed_user.lower())

                def _inline_rar_packer_body(sock):
                    true_source_dir = next_file['path']

                    # SECOND LINE OF DEFENCE: queue entries survive restarts via dcc_queue.txt,
                    # so a poisoned row queued BEFORE the traversal guard existed would otherwise
                    # still be packed here. Re-verify the path immediately before calling rar.
                    #
                    # Checked against each configured folder rather than one
                    # global (#164). Unlike the request path above, this has a
                    # real path from the queue and no heading to resolve, so
                    # "which folder does this belong to" IS the question - a
                    # queued row can legitimately come from any of them. Still
                    # is_safe_path() per folder, so each comparison resolves
                    # symlinks and compares per separator exactly as before;
                    # what changed is how many roots are legitimate, not how
                    # any one of them is tested.
                    #
                    # EVERY list's folders (#26). The request path resolved
                    # this row against the folders of the list bound to the
                    # channel it arrived in - see resolve_list_folder_with_root
                    # (wanted_list) below - so a row from any list but the
                    # primary is legitimate here. folders() with no name means
                    # the PRIMARY's, which made this check disagree with the
                    # one that admitted the row: a !rar accepted in a channel
                    # bound to a second list was destroyed here minutes later,
                    # logged as a poisoned queue entry, and the user's queue
                    # row deleted with it.
                    if not path_is_in_our_library(true_source_dir):
                        print(f"[SECURITY] Blocked a poisoned queue entry for {completed_user}: {true_source_dir}")
                        with queue_lock:
                            if completed_user.lower() in config.dcc_queue:
                                config.dcc_queue[completed_user.lower()] = [
                                    e for e in config.dcc_queue[completed_user.lower()] if e is not next_file
                                ]
                                # The save no longer prunes an emptied key (#606).
                                if not config.dcc_queue[completed_user.lower()]:
                                    del config.dcc_queue[completed_user.lower()]
                        db.save_dcc_queue()
                        # The interlocks are released ONCE, by the wrapper's
                        # finally (#714, audit L50): this exit used to clear
                        # rar_inprogress, wake the next waiting pack and drop
                        # the lock itself, and then return None - on which the
                        # finally did all three again. The second, unconditional
                        # `rar_inprogress = False` could clear a claim the first
                        # wake had just handed to another user's pack, leaving
                        # two rar processes on one archive path.
                        announce_mod.send_debug(
                            f"Poisoned queue entry discarded for {config.C_BOLD}{completed_user}{config.C_RESET}: path outside the music root.",
                            category="HARDBAN")
                        return

                    # The DCC-visible name: recomputed fresh from the folder
                    # leaf on disk, with the SAME sanitiser
                    # handle_download_request() used when this row was
                    # queued (see _sanitize_rar_leaf_name()'s own docstring).
                    # One shared function is what makes the apostrophe-
                    # recovery special case this replaced unnecessary - the
                    # two call sites can no longer disagree in the first
                    # place, so there is nothing left to detect and patch
                    # over here.
                    folder_leaf = os.path.basename(true_source_dir.rstrip('/\\'))
                    rar_filename = f"{_sanitize_rar_leaf_name(folder_leaf)}.rar"

                    # #162 finding #7: the archive's DISK location is
                    # collision-resistant - built from the path RELATIVE TO
                    # FILE_DIRECTORY, not the leaf alone - so two different
                    # artists' albums sharing a leaf name ("Greatest Hits")
                    # can no longer collide into the same file. Deliberately
                    # NOT the same string as rar_filename above; see
                    # _rar_archive_disk_name()'s own docstring for why
                    # changing what the user is OFFERED would break
                    # AutoQ.mrc's reconciliation.
                    target_rar_path = os.path.normpath(
                        os.path.join(config.TMP_ZIP_DIR, _rar_archive_disk_name(true_source_dir)))

                    if not os.path.exists(config.TMP_ZIP_DIR):
                        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)

                    # Strip any hidden line breaks (\n) out of the path
                    if isinstance(true_source_dir, str):
                        true_source_dir = true_source_dir.strip()

                    # `rar a` ADDS to an existing archive rather than
                    # replacing it - a stale file left behind by an earlier
                    # crashed run would otherwise silently have the new
                    # album packed on TOP of whatever was already there.
                    # Removed first so a fresh pack always starts from
                    # nothing, regardless of what used to be at this path.
                    long_target = platform_compat.long_path(target_rar_path)
                    if os.path.exists(long_target):
                        try:
                            os.remove(long_target)
                        except OSError as unlink_err:
                            print(f"[LINEAR RAR] Could not remove a stale archive at "
                                  f"{target_rar_path}: {unlink_err}")

                    print(f"[LINEAR RAR] Starting to pack: {true_source_dir} -> {target_rar_path}")


                    # Arguments are passed as a list, never through a shell:
                    work_dir_switch = f"-w{os.path.abspath(config.TMP_ZIP_DIR)}"
                    # Resolve the binary rather than trusting a bare name on PATH:
                    # WinRAR installs rar.exe outside PATH entirely.
                    rar_bin = platform_compat.rar_command(getattr(config, 'RAR_BINARY', None))
                    if not rar_bin:
                        raise FileNotFoundError(
                            "rar executable not found - set config.RAR_BINARY or put rar on PATH")
                    cmd = [rar_bin, "a", "-ep1", work_dir_switch, os.path.abspath(target_rar_path), os.path.abspath(true_source_dir)]
                    # A timeout is essential: with timeout=None a hung rar blocks this
                    # thread forever while config.rar_inprogress stays True, wedging folder
                    # packing for EVERY user until the daemon is restarted.
                    rar_timeout = getattr(config, 'RAR_TIMEOUT', 1800)
                    # See commands.py's note on the same call. rar is not
                    # Python, so nothing can guard what it writes - a
                    # filename in its error output is decoded here or
                    # nowhere, and a pack that failed for a nameable
                    # reason must not become a pack that failed silently.
                    try:
                        process = subprocess.run(cmd, capture_output=True,
                                                 text=True, encoding="utf-8",
                                                 errors="replace",
                                                 timeout=rar_timeout)
                    except subprocess.TimeoutExpired:
                        # rar was killed mid-write: whatever it wrote sits at
                        # the target path, and nothing else ever names that
                        # file (#717). Removed here; the wrapper's handling of
                        # the failure is unchanged.
                        _discard_partial_archive(target_rar_path, "timed out")
                        raise
                    
                    if process.returncode == 0 and os.path.exists(target_rar_path):
                        print(f"[LINEAR RAR] Compression succeeded. Waiting 2.0s for the disk to sync...")
                        time.sleep(2.0)
                        
                        final_size = os.path.getsize(target_rar_path)
                        print(f"[LINEAR RAR] The archive is settled on disk: {final_size:,} bytes")
                        
                        next_file['path'] = target_rar_path
                        next_file['file'] = rar_filename
                        next_file['is_unpacked_rar_folder'] = False
                        
                        # CAPACITY RE-CHECKED HERE, UNDER THE LOCK. The check
                        # this branch already passed happened before rar even
                        # started, which for a large album is minutes ago -
                        # long enough for every slot to have filled with plain
                        # file sends, each of which re-checked correctly on its
                        # own way through. This was the one append that did
                        # not, so a finished pack could push active_transfers
                        # past MAX_DCC_SLOTS with nothing to stop it.
                        #
                        # The archive is already built and the queue row still
                        # points at it, so leaving it queued costs nothing but
                        # a wait - the same outcome, and the same message, the
                        # two sibling dispatch paths use when they find no
                        # slot.
                        with queue_lock:
                            room = len(config.active_transfers) < config.MAX_DCC_SLOTS
                            if room:
                                config.active_transfers.append({"user": completed_user, "file": rar_filename, "bytes_sent": 0, "next_file_obj": rar_filename})
                        if not room:
                            print(f"[DCC-BLOCK] {completed_user}: all {config.MAX_DCC_SLOTS} slot(s) busy, "
                                  f"the packed archive stays queued for the next trigger.")
                            # Released by the wrapper's finally, once (#714).
                            return
                        if oserve: oserve.active_downloads = len(config.active_transfers)

                        announce_mod.send_dcc_sending_notice(completed_user, rar_filename, channel=target_chan)
                        
                        threading.Thread(
                            target=start_dcc_send, 
                            args=(sock, completed_user, target_rar_path, rar_filename, target_chan, next_file), 
                            daemon=True
                        ).start()
                        # Ownership of both interlocks now belongs to that send thread, which
                        # releases them in its own finally. Pack and send stay serialised.
                        return True
                    else:
                        error_msg = process.stderr.strip() if process.stderr else "Unknown RAR engine issue"
                        print(f"[LINJAR RAR ERROR] {error_msg}")
                        _discard_partial_archive(target_rar_path, "rar exited " + str(process.returncode))
                        announce_mod.send_debug(f"Pack FAILED in queue slot for {completed_user}: {error_msg}", category="PART")
                        # Charge the failure to the retry budget instead of recursing. The old
                        # code cleared the interlocks and called check_queue_and_send inline,
                        # which re-selected the SAME row and started another packer thread from
                        # inside this one - unbudgeted, and the wrapper finally would then strip
                        # the interlocks that new thread had just claimed.
                        release_queue_entry(completed_user, next_file, delivered=False,
                                            reason="rar exited " + str(process.returncode))
                        return False

                # At exactly the right level, so it wakes the function above immediately
                threading.Thread(target=inline_rar_packer, args=(irc_sock,), daemon=True).start()
                return

             # Plain audio file (.mp3/.flac), not a RAR folder pack.
            else:
                # FIXED (issue #27): section B already has admission control for this exact
                # race (see the already_sending check below in section B), but section A's
                # plain-file branch never got it. check_queue_and_send() can be invoked for
                # the SAME user from multiple independent triggers close together - the 3s
                # delayed_queue_trigger_fallback after every completed send, a JOIN/353
                # thaw, and !rehash's system trigger - and the initial next_file read above
                # only holds queue_lock for that one read, not for the channel-membership
                # check and dispatch that follow. Two overlapping calls could both read the
                # same queue head, both pass the channel check, and both start an
                # independent start_dcc_send for the identical file to the identical user:
                # one DCC handshake completes, the other times out or arrives as 0 bytes on
                # the leech side. Claim the user atomically, inside the lock, before
                # dispatching - start_dcc_send's finally already discards this key on every
                # exit path.
                with queue_lock:
                    # FIXED (issue #33): section B already re-checks capacity inside the
                    # lock; section A never checked it at all. check_queue_and_send() is also
                    # invoked for users who are NOT the one who just finished - a JOIN/353
                    # thaw, the freeze-abort timer, !rehash's system trigger - so repeated
                    # triggers could push active_transfers past MAX_DCC_SLOTS with no ceiling,
                    # and oserve.active_downloads (the advertised slot count) followed it over.
                    if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                        print(f"[DCC-BLOCK] {completed_user}: all {config.MAX_DCC_SLOTS} slot(s) busy, leaving queued for the next trigger.")
                        return

                    already_claimed = (
                        hasattr(config, 'user_processing_lock')
                        and completed_user.lower() in config.user_processing_lock
                    ) or any(
                        str(tx.get('user', '')).lower() == user_key
                        for tx in config.active_transfers
                    )

                    if already_claimed:
                        print(f"[DCC-BLOCK] {completed_user} is already claimed elsewhere; skipping duplicate dispatch.")
                        return

                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(completed_user.lower())

                    f_name = next_file['file'] if isinstance(next_file, dict) else os.path.basename(str(next_file))
                    f_path = next_file['path'] if isinstance(next_file, dict) else str(next_file)
                    config.active_transfers.append({"user": completed_user, "file": f_name, "bytes_sent": 0, "next_file_obj": f_name})

                print(f"[DCC QUEUE] Verified live in RAM for {target_chan}! Next file for {completed_user}: {f_name}")
                if oserve: oserve.active_downloads = len(config.active_transfers)

                announce_mod.send_dcc_sending_notice(completed_user, f_name, path=f_path, channel=target_chan)
                threading.Thread(target=start_dcc_send, args=(irc_sock, completed_user, f_path, f_name, target_chan, next_file), daemon=True).start()
                return
        else:
            # Not in any of our channels: freeze and start the countdown. The
            # policy lives in freeze_absent_user() so the global sweep below
            # applies exactly the same one (#530).
            freeze_absent_user(irc_sock, completed_user, target_chan)
            return

    # =====================================================================
    # B) Global queue handling for the next person in line, across all slots
    # =====================================================================
    if oserve:
        oserve.active_downloads = len(config.active_transfers)
        
    absent_users = []
    # The row section B claimed, dispatched AFTER the lock is released (#605).
    # The claim - user_processing_lock.add() and the active_transfers append -
    # is what needs queue_lock. The notice does not, and it stat()s the library
    # path for the console feed's byte count: on a hung NFS mount that stat
    # blocks forever, and every thread that takes queue_lock blocks behind it -
    # queue_worker samples live_speed() under it once a second (no more
    # outbound lines of any kind) and the IRC read thread takes it on every
    # NICK (no more PONGs, the server drops the bot). Section A's plain-file
    # branch has always dispatched outside the lock; this is the same shape.
    promoted = None
    if len(config.active_transfers) < config.MAX_DCC_SLOTS:
        with queue_lock:
            # FIXED: re-check the slot count INSIDE the lock. The test above is already
            # stale by the time we acquire, so two concurrent callers could both pass it
            # and overshoot MAX_DCC_SLOTS.
            if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                return

            for waiting_user, user_files in list(config.dcc_queue.items()):
                # Use the dcc_queue dict key for every lock/queue operation. The old code
                # tested the guards with one key and then rebound w_key to the display
                # name further down, so the guard and the claim could disagree.
                queue_key = str(waiting_user).lower()

                if hasattr(config, 'user_processing_lock') and queue_key in config.user_processing_lock:
                    continue

                if not user_files or len(user_files) == 0 or queue_key in config.frozen_queues:
                    continue

                # FIXED (issue #4): user_files is the LIST of this user's queued files, not a
                # single file. Without [0] the isinstance test below was always true (a list is
                # never a dict), so every waiting user was skipped and section B was dead code.
                g_next = user_files[0]
                if not isinstance(g_next, dict):
                    continue

                # FIXED: section B had no admission control at all. It was dead code until the
                # [0] fix above woke it up, and nothing had ever audited what it does when it
                # actually runs. It never claimed the entry, so two overlapping triggers - the
                # 3s fallback fires after EVERY transfer, plus the 353 and JOIN thaws - both
                # promoted the same queue head. The user received two DCC offers for one file,
                # two slots were burned on it, and then both finally blocks popped position 0:
                # the first removed the file that was sent, the second removed the NEXT file,
                # which had never been sent. Silent loss, persisted straight to dcc_queue.txt.
                already_sending = any(
                    str(tx.get('user', '')).lower() == queue_key
                    for tx in config.active_transfers
                )
                if already_sending:
                    continue

                real_username = g_next.get('user_raw', waiting_user)

                # TWO different questions, and one value used to answer both.
                #
                # "which channels prove this user is present" wants a LIST -
                # the entry may name one, and an entry that names none has to
                # be checked against every configured channel. "where do we
                # announce the finished transfer" wants exactly ONE, because
                # it ends up in `f"PRIVMSG {channel} :"`.
                #
                # g_chan answered both, so an entry with no 'channel' key
                # handed a list to start_dcc_send() below and the wire line was
                # `PRIVMSG ['#one', '#two'] :Sent ...` - a malformed target,
                # answered with a numeric nothing here reads, so the
                # announcement was lost while the transfer it announced had
                # already succeeded.
                #
                # #272 fixed the identical shape at check_queue_and_send()'s
                # other site and recorded that THIS one deliberately kept a
                # list, on the grounds that it was only a membership test. It
                # is not: g_chan is passed to start_dcc_send() at the thread
                # spawn below. Found by audit, and the test written then to
                # protect the list form was protecting a defect.
                g_chan = announce_channel_for(g_next)
                g_name = g_next.get('file', '')
                g_path = g_next.get('path', '')

                # PRESENCE IS ASKED OF EVERY CHANNEL WE ARE IN, NOT OF THE
                # ROW (#530). This used to build a list from g_next['channel']
                # and look for the user only there. A request made by PRIVATE
                # MESSAGE records the wire target as its channel - which is
                # the bot's own nick, and no such key ever exists in
                # channel_users - so a PM-originated head row was invisible
                # to this sweep however many channels the user was sitting
                # in. The specific-user branch above has always asked every
                # channel; this is the same question and now the same answer.
                # The row's channel is where to ANNOUNCE (g_chan, above), not
                # where to LOOK.
                user_is_globally_active = user_is_present_in_ram(queue_key)

                if not user_is_globally_active:
                    # Not here. Until #530 this was a silent `continue`, and
                    # a queue only the sweep ever looked at could sit
                    # forever. Freeze them exactly as the specific-user
                    # branch does - after the lock is released, because the
                    # freeze announces to the debug channel.
                    absent_users.append((real_username, g_chan))
                    continue

                if user_is_globally_active is True:
                    if g_next.get('is_unpacked_rar_folder') is True:
                        # FIXED: this was `break`, which abandoned the whole scan. One user
                        # waiting on a RAR pack starved every other waiting user behind them
                        # for as long as the pack took. Skip this user and keep looking.
                        print(f"[DCC QUEUE] Folder pack already pending for {real_username}. Skipping to the next waiting user.")
                        continue

                    # CLAIM the user before releasing the lock, so a concurrent caller sees
                    # them as busy. start_dcc_send's finally already discards this key on
                    # every exit path, including the early aborts.
                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(queue_key)

                    config.active_transfers.append({"user": real_username, "file": g_name, "bytes_sent": 0, "next_file_obj": g_name})
                    promoted = (real_username, g_path, g_name, g_chan, g_next)
                    break

    if promoted is not None:
        real_username, g_path, g_name, g_chan, g_next = promoted
        print(f"[DCC QUEUE] New user {real_username} verified live in RAM for {g_chan}. Got slot.")
        if oserve: oserve.active_downloads = len(config.active_transfers)

        announce_mod.send_dcc_sending_notice(real_username, g_name, path=g_path, channel=g_chan)
        threading.Thread(target=start_dcc_send, args=(irc_sock, real_username, g_path, g_name, g_chan, g_next), daemon=True).start()

    for absent_user, absent_chan in absent_users:
        freeze_absent_user(irc_sock, absent_user, absent_chan)


MIN_DCC_BLOCK_SIZE = 4096
MAX_DCC_BLOCK_SIZE = 1024 * 1024


# What "let the OS decide" is worth, per platform.
#
# MEASURED IN A BETA, and it cost a factor of eight. The same friend, the same
# machine, the same link: OmenServe 30.4 MB/s, DCCore 2.95 / 2.98 / 3.00 /
# 3.01 MB/s on four files of different sizes. Identical every time, because it
# was arithmetic rather than congestion.
#
# TCP cannot have more bytes in flight than the send buffer holds, so
# throughput is capped at SO_SNDBUF / round-trip-time. The default buffer on
# that machine was exactly 65,536 bytes. Setting the packet size to 4 KB made
# it WORSE (1.6 MB/s), and fitting both measurements gives the whole picture:
#
#     effective ceiling  : 3.19 MB/s
#     fixed cost / block : 1.27 ms
#     implied RTT        : 20.6 ms   <- 64 KB / 20.6 ms = 3.19 MB/s
#
# An ordinary internet round trip. Raising DCC_SEND_BUFFER to 1 MB took the
# same transfer to 23.9 and 24.7 MB/s.
#
# WHY THIS IS PER-PLATFORM AND NOT SIMPLY A NEW DEFAULT. The old behaviour was
# "never set it unless asked", justified by SO_SNDBUF disabling the OS's own
# auto-tuning. That reasoning is sound on Linux, where tcp_wmem grows the
# buffer to fit the connection and pinning it would be a downgrade on exactly
# the long-haul links that need it most. It does not hold on Windows, where
# what "leave it alone" gets you is a fixed 64 KB - so the honest default
# differs by platform, and neither one is the other's mistake.
#
# WHY 4 MB AND NOT THE 1 MB THAT FIXED THE REPORT. The measured link was
# 20.6 ms away, where 1 MB is already far more than enough - but the ceiling
# is a function of DISTANCE, and this bot serves a channel, not one friend:
#
#     RTT  20 ms   1MB ->  52.4 MB/s     4MB -> 209.7 MB/s
#     RTT 120 ms   1MB ->   8.7 MB/s     4MB ->  35.0 MB/s
#     RTT 200 ms   1MB ->   5.2 MB/s     4MB ->  21.0 MB/s
#     RTT 300 ms   1MB ->   3.5 MB/s     4MB ->  14.0 MB/s
#
# At 200 ms - an ordinary Australia-to-Europe hop - 1 MB lands back at the
# same few megabytes a second this whole change exists to escape. Fixing the
# nearby case and leaving the distant one is not a fix, it is a shorter list
# of people who are still capped.
#
# The cost is bounded and small: SO_SNDBUF is a CEILING the kernel may buffer,
# not an allocation, and it only fills when the network is slow enough to make
# it useful. MAX_DCC_SLOTS is 3 by default, so the worst case is a few tens of
# megabytes on a machine already moving files.
#
# An explicit DCC_SEND_BUFFER still wins everywhere, including a deliberate
# small value.
_DEFAULT_SEND_BUFFER = 4 * 1024 * 1024 if platform_compat.IS_WINDOWS else 0


def _apply_send_buffer(conn, log=print):
    """Set SO_SNDBUF, from config or from the platform default. Never raises.

    A socket option that cannot be set is not a reason to fail a transfer that
    would otherwise work - the kernel is entitled to refuse or to round the
    value, and the transfer proceeds either way.
    """
    try:
        wanted = int(getattr(config, "DCC_SEND_BUFFER", 0) or 0)
    except (TypeError, ValueError):
        return
    if wanted <= 0:
        # "Let the OS tune it" is the right answer on one platform and a
        # 64 KB ceiling on the other - see _DEFAULT_SEND_BUFFER.
        wanted = _DEFAULT_SEND_BUFFER
    if wanted <= 0:
        return
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, wanted)
    except (OSError, socket.error) as err:
        log(f"[DCC] Could not set the send buffer to {wanted}: {err}. "
            f"Continuing with the OS default.")


# ------------------------------------------------------------- DCC RESUME
#
# REPORTED FROM A BETA, from mIRC: a transfer sat at "Requesting resume"
# and never moved. It was waiting for a reply this bot had never been able
# to give - DCC RESUME was not implemented at all, in either direction.
#
# The exchange is three lines. We offer:
#
#     DCC SEND <name> <ip> <port> <size>
#
# a receiver holding a partial file answers with what it already has:
#
#     DCC RESUME <name> <port> <position>
#
# and the sender MUST answer before anything else happens:
#
#     DCC ACCEPT <name> <port> <position>
#
# Only then does the receiver connect. Without the ACCEPT it waits, which is
# exactly what was seen: not a failure, not an error, just a client keeping
# its side of a bargain the other side never answered.
#
# MATCHED BY PORT, NEVER BY FILENAME. The port is ours, unique per offer and
# unambiguous. The offered name has already been through a space-to-underscore
# pass, and announce.fit_irc_filename() may have SHORTENED it to fit the IRC
# line - so the name we sent is not always the name we hold, and matching on
# it would fail on exactly the long-titled files most likely to need resuming.
#
# The name in our ACCEPT is the one WE offered, read back from the handshake
# that actually went out, rather than the one echoed at us. It is what the
# receiver is matching against, and it means no text from the wire is ever
# interpolated back into an outbound line.


def register_send_offer(user, port, offered_name, file_size):
    """Record a DCC SEND that has gone out and not yet been picked up.

    Registered BEFORE the handshake is sent, not after: the receiver may
    answer with a RESUME the instant the offer lands, and an entry that
    appears a moment later would miss it.
    """
    with runtime.dcc_send_offers_lock:
        runtime.dcc_send_offers[(str(user).strip().lower(), int(port))] = {
            "filename": str(offered_name),
            "size": int(file_size),
            "position": 0,
        }


def clear_send_offer(user, port):
    """Drop the offer and return it, or None. Safe to call twice - every exit
    from a send runs through the finally that calls it, including the ones
    that never got as far as registering."""
    try:
        key = (str(user).strip().lower(), int(port))
    except (TypeError, ValueError):
        return None
    with runtime.dcc_send_offers_lock:
        return runtime.dcc_send_offers.pop(key, None)


def parse_resume_request(body):
    """(filename, port, position) from a "DCC RESUME ..." body, or None.

    Split from the RIGHT. The last two fields are numbers and everything
    between the verb and them is the name, so a name containing spaces - which
    mIRC sends in double quotes - needs no special case and no quote parsing
    that a peer could get creative with.
    """
    text = str(body or "").strip().strip("\x01").strip()
    if not text.upper().startswith("DCC RESUME"):
        return None
    parts = text[len("DCC RESUME"):].strip().rsplit(None, 2)
    if len(parts) != 3:
        return None
    filename, port_text, position_text = parts
    try:
        port, position = int(port_text), int(position_text)
    except ValueError:
        return None
    # A port outside the range cannot be one we are listening on, and a
    # negative position is not a position. Both are refused here rather than
    # left for the lookup to miss, so the log says which it was.
    if not (0 < port <= 65535) or position < 0:
        return None
    return filename.strip('"'), port, position


def handle_resume_request(irc_sock, user, body, background=False):
    """Answer a receiver's DCC RESUME with the DCC ACCEPT it is waiting for.

    True if an ACCEPT was sent - or, with `background`, is on its way. False
    means no offer of ours matched, which is the ordinary outcome for a stray
    or forged line and is not logged as an error: anyone on the network can
    send this, and the only thing that makes it ours is a port we are
    listening on for that exact nick.

    `background` is what the IRC read loop passes (#577, #602). The ACCEPT goes
    out through the shared pacer, which sleeps up to MSG_DELAY for a slot; done
    on the read thread that is up to MSG_DELAY with no PING answered and no line
    parsed, once per matching RESUME. So the lookup and the position - the parts
    that must be settled before the receiver can connect - stay here, and the
    paced send moves to a short-lived thread. One at a time per offer: a
    RESUME that arrives while one is waiting only updates the position, and the
    reply that goes out carries the latest.
    """
    parsed = parse_resume_request(body)
    if not parsed:
        return False
    _echoed_name, port, position = parsed
    key = (str(user).strip().lower(), int(port))

    with runtime.dcc_send_offers_lock:
        offer = runtime.dcc_send_offers.get(key)
        if not offer:
            return False
        size = int(offer.get("size") or 0)
        # CLAMPED, NOT TRUSTED. The position decides where we seek in a file
        # of ours, and it arrives from the network. Past the end is not an
        # error worth refusing over - a receiver whose partial copy is longer
        # than our file has a different file, and answering "you already have
        # all of it" completes their transfer honestly instead of leaving them
        # hanging, which is the failure this whole feature exists to end.
        position = max(0, min(position, size))
        offer["position"] = position
        if background:
            if offer.get("accept_pending"):
                return True
            offer["accept_pending"] = True

    if not background:
        return _send_resume_accept(irc_sock, user, key)
    try:
        threading.Thread(target=_send_resume_accept, args=(irc_sock, user, key, True),
                         daemon=True).start()
    except Exception as err:
        with runtime.dcc_send_offers_lock:
            offer = runtime.dcc_send_offers.get(key)
            if offer:
                offer.pop("accept_pending", None)
        print(f"[DCC-RESUME] Could not answer {user}'s resume request: {err}")
        return False
    return True


def _send_resume_accept(irc_sock, user, key, clear_pending=False):
    """The paced half of handle_resume_request(): wait for the shared clock,
    then send the ACCEPT for the position the offer holds at THAT moment, and
    record that it was resumed.

    The slot is waited for BEFORE the offer is read, so a RESUME that arrived
    while this one waited has already moved the position, and the pending flag
    stays set for the whole wait: that is what makes it one reply at a time.
    """
    port = key[1]
    try:
        # THROUGH THE SHARED CLOCK, not straight onto the socket (#453). A
        # resume handshake is latency-sensitive, so it is not queued behind
        # the round-robin - but it is still a PRIVMSG leaving this connection,
        # and a peer that reconnects and resumes repeatedly could otherwise
        # emit them as fast as it asked for them. Waiting for a slot keeps the
        # reply prompt while still counting it against the same budget every
        # other outbound line respects.
        runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
    except Exception as err:
        print(f"[DCC-RESUME] Could not answer {user}'s resume request: {err}")
        if clear_pending:
            with runtime.dcc_send_offers_lock:
                offer = runtime.dcc_send_offers.get(key)
                if offer:
                    offer.pop("accept_pending", None)
        return False

    with runtime.dcc_send_offers_lock:
        offer = runtime.dcc_send_offers.get(key)
        if not offer:
            return False
        if clear_pending:
            offer.pop("accept_pending", None)
        size = int(offer.get("size") or 0)
        position = int(offer.get("position") or 0)
        offered_name = offer["filename"]

    # The position is stored BEFORE the ACCEPT goes out, and that ordering is
    # the whole race. A receiver connects only once it has seen the ACCEPT, so
    # by the time accept() returns in the sending thread the offset is already
    # there to be read. Sending first and storing afterwards would leave a
    # window in which a prompt client connects and gets sent the file from
    # byte zero, appended onto what it already had.
    reply = (f"PRIVMSG {user} :\x01DCC ACCEPT {offered_name} "
             f"{port} {position}\x01\r\n")
    try:
        irc_sock.sendall(reply.encode("utf-8", errors="ignore"))
    except Exception as err:
        print(f"[DCC-RESUME] Could not answer {user}'s resume request: {err}")
        return False
    print(f"[DCC-RESUME] {user} already has {position} of {size} bytes of "
          f"{offered_name}; accepted and will send from there.")
    import stats_mgr
    announce.feed_event(
        "RESUMED",
        f'Resumed "{offered_name}" for {user} at '
        f'{stats_mgr.format_size_human(position)} of {stats_mgr.format_size_human(size)}',
        nick=user, at_bytes=position, total_bytes=size, name=offered_name)
    return True


def offered_name_from_handshake(handshake):
    """The filename as it actually went out in a DCC SEND line.

    Read back from the handshake rather than assumed, because
    announce.fit_irc_filename() may have shortened it. The line ends with
    "<ip> <port> <size>", so everything between the verb and the last three
    fields is the name - the same right-hand split parse_resume_request()
    uses, and for the same reason.
    """
    text = str(handshake or "")
    if "DCC SEND " not in text:
        return ""
    tail = text.split("DCC SEND ", 1)[1]
    parts = tail.rsplit(" ", 3)
    return parts[0] if len(parts) == 4 else ""


def dcc_block_size():
    """Bytes per read/write pass of a transfer, clamped to something sane.

    Read through config on every transfer rather than captured once, like
    every other tunable here: !rehash reloads config, and a value baked in at
    import would keep the old one for the life of the process.

    CLAMPED, not trusted. This number is multiplied by the number of
    concurrent transfers to give the memory this bot holds in send buffers, so
    a mistyped 500000000 is half a gigabyte per slot. And a value below a few
    kilobytes turns the send loop into a syscall storm for no benefit - 0 or a
    negative would spin it. Neither is worth a startup error when the honest
    thing is to use the nearest usable number and get on with the transfer.
    """
    try:
        wanted = int(getattr(config, "DCC_BLOCK_SIZE", 65536))
    except (TypeError, ValueError):
        return 65536
    return max(MIN_DCC_BLOCK_SIZE, min(MAX_DCC_BLOCK_SIZE, wanted))


def transfers_are_paused():
    """Is the bot holding new sends while something finishes?

    Set around a rehash (#310): a reload swaps the modules a running
    transfer is inside, so the safe order is stop starting new ones, let the
    ones in flight finish, reload, then start again.

    Deliberately NOT update_inprogress. That flag means "the list is being
    rebuilt" and the notice a user gets says so - telling somebody the list is
    rebuilding when it is not is the kind of small untruth that makes every
    other message less believable.
    """
    return bool(getattr(config, "transfers_paused", False))


def _discard_partial_archive(target_rar_path, why):
    """Remove what a failed rar run left at its output path (#717, audit L53).

    A run that timed out (killed mid-write) or exited non-zero left the
    partial archive at target_rar_path. The queue row points at the SOURCE
    folder, so neither discard_orphaned_temp_archives() nor the send's own
    finally ever named that file; the row was retried, and after
    MAX_SEND_FAILS dropped, with a multi-GB partial left in TMP_ZIP_DIR until
    the same folder was packed again or an operator found it. The path is
    exclusively this pack's output, so removing it is safe.
    """
    try:
        if os.path.exists(target_rar_path):
            size = os.path.getsize(target_rar_path)
            os.remove(target_rar_path)
            print(f"[LINEAR RAR] Removed the partial archive a run that {why} left behind "
                  f"({size:,} bytes): {os.path.basename(target_rar_path)}")
    except OSError as err:
        print(f"[LINEAR RAR] Could not remove the partial archive {target_rar_path}: {err}")


def a_pack_is_running():
    """Is the folder packer's thread alive right now?

    The rehash needs this and config.rar_inprogress cannot answer it (#651,
    audit M49): the flag is True both while `rar` runs and after a packer
    died without releasing it, and the reload resets it to False either
    way. A rehash whose quiesce wait timed out under a running pack then
    cleared both interlocks and woke the queue, and the user's still-queued
    row could start a second rar on the same archive path - the double-pack
    the packer's own docstring records fixing. The thread is the difference
    between "packing" and "wedged": alive, keep the interlocks; gone, they
    are stale and the rehash is the documented way to clear them.
    """
    thread = runtime.packer_thread
    return thread is not None and thread.is_alive()


def wait_for_transfers_to_finish(timeout=None, poll=0.5, sleep=None,
                                 log=print):
    """Stop starting new sends, then wait for the ones in flight to end.

    Returns True if the bot went quiet, False if the timeout ran out first.

    A TIMEOUT IS NOT OPTIONAL. A transfer can sit at "receiving" for as long
    as the far end keeps the socket open and does nothing, and a rehash that
    waits for it waits for ever - an admin typing !rehash and getting silence
    is worse than one whose transfers were interrupted, because at least the
    second one knows what happened.

    Returning False is not a failure to report and stop on: the caller carries
    on and reloads anyway, having done what it could. That is the honest
    trade - the alternative is a bot that cannot be reconfigured while one
    stuck peer holds a socket.
    """
    import time as time_mod

    naptime = sleep or time_mod.sleep
    if timeout is None:
        timeout = float(getattr(config, "REHASH_TRANSFER_WAIT", 120))

    config.transfers_paused = True
    deadline = time_mod.time() + max(0.0, float(timeout))
    announced = False

    while True:
        # A RUNNING PACK COUNTS AS BUSY. config.active_transfers is the SEND
        # side, and a folder pack has no row there while it runs:
        # check_queue_and_send() claims rar_inprogress, runs `rar` for up to
        # RAR_TIMEOUT (half an hour by default), and only appends once the
        # archive exists.
        #
        # So the wait saw an idle bot and returned at once. The reload then
        # re-executed defaults.py - whose body sets `rar_inprogress = False` -
        # and commands.py rebound user_processing_lock to a fresh empty set,
        # both while the packer was still running. The next !rar read both
        # interlocks as free and started a SECOND rar process; two packs of
        # the same album target the same archive path, and the second one
        # removes the file the first is still writing.
        active = list(getattr(config, "active_transfers", []) or [])
        if getattr(config, "rar_inprogress", False):
            active = active + [{"user": "(packing)", "file": "!rar archive"}]
        if not active:
            if announced:
                log("[REHASH WAIT] Every transfer finished; reloading now.")
            return True
        if time_mod.time() >= deadline:
            log(f"[REHASH WAIT] {len(active)} transfer(s) still running after "
                f"{timeout:.0f}s - reloading anyway. They may be interrupted.")
            return False
        if not announced:
            announced = True
            log(f"[REHASH WAIT] Waiting for {len(active)} transfer(s) to "
                f"finish before reloading. No new sends will start.")
        naptime(poll)


def resume_transfers():
    """Let sends start again. Always called, even when the wait timed out -
    a rehash that failed to quiesce must not leave the bot permanently
    refusing to send."""
    config.transfers_paused = False


def handle_download_request(irc_sock, user, requested_file, target_chan):
    """Runs when somebody requests a file, or a whole folder via !rar."""
    # ---------------------------------------------------------------------
    # target_chan becomes this request's queue row "channel" a few lines
    # down, and that stored value is what announce.py later builds an
    # outbound "PRIVMSG {channel} :Sent: ..." line from on completion - so
    # it is re-validated here, at the one place it gets persisted, even
    # though irc.py's own PRIVMSG parser is already anchored so target_chan
    # can only ever be the literal wire target of this message. It must be
    # a channel we are actually configured to be in, or our own nick (a
    # private request); anything else can only mean a parsing bug
    # upstream, and is refused rather than trusted with a real advert line.
    configured_channels = {c.strip().lower() for c in str(getattr(config, 'CHANNEL', '')).split(',') if c.strip()}
    if (str(target_chan).lower() not in configured_channels
            and str(target_chan).lower() != str(getattr(config, 'NICKNAME', '')).lower()):
        print(f"[SECURITY] Refused a download request from {user}: "
              f"target_chan {target_chan!r} is not a channel this bot is in.")
        return
    # ---------------------------------------------------------------------
    # WHICH LIST THIS REQUEST BELONGS TO (#26).
    # ---------------------------------------------------------------------
    # Resolved once, here, because two things below need the same answer and
    # they must not disagree: the folders a bare filename is looked for in, and
    # the list the filename is resolved against. A request is answered from the
    # list it was advertised from - otherwise a name that exists in two lists
    # sends the wrong file to somebody who copied the right row.
    #
    # None means no list is bound to this channel and the primary is not the
    # catch-all: #26's "a channel with no list bound gets nothing". Silence
    # rather than an error, because an error implies something went wrong and
    # nothing did - this bot does not serve here.
    import library
    wanted_list = library.list_name_for_request(target_chan)
    if wanted_list is None:
        print(f"[DCC] No list is bound to {target_chan!r}; ignoring the "
              f"request from {user}.")
        return
    # A private message has no channel to route on, but a labelled `!rar`
    # row does (#653): its first component names a folder, and folders
    # belong to lists. Routed by that label; an ambiguous one is refused
    # with a notice saying where to ask instead.
    if not str(target_chan or "").startswith(("#", "&")):
        spec = str(requested_file or "").strip()
        if spec[:5].lower() == "!rar ":
            parts = list_mod.list_heading_parts(spec[5:].split("::INFO::")[0].strip())
            routed = library.list_name_for_label(parts[0] if parts else "", wanted_list)
            if routed is None:
                print(f"[DCC] {user}'s private request names a folder label that "
                      f"more than one list serves; asked them to use the channel.")
                announce.send_dcc_error(user, "ambiguous_list")
                return
            if routed != wanted_list:
                print(f"[DCC] {user}'s private request is a {routed!r} row; "
                      f"answering from that list rather than {wanted_list!r}.")
                wanted_list = routed
    # ---------------------------------------------------------------------
    # The global maintenance gate:
    # ---------------------------------------------------------------------
    # update_inprogress, NOT search_inprogress (#214). Both are set by !update,
    # but search_inprogress is ALSO set by every @find for the seconds it reads
    # the list - so any search refused every file request from everyone else,
    # and told them "MasterList is currently rebuilding", which was untrue. A
    # search only READS the list; it is the rebuild that replaces the files
    # underneath a transfer, and only the rebuild needs this gate.
    #
    # A real rebuild is unaffected: !update sets update_inprogress
    # unconditionally and search_inprogress only when PAUSE_ON_UPDATE is on,
    # so gating on update_inprogress behind the same switch refuses exactly
    # what it refused before.
    # A rehash is quiescing: new sends wait, in-flight ones finish. Its own
    # message, because "the list is rebuilding" would not be true. The
    # request itself goes on (#668, audit L4): it used to be refused here
    # with "Your request is not lost - try again in a moment", and nothing
    # replayed it, so it was lost unless the user typed it again. Only the
    # dispatch has to wait - check_queue_and_send() is gated, and the direct
    # send below checks the pause under queue_lock - and the rehash wakes
    # the queue once the reload is done, so a request queued now is served
    # then. The notice says that.
    if transfers_are_paused():
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: The bot is reloading its configuration. Your request is queued and starts when the reload is done.\r\n")
        print(f"[MAINTENANCE] Queued a file request from {user} for after the "
              f"rehash: it is waiting for transfers to finish.")

    # Only while the rebuilt list is swapped in (#923), unless the operator
    # asked for the whole rebuild - and the notice says which it is.
    if list_mod.rebuild_pauses_requests():
        wait = ("Please wait 1-2 minutes." if list_mod.rebuild_pauses_everything()
                else "Please try again in a few seconds.")
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: MasterList is currently rebuilding. File requests temporarily paused. {wait}\r\n")
        print(f"[MAINTENANCE BLOCK] Refused a file request from {user}: an !update is running.")
        return
    # ---------------------------------------------------------------------

    oserve = sys.modules.get('oserve')
    try:
        user_key = str(user).lower().strip()
        if oserve:
            oserve.active_downloads = len(config.active_transfers)
        print(f"[DCC] {user} requested: {requested_file}")

        # ---------------------------------------------------------------------
        # Asynchronous folder packing: the !rar gate, with root and NFS guards
        # ---------------------------------------------------------------------
        if requested_file.lower().startswith("!rar "):
            import announce as announce_mod

            if not getattr(config, 'RAR_ENABLED', True):
                announce_mod.send_dcc_error(user, "rar_disabled")
                return

            # FILE_DIRECTORY is deliberately not in settings_file.REQUIRED any
            # more (#184's review) - the daemon can be up and
            # answering requests before an operator has chosen a music
            # directory. Every album this branch packs lives under it, so
            # checked here, explicitly, rather than letting
            # list_mod.resolve_list_folder()/is_safe_path() below fail on a
            # None base and fall through to the bare except at the bottom of
            # this function, which told the requester nothing at all.
            if not library.folders(wanted_list):
                announce_mod.send_dcc_error(user, "not_configured")
                return

            raw_win_path = requested_file[5:].strip()
            
            # Trim any leftovers, in case somebody pasted an old row
            if "::INFO::" in raw_win_path:
                raw_win_path = raw_win_path.split("::INFO::")[0].strip()
                
            win_path = re.sub(r'\s*\[[^\]]+\]$', '', raw_win_path).strip()

            # A pack request is a heading ("D:\\MEDIA\\<folder>\\...") - a drive
            # letter is how a heading is written, so only the remote forms are
            # refused here: a UNC or device path would be probed by realpath()
            # in the containment check below before it could be refused (#578).
            if names_a_remote_or_absolute_path(win_path, windows=False):
                print(f"[SECURITY] Refused {user}'s pack request: {win_path!r} names a remote location.")
                announce_mod.send_pack_error_notice(irc_sock, user)
                return

            # This used to be a third, differently-shaped copy of the same
            # "D:\MUSIC\<folder>\" prefix-stripping list.resolve_list_folder()
            # already does - non-anchored `.replace("D:/", "")` calls rather
            # than a startswith-anchored strip, which would have silently
            # matched "D:/" anywhere in the string, not just at the start.
            # os.path.normpath is kept even though resolve_list_folder()
            # itself doesn't call it: is_safe_path() below re-resolves the
            # path with os.path.realpath() regardless, but this is the
            # traversal guard's input and there is no reason to change its
            # exact shape while consolidating the prefix logic.
            # Resolved WITH its root, because both guards below are about the
            # one folder this heading landed in, not about a global. With
            # several folders configured (#164), asking "is it inside
            # FILE_DIRECTORY" would be asking about the wrong one; asking "is
            # it inside ANY configured folder" would be a weaker test than the
            # one this line has always had. Resolving to a single root keeps
            # the check exactly as strong as it is today.
            resolved_dir, source_folder = list_mod.resolve_list_folder_with_root(
                win_path, wanted_list)
            true_source_dir = os.path.normpath(resolved_dir)

            # ---------------------------------------------------------------------
            # THE TRAVERSAL GUARD - this one is critical:
            # without this check, anyone in the channel could type
            # "!rar ../../root/.ssh" and have that whole directory packed and sent
            # to them. os.path.normpath eats every "..", and the old root guard
            # below let through anything that contained a slash.
            # So the FINAL path is verified to still be inside the music directory
            # before anything else happens.
            # ---------------------------------------------------------------------
            # Against the folder this heading RESOLVED INTO, not a global. With
            # several folders configured (#164) the request may legitimately
            # name any of them, and checking against one would refuse every
            # album in the others. A None root means resolution found no
            # configured folder at all, which is not safe by definition.
            if source_folder is None or not is_safe_path(source_folder.path, true_source_dir):
                print(f"[SECURITY] Blocked a traversal attempt from {user}: {raw_win_path!r} -> {true_source_dir}")
                announce_mod.send_pack_error_notice(irc_sock, user)
                announce_mod.send_debug(
                    f"Path traversal denied for {config.C_BOLD}{user}{config.C_RESET}: request resolved outside the music root.",
                    category="HARDBAN")
                return

            # An artist root (one path segment under FILE_DIRECTORY, no album
            # subfolder) rather than an actual album folder. relpath() rather
            # than the old hand-built linux_sub_path - same question, asked
            # of the path resolve_list_folder() already produced.
            relative_to_root = os.path.relpath(true_source_dir, source_folder.path)
            if os.sep not in relative_to_root:
                print(f"[SECURITY] Blocked an attempt to pack the root folder from {user}: {relative_to_root}")
                announce_mod.send_pack_error_notice(irc_sock, user)
                announce_mod.send_debug(f"Pack denied for {user}: {config.C_BOLD}{relative_to_root}{config.C_RESET} is an artist root folder.", category="PART")
                return
            
            if not os.path.exists(platform_compat.long_path(true_source_dir)) or not os.path.isdir(platform_compat.long_path(true_source_dir)):
                announce_mod.send_debug(f"Pack error: Directory not found on disk storage for {user}.", category="PART")
                return

            # RAR_EXTENSIONS, ENFORCED HERE - the only place enforcing it
            # means anything.
            #
            # That setting decides whether update_list.py WRITES a
            # "!<nick> !rar <folder>" row. It never decided whether one would
            # be honoured: this path's whole gate was RAR_ENABLED, containment,
            # and "not an artist root". A folder with no row in the album list
            # was packed perfectly happily by anyone who named it.
            #
            # Harmless while nobody could name one. The film list publishes
            # folder headings in the archive every user downloads, and
            # list_heading_parts() strips the prefix, so a heading can be
            # pasted straight back as a request - which is how a folder that
            # was deliberately kept OUT of the album list became reachable.
            # That was an unbounded pack behind a line anybody in the channel
            # could send; MAX_RAR_FOLDER_SIZE below is the other half of it.
            #
            # Checked with scandir and stopped at the first match: the pack
            # about to run walks this whole folder anyway.
            packable = update_list.rar_extensions()
            if packable:
                try:
                    has_packable = any(
                        entry.is_file()
                        and update_list.is_packable_file(entry.name, packable)
                        for entry in os.scandir(
                            platform_compat.long_path(true_source_dir)))
                except OSError as scan_err:
                    print(f"[PACK] Could not read {true_source_dir!r} to check "
                          f"what is in it: {scan_err}")
                    has_packable = False
                if not has_packable:
                    print(f"[PACK] Refused {relative_to_root!r} for {user}: "
                          f"it holds nothing in RAR_EXTENSIONS.")
                    announce_mod.send_pack_error_notice(irc_sock, user)
                    announce_mod.send_debug(
                        f"Pack denied for {user}: "
                        f"{config.C_BOLD}{relative_to_root}{config.C_RESET} "
                        f"holds no packable file type. The files in it can "
                        f"still be requested by name.",
                        category="PART")
                    return

            # MAX_RAR_FOLDER_SIZE, enforced in the same place and for the same
            # reason as RAR_EXTENSIONS above: this is where a request becomes a
            # pack, and a rule not applied here is not applied.
            #
            # Refused BEFORE the queue, deliberately. Accepting it and finding
            # out at pack time costs a pack slot, half an hour of RAR_TIMEOUT,
            # a part-written archive in TMP_ZIP_DIR, and still ends with the
            # requester told nothing useful. The size is knowable now.
            size_cap = int(getattr(config, "MAX_RAR_FOLDER_SIZE", 0) or 0)
            over, measured = update_list.pack_size_over(true_source_dir, size_cap)
            if over:
                print(f"[PACK] Refused {relative_to_root!r} for {user}: over "
                      f"{size_cap:,} bytes (measured at least {measured:,}).")
                announce_mod.send_pack_error_notice(irc_sock, user)
                announce_mod.send_debug(
                    f"Pack denied for {user}: "
                    f"{config.C_BOLD}{relative_to_root}{config.C_RESET} is "
                    f"larger than MAX_RAR_FOLDER_SIZE. The files in it can "
                    f"still be requested by name.",
                    category="PART")
                return

            with queue_lock:
                total_global_queued = get_total_queued_count()
                user_queued_count = len(config.dcc_queue.get(user_key, []))

                if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                    announce_mod.send_dcc_error(user, "global_full")
                    return

                if user_queued_count >= config.MAX_USER_QUEUE:
                    announce_mod.send_dcc_error(user, "user_full")
                    return

                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []

                # ---------------------------------------------------------------------
                # Album naming, kept AutoQ-compatible with the parentheses AND
                # brackets preserved
                # ---------------------------------------------------------------------
                folder_name = os.path.basename(true_source_dir.rstrip("/"))

                # Allows parentheses, square brackets and ordinary hyphens, so
                # AutoQ.mrc can match the filename. AutoQ's own filercvd handler
                # reconciles a completed .rar against the folder path it queued
                # by de-underscoring the received name (swapping "_" back to " ")
                # and comparing it to that path's basename - it never touches
                # brackets. A folder tagged "[WEB] [192K]" used to lose those
                # brackets here while AutoQ still expected them on the other
                # side of that comparison, so the two strings never matched and
                # the request stayed listed as outstanding in AutoQ's queue
                # window forever, even though the transfer itself completed
                # correctly every time.
                #
                # _sanitize_rar_leaf_name() is the SAME function the packer
                # itself now uses (see its own docstring) - one definition,
                # so the name queued here and the name the packer eventually
                # produces can no longer silently disagree.
                clean_folder_name = _sanitize_rar_leaf_name(folder_name)

                master_rar_filename = f"{clean_folder_name}.rar"


                config.dcc_queue[user_key].append({
                    "file": master_rar_filename, # The clean name is what gets written to dcc_queue.txt
                    "path": true_source_dir,
                    "channel": target_chan,
                    "user_raw": user,
                    "is_unpacked_rar_folder": True,
                    "is_temporary_zip": True
                })
                user_pos = len(config.dcc_queue[user_key])

            # Persisted AFTER the lock is released (#605): save_dcc_queue()
            # takes its own snapshot of the dict and fsyncs under disk_lock,
            # and a slow or contended data/ disk must not hold queue_lock
            # while it does - see the file branch below for the full story.
            import db
            db.save_dcc_queue()  # Commit straight to dcc_queue.txt

            print(f"[RAR QUEUE] Added virtuell mapp {master_rar_filename} for {user} at position #{user_pos}.")

            # One single clean line to the debug channel, nothing more
            announce_mod.feed_event("REQUEST", f"{user} asked for the folder \"{clean_folder_name}\" - packing it, sending when done.",
                                    nick=user, channel=target_chan, kind="folder", name=clean_folder_name)

            announce_mod.send_dcc_queue_notice(user, folder_name, user_pos, channel=target_chan)
            threading.Thread(target=check_queue_and_send, args=(irc_sock, user), daemon=True).start()
            return


        # A request copied straight off a search result carries its own
        # "  ::INFO:: <size>" tail (list.py's own two-space convention, but
        # strip_info_suffix() tolerates any spacing and any cross-bot
        # branding after the marker too). That size is the one thing that
        # tells two identically-named copies apart, so it is kept as a hint
        # for the list scan below instead of being thrown away.
        requested_file, requested_size_hint = list_mod.strip_info_suffix(requested_file)
        requested_size_hint = requested_size_hint.lower().strip()

        requested_file = str(requested_file)
        # Before the "/" strip: "//host/share" is a UNC path to Windows, and
        # once its slashes are stripped it would look like a harmless relative
        # one. Refused on the text, before any file system call (#578).
        if names_a_remote_or_absolute_path(requested_file):
            print(f"[SECURITY] Refused {user}'s request: {requested_file!r} names a location, not a file in the library.")
            announce.send_dcc_error(user, "invalid_path")
            return
        requested_file = requested_file.lstrip("/")

        # The master list lives in LOCAL_LIST_DIR, everything else in the
        # music directory. Matched on the names the list builder writes rather
        # than on ".zip" plus the base name appearing anywhere: with .rar now
        # a list format too, a shared library file called
        # "Someone - DCCore Sessions.rar" would otherwise be looked for among
        # the lists and never found.
        if list_mod.is_list_artifact_name(requested_file):
            # THIS REQUEST'S LIST directory, not LOCAL_LIST_DIR. Every list
            # writes its archive under the same name, and a non-primary list
            # writes it in its own subdirectory - so looking in the root would
            # answer "file not found" for every list but one, which is the
            # whole of what send_file_list() just offered them.
            base_directory = os.path.abspath(list_mod.list_dir(wanted_list))
            full_path = os.path.join(base_directory, requested_file)
            # One root per list, and deliberately so: a list's files live in
            # exactly one place regardless of how many folders it spans.
            search_roots = [base_directory]
        else:
            # Same reasoning as the !rar branch above: FILE_DIRECTORY can
            # legitimately be unset (#184's review), and an
            # ordinary track request - unlike a list artifact request, which
            # never touches FILE_DIRECTORY at all - has nothing to look for
            # without it. Checked explicitly rather than letting
            # os.path.abspath(None) raise into the bare except below, which
            # left the requester with no response of any kind.
            if not library.folders(wanted_list):
                announce.send_dcc_error(user, "not_configured")
                return
            # Every configured folder, in the operator's order (#164). The
            # first is still where a bare filename is guessed to be, which is
            # what a single-folder install has always done; the rest are what
            # the list lookup and the walk below fall through to.
            search_roots = [os.path.abspath(folder.path)
                            for folder in library.folders(wanted_list)]
            base_directory = search_roots[0]
            full_path = os.path.join(base_directory, requested_file)

        is_master_zip = list_mod.is_list_artifact_name(requested_file)
        if not is_master_zip and not os.path.exists(platform_compat.long_path(full_path)):
            # Both memories first (#886), cheapest first: this exact name,
            # then the folders recent lookups resolved into. Checked against
            # search_roots - the CURRENT configuration, not whatever it was
            # when the memory was made - right here rather than trusting the
            # containment check further down to sort it out (#901).
            #
            # The dashboard's Folders and Lists pages change the library
            # without a rehash: apply_folder_changes()/apply_list_changes()
            # (webserver.py) save and return, and the folder set is re-read
            # from disk on every call, so the new roots are live on the very
            # next request. A path remembered under a folder just removed on
            # that page is still on disk - the memory's own existence check
            # cannot catch this, the file has not gone anywhere - so without
            # this check it would reach the containment check below and be
            # refused as "invalid path" for a file the new configuration
            # serves perfectly well, until LOOKUP_HIT_TTL_SECONDS passes.
            # #889's !rehash call stays; it frees the memory outright, which
            # this does not need to wait for and does not replace - a scan
            # still in flight across a rehash records its hit after the
            # forget, and this is what keeps THAT entry honest too.
            remembered_key = (str(wanted_list), str(requested_file).lower().strip())
            for candidate in (_remembered_path(remembered_key),
                              _in_a_recent_folder(str(wanted_list), requested_file)):
                if candidate and any(is_safe_path(root, candidate) for root in search_roots):
                    full_path = candidate
                    break

        if not is_master_zip and not os.path.exists(platform_compat.long_path(full_path)):
            # THE EXPENSIVE PART, BOUNDED (#580). A name that is not in the first
            # folder's root is looked up by streaming every published list and,
            # failing that, walking every configured folder - a full-library
            # metadata scan, on this request's own thread, for one ~40 byte line.
            # Ten of those per five seconds is what the flood gate allows a nick,
            # so the cost is bounded here instead: a name that just missed is
            # answered from memory, and only a few scans run at once.
            miss_key = (str(wanted_list), str(requested_file).lower().strip())
            if _lookup_missed_recently(miss_key):
                announce.send_dcc_error(user, "file_not_found")
                return
            # WAITS, briefly, rather than refusing at once (#886). Nine rows
            # pasted together arrive within a second or two of each other;
            # bouncing seven of them told the user to "try again in a
            # moment", which is the one thing that risks the flood gate.
            if not _library_scans.acquire(timeout=LOOKUP_SCAN_WAIT_SECONDS):
                print(f"[DCC-LOOKUP] {user}'s request for {requested_file!r} refused: "
                      f"{MAX_CONCURRENT_LIBRARY_SCANS} library scans were still running after "
                      f"{LOOKUP_SCAN_WAIT_SECONDS:.0f}s.")
                announce.send_dcc_error(user, "busy")
                return
            try:
                # EVERY list, not just the master one. This is the lookup that
                # turns a bare "!<nick> Some.Film.mkv" into a path on disk, and
                # film and series moved into their own list file - so reading only
                # the master would leave every video in the library listed,
                # advertised and searchable, and impossible to actually get. The
                # split would have been a regression dressed as a feature.
                #
                # Concatenated rather than searched file by file: the scan below
                # reads folder headings and rows in order, and each list carries
                # its own headings above its own rows, so joining them end to end
                # leaves that state machine correct with nothing else changed.
                # Master first, so a name in both resolves the same way it did
                # before - the first copy the list names wins.
                # Resolved at the top of this function, so the list a name is
                # looked up in and the folders it is then looked for in cannot
                # disagree.
                list_paths = list_mod.all_list_paths(wanted_list)
                if list_paths:
                    try:
                        # STREAMED, NOT LOADED. This used to readlines() every
                        # published list into one Python list and then, on a match,
                        # walk BACKWARDS through it to the nearest folder heading.
                        # The only thing the whole list in memory was for was that
                        # backward walk. On a 5.4-million-file library that is
                        # 460 MB of text as ~5.9 GB of str objects, on EVERY file
                        # request - full_path below is "<first folder>/<name>" and a
                        # track is never in a folder's root, so the direct check
                        # fails and this runs each time. Measured live: a 5.9 GB
                        # peak and 1.5 GB held afterwards, because the allocator
                        # keeps its arenas. Three busy slots could mean three at
                        # once.
                        #
                        # Headings precede their rows, so "the nearest heading
                        # above the matching row" is simply the last heading seen
                        # on the way down. One variable carries it; nothing is kept.
                        # The heading is still resolved LAZILY, on a match only, so
                        # a miss costs exactly what it cost before minus the memory.
                        #
                        # One generator across every list, in order, so a `break`
                        # below leaves the whole lookup exactly as it left the old
                        # single loop over the concatenation - and the heading
                        # state carries across the file boundary the same way the
                        # concatenation carried it, which is what the comment above
                        # ("each list carries its own headings above its own rows")
                        # relies on.
                        def _list_lines(paths):
                            for one_list in paths:
                                with open(one_list, "r", encoding="utf-8",
                                          errors="ignore") as lf:
                                    for raw_line in lf:
                                        yield raw_line

                        # THE LIST'S OWN SPELLING TRAVELS WITH THE FOLDER (#445).
                        #
                        # The match below is case-insensitive, deliberately -
                        # list.find_duplicate_filenames() gives the reason in its
                        # own docstring: "a requester typing a name back cannot be
                        # expected to reproduce its case". What was missing is that
                        # the path was then rebuilt from what the REQUESTER typed,
                        # which is the one spelling known not to be the one on
                        # disk. On Linux that names a file that does not exist and
                        # the request is refused for a file the bot is publicly
                        # advertising; on Windows it resolves, and the file is
                        # offered and received under the requester's casing rather
                        # than the operator's.
                        target_folder = None
                        target_name = ""
                        fallback_folder = None
                        fallback_name = ""
                        clean_req = str(requested_file).lower().strip()
                        request_prefix = f"!{config.NICKNAME} "
                        # The most recent heading line, unresolved. ANY known
                        # prefix, not just the one we write: this is what
                        # RECOGNISES a heading, and checking only the current
                        # prefix stops seeing the headings in every list already
                        # in somebody's hands. Found by the test that counts
                        # resolutions.
                        last_heading = None

                        for line in _list_lines(list_paths):
                            line_clean = line.strip()
                            if any(line_clean.upper().startswith(p)
                                   for p in list_mod.LIST_FOLDER_PREFIXES):
                                last_heading = line_clean
                                continue
                            if not line_clean.startswith(request_prefix):
                                continue
                            # str.split() puts what came BEFORE the separator in
                            # [0], and the line starts with the separator - so [0]
                            # is the empty string on every line here. The filename
                            # is in [1]; the whole list lookup was dead code
                            # without it, leaving the os.walk() below to answer
                            # every request.
                            parts_nick = line_clean.split(request_prefix, 1)
                            rest_in_list = parts_nick[1].strip() if len(parts_nick) > 1 else ""

                            current_file_in_list, current_size_in_list = list_mod.strip_info_suffix(rest_in_list)

                            if clean_req != str(current_file_in_list).lower().strip():
                                continue
                            if last_heading is None:
                                continue
                            # The prefix-stripping itself is
                            # list_mod.resolve_list_folder() - this used to be a
                            # second, hand-written copy of it, which could drift
                            # from the original if the list format ever changed.
                            # No explicit base: the heading itself says which
                            # folder it belongs to once there is more than one
                            # (#164), and pinning it to base_directory would
                            # resolve every heading into the first.
                            found_folder = list_mod.resolve_list_folder(
                                last_heading, name=wanted_list)
                            if found_folder is None:
                                continue

                            # Two or more copies can share this exact name and
                            # differ only in size. Without a size hint, or if it
                            # matches nothing, the first copy the list names wins -
                            # same as before this change, and pinned by
                            # test_no_error_is_reported_for_a_duplicate. With one,
                            # a copy whose own ::INFO:: size matches it wins
                            # instead, so a request built from a search result's
                            # exact line reaches the copy that result actually
                            # named. A bare request (no hint - AutoQ.mrc and every
                            # existing caller) still stops at this first match
                            # exactly as before; only a hinted request that has
                            # not matched yet pays for scanning on, since that is
                            # the one case where the answer isn't already known.
                            if fallback_folder is None:
                                fallback_folder = found_folder
                                fallback_name = str(current_file_in_list).strip()
                                if not requested_size_hint:
                                    break
                            if requested_size_hint and current_size_in_list.lower().strip() == requested_size_hint:
                                target_folder = found_folder
                                target_name = str(current_file_in_list).strip()
                                break

                        if target_folder is None:
                            target_folder = fallback_folder
                            target_name = fallback_name

                        if target_folder is not None:
                            # target_name, not requested_file (#445): the list's
                            # spelling is the one that exists on disk, because the
                            # list was written from the disk. The row that matched
                            # is the row that names it.
                            test_path = os.path.join(target_folder, target_name)
                            if os.path.exists(platform_compat.long_path(test_path)):
                                full_path = test_path
                    except Exception as list_err:
                        print(f"[DCC-LOOKUP ERROR] {list_err}")
                if not os.path.exists(platform_compat.long_path(full_path)):
                    # Last resort, once the list lookup has not placed the file:
                    # walk for it. Each configured folder in turn, in the
                    # operator's order, so the same name in two of them resolves
                    # the way the list's own ordering already does.
                    for search_root in search_roots:
                        for root, dirs, files in os.walk(search_root):
                            if requested_file in files:
                                full_path = os.path.join(root, requested_file)
                                break
                        if os.path.exists(platform_compat.long_path(full_path)):
                            break
            finally:
                _library_scans.release()
            if not os.path.exists(platform_compat.long_path(full_path)):
                _note_lookup_miss(miss_key)
            else:
                # What it cost to find this is what the next request for it -
                # or for its siblings in the same folder - does not pay.
                _note_lookup_hit(miss_key, full_path)


        # Against every legitimate root rather than one. is_safe_path() itself
        # is unchanged - each comparison still resolves symlinks and compares
        # per separator - so what widened is which roots count as legitimate,
        # not how any one of them is tested. A path outside all of them is
        # still refused exactly as before.
        if not any(is_safe_path(root, full_path) for root in search_roots):
            announce.send_dcc_error(user, "invalid_path")
            return

        if not os.path.exists(platform_compat.long_path(full_path)) or os.path.isdir(platform_compat.long_path(full_path)):
            announce.send_dcc_error(user, "file_not_found")
            return

        file_name = os.path.basename(full_path)

        # The console feed (#528): the request is real and the file exists -
        # whether it sends now or queues is decided under the lock below, and
        # each of those reports itself (SENDING / QUEUED). A refused request
        # is reported by its refusal.
        announce.feed_event("REQUEST", f'{user} asked for "{file_name}"',
                            nick=user, channel=target_chan, kind="file", name=file_name)

        with queue_lock:
            total_global_queued = get_total_queued_count()
            user_queued_count = len(config.dcc_queue.get(user_key, []))

            if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                announce.send_dcc_error(user, "global_full")
                return

            if user_queued_count >= config.MAX_USER_QUEUE:
                announce.send_dcc_error(user, "user_full")
                return

            # Check whether this nick ALREADY has a send running, in active_transfers
            user_already_transferring = any(str(tx['user']).lower() == user_key for tx in config.active_transfers)
            
            # Create the temporary send lock in config if it is missing
            if not hasattr(config, 'user_processing_lock'):
                config.user_processing_lock = set()
                
            # If the user just sent rows, check whether the nick is locked in memory
            user_is_processing = user_key in config.user_processing_lock
            user_has_queue = len(config.dcc_queue.get(user_key, [])) > 0

            # Only a user who is clear in transfers, the queue AND the memory lock sends immediately
            # - and not while a rehash is quiescing (#668): this path does
            # not go through check_queue_and_send()'s gate, so a request
            # that reached here during the pause would have started a send
            # the reload then landed in the middle of. Read under the lock.
            sends_now = (not user_already_transferring and not user_is_processing
                         and not user_has_queue and len(config.active_transfers) < config.MAX_DCC_SLOTS
                         and not transfers_are_paused())
            if sends_now:
                # Lock the nick immediately, so the next row goes to the queue
                config.user_processing_lock.add(user_key)

                next_file_fake = {"path": full_path, "file": file_name, "channel": target_chan, "is_temporary_zip": False}
                config.active_transfers.append({"user": user, "file": file_name, "bytes_sent": 0, "next_file_obj": file_name})
            else:
                # This user already has a track running; the row goes to dcc_queue.txt
                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []
                config.dcc_queue[user_key].append({"file": file_name, "path": full_path, "channel": target_chan, "user_raw": user, "is_temporary_zip": False})
                user_pos = len(config.dcc_queue[user_key])

        # EVERYTHING THAT TOUCHES A DISK RUNS AFTER THE LOCK IS RELEASED (#605).
        # The claim above is what needs queue_lock. The SENDING notice
        # stat()s the library path for the console feed's byte count, and
        # save_dcc_queue() fsyncs dcc_queue.txt - FILE_DIRECTORY is allowed to
        # be an NFS mount, and a hung one blocks a stat forever. With that stat
        # inside the lock every thread that takes queue_lock froze behind it:
        # queue_worker samples live_speed() under it once a second, so no
        # outbound line of any kind went out, and the IRC read thread takes it
        # on every NICK, so the PONGs stopped and the server dropped the bot.
        # Outside the lock the hang is confined to this request's thread.
        if sends_now:
            if oserve: oserve.active_downloads = len(config.active_transfers)
            # Where "Sent:" goes, resolved the way the queued paths resolve
            # it (#658, audit M56). target_chan is the raw wire target, and
            # for a private request that is the bot's own nick: the
            # completion line went out as PRIVMSG <ournick>, cost a VIP slot,
            # and the read loop dropped it as our own message. #530 fixed
            # this for rows picked up from the queue via
            # announce_channel_for(); this path never went through it.
            announce_chan = announce_channel_for(next_file_fake)
            announce.send_dcc_sending_notice(user, file_name, path=full_path, channel=announce_chan)
            threading.Thread(target=start_dcc_send, args=(irc_sock, user, full_path, file_name, announce_chan, next_file_fake), daemon=True).start()
            return

        # Save and update dcc_queue.txt on disk straight away
        import db
        db.save_dcc_queue()

        announce.send_dcc_queue_notice(user, file_name, user_pos, channel=target_chan)
        return

    except Exception as e:
        print(f"[DCC ERROR] {e}")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1

# ------------------------------------------------------------- DCC ACKs
#
# THE RECEIVER SAYS WHAT IT HAS. After every packet a DCC receiver sends back
# a 4-byte big-endian running total of the bytes it holds, and that total is
# the ONLY signal of what arrived. sendall() says nothing about it: it returns
# the moment the kernel's send buffer accepts the bytes - 4 MB of them on
# Windows by default - which for most files is the whole file, instantly.
#
# Nothing here read those acknowledgements. "Complete" meant "written to the
# kernel", so the bot declared success at t~0, reported a speed measured
# against a memcpy ("Speed: n/a (<1s)"), counted the file, slept 1.5 seconds
# and CLOSED - with megabytes still queued behind a link doing 50 KB/s. The
# receiver was cut off at whatever the wire had managed and reported the
# transfer incomplete; the channel had already been told it was sent. Found
# by a second operator, reproduced with mIRC (#526). The operator's own
# workaround - DCC_SEND_BUFFER = 4096 - "worked" because a tiny buffer makes
# sendall() block on the actual wire, so the loop tracked delivery by
# accident. The bigger the buffer, the earlier the bot hung up.
#
# The counter is 32 bits and a file may not be, so it is tracked unwrapped:
# a new value below the low 32 bits of what we already hold means it wrapped.
# Acks are cumulative and never go backwards, so the tracker only advances.

ACK_STALL_SECONDS = 60.0     # no ack progress for this long = the link is dead


class _AckTracker:
    """The receiver's acknowledged byte count, parsed from whatever arrives."""

    def __init__(self, start=0):
        self.acked = int(start)
        # What has actually been handed to the kernel so far - the send loop
        # keeps this current. An acknowledgement cannot honestly exceed it
        # (#656, audit M54): the tracker used to accept any word above what
        # it held, so one 0xFFFFFFFF from a peer that read nothing satisfied
        # "acked >= file_size" for any file under 4 GB - "Sent:" announced,
        # totals and the download counter incremented, the queue row
        # consumed, at no bandwidth cost - and a client acking in the wrong
        # byte order (4096 -> 1 MB) was declared complete after one packet
        # and cut off. A word past `sent` is not a position the receiver can
        # hold; it is ignored and counted, and the transfer then lives or
        # dies on the real acks like any other.
        self.sent = int(start)
        self.overshoots = 0
        self.received_any = False
        self.eof = False
        self.last_advance_at = time.time()
        self._pending = b""

    def feed(self, data):
        """Absorb raw bytes from the data socket; b"" means the peer closed."""
        if not data:
            self.eof = True
            return
        self.received_any = True
        self._pending += data
        while len(self._pending) >= 4:
            (word,) = struct.unpack("!I", self._pending[:4])
            self._pending = self._pending[4:]
            self._advance(word)

    def _advance(self, word):
        base = self.acked & ~0xFFFFFFFF
        candidate = base | word
        if candidate < self.acked:
            # Below what we hold. Two things look like that and only the SIZE
            # of the drop tells them apart: a receiver past 4 GB whose 32-bit
            # counter wrapped (a drop of nearly 2**32), or a stale/duplicated
            # word (a small one). Serial-number arithmetic: more than half the
            # counter's range is a wrap; anything less is noise and is ignored,
            # because a cumulative total never genuinely goes backwards.
            if self.acked - candidate > (1 << 31):
                candidate += 1 << 32
            else:
                return
        if candidate > self.sent:
            self.overshoots += 1
            return
        if candidate > self.acked:
            self.acked = candidate
            self.last_advance_at = time.time()

    def stalled(self, now=None):
        return ((now if now is not None else time.time())
                - self.last_advance_at) > ACK_STALL_SECONDS


def _drain_acks(conn, tracker, wait=0.0):
    """Read whatever acknowledgements are waiting, without blocking the send.

    `wait` is how long to sit for one if none is there yet - 0 inside the send
    loop, a short pause while waiting for the final one. Never blocks longer.
    """
    try:
        readable, _, _ = select.select([conn], [], [], wait)
    except (OSError, ValueError):
        tracker.eof = True
        return
    if not readable:
        return
    try:
        data = conn.recv(4096)
    except socket.timeout:
        return
    except OSError:
        tracker.eof = True
        return
    tracker.feed(data)


def _wait_for_final_ack(conn, tracker, file_size):
    """Sit until the receiver has acknowledged the whole file, or has clearly
    stopped. Returns the moment the final ack arrived, or None.

    Bounded by PROGRESS, not by a fixed clock: a slow link that is still
    advancing is allowed to finish, and a link that has not advanced for
    ACK_STALL_SECONDS is dead whether or not bytes are still queued in the
    kernel. The old fixed sleep(1.5) was the wrong shape entirely - it gave a
    50 KB/s link 75 KB of the 2.7 MB it still had to deliver.
    """
    while tracker.acked < file_size:
        if tracker.eof or tracker.stalled():
            return None
        _drain_acks(conn, tracker, wait=0.25)
    return tracker.last_advance_at


def _find_transfer_row(user, file_name):
    """The active_transfers row a send was started for, or None.

    The dispatcher appends {"user", "file", ...} just before it starts the send
    thread. Matched by nick and file, newest first; a nick alone is enough when
    it is the only row that nick has (a pack's row is filed under the archive's
    name, which is not always the name the send is handed).
    """
    key = str(user).lower()
    with queue_lock:
        rows = [tx for tx in config.active_transfers
                if str(tx.get('user', '')).lower() == key]
    for tx in reversed(rows):
        if tx.get('file') == file_name or tx.get('next_file_obj') == file_name:
            return tx
    return rows[-1] if len(rows) == 1 else None


def start_dcc_send(irc_sock, user, file_path, file_name, channel, next_file):
    """Handle the network ports and the CTCP, and stream the bytes with accurate timing."""
    # Every failure this transfer reports carries the channel it was asked
    # for in, for the structured feed (#550).
    def report_failure(*args, **kwargs):
        _report_transfer_failure(*args, channel=channel, **kwargs)

    # No `global active_transfers` here: there is no module-level name of that
    # kind in this file, and there never was. Every real use below is
    # config.active_transfers, which needs no declaration. The statement was
    # inert, and inviting: it read as though this function mutated a module
    # global somebody could go looking for (#232).
    import time
    import os
    import socket
    import sys
    import threading
    
    # If file_path or file_name happen to be dictionaries, pull the real strings out
    if isinstance(next_file, dict):
        if not isinstance(file_path, str) or "{" in str(file_path):
            file_path = next_file.get('path', str(file_path))
        if not isinstance(file_name, str) or "{" in str(file_name):
            file_name = next_file.get('file', str(file_name))

    # Addressed through long_path so a deep library path reports its real
    # size instead of 0 - the DCC offer carries this number, and a 0 makes
    # the receiver close immediately.
    _size_probe = platform_compat.long_path(file_path) if isinstance(file_path, str) else None
    file_size = os.path.getsize(_size_probe) if (_size_probe and os.path.exists(_size_probe)) else 0
    # The dashboard's queue progress bar needs a total to measure bytes_sent
    # against - bytes_sent was already kept live on this row (see the send
    # loop below), but nothing recorded what it was a fraction OF. Matched by
    # user, the same way the send loop below updates bytes_sent: only one
    # transfer runs per user at a time, so this is unambiguous.
    # #598: WHICH ROW IS THIS TRANSFER'S. The dispatcher appended one to
    # config.active_transfers under the nick the send started as, and every
    # lookup below used to find it by that nick again. A /nick in the middle
    # of the send (irc.note_nick_change() rewrites the row's user and moves
    # the in-progress lock to the new name) left every one of those lookups
    # searching for a name nobody had any more: the row and the lock outlived
    # the transfer for good, a slot was gone until a restart, the renamed user
    # was locked out, and bytes_sent stopped updating. The row is held by
    # identity instead; the nick is only what to look it up by once, here.
    _row = _find_transfer_row(user, file_name)

    def _mine(tx):
        if _row is not None:
            return tx is _row
        return str(tx.get('user', '')).lower() == user.lower()

    def _my_nick():
        # Whatever name the transfer is filed under NOW - the lock moved with it.
        return str((_row or {}).get('user') or user).lower()

    for tx in config.active_transfers:
        if _mine(tx):
            tx['size'] = file_size
    ip_long = get_public_ip_long()
    start_time = time.time()
    bytes_sent = 0
    # Only a send loop that ran to completion counts as delivered. A socket timeout, a
    # refused connection or a half-finished stream must not consume the queue row.
    transfer_completed = False

    # #430: `irc_sock` is whatever socket was live when the caller captured
    # it - which can be several minutes stale by the time this actually
    # runs. user_queue_timer waits up to 300s for the user to reappear,
    # inline_rar_packer can spend minutes packing an album, and neither
    # re-checks whether a reconnect has since torn down that socket and
    # opened a new one. Every dispatch path converges here, so this is the
    # one place worth asking what is ACTUALLY live right now, rather than
    # trusting what was threaded through four different call sites.
    #
    # Re-bound rather than only checked: every irc_sock.send() further down
    # in this function - the handshake, every error NOTICE - should use the
    # current connection too, not the one this thread was started with.
    oserve_mod = sys.modules.get('oserve')
    live_sock = getattr(oserve_mod, 'irc_connection', None)

    if live_sock is None:
        # No live connection at all right now - not this queue entry's
        # fault, exactly like "no usable public address" below, and for the
        # same reason: it affects every queued user identically, nothing
        # about retrying THIS row fixes it, and there is nothing to send a
        # NOTICE of the problem over in the first place. Left untouched
        # rather than charged a retry: the next real trigger (a reconnect's
        # NAMES thaw, a completion, a JOIN) re-selects it normally.
        print(f"[DCC HOLD] No live IRC connection right now - holding "
              f"{user}'s queue rather than dispatching into a dead socket.")
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(_my_nick())
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers
                                          if not _mine(tx)]
        return

    irc_sock = live_sock

    # is_offerable_to_strangers() is the address half; ip_long == 0 only catches
    # a blank or malformed value, and a loopback address passes it (127.0.0.1 is
    # 2130706433). See that function for what went wrong without it.
    if ip_long == 0 or file_size == 0 or not is_offerable_to_strangers():
        # Two very different failures used to share one message. "File access
        # issue or empty payload. Please try again." is actively misleading for
        # the address case: there is nothing wrong with the file, retrying
        # cannot help, and only the operator can fix it.
        if ip_long == 0 or not is_offerable_to_strangers():
            reason = ("this bot has no usable public address configured, so it "
                      "cannot offer a transfer")
            print(f"[DCC CRITICAL ABORT] No usable public address "
                  f"(MY_IP_OR_DOCK={getattr(config, 'MY_IP_OR_DOCK', '')!r}); refused "
                  f"the send for {user} rather than offering one nobody can dial. "
                  f"Set MY_IP_OR_DOCK in admin_config.py. Not settings.conf "
                  f"(#465): this address is detected at startup rather than "
                  f"read from a file, so it is not a setting that file carries.")
        else:
            reason = "file access issue or empty payload. Please try again"
            print(f"[DCC CRITICAL ABORT] Aborted the send for {user}. "
                  f"Path: {file_path} (Size: {file_size})")
        try: 
            msg = f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} {reason}.\r\n"
            irc_sock.sendall(msg.encode('utf-8', errors='ignore'))
        except: 
            pass
            
        # Clear the locks and wait three seconds, to stay well clear of Excess Flood.
        # Only clear rar_inprogress if THIS send owns it - a plain audio file never
        # did, and clearing a flag another user's pack is holding is how the
        # interlock leaks (see the identical guard a few lines down, at the port-
        # exhaustion branch, which already got this right).
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(_my_nick())
            
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if not _mine(tx)]
            
        # This abort returns BEFORE the try/finally that settles the queue row, so without
        # this call the same unreadable entry was re-selected every ~3 seconds forever,
        # with no counter and no way out - a permanent hot loop injecting a NOTICE and a
        # VIP notice on every pass. Charging it to the retry budget bounds it.
        # WHOSE FAULT IT IS DECIDES WHETHER IT COSTS THE USER A RETRY.
        #
        # This branch covers two unrelated things. A file that is missing or
        # empty is a dead row: the library moved on, no amount of retrying
        # brings it back, and charging the budget is what stops it being
        # re-selected every three seconds forever - the reason this call was
        # added.
        #
        # "No usable public address" is not that. It is the BOT's own
        # configuration, it affects every queued user identically, and it is
        # fixed by the operator setting MY_IP_OR_DOCK - not by the user
        # waiting. Charging it meant three attempts silently discarded a
        # perfectly good queue, and the fastest way to make three attempts
        # happen is three !rehash runs: each one wakes the queue, each wake
        # fails the same way, and the third deletes the row. Reported from a
        # live install - "if the bot had some queues from a user, and admin made a
        # rehash, it cancels the queue".
        #
        # The hot loop this call exists to bound is still bounded: with no
        # public address there is nothing to select and check_queue_and_send()
        # below is not called again, so the row simply waits.
        if ip_long == 0 or not is_offerable_to_strangers():
            print(f"[DCC QUEUE] {user}'s queue is untouched - the address, not "
                  f"the file, is what is missing. Nothing is retried until "
                  f"MY_IP_OR_DOCK is set.")
            if hasattr(config, 'user_processing_lock'):
                config.user_processing_lock.discard(_my_nick())
            return

        release_queue_entry(user, next_file, delivered=False,
                            reason="file missing or empty")

        time.sleep(3.0)
        check_queue_and_send(irc_sock, user)
        return


    dcc_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR means the OPPOSITE thing on Windows - it lets another process
    # bind this same port and take the incoming connection, which on a DCC
    # listener is a hijack. platform_compat picks the right option per platform.
    platform_compat.prepare_listener(dcc_sock)
    
    assigned_port = None
    for port in range(config.DCC_PORT_START, config.DCC_PORT_END + 1):
        try:
            dcc_sock.bind(('0.0.0.0', port))
            assigned_port = port
            break
        except socket.error:
            continue

    if assigned_port is None:
        try: irc_sock.sendall(f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} No available DCC ports.\r\n".encode("utf-8", errors="ignore"))
        except: pass
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if not _mine(tx)]

        # Port exhaustion is TRANSIENT and is not this entry's fault, so it is deliberately
        # NOT charged to the retry budget - a busy spell must not discard good queued files.
        # The interlocks are released so the user is not left wedged, and the row stays
        # queued for the next completion trigger. Re-triggering here would spin: the ports
        # are still full one instruction later.
        # Only clear rar_inprogress if THIS send owns it - a plain audio file never did, and
        # clearing a flag another user's pack is holding is how the interlock leaks.
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(_my_nick())
        # #162 finding #8: this branch's own comment above says the row
        # stays queued for the next completion trigger - deleting the
        # archive it still points at contradicted that in the same breath.
        # A second user's row pointing at the SAME shared archive (see
        # discard_orphaned_temp_archives()'s own comment on why two users
        # requesting the same real album share one file) was left dangling,
        # and the retry 45s later hit the file_size == 0 critical abort,
        # which classifies a consumed temporary archive as non-retryable
        # and drops the row - the album was lost by the one branch whose
        # stated purpose was not to lose it. Nothing is removed here now;
        # if the artifact is being preserved, the file backing it must be
        # too.

        # The row stays queued and is not charged a failure, but on an otherwise idle bot
        # nothing else would ever wake it. One bounded delayed retry, no tight spin.
        def delayed_port_retry():
            time.sleep(45)
            check_queue_and_send(irc_sock, user)
        threading.Thread(target=delayed_port_retry, daemon=True).start()

        print("[DCC PORTS] No free DCC port for " + str(user) + "; entry stays queued, retrying in 45s.")
        # The listener is closed HERE and not anywhere else. Every path from the
        # try: below runs through a finally: that closes it, but this branch
        # returns before reaching that try - so the socket created a few lines
        # above was leaked, once per refused request, on a path that retries
        # every 45 seconds. On a bot whose ports are genuinely exhausted that is
        # a steady file-descriptor leak for as long as the condition lasts,
        # which is exactly when the daemon can least afford one.
        try:
            dcc_sock.close()
        except Exception:
            pass
        return

    conn = None
    # Set by the accept branch below and read by the finally, so the notice
    # the user gets can say what happened rather than "did not complete".
    never_connected = False
    try:
        # settimeout() and listen() USED TO SIT ABOVE this try - the one whose
        # finally is the only thing that releases the slot and closes this
        # socket. The CALLER appends the transfer to config.active_transfers
        # before calling us (all three dispatch sites in check_queue_and_send,
        # plus the direct path), so an OSError out of listen() - EMFILE when
        # the process is out of file descriptors, or the kernel refusing the
        # backlog - killed this thread with the row still in the list and the
        # listener still open.
        #
        # Nothing ever revisits that row. It names a transfer that is not
        # happening, and no completion will fire to remove it: one permanent
        # slot gone out of MAX_DCC_SLOTS, cumulative, until the daemon is
        # restarted. The conditions that make listen() fail are exactly the
        # ones where losing serving capacity hurts most.
        #
        # listen() has to stay AHEAD of the handshake - the handshake is what
        # tells the peer to connect, and a peer dialling before we listen gets
        # a refusal - so the whole block moved inside the try rather than the
        # two calls moving down past it.
        accept_window = accept_timeout()
        dcc_sock.settimeout(accept_window)
        dcc_sock.listen(1)
    
        safe_file_name = file_name.replace(" ", "_")
        # The handshake is a PRIVMSG like any other: the server prepends our
        # ":nick!ident@host " when relaying it, and the whole thing has to fit
        # inside 512 bytes. A non-ASCII filename costs 2-3 bytes per character,
        # so a ~150-character Chinese or Japanese title overruns that on its own -
        # and the fields the transfer actually needs (address, port, size) sit
        # AFTER the name, so the server's cut takes THOSE and the receiver is
        # handed a handshake it cannot act on. Trimming the name ourselves costs a
        # shortened save-name; not trimming it costs the transfer.
        ctcp_handshake = announce.fit_irc_filename(
            lambda offered: (f"PRIVMSG {user} :\x01DCC SEND {offered} "
                             f"{ip_long} {assigned_port} {file_size}\x01\r\n"),
            safe_file_name)
        if safe_file_name not in ctcp_handshake:
            # Say so rather than letting the receiver silently save it under a
            # name the operator never chose and cannot find in their own library.
            print(f"[DCC] Offered filename shortened to fit the IRC line: {file_name!r}")
    
        # Registered BEFORE the send, not after: a receiver holding a partial
        # file answers with a DCC RESUME the moment the offer lands, and that
        # answer arrives on the IRC read loop - a different thread from this
        # one, which is about to block in accept(). An entry that appeared a
        # moment later would miss it, and the receiver would sit waiting for
        # an ACCEPT that never came.
        register_send_offer(user, assigned_port,
                            offered_name_from_handshake(ctcp_handshake),
                            file_size)

        try:
            irc_sock.sendall(ctcp_handshake.encode("utf-8", errors="ignore"))
            print(f"[DCC-LISTEN] Listening on port {assigned_port} for {user} (Handshake sent directly).")
        except Exception as e:
            # #430: this used to fall straight through into accept() below,
            # which then blocked for the full 30s listener timeout waiting
            # for a receiver who was never actually told to connect - the
            # offer never reached them, so there was never anyone coming.
            # Three of those and MAX_SEND_FAILS deletes the row with "Could
            # not send" over the very connection that just failed. Returning
            # here instead lets the enclosing finally release the slot and
            # the interlocks immediately rather than half a minute later.
            print(f"[DCC ERROR] Failed to send the handshake: {e}")
            return

        try:
            conn, addr = dcc_sock.accept()
        except socket.timeout:
            # Nobody connected inside the window. Its own branch, because the
            # `except socket.timeout` at the bottom of this try belongs to the
            # send loop and described this as the receiver "not draining"
            # a send that never started (#879). Said as what it is, with the
            # file size so the feed reads "0B of 626MB" rather than "0B of
            # 0B"; the enclosing finally settles the row and the slot as for
            # any other failure.
            report_failure(user, file_name, never_connected_reason(accept_window),
                           acked=0, total=file_size)
            never_connected = True
            oserve = sys.modules.get('oserve')
            if oserve: oserve.send_fails_count += 1
            return
        conn.settimeout(60.0)
        dcc_sock.settimeout(None)
        
       # Push the packets out immediately rather than letting them coalesce
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Only when asked for. Setting SO_SNDBUF disables the OS's own
        # auto-tuning on both platforms, so an unrequested value would be a
        # silent downgrade on every link the operator did not measure. See
        # config.DCC_SEND_BUFFER for what it is for.
        _apply_send_buffer(conn)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        print(f"[DCC-CONNECT] {user} connected from {addr}!")

        start_time = time.time()

        # WHERE TO START, agreed before the connection was made. The offer is
        # dropped here rather than in the finally: a RESUME arriving after the
        # receiver has already connected is answering a question nobody is
        # waiting on, and leaving the entry up would let a second one move an
        # offset this transfer has already read. The finally still calls it,
        # for every path that never got this far.
        resume_offset = int((clear_send_offer(user, assigned_port)
                             or {}).get("position") or 0)
        # The receiver acknowledges ABSOLUTE positions, so a resume starts the
        # tracker at the offset it already holds and the completeness check
        # below compares against the whole file, exactly as bytes_sent does.
        acks = _AckTracker(start=resume_offset)
        # The row the dispatcher appended carries user/file/bytes_sent; the
        # console's SLOT line (#550, step 3) wants the total and a speed, so
        # the size and the start moment go on the same row here, where both
        # are known. Read-only for everything else.
        for tx in config.active_transfers:
            if _mine(tx):
                tx['size'] = file_size
                tx['started_at'] = time.time()
        if resume_offset:
            # bytes_sent counts what the RECEIVER ends up holding, so the
            # completeness check below still compares against the whole file.
            # What this transfer actually put on the wire is tracked
            # separately, because a resumed send that skipped 4 GB did not
            # move 4 GB and must not be allowed to claim it in the speed
            # record the channel advert publishes.
            bytes_sent = resume_offset
            for tx in config.active_transfers:
                if _mine(tx):
                    tx['bytes_sent'] = resume_offset
                    # Where THIS connection started, so a speed can be told from
                    # the row: bytes_sent counts what the receiver holds, the
                    # skipped part included (#746).
                    tx['resume_offset'] = resume_offset
            print(f"[DCC-RESUME] Resuming {file_name} for {user} at byte "
                  f"{resume_offset} of {file_size}.")

        with open(platform_compat.long_path(file_path), 'rb') as f:
            if resume_offset:
                f.seek(resume_offset)
            # mIRC's "packet size", and the reason raising it there is
            # noticeable: mIRC defaults to 4 KB and this has always been 64.
            # Resolved once per transfer, not once per pass - the value cannot
            # change mid-file, and a getattr in the inner loop of a 4 GB send
            # is a million lookups for one answer.
            block = dcc_block_size()
            while True:
                chunk = f.read(block)
                if not chunk: break
                try:
                    conn.sendall(chunk)
                    bytes_sent += len(chunk)
                    acks.sent = bytes_sent
                except socket.error as e:
                    raise e
                # Read whatever the receiver has acknowledged so far, without
                # waiting for it - the sends must not sit behind the acks any
                # more than the acks should be ignored. A receiver that has gone
                # quiet for ACK_STALL_SECONDS while bytes are outstanding is
                # dead, and this is the first place that can tell.
                _drain_acks(conn, acks)
                if acks.eof and acks.acked < bytes_sent:
                    raise _ReceiverGone(acks.acked)
                if acks.stalled() and acks.acked < bytes_sent:
                    raise _ReceiverStalled(acks.acked)
                for tx in config.active_transfers:
                    if _mine(tx):
                        tx['bytes_sent'] += len(chunk)
                oserve = sys.modules.get('oserve')
                if oserve: oserve.total_sent_bytes += len(chunk)
                
        # COMPLETE MEANS ALL OF IT. The loop above ends on local EOF, which
        # says the file stopped giving bytes - not that it gave as many as the
        # handshake promised. file_size was read with os.path.getsize() before
        # the offer went out, and the receiver uses that same figure to decide
        # when the transfer is done, so a short send leaves it waiting for
        # bytes that are never coming.
        #
        # A file replaced by a shorter one mid-send is the ordinary way this
        # happens: a re-encode, a library tidy-up, or an NFS mount going away
        # under the read - which is why the request path already has its own
        # NFS guards. Before this, the short send was recorded as a COMPLETED
        # transfer: counted in the totals, credited to the download counter,
        # and the queue row deleted, so nothing would ever retry it.
        # The loop above ends on LOCAL EOF, which says the file stopped giving
        # bytes and that the kernel accepted them - not that the receiver has
        # them. Completion is the receiver's final acknowledgement equalling
        # the file size, and it is waited for here, bounded by progress. The
        # clock stops when THAT arrives: that is the transfer, and it is what
        # the speed record, the advert and the counters are a record of.
        short_read = bytes_sent < file_size
        if short_read:
            report_failure(user, file_name,
                f"sent {bytes_sent:,} of {file_size:,} bytes before the file ended. "
                f"Recorded as a failure rather than a completed transfer.",
                acked=acks.acked, total=file_size)
            final_ack_at = None
        else:
            final_ack_at = _wait_for_final_ack(conn, acks, file_size)
        transfer_completed = final_ack_at is not None
        if not transfer_completed and not short_read:
            if not acks.received_any:
                report_failure(user, file_name,
                    "the receiver never acknowledged a single byte, so there "
                    "is no evidence any of it arrived. Not counted. (A DCC "
                    "receiver acknowledges every packet; one that sends none "
                    "cannot be told apart from one that got nothing.)",
                    acked=0, total=file_size)
            elif acks.eof:
                report_failure(user, file_name,
                    f"the receiver closed the connection having acknowledged "
                    f"{acks.acked:,} of {file_size:,} bytes. Not counted.",
                    acked=acks.acked, total=file_size)
            else:
                report_failure(user, file_name,
                    f"the receiver stopped acknowledging at {acks.acked:,} of "
                    f"{file_size:,} bytes and made no progress for "
                    f"{int(ACK_STALL_SECONDS)}s. The link is dead; whatever the "
                    f"kernel still held will not arrive. Not counted.",
                    acked=acks.acked, total=file_size)
        # THE CLOCK STOPS WHEN THE BYTES DO. Everything below this line is
        # settling: 1.5 seconds for the receiver to close its file calmly,
        # another half-second further down, and the statistics write. None of
        # it is transfer time, and all of it used to be counted as transfer
        # time because the duration was measured from start_time at the very
        # end of the function.
        #
        # Two seconds of it, against files that mostly take less than that.
        # A 10 MB file at 46 MB/s takes 0.22s and was reported at 4.5 MB/s -
        # a tenth of the real rate - which is what "it feels slower than
        # mIRC" turned out to mean. The transfer was never slow; the number
        # was. It also fed the speed RECORD and the advert, so the figure the
        # channel saw was wrong in the same direction.
        transfer_finished_at = final_ack_at if transfer_completed else time.time()
        if transfer_completed:
            print(f"[DCC-SUCCESS] Sent the whole file to {user}; the receiver "
                  f"acknowledged all {file_size} bytes.")
        # The 1.5-second "let mIRC close its file calmly" pause is gone: the
        # receiver's final ack IS it telling us it has everything, and the
        # wait above already returned on that. Sleeping after it only held a
        # DCC slot for nothing.
 
        # ---------------------------------------------------------------------
        # Update the statistics on disk
        # ---------------------------------------------------------------------
        try:
            import db
            # ONE locked read-modify-write inside db, instead of a load here, a
            # mutate here, and a separate save here. MAX_DCC_SLOTS transfers
            # finish concurrently, and the unsynchronised version let whichever
            # thread saved second discard the other's increment - permanently,
            # since nothing ever recomputes these counters. It also rotates the
            # day, so a transfer completing after midnight is counted correctly.
            # What went out on the wire, not what the receiver now holds. A
            # resumed send completes a whole file while transferring only the
            # tail of it, and the totals are a record of bytes SENT.
            # #454: ONLY A COMPLETED TRANSFER COUNTS. transfer_completed was
            # already being computed a few lines above, and a short send
            # already printed [DCC-FAIL] - but the totals, the per-file
            # download counter and the "Sent the whole file" line all ran
            # regardless. So a truncated send was reported as a failure in
            # one line and recorded as a success in every place an operator
            # or another bot would later read: the lifetime file count, the
            # bytes total, the speed record that feeds the advert, and the
            # most-downloaded list.
            #
            # Counting a partial send also inflates the speed figure, since
            # the elapsed time covers a transfer that stopped early.
            if not transfer_completed:
                raise _ShortSend()
            stats = db.update_stats_on_complete(file_size - resume_offset)
            print(f"[DB COUNTER] Statistics updated on disk. (Files sent: {stats[0]})")

            # And count the item itself, for the Stats page's "Most
            # downloaded". A SEPARATE _disk_lock acquisition, after the one
            # above has been released: threading.Lock is not reentrant, and
            # this file is not the stats row - see db.py's note on why every
            # public entry point there takes the lock exactly once.
            #
            # An album goes out as a packed archive from TMP_ZIP_DIR whose
            # name dcc.py built from the folder, so the archive's own name is
            # already the readable one. A single file is keyed by its path
            # relative to the library and only displayed by its basename:
            # two albums can hold a track with the same filename (#110), and
            # collapsing them would credit one track with another's
            # downloads.
            db.record_download(*download_count_identity(file_path, file_name))
        except _ShortSend:
            print(f"[DB COUNTER] Not counted: {file_name} for {user} ended "
                  f"short at {bytes_sent} of {file_size} bytes. A partial send "
                  f"is not a completed transfer, so it is left out of the "
                  f"totals, the download counter and the speed record.")
        except Exception as db_err:
            print(f"[DB ERROR] Could not increment the sharing statistics through the db module: {db_err}")
        # ---------------------------------------------------------------------

        try: conn.close()
        except: pass

    except _ReceiverGone as gone:
        report_failure(user, file_name,
            f"closed the connection mid-transfer, having acknowledged "
            f"{gone.acked:,} of {file_size:,} bytes.",
            acked=gone.acked, total=file_size)
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except _ReceiverStalled as stalled:
        report_failure(user, file_name,
            f"stopped acknowledging at {stalled.acked:,} of {file_size:,} bytes "
            f"and made no progress for {int(ACK_STALL_SECONDS)}s; giving up on "
            f"a dead link.",
            acked=stalled.acked, total=file_size)
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except socket.timeout:
        # FIXED (issue #30): previously silent. This is the SEND side blocking:
        # the kernel buffer is full and the peer has not drained it within the
        # socket timeout. The old message said "no data acknowledged", which
        # was never what it measured - nothing read acknowledgements then. The
        # ack-based stall above is that check; this one is the write stalling.
        report_failure(user, file_name,
            "the send blocked for the whole socket timeout with the receiver "
            "not draining it.", acked=acks.acked, total=file_size)
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except Exception as e:
        # FIXED (issue #30): `e` was captured and never used. A connection reset, a broken
        # pipe, or any other mid-transfer failure produced the exact same silence - no way
        # to tell which one happened after the fact, especially once the temp archive is
        # already deleted and the queue row already gone.
        report_failure(user, file_name, f"{type(e).__name__}: {e}")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    finally:
        # Never leave an offer standing. The success path already dropped it
        # the moment the receiver connected; this covers every other exit -
        # the port-refusal return above, a listen() that raised, an accept()
        # that timed out because nobody ever came. A stale entry is not
        # harmless: the ports it is keyed by are reused, so the next offer on
        # the same port to the same nick could read an offset agreed for a
        # different file and send it from the middle.
        try:
            clear_send_offer(user, assigned_port)
        except Exception:
            pass

        # There used to be a 0.5 s sleep here "to give the network buffer time
        # to flush the final acknowledgement" - a pause standing in for the ack
        # that nothing read. The ack is read now, and a completed transfer
        # only reaches this point once it has arrived; a failed one has
        # nothing to wait for. Sleeping here only held the DCC slot half a
        # second longer on every exit path.

        # The real-time speed counter
        # transfer_finished_at is the moment of the receiver's final
        # acknowledgement (#526) - or, for a send that fell short, the moment
        # the wait for it gave up. It only exists on the path that reached
        # the completion check, so the fallbacks below cover the abort paths
        # that arrive here without one.
        _ended = (transfer_finished_at if 'transfer_finished_at' in locals()
                  else time.time())
        acute_duration = _ended - (start_time if 'start_time' in locals() else _ended)
        if acute_duration <= 0:
            acute_duration = 0.1
            
        # Minus whatever the receiver already had. Counting a resumed send's
        # skipped bytes as though this transfer moved them would post a speed
        # record that never happened - and that number feeds the channel
        # advert, which is the same mistake the timing comment above describes.
        _skipped = resume_offset if 'resume_offset' in locals() else 0
        acute_bytes = max(0, (bytes_sent if 'bytes_sent' in locals() else 0) - _skipped)
        final_calc_speed = int(acute_bytes / acute_duration)

        # WHAT WE ARE WILLING TO SAY IT WAS. The figure above divides bytes by
        # the time it took to hand them to the kernel, which is the same thing
        # as the transfer only for a file bigger than the socket send buffer.
        # A smaller one is copied into the buffer in one go and the clock
        # measures memory - a list zip was reported at 138 MB/s that way.
        #
        # None rather than a number when it cannot be measured, so the two
        # places that display it say so instead of stating a figure nobody
        # should act on. The RECORD has always refused these samples; this is
        # the same judgement applied to what is shown.
        stats_mgr_speed = sys.modules.get("stats_mgr")
        if stats_mgr_speed is None:
            import stats_mgr as stats_mgr_speed
        reported_speed = (final_calc_speed
                          if stats_mgr_speed.speed_is_measurable(acute_duration, file_size)
                          else None)

        # The record the channel advert publishes. db has had
        # save_speed_record() from the start and announce.py has read it into
        # every advert since, but nothing ever sat between the two - the only
        # callers of the writer were tests. So the advert has shown
        # "Record: 0k/s" for the life of the feature, on every install.
        #
        # Only a transfer that actually completed counts: a send that failed
        # part-way has moved real bytes in real time, so its rate looks like a
        # legitimate sample and is not one.
        if transfer_completed:
            try:
                import stats_mgr as stats_mgr_mod
                stats_mgr_mod.update_speed_record(final_calc_speed, acute_duration)
            except Exception as record_err:
                print(f"[STATS ERROR] Could not update the speed record: {record_err}")

        # 1. Clean up the transfer and the slot immediately
        try:
            with queue_lock:
                config.active_transfers[:] = [tx for tx in config.active_transfers if not _mine(tx)]
                oserve = sys.modules.get('oserve')
                if oserve: oserve.active_downloads = len(config.active_transfers)
        except Exception as trans_clean_err:
            print(f"[DCC CLEANUP ERROR] Could not clear active_transfers in memory: {trans_clean_err}")


        # 2. Get the channel notice out first of all
        try:
            import announce as announce_mod
            # Only announce a transfer that actually completed. A failed attempt now keeps
            # its queue row for retry, so announcing here would tell the channel "Sent" and
            # re-offer the same file on every attempt.
            if transfer_completed:
                announce_mod.send_transfer_complete(channel, user, file_name, file_size, start_time, reported_speed,
                                                    duration=acute_duration)
        except Exception as ann_chan_err:
            print(f"[ANNOUNCE CHANNEL ERROR] Could not send the channel notice: {ann_chan_err}")

        # 3. Close the network socket safely
        try: conn.close()
        except: pass
        try: dcc_sock.close()
        except: pass
        # 4. SETTLE THE QUEUE ROW - by identity, with a retry budget. This replaces an
        #    unconditional pop(0), which removed whatever was first at that instant rather
        #    than the entry actually sent. See release_queue_entry.
        #
        #    BEFORE the disk cleanup, since #657: the cleanup used to run first and
        #    delete a packed archive whose send had just failed, and the settle then
        #    found the archive gone and dropped the row - one 30-second accept window
        #    for a pack that took up to RAR_TIMEOUT to build. Settled first, a failed
        #    pack keeps its row for MAX_SEND_FAILS like a plain file, and the cleanup
        #    below keeps the archive for a row that was kept.
        row_retained = False
        try:
            if transfer_completed:
                settled_reason = "transfer complete"
            elif never_connected:
                settled_reason = never_connected_advice()
            else:
                settled_reason = "transfer did not complete"
            row_retained = release_queue_entry(
                user, next_file, delivered=transfer_completed,
                reason=settled_reason)
        except Exception as pop_err:
            print("[DCC CLEANUP ERROR] Could not settle the queue row: " + str(pop_err))

        # 5. The RAR cache and the send lock
        try:
            # A row kept for retry needs its archive (#657).
            file_still_needed = bool(row_retained)
            safe_path = str(file_path)

            if _is_temp_zip_cache_file(safe_path):
                with queue_lock:
                    # A. Is the file still QUEUED for some OTHER user in dcc_queue.txt?
                    for q_user, q_files in getattr(config, 'dcc_queue', {}).items():
                        if q_user.lower() != user.lower():
                            for q_obj in q_files:
                                if isinstance(q_obj, dict) and (q_obj.get('file') == file_name or q_obj.get('path') == file_path):
                                    file_still_needed = True
                                    break
                    
                    # B. Is the file still being sent ACTIVELY to somebody in another slot?
                    active_matches = 0
                    for tx in getattr(config, 'active_transfers', []):
                        if tx.get('file') == file_name:
                            active_matches += 1
                    
                    if active_matches > 0:
                        file_still_needed = True

                # If no other user and no active slot still needs it, delete it from disk
                if not file_still_needed:
                    if os.path.exists(platform_compat.long_path(file_path)):
                        os.remove(file_path)
                        print(f"[DCC CLEANUP] Safely deleted the temporary archive from disk: {file_name}")
        except Exception as file_rm_err:
            print(f"[DCC CLEANUP ERROR] Could not run the disk cleanup: {file_rm_err}")
        
        # 6. Release the memory lock and rule out duplicate threads.
        # #162 finding #6: a plain audio file never owned rar_inprogress - this ran
        # unconditionally on EVERY transfer's completion, so bob's ordinary MP3
        # finishing could clear the flag while alice's pack (queued as a separate
        # slot, holding the interlock for the whole duration of its own pack) was
        # still running, losing packer serialisation. Same guard as the critical-
        # abort and port-exhaustion branches above.
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            try:
                redispatch_waiting_pack(irc_sock, just_finished=user)
            except Exception as wake_err:
                # Guarded like every other step of this finally (#606): an
                # exception here skipped the lock release and the fallback
                # trigger below, and the finishing user stayed "already
                # claimed elsewhere" until a rehash.
                print("[DCC CLEANUP ERROR] Could not wake a waiting pack: " + str(wake_err))
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(_my_nick())

        # 7. Wake the queue automatically after three seconds, thread-safely
        # A retained row means the attempt FAILED and will be retried. Reusing the flat
        # 3-second completion timer as the retry timer would hammer a broken entry three
        # times in nine seconds, each pass emitting a NOTICE and a VIP notice while the VIP
        # queue drains slower than that. Back off instead.
        retry_delay = 3
        if row_retained and isinstance(next_file, dict):
            retry_delay = 15 * max(1, int(next_file.get('send_fails', 1)))

        def delayed_queue_trigger_fallback():
            time.sleep(retry_delay)
            check_queue_and_send(irc_sock, user)
        threading.Thread(target=delayed_queue_trigger_fallback, daemon=True).start()
