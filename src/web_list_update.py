"""The dashboard's controls for this bot's own list (#1317): verifying it,
starting a rebuild and following its progress, and starting the
background reading of track lengths.

Split out of webserver.py, step 3 of splitting it (docs/FUTURE.md); these
sat in its fetch section but are about the bot's own list, not other
bots'. The routes that call them are still in webserver, which imports
every name back.
"""

import time

import defaults as config
import platform_compat
import runtime
from web_common import WEB_DASHBOARD_SOURCE


def build_verify_list_payload():
    """GET /api/tools/verify-list payload: filenames the master list carries
    under more than one folder.

    Computed on demand rather than stored. The list on disk is the source of
    truth and is already parsed by find_matching_entries(), so there is no
    side file to keep in step, nothing to go stale between an !update and a
    look at this view, and no second walk of the library.

    Returns every duplicate rather than a page of them. The count is bounded
    by how many names actually collide, which on a healthy library is zero and
    on an unhealthy one is the number the operator most wants to see in full.
    """
    import list as list_mod

    entries, _total = list_mod.find_matching_entries([], limit=None)
    duplicates = list_mod.find_duplicate_filenames(entries)
    # Resolved here rather than in the finder: the finder answers "which names
    # collide, and where does the LIST put them", which is a question about the
    # list alone. Turning a heading into a path this machine has is a
    # presentation concern, and it is what the operator can act on.
    duplicates = [dict(item, folders=[list_mod.resolve_list_folder(folder)
                                      for folder in item["folders"]])
                  for item in duplicates]
    return {
        "checked": len(entries),
        "duplicates": duplicates,
        "total": len(duplicates),
        # Distinct from `total`: how many individual copies a bare-name
        # request can never reach, which is the number answering "how much of
        # my library is this".
        #
        # "shadowed" rather than "unreachable" since #128: a requester pasting
        # a search result's whole line, "  ::INFO:: <size>" tail included,
        # reaches the copy that size names. These copies are shadowed by the
        # first-listed one for anyone who types the name alone - which is what
        # AutoQ.mrc and every ordinary request sends - not unreachable outright.
        "shadowed": sum(item["count"] - 1 for item in duplicates),
    }


def build_update_list_status_payload():
    """GET /api/tools/update-list/status payload: whether a master-list
    rebuild is running right now. config.update_inprogress is set True just
    before commands.handle_list_update_request() starts its background
    thread and cleared in that thread's own `finally`, so this is accurate
    for a rebuild started here, from !update, or from the admin console.

    "ok"/"error" (#224): "running" alone could not distinguish a rebuild
    that worked from one that failed - web/app.js's poll showed "Done. Check
    Stats for the new file count." the moment `running` flipped false,
    whichever it was. `ok` is None until the FIRST rebuild in this process
    finishes (never run yet is not the same claim as "it failed"), then True
    or False for whether that one succeeded; `error` names why when it did
    not.
    """
    payload = {
        "running": bool(getattr(config, "update_inprogress", False)),
        "ok": getattr(config, "last_list_update_ok", None),
        "error": getattr(config, "last_list_update_error", None),
        # How long the LAST finished rebuild took, whole seconds. None until
        # one has finished in this process - the same "never run yet is not a
        # claim about the last run" rule `ok` follows just above.
        "seconds": getattr(config, "last_list_update_seconds", None),
    }
    # #776: the Tools page says when the schedule next rebuilds.
    try:
        import commands as _commands
        payload["schedule"] = str(getattr(config, "LIST_REBUILD_SCHEDULE", "") or "").strip()
        payload["next_scheduled"] = _commands.next_scheduled_rebuild()
    except Exception:
        payload["schedule"], payload["next_scheduled"] = "", None
    progress = read_list_progress()
    if progress:
        payload["progress"] = progress
    # The background audio reading (#1182): running after a rebuild has
    # published, or alone from the Read audio info button. Its progress is the
    # same file's, phase "reading"; `last` is how the last one ended, for the
    # page to say - "nothing new to read" included.
    payload["audio"] = {
        "enabled": bool(getattr(config, "LIST_SHOW_AUDIO_INFO", False)),
        "running": runtime.audio_reading is not None,
        "last": runtime.audio_reading_last,
    }
    return payload


def read_list_progress():
    """What a running rebuild last reported, or None.

    update_list.py runs as a SUBPROCESS, so it has no shared memory with this
    process to report into - it writes LIST_PROGRESS_FILE and this reads it.
    Written whole and renamed into place at the other end, so a read landing
    mid-write gets the previous complete object rather than half of one.

    Returns None on anything unexpected. This feeds a progress bar: a missing,
    unreadable or malformed file should cost the bar, not the page.
    """
    import json

    path = getattr(config, "LIST_PROGRESS_FILE", None)
    if not path:
        return None
    try:
        with open(platform_compat.long_path(path), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None

    def whole(name):
        try:
            return max(0, int(loaded.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    # HOW LONG IT HAS BEEN GOING, computed here rather than in the page.
    #
    # The page cannot do it: it has no idea when the rebuild started, only
    # when it happened to open. Subtracting here also means one clock is used
    # for both ends - the daemon's - where a browser with a skewed clock
    # against a server timestamp can produce a negative elapsed, or an hour of
    # it, on the first frame.
    #
    # `started_at` is absent from a progress file written by an older build
    # mid-upgrade, which is `None` and not zero: "not reported" and "started
    # at the epoch" are different claims, and the second would render as
    # 56 years.
    started_at = loaded.get("started_at")
    elapsed = None
    try:
        if started_at is not None:
            # Clamped at zero rather than shown negative. The child stamps its
            # own clock, so a machine whose time steps backwards mid-rebuild
            # (ntp correcting a drift) would otherwise report a run that has
            # not begun.
            elapsed = max(0, int(time.time() - float(started_at)))
    except (TypeError, ValueError):
        elapsed = None

    folder_count = whole("folder_count")
    folder_index = min(whole("folder_index"), folder_count) if folder_count else 0
    percent = None
    if folder_count:
        # Folders COMPLETED, not the one in hand: a bar that jumps to 100% as
        # the last folder starts is telling the operator it has finished
        # while it is still walking.
        percent = int(max(0, folder_index - 1) * 100 / folder_count)
    return {
        "phase": str(loaded.get("phase") or "")[:40],
        "folder": str(loaded.get("folder") or "")[:120],
        "folder_index": folder_index,
        "folder_count": folder_count,
        "files": whole("files"),
        "percent": percent,
        "elapsed": elapsed,
        # Files a second, while the background audio reading runs (#1182).
        "rate": whole("rate"),
    }


def start_list_update():
    """POST /api/tools/update-list's pure logic: kick off a master-list
    rebuild, the dashboard's own equivalent of !update - added because
    FILE_DIRECTORY is deliberately not in settings_file.REQUIRED (see its
    own comment): an operator who sets it for the first time from this same
    Settings page had no way at all to then build the list it enables,
    short of a real IRC client or a CLI already running.

    commands.handle_list_update_request() already starts update_list.py on
    its own daemon thread and returns immediately - see its own docstring -
    so this only needs the same re-entrancy guard it makes internally,
    surfaced as a real HTTP response rather than the debug-channel notice it
    also sends, which an operator with no IRC client open would never see.

    Returns (http_status, payload_dict).
    """
    if bool(getattr(config, "update_inprogress", False)):
        return 409, {"error": "A list update is already running."}
    # A running search only stands in the way of the old whole-rebuild pause
    # (#923): by default the rebuild runs alongside searches.
    import list as list_mod
    if (bool(getattr(config, "search_inprogress", False))
            and list_mod.rebuild_pauses_everything()):
        return 409, {"error": "Another system scan is already in progress."}

    import commands
    commands.handle_list_update_request(
        WEB_DASHBOARD_SOURCE, WEB_DASHBOARD_SOURCE, authorised=True)
    return 200, {"update": "started"}


def start_audio_info_reading():
    """POST /api/tools/audio-info's pure logic (#1182): the Read audio info
    button. Starts a reading-only run - the audio files the published list
    has no length for, read and written in - through the same
    commands.handle_audio_info_request() the console's `audioinfo` uses.

    200 when it started, or when one is already running (the page follows
    it); 409 when a rebuild runs (it reads by itself once it has published)
    or the setting is off. Whether there is anything to read is the run's to
    find out: "nothing new to read" comes back as its result in the status.

    Returns (http_status, payload_dict).
    """
    import commands
    status, message = commands.handle_audio_info_request(
        WEB_DASHBOARD_SOURCE, WEB_DASHBOARD_SOURCE, authorised=True)
    if status in ("started", "running"):
        return 200, {"reading": status, "message": message}
    if status in ("rebuilding", "off"):
        return 409, {"error": message, "reason": status}
    return 500, {"error": message}
