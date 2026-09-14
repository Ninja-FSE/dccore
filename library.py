"""The library: the ordered set of folders this bot serves from.

DCCore has always served one directory. OmenServe has always served a list of
them, and #164 is that gap. This module is the first step: one place that
answers "which folders, in what order", so the rest of the daemon stops asking
`config.FILE_DIRECTORY` directly.

Nothing here changes behaviour yet. With no folder file on disk - which is
every install today - `folders()` returns exactly one entry built from
FILE_DIRECTORY, so every caller sees what it saw before. The design and the
decisions behind it are on #164.

WHY AN ACCESSOR RATHER THAN A LIST-SHAPED SETTING

FILE_DIRECTORY has 54 references across ten modules, concentrated in dcc.py
(16) and update_list.py (9). Teaching each of them about a list of folders
would mean touching all 54 again when multi-list arrives and the folder set
moves inside a list. Funnelled through here, that later change rebinds one
accessor instead.

WHY A FILE RATHER THAN settings.conf

settings_file.py refuses any list entry containing a comma, because list
settings serialise as ", ".join(...) - and music paths routinely contain one
("D:\\Rock, Metal"). It strips leading and trailing spaces too. So the folder
list needs its own file, and JSON for the same reason KNOWN_BOTS_FILE gives:
the record has more than one field and will grow.

The file is an OVERRIDE, not a replacement. Absent, FILE_DIRECTORY is the
single folder. That means an upgrade writes nothing and migrates nothing; the
file appears the first time an operator saves folders from the dashboard.
"""

import collections
import io
import json
import os
import re
import tempfile

import defaults as config
import platform_compat


# One served folder: a label and a real path on this machine.
#
# A named tuple rather than a dict so a mistyped attribute is an AttributeError
# here, rather than a None quietly reaching a path join and producing a path
# that looks plausible.
Folder = collections.namedtuple("Folder", ("name", "path"))

# A leading drive specifier: "C:", "D:Music". Matched literally rather than
# with os.path.splitdrive() because a label made on Linux is still read on
# Windows - see problems(). Same pattern list.py uses on heading components.
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


# One served list: a name, the folders it is built from, and the channels it
# answers in. #26's design, and the first piece of it.
#
# A named tuple for the same reason Folder is one, and `folders` inside it for
# the reason FUTURE.md gives: the folder set MOVES INSIDE A LIST at this point,
# and every caller reaches it through library.folders() rather than reading a
# setting - so the move rebinds one accessor instead of touching every call
# site a second time.
#
# `channels` is empty on the implicit list, and empty means "every channel"
# only for the primary. Once a channel can be bound to a list, an unbound
# channel gets nothing - that is #26's rule, not this stage's, and nothing
# here enforces it yet.
ServedList = collections.namedtuple(
    "ServedList", ("name", "primary", "channels", "folders"))

# What a single-list install is called. It is an operator-facing name that no
# user ever types - #26 is explicit that list names are never typed in a
# channel - so it only has to be recognisable in the dashboard and the log.
DEFAULT_LIST_NAME = "Main"


def lists_file():
    """Where the list definitions live.

    Resolved per call for the same reason folders_file() is: !rehash reloads
    config, and a path baked in at import would keep pointing at the old
    location for the life of the process.
    """
    return getattr(config, "LISTS_FILE", os.path.join("data", "lists.json"))


def load_lists(path=None):
    """The lists as stored, or None if there is no usable file.

    None rather than [] on any problem, exactly as load_folders() does and for
    exactly the same reason: [] would read as "the operator configured no
    lists" and serve nothing, when the truth is "we could not read the file".
    lists() then falls back to the single implicit list, which is what every
    install has today.
    """
    target = lists_file() if path is None else path
    try:
        with io.open(platform_compat.long_path(target), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(raw, list):
        print(f"[LIBRARY] {target} does not contain a list of lists; "
              f"falling back to the single list.")
        return None

    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        folders_in = []
        for folder in item.get("folders") or []:
            if not isinstance(folder, dict):
                continue
            folder_path = str(folder.get("path", "") or "").strip()
            if not folder_path:
                continue
            label = (str(folder.get("name", "") or "").strip()
                     or default_label(folder_path))
            folders_in.append(Folder(label, folder_path))
        channels = [str(c).strip().lower()
                    for c in (item.get("channels") or []) if str(c).strip()]
        entries.append(ServedList(name, bool(item.get("primary")),
                                  channels, folders_in))

    if not entries:
        return None

    # EXACTLY ONE PRIMARY, decided here rather than left to callers. A private
    # message carries no channel, so the primary is the only answer to "which
    # list does this request mean" - and "none of them" or "two of them" are
    # both answers that would have to be handled at every call site instead of
    # once. A file with no primary makes the first list primary; a file with
    # several keeps the first and demotes the rest.
    primary_seen = False
    resolved = []
    for entry in entries:
        if entry.primary and not primary_seen:
            primary_seen = True
            resolved.append(entry)
        else:
            resolved.append(entry._replace(primary=False))
    if not primary_seen:
        resolved[0] = resolved[0]._replace(primary=True)
    return resolved


def lists():
    """Every list this bot serves, in the operator's order, primary included.

    WITH NO lists.json - every install today - this is ONE list built from
    whatever folders() already resolved: the folder file if there is one, and
    FILE_DIRECTORY if there is not. So a caller reading this sees exactly what
    it saw before lists existed, and an install that has chosen no folder at
    all gets one list with no folders rather than no lists, which keeps "which
    list is this request for" answerable on a fresh install.
    """
    stored = load_lists()
    if stored:
        return stored
    return [ServedList(DEFAULT_LIST_NAME, True, [], _configured_folders())]


def primary_list():
    """The list a request with no channel belongs to.

    A private message carries no channel, which is the whole reason a primary
    exists. load_lists() guarantees exactly one, so this cannot come back
    empty-handed while any list is configured at all.
    """
    every = lists()
    for entry in every:
        if entry.primary:
            return entry
    return every[0]


def list_for_channel(channel):
    """The list bound to `channel`, or None if nothing is bound to it.

    None is a real answer and not a fallback to the primary: #26's rule is
    that a channel with no list bound gets nothing - no advert, no requests
    answered - and quietly serving the primary instead would be the opposite
    of that. The caller decides what to do about it; nothing calls this yet.

    Matched case-insensitively, because IRC channel names are.
    """
    wanted = str(channel or "").strip().lower()
    if not wanted:
        return None
    for entry in lists():
        if wanted in entry.channels:
            return entry
    return None


def list_for_request(channel=None):
    """The list a request arriving in `channel` belongs to, or None.

    THE RULE, and the reason it is not simply list_for_channel():

      1. A channel explicitly bound to a list gets that list.
      2. Otherwise the PRIMARY answers, but only if it binds no channels of
         its own - that makes it the catch-all, which is what every install
         today is: one list, no bindings, answering everywhere.
      3. Otherwise nothing. Once the primary names its own channels the
         operator has said where each list belongs, and #26 is explicit that a
         channel with no list bound gets no advert and no requests answered.

    Rule 2 is what stops this being an upgrade that silences every bot. Rule 3
    is what makes binding mean something. Without both, either every existing
    install stops answering or binding a channel does nothing.

    A request with no channel - a private message - is the primary by
    definition, since a PM carries nothing to route on. That is the same
    answer rule 2 gives, but it holds even when the primary does bind
    channels: a PM is not "a channel with nothing bound", it is not a channel.
    """
    name = str(channel or "").strip()
    if not name or not name.startswith(("#", "&")):
        return primary_list()

    bound = list_for_channel(name)
    if bound is not None:
        return bound

    fallback = primary_list()
    return fallback if not fallback.channels else None


def list_name_for_request(channel=None):
    """Just the name, or None. What the list-reading functions actually take."""
    chosen = list_for_request(channel)
    return chosen.name if chosen else None


def list_by_name(name):
    """The list an operator named, or None. Case-insensitive: the name is
    typed into a dashboard field, not compared against anything on the wire."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for entry in lists():
        if entry.name.strip().lower() == wanted:
            return entry
    return None


def folders_file():
    """Where the folder list lives.

    Resolved per call, not once at import: it is a config value and !rehash
    reloads config, so a path baked in at import would keep pointing at the old
    location for the life of the process. Same reasoning as list.py's own
    size_file_path().
    """
    return getattr(config, "LIBRARY_FOLDERS_FILE",
                   os.path.join("data", "library_folders.json"))


def default_label(path):
    """The label a folder gets when the operator does not choose one.

    Its own basename: "D:\\Flac" becomes "Flac". A path ending in a separator
    or naming a drive root has no basename to take, and falls back to the
    drive letter or "library" rather than to an empty label - an empty one
    would produce a doubled separator in every path built from it.
    """
    cleaned = str(path or "").replace("/", os.sep).rstrip(os.sep + "/")
    base = os.path.basename(cleaned)
    if base:
        return base
    drive = os.path.splitdrive(cleaned)[0].rstrip(":")
    return drive or "library"


def _normalise(path):
    """A path in the one form comparisons here are made in.

    normcase for Windows' case-insensitivity, abspath so a relative
    FILE_DIRECTORY and an absolute one compare equal, and no realpath: this
    runs on operator input at save time, where resolving symlinks would make
    the error message name a path the operator never typed.
    """
    return os.path.normcase(os.path.abspath(str(path or "")))


def is_inside(base, path):
    """True if `path` is `base` or sits underneath it.

    Compared with a separator boundary rather than a plain startswith, for the
    reason dcc.is_safe_path() gives about the same trap: "/srv/library-backup"
    begins with "/srv/library" as a string and is not inside it.

    This is CONFIG validation, not the request-time security gate. That gate is
    dcc.is_safe_path(), which additionally resolves symlinks, and it must stay
    the one used on a request path. The two are separate today only because
    dcc.py imports list, announce, db and runtime, and importing it from here
    would cycle - #164's step 3 touches that resolution anyway and is the place
    to give both one implementation.
    """
    base_n = _normalise(base)
    path_n = _normalise(path)
    if not base_n or not path_n:
        return False
    # The separator is appended only when the base does not already end in
    # one - a drive root ("D:\\", or "/" on POSIX) already does, and appending
    # a second built a boundary nothing could match. dcc.is_safe_path() had
    # the identical defect; see its comment.
    boundary = base_n if base_n.endswith(os.sep) else base_n + os.sep
    return path_n == base_n or path_n.startswith(boundary)


def problems(entries):
    """Every reason `entries` could not be served, as operator-readable lines.

    Returns [] when the set is usable. Each message names the specific other
    entry it conflicts with, because "invalid folder list" tells an operator
    with eight folders nothing about which two to look at.

    Deliberately NOT checked here: whether a folder is reachable right now. A
    network share that is down is a scan-time condition - update_list skips it
    with a warning and builds from the rest - not a reason to refuse the whole
    configuration and lock the operator out of editing it.
    """
    found = []
    seen_paths = {}
    seen_labels = {}

    for index, entry in enumerate(entries):
        position = index + 1
        name = str(getattr(entry, "name", "") or "").strip()
        path = str(getattr(entry, "path", "") or "").strip()

        if not path:
            found.append(f"folder {position}: no path given.")
            continue

        if not name:
            found.append(f"{path}: no label. It becomes part of every path in "
                         f"the list, so it cannot be blank.")
        else:
            # Both separators and the drive pattern are checked LITERALLY, not
            # through os.path.splitdrive() or os.sep. Those follow the host's
            # own rules, and on Linux a backslash is an ordinary filename
            # character and "C:" is an ordinary directory name - so a label
            # generated there would pass, then be read as a drive specifier by
            # every Windows client that pastes the path back. The label travels
            # further than the machine that made it.
            if name in (".", "..") or any(
                    sep in name for sep in ("/", "\\")) or _DRIVE_PREFIX_RE.match(name):
                found.append(
                    f"{path}: the label {name!r} must be a single folder name "
                    f"- no slashes, no \"..\", no drive letter. It is written "
                    f"into paths users copy back.")
            key = name.lower()
            if key in seen_labels:
                found.append(
                    f"{path}: the label {name!r} is already used by "
                    f"{seen_labels[key]!r}. Labels are what tell two folders "
                    f"apart in the list, so they have to differ.")
            else:
                seen_labels[key] = path

        if not os.path.isdir(platform_compat.long_path(path)):
            found.append(f"{path}: not a folder on this machine.")

        norm = _normalise(path)
        if norm in seen_paths:
            found.append(f"{path}: already listed as entry {seen_paths[norm]}.")
            continue
        seen_paths[norm] = position

        for other in entries[:index]:
            other_path = str(getattr(other, "path", "") or "").strip()
            if not other_path or _normalise(other_path) == norm:
                continue
            if is_inside(other_path, path):
                found.append(
                    f"{path}: sits inside {other_path}, which is already "
                    f"served. Every file under it would be listed twice.")
            elif is_inside(path, other_path):
                found.append(
                    f"{path}: contains {other_path}, which is already served. "
                    f"Every file under that one would be listed twice.")

    return found


def load_folders(path=None):
    """The folder list as stored, or None if there is no usable file.

    None rather than [] on any problem, and the difference matters: [] would
    read as "the operator configured no folders" and serve nothing, where the
    truth is "we could not read the file", and folders() then falls back to
    FILE_DIRECTORY exactly as it does on a fresh install.
    """
    target = folders_file() if path is None else path
    try:
        with io.open(platform_compat.long_path(target), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None

    if not isinstance(raw, list):
        print(f"[LIBRARY] {target} does not contain a list of folders; "
              f"falling back to FILE_DIRECTORY.")
        return None

    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        folder_path = str(item.get("path", "") or "").strip()
        if not folder_path:
            continue
        label = str(item.get("name", "") or "").strip() or default_label(folder_path)
        entries.append(Folder(label, folder_path))

    return entries or None


def save_folders(entries, path=None):
    """Write the folder list, refusing a set that could not be served.

    Raises ValueError naming every problem at once rather than the first, so
    an operator fixing three things is told about three things.

    Written to a temporary file and renamed, through the same
    replace_with_retry() every other file in this project uses: a half-written
    folder list read at startup would serve part of a library with no
    indication anything was wrong.
    """
    entries = [Folder(getattr(e, "name", "") or default_label(getattr(e, "path", "")),
                      getattr(e, "path", "")) for e in entries]

    found = problems(entries)
    if found:
        raise ValueError("\n".join(found))

    target = folders_file() if path is None else path
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(platform_compat.long_path(directory), exist_ok=True)

    payload = json.dumps([{"name": e.name, "path": e.path} for e in entries],
                         indent=2, ensure_ascii=False)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=directory,
        prefix=".library-", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(payload + "\n")
        platform_compat.replace_with_retry(handle.name, target)
    except BaseException:
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise

    return entries


def list_problems(entries):
    """Every reason `entries` could not be served as a set of lists.

    Returns [] when they are usable. Each message names the specific other
    entry it conflicts with, for the same reason problems() does: "invalid
    list configuration" tells an operator with four lists nothing about which
    two to look at.

    The FOLDER rules are not repeated here - problems() already owns them, and
    each list's folders are put through it so a bad label is reported the same
    way whichever screen it was typed on. Only the rules that are about lists
    live here.
    """
    found = []
    seen_names = {}
    seen_channels = {}

    for index, entry in enumerate(entries):
        position = index + 1
        name = str(getattr(entry, "name", "") or "").strip()

        if not name:
            found.append(f"list {position}: no name given. A list is bound to "
                         f"channels and picked in this page by its name, so it "
                         f"cannot be blank.")
            continue

        key = name.lower()
        if key in seen_names:
            # Case-insensitively, because list_by_name() matches that way and
            # because on Windows two directories differing only in case are
            # one directory.
            found.append(f"{name!r}: there is already a list called "
                         f"{seen_names[key]!r}.")
        seen_names[key] = name

        # No directory-collision check here. list.list_slug() is one-to-one by
        # construction - a name it had to change carries a digest of the
        # original - so two distinct names cannot share a directory, and a
        # rule that can never fire is a branch no test can cover. The
        # invariant is asserted where it lives, in list_slug()'s own tests.

        for channel in getattr(entry, "channels", None) or []:
            channel = str(channel).strip()
            if not channel:
                continue
            if not channel.startswith(("#", "&")):
                found.append(f"{name!r}: {channel!r} is not a channel name - "
                             f"it must start with # or &.")
                continue
            low = channel.lower()
            if low in seen_channels and seen_channels[low].lower() != key:
                found.append(f"{channel} is bound to both {seen_channels[low]!r} "
                             f"and {name!r}. A channel can only serve one list.")
            seen_channels[low] = name

        found.extend(f"{name!r}: {line}"
                     for line in problems(getattr(entry, "folders", None) or []))

    if entries and not any(getattr(e, "primary", False) for e in entries):
        # Not fatal on LOAD - load_lists() promotes the first - but on a SAVE
        # the operator is looking at the screen and can be told, because a
        # private message has no channel and the primary is the only answer to
        # which list it means.
        found.append("no list is marked primary. A private message carries no "
                     "channel, so one list has to be the one it means.")

    return found


def save_lists(entries, path=None):
    """Write the list definitions, refusing a set that could not be served.

    Raises ValueError naming every problem at once rather than the first, so
    an operator fixing three things is told about three things.

    Written to a temporary file and renamed, through the same
    replace_with_retry() every other file here uses: a half-written lists file
    read at startup would serve part of a configuration with no indication
    anything was wrong.
    """
    prepared = []
    for entry in entries:
        folders_in = [Folder(getattr(f, "name", "") or default_label(getattr(f, "path", "")),
                             getattr(f, "path", ""))
                      for f in (getattr(entry, "folders", None) or [])]
        prepared.append(ServedList(
            str(getattr(entry, "name", "") or "").strip(),
            bool(getattr(entry, "primary", False)),
            [str(c).strip().lower() for c in (getattr(entry, "channels", None) or [])
             if str(c).strip()],
            folders_in))

    found = list_problems(prepared)
    if found:
        raise ValueError("\n".join(found))

    target = lists_file() if path is None else path
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(platform_compat.long_path(directory), exist_ok=True)

    payload = json.dumps(
        [{"name": e.name, "primary": e.primary, "channels": list(e.channels),
          "folders": [{"name": f.name, "path": f.path} for f in e.folders]}
         for e in prepared],
        indent=2, ensure_ascii=False)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=directory,
        prefix=".lists-", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(payload + "\n")
        platform_compat.replace_with_retry(handle.name, target)
    except BaseException:
        try:
            os.remove(handle.name)
        except OSError:
            pass
        raise

    return prepared


def _configured_folders():
    """The folder set as configured, before lists existed.

    The folder file if there is one, FILE_DIRECTORY if there is not, and []
    if neither - which is the honest answer to "which folders are configured"
    on an install that has not chosen one yet. Callers already handle that,
    because FILE_DIRECTORY is deliberately allowed to be blank until the
    dashboard sets it.

    Split out from folders() so lists() can build the implicit list from the
    same resolution rather than a copy of it. That is the whole point of this
    stage: one answer to "which folders", reached one way.
    """
    stored = load_folders()
    if stored:
        return stored

    single = str(getattr(config, "FILE_DIRECTORY", "") or "").strip()
    if not single:
        return []
    return [Folder(default_label(single), single)]


def folders(name=None):
    """Every folder a list is built from, in the order they are served.

    The order is the operator's: it decides the order the list is built in,
    and which folder wins when the same relative path exists in two of them.

    `name` picks a list; without one this is the PRIMARY list's folders, which
    on every install today is the only list there is. That default is what
    lets the thirteen existing callers stay exactly as they are - FUTURE.md's
    own argument for putting the accessor in first: the folder set moving
    inside a list rebinds this function rather than touching every call site a
    second time.

    An unknown name returns [] rather than falling back to the primary. A
    caller asking for a list that is not configured has a bug, and serving it
    somebody else's folders would hide that behind plausible-looking output.
    """
    if name is None:
        return primary_list().folders
    chosen = list_by_name(name)
    return chosen.folders if chosen else []


def every_folder():
    """Every folder of every configured list, in list order, deduplicated.

    NOT the same question as folders(). folders() answers "what is this list
    built from", and every caller on the REQUEST path knows which list it is
    serving, so that is the right accessor there - and the stronger one, since
    it will not resolve a request against some other list's library.

    This answers the question the two callers below actually have, which is
    the reverse: "is this path one of OURS at all". Both run AFTER a request
    has already been routed and validated against its own list - the pack-time
    poison check and the download counter - and by then the list name is gone.
    Asking folders() there silently means "the PRIMARY list's folders", so on
    a multi-list install every path from any other list reads as foreign.

    Deduplicated by path because two lists may legitimately share a folder,
    and a caller iterating this wants each root once.
    """
    seen = set()
    out = []
    for served in lists():
        for entry in served.folders:
            key = os.path.normcase(os.path.normpath(entry.path))
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def folder_paths():
    """Just the paths, in order. For callers that have no use for labels."""
    return [entry.path for entry in folders()]


def folder_for_label(label, name=None):
    """The folder a list path's first component names, or None.

    Matched case-insensitively: the label is written into paths that users
    copy, paste and retype, and a request that differs only in case is the
    same request.
    """
    wanted = str(label or "").strip().lower()
    if not wanted:
        return None
    for entry in folders(name):
        if entry.name.lower() == wanted:
            return entry
    return None
