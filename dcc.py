# =====================================================================
# DCC.PY - THE TRANSFER ENGINE
# =====================================================================
import socket
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
        if isinstance(named, str) and named.strip():
            return named.strip()
    return default_announce_channel()


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
      * a consumed temporary archive - the cleanup step deletes the .rar it points at, so
        every retry would abort immediately on file_size == 0 and emit a misleading error;
      * a legacy non-dict row, which has nowhere to store a counter.
    Both are settled on their first failure.
    """
    import defaults as config
    import db

    u_key = str(user).lower()

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
                config.dcc_queue[u_key] = kept
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
                config.dcc_queue[other_key] = kept
                if other_key != u_key:
                    print(f"[DCC QUEUE] Settled {user}'s row under {other_key!r} "
                          f"- they renamed while it was sending.")
                return removed
        return 0

    is_row = isinstance(next_file, dict)
    consumed_temp = bool(is_row and next_file.get("is_temporary_zip")
                         and not next_file.get("is_unpacked_rar_folder"))
    retryable = is_row and not consumed_temp

    retained = False
    gave_up = False
    budget = getattr(config, "MAX_SEND_FAILS", 3)

    with queue_lock:
        if delivered:
            removed = _remove_by_identity()
            outcome = "delivered, " + str(removed) + " row(s) removed"
        elif not retryable:
            removed = _remove_by_identity()
            why = "temporary archive already consumed" if consumed_temp else "row is not retryable"
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
            if oserve_mod:
                oserve_mod.queue_message(
                    user,
                    "NOTICE " + str(user) + " :" + config.C_BOLD + "Error" + config.C_RESET +
                    ": Could not send " + str(dropped) + " (" + str(reason) + "). Removed from your queue.\r\n")
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
                if (current_time - freeze_timestamp) > 300.0:
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
                        db.save_dcc_queue()
                        config.rar_inprogress = False
                        # #215: this release is the only moment another user's held pack can
                        # start. Nothing else revisits them - every check_queue_and_send()
                        # caller passes the user who just finished, never the one turned
                        # away at [RAR-HOLD].
                        redispatch_waiting_pack(irc_sock, just_finished=completed_user)
                        if hasattr(config, 'user_processing_lock'):
                            config.user_processing_lock.discard(completed_user.lower())
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
                    process = subprocess.run(cmd, capture_output=True,
                                             text=True, encoding="utf-8",
                                             errors="replace",
                                             timeout=rar_timeout)
                    
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
                            config.rar_inprogress = False
                            redispatch_waiting_pack(irc_sock, just_finished=completed_user)
                            if hasattr(config, 'user_processing_lock'):
                                config.user_processing_lock.discard(completed_user.lower())
                            return
                        if oserve: oserve.active_downloads = len(config.active_transfers)

                        announce_mod.send_dcc_sending_notice(completed_user, rar_filename)
                        
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

                announce_mod.send_dcc_sending_notice(completed_user, f_name)
                threading.Thread(target=start_dcc_send, args=(irc_sock, completed_user, f_path, f_name, target_chan, next_file), daemon=True).start()
                return
        else:
            # -----------------------------------------------------------------
            # NEVER freeze a queue while the bot itself is off the network.
            # On a netsplit or reconnect channel_users is empty or half-synced, so we do
            # not KNOW whether the user left. Leave the queue alone until NAMES has synced.
            # -----------------------------------------------------------------
            if not getattr(config, 'bot_joined_channel', False) or not getattr(config, 'channel_users', None):
                print(f"[DCC FREEZE-SKIP] The bot is not channel-synced yet. Leaving {completed_user}'s queue untouched.")
                return

            # A user may have exactly ONE countdown running at a time.
            if user_key in getattr(config, 'frozen_queues', {}):
                print(f"[DCC FREEZE-HOLD] {completed_user} already has a countdown running. Not starting another.")
                return

            with queue_lock:
                config.frozen_queues[user_key] = time.time()
            print(f"[DCC REACTIVE FREEZE] {completed_user} really has left {target_chan}. Starting the timer...")
            announce_mod.send_debug(f"DCC reactive freeze triggered for {completed_user} in {target_chan}. Initiating 5-minute cooldown timer.", category="QUIT")
            
            def user_queue_timer(sock, target_user, original_chan):
                """A verifying countdown, replacing the old blind 300-second sleep.
                The clock pauses entirely while the bot is disconnected - the bot's own
                downtime must NEVER count against a user's queue - and the countdown
                aborts as soon as the user reappears via JOIN or a NAMES sync."""
                t_key = target_user.lower()
                elapsed = 0
                
                while elapsed < 300:
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
                        
                    elapsed += 10
                
                if hasattr(config, 'frozen_queues') and t_key in config.frozen_queues:
                    with queue_lock:
                        if t_key in config.dcc_queue:
                            for f_obj in config.dcc_queue[t_key]:
                                if isinstance(f_obj, dict) and f_obj.get('is_temporary_zip') is True and os.path.exists(f_obj['path']) and not f_obj.get('is_unpacked_rar_folder'):
                                    try: os.remove(f_obj['path'])
                                    except: pass
                            del config.dcc_queue[t_key]
                            db.save_dcc_queue()
                        del config.frozen_queues[t_key]
                    announce_mod.send_debug(f"Timer expired for {target_user} in {original_chan}. Personal queue has been erased.", category="PART")
                    
            threading.Thread(target=user_queue_timer, args=(irc_sock, completed_user, target_chan), daemon=True).start()
            return

    # =====================================================================
    # B) Global queue handling for the next person in line, across all slots
    # =====================================================================
    if oserve:
        oserve.active_downloads = len(config.active_transfers)
        
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

                raw_chan = g_next.get('channel')
                user_is_globally_active = False
                if isinstance(raw_chan, str) and raw_chan.strip():
                    channels_to_check = [raw_chan]
                elif isinstance(raw_chan, list) and raw_chan:
                    channels_to_check = raw_chan
                else:
                    channels_to_check = config.CHANNEL.split(',')

                with runtime.channel_users_lock():
                    if hasattr(config, 'channel_users'):
                        for single_chan in channels_to_check:
                            n_chan = str(single_chan).strip().lower()
                            if n_chan in config.channel_users:
                                lowered_glob_users = [u.lower() for u in config.channel_users[n_chan]]
                                if queue_key in lowered_glob_users:
                                    user_is_globally_active = True
                                    break

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

                    print(f"[DCC QUEUE] New user {real_username} verified live in RAM for {g_chan}. Got slot.")
                    config.active_transfers.append({"user": real_username, "file": g_name, "bytes_sent": 0, "next_file_obj": g_name})
                    if oserve: oserve.active_downloads = len(config.active_transfers)

                    announce_mod.send_dcc_sending_notice(real_username, g_name)
                    threading.Thread(target=start_dcc_send, args=(irc_sock, real_username, g_path, g_name, g_chan, g_next), daemon=True).start()
                    break


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


def handle_resume_request(irc_sock, user, body):
    """Answer a receiver's DCC RESUME with the DCC ACCEPT it is waiting for.

    True if an ACCEPT was sent. False means no offer of ours matched, which
    is the ordinary outcome for a stray or forged line and is not logged as
    an error: anyone on the network can send this, and the only thing that
    makes it ours is a port we are listening on for that exact nick.
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
        # THROUGH THE SHARED CLOCK, not straight onto the socket (#453). A
        # resume handshake is latency-sensitive, so it is not queued behind
        # the round-robin - but it is still a PRIVMSG leaving this connection,
        # and a peer that reconnects and resumes repeatedly could otherwise
        # emit them as fast as it asked for them. Waiting for a slot keeps the
        # reply prompt while still counting it against the same budget every
        # other outbound line respects.
        runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)
        irc_sock.sendall(reply.encode("utf-8", errors="ignore"))
    except Exception as err:
        print(f"[DCC-RESUME] Could not answer {user}'s resume request: {err}")
        return False
    print(f"[DCC-RESUME] {user} already has {position} of {size} bytes of "
          f"{offered_name}; accepted and will send from there.")
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
    # message, because "the list is rebuilding" would not be true.
    if transfers_are_paused():
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: The bot is reloading its configuration. Your request is not lost - try again in a moment.\r\n")
        print(f"[MAINTENANCE BLOCK] Held a file request from {user}: a rehash "
              f"is waiting for transfers to finish.")
        return

    if getattr(config, 'PAUSE_ON_UPDATE', True) is True and getattr(config, 'update_inprogress', False) is True:
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: MasterList is currently rebuilding. File requests temporarily paused. Please wait 1-2 minutes.\r\n")
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
                import db
                db.save_dcc_queue()  # Commit straight to dcc_queue.txt
                
                user_pos = len(config.dcc_queue[user_key])
                print(f"[RAR QUEUE] Added virtuell mapp {master_rar_filename} for {user} at position #{user_pos}.")
                
                # One single clean line to the debug channel, nothing more
                announce_mod.send_debug(f"{user} requested \"{clean_folder_name}\". Starting rar and sending when done.", category="INFO")
                
                announce_mod.send_dcc_queue_notice(user, folder_name, user_pos)
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

        requested_file = str(requested_file).lstrip("/")

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
                    lines = []
                    for one_list in list_paths:
                        with open(one_list, "r", encoding="utf-8",
                                  errors="ignore") as lf:
                            lines.extend(lf.readlines())
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

                    for idx, line in enumerate(lines):
                        line_clean = line.strip()
                        if line_clean.startswith(f"!{config.NICKNAME} "):
                            # str.split() puts what came BEFORE the separator in
                            # [0], and the line starts with the separator - so
                            # [0] is the empty string on every line here, and
                            # this comparison never matched anything. The
                            # filename is in [1]; the whole list lookup was dead
                            # code without it, leaving the os.walk() below to
                            # answer every request.
                            parts_nick = line_clean.split(f"!{config.NICKNAME} ", 1)
                            rest_in_list = parts_nick[1].strip() if len(parts_nick) > 1 else ""

                            current_file_in_list, current_size_in_list = list_mod.strip_info_suffix(rest_in_list)

                            if clean_req == str(current_file_in_list).lower().strip():
                                found_folder = None
                                for back_idx in range(idx, -1, -1):
                                    back_line = lines[back_idx].strip()
                                    # The prefix-stripping itself is
                                    # list_mod.resolve_list_folder() - this used
                                    # to be a second, hand-written copy of it
                                    # (a hardcoded back_line[9:] rather than
                                    # len(LIST_FOLDER_PREFIX), and its own
                                    # trailing-backslash/separator handling),
                                    # which could silently drift from the
                                    # original if the list format ever changed.
                                    # ANY known prefix, not just the one
                                    # we write. This is what RECOGNISES
                                    # a heading, so checking only the
                                    # current prefix stops seeing the
                                    # headings in every list already in
                                    # somebody's hands - a bare request
                                    # against one then resolves nothing
                                    # at all. Found by the test that
                                    # counts resolutions.
                                    if any(back_line.upper().startswith(p)
                                           for p in list_mod.LIST_FOLDER_PREFIXES):
                                        # No explicit base: the heading itself
                                        # says which folder it belongs to once
                                        # there is more than one (#164), and
                                        # pinning it to base_directory would
                                        # resolve every heading into the first.
                                        found_folder = list_mod.resolve_list_folder(
                                            back_line, name=wanted_list)
                                        break
                                if found_folder is None:
                                    continue

                                # Two or more copies can share this exact name
                                # and differ only in size. Without a size hint,
                                # or if it matches nothing, the first copy the
                                # list names wins - same as before this change,
                                # and pinned by
                                # test_no_error_is_reported_for_a_duplicate.
                                # With one, a copy whose own ::INFO:: size
                                # matches it wins instead, so a request built
                                # from a search result's exact line reaches the
                                # copy that result actually named. A bare
                                # request (no hint - AutoQ.mrc and every
                                # existing caller) still stops at this first
                                # match exactly as before; only a hinted
                                # request that has not matched yet pays for
                                # scanning on, since that is the one case
                                # where the answer isn't already known.
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
            if not user_already_transferring and not user_is_processing and not user_has_queue and len(config.active_transfers) < config.MAX_DCC_SLOTS:
                # Lock the nick immediately, so the next row goes to the queue
                config.user_processing_lock.add(user_key)
                
                next_file_fake = {"path": full_path, "file": file_name, "channel": target_chan, "is_temporary_zip": False}
                config.active_transfers.append({"user": user, "file": file_name, "bytes_sent": 0, "next_file_obj": file_name})
                if oserve: oserve.active_downloads = len(config.active_transfers)
                announce.send_dcc_sending_notice(user, file_name)
                threading.Thread(target=start_dcc_send, args=(irc_sock, user, full_path, file_name, target_chan, next_file_fake), daemon=True).start()
                return
            else:
                # This user already has a track running; the row goes to dcc_queue.txt
                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []
                config.dcc_queue[user_key].append({"file": file_name, "path": full_path, "channel": target_chan, "user_raw": user, "is_temporary_zip": False})
                
                # Save and update dcc_queue.txt on disk straight away
                import db
                db.save_dcc_queue()
                
                user_pos = len(config.dcc_queue[user_key])
                announce.send_dcc_queue_notice(user, file_name, user_pos)
                return

    except Exception as e:
        print(f"[DCC ERROR] {e}")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1

def start_dcc_send(irc_sock, user, file_path, file_name, channel, next_file):
    """Handle the network ports and the CTCP, and stream the bytes with accurate timing."""
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
    for tx in config.active_transfers:
        if tx['user'].lower() == user.lower():
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
            config.user_processing_lock.discard(user.lower())
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers
                                          if tx['user'].lower() != user.lower()]
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
            config.user_processing_lock.discard(user.lower())
            
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]
            
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
                config.user_processing_lock.discard(user.lower())
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
            config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]

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
            config.user_processing_lock.discard(user.lower())
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
        dcc_sock.settimeout(30.0)
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

        conn, addr = dcc_sock.accept()
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
        if resume_offset:
            # bytes_sent counts what the RECEIVER ends up holding, so the
            # completeness check below still compares against the whole file.
            # What this transfer actually put on the wire is tracked
            # separately, because a resumed send that skipped 4 GB did not
            # move 4 GB and must not be allowed to claim it in the speed
            # record the channel advert publishes.
            bytes_sent = resume_offset
            for tx in config.active_transfers:
                if tx['user'].lower() == user.lower():
                    tx['bytes_sent'] = resume_offset
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
                except socket.error as e:
                    raise e
                for tx in config.active_transfers:
                    if tx['user'].lower() == user.lower():
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
        transfer_completed = bytes_sent >= file_size
        if not transfer_completed:
            print(f"[DCC-FAIL] {file_name} for {user}: sent {bytes_sent} of "
                  f"{file_size} bytes before the file ended. Recorded as a "
                  f"failure rather than a completed transfer.")
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
        transfer_finished_at = time.time()
        if transfer_completed:
            print(f"[DCC-SUCCESS] Sent the whole file to {user} with no errors.")
        # The original pause: gives mIRC 1.5 seconds to close the file calmly
        try: time.sleep(1.5)
        except: pass
 
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

    except socket.timeout:
        # FIXED (issue #30): previously silent. A handshake can succeed and the client can
        # connect, but if they never acknowledge fast enough the send loop times out here
        # with no log line at all - the only trace was a gap in the log between DCC-CONNECT
        # and the finally block's cleanup lines.
        print(f"[DCC-FAIL] Timeout sending to {user}: no data acknowledged within the socket timeout.")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except Exception as e:
        # FIXED (issue #30): `e` was captured and never used. A connection reset, a broken
        # pipe, or any other mid-transfer failure produced the exact same silence - no way
        # to tell which one happened after the fact, especially once the temp archive is
        # already deleted and the queue row already gone.
        print(f"[DCC-FAIL] Transfer to {user} failed: {type(e).__name__}: {e}")
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

        # Give the network buffer 0.5s to flush the final acknowledgement
        try:
            import time
            time.sleep(0.5)
        except:
            pass

        # The real-time speed counter
        # transfer_finished_at is set the instant the last byte went out; it
        # only exists on the path that actually completed a transfer, so the
        # fallbacks below cover the abort paths that reach here without one.
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
                          if stats_mgr_speed.speed_is_measurable(acute_duration)
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
                config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]
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
                announce_mod.send_transfer_complete(channel, user, file_name, file_size, start_time, reported_speed)
        except Exception as ann_chan_err:
            print(f"[ANNOUNCE CHANNEL ERROR] Could not send the channel notice: {ann_chan_err}")

        # 3. Close the network socket safely
        try: conn.close()
        except: pass
        try: dcc_sock.close()
        except: pass
        # 4. The RAR cache and the send lock
        try:
            file_still_needed = False
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
        
        # 5. SETTLE THE QUEUE ROW - by identity, with a retry budget. This replaces an
        #    unconditional pop(0), which removed whatever was first at that instant rather
        #    than the entry actually sent. See release_queue_entry.
        row_retained = False
        try:
            row_retained = release_queue_entry(
                user, next_file, delivered=transfer_completed,
                reason="transfer complete" if transfer_completed else "transfer did not complete")
        except Exception as pop_err:
            print("[DCC CLEANUP ERROR] Could not settle the queue row: " + str(pop_err))

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
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(user.lower())

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
