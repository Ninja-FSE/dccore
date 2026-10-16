"""The dashboard's view of this bot's own library and list (#1318): the
library summary, the search over the published list, and the paged list
itself, with the album counts they share.

Split out of webserver.py, step 4 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back.
"""

import os

import defaults as config
from web_common import (
    FILELISTS_DEFAULT_PAGE_SIZE,
    WEBUI_MAX_SEARCH_RESULTS,
    split_list_search_words,
)


# ==========================================================================
# Pure data-building functions - no Flask, unit tested directly.
# ==========================================================================

def count_rar_album_folders(name=None):
    """How many album folders the RAR list offers, or None if there is no list.

    The file update_list.py writes opens with three lines of explanation and
    then one "!<nick> !rar <path>" row per folder, so the rows are exactly the
    lines starting with "!". Counting those rather than the file's length is
    what keeps the header out of the total.

    None rather than 0 when there is no list at all: a bot whose first list has
    not been built yet has an unknown album count, and zero is a different
    claim - it would read on the page as "this bot offers no albums".

    `name` picks a served list (#952): a list's files live in its own
    directory (list.list_dir()), and without a name this reads the primary's,
    which is where every install's RAR list was before there were several.
    """
    import io

    if name is None:
        directory = getattr(config, "LOCAL_LIST_DIR", "./lists")
    else:
        import list as list_mod
        directory = list_mod.list_dir(name)
    prefix = f"{getattr(config, 'LIST_BASE_NAME', 'DCCore')}-RAR-"
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(prefix) and name.endswith(".txt"))
    except OSError:
        return None
    if not names:
        return None

    path = os.path.abspath(os.path.join(directory, names[-1]))
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size, st.st_ino)
        cached = _rar_counts.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        with io.open(path, encoding="utf-8", errors="ignore") as handle:
            count = sum(1 for line in handle if line.startswith("!"))
    except OSError:
        return None
    _rar_counts[path] = (key, count)
    return count


# The RAR list's row count by its path (#1123): it changes only when the list is
# rebuilt, and the Stats page counted every row of it on every poll - a quarter
# of a second at 420k rows, per served list. Keyed per path, one entry each, so
# a bot serving several lists keeps them all rather than recounting whichever
# was not asked last. A rebuild publishes with os.replace, which changes the
# key. webserver.py is not reloaded by !rehash.
_rar_counts = {}


def _format_library_size(raw_bytes):
    """A byte total the way a list's own size file writes it - two decimals,
    "1.85TB" - so a total summed from several lists reads like the figures it
    is made of and not in a second style (#952)."""
    size = float(raw_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}PB"


def build_library_payload():
    """The Library block of the Stats page: totals across EVERY served list,
    and one row per list (#952).

    It used to read the primary list alone - no name given, so the primary's
    files, its side files for the size, and LOCAL_LIST_DIR's RAR list - and on
    a bot serving music and film lists the page showed the music list's
    numbers as though they were the library. The channel adverts already ask
    per list, so the page and the advert disagreed.

    The top-level keys are the ones the page has always had (files, size,
    raw_bytes, list_date, rar_folders) and are now the totals; a single-list
    install therefore reads exactly as before. `lists` holds each list's own
    figures, in the operator's order.

    Every list is read in its own guard, the way every source in this module
    is: one list whose side file is unreadable costs its own row and nothing
    else. The primary is asked for with NO name, not its own - that is the
    path the page has always taken, and a single-list install has no reason to
    be routed differently.

    A folder that belongs to two lists is counted in both, and the totals do
    not de-duplicate; the rows are there so that can be seen.
    """
    import library
    import list as list_mod

    try:
        entries = [(str(entry.name), bool(entry.primary)) for entry in library.lists()]
    except Exception:
        entries = []
    if not entries:
        entries = [("", True)]

    rows = []
    newest_row, newest_when = None, None
    for name, primary in entries:
        row = {"name": name, "primary": primary, "files": 0, "size": None,
               "raw_bytes": 0, "list_date": None, "rar_folders": None}
        asked = None if primary else name
        try:
            files, list_date, size, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes(asked)
            row.update({"files": int(files or 0), "list_date": list_date or None,
                        "size": size or None, "raw_bytes": int(raw_bytes or 0)})
        except Exception:
            pass
        try:
            row["rar_folders"] = count_rar_album_folders(asked)
        except Exception:
            pass
        try:
            latest = list_mod.find_latest_list(asked)
            when = os.path.getmtime(latest) if latest else None
        except Exception:
            when = None
        if when is not None and (newest_when is None or when > newest_when):
            newest_row, newest_when = row, when
        rows.append(row)

    primary_row = next((row for row in rows if row["primary"]), rows[0])
    total_bytes = sum(row["raw_bytes"] for row in rows)
    albums = [row["rar_folders"] for row in rows if row["rar_folders"] is not None]

    if len(rows) == 1:
        size = primary_row["size"]
    else:
        size = _format_library_size(total_bytes) if total_bytes else primary_row["size"]

    return {
        "files": sum(row["files"] for row in rows),
        "size": size,
        "raw_bytes": total_bytes,
        # The newest build among the lists; when none has been built at all,
        # what the primary says (a bot with no list yet says so).
        "list_date": (newest_row or primary_row)["list_date"],
        # None only when NO list has a RAR list: a total of the ones that do
        # is a true count of the albums on offer, while a list with no RAR list
        # simply has none to add.
        "rar_folders": sum(albums) if albums else None,
        "lists": rows,
    }


def build_search_payload(query):
    """The Search view's data: up to WEBUI_MAX_SEARCH_RESULTS matches for `query`.

    Delegates the actual scan to list.find_matching_entries() - the same
    IRC-agnostic function execute_search() now calls - so a web search and an
    IRC @find agree on what matches. This just applies a browser-sized limit
    and its own JSON shape instead of IRC formatting.
    """
    import list as list_mod

    search_words = split_list_search_words(query)
    if not search_words:
        return []

    entries, _total = list_mod.find_matching_entries(search_words, limit=WEBUI_MAX_SEARCH_RESULTS)

    # The data model has no real per-file "which channel is this in" - a file is
    # just a line in the one master list, shared by every channel the bot joins
    # (config.CHANNEL). So "channel" here means "everywhere this bot is
    # present", not "this specific file was seen in this specific channel".
    channel_str = ", ".join(part.strip() for part in str(getattr(config, "CHANNEL", "")).split(","))

    return [
        {
            "title": entry.get("filename", "?"),
            "path": entry.get("folder") or "",
            "size": entry.get("size", ""),
            "channel": channel_str,
        }
        for entry in entries
    ]


def build_filelists_payload(offset=0, limit=None, name=None, q=""):
    """The File Lists view's data: a page of one of THIS bot's own lists.

    `name` picks which. None means the primary, which is what every list
    function already resolves it to and what this route meant before lists
    had names - so an unqualified request is unchanged.

    `q`, when given, narrows the list to rows matching every word in it -
    #399's follow-up, the List Browser's own per-list search, asked for
    because a fetched bot's archive can run to tens of thousands of rows and
    paging through them by hand to find one file is not a real option.
    split_list_search_words("") is [], and find_matching_entries() already
    treats an empty word list as "match everything" - the same rule that
    makes an unfiltered page unchanged, so this needs no branch of its own.

    v1 scope is deliberately THIS BOT ONLY - it serves DCCore's own master
    list, decomposed into rows, with "source" hardcoded to config.NICKNAME.
    Real cross-bot deduplication (parsing other bots' adverts in the channel)
    is out of scope; this dedup is the trivial single-source case, collapsing
    only the same filename listed under two different folders.

    Issue #76, option 3: what gets returned is a page, `[offset:offset+limit]`
    in folders, plus the `total` counts, never the whole list. Unfiltered, the
    page comes from list.page_of_list_files() (#1128): a table of where each
    folder is, built once per version of the lists, so a page parses only its
    own folders - it used to parse the ENTIRE list for every page, 31 s and
    2.1 GB at two million rows. A `q` search, and any list the table cannot
    answer for, still parses the whole list as before. `limit=None` (the route's
    default when `?limit=` was omitted) means FILELISTS_DEFAULT_PAGE_SIZE, not
    "unlimited" - a caller that genuinely wants no cap must pass a `limit` up
    to FILELISTS_MAX_PAGE_SIZE explicitly.

    Returns {"entries": [...], "total": N, "offset": offset, "limit": limit} -
    the shape both filelists routes now return, so the frontend's paging code
    is shared between "our own list" and "a fetched bot's list".
    """
    import list as list_mod

    if limit is None:
        limit = FILELISTS_DEFAULT_PAGE_SIZE

    search_words = split_list_search_words(q)
    source = getattr(config, "NICKNAME", "?")
    answer = None
    if not search_words:
        answer = list_mod.page_of_list_files(
            list_mod.all_list_paths(name), offset, limit, source,
            max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS)
    if answer is None:
        entries, _total = list_mod.find_matching_entries(search_words, limit=None, name=name)
        rows = list_mod.entries_to_filelist_rows(entries, source)
        groups = list_mod.group_rows_by_folder(rows)
        answer = list_mod.page_folder_groups(
            groups, offset, limit, max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS)
    page, total_folders, total_rows, row_capped = answer
    return {
        "folders": page,
        "total": total_folders,
        "total_files": total_rows,
        "offset": offset,
        "limit": limit,
        # What the caller actually got. The row ceiling can end a page early,
        # so the frontend advances by this rather than by `limit` - otherwise
        # a truncated page would silently skip the folders it did not receive.
        "returned": len(page),
        # #477: distinguishes "this page is short because the safety valve
        # cut it" from "this page is short because it is the last one" - a
        # page of one huge folder otherwise reads as the pager being broken.
        "row_capped": row_capped,
    }
