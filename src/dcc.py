# =====================================================================
# DCC.PY - THE TRANSFER ENGINE
# =====================================================================
import collections
import select
import socket
import struct
import threading
import time
import os
import sys
import re
import subprocess
import hashlib

import defaults as config
import platform_compat
import update_list
import list as list_mod
import announce
import db
import library
import runtime
import speed_cap
import transfer_log

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


def _requested_name_and_size(text):
    """A file request's name and pasted size hint, from what the user typed.

    A line copied from the list carries a "  ::INFO:: <size> ..." tail; it is
    split off (#234), and the size is kept, lower case, for the list scan to
    tell same-named copies apart.
    """
    requested_file, size_hint = list_mod.strip_info_suffix(text)
    return str(requested_file), size_hint.lower().strip()


def queued_name_of_request(text):
    """The name a plain file request for `text` is queued under, from the text
    alone: handle_download_request()'s own strip, then the last part of the
    path, as the row's name is the base name of the path the request opens.
    os.path.basename(), so "\\" separates on Windows only, as it does there;
    a leading "/" (which the request strips) changes no base name.

    `@<nick>-remove <file>` matches the queue with it (#1473), so a line
    pasted from the list, or a name with its folder in front, finds the row
    the same text queued.
    """
    return os.path.basename(_requested_name_and_size(text)[0])


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


def names_a_hidden_part_of_a_file(name, windows=None):
    """True for a requested name that would open something other than the file
    the list names: a hidden stream or fork of it, or the file under a second
    spelling.

    The containment check cannot see these. They resolve INSIDE the library,
    to a path that exists, because they are a file of the library read a
    different way:

    - On Windows, a ':' in a name component addresses an NTFS alternate data
      stream ("track.flac:Zone.Identifier", or "track.flac::$DATA" for the
      main stream under another name). A stream is never in any list, and the
      one Windows writes on every downloaded file records where the file was
      downloaded from - the operator's own browsing, URL and all.
    - On Windows, a component that ends in a dot or a space is trimmed by the
      file system before it is opened, so "track.flac." opens "track.flac"
      under a name no list ever wrote, and the queue's "already asked for"
      check (#1077) and the download counter both see a different file.
    - On macOS, a "..namedfork" component opens a file's resource fork.

    No Windows file can have ':' in its name or end in a dot or a space, so
    no legitimate request on Windows ever contains them, and refusing them
    there takes nothing away. On Linux and macOS both are ordinary, legal
    characters ("Live: 1999.flac", "R.E.M."), so those two are refused only
    on Windows (`windows` overrides, for tests, as it does above).
    "..namedfork" is refused everywhere: no library holds a folder by that
    name. Device names ("NUL", "CON") need nothing here - every file system
    call on a request goes through platform_compat.long_path(), whose \\\\?\\
    prefix makes Windows look for a file by that literal name instead.
    """
    if windows is None:
        windows = platform_compat.IS_WINDOWS
    for part in re.split(r"[\\/]", str(name)):
        if part in ("", ".", ".."):
            continue
        if part.lower() == "..namedfork":
            return True
        if windows and (":" in part or part[-1] in ". "):
            return True
    return False


def resolves_to_a_stream(path, windows=None):
    """True for a resolved path whose part after the drive holds a ':'.

    The second gate behind names_a_hidden_part_of_a_file(): that one reads
    the request, this reads the path the lookup settled on, whichever of the
    memories or the list scan produced it. On Windows a ':' after
    the drive can only be an alternate data stream.
    """
    if windows is None:
        windows = platform_compat.IS_WINDOWS
    if not windows:
        return False
    import ntpath
    return ":" in ntpath.splitdrive(str(path))[1]


# A LIBRARY-WIDE LOOKUP IS THE EXPENSIVE THING A REQUEST CAN ASK FOR (#580).
# A name that is not in the first folder's root makes handle_download_request()
# read every published list (until #1376 it then walked every configured folder
# too); each request
# runs on its own thread, and file requests no longer pass the flood gate (#888).
# Two bounds, both cheap, and a third per nick below (#969): only a few scans at a time (the rest are told the bot
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
# ONE NICK'S SHARE OF THE SCANS (#969). File requests stopped counting toward
# the flood gate in #888 - a pasted batch muted the very user it was for - and
# the "ten a nick per five seconds" the bounds above leaned on went with it.
# One nick sending made-up names as fast as the server lets it kept both scan
# slots busy, and everybody else was told "busy". So a nick's lookups take
# turns - its next one waits for its last, then looks in the memories that
# one just filled, which is how the rest of a pasted batch finds its siblings
# - and a nick whose last LOOKUP_NICK_MISSES scans in a window found nothing
# is told "busy" without another, until the window moves on. Only scans that
# MISSED count: a user asking for files that exist is never held back.
LOOKUP_NICK_MISSES = 10
LOOKUP_NICK_MISS_WINDOW_SECONDS = 60.0
# Owned by runtime.py, which nothing reloads (#749) - bound by name, the way
# queue_lock is, so a !rehash re-runs these lines and picks the same live
# objects back up. The memories themselves are this module's own cache.
_library_scans = runtime.library_scans
_lookup_misses = globals().get("_lookup_misses") or {}
_lookup_misses_lock = runtime.lookup_memory_lock
_lookup_hits = globals().get("_lookup_hits") or {}
_lookup_folders = globals().get("_lookup_folders") or {}
_scan_turns = runtime.library_scan_turns
_scanning_nicks = globals().get("_scanning_nicks") or set()
_nick_misses = globals().get("_nick_misses") or {}


def _nick_may_scan(nick, now=None):
    """Whether `nick` has scans left (#969): fewer than LOOKUP_NICK_MISSES of
    theirs found nothing in the last LOOKUP_NICK_MISS_WINDOW_SECONDS."""
    now = time.monotonic() if now is None else now
    with _lookup_misses_lock:
        recent = [when for when in _nick_misses.get(nick, ())
                  if now - when < LOOKUP_NICK_MISS_WINDOW_SECONDS]
        if recent:
            _nick_misses[nick] = recent
        else:
            _nick_misses.pop(nick, None)
        return len(recent) < LOOKUP_NICK_MISSES


def _note_nick_miss(nick, now=None):
    """One of `nick`'s scans found nothing. Bounded like the other memories:
    at most LOOKUP_NICK_MISSES times per nick, LOOKUP_MISS_MEMORY nicks."""
    now = time.monotonic() if now is None else now
    with _lookup_misses_lock:
        misses = _nick_misses.pop(nick, [])       # re-noted: newest again
        _nick_misses[nick] = (misses + [now])[-LOOKUP_NICK_MISSES:]
        excess = len(_nick_misses) - LOOKUP_MISS_MEMORY
        if excess > 0:
            for stale in list(_nick_misses)[:excess]:
                _nick_misses.pop(stale, None)


def _take_a_scan_turn(nick, timeout):
    """Wait, up to `timeout` seconds, until `nick` has no scan running, and
    claim the turn. False when the wait ran out."""
    deadline = time.monotonic() + timeout
    with _scan_turns:
        while nick in _scanning_nicks:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _scan_turns.wait(left)
        _scanning_nicks.add(nick)
    return True


def _end_scan_turn(nick):
    with _scan_turns:
        _scanning_nicks.discard(nick)
        _scan_turns.notify_all()


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


_SIZE_HINT_RE = re.compile(r"^(\d+)(?:\.(\d+))?([KMGTP]?B)$", re.IGNORECASE)
_SIZE_HINT_POWER = {"B": 0, "KB": 1, "MB": 2, "GB": 3, "TB": 4, "PB": 5}


def _size_fits_hint(size, word):
    """Whether `size` bytes reads as the size `word` says, at the word's own
    unit and precision - True, False, or None when `word` is not a size.

    Read as a number, not compared as text (#1121): the list writes rows with
    two decimals ("7.30MB") while update_list.format_size_human() gives one
    ("7.3MB"), so a text comparison never matched a pasted row, and the #886
    folder memory sent every one of them to a full list scan. Other bots, and
    lists written by older versions, carry still other precisions ("6.32Mb",
    "1.0KB"); each is taken at the precision it was written with."""
    match = _SIZE_HINT_RE.match(word)
    if not match:
        return None
    whole, decimals, unit = match.group(1), match.group(2) or "", match.group(3).upper()
    # As the list writer gets it: a float divided by 1024.0 per step. Dividing
    # by a power of two is exact, so this is the writer's number to the bit
    # and the rounding at a ".xx5" boundary goes the same way.
    value = float(size) / (1024.0 ** _SIZE_HINT_POWER[unit])
    places = len(decimals)
    return f"{value:.{places}f}" == f"{float(whole + '.' + (decimals or '0')):.{places}f}"


def _matches_size_hint(path, size_hint):
    """Whether the file at `path` is the size a pasted `::INFO::` hint says.

    The hint is what the list wrote after the name: the size ("7.30MB"),
    then - with audio info on - its length and quality. Only the first word
    is a size. No hint matches anything; a file that cannot be read, or a
    first word that is not a size, matches nothing."""
    words = str(size_hint or "").split()
    if not words:
        return True
    try:
        size = os.path.getsize(platform_compat.long_path(path))
    except OSError:
        return False
    return bool(_size_fits_hint(size, words[0]))


def _in_a_recent_folder(list_name, file_name, size_hint=""):
    """`<folder>/<name>` for the folders recent lookups resolved into.

    The reported case (#886): a user pastes nine rows from one album. The
    first costs a scan and names the folder; the other eight are one
    os.path.exists each, in the folder their sibling was just found in -
    newest folder first, because a batch arrives together.

    A request that carries a size (#962) only takes a copy of that size.
    `cover.jpg`, `folder.jpg` and `01 - Intro.mp3` are in nearly every album
    folder, so "the name exists in a folder somebody just asked in" is not
    "the file this request names": the pasted size is what tells two copies
    apart, and a copy of another size falls through to the list scan, which
    picks by it.
    """
    with _lookup_misses_lock:
        folders = list(reversed(_lookup_folders.get(list_name, [])))
    for folder in folders:
        candidate = os.path.join(folder, file_name)
        if (os.path.exists(platform_compat.long_path(candidate))
                and _matches_size_hint(candidate, size_hint)):
            return candidate
    return None


def forget_library_lookups():
    """Drop every remembered lookup - for a rebuild, and for the tests."""
    with _lookup_misses_lock:
        _lookup_hits.clear()
        _lookup_folders.clear()
        _lookup_misses.clear()
        _nick_misses.clear()


def _recall(list_name, requested_file, size_hint, search_roots):
    """A path the lookup memories give for this request, or None (#886): this
    exact name, then the folders recent lookups resolved into. Checked against
    search_roots - the CURRENT configuration, not whatever it was when the
    memory was made (#901). The size hint is part of what is remembered
    (#962)."""
    remembered_key = (str(list_name), str(requested_file).lower().strip(), size_hint)
    for candidate in (_remembered_path(remembered_key),
                      _in_a_recent_folder(str(list_name), requested_file, size_hint)):
        if candidate and any(is_safe_path(root, candidate) for root in search_roots):
            return candidate
    return None




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
        # A PRIVATE request never borrows the first configured channel
        # (#1242): that is a channel the requester may never have been in,
        # and it reached the feed, the mode check of #1204 and the "Sent:"
        # line. A row written since then carries the channel they share with
        # the bot, and returned above; one queued before has our own nick
        # there, and is resolved now, by the same rule. Sharing none, it has
        # no channel - "" - and the mode check takes the private-message
        # rule for it.
        if is_private_row(next_file):
            return library.shared_channel(next_file.get('user_raw')) or ""
    return default_announce_channel()


def is_private_row(next_file):
    """Was this queue row asked for by private message (#1242)?

    Flagged since #1242, because its channel is now the one the requester
    shares with the bot and no longer says so. A row queued before that has
    no flag and our own nick as its channel - the wire target of the
    message - which says it just as well.
    """
    if not isinstance(next_file, dict):
        return False
    if next_file.get('private') is True:
        return True
    named = next_file.get('channel')
    return isinstance(named, str) and bool(named.strip()) and not is_channel_name(named)


def privately_from(private, channel):
    """What a REQUEST line adds for a private request (#1242): the operator
    sees that it came by private message and which channel its sender
    shares with the bot. Nothing for a channel request, whose line is as
    it was."""
    return f" by private message (in {channel})" if private else ""


def refuse_unshared_private_request(user, what):
    """A private request from somebody in none of our served channels
    (#1242): refused, and silently, as a request in a channel this bot does
    not serve is. The operator still sees it, on the console and in the
    debug channel. The same check for every private request, whether a slot
    is free or not: it used to be served at once on an idle bot and frozen
    and dropped from the queue on a busy one."""
    print(f"[DCC] Ignored {user}'s private request for {what}: they are in "
          f"none of the channels this bot serves.")
    announce.send_debug(f"Ignored {config.C_BOLD}{user}{config.C_RESET}'s private request "
                        f"for {what}: they are in none of the channels this bot serves.",
                        category="INFO")


def is_channel_name(target):
    """Does this PRIVMSG target name a channel rather than a nick?

    RFC 2812 gives channels four prefixes; a nick can start with none of them.
    A request made by PRIVATE MESSAGE records the bot's OWN NICK as the row's
    channel (it is the wire target of that message - see
    handle_download_request), and until #530 announce_channel_for() handed it
    straight back, so the "Sent:" line for a PM request went out as
    `PRIVMSG <our nick> :Sent ...` - the bot telling itself. A nick is not a
    place to announce. Nor, since #1242, is the default channel for such a
    row: it is the channel the requester shares with the bot, and a private
    request is not announced at all - see announce_channel_for().
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

    Deliberately NOT made Windows-safe (#1208): a folder called AUX is
    offered as "AUX.rar", because AutoQ.mrc compares the received name with
    the queued folder's own name and "AUX_.rar" would never match. Only the
    archive's name ON DISK is made safe, in _rar_archive_disk_name().
    """
    return _clean_rar_chars(folder_leaf)


def _clean_rar_chars(text):
    """The character rules of _sanitize_rar_leaf_name(), nothing else."""
    cleaned = re.sub(r"[^\w\-\.\(\)\[\]']", "_", str(text))
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
    segments = [_clean_rar_chars(part) for part in rel.split("/") if part]
    joined = "_".join(segments) or "album"
    # A Windows-reserved name and ITS OWN "_"-suffixed form collide once
    # windows_safe_name() runs (#1249 review): "AUX" and a folder already
    # named "AUX_" both become "AUX_.rar" - the fixed suffix is not
    # reserved itself, so the second path is returned exactly as given,
    # landing on the same disk name windows_safe_name() gives the first.
    # `rar a` adds to an existing archive rather than replacing it - the
    # same #162 finding #7 this function exists to prevent, reached a
    # different way. A short digest of the real (pre-sanitised) relative
    # path, same discipline list.list_slug() already uses, makes the two
    # distinguishable - added only for a reserved name, so an ordinary
    # album keeps its plain, readable disk name exactly as before.
    if platform_compat.is_windows_reserved(joined):
        digest = hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:6]
        joined = f"{joined}-{digest}"
    return f"{platform_compat.windows_safe_name(joined, trim_end=False)}.rar"


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


def nicks_in_our_channels():
    """Every nick in any of the bot's live channel lists, lowercased, from one pass."""
    with runtime.channel_users_lock():
        return {str(known_user).lower()
                for users_set in getattr(config, 'channel_users', {}).values()
                for known_user in users_set}


def nicks_waiting_for_a_slot(except_key="", plain_files_only=False):
    """Queue keys of the nicks that have a file waiting and no send running, longest wait first.

    The caller holds queue_lock. A nick counts once it is in a channel the bot
    is in, is not frozen, and is neither sending nor claimed, nor holding a
    large file back while another is sent (#1387). With
    plain_files_only a nick whose next row is a folder pack is left out: only
    the specific-user path in check_queue_and_send() can start those.
    """
    busy = {str(tx.get("user", "")).lower() for tx in config.active_transfers}
    busy |= set(getattr(config, "user_processing_lock", ()) or ())
    # A nick whose next file is large waits while another large file is being
    # sent (#1387): it could not take a slot now, so it does not keep one.
    large_limit = _large_file_bytes()
    large_running = _a_large_send_is_running(large_limit)
    candidates = []
    for key, rows in list(config.dcc_queue.items()):
        key = str(key).lower()
        if key == except_key or not rows or key in config.frozen_queues or key in busy:
            continue
        head = rows[0]
        if plain_files_only and (not isinstance(head, dict) or head.get("is_unpacked_rar_folder")):
            continue
        if large_running and _row_is_large(head, large_limit):
            continue
        candidates.append(key)
    if not candidates:
        return []
    present = nicks_in_our_channels()
    waiting = [key for key in candidates if key in present]
    waiting.sort(key=queue_waiting_since)
    return waiting


def a_slot_is_free_beyond_those_waiting(user_key):
    """Is there a free slot left once every plain-file nick that is waiting has its own?

    The caller holds queue_lock. Only a nick the sweep could start counts: a
    nick holding a folder pack never takes a slot from a newcomer.
    """
    free = config.MAX_DCC_SLOTS - len(config.active_transfers)
    return free > len(nicks_waiting_for_a_slot(user_key, plain_files_only=True))


# A LIST GOES FIRST (#1205). "@nick" used to queue like any file, so a newcomer
# who only wanted to see what the bot has waited behind everyone's albums - and
# the list is small, quick to send, and what leads anyone to ask for anything.
# A list request now takes the next free slot ahead of the nicks waiting for
# files and folders. It never interrupts a send, and the per-nick rule stands:
# a nick still has one send at a time, so its list waits for its own send.
#
# THE CAP: only one list at a time may get its slot this way. A list that went
# first carries "list_went_first" on its transfer row, and while one is being
# sent the next list waits its turn like a file. A list that would have had the
# slot anyway, because its nick had waited longest, does not count - so lists
# hold at most one slot beyond what the fairness rule (#1032) gives them, and a
# flood of list requests cannot starve the file sends.
LIST_ROW_KEY = "is_list_request"


def is_a_list_row(row):
    """Is this queue row the list archive (#1205)? Only rows built by a list request carry the mark."""
    return isinstance(row, dict) and row.get(LIST_ROW_KEY) is True


def put_the_list_first(rows):
    """Move the list row just appended to `rows` to the front, behind any list already there (#1205).

    The dispatcher offers a slot to a list only while it heads its nick's
    queue. Caller holds queue_lock. Returns the row's new 1-based place.
    """
    row = rows.pop()
    row[LIST_ROW_KEY] = True
    at = 0
    while at < len(rows) and is_a_list_row(rows[at]):
        at += 1
    rows.insert(at, row)
    return at + 1


def a_list_may_go_first(transfers=None):
    """May a list take the next free slot ahead of the waiting nicks? Caller holds queue_lock.

    Not while another list that went first is still being sent (#1205).
    `transfers` is a copy of active_transfers for a reader that holds no lock
    (the Queue page); by default the live list.
    """
    held = config.active_transfers if transfers is None else transfers
    return not any(tx.get("list_went_first") for tx in held)


# ONE LARGE FILE AT A TIME (#1387). With MAX_DCC_SLOTS = 3 and three nicks each
# asking for a multi-gigabyte archive, every slot was held for hours and every
# ordinary track waited behind them. LARGE_FILE_MB (0 = off) makes a file of at
# least that many MB large, and at most one large file is sent at a time: a
# nick whose next file is large, while another large file is being sent, is
# passed over as a nick already sending is, keeps its place, and the slot goes
# to the next in line. When the large send ends, the longest wait is next.
#
# The size is recorded on the row when it is queued, where the path has just
# been resolved, because stat() must not run under queue_lock (#605). A row
# without one counts as small: rows from before this change and folder packs,
# whose archive does not exist until it is made. So a guess never holds a
# file back. A pack's row records its archive's size once it is packed, and
# from then on follows the rule like any file (#1435). The bot's own list is
# never large.
QUEUED_SIZE_KEY = "queued_size"
LARGE_FILE_WAIT_NOTE = "waiting: one large file at a time"


def _large_file_bytes():
    """LARGE_FILE_MB in bytes; 0 when the rule is off or the setting is unusable."""
    try:
        megabytes = int(getattr(config, "LARGE_FILE_MB", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return megabytes * 1024 * 1024 if megabytes > 0 else 0


def _size_on_disk(path):
    """The file's size for its queue row, or 0 when it cannot be read. Never under queue_lock (#605)."""
    try:
        return int(os.path.getsize(platform_compat.long_path(path)))
    except (OSError, TypeError, ValueError):
        return 0


def _recorded_size(row):
    """The size a queue row was given when it was queued; 0 for none, or one a hand edit broke."""
    if not isinstance(row, dict):
        return 0
    try:
        size = int(row.get(QUEUED_SIZE_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, size)


def _is_the_list(row):
    """A queue row or a transfer row for the bot's own list, which is never large."""
    return isinstance(row, dict) and (is_a_list_row(row) or is_a_list_row(row.get("queue_row"))
                                      or list_mod.is_list_artifact_name(str(row.get("file") or "")))


def _row_is_large(row, limit):
    """Is this queue row a large file under `limit` bytes (#1387)? Unknown sizes are small."""
    if not limit or _is_the_list(row):
        return False
    return _recorded_size(row) >= limit


def _transfer_is_large(tx, limit):
    """Is this transfer a large file? Its real size once the send has read it; before
    that, the size its queue row (or the claim itself) was given when it was queued."""
    if not limit or _is_the_list(tx):
        return False
    size = 0
    try:
        size = int(tx.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if not size:
        size = _recorded_size(tx) or _recorded_size(tx.get("queue_row"))
    return size >= limit


def _a_large_send_is_running(limit, transfers=None):
    """Is a large file being sent, or claimed for sending? Caller holds queue_lock, or passes a copy."""
    held = config.active_transfers if transfers is None else transfers
    return bool(limit) and any(_transfer_is_large(tx, limit) for tx in held if isinstance(tx, dict))


def waits_for_a_large_send(rows, transfers=None):
    """Is this nick's next file large while another large file is being sent (#1387)?

    `rows` is the nick's queue. The dispatcher passes the nick over while this
    holds; the console and the Queue page show it as "waiting: one large file
    at a time". `transfers` is a copy of active_transfers for a reader that
    holds no lock; by default the live list, under queue_lock.
    """
    limit = _large_file_bytes()
    if not limit or not rows:
        return False
    return _row_is_large(rows[0], limit) and _a_large_send_is_running(limit, transfers)


def _say_a_large_file_waits(nick, row):
    print(f"[DCC QUEUE] {nick}'s next file is large ({_recorded_size(row) // (1024 * 1024)} MB) and "
          f"another large file is being sent - the slot goes to the next in line.")


def _fill_the_slots_a_large_send_left(irc_sock, passed_over, dispatched_row):
    """One more pass of the sweep while a slot is still free (#1387).

    A trigger dispatches one nick, which was always enough: a slot stayed
    free only while nobody could use it. A large file held back can leave a
    slot free with that nick waiting, and when the large send ends the nick
    and the finished one can both be due with one trigger between them.

    Only then: when the pass that dispatched `dispatched_row` passed over a
    large head (`passed_over`), or the row it dispatched is large. Not after
    every dispatch: a send refused at once (no address, no free port) hands
    its slot straight back, and a pass after each of those refused the same
    nick over and over, a NOTICE each time. The pass this starts is a
    fill_pass, which starts none of its own.
    """
    limit = _large_file_bytes()
    if not limit or not (passed_over or _row_is_large(dispatched_row, limit)):
        return
    if len(config.active_transfers) < config.MAX_DCC_SLOTS:
        threading.Thread(target=check_queue_and_send, args=(irc_sock, "system_next_trigger_fallback"),
                         kwargs={"fill_pass": True}, daemon=True).start()


def list_first_rank(user_key, rows, lists_first):
    """The order a freed slot is offered in: a list that may go first, then the longest wait.

    `rows` is the nick's queue. Without a list at its head - or with
    `lists_first` False - this is the wait alone, the #1032 order unchanged.
    """
    goes_first = bool(lists_first and rows and is_a_list_row(rows[0]))
    return (0 if goes_first else 1, queue_waiting_since(user_key))


def slot_order(queue, lists_first):
    """The nicks with something in `queue`, in the order a freed slot is offered to them.

    The dispatcher's own key, list_first_rank(); ties keep `queue`'s order.
    What `queue`, the Queue page and move up/down show and move against, so
    the three cannot disagree with the dispatcher (#1205, #1245).
    """
    waiting = [(key, rows) for key, rows in queue.items() if rows]
    waiting.sort(key=lambda entry: list_first_rank(entry[0], entry[1], lists_first))
    return [key for key, _rows in waiting]


def a_list_passes_a_longer_wait(user_key, packs_can_start):
    """Would a nick that has waited longer get this slot if lists did not go first? Caller holds queue_lock."""
    mine = queue_waiting_since(user_key)
    return any(queue_waiting_since(key) < mine
               for key in nicks_waiting_for_a_slot(user_key, plain_files_only=not packs_can_start))


def queue_waiting_since(user_key):
    """When this nick began waiting for a slot; 0 for a nick nothing has stamped (queue restored from disk)."""
    return runtime.queue_waiting_since.get(str(user_key).lower(), 0.0)


def queued_position_of(user_key, path):
    """The place (1-based) `path` already holds in this nick's own queue, or None. Caller holds queue_lock.

    The path decides, not the name: two albums can hold a track with the same
    file name (#110), and asking for the second one is not asking twice. A
    packed folder is found by the folder it was packed from (`source_path`),
    because the packer replaces the row's `path` with the archive.
    """
    wanted = os.path.normcase(os.path.normpath(str(path)))
    for place, row in enumerate(config.dcc_queue.get(user_key, []), start=1):
        held = row.get("source_path") or row.get("path") or ""
        if os.path.normcase(os.path.normpath(str(held))) == wanted:
            return place
    return None


def queue_row_id(row):
    """A short id for a queue row, which the Queue page sends back with a move or a removal (#1245).

    Made from the path the row was queued for - the folder, for a packed one,
    as queued_position_of() reads it - so it names THAT file: two albums can
    each hold an Intro.mp3, and a name matched whichever had slid into the
    place. The path itself never reaches the page.
    """
    if isinstance(row, dict):
        held = row.get("source_path") or row.get("path") or row.get("file") or ""
    else:
        held = row
    key = os.path.normcase(os.path.normpath(str(held)))
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def queued_row_in_flight(row):
    """Is this queue row out? "sending", "packing" or None. Caller holds queue_lock.

    By the row itself (#1245): a queued send's claim carries its row as
    "queue_row" and a running pack its job's "row". The claims hold no path,
    so is_being_sent_to() - for a send that never had a row - cannot see
    them, and a place says nothing once a list has gone to the front.
    """
    if any(tx.get("queue_row") is row for tx in config.active_transfers):
        return "sending"
    job = runtime.pack_job
    if job is not None and job.get("row") is row:
        return "packing"
    return None


def is_being_sent_to(user_key, path):
    """True when a send of `path` to this nick is running and has no queue row. Caller holds queue_lock.

    A request that found a free slot goes straight to start_dcc_send() and
    never gets a row (#1086), so queued_position_of() cannot see it; its
    active_transfers entry carries the path instead.
    """
    wanted = os.path.normcase(os.path.normpath(str(path)))
    for tx in config.active_transfers:
        held = tx.get("path")
        if held and str(tx.get("user", "")).lower() == user_key \
                and os.path.normcase(os.path.normpath(str(held))) == wanted:
            return True
    return False


def start_waiting(user_key):
    """A nick with nothing queued has just queued its first file: its wait begins now. Caller holds queue_lock."""
    if not config.dcc_queue.get(user_key):
        runtime.queue_waiting_since[user_key] = time.time()


def go_to_the_back(nick_key, keep_place=False):
    """A nick's send is over: its next file waits from now, behind every nick that was already waiting.

    A nick with nothing left queued has no wait to record, so its stamp is dropped.
    With keep_place the nick keeps the wait it has: a list is not the nick's
    turn (#1205), so the files it was already waiting for keep their place.
    """
    with queue_lock:
        if config.dcc_queue.get(nick_key):
            if keep_place and nick_key in runtime.queue_waiting_since:
                return
            runtime.queue_waiting_since[nick_key] = time.time()
        else:
            runtime.queue_waiting_since.pop(nick_key, None)


def forget_stamps_of_empty_queues():
    """Drop the wait stamp of every nick whose queue is gone. Caller holds queue_lock."""
    for key in [k for k in runtime.queue_waiting_since if not config.dcc_queue.get(k)]:
        del runtime.queue_waiting_since[key]


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


def _archive_key(path):
    """One spelling of an archive's path, for comparing two of them (#1268)."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _transfer_path(tx):
    """The file a transfer row is sending: its own "path" (the direct send and
    a packed archive's handoff carry one), else its queue row's (#1268)."""
    held = tx.get("path")
    if not held and isinstance(tx.get("queue_row"), dict):
        held = tx["queue_row"].get("path")
    return held


def temp_archive_in_use(path, ignoring=()):
    """Does anything still need the temp archive at `path`? Caller holds queue_lock.

    THE ONE ANSWER TO "MAY THIS ARCHIVE GO" (#1268). A packed archive's disk
    name comes from its FOLDER, not from who asked (_rar_archive_disk_name()),
    so every nick that queued the same album ends up naming one file. Four
    places deleted it, each with its own idea of "still needed": the freeze
    sweep and the freeze timer asked nobody, and the send's cleanup and
    discard_orphaned_temp_archives() compared the OFFERED name - the leaf
    alone, so a different album with the same leaf name kept an archive on
    disk for good. The packer asked nobody either and deleted another nick's
    waiting archive as "stale".

    Needed means, by path:
      * a queue row of any nick that names it as a packed archive - except the
        rows in `ignoring`, the ones the caller is about to drop;
      * a transfer sending it right now;
      * the pack that is writing it.
    """
    wanted = _archive_key(path)
    for rows in list(getattr(config, "dcc_queue", {}).values()):
        for row in rows or ():
            if not isinstance(row, dict) or any(row is gone for gone in ignoring):
                continue
            if row.get("is_temporary_zip") is not True or row.get("is_unpacked_rar_folder"):
                continue
            if row.get("path") and _archive_key(row["path"]) == wanted:
                return True
    for tx in getattr(config, "active_transfers", []):
        held = _transfer_path(tx)
        if held and _archive_key(held) == wanted:
            return True
    job = runtime.pack_job
    if job is not None and job.get("archive") and _archive_key(job["archive"]) == wanted:
        return True
    return False


def discard_orphaned_temp_archives(user_key, rows=None):
    """Delete the temp .rar files that only `user_key`'s queue rows still name.

    MUST be called with queue_lock held and BEFORE the rows are dropped: those
    rows are the only record that the archives exist, so once they are gone the
    files sit in TMP_ZIP_DIR forever.

    Four paths delete a whole queue - the freeze-timeout sweep, the per-user
    freeze timer, !clearqueue, and the user's own @<nick>-remove / CTCP REMOVE.
    Only the first three cleaned up; REMOVE, the one users actually type, did
    not. This is that cleanup, in one place - and since #1268 the two freeze
    paths use it too: their own loops deleted every archive the expired queue
    named, including one another nick's row was still waiting to send.

    Two rows are deliberately skipped:

    * is_unpacked_rar_folder rows, whose "path" is the source album directory in
      the music library, not a temp file. Deleting that would delete the music.
    * archives another queue row still points at, that a transfer is streaming
      right now or that a pack is writing - temp_archive_in_use(), by path.
      Archive names come from the FOLDER, not the user
      (_rar_archive_disk_name()), so two people who queued the same album
      share one file on disk.

    `rows`: only these rows are being dropped (one file out of a queue), so the
    archives the user's OTHER rows still name are kept. None means the whole
    queue is going.

    Returns the paths actually removed, for the caller to log.
    """
    removed = []
    queue = getattr(config, 'dcc_queue', {})
    if user_key not in queue:
        return removed

    dropping = list(queue[user_key] if rows is None else rows)
    for f_obj in dropping:
        if not isinstance(f_obj, dict):
            continue
        if f_obj.get('is_temporary_zip') is not True or f_obj.get('is_unpacked_rar_folder'):
            continue
        temp_path = f_obj.get('path')
        if not temp_path or not os.path.exists(temp_path):
            continue

        # By PATH, through the one answer every deleter now shares (#1268):
        # the offered name is the leaf alone, and a different album with the
        # same leaf name kept this archive on disk for good.
        if temp_archive_in_use(temp_path, ignoring=dropping):
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


# HOW A SEND ENDED, IN THE RECORD (#1203). The record kept completed sends
# only, so an operator could not tell "users never accept my offers" from "my
# ports are broken". Every attempt now writes one row when it ends - a retry
# is a second attempt and a second row - and an attempt that is put back
# without being charged to the row's retries (no connection to IRC, no free
# port) has not ended and writes nothing.


def _transfer_outcome_identity(file_path, file_name):
    """(key, display name, kind) of a send, as its completed row would have
    them. Never raises: the record must not cost the transfer anything."""
    try:
        return download_count_identity(file_path, file_name)
    except Exception:
        return None, file_name, transfer_log.KIND_FILE


def _claim_transfer_outcome(holder):
    """True for the one caller that writes how this transfer ended.

    Two can try: the transfer's own thread, and a stop that cuts it off
    (record_transfers_cut_off()). `holder` is the transfer's row in
    active_transfers or the pack's job; None has no one to race with."""
    if holder is None:
        return True
    with queue_lock:
        if holder.get("transfer_outcome_recorded"):
            return False
        holder["transfer_outcome_recorded"] = True
        return True


def _record_send_outcome(holder, status, identity, size, reached, nick):
    """One send that ended without completing, in the record. Never raises."""
    try:
        if not _claim_transfer_outcome(holder):
            return False
        key, shown, kind = identity
        return transfer_log.record_unfinished(transfer_log.SENT, status, kind, size, reached,
                                              nick=nick, item_key=key, name=shown)
    except Exception as record_err:
        print(f"[TRANSFER-LOG ERROR] Could not record how the send ended: {record_err}")
        return False


def _pack_identity(folder_name):
    """The (key, display name) an album's send is recorded under, from the
    folder's name: the archive name the packer gives it."""
    leaf = os.path.basename(str(folder_name or "").strip().rstrip("/\\"))
    if not leaf:
        return None, None
    archive = f"{_sanitize_rar_leaf_name(leaf)}.rar"
    return archive, archive[:-4]


def _record_pack_outcome(user, next_file, status, job=None):
    """A folder pack that ended with nothing to send (#1203): it failed, timed
    out or was cancelled. Recorded as an album, with the folder's size when
    it was measured and no bytes sent. Never raises."""
    try:
        if not _claim_transfer_outcome(job):
            return False
        folder = next_file.get("path") if isinstance(next_file, dict) else None
        key, shown = _pack_identity(folder)
        return transfer_log.record_unfinished(
            transfer_log.SENT, status, transfer_log.KIND_ALBUM,
            int(job["total"]) if job else 0, 0, nick=user, item_key=key, name=shown)
    except Exception as record_err:
        print(f"[TRANSFER-LOG ERROR] Could not record how the pack ended: {record_err}")
        return False


def record_transfers_cut_off():
    """At a stop or a restart (#1203): every send still running, and the pack
    still being made, ends with the bot. Each is written to the record as
    cancelled - the bot stopped it, not the network. A send that was handed
    a slot but had not offered anything yet has not begun, and is left out.
    Returns how many were written."""
    written = 0
    with queue_lock:
        running = list(getattr(config, "active_transfers", []) or [])
    for tx in running:
        identity = tx.get("transfer_outcome_identity")
        if not identity:
            continue
        reached = int(tx.get("bytes_sent") or 0) - int(tx.get("resume_offset") or 0)
        if _record_send_outcome(tx, transfer_log.STATUS_CANCELLED, tuple(identity),
                                tx.get("size") or 0, max(0, reached), tx.get("user")):
            written += 1
    job = runtime.pack_job
    if job is not None and _record_pack_outcome(job["user"], {"path": job["name"]},
                                                transfer_log.STATUS_CANCELLED, job=job):
        written += 1
    return written


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


class _TooSlowWhileOthersWait(Exception):
    """Acknowledged under SLOW_SEND_MIN_KB over the last
    SLOW_SEND_WINDOW_SECONDS, with every slot taken and someone waiting for
    one (#1388). `speed` is that rate in bytes per second."""

    def __init__(self, acked, speed):
        super().__init__(acked, speed)
        self.acked = acked
        self.speed = speed


class _SourceUnreadable(Exception):
    """Our own file could not be opened or read mid-send (#1389).

    Not an OSError on purpose. Since Python 3.10 socket.timeout IS
    TimeoutError, and a network share that hiccups raises TimeoutError,
    ConnectionResetError or ConnectionAbortedError from open() or read() -
    the very types start_dcc_send() takes for the receiver timing out or
    closing. Wrapped, a NAS hiccup is reported as the bot's failure and never
    counts toward a downloader's DAILY_SEND_FAILS_LIMIT.
    """

    def __init__(self, err):
        super().__init__(err)
        self.err = err


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


class _AlreadyHeld(Exception):
    """Raised, like _ShortSend, to skip the completion bookkeeping - for a send
    that completed with nothing on the wire because the receiver resumed at
    the end of the file. See `already_held` in start_dcc_send()."""


def release_queue_entry(user, next_file, delivered, reason="", cancelled=False, shown_name=None, retry=True,
                        told=None):
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

    `cancelled` is the operator stopping a folder pack (#1202): not a failure
    of the row, so nothing is charged to its retry budget. The row is removed
    - the user asks again if they still want it - and the notice says so in
    place of "Could not send", naming `shown_name` and no path.

    `retry` False settles a failure on its first time: a folder pack refused
    for a reason a retry would meet again (PackRefused, #1376).

    `told` is what the user's notice says in place of `reason`, which then
    goes to the log alone: a folder pack's reason can name paths and sizes
    of this machine (PACK_FAILED_TO_USER, #1376).
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
    retryable = is_row and in_a_queue and not consumed_temp and retry

    retained = False
    gave_up = False
    budget = getattr(config, "MAX_SEND_FAILS", 3)

    with queue_lock:
        if delivered:
            removed = _remove_by_identity()
            outcome = "delivered, " + str(removed) + " row(s) removed"
        elif cancelled:
            removed = _remove_by_identity()
            outcome = "cancelled, " + str(removed) + " row(s) removed"
        elif not retryable:
            removed = _remove_by_identity()
            if consumed_temp:
                why = "temporary archive is gone from disk"
            elif is_row and not in_a_queue:
                why = "sent directly, not queued - nothing to retry"
            elif not retry:
                why = reason or "it may not be tried again"
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

    if cancelled:
        try:
            oserve_mod = sys.modules.get("oserve")
            if oserve_mod:
                import announce as announce_mod
                shown = str(shown_name or (next_file.get("file") if is_row else "") or "your folder")

                def _build_cancelled(shown_name):
                    return ("NOTICE " + str(user) + " :Your folder request " + config.C_BOLD + shown_name +
                            config.C_RESET + " was cancelled by the operator. "
                            "Please ask again if you still want it.\r\n")

                oserve_mod.queue_message(user, announce_mod.fit_irc_line(_build_cancelled, shown))
        except Exception as notify_err:
            print("[DCC QUEUE] Could not notify " + str(user) + ": " + str(notify_err))
    elif gave_up or (not delivered and not retained):
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
                        ": Could not send " + shown_name + " - " + str(reason if told is None else told) +
                        ". " + tail + "\r\n")

            if oserve_mod:
                import announce as announce_mod
                oserve_mod.queue_message(user, announce_mod.fit_irc_line(_build, str(dropped)))
        except Exception as notify_err:
            print("[DCC QUEUE] Could not notify " + str(user) + ": " + str(notify_err))

    print("[DCC QUEUE] " + str(user) + ": " + outcome + ((" (" + reason + ")") if reason else "") + ".")
    return retained


# ---------------------------------------------------------------------
# A NICK WHOSE SENDS KEEP FAILING (#1389): DAILY_SEND_FAILS_LIMIT.
# ---------------------------------------------------------------------
# MAX_SEND_FAILS above is per queued file; this is per nick, per day. The
# count is runtime.failed_transfers, under runtime.failed_transfers_lock,
# keyed like dcc_queue. In memory only: a restart forgives, which a daily
# limit can afford.
#
# WHAT COUNTS is decided where a send fails, not here: start_dcc_send() sets
# receiver_failed in the handlers for a receiver that never connected,
# stalled, let a write time out or closed the connection before it had the
# whole file, and finish_send() counts it. Everything else that ends a send
# short leaves it unset - a failure on our side (the file vanished or
# shrank, the handshake could not be sent, no port), and a stop the bot
# makes itself. A new way for the bot to stop a send gets its own handler
# and does not set it. The pack cancels, the operator's and the user's own
# remove, never reach a send at all.

def _send_fails_today():
    """Today, by the bot's local clock: the day a failure is counted in. A
    function of its own so a test can move midnight."""
    import datetime
    return datetime.date.today()


# "NEVER CONNECTED" IS OFTEN OUR SIDE. A stale MY_IP_OR_DOCK (an ISP's daily
# reconnect), a port forward the router lost or a firewall in front of the
# bot makes every offer go unanswered, and counting those would pause
# everybody and empty every queue within minutes. Two rules, both reset by
# the next send that connects (note_send_connected()):
#   * before any send has connected since start-up, none counts;
#   * a run of never-connected sends with no connect in between stops
#     counting once it reaches NEVER_CONNECTED_STREAK sends to at least
#     NEVER_CONNECTED_STREAK_NICKS different nicks - several downloaders
#     failing the same way at once is the bot. One nick failing alone keeps
#     counting: that is its own problem.
NEVER_CONNECTED_STREAK = 3
NEVER_CONNECTED_STREAK_NICKS = 2


def note_send_connected():
    """A DCC send's receiver connected: the bot's offers work, so the
    never-connected streak starts again from nothing."""
    with runtime.send_connected_lock:
        runtime.send_connected_since_start = True
        runtime.never_connected_streak = 0
        runtime.never_connected_streak_nicks.clear()
        runtime.never_connected_streak_logged = False


def never_connected_counts(nick):
    """Whether a send to `nick` whose receiver never connected may count
    toward DAILY_SEND_FAILS_LIMIT; adds it to the streak either way. Not
    before any send has connected since start-up, nor once the streak
    (this send included) has reached NEVER_CONNECTED_STREAK sends to
    NEVER_CONNECTED_STREAK_NICKS or more nicks. When it does not count, the
    log says why - once per streak, not once per offer."""
    with runtime.send_connected_lock:
        runtime.never_connected_streak += 1
        runtime.never_connected_streak_nicks.add(_send_fails_key(nick))
        since_start = runtime.send_connected_since_start
        broken = (runtime.never_connected_streak >= NEVER_CONNECTED_STREAK
                  and len(runtime.never_connected_streak_nicks) >= NEVER_CONNECTED_STREAK_NICKS)
        if since_start and not broken:
            return True
        say = not runtime.never_connected_streak_logged
        runtime.never_connected_streak_logged = True
        streak = runtime.never_connected_streak
        nicks = len(runtime.never_connected_streak_nicks)
    if say:
        why = (f"no send has connected since start-up" if not since_start
               else f"{streak} offers to {nicks} different nicks have gone unanswered "
                    f"since the last send that connected")
        print(f"[DCC PAUSE] Sends that never connected are not counted toward "
              f"DAILY_SEND_FAILS_LIMIT until one connects: {why}, so it looks like "
              f"the bot's own address (MY_IP_OR_DOCK) or port forwarding.")
    return False


def daily_send_fails_limit():
    """DAILY_SEND_FAILS_LIMIT as a whole number; 0, off, for anything that
    is not a positive one."""
    try:
        return max(0, int(getattr(config, "DAILY_SEND_FAILS_LIMIT", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _send_fails_key(nick):
    """The dcc_queue key of `nick`: the count follows the queue it pauses,
    to a new nick too (carry_send_fails(), #1437)."""
    return str(nick or "").lower().strip()


def _never_paused(key):
    """An admin (ADMIN_NICK) is never counted or paused."""
    import commands
    return key in commands.admin_nicks()


def send_fails_paused(nick):
    """How many of `nick`'s sends failed today, when that has paused it; 0
    when it is not paused - the limit off, an admin, under the limit, or the
    count from a day that has ended."""
    limit = daily_send_fails_limit()
    key = _send_fails_key(nick)
    if not limit or not key or _never_paused(key):
        return 0
    today = _send_fails_today()
    with runtime.failed_transfers_lock:
        entry = runtime.failed_transfers.get(key)
        fails = int(entry["fails"]) if entry and entry.get("day") == today else 0
    return fails if fails >= limit else 0


def paused_for_send_fails():
    """{nick_key: failed sends today} for every nick paused right now."""
    limit = daily_send_fails_limit()
    if not limit:
        return {}
    today = _send_fails_today()
    with runtime.failed_transfers_lock:
        counted = {key: int(entry["fails"]) for key, entry in runtime.failed_transfers.items()
                   if entry.get("day") == today and int(entry["fails"]) >= limit}
    return {key: fails for key, fails in counted.items() if not _never_paused(key)}


def note_failed_send(nick):
    """Count one failed send against `nick` (#1389) and return its count for
    today; 0 when nothing is counted (the limit off, or an admin).

    The first count of a new day finds yesterday's counts and drops them,
    so midnight resets every nick and the dict never holds more than a day.
    The failure that reaches the limit pauses the nick: its queued files are
    removed and it is told why (_pause_for_send_fails). Once, not on every
    failure after it; a limit raised past the count and lowered again
    pauses it again.
    """
    limit = daily_send_fails_limit()
    key = _send_fails_key(nick)
    if not limit or not key or _never_paused(key):
        return 0
    today = _send_fails_today()
    with runtime.failed_transfers_lock:
        for stale in [k for k, entry in runtime.failed_transfers.items() if entry.get("day") != today]:
            del runtime.failed_transfers[stale]
        entry = runtime.failed_transfers.setdefault(key, {"day": today, "fails": 0, "paused": False})
        entry["fails"] += 1
        fails = entry["fails"]
        pause_now = fails >= limit and not entry["paused"]
        entry["paused"] = fails >= limit
    print(f"[DCC PAUSE] {nick}: {fails} failed send(s) today, of {limit} allowed.")
    if pause_now:
        _pause_for_send_fails(nick, key, fails)
    return fails


def _paused_pack_reason(nick):
    """Why a folder row left the queue around its pack when `nick`'s pause
    took it (#1468): the packer's log says this, not that the nick removed
    the folder."""
    return f"{nick} is paused for failed sends today"


def _pause_for_send_fails(nick, key, fails, queued_only=False, stop_pack=True):
    """`nick` has reached DAILY_SEND_FAILS_LIMIT: drop its queued files, as
    !clearqueue would, and say why - to the nick and to the operator.
    Returns how many queued files went.

    `queued_only`: the dispatcher found rows under a nick already paused
    (drop_queues_of_paused_nicks(), #1460). With nothing left to drop by the
    time the lock is held, nothing is said - the nick was told when it was
    paused.

    A folder among the dropped rows may be being packed right now: that pack
    is stopped (#1468), as the user's own remove stops it (#1268), rather
    than running rar to the end for nothing while every other pack waits.
    `stop_pack=False` is the packer itself, whose pack has already ended.

    Lock order (#1452): queue_lock is taken and released here, and only then
    is runtime.pack_lock taken, by cancel_pack(). The two are never held
    together, and the caller holds neither, nor failed_transfers_lock."""
    import db
    with queue_lock:
        rows = config.dcc_queue.get(key) or []
        removed = len(rows)
        if rows:
            # Before the rows go: they are the only record of their archives.
            for temp_path in discard_orphaned_temp_archives(key):
                print(f"[DCC PAUSE] Removed orphaned temp archive: {temp_path}")
            config.dcc_queue.pop(key, None)
        if removed or not queued_only:
            getattr(config, "frozen_queues", {}).pop(key, None)
    if stop_pack:
        # Out of the queue first: a pack that ends before this lands finds
        # its row gone and does not send. Nothing goes to the user from
        # here or from the stopped packer: the NOTICE below says it all.
        for row in rows:
            cancel_pack(row=row, reason=_paused_pack_reason(nick))
    if queued_only and not removed:
        return 0
    if removed:
        try:
            db.save_dcc_queue()
        except Exception as save_err:
            print("[DCC QUEUE ERROR] Could not persist the queue: " + str(save_err))
    print(f"[DCC PAUSE] {nick} is paused until midnight after {fails} failed sends today; "
          f"{removed} queued file(s) removed.")
    announce.send_debug(f"{config.C_BOLD}{nick}{config.C_RESET} is paused until midnight: "
                        f"{fails} failed sends today, {removed} queued file(s) removed. "
                        f"unpause {nick} lifts it.", category="INFO")
    oserve_mod = sys.modules.get("oserve")
    if oserve_mod:
        oserve_mod.queue_message(nick, f"NOTICE {nick} :{config.C_BOLD}"
                                       f"{announce.send_fails_paused_text(fails, removed)}{config.C_RESET}\r\n")
    return removed


def drop_queues_of_paused_nicks():
    """The dispatcher's side of DAILY_SEND_FAILS_LIMIT (#1460): drop what is
    still queued for a nick paused right now, as the pause itself does
    (_pause_for_send_fails()), and return {nick_key: fails} of every paused
    nick, for the claim sites to pass over.

    The pause removes the queue when it begins, but the queue can still be
    there: after a rename merges a pause onto a nick that has a queue of its
    own, or when a NICK lands between note_failed_send() counting the
    failure and the removal, which then looks under the old nick. Only the
    request gate asked send_fails_paused(), so those rows were sent.

    Dropped, not kept for midnight: the pause leaves a nick no queue, and it
    would have left this one none had it found the rows. Rows kept would also
    count as waiting in nicks_waiting_for_a_slot() while never being sent.

    Lock order (#1452): the paused set is read under failed_transfers_lock,
    which is released before queue_lock is taken. The two are never held
    together."""
    paused = paused_for_send_fails()
    if not paused:
        return paused
    with queue_lock:
        holding = []
        for key, rows in config.dcc_queue.items():
            if rows and _send_fails_key(key) in paused:
                head = rows[0] if isinstance(rows[0], dict) else {}
                holding.append((key, head.get("user_raw") or key))
    for key, nick in holding:
        _pause_for_send_fails(nick, key, paused[_send_fails_key(key)], queued_only=True)
    return paused


def unpause_send_fails(nick):
    """Forget `nick`'s failed sends today, lifting a pause (the console's
    `unpause`). Returns how many there were; 0 when there were none."""
    key = _send_fails_key(nick)
    today = _send_fails_today()
    with runtime.failed_transfers_lock:
        entry = runtime.failed_transfers.pop(key, None)
    return int(entry["fails"]) if entry and entry.get("day") == today else 0


def carry_send_fails(old_key, new_key):
    """Move today's failed sends, and the pause they made, from `old_key` to
    `new_key` on a rename (irc.note_nick_change(), #1437). Both are
    dcc_queue keys. True when the old nick had a count today.

    Takes failed_transfers_lock only; call it outside queue_lock. An entry
    from an earlier day counts for nothing (send_fails_paused()), so it is
    dropped, not carried. When the new key holds today's entry too - the nick
    was someone else's earlier today, or this user's before an earlier
    rename - the larger count wins, whole: its "paused" says whether that
    count has already paused (note_failed_send()), so it belongs with the
    count. Never the sum, which would charge one person with another's
    failures. Equal counts keep a pause either entry had."""
    today = _send_fails_today()
    with runtime.failed_transfers_lock:
        fails_store = runtime.failed_transfers
        old_entry = fails_store.pop(old_key, None)
        if not (old_entry and old_entry.get("day") == today):
            return False
        new_entry = fails_store.get(new_key)
        if not new_entry or new_entry.get("day") != today:
            fails_store[new_key] = old_entry
        elif int(old_entry["fails"]) > int(new_entry["fails"]):
            fails_store[new_key] = old_entry
        elif int(old_entry["fails"]) == int(new_entry["fails"]):
            new_entry["paused"] = bool(new_entry.get("paused") or old_entry.get("paused"))
        return True


def carry_never_connected_streak(old_key, new_key):
    """Move `old_key` to `new_key` in the never-connected streak's nicks on a
    rename (irc.note_nick_change()), as carry_send_fails() moves the count:
    one user renamed mid-streak is still one nick, not two
    (never_connected_counts()). Both are dcc_queue keys, as
    _send_fails_key() makes them. True when the old nick was in the streak.

    Takes send_connected_lock only, and nothing inside it; call it outside
    queue_lock. A new key already in the streak stays there once: the set
    then counts this user once, which is the point. The streak's length is
    left alone - the sends happened, under whichever name."""
    with runtime.send_connected_lock:
        nicks = runtime.never_connected_streak_nicks
        if old_key not in nicks:
            return False
        nicks.discard(old_key)
        nicks.add(new_key)
        return True


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

def dcc_address():
    """The address a DCC offer carries: MY_IP_OR_DOCK when the operator
    pinned one, otherwise the one irc.refresh_dcc_address() detected, or ""
    while nothing is known (#1271).

    Two names because they are two facts. The detection used to be written
    into MY_IP_OR_DOCK, and from then on it read as a pin, so it was never
    looked up again for the life of the process.
    """
    pinned = str(getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    return pinned or str(runtime.dcc_address_detected or "").strip()


def get_public_ip_long():
    """Convert dcc_address() (MY_IP_OR_DOCK, or the detected address) into the mIRC-compatible long format, or 0
    if it is blank or not a dotted-quad at all.

    Deliberately a pure converter, and it stays one. An earlier version of this
    fix folded the "is this address any use to a remote client?" question in
    here, which broke the admin console: adminchat.py's DCC CHAT listen-back
    uses the same value, and an operator connecting from the same machine or
    the same LAN has a loopback or private address that is entirely correct for
    that purpose. Only the file-transfer path needs the stricter rule, so the
    stricter rule lives there - see is_offerable_to_strangers() below.
    """
    text = dcc_address()
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

    text = str(ip_text if ip_text is not None else dcc_address()).strip()
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
            # A row waiting for room is not woken: the watcher wakes it (#1376).
            if (isinstance(head, dict) and head.get('is_unpacked_rar_folder') is True
                    and _space_hold_for(head) is None):
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

    While packs are held (pack_hold_reason(), #1376) nothing is woken: the
    watcher is started instead, and wakes the queue once they are not.
    """
    if getattr(config, 'rar_inprogress', False):
        return None
    with queue_lock:
        held = pack_hold_reason()
        waiting_for_room = bool(runtime.pack_space_holds)
    if held is not None or waiting_for_room:
        if waiting_for_room or next_waiting_pack_owner() is not None:
            if held is not None:
                print(f"[RAR-HOLD] Folder packs wait in the queue: {held}.")
            _arm_pack_hold_watch(irc_sock)
        if held is not None:
            return None
    owner = next_waiting_pack_owner(exclude_user=just_finished)
    if not owner:
        return None
    print(f"[RAR-WAKE] A pack for {owner} was waiting on the packer lock; dispatching it now.")
    threading.Thread(target=check_queue_and_send, args=(irc_sock, owner),
                     daemon=True).start()
    return owner

FREEZE_TIMEOUT = 300.0   # seconds an absent user's queue is kept, counted only while the bot is online
# How often the countdown wakes to look at the user again. A module constant,
# read on every pass of user_queue_timer (#1148), so a test can make the
# countdown wake quickly instead of waiting out a real ten-second step, and a
# !rehash that changes it reaches a countdown that is already running.
FREEZE_POLL_SECONDS = 10.0


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
    # Under the server's casemapping (#1271): a row queued before the read
    # loop carried the configured spelling may still hold the server's.
    import irc
    key = irc.channel_key(chan)
    with runtime.channel_users_lock():
        return key in getattr(config, "channel_users", {})


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
            time.sleep(FREEZE_POLL_SECONDS)

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
                elapsed += FREEZE_POLL_SECONDS

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
                # Not every archive the queue names (#1268): another nick's
                # row may be waiting to send the same one.
                discard_orphaned_temp_archives(t_key)
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


# WHAT A PACK HOLDS (#1376). A !rar archive holds the files the list lists
# below the folder - update_list.pack_members() - and none the list leaves
# out: LIST_IGNORED_EXTENSIONS and the list's own exclusions (#1295). A !rar
# row records the list it was asked through (PACK_LIST_KEY), so its pack
# follows that list's rule; a packed archive's row records the types it was
# packed without (PACKED_WITHOUT_KEY), so a request through a list that
# leaves out more is never handed an archive packed for one that leaves out
# less.
PACK_LIST_KEY = "served_list"
PACKED_WITHOUT_KEY = "packed_without"


def pack_leaves_out(row):
    """The file types a pack of `row` leaves out: update_list.ignored_extensions() of its list.

    A row with no list on it (queued before rows recorded one), or naming a
    list that is gone, gets every list's left-out types together, so its pack
    holds nothing any list leaves out.
    """
    name = row.get(PACK_LIST_KEY) if isinstance(row, dict) else None
    if name is not None and library.list_by_name(name) is not None:
        return update_list.ignored_extensions(name)
    every = list(update_list.ignored_extensions())
    for entry in library.lists():
        every.extend(update_list.ignored_extensions(entry.name))
    return tuple(dict.fromkeys(every))


def _archive_packed_without(path, ignored):
    """Was the archive at `path` packed leaving out every type in `ignored`? Caller holds queue_lock.

    Asked of the queue rows that name it as a packed archive, and of the rows
    of the sends sending it: one whose PACKED_WITHOUT_KEY covers `ignored`
    says yes. An archive no row says that of is not handed to another request.
    """
    wanted = _archive_key(path)
    wanting = set(ignored)
    rows = [row for rows in list(config.dcc_queue.values()) for row in rows or ()]
    rows += [tx.get("queue_row") for tx in config.active_transfers if isinstance(tx, dict)]
    for row in rows:
        if not isinstance(row, dict) or row.get("is_unpacked_rar_folder") or not row.get("path"):
            continue
        if _archive_key(row["path"]) != wanted:
            continue
        without = row.get(PACKED_WITHOUT_KEY)
        if isinstance(without, (list, tuple)) and wanting <= set(without):
            return True
    return False


def _archive_path_leaving_out(target_rar_path, ignored):
    """Where an archive of a folder packed without `ignored` goes when the one at
    `target_rar_path` was packed for another rule and is still wanted: beside
    it, named with a digest of the types, so each rule has a path of its own."""
    digest = hashlib.sha1("\n".join(sorted(ignored)).encode("utf-8")).hexdigest()[:8]
    stem, ext = os.path.splitext(target_rar_path)
    return f"{stem}-{digest}{ext}"


def waiting_archive_count():
    """How many packed archives wait in TMP_ZIP_DIR to be sent. Caller holds queue_lock.

    By path, so the rows of two nicks that name one archive count once. One
    being sent is not waiting. Nor is one behind a folder pack in its own
    nick's queue: it cannot be sent before that pack is made, and counted, it
    could hold that very pack at the cap for ever. In practice the count is
    of queue heads, and of what follows a head that is not a pack.
    """
    sending = set()
    for tx in config.active_transfers:
        held = _transfer_path(tx) if isinstance(tx, dict) else None
        if held:
            sending.add(_archive_key(held))
    waiting = set()
    for rows in list(config.dcc_queue.values()):
        for row in rows or ():
            if isinstance(row, dict) and row.get("is_unpacked_rar_folder"):
                break
            if (isinstance(row, dict) and row.get("is_temporary_zip") is True
                    and row.get("path")):
                key = _archive_key(row["path"])
                if key not in sending:
                    waiting.add(key)
    return len(waiting)


def waiting_archive_cap():
    """The most packed archives that may wait at once: MAX_DCC_SLOTS, and at least one."""
    try:
        return max(1, int(config.MAX_DCC_SLOTS or 1))
    except (TypeError, ValueError):
        return 1


def _megabytes(size):
    return f"{max(0, int(size or 0)) // (1024 * 1024)} MB"


def _space_hold_for(row):
    """The hold that keeps `row` waiting for room, or None. Caller holds queue_lock."""
    for hold in runtime.pack_space_holds:
        if hold["row"] is row:
            return hold
    return None


def pack_hold_reason(row=None):
    """Why a folder pack may not start now, or None. Caller holds queue_lock.

    A FOLDER PACK WAITS, KEEPING ITS PLACE (#1376), while:
      * waiting_archive_cap() packed archives already wait to be sent - every
        pack waits then. A finished archive can wait for a slot, or for a
        large send (#1435), while the packer moves on, so without this they
        piled up, one per nick with a !rar row at its queue head;
      * the disk TMP_ZIP_DIR is on had no room for `row`'s files when it was
        about to be packed (_hold_packs_for_space()). That row alone waits:
        a pack that fits goes ahead.
    Without a `row`, the answer for folder packs in general: the cap.
    """
    if row is not None:
        hold = _space_hold_for(row)
        if hold is not None:
            return (f"{_megabytes(hold['free'])} is free where archives are built, "
                    f"and the pack needs {_megabytes(hold['needed'])}")
    waiting = waiting_archive_count()
    if waiting >= waiting_archive_cap():
        return f"{waiting} packed archive(s) are already waiting to be sent"
    return None


def packs_can_start(row=None):
    """Can a folder pack - `row`'s, or one in general - start now? Caller holds queue_lock.

    No pack is being made (config.rar_inprogress) and none is held
    (pack_hold_reason()). Every place that asks whether a waiting pack could
    start asks this - the dispatcher, the sweep, the wake and the fairness
    rule - so they cannot disagree.
    """
    return not getattr(config, 'rar_inprogress', False) and pack_hold_reason(row) is None


def _room_where_archives_are_built():
    """(free, total) bytes of the disk TMP_ZIP_DIR is on, or (None, None) when it cannot be measured."""
    import shutil
    try:
        usage = shutil.disk_usage(platform_compat.long_path(os.path.abspath(config.TMP_ZIP_DIR)))
    except OSError:
        return None, None
    return usage.free, usage.total


def _has_room_to_pack(size, free):
    """Does an archive of `size` fit in `free` and leave room to spare? The
    rule fetching waits by (dcc_fetch.MIN_FREE_BYTES), for the same reason:
    a disk filled to nothing fails everything that writes to it."""
    import dcc_fetch
    return dcc_fetch.has_room(size, free)


def _room_needed_to_pack(size):
    """What must be free for an archive of `size`: _has_room_to_pack()'s rule, as a number."""
    import dcc_fetch
    return int(size) + dcc_fetch.MIN_FREE_BYTES


# What a user is told of a folder that could not be packed (#1376): one fixed
# line. Why is in the log - the reason can name TMP_ZIP_DIR, a size or a path.
PACK_FAILED_TO_USER = "the folder could not be packed"


class PackRefused(RuntimeError):
    """A folder pack that cannot be made however often it is tried. The
    wrapper refuses it once: its row goes, and uses up no retries."""


def _hold_packs_for_space(row, nick, folder_leaf, size, free):
    """`row`'s files (`size`) do not fit in `free` with room to spare: it
    waits, keeping its place, until they do or it leaves the queue."""
    import announce as announce_mod
    needed = _room_needed_to_pack(size)
    hold = {"row": row, "total": int(size), "needed": needed, "free": int(free)}
    with queue_lock:
        runtime.pack_space_holds = tuple(
            held for held in runtime.pack_space_holds if held["row"] is not row) + (hold,)
    message = (f"{nick}'s pack of {folder_leaf} waits in the queue: {_megabytes(free)} is free where "
               f"archives are built, and it needs {_megabytes(needed)}. Packing carries on by "
               f"itself once there is room.")
    print("[RAR-HOLD] " + message)
    announce_mod.send_debug(message, category="PACK")


def _end_space_hold(row):
    """`row` no longer waits for room."""
    with queue_lock:
        runtime.pack_space_holds = tuple(
            held for held in runtime.pack_space_holds if held["row"] is not row)


# How often held folder packs are looked at again (#1376). A module constant,
# read on every pass, like FREEZE_POLL_SECONDS, so a test can make it quick.
PACK_HOLD_POLL_SECONDS = 30.0


def _arm_pack_hold_watch(irc_sock):
    """Start the watcher for held folder packs, unless one runs. Returns whether it started."""
    with queue_lock:
        if runtime.pack_hold_watch is not None:
            return False
        runtime.pack_hold_watch = True
    try:
        threading.Thread(target=_watch_held_packs, args=(irc_sock,), daemon=True).start()
    except Exception as start_err:
        with queue_lock:
            runtime.pack_hold_watch = None
        print(f"[RAR-HOLD] Could not start looking at the held folder packs again: {start_err}")
        return False
    return True


def _watch_held_packs(irc_sock):
    """Look at the held folder packs every PACK_HOLD_POLL_SECONDS until none
    is held, then wake the queue.

    A send ending wakes the queue anyway, and with it a pack that waited for
    the archives to be sent. This is what wakes one when nothing else would -
    disk freed by hand, a waiting archive's nick gone and its queue dropped -
    so a held pack is never stranded. A row that stops waiting while others
    still do is woken at once, not when the last one stops.
    """
    try:
        while True:
            # Whatever goes wrong in a look, the queue is still woken below:
            # a watcher that died here would strand the packs it watched.
            try:
                time.sleep(PACK_HOLD_POLL_SECONDS)
                still_held, stopped_waiting = _look_at_held_packs()
                if not still_held:
                    break
                if stopped_waiting:
                    check_queue_and_send(irc_sock, "system_next_trigger_fallback")
            except Exception as look_err:
                print(f"[RAR-HOLD] Looking at the held folder packs failed: {look_err}")
                break
    finally:
        with queue_lock:
            runtime.pack_hold_watch = None
    print("[RAR-WAKE] Folder packs are no longer held; looking at the queue.")
    check_queue_and_send(irc_sock, "system_next_trigger_fallback")


def _look_at_held_packs():
    """One look at the held folder packs. A row waiting for room stops
    waiting once its files fit, or once it has left the queue. Returns
    (whether a folder pack still waits on a hold, whether one stopped)."""
    with queue_lock:
        holds = runtime.pack_space_holds
        queued = [held for held in holds if _row_is_still_queued(held["row"])
                  and held["row"].get("is_unpacked_rar_folder") is True]
    free = _room_where_archives_are_built()[0] if queued else None
    done = [held for held in holds
            if not any(held is waiting for waiting in queued) or _has_room_to_pack(held["total"], free)]
    if done:
        with queue_lock:
            runtime.pack_space_holds = tuple(
                held for held in runtime.pack_space_holds if not any(held is gone for gone in done))
        print(f"[RAR-HOLD] {len(done)} folder pack(s) no longer wait for room where archives are built.")
    with queue_lock:
        holds_left = bool(runtime.pack_space_holds)
        capped = pack_hold_reason() is not None
    return holds_left or (capped and next_waiting_pack_owner() is not None), bool(done)


def _rar_list_file_lines(parent, leaf, members, separator=None, windows=None):
    """The lines of the list file rar takes a folder's files by, and how many files were left out.

    `members` are update_list.pack_members()' paths, below the folder `leaf`
    in `parent`. Each line is a file's absolute path, as rar finds it under
    the folder it is given: `-n@` keeps exactly those files, and the archive
    keeps the folder by its name as it always has (-ep1).

    Files only, never a folder: a folder named to -n takes everything in it.

    rar does not read every line as one exact name, so a path it would read
    as more, or as another, is left out rather than written:
      * * or ? in any part: a mask, and in a name it matches files in every
        folder below too;
      * a part that ends in a space or a tab, or on Windows in a dot: the end
        of the line is trimmed, so `keep.flac ` takes `keep.flac` and a file
        `Sub ` the whole folder `Sub`;
      * a control character (a line break among them); with "/" as the
        separator, a backslash, which rar may read as one; a path the list
        file cannot hold (a POSIX name that is not valid text). U+0085,
        U+2028 and U+2029 are not among them: rar 7.22 takes a name holding
        one as it is, and lists it as one name.
    Windows allows none of these in a name made the ordinary way.

    `separator` and `windows` are for the tests, as in
    update_list.has_backslash_component(): os.sep and this platform otherwise.
    """
    sep = os.sep if separator is None else separator
    on_windows = platform_compat.IS_WINDOWS if windows is None else windows
    trimmed = (" ", "\t", ".") if on_windows else (" ", "\t")
    above = str(parent).rstrip(sep).split(sep)

    lines = []
    left_out = 0
    for member in members:
        below = [leaf] + [part for part in str(member).split(sep) if part]
        parts = above + below
        line = sep.join(parts)
        unsafe = any(c < " " or c == "\x7f" for c in line) or (sep == "/" and "\\" in line)
        unsafe = unsafe or any("*" in part or "?" in part for part in parts)
        unsafe = unsafe or any(part.endswith(trimmed) for part in parts if part)
        if not unsafe:
            try:
                line.encode("utf-16-le")
            except UnicodeEncodeError:
                unsafe = True
        if unsafe:
            left_out += 1
        else:
            lines.append(line)
    return lines, left_out


def _rar_environment(windows=None, darwin=None):
    """The environment rar runs in: this process's own, and off Windows in a
    UTF-8 locale, so that rar writes the names it lists as UTF-8 whatever
    the bot's locale is. None on Windows, where -scfr says it instead.

    A locale that already says UTF-8 (LC_ALL, else LC_CTYPE, else LANG, as
    the C library picks them) is left as it is. Otherwise LC_ALL is set to
    one that exists on the system: C.UTF-8 on Linux, en_US.UTF-8 on macOS,
    which has no C.UTF-8 on every release. `windows` and `darwin` are for
    the tests."""
    on_windows = platform_compat.IS_WINDOWS if windows is None else windows
    if on_windows:
        return None
    env = dict(os.environ)
    in_force = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if "utf-8" in in_force.lower() or "utf8" in in_force.lower():
        return env
    on_darwin = sys.platform == "darwin" if darwin is None else darwin
    env["LC_ALL"] = "en_US.UTF-8" if on_darwin else "C.UTF-8"
    return env


def _listed_names(text):
    """The names in rar's listing `text`, one a line, with "/" between their parts.

    Split at "\\n" alone, one "\\r" off each line: str.splitlines() also
    splits at U+0085, U+2028 and U+2029 - and at more - which a file name
    may hold, and rar lists such a name as one."""
    names = []
    for line in str(text or "").lstrip("\ufeff").split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line:
            names.append(line.replace(os.sep, "/"))
    return names


def _archive_names(rar_bin, archive, timeout):
    """(names, None) for what `rar lb` lists in `archive`, or (None, why) when
    it cannot be listed.

    Run as the pack is, by _run_rar(), which reads rar's output as UTF-8:
    rar writes it so on Windows when -scfr says so (checked with rar 7.22,
    Unicode names intact), and elsewhere in _rar_environment()'s locale.
    The job is the listing's own, so nothing can cancel it - an operator's
    cancel is for the pack, which is over - and only its timeout stops it.
    """
    cmd = [rar_bin, "lb"] + (["-scfr"] if platform_compat.IS_WINDOWS else []) + [archive]
    try:
        done = _run_rar({"cancelled": False, "process": None}, cmd, timeout)
    except (OSError, subprocess.TimeoutExpired) as err:
        return None, f"{type(err).__name__}: {err}"
    if done.returncode != 0:
        detail = (done.stderr or "").strip()
        return None, f"rar lb exited {done.returncode}" + (f": {detail}" if detail else "")
    return _listed_names(done.stdout), None


def _what_the_archive_got_wrong(names, expected):
    """(stray, missing) for an archive listing `names` that was to hold
    `expected`: names nobody asked for, and asked-for files it does not hold.

    THE LAST WORD ON WHAT A PACK HOLDS (#1376), whatever rar made of the list
    it was given and whatever changed in the folder between the walk and
    rar's - a file turned into a folder or a link. Both ways: the packer
    refuses an archive with a stray name, or with none of what was asked for
    (an empty listing misses all of it), and sends one short of a few asked-
    for files, saying so. A folder above an asked-for file is not stray -
    rar off Windows may store one, with no data in it.
    """
    folders = set()
    for name in expected:
        parts = name.split("/")
        folders.update("/".join(parts[:end]) for end in range(1, len(parts)))
    held = set(names)
    stray = sorted(name for name in held if name not in expected and name not in folders)
    missing = sorted(set(expected) - held)
    return stray, missing


def _members_inside_the_library(source_abs, members, islink=None, safe=None):
    """(kept, left out): pack_members()' `members` of the folder `source_abs`,
    less every file that is a link to somewhere outside the library folder
    the folder is in.

    rar packs what a file link points at. A request for the file on its own
    is refused by is_safe_path(), which resolves it; a pack must not hand out
    what the file would not be. Only links are resolved: the walk does not
    go into a linked folder, so a file that is not a link is where it is.
    `islink` and `safe` (is_safe_path()'s shape) are for the tests.
    """
    islink = os.path.islink if islink is None else islink
    safe = is_safe_path if safe is None else safe
    roots = [folder.path for folder in library.every_folder() if safe(folder.path, source_abs)]
    kept, left_out = [], []
    for member in members:
        full = os.path.join(source_abs, member[0])
        if islink(platform_compat.long_path(full)) and not any(safe(root, full) for root in roots):
            left_out.append(member)
        else:
            kept.append(member)
    return kept, left_out


def _rar_list_file_bytes(lines, windows=None):
    """(switch, bytes): the list file rar reads `lines` from, and the switch that
    tells it the encoding.

    UTF-16 with -scul on Windows, where rar 7.22 was checked with it. UTF-8
    with -scfl elsewhere: rar 6.23 on Linux leaves out a name with a
    character above U+FFFF read from a UTF-16 list (an emoji), and packs it
    from a UTF-8 one, as it does U+2028. `windows` is for the tests."""
    on_windows = platform_compat.IS_WINDOWS if windows is None else windows
    if on_windows:
        return "-scul", ("\ufeff" + "".join(line + "\r\n" for line in lines)).encode("utf-16-le")
    return "-scfl", "".join(line + "\n" for line in lines).encode("utf-8")


def _write_rar_list_file(lines):
    """Write `lines` to a list file in TMP_ZIP_DIR (_rar_list_file_bytes())
    and return (its path, the switch rar reads it with). The caller removes it.

    One pack runs at a time, so a list file already there was left by a run
    that never got to remove its own: it goes first."""
    import glob
    import tempfile
    for stale in glob.glob(os.path.join(glob.escape(os.path.abspath(config.TMP_ZIP_DIR)), "pack-*.lst")):
        try:
            os.remove(stale)
        except OSError:
            pass
    handle, path = tempfile.mkstemp(prefix="pack-", suffix=".lst",
                                    dir=os.path.abspath(config.TMP_ZIP_DIR))
    switch, data = _rar_list_file_bytes(lines)
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    return path, switch


def _pack_again_if_packed_before(row):
    """Turn a packed archive's row that says nothing of what it was packed
    without back into its folder's row. Caller holds queue_lock. Returns
    whether it did.

    Such a row was packed before archives kept the list's left-out types out
    (#1376) - restored from dcc_queue.txt, say - so it is not sent as it is:
    the folder it was packed from (`source_path`) is packed again under the
    list's rule when its turn comes, and the archive goes unless another row
    or a send still needs it. A row being sent is left alone, as is one with
    no folder on it.
    """
    if not (isinstance(row, dict) and row.get("is_temporary_zip") is True
            and not row.get("is_unpacked_rar_folder") and PACKED_WITHOUT_KEY not in row):
        return False
    source = row.get("source_path")
    if not isinstance(source, str) or not source:
        return False
    if any(isinstance(tx, dict) and tx.get("queue_row") is row for tx in config.active_transfers):
        return False
    archive = row.get("path")
    row["path"] = source
    row["is_unpacked_rar_folder"] = True
    row.pop(QUEUED_SIZE_KEY, None)
    if archive and _is_temp_zip_cache_file(archive) and not temp_archive_in_use(archive):
        try:
            os.remove(platform_compat.long_path(str(archive)))
        except OSError:
            pass
    print(f"[DCC QUEUE] {row.get('user_raw') or 'A nick'}'s archive of {os.path.basename(source.rstrip('/' + chr(92)))} "
          f"was packed by an earlier version; the folder is packed again.")
    return True


def pack_again_every_row_packed_before():
    """Turn every packed archive's row that says nothing of what it was packed
    without back into its folder's row (_pack_again_if_packed_before()), in
    every queue and at every place. Called once the queue is loaded from
    dcc_queue.txt; the dispatcher turns back a head it meets later as well.
    Returns how many were turned back."""
    turned = 0
    with queue_lock:
        for rows in list(config.dcc_queue.values()):
            for row in list(rows or ()):
                turned += bool(_pack_again_if_packed_before(row))
    if turned:
        print(f"[DCC QUEUE] {turned} queued archive(s) packed by an earlier version will be packed again.")
    return turned


def _row_is_still_queued(row):
    """Is `row` still in the queue? Caller holds queue_lock. By the row
    itself, under any nick: a rename moves it to another key (#1268).
    Was a closure in check_queue_and_send() until #1300."""
    return any(queued is row
               for rows in config.dcc_queue.values() if rows
               for queued in rows)


def pack_folder_for_request(sock, next_file, owner_nick, target_chan, oserve):
    """Pack the folder `next_file` asks for and hand the archive to a send.
    Returns True once it has handed config.rar_inprogress and the user's
    processing lock to another thread, as inline_rar_packer() expects; its
    finally releases them otherwise.

    `owner_nick` is a callable: the nick the claim is filed under NOW, which
    a rename mid-pack changes (#1268).

    Was _inline_rar_packer_body(), a closure inside check_queue_and_send(),
    until #1300 - step 2 of splitting dcc.py's largest functions
    (docs/FUTURE.md). Moved unchanged; inline_rar_packer() keeps the
    interlocks and their release.
    """
    import announce as announce_mod
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
        print(f"[SECURITY] Blocked a poisoned queue entry for {owner_nick()}: {true_source_dir}")
        with queue_lock:
            # By the row itself, under whichever nick a rename
            # filed it (#1268).
            for q_key, q_rows in list(config.dcc_queue.items()):
                kept = [e for e in q_rows if e is not next_file]
                if len(kept) != len(q_rows):
                    # The save no longer prunes an emptied key (#606).
                    if kept:
                        config.dcc_queue[q_key] = kept
                    else:
                        del config.dcc_queue[q_key]
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
            f"Poisoned queue entry discarded for {config.C_BOLD}{owner_nick()}{config.C_RESET}: path outside the music root.",
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

    def point_row_at_archive():
        # The folder it was packed from stays on the row: a
        # repeat !rar of it is still a repeat while the
        # archive is being sent (#1077).
        next_file['source_path'] = next_file.get('source_path') or next_file.get('path')
        next_file['path'] = target_rar_path
        next_file['file'] = rar_filename
        next_file['is_unpacked_rar_folder'] = False
        # What it was packed without (#1376): a later request is
        # handed this archive only if its list leaves out no more.
        next_file[PACKED_WITHOUT_KEY] = list(ignored)

    # THE ARCHIVE MAY ALREADY BE SOMEBODY ELSE'S (#1268). The
    # disk name comes from the folder, so a second nick's !rar
    # of an album lands on the path the first nick's finished
    # archive is waiting at - and this used to delete it as
    # "stale", or let a failed run remove it outright. A
    # finished archive another row still names is reused
    # as it is; nothing is packed. Decided, and the pack
    # registered, under one hold of queue_lock, so a send's
    # cleanup cannot remove it in between.
    #
    # AND IT IS REUSED ONLY IF IT HOLDS NOTHING THIS ROW'S LIST LEAVES
    # OUT (#1376). Two lists can serve one folder with different
    # exclusions, and an operator can change them while an archive
    # waits. An archive packed for another rule is left to the request
    # it was packed for, and this one is packed beside it, at a path of
    # its rule's own (_archive_path_leaving_out()).
    ignored = pack_leaves_out(next_file)
    long_target = platform_compat.long_path(target_rar_path)
    job = None
    with queue_lock:
        withdrawn = not _row_is_still_queued(next_file)
        in_use = (not withdrawn and os.path.exists(long_target)
                  and temp_archive_in_use(target_rar_path))
        if in_use and not _archive_packed_without(target_rar_path, ignored):
            target_rar_path = _archive_path_leaving_out(target_rar_path, ignored)
            long_target = platform_compat.long_path(target_rar_path)
            in_use = os.path.exists(long_target) and temp_archive_in_use(target_rar_path)
        reuse = in_use and _archive_packed_without(target_rar_path, ignored)
        # Wanted, and packed for another rule, at both paths: nothing
        # may be packed over it.
        taken = in_use and not reuse
        if reuse:
            point_row_at_archive()
        elif not withdrawn and not taken:
            # `rar a` ADDS to an existing archive rather than
            # replacing it - a stale file left behind by an
            # earlier crashed run would otherwise silently have
            # the new album packed on TOP of whatever was
            # already there. Removed first so a fresh pack
            # always starts from nothing. Nothing needs it:
            # temp_archive_in_use() said so above.
            if os.path.exists(long_target):
                try:
                    os.remove(long_target)
                except OSError as unlink_err:
                    print(f"[LINEAR RAR] Could not remove a stale archive at "
                          f"{target_rar_path}: {unlink_err}")
            # Registered here, under the lock that answered,
            # so temp_archive_in_use() counts this pack from
            # now on (#1202 for the job itself).
            job = _begin_pack_job(owner_nick(), folder_leaf, true_source_dir, target_rar_path, row=next_file)

    if withdrawn:
        # The user took the row out of their queue before the
        # pack began (#1268): nothing to pack, nothing to send.
        # Or their pause did (#1468), and the log says that.
        if send_fails_paused(owner_nick()):
            print(f"[LINEAR RAR] {folder_leaf} is not packed: "
                  f"{_paused_pack_reason(owner_nick())}.")
        else:
            print(f"[LINEAR RAR] {owner_nick()} removed {folder_leaf} before it was packed.")
        _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_CANCELLED)
        return False

    if taken:
        raise PackRefused(f"an archive of {folder_leaf} packed for another list is "
                          f"still waiting at {target_rar_path}")

    if reuse:
        print(f"[LINEAR RAR] {folder_leaf} is already packed and waiting for another "
              f"request; {owner_nick()} is sent the same archive: {target_rar_path}")
    else:
        # A timeout is essential: with timeout=None a hung rar blocks this
        # thread forever while config.rar_inprogress stays True, wedging folder
        # packing for EVERY user until the daemon is restarted.
        rar_timeout = getattr(config, 'RAR_TIMEOUT', 1800)
        # See commands.py's note on the same call. rar is not
        # Python, so nothing can guard what it writes - a
        # filename in its error output is decoded here or
        # nowhere, and a pack that failed for a nameable
        # reason must not become a pack that failed silently.
        #
        # Run as a process this module holds a handle to (#1202),
        # so the operator can see how far it has got and stop
        # THAT process. The timeout stays: it is the backstop for
        # a rar nobody is watching.
        list_file = None
        try:
            # THE ARCHIVE HOLDS WHAT THE LIST LISTS (#1376): the files
            # update_list.pack_members() finds by the scan's own rules,
            # named to rar one by one in a list file. `rar a <folder>`
            # took everything under it, the types the list leaves out
            # included. Named rather than excluded with -x masks: a file
            # a mask fails to match would go in, and whether *.flac
            # matches Track.FLAC is rar's platform's rule, not the list's.
            # Named as a filter (-n@) on the folder rar is given, as it
            # always was: rar walks it by its own absolute path, so a
            # folder deeper than Windows' 260 characters packs as before,
            # and nothing it walks goes in unless it is named.
            source_abs = os.path.abspath(true_source_dir)
            parent, leaf = os.path.split(source_abs)
            members = update_list.pack_members(source_abs, ignored)
            members, outside = _members_inside_the_library(source_abs, members)
            if outside:
                print(f"[SECURITY] {len(outside)} file(s) in {folder_leaf} are links to somewhere outside "
                      f"the library and are left out of its archive.")
            packed_bytes = sum(size for _name, size in members)

            # ROOM FIRST (#1376). Nothing else asks whether the archive
            # fits, and finished archives can wait in TMP_ZIP_DIR while
            # the packer moves on (#1435). Short, this row waits in the
            # queue, keeping its place, until there is room
            # (pack_hold_reason(row)); a pack that fits goes ahead. One
            # that could never fit - more than the whole disk - is refused
            # once rather than waiting for ever. The wrapper releases the
            # packer, once (#714).
            free, disk_total = _room_where_archives_are_built()
            if not _has_room_to_pack(packed_bytes, free):
                needed = _room_needed_to_pack(packed_bytes)
                if disk_total is not None and needed > disk_total:
                    raise PackRefused(f"{folder_leaf} needs {_megabytes(needed)} where archives are "
                                      f"built, and that disk holds {_megabytes(disk_total)} in all")
                _hold_packs_for_space(next_file, owner_nick(), folder_leaf, packed_bytes, free)
                return None
            _end_space_hold(next_file)

            lines, left_out = _rar_list_file_lines(parent, leaf, [name for name, _size in members])
            if left_out:
                print(f"[LINEAR RAR] {left_out} file(s) in {folder_leaf} are left out of "
                      f"its archive: rar could not be given their names exactly.")
            if not lines:
                raise PackRefused(f"nothing in {folder_leaf} is in the list")
            # The names the archive may hold: each line from the folder's own name.
            from_parent = len(parent.rstrip(os.sep)) + len(os.sep)
            expected = {line[from_parent:].replace(os.sep, "/") for line in lines}

            print(f"[LINEAR RAR] Starting to pack: {true_source_dir} -> {target_rar_path}")
            announce_mod.send_debug(f"Packing {folder_leaf} for {owner_nick()}", category="PACK")

            # Arguments are passed as a list, never through a shell:
            work_dir_switch = f"-w{os.path.abspath(config.TMP_ZIP_DIR)}"
            # Resolve the binary rather than trusting a bare name on PATH:
            # WinRAR installs rar.exe outside PATH entirely.
            rar_bin = platform_compat.rar_command(getattr(config, 'RAR_BINARY', None))
            if not rar_bin:
                raise FileNotFoundError(
                    "rar executable not found - set config.RAR_BINARY or put rar on PATH")
            # -ep1 keeps the folder by its own name in the archive;
            # -scul or -scfl says how the list file is written.
            list_file, list_charset = _write_rar_list_file(lines)
            cmd = [rar_bin, "a", "-ep1", list_charset, "-n@" + list_file, work_dir_switch,
                   os.path.abspath(target_rar_path), source_abs]
            process = _run_rar(job, cmd, rar_timeout)
        except subprocess.TimeoutExpired:
            # rar was killed mid-write: whatever it wrote sits at
            # the target path, and nothing else ever names that
            # file (#717). Removed here; the wrapper's handling of
            # the failure is unchanged.
            _discard_partial_archive(target_rar_path, "timed out")
            raise
        finally:
            _end_pack_job(job)
            if list_file is not None:
                try:
                    os.remove(list_file)
                except OSError:
                    pass

        if process.returncode == 0 and os.path.exists(target_rar_path) and not job["cancelled"]:
            names, unlisted = _archive_names(rar_bin, os.path.abspath(target_rar_path), rar_timeout)
            if names is None:
                # rar could not say what it made: tried again, as a pack
                # that failed is (its retries).
                print(f"[LINEAR RAR ERROR] The archive of {folder_leaf} could not be listed, "
                      f"so it is not sent: {unlisted}")
                _discard_partial_archive(target_rar_path, "could not be listed")
                announce_mod.send_debug(
                    f"Pack FAILED for {owner_nick()}: the archive of {folder_leaf} could not be listed.",
                    category="PART")
                release_queue_entry(owner_nick(), next_file, delivered=False,
                                    reason="the archive could not be listed", told=PACK_FAILED_TO_USER)
                _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_PACK_FAILED, job=job)
                return False
            stray, missing = _what_the_archive_got_wrong(names, expected)
            if missing and not stray and len(missing) < len(expected):
                # SHORT OF A LISTED FILE, NOTHING ELSE IN IT: sent, and said
                # loudly. It holds nothing the list leaves out; what it lacks
                # (a file gone since the walk, a name rar would not take) can
                # still be asked for by name.
                print(f"[LINEAR RAR ERROR] The archive of {folder_leaf} lacks {len(missing)} of the "
                      f"{len(expected)} file(s) asked for, and is sent without them: {missing[:5]}")
                announce_mod.send_debug(
                    f"Pack of {folder_leaf} for {owner_nick()} lacks {len(missing)} file(s); sent without them.",
                    category="PACK")
            elif stray or missing:
                # A name nobody asked for, or nothing that was: packing it
                # again would make the same archive, holding every other
                # pack meanwhile.
                print(f"[LINEAR RAR ERROR] The archive of {folder_leaf} is not what was asked for, so it "
                      f"is not sent: {len(stray)} name(s) nobody asked for {stray[:5]}, "
                      f"{len(missing)} asked-for file(s) missing {missing[:5]}.")
                _discard_partial_archive(target_rar_path, "was not what was asked for")
                raise PackRefused(f"the archive of {folder_leaf} was not what was asked for "
                                  f"({len(stray)} name(s) nobody asked for, {len(missing)} missing)")

    if job is not None and job["cancelled"]:
        _discard_partial_archive(target_rar_path, "was cancelled")
        if job.get("withdrawn"):
            # The user's own remove stopped it (#1268), or their
            # pause did (#1468, with its reason), and that already
            # took the row and told them.
            if job.get("stop_reason"):
                print(f"[LINEAR RAR] The pack of {folder_leaf} for {owner_nick()} is "
                      f"stopped: {job['stop_reason']}.")
            else:
                print(f"[LINEAR RAR] {owner_nick()} removed {folder_leaf} while it was "
                      f"being packed; the pack is stopped.")
        else:
            announce_mod.send_debug(
                f"Pack for {owner_nick()} cancelled: {folder_leaf}", category="PACK")
            release_queue_entry(owner_nick(), next_file, delivered=False,
                                reason="cancelled by the operator", cancelled=True,
                                shown_name=rar_filename)
        _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_CANCELLED, job=job)
        return False

    if reuse or (process.returncode == 0 and os.path.exists(target_rar_path)):
        if not reuse:
            print(f"[LINEAR RAR] Compression succeeded. Waiting 2.0s for the disk to sync...")
            time.sleep(2.0)

        final_size = os.path.getsize(target_rar_path)
        print(f"[LINEAR RAR] The archive is settled on disk: {final_size:,} bytes")

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
        #
        # To the nick the claim is filed under NOW (#1268): a
        # rename while rar ran used to have the archive offered
        # to a nick that had gone. The row the claim carries
        # names the archive, so temp_archive_in_use() sees it.
        #
        # AND THE ROW MAY HAVE GONE WHILE RAR RAN (#1268): the
        # user's own remove, between the pack ending and this
        # point, when nothing marks the row as in flight.
        # Looked at in the same hold that claims the slot, so
        # a removed folder is never sent - and its archive
        # goes unless another row is waiting for it.
        #
        # ONE LARGE FILE AT A TIME HERE TOO (#1435). The archive's
        # size is known now, so the row records it as a queued
        # file's does (#1387) - stat()ed above, outside the lock -
        # and a large archive waits, keeping its place, while
        # another large file is being sent: the same outcome as
        # no room. The row is a plain file from here on, so the
        # sweep the large send's end triggers dispatches it, and
        # the claim below counts as large until the send reads
        # its size.
        #
        # NOT TO A NICK PAUSED WHILE RAR RAN (#1460), by
        # DAILY_SEND_FAILS_LIMIT - a rename can carry a pause
        # onto the owner. Read before queue_lock, never inside
        # it (#1452); the row is pointed at the archive all the
        # same, so the drop below takes the archive with it.
        paused_fails = send_fails_paused(owner_nick())
        room = False
        large_waits = False
        with queue_lock:
            withdrawn = not _row_is_still_queued(next_file)
            if withdrawn:
                if not temp_archive_in_use(target_rar_path):
                    _discard_partial_archive(target_rar_path, "was packed for a removed request")
            else:
                if not reuse:
                    point_row_at_archive()
                next_file[QUEUED_SIZE_KEY] = final_size
                send_to = owner_nick()
                room = len(config.active_transfers) < config.MAX_DCC_SLOTS
                if paused_fails:
                    # No slot for a paused owner: dropped below (#1460).
                    room = False
                if room:
                    large_waits = waits_for_a_large_send([next_file])
                    if not large_waits:
                        config.active_transfers.append({"user": send_to, "file": rar_filename, "bytes_sent": 0, "next_file_obj": rar_filename,
                                                         "queue_row": next_file})
        if withdrawn:
            # Read again, outside queue_lock: a pause that landed after the
            # read above took the row too (#1468), not the user.
            if paused_fails or send_fails_paused(owner_nick()):
                print(f"[LINEAR RAR] The packed {folder_leaf} is not sent: "
                      f"{_paused_pack_reason(owner_nick())}.")
            else:
                print(f"[LINEAR RAR] {owner_nick()} removed {folder_leaf} while it was "
                      f"being packed; it is not sent.")
            _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_CANCELLED, job=job)
            return False
        if paused_fails:
            print(f"[DCC PAUSE] {send_to} is paused; the packed {rar_filename} is not sent.")
            # stop_pack=False (#1468): this thread is the packer of this very
            # row and its pack has ended (_end_pack_job() above); it still
            # holds rar_inprogress, so no other pack is running to stop.
            _pause_for_send_fails(send_to, _send_fails_key(send_to), paused_fails, queued_only=True,
                                  stop_pack=False)
            with queue_lock:
                dropped = not _row_is_still_queued(next_file)
            if dropped:
                # Recorded by the folder it was packed from, not the archive.
                folder_row = dict(next_file, path=next_file.get("source_path") or next_file.get("path"))
                _record_pack_outcome(send_to, folder_row, transfer_log.STATUS_CANCELLED, job=job)
            # The interlocks are released by the wrapper's finally (#714).
            return False
        if not room:
            print(f"[DCC-BLOCK] {send_to}: all {config.MAX_DCC_SLOTS} slot(s) busy, "
                  f"the packed archive stays queued for the next trigger.")
            # Released by the wrapper's finally, once (#714).
            return
        if large_waits:
            _say_a_large_file_waits(send_to, next_file)
            # Released by the wrapper's finally, once (#714). Unlike section A's
            # hold-back, no sweep follows for the slot left free: the plain
            # files were offered it by the sweep that ran while rar did, other
            # large heads are held as this one is, and a pack waiting on
            # [RAR-HOLD] is woken by the wrapper (redispatch_waiting_pack()).
            return
        if oserve: oserve.active_downloads = len(config.active_transfers)

        announce_mod.send_dcc_sending_notice(send_to, rar_filename, channel=target_chan)

        threading.Thread(
            target=start_dcc_send,
            args=(sock, send_to, target_rar_path, rar_filename, target_chan, next_file, True),
            daemon=True
        ).start()
        # Ownership of both interlocks now belongs to that send thread (the
        # trailing True is owns_packer), which releases them in its own
        # finally. Pack and send stay serialised.
        return True
    else:
        error_msg = process.stderr.strip() if process.stderr else "Unknown RAR engine issue"
        print(f"[LINEAR RAR ERROR] {error_msg}")
        _discard_partial_archive(target_rar_path, "rar exited " + str(process.returncode))
        announce_mod.send_debug(f"Pack FAILED in queue slot for {owner_nick()}: {error_msg}", category="PART")
        # Charge the failure to the retry budget instead of recursing. The old
        # code cleared the interlocks and called check_queue_and_send inline,
        # which re-selected the SAME row and started another packer thread from
        # inside this one - unbudgeted, and the wrapper finally would then strip
        # the interlocks that new thread had just claimed.
        release_queue_entry(owner_nick(), next_file, delivered=False,
                            reason="rar exited " + str(process.returncode), told=PACK_FAILED_TO_USER)
        _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_PACK_FAILED, job=job)
        return False


def claim_next_in_line(passed_over=None, paused=None):
    """Section B of check_queue_and_send(): under queue_lock, give a free
    slot to the next in line and claim them. Returns (promoted,
    promoted_key, pack_to_wake, absent_users):

      promoted      (nick, path, file, channel, row) claimed, or None
      promoted_key  its queue key, or None
      pack_to_wake  the nick whose folder pack has waited longest and may
                    start now (#1034), or None
      absent_users  [(nick, channel)] not present anywhere, to freeze

    Nothing here is sent or announced: the notice stat()s the library and
    must not run under the lock (#605), so the caller does that, as it
    always did. With every slot taken by the time the lock is held, nothing
    is claimed - check_queue_and_send() used to return there, and with
    nothing claimed what follows it does nothing.

    Was section B's sweep inside check_queue_and_send() until #1304, step 3
    of splitting dcc.py's largest functions (docs/FUTURE.md). Moved
    unchanged. Since #1387 a nick whose next file is large is passed over
    while another large file is being sent; `passed_over`, a list if given,
    gets the nick of each one, so the caller knows to fill the slot it left.

    A nick paused by DAILY_SEND_FAILS_LIMIT is passed over (#1460) and the
    sweep goes on to the next in line. `paused` is {nick_key: fails}, read
    before queue_lock is taken (drop_queues_of_paused_nicks()); None reads
    it here, before the lock too - failed_transfers_lock is never taken
    inside queue_lock.
    """
    promoted = None
    promoted_key = None
    pack_to_wake = None
    absent_users = []
    if paused is None:
        paused = paused_for_send_fails()
    with queue_lock:
        # FIXED: re-check the slot count INSIDE the lock. The test above is already
        # stale by the time we acquire, so two concurrent callers could both pass it
        # and overshoot MAX_DCC_SLOTS.
        if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
            return promoted, promoted_key, pack_to_wake, absent_users


        forget_stamps_of_empty_queues()
        # A list waiting at a queue's head is offered the slot first (#1205),
        # while no other list that went first is being sent; then the
        # longest wait, as before.
        lists_first = a_list_may_go_first()
        # One large file at a time (#1387). Read once: the sweep claims at
        # most one nick and then stops, and a claim carries its row, whose
        # recorded size a concurrent caller then counts.
        large_limit = _large_file_bytes()
        large_running = _a_large_send_is_running(large_limit)
        for waiting_user, user_files in sorted(config.dcc_queue.items(),
                                               key=lambda entry: list_first_rank(entry[0], entry[1], lists_first)):
            # Use the dcc_queue dict key for every lock/queue operation. The old code
            # tested the guards with one key and then rebound w_key to the display
            # name further down, so the guard and the claim could disagree.
            queue_key = str(waiting_user).lower()

            if hasattr(config, 'user_processing_lock') and queue_key in config.user_processing_lock:
                continue

            # Paused (#1460): never sent, and passed over before it can
            # take the slot kept for a pack below. The dispatcher drops
            # the rows (drop_queues_of_paused_nicks()).
            if _send_fails_key(queue_key) in paused:
                continue

            if not user_files or len(user_files) == 0 or queue_key in config.frozen_queues:
                continue

            # FIXED (issue #4): user_files is the LIST of this user's queued files, not a
            # single file. Without [0] the isinstance test below was always true (a list is
            # never a dict), so every waiting user was skipped and section B was dead code.
            g_next = user_files[0]
            if not isinstance(g_next, dict):
                continue
            _pack_again_if_packed_before(g_next)

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
                    # A PACK WHOSE TURN HAS COME GETS THE SLOT (#1034). Only the
                    # specific-user path (section A) can start a pack, and every
                    # caller hands it the nick that just finished - so a pack
                    # turned away at [DCC-BLOCK], all slots busy, was never
                    # tried again: queued until its owner's next transfer
                    # ended, which for a nick with only the pack is never.
                    # In wait order with a slot free, so its owner's own dispatch
                    # is started (after the lock) and a slot is kept for it: a
                    # nick further on is promoted only if a second one is free,
                    # or with one slot the plain file would win the race every
                    # time and the pack wait for ever again. While another pack
                    # is being made it could not start; the packer's release
                    # wakes it ([RAR-HOLD], redispatch_waiting_pack()), and the
                    # sweep looks further, as it always did. So while packs
                    # are held (#1376): a send ending, or the watcher, wakes it.
                    if packs_can_start(g_next):
                        if pack_to_wake is None:
                            pack_to_wake = real_username
                        continue
                    print(f"[DCC QUEUE] Folder pack already pending for {real_username}. Skipping to the next waiting user.")
                    continue

                # The slot kept for a pack woken above (#1034).
                if pack_to_wake is not None and len(config.active_transfers) + 1 >= config.MAX_DCC_SLOTS:
                    break

                # Passed over, keeping its place, while another large file
                # is being sent (#1387): the slot goes to the next in line.
                if large_running and _row_is_large(g_next, large_limit):
                    _say_a_large_file_waits(real_username, g_next)
                    if passed_over is not None:
                        passed_over.append(real_username)
                    continue

                # CLAIM the user before releasing the lock, so a concurrent caller sees
                # them as busy. start_dcc_send's finally already discards this key on
                # every exit path, including the early aborts.
                if not hasattr(config, 'user_processing_lock'):
                    config.user_processing_lock = set()
                config.user_processing_lock.add(queue_key)

                claim = {"user": real_username, "file": g_name, "bytes_sent": 0, "next_file_obj": g_name,
                         "queue_row": g_next}
                # Counted by the cap only when the list took the slot from
                # a nick that had waited longer (#1205).
                if (lists_first and is_a_list_row(g_next)
                        and a_list_passes_a_longer_wait(queue_key, packs_can_start())):
                    claim["list_went_first"] = True
                config.active_transfers.append(claim)
                promoted = (real_username, g_path, g_name, g_chan, g_next)
                promoted_key = queue_key
                break

    return promoted, promoted_key, pack_to_wake, absent_users


def check_queue_and_send(irc_sock, completed_user, fill_pass=False):
    """Check the queues and run RAR packing one at a time, without flooding the server.

    fill_pass is the one extra sweep a dispatch starts for a slot a held
    large file left free (#1387): it starts no further one.
    """
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
            frozen = list(config.frozen_queues.items())
            # ONE READ OF THE CHANNEL LISTS PER SWEEP, not one per frozen nick
            # (#1140). user_is_present_in_ram() walks every nick in every
            # channel, and this runs under queue_lock on every finished
            # transfer - after a netsplit, with a hundred queues frozen, that
            # was a hundred full scans (about 180 ms) with requests waiting on
            # the lock. The set compares exactly as that function does, both
            # sides lowercased, so a nick stored in any case still matches. A
            # single frozen nick keeps the early-exit scan, which is cheaper
            # than building the whole set.
            if len(frozen) > 1:
                present = nicks_in_our_channels()
                is_back = lambda nick: str(nick).lower() in present
            else:
                is_back = user_is_present_in_ram
            for f_user, freeze_timestamp in frozen:
                # THAW: the user is back in memory - release the freeze instead of deleting
                if is_back(f_user):
                    del config.frozen_queues[f_user]
                    print(f"[DCC FREEZE-THAW] {f_user} is back in the channel list. Their queue was saved.")
                    continue
                # The same clock as the timer thread (#652): the timestamp
                # has the bot's own downtime taken out of it, and a channel
                # the bot has no member list for yet is not evidence of
                # absence - see frozen_users_channel_is_synced().
                if (current_time - freeze_timestamp) > FREEZE_TIMEOUT and frozen_users_channel_is_synced(f_user):
                    if f_user in config.dcc_queue:
                        # The archives no other row or send needs (#1268).
                        discard_orphaned_temp_archives(f_user)
                        del config.dcc_queue[f_user]
                        db.save_dcc_queue()
                    if f_user in config.frozen_queues:
                        del config.frozen_queues[f_user]
                    print(f"[DCC QUEUE_CLEAN] {f_user} was frozen for over "
                          f"five minutes and never came back. Queue dropped.")

    # A NICK PAUSED BY DAILY_SEND_FAILS_LIMIT IS SENT NOTHING (#1460). Its
    # queue is dropped here, as the pause drops it, and both claims below
    # pass it over: the request gate was the only place that asked, so rows
    # a rename or a NICK left under a paused nick were sent. Read before
    # queue_lock is taken, never inside it (#1452).
    paused = drop_queues_of_paused_nicks()

    if user_key == "system_next_trigger_fallback":
        user_key = ""

    next_file = None
    with queue_lock:
        if user_key and user_key in config.dcc_queue and config.dcc_queue[user_key]:
            if user_key not in config.frozen_queues and _send_fails_key(user_key) not in paused:
                next_file = config.dcc_queue[user_key][0]  # FIXED: takes the top entry
                _pack_again_if_packed_before(next_file)

    # THE FREED SLOT GOES TO WHO HAS WAITED LONGEST (#1032). Handing it straight
    # back to the nick that just finished meant a nick with a long queue took
    # every slot in turn while the others waited for it to run dry. A folder
    # pack is left to this path: section B cannot start one.
    #
    # A folder pack that has waited longer counts too (#1034), while no other
    # pack is being made: the sweep below now hands the slot to it. While one
    # is, the pack could not start anyway - the packer's own release wakes it.
    #
    # A list waiting at the head of a queue comes before all of them (#1205),
    # while no other list that went first is being sent: list_first_rank()
    # puts it ahead. A list of this nick's own that takes the slot from a nick
    # that waited longer is marked as having gone first, which is what the cap
    # counts.
    list_went_first = False
    if next_file and not (isinstance(next_file, dict) and next_file.get('is_unpacked_rar_folder')):
        with queue_lock:
            packs_may_start = packs_can_start()
            lists_first = a_list_may_go_first()
            my_rank = list_first_rank(user_key, config.dcc_queue.get(user_key), lists_first)
            ranked = sorted((list_first_rank(k, config.dcc_queue.get(k), lists_first), k)
                            for k in nicks_waiting_for_a_slot(user_key, plain_files_only=not packs_may_start))
            waited_longer = [(rank, k) for rank, k in ranked if rank < my_rank]
            if not waited_longer and my_rank[0] == 0:
                list_went_first = a_list_passes_a_longer_wait(user_key, packs_may_start)
        if waited_longer:
            rank, ahead = waited_longer[0]
            why = "has a list waiting" if rank[0] == 0 else "has waited longer"
            print(f"[DCC QUEUE] {completed_user} goes to the back of the line; {ahead} {why}.")
            next_file = None


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

                    pack_turned_away = None
                    # Held (#1376): the disk is short, or enough packed
                    # archives already wait. The row keeps its place.
                    pack_held = None if user_already_locked or pack_in_progress else pack_hold_reason(next_file)
                    if user_already_locked:
                        pack_turned_away = f"[RAR-BLOCK] {completed_user} is already locked in memory; blocking a stale thread."
                    elif pack_in_progress:
                        pack_turned_away = f"[RAR-HOLD] {completed_user} waits in the queue while another packing run is in progress..."
                    elif pack_held is not None:
                        pack_turned_away = f"[RAR-HOLD] {completed_user}'s folder pack waits in the queue: {pack_held}."
                    else:
                        config.rar_inprogress = True
                        if not hasattr(config, 'user_processing_lock'):
                            config.user_processing_lock = set()
                        config.user_processing_lock.add(completed_user.lower())
                        # Who holds the claim, kept where a rename can follow
                        # it (#1268): irc.note_nick_change() renames this
                        # holder under queue_lock as it moves the lock.
                        pack_owner = {"nick": completed_user}
                        runtime.pack_owner = pack_owner

                if pack_turned_away:
                    # A pack that takes no slot leaves the one just freed to the
                    # nick that has waited longest (#1038): nothing else would
                    # dispatch it, and a newcomer is kept out of it meanwhile.
                    print(pack_turned_away)
                    if pack_held is not None:
                        # Woken once it is not held (#1376).
                        _arm_pack_hold_watch(irc_sock)
                    check_queue_and_send(irc_sock, "system_next_trigger_fallback")
                    return

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
                        # The packing itself is pack_folder_for_request() (#1300).
                        handed_off = pack_folder_for_request(
                            sock, next_file, owner_nick, target_chan, oserve)
                    except PackRefused as refused:
                        # Refused once (#1376): trying again could only give
                        # the same answer, so no retry is used up.
                        # The user is told PACK_FAILED_TO_USER, once, by
                        # release_queue_entry(); the reason names paths and
                        # sizes of this machine, so it goes to the log.
                        print("[LINEAR RAR] " + str(owner_nick()) + "'s pack is refused: " + str(refused))
                        release_queue_entry(owner_nick(), next_file, delivered=False,
                                            reason="pack refused: " + str(refused), retry=False,
                                            told=PACK_FAILED_TO_USER)
                        _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_PACK_FAILED)
                    except Exception as packer_err:
                        print("[LINEAR RAR ERROR] Packing failed for " + str(owner_nick()) + ": " + str(packer_err))
                        # As a refusal: the user is told PACK_FAILED_TO_USER
                        # once the retries are spent, never the error, which
                        # can carry a path (#1376).
                        release_queue_entry(owner_nick(), next_file, delivered=False,
                                            reason="pack failed: " + str(packer_err),
                                            told=PACK_FAILED_TO_USER)
                        # A pack that raised - rar missing, a timeout, a full
                        # disk - is a request that ended unsent (#1203).
                        _record_pack_outcome(owner_nick(), next_file, transfer_log.STATUS_PACK_FAILED)
                    finally:
                        # The pack itself is over either way (#651): what is
                        # handed off is the SEND, which active_transfers
                        # already counts.
                        if runtime.packer_thread is threading.current_thread():
                            runtime.packer_thread = None
                        # The claim is let go of under queue_lock, the lock a
                        # rename moves it under (#1268): by the nick it is
                        # held under now. A handed-off send follows the
                        # rename through its own transfer row (#598).
                        with queue_lock:
                            if runtime.pack_owner is pack_owner:
                                runtime.pack_owner = None
                            holder = owner_nick()
                        if not handed_off:
                            config.rar_inprogress = False
                            # #215: this release is the only moment another user's held pack can
                            # start. Nothing else revisits them - every check_queue_and_send()
                            # caller passes the user who just finished, never the one turned
                            # away at [RAR-HOLD].
                            redispatch_waiting_pack(irc_sock, just_finished=holder)
                            if hasattr(config, 'user_processing_lock'):
                                config.user_processing_lock.discard(holder.lower())

                def owner_nick():
                    # The nick the claim is filed under now (#1268).
                    return str(pack_owner["nick"])

                # At exactly the right level, so it wakes the function above immediately
                threading.Thread(target=inline_rar_packer, args=(irc_sock,), daemon=True).start()
                # Packing holds no slot, and a pack can take up to RAR_TIMEOUT: the
                # slot just freed goes to the longest-waiting nick meanwhile (#1038).
                # The packed archive is a plain row and waits its turn for a slot.
                check_queue_and_send(irc_sock, "system_next_trigger_fallback")
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

                    # ONE LARGE FILE AT A TIME (#1387), asked in the hold that
                    # claims: their own next file waits, keeping its place,
                    # while another large file is being sent, and the slot
                    # goes to the sweep.
                    large_waits = waits_for_a_large_send([next_file])
                    # The nicks this slot passes over for a large file:
                    # nicks_waiting_for_a_slot() left them out of the line
                    # above, so they did not send this nick to the back.
                    held_back = [key for key, rows in config.dcc_queue.items()
                                 if str(key).lower() != user_key and key not in config.frozen_queues
                                 and waits_for_a_large_send(rows)]

                    # THE LIST CAP, READ AGAIN UNDER THE CLAIM (#1205). The rank
                    # above was taken under an earlier hold of the lock, and a
                    # concurrent trigger may have sent another list ahead since.
                    list_cap_reached = list_went_first and not a_list_may_go_first()
                    if not list_cap_reached and not large_waits:
                        if not hasattr(config, 'user_processing_lock'):
                            config.user_processing_lock = set()
                        config.user_processing_lock.add(completed_user.lower())

                        f_name = next_file['file'] if isinstance(next_file, dict) else os.path.basename(str(next_file))
                        f_path = next_file['path'] if isinstance(next_file, dict) else str(next_file)
                        claim = {"user": completed_user, "file": f_name, "bytes_sent": 0, "next_file_obj": f_name,
                                 "queue_row": next_file}
                        if list_went_first:
                            claim["list_went_first"] = True
                        config.active_transfers.append(claim)

                if list_cap_reached:
                    # The slot goes to whoever has waited longest instead.
                    print(f"[DCC QUEUE] {completed_user}'s list waits: another list went first meanwhile.")
                    check_queue_and_send(irc_sock, "system_next_trigger_fallback")
                    return
                if large_waits:
                    # The sweep passes this nick over too, and says so when
                    # it reaches them: one log line, not two.
                    check_queue_and_send(irc_sock, "system_next_trigger_fallback")
                    return

                # STILL SHARED? (#1268) The row carries the path it was
                # resolved to when it was asked for, and the dashboard's
                # Folders page takes a folder out of the library live. A
                # folder pack is re-checked before rar runs; a plain file was
                # sent from the folder the operator had just stopped sharing.
                if drop_an_unshared_row(completed_user, next_file):
                    check_queue_and_send(irc_sock, "system_next_trigger_fallback")
                    return

                print(f"[DCC QUEUE] Verified live in RAM for {target_chan}! Next file for {completed_user}: {f_name}")
                if oserve: oserve.active_downloads = len(config.active_transfers)

                announce_mod.send_dcc_sending_notice(completed_user, f_name, path=f_path, channel=target_chan)
                threading.Thread(target=start_dcc_send, args=(irc_sock, completed_user, f_path, f_name, target_chan, next_file), daemon=True).start()
                if not fill_pass:
                    _fill_the_slots_a_large_send_left(irc_sock, held_back, next_file)
                return
        else:
            # Not in any of our channels: freeze and start the countdown. The
            # policy lives in freeze_absent_user() so the global sweep below
            # applies exactly the same one (#530). The slot this nick just
            # freed goes on to the next waiting nick (#1038).
            freeze_absent_user(irc_sock, completed_user, target_chan)
            check_queue_and_send(irc_sock, "system_next_trigger_fallback")
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
    pack_to_wake = None
    # The nicks this sweep passed over for a large file (#1387).
    held_back = []
    if len(config.active_transfers) < config.MAX_DCC_SLOTS:
        # The sweep under queue_lock is claim_next_in_line() (#1304).
        promoted, promoted_key, pack_to_wake, swept_absent = claim_next_in_line(passed_over=held_back,
                                                                                paused=paused)
        absent_users.extend(swept_absent)

    if promoted is not None and drop_an_unshared_row(promoted[0], promoted[4], lock_key=promoted_key):
        # Not shared any more (#1268): the slot goes to the next in line.
        promoted = None
        threading.Thread(target=check_queue_and_send, args=(irc_sock, "system_next_trigger_fallback"),
                         daemon=True).start()

    if promoted is not None:
        real_username, g_path, g_name, g_chan, g_next = promoted
        print(f"[DCC QUEUE] New user {real_username} verified live in RAM for {g_chan}. Got slot.")
        if oserve: oserve.active_downloads = len(config.active_transfers)

        announce_mod.send_dcc_sending_notice(real_username, g_name, path=g_path, channel=g_chan)
        threading.Thread(target=start_dcc_send, args=(irc_sock, real_username, g_path, g_name, g_chan, g_next), daemon=True).start()
        if not fill_pass:
            _fill_the_slots_a_large_send_left(irc_sock, held_back, g_next)

    if pack_to_wake is not None:
        print(f"[DCC QUEUE] A slot is free and {pack_to_wake}'s folder pack has waited longest; starting it.")
        threading.Thread(target=check_queue_and_send, args=(irc_sock, pack_to_wake),
                         daemon=True).start()

    for absent_user, absent_chan in absent_users:
        freeze_absent_user(irc_sock, absent_user, absent_chan)


MIN_DCC_BLOCK_SIZE = 4096
MAX_DCC_BLOCK_SIZE = 1024 * 1024
# What a missing or unreadable DCC_BLOCK_SIZE falls back to: the shipped
# default in defaults.py, 64 KB. A test holds the two together.
DEFAULT_DCC_BLOCK_SIZE = 64 * 1024

# How long one block's sendall() may take before the receiver is given up on,
# at a 64 KB block. CPython applies a socket timeout to the WHOLE sendall()
# call, not to each send() inside it, so the slowest link a send survives is
# block / timeout: 64 KB a minute, about 1.1 KB/s. A bigger block with the same
# 60 s would quietly raise that floor - 4.4 KB/s at 256 KB - so the timeout
# grows with the block instead (#1139). A send() loop would not help: on
# Windows one send() accepts the whole block at once.
SEND_TIMEOUT_SECONDS = 60.0
SEND_TIMEOUT_BLOCK = 64 * 1024


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
    # Recorded with no lane (#1386): an ACCEPT the server refuses is not sent
    # again, since the position in it may be stale by then - but the record
    # keeps that refusal from being taken for an older line to this user.
    runtime.note_paced_line(reply, None)
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

    Read through config on every pass of a send (#1461), not captured once:
    !rehash reloads config, and a send that kept the block it started with
    took a bigger share of the upload cap's shared clock than the sends
    started after it.

    CLAMPED, not trusted. This number is multiplied by the number of
    concurrent transfers to give the memory this bot holds in send buffers, so
    a mistyped 500000000 is half a gigabyte per slot. And a value below a few
    kilobytes turns the send loop into a syscall storm for no benefit - 0 or a
    negative would spin it. Neither is worth a startup error when the honest
    thing is to use the nearest usable number and get on with the transfer.
    """
    try:
        wanted = int(getattr(config, "DCC_BLOCK_SIZE", DEFAULT_DCC_BLOCK_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_DCC_BLOCK_SIZE
    return max(MIN_DCC_BLOCK_SIZE, min(MAX_DCC_BLOCK_SIZE, wanted))


def _send_timeout(block):
    """The data socket's timeout for a transfer sending `block` bytes a pass.

    60 s up to 64 KB, exactly as before #1139, and in proportion above it, so
    the slowest receiver a send survives stays 64 KB a minute whatever block
    size is picked. The cost is that a peer which stops reading altogether is
    noticed later - 240 s at 256 KB - while one that stops acknowledging is
    still caught by ACK_STALL_SECONDS between blocks.
    """
    return SEND_TIMEOUT_SECONDS * max(1.0, block / float(SEND_TIMEOUT_BLOCK))


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


def _folder_total_bytes(path):
    total = 0
    for here, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(here, name))
            except OSError:
                pass
    return total


def _begin_pack_job(user, folder_name, source, archive, row=None):
    """Register the pack about to start (#1202) and return its job.

    Registered BEFORE rar is started, so a cancel that arrives in the gap
    still finds something to mark. The folder's size is added by a thread of
    its own: walking a big album must not delay the pack, and the status is
    honest about a total it does not have yet (0).

    `row` is the queue row being packed: the Queue page's controls know it by
    that, not by its place (#1245).
    """
    job = {"user": str(user), "name": str(folder_name), "archive": archive,
           "started": time.time(), "total": 0, "process": None, "cancelled": False,
           "row": row}
    with runtime.pack_lock:
        runtime.pack_job = job

    def measure():
        total = _folder_total_bytes(source)
        job["total"] = total

    threading.Thread(target=measure, daemon=True).start()
    return job


def _end_pack_job(job):
    with runtime.pack_lock:
        if runtime.pack_job is job:
            runtime.pack_job = None
        job["process"] = None
        timer = job.pop("kill_timer", None)
    if timer is not None:
        timer.cancel()


def _run_rar(job, cmd, timeout):
    """Run rar for `job` and return a CompletedProcess, as subprocess.run did.

    The Popen handle is kept on the job while rar runs, which is what
    cancel_pack() terminates. A timeout kills it and is raised, as before.
    """
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=_rar_environment(),
                               text=True, encoding="utf-8", errors="replace",
                               **platform_compat.no_console_window())
    with runtime.pack_lock:
        job["process"] = process
        stop_now = job["cancelled"]
    if stop_now:
        job["kill_timer"] = _stop_process(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _stop_process(process):
    """Ask a process to stop, and make sure it does: terminate now, kill
    five seconds later if it is still there. The caller is a web request or
    a console command, so the second step is a timer, not a wait. The timer
    is returned for the packer to cancel once rar is gone."""
    try:
        process.terminate()
    except OSError:
        return None

    def make_sure():
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass

    timer = threading.Timer(5.0, make_sure)
    timer.daemon = True
    timer.start()
    return timer


def pack_status(now=None):
    """The pack that is running, or None: {user, name, elapsed, done, total}.

    `name` is the folder's name as the list shows it, never its path. `done`
    is the size of the archive so far, `total` the folder's size - rar
    compresses, so the two are a guide, not a fraction of the work, and
    `total` is 0 until the folder has been measured.
    """
    job = runtime.pack_job
    if job is None:
        return None
    return _pack_status_of(job, now)


def _pack_status_of(job, now=None):
    now = time.time() if now is None else now
    try:
        done = os.path.getsize(platform_compat.long_path(job["archive"]))
    except OSError:
        done = 0
    return {"user": job["user"], "name": job["name"],
            "elapsed": max(0, int(now - job["started"])),
            "done": done, "total": int(job["total"]),
            "cancelled": bool(job["cancelled"])}


def cancel_pack(row=None, reason=None):
    """Stop the pack that is running. Returns its status as it was, or None
    when nothing is packing - a cancel of nothing changes nothing (#1202).

    Only marks the job and terminates ITS process; the packer thread sees the
    mark when rar returns, removes the partial archive, settles the queue row
    without charging the retry budget, tells the user and lets the next
    request start.

    With `row`, only a pack of that very queue row is stopped, and it is the
    user withdrawing it (#1268): their own remove has already taken the row
    and told them, so the packer neither settles it again nor says the
    operator cancelled it.

    `reason`, with `row`: the row went for something other than the user's
    remove - their pause (#1468) - and the packer logs that instead.
    """
    with runtime.pack_lock:
        job = runtime.pack_job
        if job is None or job["cancelled"]:
            return None
        if row is not None:
            if job.get("row") is not row:
                return None
            job["withdrawn"] = True
            if reason:
                job["stop_reason"] = str(reason)
        job["cancelled"] = True
        process = job["process"]
    status = _pack_status_of(job)
    if process is not None:
        timer = _stop_process(process)
        with runtime.pack_lock:
            job["kill_timer"] = timer
            if runtime.pack_job is not job and timer is not None:
                timer.cancel()
    return status


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


def path_from_published_lists(list_paths, requested_file, requested_size_hint, wanted_list):
    """The path on disk a request names, found by reading the published lists
    `list_paths` in order, or None if they do not place it on disk.

    A row is matched case-insensitively and placed under the folder heading
    above it; `requested_size_hint` (the pasted ::INFO:: size, lower case) picks
    between same-named copies, and the list's own spelling of the name is the
    one returned (#445). Reads files; may raise on one it cannot read, which the
    caller reports.

    Was a block inside handle_download_request() until #1298, the first step of
    splitting dcc.py's largest functions (docs/FUTURE.md). Moved unchanged.
    """
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
        # without it, leaving the os.walk() that followed it
        # (gone since #1376) to answer every request.
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
            return test_path
    return None


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
    # A PRIVATE REQUEST BELONGS TO THE CHANNEL ITS SENDER SHARES WITH US
    # (#1242). Resolved once, here, and stored on the row: the feed names it,
    # the notices follow its mode, and nothing is announced in it. Sharing
    # none, the request is refused - up front, so a free slot and a full
    # queue answer it the same way. target_chan itself stays our nick: the
    # list routing below and the refusals on the way are about the message.
    private = not is_channel_name(target_chan)
    row_chan = target_chan
    if private:
        row_chan = library.shared_channel(user)
        if row_chan is None:
            refuse_unshared_private_request(user, repr(str(requested_file or "")[:120]))
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
                announce.send_dcc_error(user, "ambiguous_list", target_chan)
                return
            if routed != wanted_list:
                print(f"[DCC] {user}'s private request is a {routed!r} row; "
                      f"answering from that list rather than {wanted_list!r}.")
                wanted_list = routed
    # A nick paused by DAILY_SEND_FAILS_LIMIT (#1389) is refused here, before
    # any lookup: the request is not queued, and the notice says why.
    paused_fails = send_fails_paused(user)
    if paused_fails:
        print(f"[DCC PAUSE] Refused a request from {user}: {paused_fails} failed sends "
              f"today, paused until midnight.")
        announce.send_fails_paused_notice(user, paused_fails, target_chan)
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
                announce_mod.send_dcc_error(user, "rar_disabled", target_chan)
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
                announce_mod.send_dcc_error(user, "not_configured", target_chan)
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
                announce_mod.send_pack_error_notice(irc_sock, user, target_chan, reason=announce_mod.PACK_REFUSED_NOT_OFFERED)
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
                announce_mod.send_pack_error_notice(irc_sock, user, target_chan, reason=announce_mod.PACK_REFUSED_NOT_OFFERED)
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
                announce_mod.send_pack_error_notice(irc_sock, user, target_chan, reason=announce_mod.PACK_REFUSED_ARTIST_ROOT)
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
            #
            # Through the disc folders too (#1270): a multi-disc album's row
            # names the album above CD1 and CD2, whose own top level holds no
            # track, and checking only that level refused every one of them.
            # folder_holds_packable() is the rule update_list.rar_row_folder()
            # writes those rows by, so the list and this gate cannot drift.
            #
            # With the list's own left-out types (#1376, #1295), as the scan
            # has them: a packable file the list leaves out gives its folder
            # no row there, and none here either.
            packable = update_list.rar_extensions()
            if packable:
                try:
                    has_packable = update_list.folder_holds_packable(
                        true_source_dir, packable, update_list.ignored_extensions(wanted_list))
                except OSError as scan_err:
                    print(f"[PACK] Could not read {true_source_dir!r} to check "
                          f"what is in it: {scan_err}")
                    has_packable = False
                if not has_packable:
                    print(f"[PACK] Refused {relative_to_root!r} for {user}: "
                          f"it holds nothing in RAR_EXTENSIONS that the list lists.")
                    announce_mod.send_pack_error_notice(irc_sock, user, target_chan, reason=announce_mod.PACK_REFUSED_NOTHING_PACKABLE)
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
                announce_mod.send_pack_error_notice(irc_sock, user, target_chan, reason=announce_mod.PACK_REFUSED_TOO_LARGE, limit_bytes=size_cap)
                announce_mod.send_debug(
                    f"Pack denied for {user}: "
                    f"{config.C_BOLD}{relative_to_root}{config.C_RESET} is "
                    f"larger than MAX_RAR_FOLDER_SIZE. The files in it can "
                    f"still be requested by name.",
                    category="PART")
                return

            with queue_lock:
                already_at = queued_position_of(user_key, true_source_dir)
                if already_at is not None:
                    print(f"[DCC QUEUE] {user} asked again for {os.path.basename(true_source_dir.rstrip('/'))!r}: "
                          f"already queued at #{already_at}, not added again.")
                    announce_mod.send_dcc_already_queued_notice(user, os.path.basename(true_source_dir.rstrip("/")),
                                                                already_at, target_chan)
                    return

                total_global_queued = get_total_queued_count()
                user_queued_count = len(config.dcc_queue.get(user_key, []))

                if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                    announce_mod.send_dcc_error(user, "global_full", target_chan)
                    return

                if user_queued_count >= config.MAX_USER_QUEUE:
                    announce_mod.send_dcc_error(user, "user_full", target_chan)
                    return

                start_waiting(user_key)
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
                    "channel": row_chan,
                    "user_raw": user,
                    "is_unpacked_rar_folder": True,
                    "is_temporary_zip": True,
                    "queued_at": time.time(),
                    # Which list's rule the archive follows (#1376).
                    PACK_LIST_KEY: wanted_list,
                })
                # Only on a private row (#1242): a channel row stays as it was.
                if private:
                    config.dcc_queue[user_key][-1]["private"] = True
                user_pos = len(config.dcc_queue[user_key])

            # Persisted AFTER the lock is released (#605): save_dcc_queue()
            # takes its own snapshot of the dict and fsyncs under disk_lock,
            # and a slow or contended data/ disk must not hold queue_lock
            # while it does - see the file branch below for the full story.
            import db
            db.save_dcc_queue()  # Commit straight to dcc_queue.txt

            print(f"[RAR QUEUE] Queued folder pack {master_rar_filename} for {user} at position #{user_pos}.")

            # One single clean line to the debug channel, nothing more
            announce_mod.feed_event("REQUEST", f"{user} asked for the folder \"{clean_folder_name}\""
                                    f"{privately_from(private, row_chan)} - packing it, sending when done.",
                                    nick=user, channel=row_chan, kind="folder", name=clean_folder_name)

            announce_mod.send_dcc_queue_notice(user, folder_name, user_pos, channel=row_chan)
            threading.Thread(target=check_queue_and_send, args=(irc_sock, user), daemon=True).start()
            return


        # A request copied straight off a search result carries its own
        # "  ::INFO:: <size>" tail (list.py's own two-space convention, but
        # strip_info_suffix() tolerates any spacing and any cross-bot
        # branding after the marker too). That size is the one thing that
        # tells two identically-named copies apart, so it is kept as a hint
        # for the list scan below instead of being thrown away.
        requested_file, requested_size_hint = _requested_name_and_size(requested_file)
        # Before the "/" strip: "//host/share" is a UNC path to Windows, and
        # once its slashes are stripped it would look like a harmless relative
        # one. Refused on the text, before any file system call (#578).
        if names_a_remote_or_absolute_path(requested_file):
            print(f"[SECURITY] Refused {user}'s request: {requested_file!r} names a location, not a file in the library.")
            announce.send_dcc_error(user, "invalid_path", target_chan)
            return
        # A stream, a fork or a trimmed second spelling of a library file:
        # inside the library, so the containment check below would let it
        # through. Refused on the text too, before any file system call.
        if names_a_hidden_part_of_a_file(requested_file):
            print(f"[SECURITY] Refused {user}'s request: {requested_file!r} names a hidden part of a file, not a file in the library.")
            announce.send_dcc_error(user, "invalid_path", target_chan)
            return
        requested_file = requested_file.lstrip("/")

        # The master list lives in LOCAL_LIST_DIR, everything else in the
        # music directory. Matched on the names the list builder writes rather
        # than on ".zip" plus the base name appearing anywhere: with .rar now
        # a list format too, a shared library file called
        # "Someone - DCCore Sessions.rar" would otherwise be looked for among
        # the lists and never found.
        if list_mod.is_list_artifact_name(requested_file):
            # THE ARCHIVE WAITS FOR THE WHOLE REBUILD (#971), as "@nick" does
            # in list.send_file_list(). Other files are served through the
            # scan since #923 - the rebuild does not touch them - but this is
            # the file the swap replaces. A slow send of a same-day archive
            # (a second rebuild keeps its name) started mid-scan held it
            # open, and on Windows the swap's replace gave up and the whole
            # rebuild rolled back. The background audio reading's own swap
            # (#1182) waits the same way - list.list_archive_waits().
            if list_mod.list_archive_waits():
                oserve = sys.modules.get('oserve')
                if oserve:
                    oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Notice{config.C_RESET}: Master list is currently rebuilding. Please wait a few minutes and try again. \r\n")
                print(f"[MAINTENANCE BLOCK] Refused {user}'s request for the list archive "
                      f"{requested_file!r}: the list is being rebuilt.")
                return
            # THIS REQUEST'S LIST directory, not LOCAL_LIST_DIR. Every list
            # writes its archive under the same name, and a non-primary list
            # writes it in its own subdirectory - so looking in the root would
            # answer "file not found" for every list but one, which is the
            # whole of what send_file_list() just offered them.
            base_directory = os.path.abspath(list_mod.list_dir(wanted_list))
            full_path = os.path.join(base_directory, requested_file)
            # THIS list's archive and no other. A list archive is asked for by
            # its file name alone - that is all send_file_list() offers - but
            # the name is matched on its last component, and a non-primary
            # list's directory sits INSIDE the primary's. So a name with a
            # folder in front of it passed the containment check below and
            # reached another list's archive - one bound to channels this
            # request did not come from (#26). Only a file directly in this
            # list's own directory is served.
            if (os.path.normcase(os.path.dirname(os.path.abspath(full_path)))
                    != os.path.normcase(base_directory)):
                print(f"[SECURITY] Refused {user}'s request: {requested_file!r} "
                      f"is not an archive of the list this channel is bound to.")
                announce.send_dcc_error(user, "invalid_path", target_chan)
                return
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
                announce.send_dcc_error(user, "not_configured", target_chan)
                return
            # Every configured folder, in the operator's order (#164). The
            # first is still where a bare filename is guessed to be, which is
            # what a single-folder install has always done; the rest are what
            # the list lookup below falls through to.
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
            #
            # THE SIZE HINT IS PART OF WHAT IS REMEMBERED (#962). A scan picks
            # between same-named copies by the pasted ::INFO:: size; a memory
            # keyed on the name alone handed the next request the copy the
            # LAST one wanted - album A's "01 - Intro.mp3" to somebody who
            # pasted album B's. So an exact-name memory is only reused by a
            # request carrying the same hint (or none, as before), and a
            # folder candidate must be the hinted size.
            full_path = (_recall(wanted_list, requested_file, requested_size_hint, search_roots)
                         or full_path)

        if not is_master_zip and not os.path.exists(platform_compat.long_path(full_path)):
            # THE EXPENSIVE PART, BOUNDED (#580). A name that is not in the first
            # folder's root is looked up by streaming every published list - on
            # a large library tens of megabytes, on this request's own thread,
            # for one ~40 byte line (until #1376 a walk of every folder followed).
            # The flood gate no longer counts file requests (#888), so the cost
            # is bounded here instead: a name that just missed is answered
            # from memory, only a few scans run at once, and one nick gets one
            # at a time and a few misses a minute (#969).
            miss_key = (str(wanted_list), str(requested_file).lower().strip())
            if _lookup_missed_recently(miss_key):
                announce.send_dcc_error(user, "file_not_found", target_chan)
                return
            # This nick's share (#969): see LOOKUP_NICK_MISSES.
            nick_key = str(user).strip().lower()
            if not _nick_may_scan(nick_key):
                print(f"[DCC-LOOKUP] {user}'s request for {requested_file!r} refused: their last "
                      f"{LOOKUP_NICK_MISSES} lookups found nothing, within "
                      f"{LOOKUP_NICK_MISS_WINDOW_SECONDS:.0f}s.")
                announce.send_dcc_error(user, "busy", target_chan)
                return
            # WAITS, briefly, rather than refusing at once (#886). Nine rows
            # pasted together arrive within a second or two of each other;
            # bouncing seven of them told the user to "try again in a
            # moment", which is the one thing that risks the flood gate.
            # The wait covers both: this nick's own turn, then a slot.
            waited_from = time.monotonic()
            if not _take_a_scan_turn(nick_key, LOOKUP_SCAN_WAIT_SECONDS):
                print(f"[DCC-LOOKUP] {user}'s request for {requested_file!r} refused: their "
                      f"previous lookup was still running after {LOOKUP_SCAN_WAIT_SECONDS:.0f}s.")
                announce.send_dcc_error(user, "busy", target_chan)
                return
            # What their previous lookup just learned - a sibling's folder,
            # or that this name is not there - answers this one without a
            # scan of its own.
            if _lookup_missed_recently(miss_key):
                _end_scan_turn(nick_key)
                announce.send_dcc_error(user, "file_not_found", target_chan)
                return
            recalled = _recall(wanted_list, requested_file, requested_size_hint, search_roots)
            if recalled is not None:
                full_path = recalled
            elif not _library_scans.acquire(
                    timeout=max(0.0, LOOKUP_SCAN_WAIT_SECONDS - (time.monotonic() - waited_from))):
                _end_scan_turn(nick_key)
                print(f"[DCC-LOOKUP] {user}'s request for {requested_file!r} refused: "
                      f"{MAX_CONCURRENT_LIBRARY_SCANS} library scans were still running after "
                      f"{LOOKUP_SCAN_WAIT_SECONDS:.0f}s.")
                announce.send_dcc_error(user, "busy", target_chan)
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
                # Nothing to read when the memories answered it.
                list_paths = list_mod.all_list_paths(wanted_list) if recalled is None else []
                if list_paths:
                    try:
                        # The lookup itself is path_from_published_lists() (#1298).
                        found_in_lists = path_from_published_lists(
                            list_paths, requested_file, requested_size_hint, wanted_list)
                        if found_in_lists is not None:
                            full_path = found_in_lists
                    except Exception as list_err:
                        print(f"[DCC-LOOKUP ERROR] {list_err}")
                # NO WALK AFTER THE LISTS (#1376). A name the published lists
                # do not place is not found. There used to be a last resort
                # here, from OmenServe: os.walk() every configured folder for
                # the name. It could only find a file no list names, which
                # nobody can have got from this bot, and it had no time
                # limit: over a network share it ran for minutes, holding a
                # scan slot and this nick's turn, so every request of theirs
                # was answered "busy" - and a minute after it ended, the next
                # request for the same name walked again. A file added since
                # the last rebuild is requestable after the next one.
            finally:
                if recalled is None:
                    _library_scans.release()
                _end_scan_turn(nick_key)
            if not os.path.exists(platform_compat.long_path(full_path)):
                _note_lookup_miss(miss_key)
                _note_nick_miss(nick_key)
            else:
                # What it cost to find this is what the next request for it -
                # or for its siblings in the same folder - does not pay. Under
                # the size hint too (#962): see remembered_key above.
                _note_lookup_hit(miss_key + (requested_size_hint,), full_path)


        # Against every legitimate root rather than one. is_safe_path() itself
        # is unchanged - each comparison still resolves symlinks and compares
        # per separator - so what widened is which roots count as legitimate,
        # not how any one of them is tested. A path outside all of them is
        # still refused exactly as before.
        if not any(is_safe_path(root, full_path) for root in search_roots):
            announce.send_dcc_error(user, "invalid_path", target_chan)
            return
        # Inside the library is not enough: a stream of a library file is
        # inside it too. Whatever produced full_path, it must name a file.
        if resolves_to_a_stream(full_path):
            print(f"[SECURITY] Refused {user}'s request: {full_path!r} is a stream of a file, not a file.")
            announce.send_dcc_error(user, "invalid_path", target_chan)
            return

        if not os.path.exists(platform_compat.long_path(full_path)) or os.path.isdir(platform_compat.long_path(full_path)):
            announce.send_dcc_error(user, "file_not_found", target_chan)
            return

        # WHAT THE LIST LEAVES OUT IS NOT SERVED (#1376). The list is what
        # this bot serves, and a file type it leaves out - LIST_IGNORED_EXTENSIONS
        # or the list's own exclusions (#1295) - is not in it. Two shortcuts
        # above find a file without reading the list: the name in the first
        # folder's root, and a folder a recent lookup resolved into (#886). So
        # the file they found is held to the list's own rule here, the same
        # update_list.is_listed_file() the build uses.
        if not is_master_zip and not update_list.is_listed_file(
                full_path, update_list.ignored_extensions(wanted_list)):
            print(f"[DCC-LOOKUP] {user}'s request for {requested_file!r} refused: "
                  f"the list leaves its file type out.")
            announce.send_dcc_error(user, "file_not_found", target_chan)
            return

        # The row's name. queued_name_of_request() works out the same name
        # from the text, for `@<nick>-remove <file>` (#1473).
        file_name = os.path.basename(full_path)

        # The console feed (#528): the request is real and the file exists -
        # whether it sends now or queues is decided under the lock below, and
        # each of those reports itself (SENDING / QUEUED). A refused request
        # is reported by its refusal.
        announce.feed_event("REQUEST", f'{user} asked for "{file_name}"{privately_from(private, row_chan)}',
                            nick=user, channel=row_chan, kind="file", name=file_name)

        # The size goes on the row now, outside the lock (#1387, #605): the
        # one-large-file rule reads it under queue_lock, where no stat may
        # run. The list is never large, so it gets none.
        queued_size = 0 if is_master_zip else _size_on_disk(full_path)

        with queue_lock:
            # Asking again for what is already waiting adds nothing (#1077):
            # one nick pasting the same ISO every few seconds filled its
            # whole queue with copies of it. Told once, privately.
            already_at = queued_position_of(user_key, full_path)
            if already_at is not None:
                print(f"[DCC QUEUE] {user} asked again for {file_name!r}: already queued at #{already_at}, not added again.")
                announce.send_dcc_already_queued_notice(user, file_name, already_at, target_chan)
                return
            if is_being_sent_to(user_key, full_path):
                print(f"[DCC QUEUE] {user} asked again for {file_name!r}: it is being sent to them now, not added again.")
                announce.send_dcc_already_queued_notice(user, file_name, None, target_chan)
                return

            total_global_queued = get_total_queued_count()
            user_queued_count = len(config.dcc_queue.get(user_key, []))

            if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                announce.send_dcc_error(user, "global_full", target_chan)
                return

            if user_queued_count >= config.MAX_USER_QUEUE:
                announce.send_dcc_error(user, "user_full", target_chan)
                return

            # Check whether this nick ALREADY has a send running, in active_transfers
            user_already_transferring = any(str(tx['user']).lower() == user_key for tx in config.active_transfers)
            
            # Create the temporary send lock in config if it is missing
            if not hasattr(config, 'user_processing_lock'):
                config.user_processing_lock = set()
                
            # If the user just sent rows, check whether the nick is locked in memory
            user_is_processing = user_key in config.user_processing_lock
            user_has_queue = len(config.dcc_queue.get(user_key, [])) > 0

            # A list goes first (#1205): any free slot will do, the nicks
            # waiting for files and the nick's own queued files notwithstanding,
            # unless another list that went first is still being sent.
            list_goes_first = is_master_zip and a_list_may_go_first()

            # Only a user who is clear in transfers, the queue AND the memory lock sends immediately
            # - and not while a rehash is quiescing (#668): this path does
            # not go through check_queue_and_send()'s gate, so a request
            # that reached here during the pause would have started a send
            # the reload then landed in the middle of. Read under the lock.
            sends_now = (not user_already_transferring and not user_is_processing
                         and len(config.active_transfers) < config.MAX_DCC_SLOTS
                         and not transfers_are_paused()
                         and (list_goes_first
                              or (not user_has_queue and a_slot_is_free_beyond_those_waiting(user_key))))
            # ONE LARGE FILE AT A TIME (#1387). A large file is queued,
            # keeping its place, instead of sent now: while another large
            # file is being sent, and while a nick that has waited longer
            # has a large file next - the large turn is theirs. Just after
            # a large send ends that nick is waiting for the trigger it
            # started, a slot is idle, and a newcomer's large file would
            # otherwise take the turn from them.
            #
            # Asked only of a request that would be sent now (the `if
            # sends_now`), on purpose: that is the nick's next file. One
            # queued for any other reason - behind the nick's own files, or
            # with every slot taken - is not held back by this rule, and the
            # log line below would say it was.
            large_limit = _large_file_bytes()
            large_running = False
            large_ahead = None
            if sends_now and _row_is_large({QUEUED_SIZE_KEY: queued_size, "file": file_name}, large_limit):
                large_running = _a_large_send_is_running(large_limit)
                if not large_running:
                    large_ahead = next((key for key in nicks_waiting_for_a_slot(user_key)
                                        if config.dcc_queue.get(key)
                                        and _row_is_large(config.dcc_queue[key][0], large_limit)), None)
            large_waits = large_running or large_ahead is not None
            if large_waits:
                sends_now = False
            # It went first only if the rule for files would have queued it.
            list_went_first = bool(sends_now and list_goes_first
                                   and (user_has_queue or not a_slot_is_free_beyond_those_waiting(user_key)))
            if sends_now:
                # Lock the nick immediately, so the next row goes to the queue
                config.user_processing_lock.add(user_key)

                next_file_fake = {"path": full_path, "file": file_name, "channel": row_chan, "is_temporary_zip": False,
                                  "queued_at": time.time()}
                if private:
                    next_file_fake["private"] = True
                claim = {"user": user, "file": file_name, "bytes_sent": 0, "next_file_obj": file_name,
                         "path": full_path}
                # This claim has no queue row to carry the size, and a
                # concurrent dispatch must see a large file from the claim
                # on, before the send reads its real size (#1387).
                if queued_size:
                    next_file_fake[QUEUED_SIZE_KEY] = queued_size
                    claim[QUEUED_SIZE_KEY] = queued_size
                if is_master_zip:
                    next_file_fake[LIST_ROW_KEY] = True
                if list_went_first:
                    claim["list_went_first"] = True
                config.active_transfers.append(claim)
            else:
                # This user already has a track running; the row goes to dcc_queue.txt
                start_waiting(user_key)
                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []
                config.dcc_queue[user_key].append({"file": file_name, "path": full_path, "channel": row_chan, "user_raw": user, "is_temporary_zip": False,
                                                   "queued_at": time.time()})
                if private:
                    config.dcc_queue[user_key][-1]["private"] = True
                if queued_size:
                    config.dcc_queue[user_key][-1][QUEUED_SIZE_KEY] = queued_size
                if large_running:
                    _say_a_large_file_waits(user, config.dcc_queue[user_key][-1])
                elif large_ahead is not None:
                    print(f"[DCC QUEUE] {user}'s file is large ({queued_size // (1024 * 1024)} MB) and "
                          f"{large_ahead} has waited longer with a large file next - it is queued behind them.")
                user_pos = len(config.dcc_queue[user_key])
                if is_master_zip:
                    user_pos = put_the_list_first(config.dcc_queue[user_key])

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
            # announce_channel_for(); this path never went through it. For a
            # private request it is the channel they share with us (#1242),
            # which the "Sent:" line is then not sent to.
            announce_chan = announce_channel_for(next_file_fake)
            announce.send_dcc_sending_notice(user, file_name, path=full_path, channel=announce_chan)
            threading.Thread(target=start_dcc_send, args=(irc_sock, user, full_path, file_name, announce_chan, next_file_fake), daemon=True).start()
            return

        # Save and update dcc_queue.txt on disk straight away
        import db
        db.save_dcc_queue()

        if is_master_zip:
            announce.send_dcc_queue_notice(user, file_name, user_pos, channel=row_chan, list_is_next=True)
        else:
            announce.send_dcc_queue_notice(user, file_name, user_pos, channel=row_chan)
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

    def sending(self, now=None):
        """More is about to be sent. A receiver that holds everything it has
        been sent owes nothing, so the time since its last acknowledgement
        is not silence, and the stall clock starts again here.

        It used to start at the last acknowledgement whatever happened after
        it, which only held while the sender never paused between chunks.
        Under the bot's own upload cap (#1352) it does: a receiver that had
        acknowledged the last chunk, followed by a wait past
        ACK_STALL_SECONDS, read as stalled the instant the next chunk went.
        """
        if self.acked >= self.sent:
            self.last_advance_at = now if now is not None else time.time()


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


# A SEND THAT CRAWLS WHILE OTHERS WAIT (#1388). The stall check above catches a
# receiver that has stopped; one on a bad link that still moves a few KB/s
# could hold a slot for hours while the queue behind it did not move. With
# SLOW_SEND_MIN_KB above 0 such a send is stopped, as a failed attempt, but
# only while every slot is taken and somebody is waiting for one: with a slot
# free nobody is kept waiting, so it may take as long as it takes.
#
# Measured fairly: bytes the receiver ACKNOWLEDGED, over about the last minute
# rather than since the start, so a burst that recovers is not punished - and
# not at all for the first minute after it connects (TCP ramp-up, a resume
# finding its feet). Never while the bot's own upload cap (#1352) is what holds
# it back: then the cap is slow, not the receiver.
#
# THE CAP SHIELDS ONLY THE SENDS IT HOLDS (#1436). Every send reserves from one
# shared clock under the cap, in chunks of one size, so a send the cap holds
# back moves at least an even split of what the cap let through; one on a
# slow link moves less, and the fast sends take what it leaves - the cap
# stays full while it crawls. So
# a full cap keeps a send only while that send moves at least
# SLOW_SEND_CAP_NEAR of the even split, every send together divided by the
# running sends; under that it is judged as if there were no cap, which is
# what the cap-off case would have done with it.
#
# SLOW HAS TO BE THE RECEIVER'S FAULT. A full uplink with no cap set, a slow
# disk or a cap above what the line really does makes EVERY send slow, and
# stopping each in turn would only burn everybody's MAX_SEND_FAILS. So each
# send publishes its window rate on its transfer row ("acked_bps"), and one is
# stopped only while ANOTHER running send moved at least the floor over its
# own window - proof the bot's side can deliver right now - and only if it is
# the slowest of the rated sends, ties kept: one stop per decision. With one
# slot, or one rated send, it never fires; that is the deliberate safe side.
#
# AND NOT TWICE FOR THE SAME TRADE. A row stopped for its speed carries
# "stopped_for_speed", and a nick waiting with such a row at its head does not
# count as waiting: two slow receivers would otherwise take the slot from
# each other in turn. The mark lives on the queue row, so it is saved in
# dcc_queue.txt with it, survives a restart, and goes when the row does.
SLOW_SEND_GRACE_SECONDS = 60.0   # not judged until this long after it connected
SLOW_SEND_WINDOW_SECONDS = 60.0  # its speed is what was acknowledged over about this long
SLOW_SEND_CHECK_SECONDS = 0.5    # looked at no more often than this, not once per chunk
# The upload cap binds while every send together moves at least this share of
# it, and holds a send back while that send moves at least this share of an
# even split of it.
SLOW_SEND_CAP_NEAR = 0.9
# The transfer-row key a send publishes its window rate under (bytes/s), and
# the queue-row mark of a row stopped for its speed.
ACKED_RATE = "acked_bps"
STOPPED_FOR_SPEED = "stopped_for_speed"

# The watch's clock. A module-level name so a test can drive it, the way
# _AckTracker.stalled() takes now=.
_slow_send_clock = time.monotonic


def slow_send_floor():
    """SLOW_SEND_MIN_KB in bytes per second; 0 when it is off. A value that is
    not a positive number is off. Read the way the speed caps are: the same
    KB/s setting, the same rule for an odd value."""
    return speed_cap.rate("SLOW_SEND_MIN_KB")


def upload_cap_binds(total_bps):
    """Is the bot's own upload cap (#1352) what holds the sends back? True
    while MAX_UPLOAD_KB is set and every send together (`total_bps`) moves
    at least SLOW_SEND_CAP_NEAR of it."""
    cap = speed_cap.rate(speed_cap.UPLOAD)
    return bool(cap) and total_bps >= cap * SLOW_SEND_CAP_NEAR


def upload_cap_holds_this_send(speed, total_bps):
    """Is the upload cap (#1352) what holds THIS send to `speed`? (#1436)

    True while the cap binds (upload_cap_binds(total_bps)) and `speed` is at
    least SLOW_SEND_CAP_NEAR of an even split of `total_bps` between the
    running sends (active_transfers, this one included). Every send reserves
    from one clock under the cap, so a send the cap holds gets at least that
    split; one under it is held by its own link while the others take the
    rest. Both rates in bytes per second, over the same window. A row that
    moves nothing yet (a claimed send still connecting, or one whose offer
    is not accepted yet) counts as running and lowers the split - the safe
    side. Reads active_transfers' length with no lock, as
    the_slowest_while_another_delivers() reads the list.
    """
    if not upload_cap_binds(total_bps):
        return False
    running = max(1, len(config.active_transfers))
    return speed >= SLOW_SEND_CAP_NEAR * total_bps / running


def others_wait_for_a_slot():
    """Is every slot taken, with a nick waiting that a freed one could go to?

    Takes queue_lock, so it is asked only once a send is already too slow.
    The waiting nicks are nicks_waiting_for_a_slot()'s - in a channel, not
    frozen, nothing running - which is who the dispatcher would hand the
    slot to; a nick whose next row is a folder pack counts only while no
    pack is being made, as check_queue_and_send() has it. A nick whose next
    row was itself stopped for its speed does not count (STOPPED_FOR_SPEED).
    """
    with queue_lock:
        if len(config.active_transfers) < config.MAX_DCC_SLOTS:
            return False
        packs_may_start = packs_can_start()
        for key in nicks_waiting_for_a_slot(plain_files_only=not packs_may_start):
            head = config.dcc_queue.get(key, [None])[0]
            if not (isinstance(head, dict) and head.get(STOPPED_FOR_SPEED)):
                return True
        return False


def the_slowest_while_another_delivers(row, speed, floor):
    """Is the bot's side delivering, and this send the one that is slow?

    True while another running send's published window rate ("acked_bps")
    is at least `floor`, and `speed` is under every other published rate -
    a tie is kept. `row` is this send's transfer row; without one, or with no
    other rated send (one slot), the answer is no. Reads a copy of
    active_transfers, no lock: a send joining or leaving mid-read changes
    nothing that the next half-second does not see.
    """
    if row is None:
        return False
    others = [tx.get(ACKED_RATE) for tx in list(config.active_transfers) if tx is not row]
    others = [rate for rate in others if isinstance(rate, (int, float))]
    return (any(rate >= floor for rate in others)
            and all(speed < rate for rate in others))


def _bytes_uploaded():
    """Every byte the bot's sends have put on the wire this session
    (oserve.total_sent_bytes), 0 without the daemon loaded."""
    oserve = sys.modules.get("oserve")
    return int(getattr(oserve, "total_sent_bytes", 0) or 0) if oserve else 0


class _SlowSendWatch:
    """Is this send crawling while others wait for its slot? (#1388)

    check() is called after every chunk and costs one clock read; at most
    every SLOW_SEND_CHECK_SECONDS it takes a sample - when, what the receiver
    has acknowledged, what every send together has moved - publishes its
    window rate on `row` once past the grace, and judges.
    `clock` is _slow_send_clock unless a test hands it one.
    """

    def __init__(self, acked, clock=None, row=None):
        self.clock = clock or _slow_send_clock
        self.row = row
        now = self.clock()
        self.connected_at = now
        self.next_check_at = now + SLOW_SEND_CHECK_SECONDS
        # (when, acknowledged, the bot's total sent), oldest first. The first
        # is where the window starts until a minute has passed.
        self.samples = collections.deque([(now, int(acked), _bytes_uploaded())])

    def check(self, acked):
        """The acknowledged rate in bytes per second when the send should be
        stopped; None to let it go on."""
        now = self.clock()
        if now < self.next_check_at:
            return None
        self.next_check_at = now + SLOW_SEND_CHECK_SECONDS
        uploaded = _bytes_uploaded()
        self.samples.append((now, int(acked), uploaded))
        # The oldest sample kept is the last one at or before the window's
        # start, so the rate always covers at least the whole window.
        while len(self.samples) > 1 and self.samples[1][0] <= now - SLOW_SEND_WINDOW_SECONDS:
            self.samples.popleft()
        if now - self.connected_at < SLOW_SEND_GRACE_SECONDS:
            return None
        then, acked_then, uploaded_then = self.samples[0]
        span = now - then
        if span <= 0:
            return None
        speed = (int(acked) - acked_then) / span
        # Published whether or not the setting is on, so turning it on finds
        # every running send already rated.
        if self.row is not None:
            self.row[ACKED_RATE] = speed
        floor = slow_send_floor()
        if not floor or speed >= floor:
            return None
        # Cheapest first; the lock last.
        if upload_cap_holds_this_send(speed, (uploaded - uploaded_then) / span):
            return None
        if not the_slowest_while_another_delivers(self.row, speed, floor):
            return None
        if not others_wait_for_a_slot():
            return None
        return speed


def too_slow_reason(speed, to_the_user=False):
    """Why a send was stopped for speed (#1388): "sent at N KB/s, under
    SLOW_SEND_MIN_KB (M KB/s) while others waited for a slot" for the log
    and the feed; without the setting's name `to_the_user`."""
    floor = getattr(config, "SLOW_SEND_MIN_KB", 0)
    if to_the_user:
        return (f"it was arriving at {speed / 1024:.1f} KB/s, under this bot's minimum of "
                f"{floor} KB/s while every slot was taken and others waited for one")
    return (f"sent at {speed / 1024:.1f} KB/s, under SLOW_SEND_MIN_KB ({floor} KB/s) "
            f"while others waited for a slot")


def tell_them_it_was_too_slow(user, file_name, speed):
    """Tell a nick its send was stopped for its speed and the file is still
    queued (#1388). A row that was dropped instead is told by
    release_queue_entry(), with too_slow_reason(to_the_user=True) as the
    reason."""
    oserve_mod = sys.modules.get("oserve")
    if not oserve_mod:
        return
    floor = getattr(config, "SLOW_SEND_MIN_KB", 0)

    def _build(shown_name):
        return (f"NOTICE {user} :{config.C_BOLD}Stopped{config.C_RESET}: {shown_name} was "
                f"arriving at {speed / 1024:.1f} KB/s, under this bot's minimum of {floor} KB/s "
                f"while every slot is taken and others are waiting, so the slot went to the "
                f"next in line. It stays in your queue, and your client can resume it when "
                f"your turn comes again.\r\n")
    try:
        oserve_mod.queue_message(user, announce.fit_irc_line(_build, str(file_name)))
    except Exception as notify_err:
        print(f"[DCC QUEUE] Could not notify {user}: {notify_err}")


def drop_an_unshared_row(user, row, lock_key=None):
    """Refuse a claimed queue row whose file is no longer shared (#1268).

    Called by the dispatcher after it claimed the slot and released
    queue_lock - the library check resolves paths, and a hung mount must not
    hold the lock (#605). Returns False for a row that may be sent. Otherwise
    the claim is let go of, the row is dropped (never retried, like a
    poisoned pack row), the user is told, and True is returned.
    """
    file_path = row.get("path", "") if isinstance(row, dict) else str(row)
    if may_still_be_sent(file_path, row):
        return False
    file_name = row.get("file", "") if isinstance(row, dict) else os.path.basename(file_path)
    print(f"[SECURITY] Refused to send {file_name} to {user}: it is no longer "
          f"inside any shared folder. The row is dropped.")
    with queue_lock:
        config.active_transfers[:] = [tx for tx in config.active_transfers
                                      if tx.get("queue_row") is not row]
        getattr(config, "user_processing_lock", set()).discard(str(lock_key or user).lower())
        # By the row itself, under whichever nick holds it now.
        for q_key, q_rows in list(config.dcc_queue.items()):
            kept = [held for held in q_rows if held is not row]
            if len(kept) != len(q_rows):
                if kept:
                    config.dcc_queue[q_key] = kept
                else:
                    del config.dcc_queue[q_key]
    try:
        db.save_dcc_queue()
    except Exception as save_err:
        print("[DCC QUEUE ERROR] Could not persist the queue: " + str(save_err))
    go_to_the_back(str(lock_key or user).lower())
    oserve_mod = sys.modules.get("oserve")
    if oserve_mod:
        def _build(shown_name):
            return (f"NOTICE {user} :{config.C_BOLD}Error{config.C_RESET}: {shown_name} "
                    f"is no longer shared here. Removed from your queue.\r\n")
        oserve_mod.queue_message(user, announce.fit_irc_line(_build, str(file_name)))
    return True


def may_still_be_sent(file_path, next_file):
    """Is this file still one the bot serves? (#1268)

    A file inside a shared folder, by the CURRENT library - or one the bot
    made itself: a packed archive or a list in TMP_ZIP_DIR, a list in its
    list directory, or a row marked as the list (#1205). Those live outside
    every shared folder by design.

    Never a stream of a file: one sits inside a shared folder as far as the
    containment check can tell. handle_download_request() refuses such a
    request, but a row can also come back from dcc_queue.txt - queued by a
    version without that guard, or written into the file - and this is the
    check every queued row passes on its way to a send.
    """
    if resolves_to_a_stream(file_path):
        return False
    if is_a_list_row(next_file):
        return True
    for own_dir in (getattr(config, "TMP_ZIP_DIR", None), getattr(config, "LOCAL_LIST_DIR", None)):
        if own_dir and is_safe_path(own_dir, file_path):
            return True
    return path_is_in_our_library(file_path)


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


def _is_this_send(tx, row, user):
    """Whether an active_transfers entry is this send's: the row itself when
    the send found one, else any entry under the send's nick."""
    if row is not None:
        return tx is row
    return str(tx.get('user', '')).lower() == user.lower()


def _send_nick(row, user):
    """Whatever name the transfer is filed under NOW - the lock moved with it."""
    return str((row or {}).get('user') or user).lower()


SentFile = collections.namedtuple("SentFile", (
    "row", "user", "next_file", "file_path", "file_name", "file_size", "resume_offset", "bytes_sent",
    "transfer_completed", "already_held", "offered_at", "start_time", "transfer_finished_at",
    "channel", "irc_sock", "owns_packer", "outcome_identity", "outcome_status", "acked",
    "never_connected", "assigned_port", "conn", "dcc_sock", "too_slow_bps",
    "receiver_failed"),
    defaults=(None, None, False, None, None, 0, False, None, None, None, None, False))
SentFile.__doc__ = """What start_dcc_send() knows about a send once it has ended (#1328): the
transfer row, the nick, the queue row, the file and its size, where it
resumed, the bytes sent, whether the receiver acknowledged the whole file or
already held it, and when it was offered, started and finished.

Since #1336 also what finish_send() needs to wind it up: the channel and the
IRC socket, whether it owns the packer, the outcome's identity and status,
the bytes the receiver acknowledged, whether it never connected, the port,
and the two sockets. record_completed_send() leaves those at their defaults.

too_slow_bps (#1388) is the rate a send was stopped at for crawling while
others waited for a slot, None for any other ending.

Since #1389 whether the receiver failed it, which counts toward the nick's
DAILY_SEND_FAILS_LIMIT."""


def record_completed_send(sent):
    """Count a finished send, from a SentFile: the totals, the download
    count and the transfer record - or nothing, for a send that ended short
    or a resume of a file the receiver already held. Reports and swallows a
    database failure, as the block in start_dcc_send() always did.

    Was that block until #1328, step 1 of splitting start_dcc_send()
    (docs/FUTURE.md). Moved unchanged; it reads the send through `sent`.
    """
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
        # #454: ONLY A COMPLETED TRANSFER COUNTS. sent.transfer_completed was
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
        if not sent.transfer_completed:
            raise _ShortSend()
        if sent.already_held:
            raise _AlreadyHeld()
        stats = db.update_stats_on_complete(sent.file_size - sent.resume_offset)
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
        _key, _shown, _kind = download_count_identity(sent.file_path, sent.file_name)
        db.record_download(_key, _shown, _kind)

        # The same send, for the record of what went to whom and how
        # fast (#1068). transfer_log catches its own write errors.
        import stats_mgr
        _queued_at = sent.next_file.get("queued_at") if isinstance(sent.next_file, dict) else None
        _wire_bytes = max(0, sent.bytes_sent - sent.resume_offset)
        _seconds = sent.transfer_finished_at - sent.start_time
        if _seconds <= 0:
            _seconds = 0.1
        # Claimed first (#1203): a stop that cut this send off has
        # written it as cancelled already.
        if _claim_transfer_outcome(sent.row):
            transfer_log.record_sent(
                _kind, _key, _shown, sent.file_size, _wire_bytes, _seconds,
                _wire_bytes / _seconds if stats_mgr.speed_is_measurable(_seconds, sent.file_size) else None,
                (sent.offered_at - _queued_at) if isinstance(_queued_at, (int, float)) else None,
                nick=sent.user)
    except _ShortSend:
        print(f"[DB COUNTER] Not counted: {sent.file_name} for {sent.user} ended "
              f"short at {sent.bytes_sent} of {sent.file_size} bytes. A partial send "
              f"is not a completed transfer, so it is left out of the "
              f"totals, the download counter and the speed record.")
    except _AlreadyHeld:
        print(f"[DB COUNTER] Not counted: {sent.user} already held all "
              f"{sent.file_size} bytes of {sent.file_name} and resumed at the end, "
              f"so nothing was sent.")
    except Exception as db_err:
        print(f"[DB ERROR] Could not increment the sharing statistics through the db module: {db_err}")


def finish_send(sent):
    """Everything a send does on its way out, whatever ended it, from a
    SentFile: drop the offer, speed and record the send, free the slot,
    announce a completed send, close the sockets, settle the queue row,
    remove a spent archive, release the packer and the nick, and wake the
    queue.

    start_dcc_send()'s `finally` block until #1336, step 2 of splitting it
    (docs/FUTURE.md). Moved unchanged; it reads the send through `sent`, and
    the three values the block read through `locals()` - unset on the early
    exits - arrive with the fallbacks it used.
    """
    import time
    import os
    import sys
    import threading

    # Never leave an offer standing. The success path already dropped it
    # the moment the receiver connected; this covers every other exit -
    # the port-refusal return above, a listen() that raised, an accept()
    # that timed out because nobody ever came. A stale entry is not
    # harmless: the ports it is keyed by are reused, so the next offer on
    # the same port to the same nick could read an offset agreed for a
    # different file and send it from the middle.
    try:
        clear_send_offer(sent.user, sent.assigned_port)
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
    _ended = (sent.transfer_finished_at if sent.transfer_finished_at is not None
              else time.time())
    acute_duration = _ended - sent.start_time
    if acute_duration <= 0:
        acute_duration = 0.1

    # Minus whatever the receiver already had. Counting a resumed send's
    # skipped bytes as though this transfer moved them would post a speed
    # record that never happened - and that number feeds the channel
    # advert, which is the same mistake the timing comment above describes.
    _skipped = sent.resume_offset
    acute_bytes = max(0, sent.bytes_sent - _skipped)
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
                      if stats_mgr_speed.speed_is_measurable(acute_duration, sent.file_size)
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
    if sent.transfer_completed:
        # Not a sample at all when nothing moved (a resume at the end).
        if not sent.already_held:
            try:
                import stats_mgr as stats_mgr_mod
                stats_mgr_mod.update_speed_record(final_calc_speed, acute_duration)
            except Exception as record_err:
                print(f"[STATS ERROR] Could not update the speed record: {record_err}")
    else:
        # How it ended, in the record (#1203), while the row is still in
        # active_transfers: a stop cutting it off now finds it claimed.
        # What reached the receiver is what it acknowledged, less what it
        # already held before a resume.
        _acked = sent.acked
        _record_send_outcome(sent.row, sent.outcome_status, sent.outcome_identity, sent.file_size,
                             max(0, _acked - _skipped), sent.user)

    # 1. Clean up the transfer and the slot immediately
    try:
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if not _is_this_send(tx, sent.row, sent.user)]
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
        # A private request is not announced (#1242): the operator's
        # decision, though the channel it belongs to is known by now.
        # Nor is a resume at the end: nothing was sent.
        if sent.transfer_completed and not sent.already_held:
            announce_mod.send_transfer_complete(sent.channel, sent.user, sent.file_name, sent.file_size, sent.start_time, reported_speed,
                                                duration=acute_duration,
                                                private=is_private_row(sent.next_file))
    except Exception as ann_chan_err:
        print(f"[ANNOUNCE CHANNEL ERROR] Could not send the channel notice: {ann_chan_err}")

    # 3. Close the network socket safely
    try: sent.conn.close()
    except: pass
    try: sent.dcc_sock.close()
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
        if sent.transfer_completed:
            settled_reason = "transfer complete"
        elif sent.never_connected:
            settled_reason = never_connected_advice()
        elif sent.too_slow_bps is not None:
            settled_reason = too_slow_reason(sent.too_slow_bps, to_the_user=True)
            # Marked before the settle saves the queue, so the mark is in
            # dcc_queue.txt with the row: waiting with it, this nick cannot
            # cost another crawling send its slot (see STOPPED_FOR_SPEED).
            if isinstance(sent.next_file, dict):
                with queue_lock:
                    sent.next_file[STOPPED_FOR_SPEED] = True
        else:
            settled_reason = "transfer did not complete"
        row_retained = release_queue_entry(
            sent.user, sent.next_file, delivered=sent.transfer_completed,
            reason=settled_reason)
    except Exception as pop_err:
        print("[DCC CLEANUP ERROR] Could not settle the queue row: " + str(pop_err))
    # A row dropped above has already told them why; one kept is told here,
    # or a send cut off for its speed would read as the bot failing (#1388).
    if sent.too_slow_bps is not None and row_retained:
        tell_them_it_was_too_slow(sent.user, sent.file_name, sent.too_slow_bps)

    # 4b. A send the receiver failed counts toward the nick's daily limit
    #     (#1389) - after the row is settled, so a pause removes what is left
    #     of the queue, a row kept for retry included. Under the nick the
    #     send is filed under now: the queue moved with a /nick.
    #     A never-connected one only while the bot's offers are not all
    #     going unanswered (never_connected_counts()): else it is our address.
    if sent.receiver_failed and daily_send_fails_limit():
        try:
            nick = (sent.row or {}).get('user') or sent.user
            if not sent.never_connected or never_connected_counts(nick):
                note_failed_send(nick)
        except Exception as count_err:
            print("[DCC PAUSE ERROR] Could not count the failed send: " + str(count_err))

    # 5. The RAR cache and the send lock
    try:
        # A row kept for retry needs its archive (#657).
        file_still_needed = bool(row_retained)
        safe_path = str(sent.file_path)

        if _is_temp_zip_cache_file(safe_path):
            # Still queued for anybody, being sent in another slot, or
            # being packed: asked BY PATH (#1268). The offered name is
            # the leaf alone, and another nick's different album with the
            # same leaf name kept this one on disk for good. Removed under
            # the lock that answered, so a pack of the same folder cannot
            # start writing it in between.
            with queue_lock:
                if not file_still_needed and not temp_archive_in_use(sent.file_path):
                    if os.path.exists(platform_compat.long_path(sent.file_path)):
                        os.remove(sent.file_path)
                        print(f"[DCC CLEANUP] Safely deleted the temporary archive from disk: {sent.file_name}")
    except Exception as file_rm_err:
        print(f"[DCC CLEANUP ERROR] Could not run the disk cleanup: {file_rm_err}")

    # 6. Release the memory lock and rule out duplicate threads.
    # #162 finding #6: a plain audio file never owned rar_inprogress - this ran
    # unconditionally on EVERY transfer's completion, so bob's ordinary MP3
    # finishing could clear the flag while alice's pack (queued as a separate
    # slot, holding the interlock for the whole duration of its own pack) was
    # still running, losing packer serialisation. Same guard as the critical-
    # abort and port-exhaustion branches above.
    if sent.owns_packer:
        config.rar_inprogress = False
        # #215: this release is the only moment another user's held pack can
        # start. Nothing else revisits them - every check_queue_and_send()
        # caller passes the user who just finished, never the one turned
        # away at [RAR-HOLD].
        try:
            redispatch_waiting_pack(sent.irc_sock, just_finished=sent.user)
        except Exception as wake_err:
            # Guarded like every other step of this finally (#606): an
            # exception here skipped the lock release and the fallback
            # trigger below, and the finishing user stayed "already
            # claimed elsewhere" until a rehash.
            print("[DCC CLEANUP ERROR] Could not wake a waiting pack: " + str(wake_err))
    # A list was not the nick's turn (#1205): files it was already waiting
    # for keep their place in the line, here and at every exit above.
    if hasattr(config, 'user_processing_lock'):
        config.user_processing_lock.discard(_send_nick(sent.row, sent.user))
        go_to_the_back(_send_nick(sent.row, sent.user), keep_place=is_a_list_row(sent.next_file))

    # 7. Wake the queue automatically after three seconds, thread-safely
    # A retained row means the attempt FAILED and will be retried. Reusing the flat
    # 3-second completion timer as the retry timer would hammer a broken entry three
    # times in nine seconds, each pass emitting a NOTICE and a VIP notice while the VIP
    # queue drains slower than that. Back off instead.
    #
    # Not after a send stopped for its speed (#1388): it was stopped so that
    # somebody waiting gets the slot, and its nick has just gone to the back,
    # so the sweep hands the slot to the next in line rather than retrying it.
    retry_delay = 3
    if row_retained and isinstance(sent.next_file, dict) and sent.too_slow_bps is None:
        retry_delay = 15 * max(1, int(sent.next_file.get('send_fails', 1)))

    def delayed_queue_trigger_fallback():
        time.sleep(retry_delay)
        check_queue_and_send(sent.irc_sock, sent.user)
    threading.Thread(target=delayed_queue_trigger_fallback, daemon=True).start()


def refuse_an_unsendable_file(irc_sock, user, row, next_file, file_path, file_size, ip_long, owns_packer,
                              outcome_identity):
    """A send that cannot be offered: no public address to offer from, a bot
    not offerable to strangers, or an empty or vanished file. Tells the user,
    and releases the packer, the nick and the transfer row. Without an address
    the queue is left untouched, since nothing can be retried until one is
    set; a bad file is charged to its queue row, recorded, and the queue is
    woken three seconds later.

    A refusal in start_dcc_send() until #1340, step 3 of splitting it
    (docs/FUTURE.md). Moved unchanged; `row` is the transfer row the send
    found, and the guard returns once this has.
    """
    import time
    # Two very different failures used to share one message. "File access
    # issue or empty payload. Please try again." is actively misleading for
    # the address case: there is nothing wrong with the file, retrying
    # cannot help, and only the operator can fix it.
    if ip_long == 0 or not is_offerable_to_strangers():
        reason = ("this bot has no usable public address configured, so it "
                  "cannot offer a transfer")
        print(f"[DCC CRITICAL ABORT] No usable public address "
              f"({dcc_address()!r}); refused "
              f"the send for {user} rather than offering one nobody can dial. "
              f"Set MY_IP_OR_DOCK in admin_config.py. Not settings.conf "
              f"(#465): this address is detected when the bot connects rather than "
              f"read from a file, so it is not a setting that file carries.")
    else:
        reason = "file access issue or empty payload. Please try again"
        print(f"[DCC CRITICAL ABORT] Aborted the send for {user}. "
              f"Path: {file_path} (Size: {file_size})")
    try: 
        msg = f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} {reason}.\r\n"
        irc_sock.sendall(msg.encode('utf-8', errors='ignore'))
        # No lane, never sent again; recorded only so a refusal of it is
        # not taken for an older line to this user (#1386).
        runtime.note_paced_line(msg, None)
    except:
        pass

    # Clear the locks and wait three seconds, to stay well clear of Excess Flood.
    # Only clear rar_inprogress if THIS send owns it - a plain audio file never
    # did, and clearing a flag another user's pack is holding is how the
    # interlock leaks (see the identical guard a few lines down, at the port-
    # exhaustion branch, which already got this right).
    if owns_packer:
        config.rar_inprogress = False
        # #215: this release is the only moment another user's held pack can
        # start. Nothing else revisits them - every check_queue_and_send()
        # caller passes the user who just finished, never the one turned
        # away at [RAR-HOLD].
        redispatch_waiting_pack(irc_sock, just_finished=user)
    if hasattr(config, 'user_processing_lock'):
        config.user_processing_lock.discard(_send_nick(row, user))
        go_to_the_back(_send_nick(row, user), keep_place=is_a_list_row(next_file))

    with queue_lock:
        config.active_transfers[:] = [tx for tx in config.active_transfers if not _is_this_send(tx, row, user)]

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
            config.user_processing_lock.discard(_send_nick(row, user))
            go_to_the_back(_send_nick(row, user), keep_place=is_a_list_row(next_file))
        return

    release_queue_entry(user, next_file, delivered=False,
                        reason="file missing or empty")
    # A list that is gone was replaced by a rebuild while it waited
    # (#1203): the bot's doing, so cancelled. A file that is gone failed.
    _record_send_outcome(
        row, transfer_log.STATUS_CANCELLED if outcome_identity[2] == transfer_log.KIND_LIST
        else transfer_log.STATUS_FAILED, outcome_identity, file_size, 0, user)

    time.sleep(3.0)
    check_queue_and_send(irc_sock, user)
    return


def refuse_without_a_port(irc_sock, user, row, next_file, dcc_sock, listener_error, owns_packer):
    """No DCC port or listener could be had: says so, releases what the send held
    without charging the row, schedules one delayed retry, and closes the
    listener.

    A refusal in start_dcc_send() until #1340, step 3 of splitting it
    (docs/FUTURE.md). Moved unchanged; `row` is the transfer row the send
    found, and the guard returns once this has.
    """
    import time
    import threading
    if listener_error is not None:
        print(f"[DCC PORTS] Could not open a listener for {user}: {listener_error}")
        refusal = "Could not open a DCC port right now. Your file stays queued."
    else:
        refusal = "No available DCC ports."
    refusal_line = f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} {refusal}\r\n"
    try:
        irc_sock.sendall(refusal_line.encode("utf-8", errors="ignore"))
        # No lane, never sent again; recorded only so a refusal of it is
        # not taken for an older line to this user (#1386).
        runtime.note_paced_line(refusal_line, None)
    except: pass
    with queue_lock:
        config.active_transfers[:] = [tx for tx in config.active_transfers if not _is_this_send(tx, row, user)]

    # Port exhaustion is TRANSIENT and is not this entry's fault, so it is deliberately
    # NOT charged to the retry budget - a busy spell must not discard good queued files.
    # The interlocks are released so the user is not left wedged, and the row stays
    # queued for the next completion trigger. Re-triggering here would spin: the ports
    # are still full one instruction later.
    # Only clear rar_inprogress if THIS send owns it - a plain audio file never did, and
    # clearing a flag another user's pack is holding is how the interlock leaks.
    if owns_packer:
        config.rar_inprogress = False
        # #215: this release is the only moment another user's held pack can
        # start. Nothing else revisits them - every check_queue_and_send()
        # caller passes the user who just finished, never the one turned
        # away at [RAR-HOLD].
        redispatch_waiting_pack(irc_sock, just_finished=user)
    if hasattr(config, 'user_processing_lock'):
        config.user_processing_lock.discard(_send_nick(row, user))
        go_to_the_back(_send_nick(row, user), keep_place=is_a_list_row(next_file))
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
    if dcc_sock is not None:
        try:
            dcc_sock.close()
        except Exception:
            pass
    return


def stream_the_file(conn, file_path, resume_offset, bytes_sent, acks, row, user):
    """Send the file down a connected DCC socket, from resume_offset to its
    end, reading the receiver's acknowledgements as it goes; return the
    bytes sent, counted the way the receiver holds them (the skipped part
    of a resume included).

    Raises _ReceiverGone or _ReceiverStalled when the receiver closes or
    stops acknowledging with bytes outstanding, and lets a socket error
    through. acks.sent equals the bytes sent after every chunk, so whoever
    catches it still knows how far the send got. Each chunk also moves the
    transfer row's bytes_sent and the bot's total.

    Raises _TooSlowWhileOthersWait when it crawls while every slot is taken
    and others wait (#1388); its _SlowSendWatch publishes the send's window
    rate on `row` for the other sends' watches to compare against.
    Raises _SourceUnreadable when the FILE fails - open, seek or read
    (#1389): a timeout or a reset from a network share is an OSError of the
    same type as the receiver's, and must not be taken for one.

    start_dcc_send()'s send loop until #1350, step 4 of splitting it
    (docs/FUTURE.md).
    """
    try:
        f = open(platform_compat.long_path(file_path), 'rb')
    except OSError as err:
        raise _SourceUnreadable(err) from err
    with f:
        if resume_offset:
            try:
                f.seek(resume_offset)
            except OSError as err:
                raise _SourceUnreadable(err) from err
        # mIRC's "packet size", and the reason raising it there is
        # noticeable: mIRC defaults to 4 KB and this has always been 64.
        block = dcc_block_size()
        # The timeout covers one whole sendall() of `block` bytes, so it
        # scales with the block; see SEND_TIMEOUT_SECONDS.
        conn.settimeout(_send_timeout(block))
        # From here the send is connected: the slow-send grace runs from now.
        slow = _SlowSendWatch(acks.acked, row=row)
        while True:
            # The bot's upload cap (#1352), read per chunk so a change
            # reaches a send already running: under a cap the chunk is a
            # tenth of a second of line, and it waits its turn behind
            # every other send's.
            try:
                chunk = f.read(speed_cap.chunk_size(speed_cap.UPLOAD, block))
            except OSError as err:
                raise _SourceUnreadable(err) from err
            if not chunk: break
            speed_cap.take(speed_cap.UPLOAD, len(chunk))
            acks.sending()
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
            # Crawling while every slot is taken and others wait (#1388).
            too_slow = slow.check(acks.acked)
            if too_slow is not None:
                raise _TooSlowWhileOthersWait(acks.acked, too_slow)
            for tx in config.active_transfers:
                if _is_this_send(tx, row, user):
                    tx['bytes_sent'] += len(chunk)
            oserve = sys.modules.get('oserve')
            if oserve: oserve.total_sent_bytes += len(chunk)
            # The block for the next pass, read again every pass rather
            # than once per transfer (#1461): DCC_BLOCK_SIZE changes on a
            # rehash, and under the upload cap the chunk is bounded by it.
            # A send that kept the block it started with took bigger (or
            # smaller) chunks from the shared clock than the sends started
            # after the change, so the newer ones moved less than an even
            # split although only the cap held them - and
            # upload_cap_holds_this_send() let the slow-send watch stop one
            # as a crawler. One getattr a pass is nothing beside the read
            # and the sendall(); the timeout is set again only when the
            # block did change, before the chunk it covers goes out. In a
            # !rehash's few milliseconds of shipped defaults
            # (runtime.config_reload_lock) one pass may move the default
            # 64 KB with its 60 s, and the next goes back.
            now_block = dcc_block_size()
            if now_block != block:
                block = now_block
                conn.settimeout(_send_timeout(block))
    return bytes_sent


def start_dcc_send(irc_sock, user, file_path, file_name, channel, next_file, owns_packer=False):
    """Handle the network ports and the CTCP, and stream the bytes with accurate timing.

    owns_packer is True only for the send the packer hands its fresh archive to
    (#1081): that send holds config.rar_inprogress and releases it on every exit.
    Any other send - including an archive row that waited in the queue and was
    dispatched as a plain row - never took the lock and must not clear it.
    """
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

    # Module functions since #1336, which finish_send() uses too.
    def _mine(tx):
        return _is_this_send(tx, _row, user)

    def _my_nick():
        return _send_nick(_row, user)

    # What this send is, for the row its ending writes (#1203). Worked out
    # once, here, and kept on the transfer's row too, so a stop that cuts
    # the send off (record_transfers_cut_off()) can say what it was.
    outcome_identity = _transfer_outcome_identity(file_path, file_name)
    for tx in config.active_transfers:
        if _mine(tx):
            tx['size'] = file_size
            tx['transfer_outcome_identity'] = outcome_identity
    ip_long = get_public_ip_long()
    start_time = time.time()
    offered_at = start_time   # start_time is reset once the receiver connects; the queue wait ends here
    bytes_sent = 0
    # Only a send loop that ran to completion counts as delivered. A socket timeout, a
    # refused connection or a half-finished stream must not consume the queue row.
    transfer_completed = False
    # A completed send that moved nothing: see where it is set below.
    already_held = False
    # The rate a send was stopped at for crawling while others waited (#1388).
    too_slow_bps = None

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
        if owns_packer:
            config.rar_inprogress = False
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(_my_nick())
            go_to_the_back(_my_nick(), keep_place=is_a_list_row(next_file))
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers
                                          if not _mine(tx)]
        return

    irc_sock = live_sock

    # is_offerable_to_strangers() is the address half; ip_long == 0 only catches
    # a blank or malformed value, and a loopback address passes it (127.0.0.1 is
    # 2130706433). See that function for what went wrong without it.
    if ip_long == 0 or file_size == 0 or not is_offerable_to_strangers():
        # refuse_an_unsendable_file() since #1340.
        refuse_an_unsendable_file(irc_sock, user, _row, next_file, file_path, file_size, ip_long, owns_packer,
                                  outcome_identity)
        return


    # CREATING THE LISTENER CAN FAIL TOO (#1268). listen() moved inside the
    # try below for EMFILE, but socket.socket() is the first call to fail when
    # the process is out of descriptors, and it sat out here: the OSError
    # killed this thread with the slot in active_transfers and the nick in
    # user_processing_lock - one slot gone and that nick never served again,
    # and a pack handoff's rar_inprogress latched for everyone. A listener
    # that cannot be made is treated like no free port: same release, same
    # delayed retry, not charged to the row.
    dcc_sock = None
    listener_error = None
    assigned_port = None
    try:
        dcc_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR means the OPPOSITE thing on Windows - it lets another process
        # bind this same port and take the incoming connection, which on a DCC
        # listener is a hijack. platform_compat picks the right option per platform.
        platform_compat.prepare_listener(dcc_sock)

        for port in range(config.DCC_PORT_START, config.DCC_PORT_END + 1):
            try:
                dcc_sock.bind(('0.0.0.0', port))
                assigned_port = port
                break
            except socket.error:
                continue
    except Exception as sock_err:
        listener_error = sock_err

    if assigned_port is None:
        # refuse_without_a_port() since #1340.
        refuse_without_a_port(irc_sock, user, _row, next_file, dcc_sock, listener_error, owns_packer)
        return

    conn = None
    # Set by the accept branch below and read by the finally, so the notice
    # the user gets can say what happened rather than "did not complete".
    never_connected = False
    # The receiver failed the send (#1389): counted toward the nick's
    # DAILY_SEND_FAILS_LIMIT by finish_send(). Set only in the handlers below
    # for a receiver that never connected, stalled, let a write time out or
    # closed the connection before it had the whole file - never for a
    # failure on our side or a stop the bot makes (see the #1389 block above
    # _send_fails_today()).
    receiver_failed = False
    # How the send ended when it did not complete (#1203); set where that is
    # known, and FAILED wherever nothing more is.
    outcome_status = transfer_log.STATUS_FAILED
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
            # Recorded with no lane (#1386): unpaced and never sent again,
            # recorded only so that the server refusing it is not taken for
            # an older line to this user, which would then go out twice.
            runtime.note_paced_line(ctcp_handshake, None)
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
            receiver_failed = True
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
        # The bot's offers reach people: never-connected sends count (#1389).
        note_send_connected()

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

        # stream_the_file() since #1350; acks.sent follows it when it raises.
        bytes_sent = stream_the_file(conn, file_path, resume_offset, bytes_sent, acks, _row, user)
                
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
            # The file got shorter under the send. For a list that is a
            # rebuild replacing it (#1203) - the bot's doing, so cancelled.
            if outcome_identity[2] == transfer_log.KIND_LIST:
                outcome_status = transfer_log.STATUS_CANCELLED
            report_failure(user, file_name,
                f"sent {bytes_sent:,} of {file_size:,} bytes before the file ended. "
                f"Recorded as a failure rather than a completed transfer.",
                acked=acks.acked, total=file_size)
            final_ack_at = None
        else:
            final_ack_at = _wait_for_final_ack(conn, acks, file_size)
        transfer_completed = final_ack_at is not None
        # A RESUME AT THE END OF THE FILE COMPLETES WITH NOTHING SENT.
        # handle_resume_request() clamps a position past the end to the size,
        # on purpose: the receiver already holds all of it, and the ACCEPT
        # lets its transfer finish instead of hanging. That is still the
        # outcome here - the queue row is settled as delivered - but nothing
        # moved, so nothing is credited: not the files-sent total, not the
        # download counter, not the transfer record, not the speed record,
        # and no "Sent:" line in the channel. Otherwise a peer could raise
        # any file's count, and the bot's totals, without a byte of traffic -
        # what #656 closed for a forged final acknowledgement.
        already_held = bool(transfer_completed and resume_offset
                            and resume_offset >= file_size)
        if not transfer_completed and not short_read:
            # Every byte went out and the receiver did not take them all:
            # it closed, or stopped acknowledging (#1389).
            receiver_failed = True
            if not acks.received_any:
                report_failure(user, file_name,
                    "the receiver never acknowledged a single byte, so there "
                    "is no evidence any of it arrived. Not counted. (A DCC "
                    "receiver acknowledges every packet; one that sends none "
                    "cannot be told apart from one that got nothing.)",
                    acked=0, total=file_size)
            elif acks.eof:
                # The receiver closed it: in DCC that is how a user stops a
                # transfer, so cancelled (#1203).
                outcome_status = transfer_log.STATUS_CANCELLED
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
        if already_held:
            print(f"[DCC-RESUME] {user} resumed {file_name} at its end and "
                  f"already held all {file_size} bytes; nothing was sent.")
        elif transfer_completed:
            print(f"[DCC-SUCCESS] Sent the whole file to {user}; the receiver "
                  f"acknowledged all {file_size} bytes.")
        # The 1.5-second "let mIRC close its file calmly" pause is gone: the
        # receiver's final ack IS it telling us it has everything, and the
        # wait above already returned on that. Sleeping after it only held a
        # DCC slot for nothing.
 
        # Counting it is record_completed_send() (#1328).
        record_completed_send(SentFile(
            _row, user, next_file, file_path, file_name, file_size, resume_offset, bytes_sent,
            transfer_completed, already_held, offered_at, start_time, transfer_finished_at))

        try: conn.close()
        except: pass

    except _ReceiverGone as gone:
        # Closed mid-transfer by the receiver: a user stopping it (#1203).
        outcome_status = transfer_log.STATUS_CANCELLED
        report_failure(user, file_name,
            f"closed the connection mid-transfer, having acknowledged "
            f"{gone.acked:,} of {file_size:,} bytes.",
            acked=gone.acked, total=file_size)
        # Counted toward the daily limit all the same (#1389): a user's Close
        # and a client that drops every send halfway cannot be told apart on
        # the wire - either closes with a FIN, or with a reset while data is
        # still unread - and both hold a slot for a file that does not arrive.
        receiver_failed = True
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except _SourceUnreadable as unreadable:
        # Our side (#1389): the file, not the receiver. Not counted against
        # the nick, whatever type of OSError the share raised.
        report_failure(user, file_name,
            f"could not read the file being sent: {type(unreadable.err).__name__}: "
            f"{unreadable.err}. The bot's side, not the receiver's.",
            acked=acks.acked, total=file_size)
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except _ReceiverStalled as stalled:
        report_failure(user, file_name,
            f"stopped acknowledging at {stalled.acked:,} of {file_size:,} bytes "
            f"and made no progress for {int(ACK_STALL_SECONDS)}s; giving up on "
            f"a dead link.",
            acked=stalled.acked, total=file_size)
        receiver_failed = True
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except _TooSlowWhileOthersWait as slow:
        # A failed attempt like a stall (#1388): finish_send() keeps the row
        # against MAX_SEND_FAILS, sends the nick to the back and hands the
        # slot to the next in line.
        too_slow_bps = slow.speed
        report_failure(user, file_name,
            f"{too_slow_reason(slow.speed)}; stopped at {slow.acked:,} of "
            f"{file_size:,} bytes so the next in line gets the slot.",
            acked=slow.acked, total=file_size)
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
        receiver_failed = True
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except Exception as e:
        # FIXED (issue #30): `e` was captured and never used. A connection reset, a broken
        # pipe, or any other mid-transfer failure produced the exact same silence - no way
        # to tell which one happened after the fact, especially once the temp archive is
        # already deleted and the queue row already gone.
        report_failure(user, file_name, f"{type(e).__name__}: {e}")
        # A reset or a broken pipe is the receiver closing on us (#1389). Not
        # anything else that lands here: a file that vanished or could not
        # be read is our side's failure, not theirs.
        if isinstance(e, ConnectionError):
            receiver_failed = True
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    finally:
        # finish_send() since #1336. resume_offset, acks and
        # transfer_finished_at are unset on the exits before the transfer.
        # Once acks is set, acks.sent is the bytes sent: a send that raised
        # out of stream_the_file() never returned its count (#1350).
        _here = locals()
        finish_send(SentFile(
            _row, user, next_file, file_path, file_name, file_size,
            _here.get('resume_offset', 0), _here['acks'].sent if 'acks' in _here else bytes_sent,
            transfer_completed, already_held,
            offered_at, start_time, _here.get('transfer_finished_at'),
            channel=channel, irc_sock=irc_sock, owns_packer=owns_packer,
            outcome_identity=outcome_identity, outcome_status=outcome_status,
            acked=_here['acks'].acked if 'acks' in _here else 0,
            never_connected=never_connected, assigned_port=assigned_port,
            conn=conn, dcc_sock=dcc_sock, too_slow_bps=too_slow_bps,
            receiver_failed=receiver_failed))
