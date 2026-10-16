"""The dashboard's served folders and lists (#1320): the folders this bot
serves from, the folder browser that picks them, and the lists bound to
channels.

Split out of webserver.py, step 5 of splitting it (docs/FUTURE.md); the
routes and the console's `served` and `folders` commands still call these
through webserver, which imports every name back.
"""

import os

import defaults as config
import platform_compat
from web_common import (
    FOLDER_BROWSE_MAX_ENTRIES,
    MAX_FOLDER_PATH_LEN,
    MAX_SERVED_FOLDERS,
)


# ---------------------------------------------------------------------
# SERVED FOLDERS
#
# A list of {label, path} pairs, which is why it is not on the Settings page
# with everything else: every other setting there is one scalar an operator
# types into one box, and settings_file.save() writes exactly that. This is
# ordered, validated as a SET (two folders can each be fine and still conflict
# with each other), and stored as JSON in its own file - so it gets its own
# endpoint pair rather than being bent into the settings shape.
#
# #164 step 4. Steps 1-3 shipped in v1.11.0 and made the daemon serve from a
# list of folders; until now the only way to WRITE that list was to create
# data/library_folders.json by hand, so the feature existed and no operator
# could reach it.
#
# `library` is imported inside each function, never at module scope: see
# tests/test_import_graph.py, which holds this module to a short list of
# imports that must work with no daemon running.
# ---------------------------------------------------------------------

def build_folders_payload():
    """GET /api/folders: the served folders, and where they came from.

    `source` is the honest answer to "what is this bot serving", which the
    folder list alone cannot give:

      "file"           - data/library_folders.json exists and is being used
      "file_directory" - no folder list; the single FILE_DIRECTORY is served
      "none"           - neither, so nothing is served at all

    An operator looking at one folder needs to know which of the first two
    they are in, because editing the list is what switches them.
    """
    import library

    stored = library.load_folders()
    single = str(getattr(config, "FILE_DIRECTORY", "") or "").strip()
    if stored:
        source = "file"
    elif single:
        source = "file_directory"
    else:
        source = "none"

    return {
        "folders": [{"name": entry.name, "path": entry.path}
                    for entry in library.folders()],
        "source": source,
        "file_directory": single,
        "path": library.folders_file(),
        # So the page can offer a Browse button, or say plainly why there
        # isn't one, instead of the operator wondering.
        "browser_enabled": bool(getattr(config, "WEBUI_FOLDER_BROWSER_ENABLED",
                                        False)),
    }


def browse_roots():
    """The top of the tree: drive letters on Windows, "/" everywhere else.

    Probed rather than assumed. A machine has the drives it has, and a letter
    that is mapped but disconnected raises rather than answering, which is why
    each one is tested individually and a failure just leaves it out.
    """
    if not platform_compat.IS_WINDOWS:
        return ["/"]

    found = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            if os.path.isdir(platform_compat.long_path(root)):
                found.append(root)
        except OSError:
            continue
    return found


def build_folder_browse_payload(raw_path):
    """GET /api/folders/browse: the immediate SUBDIRECTORIES of `raw_path`.

    DIRECTORIES ONLY, NEVER FILES. The caller is choosing a folder to serve;
    listing files would expose far more of the machine and answer nothing the
    picker asks. This is also why there are no sizes, no timestamps and no
    contents anywhere in the payload - a name and whether it can be opened is
    the whole of what a picker needs.

    An empty path means the top of the tree rather than the working directory,
    because "where does the operator start" has no sensible relative answer.

    Every entry is tested individually and a failure drops that entry rather
    than the listing: a directory holding one item the daemon may not stat -
    a system folder, a dead symlink, a disconnected network mount - must still
    list the other forty.
    """
    target = str(raw_path or "").strip()
    roots = browse_roots()

    if not target:
        return {"path": "", "parent": None, "at_root": True,
                "entries": [{"name": root, "path": root} for root in roots],
                "truncated": False}

    target = os.path.abspath(target)
    if not os.path.isdir(platform_compat.long_path(target)):
        return {"error": f"{target} is not a folder on this machine."}

    names = []
    try:
        with os.scandir(platform_compat.long_path(target)) as scan:
            for entry in scan:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError as err:
        return {"error": f"Could not read {target}: {err}"}

    names.sort(key=str.lower)
    truncated = len(names) > FOLDER_BROWSE_MAX_ENTRIES
    names = names[:FOLDER_BROWSE_MAX_ENTRIES]

    parent = os.path.dirname(target.rstrip(os.sep)) or None
    # At a drive root, dirname() answers the drive itself and the operator
    # would climb to where they already are. "" sends them to the root list.
    #
    # Compared with normcase, because browse_roots() builds its entries from
    # the uppercase letters A-Z while os.path.abspath() preserves whatever
    # case the caller sent. "c:\\" therefore missed the root list, and the
    # parent fell through to ntpath.dirname("c:") - which is "c:", a
    # DRIVE-RELATIVE path meaning "the current directory on C:". Clicking Up
    # from a lowercase drive root browsed the daemon's own working directory.
    # Found by audit.
    normalised_roots = {os.path.normcase(root) for root in roots}
    if parent == target or os.path.normcase(target) in normalised_roots:
        parent = ""

    return {
        "path": target,
        "parent": parent,
        "at_root": False,
        "entries": [{"name": name, "path": os.path.join(target, name)}
                    for name in names],
        "truncated": truncated,
    }


MAX_SERVED_LISTS = 16


def build_lists_payload():
    """GET /api/lists: every configured list, and whether one is implied.

    `source` answers "is this what I configured, or what the daemon fell back
    to", which the rows alone cannot: with no lists.json there is still
    exactly one list in this payload - the implicit one, over whatever
    FILE_DIRECTORY or the folder file resolved to - and an operator needs to
    know that editing it is what makes it real.
    """
    import library

    stored = library.load_lists()
    rows = []
    for entry in library.lists():
        rows.append({
            "name": entry.name,
            "primary": entry.primary,
            "channels": list(entry.channels),
            "modes": dict(getattr(entry, "modes", None) or {}),
            "folders": [{"name": f.name, "path": f.path} for f in entry.folders],
        })
        # Only when there is one (#1295), the way lists.json keeps `modes`:
        # a list without exclusions reads exactly as it did before.
        if getattr(entry, "exclude", None):
            rows[-1]["exclude"] = list(entry.exclude)
    return {
        "lists": rows,
        "source": "file" if stored else "implied",
        "max_lists": MAX_SERVED_LISTS,
    }


def apply_list_changes(payload):
    """POST /api/lists: validate the whole set, then write it.

    Returns (http_status, payload_dict).

    THE WHOLE SET, not each row, for the reason apply_folder_changes() gives
    and one more of its own: two lists can each be perfectly good and still be
    an invalid pair - the same channel bound to both, or the same name twice -
    and only the set can show that.

    AN EMPTY LIST IS ALLOWED and means "go back to one list over the
    configured folders". The file is REMOVED rather than written as [],
    because library.load_lists() already returns None for an empty file and
    falls back - so writing [] would leave a file on disk that does nothing.

    NO REHASH, same as folders: library.lists() re-reads on every call, so the
    running daemon picks this up immediately. It does NOT rebuild the
    published lists, and a list with no index is one nobody can request from -
    hence rebuild_required.
    """
    import library

    if not isinstance(payload, dict):
        return 400, {"error": "Expected an object with a 'lists' list."}
    rows = payload.get("lists")
    if not isinstance(rows, list):
        return 400, {"error": "'lists' must be a list."}
    if len(rows) > MAX_SERVED_LISTS:
        return 400, {"error": f"At most {MAX_SERVED_LISTS} lists."}

    # A row that does not carry 'exclude' keeps what the list of that name
    # has (#1295). The mIRC settings window, a dashboard tab opened before
    # the upgrade and the console's `served` transaction all send the whole
    # set without it; treated as "none", each of those saves would wipe every
    # list's exclusions without a word.
    stored_exclude = {entry.name.lower(): tuple(getattr(entry, "exclude", None) or ())
                      for entry in (library.load_lists() or [])}

    entries = []
    for row in rows:
        if not isinstance(row, dict):
            return 400, {"error": "Each list must be an object with a 'name'."}
        name = str(row.get("name", "") or "").strip()
        if len(name) > MAX_FOLDER_PATH_LEN:
            return 400, {"error": f"A list name is longer than "
                                  f"{MAX_FOLDER_PATH_LEN} characters."}
        raw_channels = row.get("channels")
        if raw_channels is None:
            raw_channels = []
        if not isinstance(raw_channels, list):
            return 400, {"error": f"{name or 'A list'}: 'channels' must be a list."}
        raw_modes = row.get("modes")
        if raw_modes is None:
            raw_modes = {}
        if not isinstance(raw_modes, dict):
            return 400, {"error": f"{name or 'A list'}: 'modes' must be an object "
                                  f"of channel -> mode."}
        if "exclude" in row:
            raw_exclude = row.get("exclude")
            if raw_exclude is None:
                raw_exclude = []
            if not isinstance(raw_exclude, (list, str)):
                return 400, {"error": f"{name or 'A list'}: 'exclude' must be a list "
                                      f"or a comma-separated text."}
        else:
            raw_exclude = stored_exclude.get(name.lower(), ())
        raw_folders = row.get("folders")
        if raw_folders is None:
            raw_folders = []
        if not isinstance(raw_folders, list):
            return 400, {"error": f"{name or 'A list'}: 'folders' must be a list."}
        if len(raw_folders) > MAX_SERVED_FOLDERS:
            return 400, {"error": f"{name or 'A list'}: at most "
                                  f"{MAX_SERVED_FOLDERS} folders."}

        folders = []
        for folder in raw_folders:
            if not isinstance(folder, dict):
                return 400, {"error": "Each folder must be an object with "
                                      "'name' and 'path'."}
            raw_path = str(folder.get("path", "") or "").strip()
            raw_name = str(folder.get("name", "") or "").strip()
            if (len(raw_path) > MAX_FOLDER_PATH_LEN
                    or len(raw_name) > MAX_FOLDER_PATH_LEN):
                return 400, {"error": f"A folder path or label is longer than "
                                      f"{MAX_FOLDER_PATH_LEN} characters."}
            folders.append(library.Folder(
                raw_name or library.default_label(raw_path), raw_path))

        entries.append(library.ServedList(
            name, bool(row.get("primary")),
            [str(c).strip() for c in raw_channels if str(c).strip()],
            folders,
            {str(c).strip().lower(): str(m or "").strip().lower()
             for c, m in raw_modes.items()
             if str(m or "").strip().lower() not in (library.NORMAL, "")},
            raw_exclude))

    if not entries:
        import os as os_mod
        try:
            os_mod.remove(library.lists_file())
        except OSError:
            pass
        return 200, {"lists": build_lists_payload()["lists"],
                     "rebuild_required": True,
                     "message": "Back to one list over the configured folders."}

    try:
        library.save_lists(entries)
    except ValueError as err:
        return 400, {"error": str(err)}
    except OSError as err:
        return 500, {"error": f"Could not write the list definitions: {err}"}

    return 200, {"lists": build_lists_payload()["lists"],
                 "rebuild_required": True,
                 "message": "Saved. Rebuild the lists to publish them."}


def apply_folder_changes(payload):
    """POST /api/folders: validate the whole set, then write it.

    Returns (http_status, payload_dict).

    THE WHOLE SET, not each row. Two folders can each be perfectly good and
    still be an invalid pair - one nested inside the other lists every file
    under it twice, and two sharing a label make the label useless for telling
    them apart. library.problems() returns every fault at once for the same
    reason: an operator fixing three things should be told about three things.

    AN EMPTY LIST IS REFUSED (#1357). It used to mean "go back to the single
    Music directory" and removed the file. Since the folder rows became the
    one place a bot's folders are set - the Music directory showing as their
    first row when no list is saved - there is nothing to go back to, and a
    bot with no folders has nothing to share.

    NO REHASH. Unlike a settings save, nothing here needs reloading:
    library.folders() re-reads the file on every call, so the running daemon
    picks this up immediately. What it does NOT do is rebuild the published
    list, and a folder nobody can see in the list is not really served yet -
    hence rebuild_required, which the page turns into a sentence rather than
    leaving the operator to discover it.
    """
    import library

    if not isinstance(payload, dict):
        return 400, {"error": "Expected an object with a 'folders' list."}
    rows = payload.get("folders")
    if not isinstance(rows, list):
        return 400, {"error": "'folders' must be a list."}
    if len(rows) > MAX_SERVED_FOLDERS:
        return 400, {"error": f"At most {MAX_SERVED_FOLDERS} folders."}

    entries = []
    for row in rows:
        if not isinstance(row, dict):
            return 400, {"error": "Each folder must be an object with "
                                  "'name' and 'path'."}
        raw_path = str(row.get("path", "") or "").strip()
        raw_name = str(row.get("name", "") or "").strip()
        if len(raw_path) > MAX_FOLDER_PATH_LEN or len(raw_name) > MAX_FOLDER_PATH_LEN:
            return 400, {"error": f"A folder path or label is longer than "
                                  f"{MAX_FOLDER_PATH_LEN} characters."}
        # An unnamed folder is named after itself, exactly as the file format
        # does - so an operator who only wants to add a path can.
        entries.append(library.Folder(raw_name or library.default_label(raw_path),
                                      raw_path))

    faults = library.problems(entries)
    if faults:
        return 400, {"error": "Those folders cannot be served.",
                     "problems": faults}

    if not entries:
        return 400, {"error": "Keep at least one folder: a bot with no folders "
                              "has nothing to share."}

    try:
        library.save_folders(entries)
    except (ValueError, OSError) as err:
        return 400, {"error": str(err)}

    return 200, dict(build_folders_payload(), written=len(entries),
                     rebuild_required=True)
