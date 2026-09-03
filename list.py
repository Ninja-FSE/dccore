# list.py - Slimmed down; scanning lives in update_list.py
import os
import time
import datetime
import defaults as config
import oserve
import dcc
import announce
import theme



# Resolved per call rather than once at import. LOCAL_LIST_DIR is a config value,
# and !rehash reloads config - a path baked in at import time would keep pointing
# at the old directory for the life of the process after the operator moved it.
def size_file_path():
    return os.path.join(config.LOCAL_LIST_DIR, config.LIST_SIZE_FILE)


def rawbytes_file_path():
    return os.path.join(config.LOCAL_LIST_DIR, config.LIST_RAWBYTES_FILE)

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


def is_list_artifact(filename, fmt):
    """True if `filename` is a delivered master list in `fmt`.

    Matched on the name the builder actually writes, not on the extension
    alone: people share .zip and .rar files out of their library too, and a
    file called "Someone - DCCore Sessions.rar" must not be looked for in the
    lists directory instead of the music directory.
    """
    name = os.path.basename(str(filename))
    if fmt == "txt":
        return (name.startswith(config.LIST_BASE_NAME + FULL_LIST_MARKER)
                and name.endswith(".txt"))
    return name.startswith(config.LIST_BASE_NAME + "-") and name.endswith("." + fmt)


def is_list_artifact_name(filename):
    """True if this names a delivered master list in any of the three formats."""
    return any(is_list_artifact(filename, fmt) for fmt in LIST_FORMATS)


def find_latest_list_file():
    """The artifact to send when somebody types the bot's nickname.

    The configured format first. If it has not been built yet - the operator
    changed LIST_FORMAT and the next rebuild has not run - this falls back to
    another format rather than answering "list missing". Handing somebody a
    .zip when the setting now says .rar is a far smaller thing than the bot
    having no list at all until the weekly update comes round, which is the
    failure the atomic-publish rewrite exists to prevent.
    """
    if not os.path.exists(config.LOCAL_LIST_DIR):
        return None
    try:
        entries = os.listdir(config.LOCAL_LIST_DIR)
    except OSError as err:
        print(f"[LIST ERROR] Could not read {config.LOCAL_LIST_DIR}: {err}")
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
        return os.path.join(config.LOCAL_LIST_DIR, files[0])
    return None

def get_file_count_date_size_and_raw_bytes():
    """The EXACT number of music files, counting only lines that start with the trigger."""
    latest_list = find_latest_list()
    if not latest_list or not os.path.exists(latest_list):
        return 0, "No List", "0B", 0
        
    try:
        count = 0
        with open(latest_list, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_strip = line.strip()
                # Count only real request lines, skipping the "===" folder separators,
                # blank lines and text headers.
                #
                # This matches on "!" alone rather than on f"!{config.NICKNAME} ". The list
                # is written with whatever nick was current at generation time, so after a
                # 433 fallback the old test matched nothing and the advert reported 0 files
                # even though the list was fine. Every request line in the generated file
                # starts with "!" (update_list.py:156) and no header or separator does, so
                # this is the same filter execute_search already applies to the same file.
                if line_strip.startswith("!"):
                    count += 1
            
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
            size_path = size_file_path()
            if os.path.exists(size_path):
                with open(size_path, "r", encoding="utf-8") as sf:
                    size_str = sf.read().strip() or "0B"
        except OSError as size_err:
            print(f"[LIST] Could not read {config.LIST_SIZE_FILE}: {size_err}")

        raw_bytes = 0
        try:
            raw_path = rawbytes_file_path()
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


def find_latest_list():
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
        pattern = os.path.join(glob.escape(config.LOCAL_LIST_DIR),
                               f"{glob.escape(config.LIST_BASE_NAME)}-*.txt")
        all_txt_files = sorted(glob.glob(pattern))
        # Keep the RAR list out of the search, so only the master list is scanned.
        # FULL_LIST_MARKER keeps the DELIVERED text list out too: that one is a
        # copy of this file with the album rows appended, and which of the two
        # sorts last is an accident of punctuation. If it ever won, @find would
        # offer album rows as though they were tracks and the advert would count
        # the albums as files.
        true_master_lists = [f for f in all_txt_files
                             if "-RAR-" not in f and FULL_LIST_MARKER not in f]
        if true_master_lists:
            return true_master_lists[-1]
    except Exception as e:
        print(f"[SEARCH ERROR] Could not find the latest list: {e}")
    return None

_INFO_MARKER_RE = re.compile(r'\s*::INFO::\s*', re.IGNORECASE)


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
    own. Best-effort on purpose: a line that does not carry the marker at
    all returns the whole thing as the filename with an empty second value,
    rather than raising. Shared by `_split_entry_line()` below (this bot's
    own master list) and irc.py's cross-bot broadcast-search capture, which
    extracts the same shape out of another bot's reply and must not mistake
    any of the trailing tag for part of the filename when it later requests
    that exact name back with `!<nick> <filename>`.
    """
    parts = _INFO_MARKER_RE.split(rest, maxsplit=1)
    if len(parts) == 2:
        filename, size = parts
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


def find_matching_entries(search_words, limit=None, list_path=None):
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
    entries = []
    total_matches = 0

    current_list_path = list_path if list_path is not None else find_latest_list()
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
# raw_folder_str) because the OmenServe listing format has always looked that
# way. It is a piece of the format, NOT a path - the operator's library may
# well be at Z:\Music or /srv/library. What follows it is the folder
# relative to FILE_DIRECTORY, which is what dcc.py joins to resolve a request.
LIST_FOLDER_PREFIX = "D:\\MUSIC\\"

# A leading drive specifier on one path component: "C:", "C:Windows".
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def resolve_list_folder(header, base=None):
    """Turn a master-list folder heading into a real path on this machine.

    Mirrors what dcc.handle_download_request() does when it resolves a
    requested name: strip the format's fixed prefix, and join what remains to
    FILE_DIRECTORY. An entry that carried no heading at all resolves to the
    library root, which is where such a file actually sits.

    This exists so the operator is shown a path they can act on. The raw
    heading names a drive most installs do not have, which is worse than
    useless in a tool whose whole job is "go and look at these folders".
    """
    base = getattr(config, "FILE_DIRECTORY", "") if base is None else base
    text = (header or "").strip()
    if text.upper().startswith(LIST_FOLDER_PREFIX):
        text = text[len(LIST_FOLDER_PREFIX):]
    # Headings are written with backslashes regardless of the host, so split on
    # both and let os.path.join put the platform's own separator back.
    parts = [part for part in text.replace("\\", "/").split("/") if part]

    # A drive specifier has to come off each part before joining. On Windows,
    # os.path.join() treats an argument like "C:" or "C:Windows" as
    # drive-relative and DISCARDS everything before it - so a heading naming
    # any drive other than the one the prefix strips returns a path with no
    # relation to `base` at all, which is the one thing this promises not to do.
    #
    # Reachable input since #121: the `!rar` path passes a folder the user
    # typed in the channel through here. is_safe_path() still refuses the
    # result, but a joiner that can silently drop its own base is the wrong
    # thing to be relying on a downstream guard for.
    parts = [_DRIVE_PREFIX_RE.sub("", part, count=1) for part in parts]
    parts = [part for part in parts if part]

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

    Returns (page, total_folders, total_rows).

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
    """
    total_folders = len(groups)
    total_rows = sum(group["count"] for group in groups)

    if offset < 0:
        offset = 0
    window = groups[offset:offset + limit] if limit else groups[offset:]

    if not max_rows:
        return window, total_folders, total_rows

    page = []
    rows_so_far = 0
    for group in window:
        if page and rows_so_far + group["count"] > max_rows:
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
            break
        page.append(group)
        rows_so_far += group["count"]
    return page, total_folders, total_rows


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

    config.search_inprogress = True
    
    try:
        current_list_path = find_latest_list()
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
            found_entries, total_matches = find_matching_entries(search_words, limit=max_results)
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

    current_zip_path = find_latest_list_file()
    
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


