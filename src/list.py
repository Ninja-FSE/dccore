# list.py - Slimmed down; scanning lives in update_list.py
import array
import bisect
import os
import zlib
import time
import datetime
import defaults as config
import library
import runtime
import oserve
import dcc
import announce
import hashlib
import platform_compat
import theme



def list_slug(name):
    """A directory-safe form of a list name, unique to that exact name.

    List names are operator-facing and never typed in a channel (#26), so they
    can hold anything an operator likes - including the characters Windows
    refuses in a path and the separators that would turn one list into a
    nested tree. Everything outside a small safe set becomes "_", and no "."
    survives as itself, so no name can produce a traversal.

    THE SUFFIX IS THE POINT. Replacing characters is not injective: "A/B" and
    "A B" both flatten to "A_B", and two lists landing in one directory would
    have them overwriting each other's index, archive and side files with no
    error anywhere. So a name that had to be changed carries a short digest of
    the ORIGINAL, which makes the mapping one-to-one again.

    A name that needed no changing keeps exactly itself, which is the common
    case and the readable one: "Films" is the "Films" directory. Two such
    names cannot collide, because they are equal.

    A name Windows will not create ("NUL", "COM1", #1208) counts as one that
    needed changing: it takes the digest too, so it is not made safe by a
    suffix that another list's own name could already be using.

    Stable across restarts and across reordering the lists: it depends on that
    one name and nothing else. hashlib rather than hash(), which is randomised
    per process and would rename every directory on each start.
    """
    raw = str(name or "").strip()
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_ ") else "_" for ch in raw)
    cleaned = cleaned.strip(" ")
    if cleaned == raw and cleaned and not platform_compat.is_windows_reserved(cleaned):
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:6]
    return f"{cleaned or 'list'}-{digest}"


def list_dir(name=None):
    """Where one list's files live.

    THE PRIMARY LIST USES LOCAL_LIST_DIR ITSELF, which is exactly where every
    install's files are today - so nothing moves, no upgrade migrates anything,
    and a single-list install cannot tell this function exists. Every other
    list gets a subdirectory named after it.

    A subdirectory rather than a longer filename, deliberately. The names a
    build writes already carry three markers - "-RAR-", "-VIDEO-", "-FULL-" -
    and the code that reads them has been wrong about a base name containing
    one of those before. Putting the list in the PATH instead of the NAME
    means every one of those names stays exactly as it is, and none of that
    parsing grows a dimension.
    """
    import library

    root = config.LOCAL_LIST_DIR
    # No separate branch for `name is None`: list_by_name(None) is None, and
    # the test below already answers `root` for that. A mutation run showed
    # the early return changed nothing, so it went rather than being propped
    # up with a test that could only ever pass.
    chosen = library.list_by_name(name)
    if chosen is None or chosen.primary:
        # An unknown name resolves to the root rather than inventing a
        # directory for a list nobody configured: callers that look there find
        # nothing, which is the truthful outcome, and a build never writes to
        # a name it did not get from lists() in the first place.
        return root
    return os.path.join(root, list_slug(chosen.name))


# Resolved per call rather than once at import. LOCAL_LIST_DIR is a config value,
# and !rehash reloads config - a path baked in at import time would keep pointing
# at the old directory for the life of the process after the operator moved it.
def size_file_path(name=None):
    return os.path.join(list_dir(name), config.LIST_SIZE_FILE)


def rawbytes_file_path(name=None):
    return os.path.join(list_dir(name), config.LIST_RAWBYTES_FILE)

# NOTE: a second, shadowing definition of find_latest_list() used to sit here. Python keeps
# the LAST definition, so this one never ran - and the two had drifted: this one did not
# exclude the "-RAR-" album list, so had it ever become the live one the bot would have
# served the album list as its master list. Removed so they cannot diverge again.

# The three ways the master list can be handed over, in the order they are
# tried when the configured one has not been built yet.
LIST_FORMATS = ("zip", "rar", "txt")

# The delivered text list is a file of its own rather than the master index,
# and this marks it. The index is what @find and the file count read, and the
# album rows would be matched by both if the two were ever the same file: a
# search for "dolch" would offer an album row as though it were a track, and
# the advert would count the albums as files.
FULL_LIST_MARKER = "-FULL-"


def list_format():
    """The configured delivery format, normalised.

    An unrecognised value serves .zip rather than nothing. settings_file
    refuses one at the point of saving, but admin_config.py assigns straight
    onto config and answers to nobody, so this is the last place a typo can be
    caught before it costs the bot its list.
    """
    raw = getattr(config, "LIST_FORMAT", "zip")
    chosen = str(raw or "").strip().lower()
    if chosen in LIST_FORMATS:
        return chosen
    print(f"[LIST] LIST_FORMAT={raw!r} is not one of {sorted(LIST_FORMATS)} "
          f"- handing out the .zip instead.")
    return "zip"


def list_artifact_name(fmt, date_str):
    """What the artifact for `fmt` is called on the date given."""
    if fmt == "txt":
        return f"{config.LIST_BASE_NAME}{FULL_LIST_MARKER}{date_str}.txt"
    return f"{config.LIST_BASE_NAME}-{date_str}.{fmt}"


def _is_dated(name, prefix, suffix):
    """True if `name` is exactly "<prefix><a date><suffix>".

    THE PREFIX ALONE WAS NOT ENOUGH, and the docstring below already said why
    it needed to be: a library file is only kept out of the lists directory if
    the test can tell one apart from a real artifact. With LIST_BASE_NAME
    derived from a nickname - "Muzik", say - a shared file called
    "Muzik-Collection.rar" starts with that prefix and ends in that
    extension, so a request for it was looked for among the lists, found
    missing, and refused for ever, while the file sat in the library being
    advertised. The extension case was guarded and the prefix case, which is
    the likelier of the two, was not.
    """
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return False
    middle = name[len(prefix):len(name) - len(suffix)]
    # Parsed with the FORMAT THE BUILDER WRITES, rather than a pattern that
    # resembles it: update_list.py names every list
    # `datetime.now().strftime("%Y-%m-%d")`, so if that ever changes this
    # stops matching loudly instead of drifting apart quietly. (`re` is not
    # importable this far up the file - see the second import block below.)
    try:
        datetime.datetime.strptime(middle, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def is_list_artifact(filename, fmt):
    """True if `filename` is a delivered master list in `fmt`.

    Matched on the name the builder actually writes, not on the extension
    alone: people share .zip and .rar files out of their library too, and a
    file called "Someone - DCCore Sessions.rar" must not be looked for in the
    lists directory instead of the music directory.
    """
    name = os.path.basename(str(filename))
    if fmt == "txt":
        return _is_dated(name, config.LIST_BASE_NAME + FULL_LIST_MARKER, ".txt")
    return _is_dated(name, config.LIST_BASE_NAME + "-", "." + fmt)


def is_list_artifact_name(filename):
    """True if this names a delivered master list in any of the three formats."""
    return any(is_list_artifact(filename, fmt) for fmt in LIST_FORMATS)


def find_latest_list_file(name=None):
    """The artifact to send when somebody types the bot's nickname.

    The configured format first. If it has not been built yet - the operator
    changed LIST_FORMAT and the next rebuild has not run - this falls back to
    another format rather than answering "list missing". Handing somebody a
    .zip when the setting now says .rar is a far smaller thing than the bot
    having no list at all until the weekly update comes round, which is the
    failure the atomic-publish rewrite exists to prevent.
    """
    directory = list_dir(name)
    if not os.path.exists(directory):
        return None
    try:
        entries = os.listdir(directory)
    except OSError as err:
        print(f"[LIST ERROR] Could not read {directory}: {err}")
        return None

    wanted = list_format()
    order = (wanted,) + tuple(f for f in LIST_FORMATS if f != wanted)
    for fmt in order:
        files = [f for f in entries if is_list_artifact(f, fmt)]
        if not files:
            continue
        files.sort(reverse=True)
        if fmt != wanted:
            print(f"[LIST] No .{wanted} list has been built yet - sending {files[0]}. "
                  f"The next list update will build the .{wanted}.")
        return os.path.join(directory, files[0])
    return None

# The count is the answer to "how many files do I share", and it is asked
# constantly: every advert cycle, every Stats page load, every -que from
# somebody with nothing queued, the admin console's status. It is answered by
# reading every published list end to end and counting the lines that start
# with "!". On a library that is 5.4 million files - 460 MB of list - that is
# three seconds warm and considerably more cold, paid again by every caller,
# for a number that cannot change between one !update and the next.
#
# So it is computed once per build. The cache key is each list file's path,
# mtime and size; update_list.py publishes a new list with os.replace(), which
# gives it a new mtime and (almost always) a new size, so the first call after
# a rebuild misses, recounts once, and every call until the next rebuild is
# free. Nothing is invalidated by hand, because there is nothing to forget:
# the file on disk is the truth, and the key IS the file on disk.
# The lock is runtime.py's, not constructed here, for the reason dcc.queue_lock
# gives at length: this module is reloaded by !rehash, and a Lock() built here
# would be a new object after every reload while a caller still inside the
# count held the old one. The cache dict IS constructed here, deliberately -
# rebinding it on reload costs one recount, which is harmless.
_count_lock = runtime.list_count_lock
_count_cache = {}     # signature -> count


def _list_signature(paths):
    """What has to be unchanged for a cached count to still be right."""
    parts = []
    for path in paths:
        try:
            st = os.stat(path)
            parts.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((path, None, None))
    return tuple(parts)


def count_request_lines(paths):
    """The number of request lines across `paths`, cached per file state.

    Count only real request lines, skipping the "===" folder separators,
    blank lines and text headers. This matches on "!" alone rather than on
    f"!{config.NICKNAME} ": the list is written with whatever nick was
    current at generation time, so after a 433 fallback a nick-specific test
    matched nothing and the advert reported 0 files even though the list was
    fine. Every request line in the generated file starts with "!"
    (update_list.py) and no header or separator does - the same filter
    execute_search applies to the same file.

    Each path gets its OWN try (#433): one unreadable list - an AV scanner or
    backup agent holding the VIDEO list open with no sharing, on Windows; a
    permission change or a vanished path on either platform - must cost only
    that list's lines, not the count from the ones that read fine.
    """
    paths = list(paths)
    signature = _list_signature(paths)
    with _count_lock:
        cached = _count_cache.get(signature)
        if cached is not None:
            return cached

        count = 0
        complete = True
        for one_list in paths:
            try:
                with open(one_list, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("!"):
                            count += 1
            except OSError as list_err:
                print(f"[LIST] Could not read {one_list}: {list_err}")
                complete = False

        # Only a COMPLETE count is kept. The #433 case is a list that stat()s
        # fine but will not open - an AV scanner holding it - and that leaves
        # the signature unchanged when the scanner lets go. Caching the short
        # count under it would serve that number until the next !update; the
        # uncached code retried on the next call, and so does this.
        #
        # One entry. A stale signature is a list that no longer exists on
        # disk in that form, and keeping its count around would only serve a
        # later caller a number for a file nobody can read any more.
        if complete:
            _count_cache.clear()
            _count_cache[signature] = count
        return count


def get_file_count_date_size_and_raw_bytes(name=None):
    """The EXACT number of music files, counting only lines that start with the trigger.

    `name` picks a list; without one this is the primary, which on a
    single-list install is the only one there is.
    """
    latest_list = find_latest_list(name)
    if not latest_list or not os.path.exists(latest_list):
        return 0, "No List", "0B", 0
        
    try:
        # EVERY list, not just the master. Film and series moved into their
        # own file, and counting one of two would advertise a number smaller
        # than the library the bot actually serves - and smaller than what a
        # user sees when they open the archive. The date below still comes
        # from the master list, which is the one always present.
        count = count_request_lines(all_list_paths(name))
            
        mtime = os.path.getmtime(latest_list)
        dt = datetime.datetime.fromtimestamp(mtime)
        day = dt.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = dt.strftime(f"%b {day}{suffix}")
        
        # Each side file is read in its own try. They used to sit inside the outer
        # try below, so an unparseable rawbytes file - int("") on a truncated write
        # raises ValueError - collapsed the WHOLE tuple to (0, "Error", "0B", 0).
        # The count and the list date come from the master list and were perfectly
        # good; one clipped byte count must not throw them away and make the advert
        # announce an error.
        size_str = "0B"
        try:
            # size_file_path(NAME), not size_file_path(). This function is
            # already list-aware everywhere else - find_latest_list(name) and
            # all_list_paths(name) both take it, a few lines up - and these
            # two were the ones the multi-list work missed. Without the name
            # they resolve to the PRIMARY list's side files, so a channel
            # bound to a second list advertised its own file count and list
            # date beside the primary library's size and byte total.
            size_path = size_file_path(name)
            if os.path.exists(size_path):
                with open(size_path, "r", encoding="utf-8") as sf:
                    size_str = sf.read().strip() or "0B"
        except OSError as size_err:
            print(f"[LIST] Could not read {config.LIST_SIZE_FILE}: {size_err}")

        raw_bytes = 0
        try:
            raw_path = rawbytes_file_path(name)
            if os.path.exists(raw_path):
                with open(raw_path, "r", encoding="utf-8") as rbf:
                    raw_bytes = int(rbf.read().strip())
        except (OSError, ValueError) as raw_err:
            print(f"[LIST] Could not read {config.LIST_RAWBYTES_FILE}: {raw_err}")
                
        return count, date_str, size_str, raw_bytes
    except Exception as e:
        print(f"[ERROR] Could not read the exact file statistics: {e}")
        return 0, "Error", "0B", 0

# list.py - The search module (part 1 of 2)
import os
import glob
import re
import sys
import defaults as config
import announce

_CONTROL_CODE_RE = re.compile(r'\x03(?:\d{1,2}(?:,\d{1,2})?)?')


def strip_control_codes(text):
    """Strip mIRC colour codes and formatting control characters from `text`.

    Extracted out of execute_search()'s own inline version (byte-identical
    logic, just named) so other callers can reuse it instead of reinventing
    it: irc.py's cross-bot broadcast-search capture and dcc_fetch.py's
    inbound-offer filename cleaning both need the exact same treatment before
    they can safely look at or store what a foreign bot sent.
    """
    clean = str(text).replace('\x02', '').replace('\x1f', '').replace('\x0f', '')
    return _CONTROL_CODE_RE.sub('', clean)


# Every C0 control, DEL and the C1 range (#670, audit L6). What a user typed
# in a request or a search is printed to the operator's terminal, sent to
# the debug channel and rendered by the admin chat, and strip_control_codes()
# leaves reverse (\x16), italics (\x1d), a mid-line \x01, an ESC and a BEL in
# it: "!rar \x1b]0;pwned\x07\x034,4 SENT: admin.rar to victim" retitled a
# Windows Terminal window and drew a red block that read like a fake SENT
# line inside the PART line. CR and LF never get this far (the reader splits
# on them); nothing else below a space belongs in a filename or a search.
_UNPRINTABLE_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def printable_text(text):
    """strip_control_codes(), then every remaining control character."""
    return _UNPRINTABLE_RE.sub('', strip_control_codes(text))


def _has_marker(path, marker):
    """True if the builder's `marker` appears in the part of the name it owns.

    THE MARKERS ARE THE BUILDER'S, AND THEY SIT AFTER THE BASE NAME - so
    testing the whole path for them let the operator's own choices decide
    whether this bot has a list at all. A LIST_BASE_NAME containing "-VIDEO-"
    or "-RAR-" excluded the master list from its own search: @find answered
    "No MasterList found" and the advert published 0 files, permanently, with
    the file sitting right there. A LOCAL_LIST_DIR with "-FULL-" somewhere in
    its path did the same to every list under it.
    """
    name = os.path.basename(str(path))
    base = str(getattr(config, "LIST_BASE_NAME", "") or "")
    tail = name[len(base):] if base and name.startswith(base) else name
    return marker in tail


def find_latest_list(name=None):
    """Find the newest master text list in the lists directory.

    Globs on config.LIST_BASE_NAME, which is what update_list.py actually names the files
    with (update_list.py:38). It previously globbed on config.NICKNAME - a value irc.py
    REBINDS at runtime when the server returns 433 and the bot falls back to ALT_NICKNAME.
    From that moment the glob matched nothing: @find answered "No MasterList found" and the
    5-minute advert publicly announced "For My List Of: 0 Files" into all six channels.
    The two constants are equal in normal operation, so this changes nothing until a
    nick collision happens.
    """
    try:
        # glob.escape both halves: "[" and "]" are a character class to glob, and
        # both are ordinary in the two values interpolated here. Bot[GR] is a
        # standard IRC nick, and LIST_BASE_NAME follows NICKNAME by default; a
        # music share under D:\Lists[FLAC]\ is the same bug from the other side.
        # Unescaped, the pattern matched nothing and never errored: @find answered
        # "No MasterList found" and the advert published "0 Files" forever.
        pattern = os.path.join(glob.escape(list_dir(name)),
                               f"{glob.escape(config.LIST_BASE_NAME)}-*.txt")
        all_txt_files = sorted(glob.glob(pattern))
        # Keep the RAR list out of the search, so only the master list is scanned.
        # FULL_LIST_MARKER keeps the DELIVERED text list out too: that one is a
        # copy of this file with the album rows appended, and which of the two
        # sorts last is an accident of punctuation. If it ever won, @find would
        # offer album rows as though they were tracks and the advert would count
        # the albums as files.
        true_master_lists = [f for f in all_txt_files
                             if not _has_marker(f, "-RAR-")
                             and not _has_marker(f, f"-{VIDEO_LIST_MARKER}-")
                             and not _has_marker(f, FULL_LIST_MARKER)]
        if true_master_lists:
            return true_master_lists[-1]
    except Exception as e:
        print(f"[SEARCH ERROR] Could not find the latest list: {e}")
    return None

# The film-and-series list's name marker. update_list.py names that file
# "<base>-VIDEO-<date>.txt", which the glob in find_latest_list() matches and
# which sorts AFTER "<base>-<date>.txt" - so without a guard it would win the
# [-1] and become "the" master list that @find searches and the advert counts.
# Exactly the failure the comment there already records for "-RAR-".
VIDEO_LIST_MARKER = "VIDEO"


def find_latest_video_list(name=None):
    """The newest film-and-series list, or None if the library has no video.

    None is the ordinary case, not a fault: the file is only published when
    there is video to put in it, and SEPARATE_VIDEO_LIST can be off entirely.
    Every caller treats a missing one as "nothing extra to read".
    """
    try:
        pattern = os.path.join(
            glob.escape(list_dir(name)),
            f"{glob.escape(config.LIST_BASE_NAME)}-{VIDEO_LIST_MARKER}-*.txt")
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    except Exception as e:
        print(f"[SEARCH ERROR] Could not find the latest film list: {e}")
    return None


def all_list_paths(name=None):
    """Every list a REQUEST may be answered from, newest master first.

    This is the seam the split turns on. A file request and an @find both used
    to read find_latest_list() alone; once film and series moved to their own
    file, reading only that one would have left every video in the library
    listed, advertised, and impossible to actually get - the exact opposite of
    what publishing it is for.

    Returns one path when there is no film list, which is what a music-only
    library and a switched-off SEPARATE_VIDEO_LIST both look like.
    """
    paths = []
    for path in (find_latest_list(name), find_latest_video_list(name)):
        if path and os.path.exists(path):
            paths.append(path)
    return paths


# The marker alone, without the r'\s*' either side it used to carry (#1136).
# The leading one made re.split() retry the whitespace at every position of a
# row before giving up, on every row of every list; strip_info_suffix() strips
# both halves anyway, and str.strip() removes exactly the characters \s
# matches, so the split lands in the same place.
_INFO_MARKER_RE = re.compile('::INFO::', re.IGNORECASE)

# The other family of size suffix seen in production, from bots that do not
# use "::INFO::" at all: "SDFind v3.91 by SDSailor" writes
# "!SomeBot A101. Donna Summer - I Feel Love (Original 12'' Version).mp3
# ---- 18.8Mb" - two or more hyphens between spaces, then a bare size with no
# marker word at all.
#
# Anchored to the END of the string ($), and requires what follows the dashes
# to actually look like a size (digits, an optional decimal point, an
# optional K/M/G/T, then B) - unlike "::INFO::", "----" is not a string that
# only ever appears as this one bot's deliberate marker, so matching it
# ANYWHERE (the way the marker search above safely can) would risk cutting a
# real filename that happens to contain a run of hyphens. Requiring a
# size-shaped tail at the very end is what keeps this from firing on one.
_DASH_SIZE_SUFFIX_RE = re.compile(
    r'\s+-{2,}\s+([\d,]*\.?[\d,]+\s*[KMGTkmgt]?[Bb])\s*$')


def strip_info_suffix(rest):
    """Split "<filename> ::INFO:: <everything after>" into (filename, rest).

    update_list.py (update_list.py:216) writes "!<nick> <filename>  ::INFO::
    <size>" with two spaces before the marker - `rest` here is everything
    after the "!<nick> " prefix. Other bots on the network carry the same
    "::INFO::" marker but do not agree on the whitespace around it, and
    routinely tack on more than just a size afterwards - real examples seen
    in production: "...flac ::INFO:: 153.03MB (c) OmeNServE v2.60 (c)",
    "...mp3 ::INFO:: 6.32Mb 4m30s 192/44.10/JS  OmeNServE v2.60",
    "...mp3 ::INFO:: 19.95MB : OmenServe v2.71 :". A caller that only strips
    an exact "  ::INFO:: " (this project's own two-space convention) leaves
    all of that trailing branding/metadata attached to what it thinks is the
    filename - which is exactly what broke irc.py's cross-bot broadcast-
    search capture: the stored "filename" included the size and branding
    text, so the real DCC SEND offer that later came back (bearing only the
    bare filename) never matched it and every such fetch was rejected as
    unsolicited. Matching on the marker itself, tolerant of any amount of
    whitespace around it, and discarding EVERYTHING after it (not just a
    size field) fixes that for every bot's format, not just this project's
    own.

    A bot with NO marker word at all is the second, separate case - see
    _DASH_SIZE_SUFFIX_RE above. Tried only once the marker search above has
    already failed, so a line that happens to carry both would still prefer
    "::INFO::", the far more specific and far more common of the two.

    Best-effort beyond that: a line that carries neither returns the whole
    thing as the filename with an empty second value, rather than raising.
    Shared by `_split_entry_line()` below (this bot's own master list) and
    irc.py's cross-bot broadcast-search capture, which extracts the same
    shape out of another bot's reply and must not mistake any of the
    trailing tag for part of the filename when it later requests that exact
    name back with `!<nick> <filename>`.
    """
    marker = _INFO_MARKER_RE.search(rest)
    if marker:
        filename, size = rest[:marker.start()], rest[marker.end():]
    else:
        dash_match = _DASH_SIZE_SUFFIX_RE.search(rest)
        if dash_match:
            filename, size = rest[:dash_match.start()], dash_match.group(1)
        else:
            filename, size = rest, ""
    return filename.strip(), size.strip()


# The duration-and-quality tail one of OUR rows carries after its size when
# LIST_SHOW_AUDIO_INFO is on (#567): "::INFO:: 10.3MB 4m31s 320/44.1/JS", or
# "~245/44.1/JS" for a VBR average. Anchored to the end, and to the size
# token right after the marker, so nothing in a filename can match it.
_AUDIO_TAIL_RE = re.compile(r'(::INFO::\s*\S+)\s+\d+m\d+s\s+~?\d+/[\d.]+/\w+\s*$')


def without_audio_info(row):
    """The row with its audio tail removed, or the row unchanged."""
    return _AUDIO_TAIL_RE.sub(r'\1', row)


def _split_entry_line(line_strip):
    """Pull the filename and size back out of one "!..." master-list line.

    Best-effort on purpose: a line that does not split cleanly returns what
    it can rather than raising.

    This said it feeds "the read-only web dashboard, never IRC". It is on
    the live IRC search path too - find_matching_entries() calls it, and
    execute_search() calls that - so anything slow or throwing here costs a
    channel @find, not just a dashboard render (#234).
    """
    _, _, rest = line_strip.partition(" ")
    return strip_info_suffix(rest)


# What may sit between the words of a quoted phrase (#774): the same four
# characters an unquoted term is split on, plus the space - so "Metal Church"
# finds Metal Church, Metal_Church, Metal-Church and metal.church, and not
# Metallica ... Church.
_PHRASE_GAP = r"[ _.*\-]+"
_QUOTED = re.compile(r'"([^"]*)"')


def split_search_term(term):
    """A search term as the list find_matching_entries() takes (#774).

    Unquoted, exactly the rule @find has always used: `-`, `*`, `_` and `.`
    become spaces, the rest is split into lower-cased words, and a row
    matches when every word appears on it somewhere, in any order. That is
    right for "vivaldi winter" and wrong for a band whose name is two common
    words - `@find Metal Church` matched 6516 rows, every file with both
    "metal" and "church" in it anywhere.

    A part in double quotes is a PHRASE: its words must appear together, in
    that order, with only separators between them. It comes back as a tuple
    inside the same list, so every caller that passes the list through gets
    phrases without changing, and a term with no quotes returns exactly the
    list it always did. A one-word "phrase" is just a word, and a stray,
    unpaired quote is dropped rather than searched for - a literal quote was
    never in a filename anyone searched for, and today it made the whole
    search come back empty.

    Shared by execute_search() (@find) and webserver.split_list_search_words()
    (the dashboard's Search tab and a list's own search), so the two cannot
    drift apart - their docstrings have always said they use one rule.
    """
    text = str(term or "")
    phrases = []

    def _take(match):
        words = [word for word in re.split(r"[-*_.\s]+", match.group(1).lower()) if word]
        if len(words) > 1:
            phrases.append(tuple(words))
        elif words:
            phrases.append(words[0])
        return " "

    rest = _QUOTED.sub(_take, text).replace('"', " ")
    clean_term = re.sub(r'[-*_.]', ' ', rest)
    return [w.strip().lower() for w in clean_term.split() if w.strip()] + phrases


def find_matching_entries(search_words, limit=None, list_path=None, name=None):
    """IRC-agnostic core of the master-list search, extracted from execute_search().

    Scans the current master list exactly the way execute_search() always has -
    same file, same "!" line filter, same "every word must appear (case-
    insensitive) on the line" rule - but returns plain data instead of talking to
    IRC, so it has no queue_message/announce calls and no user/channel formatting.
    execute_search() calls this and keeps doing its own presentation on top;
    webserver.py's build_search_payload() and build_filelists_payload() call it
    too, with their own limit.

    Also carries FOLDER context forward, which execute_search() never needed and
    so never recorded: update_list.py writes each folder as a
    "D:\\MUSIC\\<folder>\\" line wrapped in a pair of "====...====" rule lines
    (update_list.py:190-195) before that folder's file lines. That header text is
    tracked here and attached to every entry as "folder".

    An empty search_words list matches every "!" line - this is what
    build_filelists_payload() wants. That is a deliberate difference from
    execute_search()'s own historical behaviour, where a search term that
    stripped down to zero words (e.g. "---") matched nothing at all; callers
    that need that old behaviour must check for an empty search_words list
    themselves before calling in, same as execute_search() now does below.

    `list_path`, when given, scans that file instead of find_latest_list()'s
    result - the same "!<nick> <filename>  ::INFO:: ..." shape, just not
    necessarily THIS bot's own master list. list_fetch.py uses this to run a
    fetched-and-extracted third-party bot's list through the exact same
    parsing pipeline (this function, _split_entry_line(), strip_info_suffix())
    rather than writing a second, parallel parser for someone else's list.

    Returns (entries, total_matches): `entries` is capped at `limit` (None means
    unlimited) and each is {"line": the raw "!..." text, "folder": the header
    text in effect or None, "filename": parsed filename, "size": parsed size
    string}; `total_matches` counts every match regardless of the cap, which is
    what the IRC search header reports even when only a handful are shown.
    """
    if list_path is None:
        # OUR OWN lists, all of them. Film and series live in a second file
        # since SEPARATE_VIDEO_LIST, and searching only the master would leave
        # every video listed and advertised but unfindable by @find - and,
        # through the same seam in dcc.py, unrequestable.
        entries = []
        total_matches = 0
        for path in all_list_paths(name):
            found, matched = find_matching_entries(
                search_words,
                limit=None if limit is None else max(0, limit - len(entries)),
                list_path=path, name=name)
            entries.extend(found)
            # Counted across every list, not per file: the search header
            # reports the true total even when the cap hides most of it.
            total_matches += matched
        return entries, total_matches

    entries = []
    total_matches = 0
    for line_strip, folder in _matching_lines(search_words, list_path):
        total_matches += 1
        if limit is None or len(entries) < limit:
            filename, size = _split_entry_line(line_strip)
            entries.append({
                "line": line_strip,
                "folder": folder,
                "filename": filename,
                "size": size,
            })
    return entries, total_matches


def _matching_lines(search_words, list_path):
    """Every "!" line of one list that matches, as (line, folder heading).

    The scan itself, one row at a time (#1134): find_matching_entries()
    collects it into a capped list, and iter_filelist_rows() hands it on
    without collecting anything, so installing a fetched list no longer
    holds every row of it in memory twice. One scan, so the two cannot
    drift apart. Nothing for a missing file.
    """
    # A tuple in the list is a quoted phrase (#774), compiled once per list
    # rather than once per line; a string is a word, matched as it always was.
    plain_words = [item for item in search_words if not isinstance(item, tuple)]
    phrase_patterns = [re.compile(_PHRASE_GAP.join(re.escape(word) for word in item))
                       for item in search_words if isinstance(item, tuple)]

    current_list_path = list_path
    if not current_list_path or not os.path.exists(current_list_path):
        return

    with open(current_list_path, "r", encoding="utf-8", errors="replace") as f:
        yield from _scan_lines(f, plain_words, phrase_patterns,
                               banner_first=is_mxrarserver_list(current_list_path))


# An mxrarserver list file (#1209): "<name>-MX.txt", "<name>-Files(<x>)-MX.txt",
# "<name>-Folders(<x>)-MX.txt". Every one it writes ends in "-MX".
_MXRARSERVER_LIST_RE = re.compile(r"-MX\.txt$", re.IGNORECASE)


def is_mxrarserver_list(path):
    """Whether the list at `path` is one mxrarserver wrote, by its name."""
    return bool(_MXRARSERVER_LIST_RE.search(os.path.basename(str(path or ""))))


def _searched_part(line_strip):
    """The part of one "!" row a search word is matched against, lower-cased (#1199).

    The filename: what is between "!<nick> " and the "::INFO::" marker (or
    the end of the row). Matching the whole row made "info" and "nfo" find
    every file, and the bot's own nick find the whole list, because every row
    carries both - and it disagreed with the cross-list search index, which
    holds only the filename. A size, a length or a bitrate in the tail is not
    searched either, as in the index.

    The marker is looked for with str.find first: this runs on every row of
    every list a search reads (#1126).
    """
    lowered = line_strip.lower()
    start = lowered.find(" ") + 1
    if not start:
        return ""
    end = lowered.find("::info::", start)
    if end >= 0:
        return lowered[start:end]
    if "--" in lowered:
        return strip_info_suffix(lowered[start:])[0]
    return lowered[start:]


def _scan_lines(lines, plain_words, phrase_patterns, state="none", on_heading=None,
                banner_first=False):
    """_matching_lines()'s parser, over any source of lines.

    The List Browser's folder table (#1128) reads the same lists in binary,
    to know where each folder is, and starts this part-way through a file -
    at a heading, in the "open" state - so there is one parser and not two
    that could drift. `on_heading` is called as each heading is taken.

    `banner_first` (#1209) is for an mxrarserver list, whose rows have no
    folder headings and which opens with its operator's banner: blocks of
    text between "=" rules, which this parser takes for headings - the last
    one, "> Overview" or the bot's name, then filed every row in the list.
    In such a list nothing before the first row is a heading. Only when the
    scan starts at the top of the file: one started at a heading (the folder
    table's "open") is past that point already. Every other list is read
    exactly as before - DCCore's own put a summary line under each heading,
    which is why "text after a heading" cannot be the test.
    """
    current_folder = None
    in_banner = banner_first and state == "none"
    # "none" -> saw the opening rule line, now expecting the folder line ("open")
    # -> saw the folder line, now expecting the closing rule line ("folder_seen")
    for line in lines:
        line_strip = line.replace('\x00', '').strip()
        if not line_strip:
            continue

        # "Every character is =", asked without building a set of the
        # line's characters (#1126). line_strip is not empty here, so
        # stripping the "=" away leaves nothing exactly when that is all
        # it held. The set cost about ten times as much and ran on every
        # line of every list: most of the scan's time at two million rows.
        is_rule = not line_strip.strip("=")
        if state == "none":
            if is_rule:
                state = "open"
                continue
        elif state == "open":
            if is_rule:
                continue  # malformed doubled rule; keep waiting for the folder line
            if line_strip.startswith("!"):
                # A FILE line where a folder heading was expected. The list
                # is malformed, and taking this as the heading loses the
                # file AND shifts every heading after it by one, so the
                # rest of the list is mis-attributed or swallowed too.
                #
                # Our own lists cannot reach this: update_list.py writes
                # every heading as "D:\MUSIC\<folder>\", which is never
                # all "=" and never starts with "!". A FETCHED list can -
                # list_fetch.py runs this same parser over a list another
                # bot wrote, and a folder there named "====" reads as a
                # second rule line, leaving this state machine waiting for
                # a heading that never comes. Found with a folder named
                # exactly that: every file after it, including files in
                # perfectly normal folders, vanished from search.
                #
                # A line starting with "!" is a file, whatever the state
                # machine expected, so the parser resynchronises here
                # instead of consuming it.
                state = "none"
            else:
                current_folder = line_strip
                state = "folder_seen"
                if on_heading is not None:
                    on_heading()
                continue
        elif state == "folder_seen":
            state = "none"
            if is_rule:
                continue  # the expected closing rule

        if not line_strip.startswith("!"):
            continue
        if in_banner:
            # The first row: what was taken for a heading above it was the
            # banner (#1209).
            current_folder = None
            in_banner = False

        if not plain_words and not phrase_patterns:
            yield line_strip, current_folder
            continue
        line_lower = _searched_part(line_strip)
        # Plain loops, not all() over a generator (#1126): the generator
        # was built afresh for every file line, and cost more than the
        # substring tests it ran. Same order, same early stop.
        matched = True
        for word in plain_words:
            if word not in line_lower:
                matched = False
                break
        if matched:
            for pattern in phrase_patterns:
                if not pattern.search(line_lower):
                    matched = False
                    break
        if not matched:
            continue

        yield line_strip, current_folder


# Every folder heading in the master list starts with this, whatever the
# library's real location is: update_list.py writes it verbatim (see its
# raw_folder_str). It is a piece of the format, NOT a path - the operator's
# library may well be at Z:\Music or /srv/library. What follows it is the
# folder relative to FILE_DIRECTORY, which is what dcc.py joins to resolve a
# request.
#
# This used to say the prefix was here "because the OmenServe listing format
# has always looked that way". That is not so, and the correction matters
# because it was being treated as a constraint.
#
# QuickList - the program that actually built OmenServe's lists - makes the
# written path an OPTION rather than a fixed shape. Its own documentation:
# "Optional partial folders in the public list, reducing chances of leaking
# personal info. This strips the initial input portion of the folder from the
# list." So lists in the wild carry full drive paths, stripped relative ones,
# and bare folder names, and OmenServe consumed all of them. There is no
# canonical prefix; "D:\MUSIC\" imitates one operator's list.
#
# Which means the prefix is ours to choose. AutoQ.mrc does not read it either:
# its dequeue match takes $nopath() of the folder - the last component only -
# so what comes in front has never mattered to it. See the comment at
# update_list.py's !rar row for that mechanism, and #256 for how it was
# verified.
#
# That is what makes the per-root labels in the multi-folder design (#164)
# possible without breaking anything downstream.
# "MEDIA", not "MUSIC", since the lists stopped being music-only. A heading
# reading "D:\MUSIC\TV\Spider-Noir (2026)\Season 01\" says the wrong
# thing about itself: the second component is the operator's FOLDER LABEL,
# so the fixed part in front of it should not contradict it. An
# operator's observation, on a real list.
#
# Safe to change for the reasons above: there is no canonical prefix, and
# AutoQ does not read this one - its dequeue match takes $nopath() of the
# folder, the last component only.
#
# What is NOT safe is stopping understanding the old one. Every list already
# in somebody's hands says "D:\MUSIC\", and a row pasted back out of one
# has to keep working - so the tuple below is what the read side uses.
LIST_FOLDER_PREFIX = "D:\\MEDIA\\"

# Every prefix a heading may ARRIVE with, current first. One is written; all
# are understood. A list downloaded a year ago is still a list somebody is
# pasting rows out of today.
LIST_FOLDER_PREFIXES = (LIST_FOLDER_PREFIX, "D:\\MUSIC\\")

# A leading drive specifier on one path component: "C:", "C:Windows".
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def list_heading_parts(header):
    """The path components of a folder heading, prefix and drive letters gone.

    Split out of resolve_list_folder() so the label-aware resolution below and
    the single-root one share one answer about what a heading actually says.

    A drive specifier has to come off each part before anything joins them. On
    Windows, os.path.join() treats an argument like "C:" or "C:Windows" as
    drive-relative and DISCARDS everything before it - so a heading naming any
    drive other than the one the prefix strips would return a path with no
    relation to the base at all, which is the one thing resolution promises not
    to do.

    Reachable input since #121: the `!rar` path passes a folder the user typed
    in the channel through here. is_safe_path() still refuses the result, but a
    joiner that can silently drop its own base is the wrong thing to be relying
    on a downstream guard for.
    """
    text = (header or "").strip()
    for prefix in LIST_FOLDER_PREFIXES:
        if text.upper().startswith(prefix):
            text = text[len(prefix):]
            break
    # Headings are written with backslashes regardless of the host, so split on
    # both and let os.path.join put the platform's own separator back.
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    parts = [_DRIVE_PREFIX_RE.sub("", part, count=1) for part in parts]
    return [part for part in parts if part]


def resolve_list_folder_with_root(header, name=None):
    """(path, Folder) for a heading: where it points, and which folder it is in.

    The root matters to callers that guard on it. dcc.py's `!rar` path runs
    is_safe_path() against the library root and then asks whether the result is
    an artist root; with several folders configured, both questions are about
    the ONE folder this heading resolved into, not about a global. Returning
    the root here is what lets those checks keep their exact current strength
    instead of widening to "inside any configured folder".

    How a heading is read, in order:

    1. If its first component is a configured label, it is a labelled path -
       resolve inside that folder. This is what update_list.py writes once
       there is more than one folder (#164).
    2. Otherwise, try each configured folder in turn, in the operator's own
       order, and take the first where the path actually exists. This is how a
       heading from an OLDER list - written before labels, and still pasted
       back by anyone who saved one - keeps resolving.

    Existence decides between the two, so a label that happens to share a name
    with a real subfolder resolves to whichever one is really there rather than
    to whichever rule was written first.

    When nothing exists, the labelled reading is returned if there was one and
    the first folder otherwise. The caller's own existence check then fails
    exactly as it did before, rather than this inventing a path that is real
    but wrong.
    """
    # This list's folders. A heading is a heading IN a list, and a label
    # that names a folder of one list means nothing in another - resolving
    # against the primary's would send a request into the wrong library.
    folders = library.folders(name)
    parts = list_heading_parts(header)

    if not folders:
        return ("", None)
    if not parts:
        return (folders[0].path, folders[0])

    labelled = None
    label = library.folder_for_label(parts[0], name)
    if label is not None:
        labelled = (os.path.join(label.path, *parts[1:]) if parts[1:] else label.path,
                    label)
        if os.path.isdir(platform_compat.long_path(labelled[0])):
            return labelled

    for folder in folders:
        candidate = os.path.join(folder.path, *parts)
        if os.path.isdir(platform_compat.long_path(candidate)):
            return (candidate, folder)

    if labelled is not None:
        return labelled
    return (os.path.join(folders[0].path, *parts), folders[0])


def resolve_list_folder(header, base=None, name=None):
    """Turn a master-list folder heading into a real path on this machine.

    Mirrors what dcc.handle_download_request() does when it resolves a
    requested name: strip the format's fixed prefix, and join what remains to
    the folder it belongs to. An entry that carried no heading at all resolves
    to the library root, which is where such a file actually sits.

    This exists so the operator is shown a path they can act on. The raw
    heading names a drive most installs do not have, which is worse than
    useless in a tool whose whole job is "go and look at these folders".

    An explicit `base` still resolves against that one directory, for callers
    that genuinely mean a particular root rather than "wherever this heading
    lives". Without one, resolution goes through the configured folders - see
    resolve_list_folder_with_root() for how a heading is read.
    """
    if base is None:
        return resolve_list_folder_with_root(header, name)[0]

    parts = list_heading_parts(header)
    return os.path.join(base, *parts) if parts else base


def find_duplicate_filenames(entries):
    """Filenames the master list carries under more than one folder.

    Takes find_matching_entries([]) output and returns

        [{"filename": str, "folders": [str, ...], "count": int}, ...]

    in the order the list meets them, folders in list order too, and only for
    names that appear under two or more folders.

    WHY THIS IS WORTH KNOWING

    A request names a file, not a path. "!<nick> Track 01.flac" is all a
    requester can say, because a bare filename is all the list gives them to
    copy. dcc.handle_download_request() then resolves that name against this
    same list and serves the FIRST folder it finds it under - so every later
    copy is listed, looks requestable, and cannot be fetched at all.

    READS THE LIST, NOT THE LIBRARY

    Deliberately. The list is what the requester saw and what the resolver
    reads, so a name that collides here is a name that collides for them,
    whatever the filesystem happens to hold at this moment. It also means this
    can be answered on demand from a file already on disk, without walking the
    library again.

    The order is the useful part: the first folder listed under a name is the
    copy a request for that name will actually reach.

    Matching is case-insensitive, because the resolver compares lowercased and
    a requester typing a name back cannot be expected to reproduce its case.
    """
    folders_by_name = {}
    first_seen = []
    for entry in entries:
        filename = (entry.get("filename") or "").strip()
        if not filename:
            continue
        key = filename.lower()
        # A folderless entry is a real location - the library root - not a
        # missing value, so it counts as somewhere a copy can sit.
        folder = entry.get("folder") or ""
        if key not in folders_by_name:
            folders_by_name[key] = []
            first_seen.append((filename, key))
        folders = folders_by_name[key]
        if folder not in folders:
            folders.append(folder)
    return [{"filename": name,
             "folders": folders_by_name[key],
             "count": len(folders_by_name[key])}
            for name, key in first_seen if len(folders_by_name[key]) > 1]


# Marks a name already counted, so a third folder holding it does not count
# it twice. A sentinel object rather than a string, because any string is a
# folder name somebody could have.
_ALREADY_COUNTED = object()


def count_duplicate_filenames(pairs):
    """How many filenames appear under more than one folder.

    `pairs` is any iterable of (folder, filename). Exactly the length of what
    find_duplicate_filenames() returns, for callers that only want the number.

    WHY BOTH EXIST (#463)

    find_duplicate_filenames() answers "which names, and where" and has to
    hold every folder for every name to do it. update_list.py wants the count
    alone, for one warning line at the end of a build - and was building a
    second full copy of the library as dicts to ask for it. At 5.4M files that
    copy measured 2.2 GiB and took the peak for the whole rebuild to 3.5 GiB;
    the result was passed to len() and dropped.

    This keeps one entry per distinct name instead of one per row, and holds a
    single folder against each rather than a growing list.

    The DEFINITION is the other function's, deliberately: same lowercased
    match, same "two or more DISTINCT folders" rule, and a folderless entry
    counts as the library root rather than a missing value. A count that
    disagreed with the view the operator is sent to would be worse than no
    count at all.
    """
    first_folder = {}
    duplicates = 0
    for folder, filename in pairs:
        name = (filename or "").strip()
        if not name:
            continue
        key = name.lower()
        where = folder or ""
        if key not in first_folder:
            first_folder[key] = where
            continue
        seen = first_folder[key]
        if seen is _ALREADY_COUNTED or seen == where:
            continue
        first_folder[key] = _ALREADY_COUNTED
        duplicates += 1
    return duplicates


# "!rar <folder>" - what is left of a pack request once the "!<nick> " prefix
# has been taken off by the same parse every other row goes through.
_RAR_REQUEST_RE = re.compile(r"^!rar\s+(.+)$", re.IGNORECASE)


def rar_folder_of(title):
    """The folder a "!rar" row asks for, or "" if the row is not one.

    Whitespace-only or bare "!rar" is not a request for anything, and an empty
    answer is the honest reading rather than a folder named "".
    """
    match = _RAR_REQUEST_RE.match(str(title or "").strip())
    return match.group(1).strip() if match else ""


def pack_path_of(title, size=""):
    """The folder an mxrarserver pack row asks for, or "" if the row is not
    one (#1209).

    mxrarserver's folder list has no "!rar": each row is "!<trigger>
    <absolute Windows path>.rar" - "!Music E:\\Music\\Artist\\Album.rar" - and
    asking for that line verbatim has the bot pack the folder and send it as
    a RAR. Its own test for a folder request is the same: ends in ".rar" and
    holds a path separator. A row with a size (an "::INFO::" or a "----"
    suffix) is a file, which is how its file list writes every row, so that
    is asked first and costs a row nothing else.

    The path itself is the answer, unchanged: it is what goes back on the
    wire, so it must survive exactly as the list wrote it.
    """
    if size:
        return ""
    text = str(title or "").strip()
    if (not text or text.startswith("!") or "\\" not in text
            or not text.lower().endswith(".rar")):
        return ""
    return text


def entries_to_filelist_rows(entries, source):
    """Shape find_matching_entries() output into the File Lists view's row
    format: {"title", "size", "format", "source"}, deduping same
    filename+size the way webserver.build_filelists_payload() always has.

    Shared by webserver.py (this bot's own list, source=config.NICKNAME) and
    list_fetch.py (another bot's fetched-and-extracted list, source=that
    bot's nick) so the two surfaces can never drift in what a "row" looks
    like on the dashboard.
    """
    return list(_filelist_rows(entries, source))


def iter_filelist_rows(list_path, source):
    """entries_to_filelist_rows() over the whole of one list, a row at a time.

    For a caller that only counts the rows and writes them to the search
    index (#1134): installing a fetched list, an extra list in its archive,
    a backfill. find_matching_entries() with no limit built a dict per row,
    raw line included, and entries_to_filelist_rows() a second, and both
    lists stayed alive for the whole write - about 412 MB at 378k rows,
    against 143 MB streamed. Same scan, same split, same dedup: only the
    collecting is gone. Wrap it in CountedRows to know how many there were.
    """
    def entries():
        for line_strip, folder in _matching_lines([], list_path):
            filename, size = _split_entry_line(line_strip)
            yield {"folder": folder, "filename": filename, "size": size}

    return _filelist_rows(entries(), source)


class CountedRows(object):
    """Rows for list_index.index_bot_list() that count themselves as they pass.

    One pass only, like the generator inside it. total() is the number of
    rows in the whole list, not the number the index happened to take: it
    drains whatever is left first. That matters because index_bot_list()
    returns 0 WITHOUT iterating when the index is unavailable, and stops
    part-way on an error, and a count of what it consumed would then be 0 or
    partial - and list_fetch would store that as the list's size, or throw
    away a good extra list as having "no entries" (#1134).

    An error from the list itself is kept and raised again by total(), so a
    list that cannot be read fails at the count as it did when it was parsed
    up front - and not as a short count.

    No __len__, on purpose: list() and other consumers ask an object with
    one for its length before iterating, and here that would drain every
    row before the first was handed over. bool() peeks at one row and keeps
    it for the next pass.
    """

    def __init__(self, rows):
        self._rows = iter(rows)
        self._peeked = []
        self._count = 0
        self._done = False
        self.error = None

    def __iter__(self):
        return self

    def __next__(self):
        if self._peeked:
            self._count += 1
            return self._peeked.pop()
        if self._done:
            raise StopIteration
        try:
            row = next(self._rows)
        except StopIteration:
            self._done = True
            raise
        except Exception as err:
            self._done = True
            self.error = err
            raise
        self._count += 1
        return row

    def any_rows(self):
        """True if there is at least one row, without consuming it."""
        if self._peeked:
            return True
        if self._done:
            return False
        try:
            self._peeked.append(next(self._rows))
        except StopIteration:
            self._done = True
            return False
        except Exception as err:
            self._done = True
            self.error = err
            raise
        return True

    __bool__ = any_rows

    def total(self):
        """How many rows the list has, draining any not yet consumed."""
        if not self._done or self._peeked:
            try:
                for _row in self:
                    pass
            except Exception:
                pass
        if self.error is not None:
            raise self.error
        return self._count


def _filelist_rows(entries, source):
    """The rows entries_to_filelist_rows() returns, one at a time."""
    seen = set()
    for entry in entries:
        filename = entry.get("filename", "?")
        size = entry.get("size", "")
        # `or ""`, not a get() default: a list with no folder headers
        # stores folder=None, and .get(k, "") returns the default only
        # when the KEY is absent, never when its value is None.
        folder = entry.get("folder") or ""
        # Folder is part of the key. It was (filename, size) alone, which
        # collapsed the same track appearing under two albums into one row and
        # silently discarded the second folder - invisible while rows were a
        # flat list, wrong once they are grouped under the folder they came
        # from. (Zero occurrences in the operator's own 36,208-entry library,
        # but a fetched bot's list has no such guarantee.)
        key = (folder.lower(), filename.lower(), size)
        if key in seen:
            continue
        seen.add(key)
        ext = os.path.splitext(filename)[1].lstrip(".").upper()
        yield {
            "title": filename,
            "size": size,
            "format": ext,
            "source": source,
            "folder": folder,
            # THE FOLDER THIS ROW ASKS FOR, when the row is a "!rar" request
            # rather than a file - and "" when it is not.
            #
            # A bot that packs whole albums publishes a SEPARATE list whose
            # every row is the line to type: "!<nick> !rar <folder>". So the
            # list itself says which folders that bot will pack, per folder,
            # and no capability has to be inferred from an advert or guessed
            # at from a filename convention. That matters because the two
            # lists need not agree: a bot can offer one folder as loose files
            # and another only as a pack, and asking it for a folder it never
            # offered spends a fetch slot for half an hour on a reply that is
            # never coming.
            #
            # The title is left exactly as the list wrote it. These rows are
            # meant to be copied verbatim - that is what the header of every
            # such list tells the reader to do - so this adds a field beside
            # it rather than reformatting it.
            #
            # Asked only of a title that starts with "!" (#1136): nothing
            # else can match rar_folder_of()'s anchored "^!rar", and running
            # the regex on every row of every list cost more than the answer.
            #
            # mxrarserver's folder rows (#1209) are the other shape of the
            # same thing, "!<trigger> <path>.rar" with no "!rar", and are
            # offered the same way: rar_folder is the path, which
            # webserver.build_folder_rar_fetch_enqueue_result() recognises
            # and sends back as the bot wrote it.
            "rar_folder": (rar_folder_of(filename)
                           if filename.lstrip().startswith("!")
                           else pack_path_of(filename, size)),
            # What we have already asked this bot for: "requested",
            # "received", or "" for neither. Declared HERE, empty, rather
            # than added by whichever payload happens to know - both this
            # bot's own list and a fetched one go through this function
            # precisely so the frontend sees one row shape, and a key present
            # in one and absent in the other is how that stops being true.
            #
            # Always "" for our own list, which is correct rather than a
            # placeholder: nothing is ever requested from ourselves.
            # webserver.mark_rows_with_fetch_state() fills it in for the two
            # payloads where the question means something.
            "mark": "",
        }


def group_rows_by_folder(rows):
    """Rows from entries_to_filelist_rows() -> one group per folder.

    [{"folder": str, "count": int, "entries": [row, ...]}, ...]

    Order is first-seen, which is the master list's own order, so albums stay
    where update_list.py wrote them instead of being re-sorted into an order
    the operator does not recognise from their own disk.

    A row with no folder - possible in a foreign bot's list, whose format is
    not ours to rely on - is grouped under "" rather than dropped, and the
    frontend labels that group rather than showing a blank heading.
    """
    order = []
    groups = {}
    for row in rows:
        folder = row.get("folder", "") or ""
        group = groups.get(folder)
        if group is None:
            group = {"folder": folder, "count": 0, "entries": []}
            groups[folder] = group
            order.append(folder)
        group["entries"].append(row)
        group["count"] += 1
    return [groups[folder] for folder in order]


# The second bound on a page of folders, applied between folders and never
# inside one. A folder count alone does not bound the response: folder sizes
# are uneven (the operator's own library runs 1 to 127 files, median 9), and a
# foreign bot's list carries no shape guarantee at all. This is the safety
# valve for the unbounded-payload problem of issue #76, not the unit of paging.
#
# 2500 is chosen against the real library: 200 folders comes to about 1,720
# rows, so the valve stays shut in ordinary use and only trips on a list of
# unusually large folders.
FILELISTS_MAX_PAGE_ROWS = 2500


def page_folder_groups(groups, offset, limit, max_rows=None):
    """One page of folder groups, sliced by FOLDER rather than by row.

    Returns (page, total_folders, total_rows, row_capped).

    A folder is never split across a page: whatever the caller asked for, a
    group is returned whole or not at all. Grouping only helps if opening a
    folder shows all of it.

    `max_rows` is a safety valve, not the unit. Folder sizes are uneven - the
    operator's library runs 1 to 127 files per folder, median 9 - so a folder
    count alone does not bound the response, which is the unbounded-payload
    problem issue #76 existed to remove. The page stops early once adding the
    next folder would exceed it, and always returns at least one folder even
    if that folder alone is larger, because returning nothing would leave the
    caller unable to advance.

    `row_capped` is True exactly when the valve is the reason this page has
    fewer folders than `limit` asked for - never when it simply ran out of
    groups. #477: without this, a page cut short by one outsized folder (down
    to a single folder, in the reported case) is indistinguishable from a
    short LAST page, and reads as the pager being broken rather than a
    safety valve doing its job.
    """
    total_folders = len(groups)
    total_rows = sum(group["count"] for group in groups)

    if offset < 0:
        offset = 0
    window = groups[offset:offset + limit] if limit else groups[offset:]

    if not max_rows:
        return window, total_folders, total_rows, False

    page = []
    rows_so_far = 0
    row_capped = False
    for group in window:
        if page and rows_so_far + group["count"] > max_rows:
            row_capped = True
            break
        if not page and group["count"] > max_rows:
            # One folder larger than the whole ceiling. Returning it whole
            # would reopen the unbounded response issue #76 removed - and it
            # is not hypothetical: a list with no folder headers at all parses
            # as ONE group holding every row, which is exactly the shape a
            # foreign bot can send.
            #
            # So this is the one place a group is cut. It is still returned,
            # because returning nothing would leave the caller unable to
            # advance past it, and `count` still reports the true size so the
            # view can say "showing 2500 of 51000" rather than quietly
            # implying that is all there is.
            page.append({
                "folder": group["folder"],
                "count": group["count"],
                "entries": group["entries"][:max_rows],
                "truncated": True,
            })
            rows_so_far += max_rows
            row_capped = True
            break
        page.append(group)
        rows_so_far += group["count"]
    return page, total_folders, total_rows, row_capped


# THE FOLDER TABLE (#1128). The List Browser asks for one page of folders at
# a time, and every page re-read the whole list to answer it - our own lists
# here, a fetched one in list_fetch.get_fetched_bot_page(): a dict per row,
# a second dict per row, every row grouped, and then 200 folders kept - 31 s
# and 2.1 GB per page at two million rows of our own list, paid
# again on every page change. So the first page of a list builds this table
# instead: where each folder's rows are in the files, how many there are, and
# which of them dedup drops. A page then seeks to its own folders and parses
# only those. Nothing per ROW is kept, so the #76 rule that a list is not held
# in memory still stands: about 38 bytes per folder - 133,000 folders come to
# about 5 MB.
#
# AND NEVER MUCH MORE THAN THE LIST ITSELF. A fetched list is written by
# somebody else, and a crafted one under the 128 MB text ceiling - two
# headings alternating with one row each, or millions of copies of one row -
# made the first version of this table hold about 30 times the list's size:
# 526 MB for a 16.8 MB list, built in 21 s under list_fetch's lock (the
# #1128 audit). So the rows dedup drops are kept as [start, stop) spans in
# one flat array, and only for runs a group shows, and the build counts what
# it stores as it goes: past _folder_table_budget() it gives up, and the
# list is read whole for every page, as it was before there was a table.
#
# EXACTLY today's page, which is more than it looks:
#  - dedup is (folder.lower(), filename.lower(), size) in FILE ORDER, across
#    every file, and two headings that differ only in case share keys - so a
#    page cannot recompute it from its own folders. The table records each
#    dropped row by its position in its run;
#  - a heading that appears twice is ONE group, at its first position, so a
#    group can be several runs of rows, even in different files;
#  - a group exists only if one of its rows survived dedup, so the table is
#    grouped after dedup, as group_rows_by_folder() is;
#  - rows before any heading are the '' group;
#  - our own list is the master list AND the video list, read as one; a
#    fetched list is one file, read the same way.
#
# Keyed on every file's (path, mtime, size), as count_request_lines() is, so
# a rebuilt list is read again; commands.py also drops the tables when a
# rebuild finishes. A page checks each file it opens against that key and
# each run's rows against a checksum, so a rewrite landing between the stat
# and the read, or a same-size one inside one mtime tick, costs a fallback to
# the whole-list parse, never wrong rows. A refetch and a purge drop the
# tables of that bot's files by name as well (forget_folder_tables(under=)).
# The lock is runtime.py's; the dict starts empty after a !rehash, which
# costs one rebuild of the table.
#
# At most _FOLDER_TABLES_KEPT, the least recently used dropped first: our own
# list and the few fetched lists somebody is paging through. A fetched list
# is rarely more than a few tens of thousands of folders, about 1 MB of table.
_folder_table_lock = runtime.list_folder_table_lock
_folder_tables = {}   # signature -> _FolderTable, or None when unusable
_FOLDER_TABLES_KEPT = 8

# The budget: a table may cost at most this share of its files' size, and
# any list at least the floor, so a small list always gets one. A normal list
# costs far less - about 3% of its size at two million rows of our own list,
# where a folder holds a heading and a dozen rows of 60 bytes or more. Only a
# list of tiny folders or of rows dedup drops, which is what a crafted list
# is made of, comes near it.
_FOLDER_TABLE_BUDGET_FLOOR = 1 << 20
_FOLDER_TABLE_BUDGET_SHARE = 2       # a table costs at most size / 2

# What the build counts against the budget, in bytes. A run: its six seg_
# items and run_next (30), what the build alone keeps for it - its heading's
# number, the next run with the same lower-cased heading, a slot for the
# second dedup (12) - and, at most, the group it starts, with its last run
# while built (12), the heading (4) and the lower-cased heading (8). A span of dropped rows: two numbers while
# the build collects it and two in the table (16). A run with spans: its
# number and where they start, while built and in the table (16). The heading
# strings themselves are not counted: the build drops them, and there are no
# more of them than runs.
_RUN_BYTES = 66
_SPAN_BYTES = 16
_DUP_RUN_BYTES = 16


def _folder_table_budget(signature):
    """The most a table of the files in `signature` may cost, in bytes."""
    size = sum(size for _path, _mtime, size in signature)
    return max(_FOLDER_TABLE_BUDGET_FLOOR, size // _FOLDER_TABLE_BUDGET_SHARE)


class _StaleFolderTable(Exception):
    """A file no longer holds what its table says. The page falls back."""


class _OverBudget(Exception):
    """The table would cost more than _folder_table_budget(). Never kept."""


class _FolderTable(object):
    """Where each folder group of a set of list files is. See above."""

    __slots__ = ("signature", "seg_file", "seg_start", "seg_open",
                 "seg_end", "seg_rows", "seg_crc", "run_next", "dup_runs",
                 "dup_from", "dup_spans", "group_first", "group_count",
                 "total_rows")

    def __init__(self, signature):
        self.signature = signature
        # One entry per RUN: consecutive rows of one file under one heading.
        self.seg_file = array.array("B")
        # Where to start reading it: the heading line, in the parser's "open"
        # state, or the top of the file for rows before any heading.
        self.seg_start = array.array("q")
        self.seg_open = array.array("B")
        self.seg_end = array.array("q")      # just past its last row
        self.seg_rows = array.array("i")     # rows the parser yields in it
        self.seg_crc = array.array("I")      # crc32 of those rows' lines
        self.run_next = array.array("i")     # the next run of its group, or -1
        # The rows dedup drops, for the runs a group shows (#1128's audit):
        # those runs in order, where each one's spans start in dup_spans
        # (one more entry at the end), and [start, stop) row positions in the
        # run, in pairs. A run of a million copies of one row is one span.
        self.dup_runs = array.array("i")
        self.dup_from = array.array("i")
        self.dup_spans = array.array("i")
        self.group_first = array.array("i")  # a group's first run
        self.group_count = array.array("i")  # rows it shows
        self.total_rows = 0

    def runs(self, group):
        """The runs of `group`, in file order."""
        runs = []
        run = self.group_first[group]
        while run != -1:
            runs.append(run)
            run = self.run_next[run]
        return runs

    def dups(self, run):
        """The [start, stop) spans of rows dedup drops in `run`, flat."""
        at = bisect.bisect_left(self.dup_runs, run)
        if at == len(self.dup_runs) or self.dup_runs[at] != run:
            return ()
        return self.dup_spans[self.dup_from[at]:self.dup_from[at + 1]]

    def nbytes(self):
        """What the table keeps: the items of its arrays, which is all it
        holds that grows with the list."""
        arrays = (self.seg_file, self.seg_start, self.seg_open, self.seg_end,
                  self.seg_rows, self.seg_crc, self.run_next, self.dup_runs,
                  self.dup_from, self.dup_spans, self.group_first,
                  self.group_count)
        return sum(len(a) * a.itemsize for a in arrays)


class _LoneCarriageReturn(Exception):
    """A line ending the binary reader would not see as the text reader does."""


def _binary_lines(handle, start, end, at, crc_line):
    """Lines of `handle` from byte `start` up to `end` (None: the end).

    Decoded as the text reader decodes them - utf-8, errors="replace", line
    by line, which is the same because no byte of a multi-byte sequence can
    be a newline. at[0], at[1] are set to each line's start and end; crc_line
    gets its raw bytes. Text mode also ends a line at a lone "\\r", which
    splitting on "\\n" does not, so such a file is refused, not misread.
    """
    handle.seek(start)
    offset = start
    for raw in handle:
        if end is not None and offset >= end:
            return
        cr = raw.find(b"\r")
        if cr != -1 and (cr != len(raw) - 2 or not raw.endswith(b"\n")):
            raise _LoneCarriageReturn()
        at[0] = offset
        offset += len(raw)
        at[1] = offset
        crc_line[0] = raw
        yield raw.decode("utf-8", "replace")


def _build_folder_table(paths, signature):
    """The folder table of `paths`, or None when it cannot be exact or would
    cost more than _folder_table_budget().

    Raises OSError when a file cannot be read, and _StaleFolderTable when one
    changed while it was being read. A file rewritten between the signature
    and this read is caught later, by the check each page makes as it opens
    the file."""
    if len(paths) > 255:
        return None
    try:
        return _build_folder_table_within(paths, signature,
                                          _folder_table_budget(signature))
    except (_LoneCarriageReturn, _OverBudget):
        # Nothing of the half-built table is kept: it is only ever returned
        # whole.
        return None


def _span_append(runs, starts, spans, run, position):
    """Mark row `position` of `run` dropped, in the flat span arrays; True
    when that took a new span, which the caller counts."""
    if runs and runs[-1] == run:
        if spans[-1] == position:
            spans[-1] = position + 1
            return False
    else:
        runs.append(run)
        starts.append(len(spans))
    spans.append(position)
    spans.append(position + 1)
    return True


def _build_folder_table_within(paths, signature, budget):
    table = _FolderTable(signature)
    # For the build only, per run: its heading's number in `keys`, and the
    # next run whose heading is the same lower-cased (-1: none).
    run_key = array.array("i")
    next_same = array.array("i")
    keys = {}                        # heading text -> its number
    key_lower = array.array("i")     # per heading: its lower-cased one's number
    lowers = {}                      # heading.lower() -> its number
    lower_first = array.array("i")   # per lower-cased heading: its first run
    lower_last = array.array("i")    # ... and its last
    # The rows each run's own dedup drops, in run order, and those the second
    # dedup below drops, in the order it reads runs - flat spans, as kept.
    one_runs, one_from, one_spans = (array.array("i"), array.array("i"),
                                     array.array("i"))
    two_runs, two_from, two_spans = (array.array("i"), array.array("i"),
                                     array.array("i"))

    def check_budget():
        cost = (len(run_key) * _RUN_BYTES
                + (len(one_spans) + len(two_spans)) // 2 * _SPAN_BYTES
                + (len(one_runs) + len(two_runs)) * _DUP_RUN_BYTES)
        if cost > budget:
            raise _OverBudget()

    for file_index, path in enumerate(paths):
        with open(path, "rb") as handle:
            at = [0, 0]
            raw = [b""]
            heading = [0]   # where the heading in force started

            def on_heading():
                heading[0] = at[0]

            run_folder = _NO_RUN
            run = -1
            seen = None
            position = 0
            crc = 0
            for line_strip, folder in _scan_lines(
                    _binary_lines(handle, 0, None, at, raw), [], [],
                    on_heading=on_heading, banner_first=is_mxrarserver_list(path)):
                if run_folder is _NO_RUN or folder != run_folder:
                    if run_folder is not _NO_RUN:
                        table.seg_rows.append(position)
                        table.seg_crc.append(crc)
                    run_folder = folder
                    run = len(table.seg_start)
                    table.seg_file.append(file_index)
                    table.seg_start.append(heading[0] if folder is not None else 0)
                    table.seg_open.append(1 if folder is not None else 0)
                    table.seg_end.append(0)
                    key = folder or ""
                    number = keys.get(key)
                    if number is None:
                        lower = key.lower()
                        lower_number = lowers.get(lower)
                        if lower_number is None:
                            lower_number = lowers[lower] = len(lower_first)
                            lower_first.append(run)
                            lower_last.append(-1)
                        number = keys[key] = len(key_lower)
                        key_lower.append(lower_number)
                    run_key.append(number)
                    next_same.append(-1)
                    lower_number = key_lower[number]
                    if lower_last[lower_number] != -1:
                        next_same[lower_last[lower_number]] = run
                    lower_last[lower_number] = run
                    check_budget()
                    seen = set()
                    position = 0
                    crc = 0
                crc = zlib.crc32(raw[0], crc)
                table.seg_end[-1] = at[1]
                filename, size = _split_entry_line(line_strip)
                dedup_key = (filename.lower(), size)
                if dedup_key in seen:
                    if _span_append(one_runs, one_from, one_spans, run, position):
                        check_budget()
                else:
                    seen.add(dedup_key)
                position += 1
            if run_folder is not _NO_RUN:
                table.seg_rows.append(position)
                table.seg_crc.append(crc)
    seen = None
    one_from.append(len(one_spans))

    # A run is deduplicated on its own above, which is exact only when no
    # other run shares its folder.lower(). Those that do - a repeated
    # heading, two headings that differ only in case, rows before the
    # first heading of each file - are done again together, in file order.
    two_slot = array.array("i", [-1]) * len(run_key)
    handles = {}
    try:
        for lower_number, first in enumerate(lower_first):
            if first == lower_last[lower_number]:
                continue
            seen = set()
            run = first
            while run != -1:
                for position, (line_strip, _folder) in enumerate(
                        _run_lines(table, run, handles)):
                    filename, size = _split_entry_line(line_strip)
                    dedup_key = (filename.lower(), size)
                    if dedup_key in seen:
                        if not two_runs or two_runs[-1] != run:
                            two_slot[run] = len(two_runs)
                        if _span_append(two_runs, two_from, two_spans, run, position):
                            check_budget()
                    else:
                        seen.add(dedup_key)
                run = next_same[run]
            seen = None
    finally:
        for handle in handles.values():
            handle.close()
    two_from.append(len(two_spans))

    # Grouped AFTER dedup, by heading text, in first-seen order: a run whose
    # every row was a duplicate starts no group, as in group_rows_by_folder(),
    # and its spans are not kept - no page reads that run.
    groups = {}                       # heading number -> its group
    group_last = array.array("i")     # per group: its last run so far
    table.run_next = array.array("i", [-1]) * len(run_key)
    one_at = 0
    for run, number in enumerate(run_key):
        lower_number = key_lower[number]
        spans = ()
        if lower_first[lower_number] != lower_last[lower_number]:
            slot = two_slot[run]
            if slot != -1:
                spans = two_spans[two_from[slot]:two_from[slot + 1]]
        else:
            while one_at < len(one_runs) and one_runs[one_at] < run:
                one_at += 1
            if one_at < len(one_runs) and one_runs[one_at] == run:
                spans = one_spans[one_from[one_at]:one_from[one_at + 1]]
        dropped = 0
        for index in range(0, len(spans), 2):
            dropped += spans[index + 1] - spans[index]
        shown = table.seg_rows[run] - dropped
        if not shown:
            continue
        if spans:
            table.dup_runs.append(run)
            table.dup_from.append(len(table.dup_spans))
            table.dup_spans.extend(spans)
        group = groups.get(number)
        if group is None:
            group = groups[number] = len(table.group_first)
            table.group_first.append(run)
            table.group_count.append(0)
            group_last.append(run)
        else:
            table.run_next[group_last[group]] = run
            group_last[group] = run
        table.group_count[group] += shown
        table.total_rows += shown
    table.dup_from.append(len(table.dup_spans))
    return table


_NO_RUN = object()


def _run_lines(table, run, handles):
    """(line, folder) for every row the parser yields in one run, checked
    against what the table recorded for it."""
    file_index = table.seg_file[run]
    handle = handles.get(file_index)
    if handle is None:
        path, mtime_ns, size = table.signature[file_index]
        handle = open(path, "rb")
        handles[file_index] = handle
        st = os.fstat(handle.fileno())
        if (st.st_mtime_ns, st.st_size) != (mtime_ns, size):
            raise _StaleFolderTable()
    at = [0, 0]
    raw = [b""]
    crc = 0
    lines = _binary_lines(handle, table.seg_start[run], table.seg_end[run], at, raw)
    for line_strip, folder in _scan_lines(
            lines, [], [], state="open" if table.seg_open[run] else "none",
            banner_first=is_mxrarserver_list(table.signature[file_index][0])):
        crc = zlib.crc32(raw[0], crc)
        yield line_strip, folder
    if crc != table.seg_crc[run]:
        raise _StaleFolderTable()


def _folder_table(paths):
    """The cached table for `paths` as they are now, built if needed."""
    signature = _list_signature(paths)
    if any(mtime is None for _path, mtime, _size in signature):
        return None
    with _folder_table_lock:
        if signature in _folder_tables:
            # To the back of the line: the dict's order is the use order.
            table = _folder_tables.pop(signature)
            _folder_tables[signature] = table
            return table
        try:
            table = _build_folder_table(paths, signature)
        except _StaleFolderTable:
            # Rewritten while it was read: nothing kept, and the next page
            # builds the table of the new file.
            return None
        except OSError as err:
            print(f"[LIST] Could not index the list's folders ({err}); "
                  f"reading it whole for this page.")
            return None
        # A None is kept too: a list over its budget, or with a lone
        # carriage return, is the same list on the next page, which then
        # reads it whole at once instead of building most of a table first.
        while len(_folder_tables) >= _FOLDER_TABLES_KEPT:
            _folder_tables.pop(next(iter(_folder_tables)))
        _folder_tables[signature] = table
        return table


def forget_folder_tables(under=None):
    """Drop the folder tables: every one, or those reading a file in `under`.

    Every one when a rebuild of our own lists finishes; `under` a bot's
    extract directory when its list is fetched again or purged, which
    rewrites or removes files in place (#1128). Compared as long paths, the
    form a fetched list's path is read in, so either spelling matches."""
    with _folder_table_lock:
        if under is None:
            _folder_tables.clear()
            return
        prefix = os.path.normcase(platform_compat.long_path(
            os.path.abspath(str(under)))).rstrip("\\/") + os.sep
        for signature in list(_folder_tables):
            if any(os.path.normcase(platform_compat.long_path(
                    os.path.abspath(path))).startswith(prefix)
                   for path, _mtime, _size in signature):
                del _folder_tables[signature]


def _forget_folder_table(signature):
    with _folder_table_lock:
        _folder_tables.pop(signature, None)


def page_of_list_files(paths, offset, limit, source, max_rows=None):
    """page_folder_groups() of the whole of `paths`, from the folder table.

    The same (page, total_folders, total_rows, row_capped), parsing only the
    folders the page holds. None when the table cannot answer - a file that
    cannot be read, a lone carriage return, a file that changed under it, a
    list whose table would cost more than its budget - and the caller then
    reads the lists whole, as it always did.
    """
    paths = list(paths)
    table = _folder_table(paths)
    if table is None:
        return None
    total = len(table.group_first)
    start = 0 if offset < 0 else offset
    # The same slice page_folder_groups() takes, on group numbers.
    window = range(total)[start:start + limit] if limit else range(total)[start:]

    # Only the groups the page can hold are read. The valve stops at the
    # first group that would carry the page past max_rows, and reads that
    # one's rows only when it is the first (it is then cut, not dropped).
    wanted = []
    rows_so_far = 0
    for group in window:
        count = table.group_count[group]
        if max_rows and wanted and rows_so_far + count > max_rows:
            wanted.append((group, False))
            break
        wanted.append((group, True))
        rows_so_far += count

    handles = {}
    try:
        groups = []
        for group, read in wanted:
            entries = []
            if read:
                for run in table.runs(group):
                    # Its dropped rows' spans, walked along with the rows:
                    # both are in position order.
                    spans = table.dups(run)
                    span = 0
                    for position, (line_strip, folder) in enumerate(
                            _run_lines(table, run, handles)):
                        while span < len(spans) and position >= spans[span + 1]:
                            span += 2
                        if span < len(spans) and position >= spans[span]:
                            continue
                        filename, size = _split_entry_line(line_strip)
                        entries.append({"folder": folder, "filename": filename,
                                        "size": size})
            rows = list(_filelist_rows(entries, source))
            if read and len(rows) != table.group_count[group]:
                raise _StaleFolderTable()
            groups.append({"folder": rows[0]["folder"] if rows else "",
                           "count": table.group_count[group], "entries": rows})
    except (_StaleFolderTable, _LoneCarriageReturn, OSError):
        _forget_folder_table(table.signature)
        return None
    finally:
        for handle in handles.values():
            handle.close()

    page, _folders, _rows, row_capped = page_folder_groups(
        groups, 0, len(groups), max_rows=max_rows)
    return page, total, table.total_rows, row_capped


def rebuild_pauses_everything():
    """PAUSE_FOR_WHOLE_UPDATE's old behaviour: a rebuild pauses searching and
    sharing from its start to its end."""
    return (getattr(config, 'PAUSE_ON_UPDATE', True) is True
            and bool(getattr(config, 'PAUSE_FOR_WHOLE_UPDATE', False)))


# The background audio reading's own swap (#1182): building the archive again
# and moving the rewritten list and archive into place.
READING_SWAP_PHASES = ("packing", "publishing")


def list_archive_waits():
    """Whether the list archive must not be sent right now - "@nick", and the
    archive asked for by name (dcc.py), both ask this.

    Through a whole rebuild (#971): the archive is the file its swap
    replaces, and a slow send holding it open on Windows made the replace
    give up and the rebuild roll back. And through a background audio reading
    (#1182) in every phase but the reading itself - from its start, through
    the rewrite of its rows, the packing and the swap - so a send cannot
    start in the stretch that ends in its swap (#1182 audit). The reading,
    which can last hours, leaves the archive free; a send still running when
    it reaches the swap is waited for (update_list's READING_SWAP_WAIT)."""
    if getattr(config, 'update_inprogress', False) is True:
        return True
    if runtime.audio_reading is None:
        return False
    import update_list
    return update_list.read_phase() != update_list.READING_PHASE


def rebuild_pauses_requests():
    """Whether a search or a file request must wait for the rebuild right now.

    Only while the new list is being swapped in (#923). The rebuild builds
    under temporary names, so through the scan, the audio-info reading and
    the writing the published list is complete and exactly what users have -
    answering from it is answering from the list they are looking at. What
    the rebuild is doing comes from its progress file, since it is another
    process; a phase that cannot be read is treated as the swap, so a rebuild
    that cannot report (a read-only data/) pauses the way it always did."""
    if getattr(config, 'PAUSE_ON_UPDATE', True) is not True:
        return False
    import update_list
    if getattr(config, 'update_inprogress', False) is not True:
        # THE BACKGROUND AUDIO READING (#1182) swaps the list too, once, when
        # it writes the lengths in: only then - while it packs the archive and
        # swaps - and never for the reading itself, which can take hours. Not
        # "any phase but a few" as for a rebuild: its progress file is absent
        # while it starts and after it ends, and that is no reason to pause.
        if runtime.audio_reading is None:
            return False
        return update_list.read_phase() in READING_SWAP_PHASES
    if rebuild_pauses_everything():
        return True
    return update_list.read_phase() not in update_list.PHASES_BEFORE_THE_SWAP


# The shortest SEARCH_FOLDER_MAX_CHARS honoured (#1228). The cut keeps
# "..." and the end of the folder, so a cap of 3 or less would show nothing
# of it, and 0 would slice nothing off at all (text[-0:] is the whole text).
# Ten keeps at least the last seven characters - usually enough of an album
# name to tell two apart.
SEARCH_FOLDER_MIN_CHARS = 10


def search_folder_text(folder, max_chars):
    """The folder a search reply's From: line shows (#1228), or "" for none.

    The list's own heading, as find_matching_entries() returns it - the
    "D:\\MEDIA\\..." prefix included, never a path on the operator's disk -
    without the trailing backslash every heading is written with, so the line
    ends on the folder's own name. Past `max_chars` it is cut from the LEFT
    and marked with "...": the end of a folder (the album) is what tells two
    results apart, and the start is the prefix every heading shares.
    """
    text = str(folder or "").strip().rstrip("\\")
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 80
    limit = max(limit, SEARCH_FOLDER_MIN_CHARS)
    if len(text) <= limit:
        return text
    return "..." + text[len(text) - (limit - 3):]


def group_search_entries_by_folder(entries):
    """The search results with each folder's files together, folders sorted (#1228).

    A stable sort, so the files of one folder keep the list's own order.
    Results with no folder (rows above the first heading) come FIRST: placed
    after a From: line they would read as part of that folder.
    """
    def key(entry):
        folder = entry.get("folder") or ""
        return (bool(folder), folder.casefold(), folder)
    return sorted(entries, key=key)


def execute_search(irc_sock, user, search_term, channel):
    """Search the list file, sending the matching rows exactly as they are stored."""
    # Off (#1237): no reply at all, the same silence as a channel that is not
    # served. "is False" so only a real off turns searching off.
    if getattr(config, 'SEARCH_ENABLED', True) is False:
        print(f'[SEARCH] Ignored a search from {user} in {channel}: searching is off (SEARCH_ENABLED).')
        return
    # update_inprogress, not search_inprogress (#214) - see dcc.py's own comment
    # on the same change. This branch is the REBUILD case and its message says
    # so; the branch below is the concurrent-search case and needs its own.
    if rebuild_pauses_requests():
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: Search engine is temporarily paused during MasterList rebuild. Please wait a moment.\r\n")
        print(f"[MAINTENANCE BLOCK] Refused a search (@find) from {user}: an !update is running.")
        return

    if len(search_term) < 3:
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}Error{config.C_RESET}: Search term must be at least 3 characters long.\r\n")
        return

    # WHICH LIST THIS CHANNEL SEARCHES (#26). None means no list is bound
    # here and the primary is not the catch-all - #26's "a channel with no list
    # bound gets nothing", answered with silence rather than an error, because
    # an error implies something went wrong and nothing did.
    import library
    wanted = library.list_name_for_request(channel)
    if wanted is None:
        print(f"[SEARCH] No list is bound to {channel!r}; ignoring the search "
              f"from {user}.")
        return

    # Guard against two searches at once. This used to print to the console and
    # return, sending the user nothing at all - their @find simply vanished.
    #
    # It was also unreachable with PAUSE_ON_UPDATE on, because the branch above
    # returned first on the very same flag. Now that the two flags mean
    # different things, this is the branch a second searcher actually reaches,
    # so it has to say something, and something accurate: the previous wording
    # anywhere near here blamed a MasterList rebuild that is not happening.
    #
    # CHECKED AND SET AS ONE STEP (#607). Every @find runs on its own thread,
    # and this guard used to read the flag at the top of the function and set
    # it here, with list_name_for_request()'s trip to lists.json in between -
    # so two @find lines dispatched from the same recv() buffer both passed
    # the check, both walked the master list at once, and the first to finish
    # cleared the flag while the other was still running, admitting a third.
    # The gate is the one !update takes for the same flag, so a rebuild and a
    # search cannot slip past each other either. The refused searcher returns
    # BEFORE the try below, so its finally never clears a flag it did not set.
    with runtime.list_update_gate:
        if getattr(config, 'search_inprogress', False):
            oserve = sys.modules.get('oserve')
            if oserve:
                oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: Another search is running right now - try again in a moment.\r\n")
            print(f"[SEARCH BLOCK] Ignored a search from {user}: another scan is already running.")
            return
        config.search_inprogress = True

    try:
        current_list_path = find_latest_list(wanted)
        if not current_list_path or not os.path.exists(current_list_path):
            oserve = sys.modules.get('oserve')
            if oserve:
                oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}Error{config.C_RESET}: No MasterList found.\r\n")
            return

        print(f"[NEW SEARCH] {user} in {channel} searched for '{search_term}'")
        
        # Strip mIRC colour codes and control characters from the search terms
        raw_clean = strip_control_codes(search_term)
        
        # Split the search terms - words, and "quoted phrases" (#774)
        search_words = split_search_term(raw_clean)
        
        # ---------------------------------------------------------------------
        # Straight copy: no reformatting, the file row is sent raw
        # ---------------------------------------------------------------------
        # find_matching_entries() treats an empty search_words as "match
        # everything" (build_filelists_payload() wants that). execute_search()
        # never has - a search term that stripped down to zero words (e.g.
        # "---") has always matched nothing - so that historical behaviour is
        # preserved explicitly here rather than inside the shared function.
        max_results = getattr(config, 'MAX_SEARCH_RESULTS', 5)
        if search_words:
            found_entries, total_matches = find_matching_entries(
                search_words, limit=max_results, name=wanted)
        else:
            found_entries, total_matches = [], 0
        # SEARCH_SHOW_FOLDER (#1228): each folder's files together, so its
        # From: line is sent once above them rather than once per result.
        # Sorted AFTER the cap, so MAX_SEARCH_RESULTS still counts files and
        # the same files are sent either way; off, nothing is reordered.
        show_folder = getattr(config, 'SEARCH_SHOW_FOLDER', False) is True
        if show_folder:
            found_entries = group_search_entries_by_folder(found_entries)
        # The row is kept exactly as it is on disk - matches go to IRC raw.
        matches = [entry["line"] for entry in found_entries]

        # The console feed (#528): one line per served search, hits or none.
        # total_matches, not len(matches): the reply is capped at
        # MAX_SEARCH_RESULTS, and the operator wants to know what the list
        # had, not what the cap let through.
        announce.feed_event(
            "SEARCH",
            f'{user} searched "{search_term}" - {total_matches} result'
            f'{"" if total_matches == 1 else "s"}',
            nick=user, channel=channel, results=total_matches, term=search_term)

        if matches:
            # Send the search header privately to the requester
            announce.send_search_result_header(user, search_term, total_matches, channel)
            
            oserve = sys.modules.get('oserve')
            if oserve:
                BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()

                # The From: line (#1228), framed like the header above it so it
                # follows the theme. "From: " comes BEFORE the frame, not
                # after it (#1249 review): CUSTOM_THEME_BORDER/SEPARATOR/
                # TEXTBOX are free-text settings only the operator can set,
                # and framing first meant a border set to e.g. "!othernick"
                # put that text, unescaped, at the very start of the line -
                # a real request to another bot, pasted into a channel.
                # "From: " first means the line always starts with that
                # literal text, under any theme or custom colour, matching
                # the guarantee this feature's own changelog entry promised.
                def _build_folder(shown_folder):
                    return (f"PRIVMSG {user} :From: {BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} "
                            f"{V}{shown_folder} {BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n")

                folder_cap = getattr(config, 'SEARCH_FOLDER_MAX_CHARS', 80)
                previous_folder = None

                for entry, match in zip(found_entries, matches):
                    folder = entry.get("folder")
                    if show_folder and folder and folder != previous_folder:
                        previous_folder = folder
                        shown_folder = search_folder_text(folder, folder_cap)
                        if shown_folder:
                            oserve.queue_message(user, announce.fit_irc_line(
                                _build_folder, shown_folder))
                    # Through fit_irc_line, like the header of this very reply
                    # two lines above. These rows were the only user-visible
                    # lines in the module that skipped it, so a long filename
                    # was cut by the server instead - and the cut discards the
                    # trailing reset, smearing colour down the client's window.
                    # #162 finding #31.
                    def _build(shown_match):
                        block_match = (f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} "
                                       f"{shown_match}{R} {BG_CYAN_BLOCK} {BG_RED_BLOCK} ")
                        return f"PRIVMSG {user} :{block_match}\r\n"

                    line = announce.fit_irc_line(_build, match)
                    # A row the budget would cut loses its audio tail
                    # (#567) before a single letter of its name: the name
                    # is what the reader pastes back to ask for the file.
                    if line != _build(match) and without_audio_info(match) != match:
                        line = announce.fit_irc_line(_build, without_audio_info(match))
                    oserve.queue_message(user, line)
        else:
            print(f"[SEARCH RESULT] 0 Match(es) found for {user} in {channel} on '{search_term}'")
                
    except Exception as e:
        print(f"[SEARCH CRITICAL ERROR] The search crashed while scanning the file: {e}")
        
    finally:
        # Release the search lock so the next user can search immediately
        config.search_inprogress = False
        print(f"[SEARCH-FINISHED] The search for {user} finished and the lock was released cleanly.")

def send_list_trigger_info(irc_sock, user):
    msg = f"List trigger(s): {theme.palette()['alert']}@{config.NICKNAME}{config.C_RESET} {config.SCRIPT_VERSION}{config.C_RESET}\r\n"
    oserve.queue_message(user, f"NOTICE {user} :{msg}")

def send_file_list(irc_sock, user, channel):
    """Find the existing .zip list and start a DCC SEND, tracking the right channel."""
    # If a list update is running, answer with the status rather than an
    # error - or the audio reading is swapping the list in (#1182).
    if list_archive_waits():
        msg = f"NOTICE {user} :{config.C_BOLD}System Notice{config.C_RESET}: Master list is currently rebuilding. Please wait a few minutes and try again. \r\n"
        oserve.queue_message(user, msg)
        return

    # WHICH LIST THIS CHANNEL GETS (#26). None means no list is bound here and
    # the primary is not the catch-all, which is #26's "a channel with no list
    # bound gets nothing" - answered with silence rather than an error, because
    # an error implies something went wrong and nothing did: this bot simply
    # does not serve here.
    import library
    wanted = library.list_name_for_request(channel)
    if wanted is None:
        print(f"[LIST] No list is bound to {channel!r}; ignoring the request "
              f"from {user}.")
        return
    # Asked for by private message from somebody in none of our channels
    # (#1242): handle_download_request() below refuses it, so it is refused
    # here, before "Preparing full list" promises a list that never comes.
    if not dcc.is_channel_name(channel) and library.shared_channel(user) is None:
        dcc.refuse_unshared_private_request(user, "the list")
        return

    current_zip_path = find_latest_list_file(wanted)
    
    if not current_zip_path or not os.path.exists(current_zip_path):
        oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}Error{config.C_RESET}: List file missing. {config.C_BOLD}{config.SCRIPT_VERSION}{config.C_RESET} \r\n")
        return
        
    zip_filename = os.path.basename(current_zip_path)
    # queue_message payloads go straight to the socket, so they must be complete IRC
    # commands. This one was a bare text line, so the server answered
    # "421 Preparing :Unknown command", the user never saw the notice, and a flood-queue
    # slot was spent on an error. The other NOTICE payloads in this module were already
    # correctly formed; the search results use PRIVMSG, which is equally valid.
    msg = f"NOTICE {user} :Preparing full list ({zip_filename}) for {user}... {config.C_BOLD}{config.SCRIPT_VERSION}{config.C_RESET} \r\n"
    oserve.queue_message(user, msg)
    
    # 'channel' is handed to the DCC engine now, instead of 'user'
    dcc.handle_download_request(irc_sock, user, zip_filename, channel)


