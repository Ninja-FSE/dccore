# list.py - Slimmed down; scanning lives in update_list.py
import os
import time
import datetime
import defaults as config
import library
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

    Stable across restarts and across reordering the lists: it depends on that
    one name and nothing else. hashlib rather than hash(), which is randomised
    per process and would rename every directory on each start.
    """
    raw = str(name or "").strip()
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_ ") else "_" for ch in raw)
    cleaned = cleaned.strip(" ")
    if cleaned == raw and cleaned:
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

def get_file_count_date_size_and_raw_bytes(name=None):
    """The EXACT number of music files, counting only lines that start with the trigger.

    `name` picks a list; without one this is the primary, which on a
    single-list install is the only one there is.
    """
    latest_list = find_latest_list(name)
    if not latest_list or not os.path.exists(latest_list):
        return 0, "No List", "0B", 0
        
    try:
        count = 0
        # EVERY list, not just the master. Film and series moved into their
        # own file, and counting one of two would advertise a number smaller
        # than the library the bot actually serves - and smaller than what a
        # user sees when they open the archive. The date below still comes
        # from the master list, which is the one always present.
        # Count only real request lines, skipping the "===" folder separators,
        # blank lines and text headers.
        #
        # This matches on "!" alone rather than on f"!{config.NICKNAME} ". The list
        # is written with whatever nick was current at generation time, so after a
        # 433 fallback the old test matched nothing and the advert reported 0 files
        # even though the list was fine. Every request line in the generated file
        # starts with "!" (update_list.py:156) and no header or separator does, so
        # this is the same filter execute_search already applies to the same file.
        for one_list in all_list_paths(name):
            # #433: each path gets its OWN try, the same as the two side-file
            # reads a few lines down and for the identical reason - this used
            # to sit inside the outer try below, so one unreadable list (an
            # AV scanner or backup agent holding the VIDEO list open with no
            # sharing, on Windows; a permission change or a vanished path on
            # either platform) collapsed the WHOLE tuple to (0, "Error",
            # "0B", 0), throwing away a master-list count and date that were
            # perfectly fine.
            try:
                with open(one_list, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("!"):
                            count += 1
            except OSError as list_err:
                print(f"[LIST] Could not read {one_list}: {list_err}")
            
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


_INFO_MARKER_RE = re.compile(r'\s*::INFO::\s*', re.IGNORECASE)

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
# ANYWHERE (the way the marker split above safely can) would risk cutting a
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
    parts = _INFO_MARKER_RE.split(rest, maxsplit=1)
    if len(parts) == 2:
        filename, size = parts
    else:
        dash_match = _DASH_SIZE_SUFFIX_RE.search(rest)
        if dash_match:
            filename, size = rest[:dash_match.start()], dash_match.group(1)
        else:
            filename, size = rest, ""
    return filename.strip(), size.strip()


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

    current_list_path = list_path
    if not current_list_path or not os.path.exists(current_list_path):
        return entries, total_matches

    current_folder = None
    # "none" -> saw the opening rule line, now expecting the folder line ("open")
    # -> saw the folder line, now expecting the closing rule line ("folder_seen")
    state = "none"

    with open(current_list_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_strip = line.replace('\x00', '').strip()
            if not line_strip:
                continue

            is_rule = set(line_strip) == {"="}
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
                    continue
            elif state == "folder_seen":
                state = "none"
                if is_rule:
                    continue  # the expected closing rule

            if not line_strip.startswith("!"):
                continue

            line_lower = line_strip.lower()
            if search_words and not all(word in line_lower for word in search_words):
                continue

            total_matches += 1
            if limit is None or len(entries) < limit:
                filename, size = _split_entry_line(line_strip)
                entries.append({
                    "line": line_strip,
                    "folder": current_folder,
                    "filename": filename,
                    "size": size,
                })

    return entries, total_matches


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


def entries_to_filelist_rows(entries, source):
    """Shape find_matching_entries() output into the File Lists view's row
    format: {"title", "size", "format", "source"}, deduping same
    filename+size the way webserver.build_filelists_payload() always has.

    Shared by webserver.py (this bot's own list, source=config.NICKNAME) and
    list_fetch.py (another bot's fetched-and-extracted list, source=that
    bot's nick) so the two surfaces can never drift in what a "row" looks
    like on the dashboard.
    """
    seen = set()
    rows = []
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
        rows.append({
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
            "rar_folder": rar_folder_of(filename),
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
        })
    return rows


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


def execute_search(irc_sock, user, search_term, channel):
    """Search the list file, sending the matching rows exactly as they are stored."""
    # update_inprogress, not search_inprogress (#214) - see dcc.py's own comment
    # on the same change. This branch is the REBUILD case and its message says
    # so; the branch below is the concurrent-search case and needs its own.
    if getattr(config, 'PAUSE_ON_UPDATE', True) is True and getattr(config, 'update_inprogress', False) is True:
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: Search engine is temporarily paused during MasterList rebuild. Please wait a moment.\r\n")
        print(f"[MAINTENANCE BLOCK] Refused a search (@find) from {user}: an !update is running.")
        return

    # Guard against two searches at once. This used to print to the console and
    # return, sending the user nothing at all - their @find simply vanished.
    #
    # It was also unreachable with PAUSE_ON_UPDATE on, because the branch above
    # returned first on the very same flag. Now that the two flags mean
    # different things, this is the branch a second searcher actually reaches,
    # so it has to say something, and something accurate: the previous wording
    # anywhere near here blamed a MasterList rebuild that is not happening.
    if getattr(config, 'search_inprogress', False):
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: Another search is running right now - try again in a moment.\r\n")
        print(f"[SEARCH BLOCK] Ignored a search from {user}: another scan is already running.")
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
        
        # Split the search terms
        clean_term = re.sub(r'[-*_.]', ' ', raw_clean)
        search_words = [w.strip().lower() for w in clean_term.split() if w.strip()]
        
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
        # The row is kept exactly as it is on disk - matches go to IRC raw.
        matches = [entry["line"] for entry in found_entries]

        if matches:
            # Send the search header privately to the requester
            announce.send_search_result_header(user, search_term, total_matches, channel)
            
            oserve = sys.modules.get('oserve')
            if oserve:
                BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
                
                for match in matches:
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

                    oserve.queue_message(user, announce.fit_irc_line(_build, match))
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
    # If a list update is running, answer with the status rather than an error
    if getattr(config, 'update_inprogress', False) is True:
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


