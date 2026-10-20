"""The dashboard's fetch queue (#1317): asking other bots for files, and the
Downloads page's view of those requests - their state, pausing,
deleting, clearing, and purging a fetched list.

Split out of webserver.py, step 3 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back. Four helpers come from web_lists, which they share with the List
Browser's own fetches.
"""

import os

import defaults as config
import platform_compat
from web_common import FETCH_ENQUEUE_MAX_ITEMS, reject_if_unsafe_for_irc_line
from web_lists import (
    _enqueue_folder_request,
    _preferred_fetch_channel,
    bot_not_here_error,
    fetch_feature_error,
)


# ==========================================================================
# Cross-bot file fetch (mutating - behind the same login as every other
# route). Pure logic, same reasoning as above.
# ==========================================================================

def build_fetch_enqueue_result(payload):
    """Validate `payload` (one {"bot","filename"} object, or a list of them -
    the operator explicitly wants multi-select) and append a `pending` row
    per valid item to config.fetch_queue. Does NOT dispatch anything itself;
    dcc_fetch.check_fetch_queue()'s background dispatcher owns pacing.

    Returns (http_status, payload_dict) with "created" (the new request ids)
    and "errors" (one entry per rejected item, if any).
    """

    unavailable = fetch_feature_error()
    if unavailable:
        return 503, {"error": unavailable}
    import dcc_fetch
    import list as list_mod

    items = [payload] if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        return 400, {"error": 'Expected a {"bot": .., "filename": ..} object, '
                               'or a non-empty list of them.'}
    # Before the per-item loop, not inside it: the whole point is to refuse the
    # body without paying for it. Rejecting the request outright rather than
    # taking the first 500 and reporting the rest - a partial accept on a batch
    # this size is worse than a refusal, because the operator cannot tell from
    # the dashboard which files made it in and would have to diff the Downloads
    # table against what they pasted.
    if len(items) > FETCH_ENQUEUE_MAX_ITEMS:
        return 413, {"error": f"At most {FETCH_ENQUEUE_MAX_ITEMS} items per "
                              f"request; this one had {len(items)}."}

    created = []
    errors = []
    for raw in items:
        if not isinstance(raw, dict):
            errors.append({"error": "Each item must be an object with bot/filename.", "item": raw})
            continue
        bot_raw = raw.get("bot", "")
        filename_raw = raw.get("filename", "")
        # reject_if_unsafe_for_irc_line() covers two bugs at once here: a
        # non-string bot/filename (previously silently str()-coerced into a
        # real queue row instead of rejected) and an embedded \r/\n (which
        # dcc_fetch.check_fetch_queue() would later interpolate verbatim into
        # an outbound "PRIVMSG <bot> :!<bot> <filename>\r\n" line - see the
        # function's own docstring).
        field_err = (reject_if_unsafe_for_irc_line(bot_raw, "bot")
                     or reject_if_unsafe_for_irc_line(filename_raw, "filename"))
        if field_err:
            errors.append({"error": field_err, "item": raw})
            continue
        bot = bot_raw.strip()
        filename = filename_raw.strip()
        if not bot or not filename:
            errors.append({"error": "Both 'bot' and 'filename' are required.", "item": raw})
            continue
        # A TRIGGER IS NOT A NICK (#1209). A line pasted out of an
        # mxrarserver list begins "!<trigger>", a word its operator chose, so
        # the "bot" the paste box split off may be that word and not anybody
        # in the channel. When it is not a bot we know and exactly one known
        # bot answers to it, the request is that bot's - matched by its nick
        # as every request is - and still addressed to the trigger it was
        # written with.
        trigger = None
        if not dcc_fetch.bot_is_known(bot):
            owner = dcc_fetch.bot_for_trigger(bot)
            if owner:
                bot, trigger = owner, bot
        # PER ITEM, not per request: a bulk paste is routinely several bots at
        # once, and one of them having signed off is no reason to refuse the
        # rest. It joins `errors`, which this route already reports beside
        # whatever it did manage to queue.
        # A bot we know may be away (#926): its request waits for it and goes
        # out when it is back. One we have never seen is still refused - a
        # typo in a pasted nick would otherwise wait for ever.
        absent = bot_not_here_error(bot)
        if absent and not dcc_fetch.bot_is_known(bot):
            errors.append({"error": absent, "item": raw})
            continue
        # AN MXRARSERVER FOLDER ROW (#1209), ticked in the List Browser or
        # pasted: "<path>.rar" is a folder for that bot to pack, and the RAR
        # comes back under another name, so as a file request it could never
        # be admitted. It goes the folder route instead.
        if list_mod.pack_path_of(filename):
            folder_status, result = _enqueue_folder_request(
                dcc_fetch, bot, filename, trigger, raw=raw)
            if folder_status == 200:
                created.extend(result["created"])
            else:
                errors.append({"error": result["error"], "item": raw})
            continue
        if dcc_fetch.request_already_waiting(bot, filename):
            errors.append({"error": f"Already requested from {bot} and not finished - "
                                    "it is not asked for twice.",
                           "item": raw})
            continue
        request_id = dcc_fetch.enqueue_fetch(bot, filename, trigger=trigger,
                                            channel=_preferred_fetch_channel(raw, bot))
        if request_id is None:
            # Only reachable via the queue cap: enqueue_fetch()'s other refusal
            # is for "list"/"folder" rows and this route only creates "file"
            # ones. Appending the None unchecked - which is what this line used
            # to do - would have put a null in "created", so the dashboard would
            # report the row as queued and then never find it again.
            errors.append({"error": "The fetch queue is full - wait for some "
                                    "downloads to finish, or delete queued rows.",
                           "item": raw})
            continue
        created.append(request_id)

    status = 200 if created else 400
    return status, {"created": created, "errors": errors}


def build_fetch_status_payload():
    """GET /api/fetch/status payload: every fetch_queue row, NEWEST FIRST, so
    the dashboard's Downloads panel has a stable order to render and the row
    an operator just created is the one they are already looking at.

    An operator's request, and the right way round for a log: the reason to
    open this view is
    almost always the most recent thing that happened. Oldest-first meant
    scrolling past every completed fetch to find it.

    Still ordered by time, so it is no less stable than before - rows do not
    reshuffle between polls, they are simply counted from the other end.

    Reads config.fetch_queue under the same lock dcc_fetch.py's writers use
    (enqueue_fetch() inserting a new row, check_fetch_queue() promoting or
    expiring one) - without it, this dict(...) copy could observe a row
    appearing mid-insert, or raise "dictionary changed size during
    iteration" if a new row was enqueued from another thread while this
    copy was being built.
    """
    import dcc_fetch
    with dcc_fetch._fetch_lock():
        queue = {request_id: dict(row)
                 for request_id, row in getattr(config, "fetch_queue", {}).items()}
    rows = []
    for request_id, row in sorted(queue.items(),
                                  key=lambda kv: kv[1].get("requested_at", 0),
                                  reverse=True):
        row["id"] = request_id
        rows.append(row)
    return rows


def build_fetch_pause_result(payload, pause):
    """POST /api/fetch/pause and /api/fetch/resume (#926): stop or restart
    fetching from one bot. Its requests stay in the queue - paused ones wait,
    "Paused", and go out again once it is resumed."""
    import dcc_fetch
    bot = str((payload or {}).get("bot") or "").strip() if isinstance(payload, dict) else ""
    if not bot:
        return 400, {"error": "Which bot?"}
    unsafe = reject_if_unsafe_for_irc_line(bot, "bot")
    if unsafe:
        return 400, {"error": unsafe}
    if pause:
        dcc_fetch.pause_bot(bot, "paused from the Downloads page")
        return 200, {"paused": bot}
    if not dcc_fetch.resume_bot(bot):
        return 404, {"error": f"{bot} is not paused."}
    return 200, {"resumed": bot}


def build_fetch_delete_result(request_id, only_states=None):
    """DELETE /api/fetch/<request_id>: forget a finished fetch and remove its
    file from FETCHED_FILES_DIR, if it has one.

    Refuses anything actually in flight (offered/listening/receiving) - there
    is no cancellation path for a transfer thread already running, so dropping
    the row out from under it would just let the thread keep writing to a file
    nothing in the UI can see or ever clean up again.

    `pending` is deletable, and used to be refused alongside those three. It
    does not belong with them: a pending row is one check_fetch_queue() has not
    promoted yet, so there is no thread, no socket, no offer on the wire and no
    file on disk - nothing to cancel, only a row to forget. Sweeping it in with
    the in-flight states meant a queue could only be emptied by restarting the
    daemon, since !rehash preserves fetch_queue too: one mistaken bulk enqueue
    was unrecoverable from the dashboard that created it.

    Safe against the dispatcher precisely because both sides take
    dcc_fetch._fetch_lock(): check_fetch_queue() holds it while flipping
    pending -> offered, and this function holds it across reading the state and
    doing the del. A row cannot be promoted in between - either we observe
    `pending` and it is still pending when it is removed, or we observe
    `offered` and refuse.

    "complete" (this includes a rejected list archive - see
    build_fetch_status_payload()'s caller for that distinction, it is still
    state == "complete" here) and "failed" rows remain deletable as before.

    Never builds the on-disk path from request_id or anything else attacker-
    reachable - request_id only selects the row, and the row's own
    stored_filename (set by dcc_fetch.py after running the offer's filename
    through dcc.is_safe_path()) is what actually gets removed.

    That upstream check is real, but this route used to lean on it alone: a
    plain os.path.join() has no protection against an absolute or
    "../"-laden stored_filename the way api_fetch_download()'s
    send_from_directory() does (it performs its own safe join and raises
    NotFound on anything that escapes `directory`) - so download was
    protected twice and delete, the destructive one, only once. re-checks
    with the exact same dcc.is_safe_path() dcc_fetch.py's write path already
    uses, rather than trusting that stored_filename can never be anything
    else, forever, everywhere it is read.
    """
    import dcc
    import dcc_fetch
    with dcc_fetch._fetch_lock():
        row = getattr(config, "fetch_queue", {}).get(request_id)
        if row is None:
            return 404, {"error": "Unknown fetch request."}
        # `only_states` is for a caller that means to let a REQUEST go and never
        # a file (the mIRC window's Cancel, #1022): checked here, under the same
        # hold of the lock as the del, so a row that finished a moment ago is
        # not taken for one that had not started, and its file is not removed.
        if only_states is not None and row.get("state") not in only_states:
            return 409, {"error": "That download is no longer waiting."}
        # "queued" too (#926): the other bot holds our request in its queue
        # and nothing is moving yet - letting it go is only forgetting it.
        # "offered" too: our request is out and no offer has come, so there is
        # no thread and no file. handle_incoming_offer() only claims a row
        # while it is offered or queued, so an offer arriving after this is
        # refused as unsolicited.
        if row.get("state") not in ("complete", "failed", "pending", "queued", "offered"):
            return 409, {"error": "A fetch already in progress cannot be deleted."}
        stored_filename = row.get("stored_filename")
        # A request the other bot may already hold in its queue: one it has not
        # answered, one it queued, and one we gave up on for silence - a busy
        # bot holds that too, and sends the file later.
        at_the_bot = ((row.get("state") in ("offered", "queued")
                       or (row.get("state") == "failed" and row.get("reason") == "no response"))
                      and row.get("request_type", "file") == "file")
        bot = row.get("bot")
        asked_for = row.get("requested_filename") or row.get("filename")
        del config.fetch_queue[request_id]
        # The row's own folder: FETCHED_FILES_DIR, or its bot's (#1423).
        directory = dcc_fetch.fetched_dir_of(row)
        if directory is None:
            stored_filename = None
        if stored_filename and dcc_fetch.another_row_holds_file_locked(
                config.fetch_queue, directory, stored_filename):
            # Another row's file now (#1269): a newer fetch of the same name
            # took the plain name after this row's file was moved away.
            stored_filename = None
        never_sent = dcc_fetch.take_back_unsent_request(row)
        if at_the_bot and dcc_fetch.another_row_wants_locked(config.fetch_queue, bot, asked_for):
            # Another row still waits on the same file there (#1083).
            at_the_bot = False

    removed_at_bot = False
    if at_the_bot and not never_sent:
        removed_at_bot = dcc_fetch.drop_our_request_at(bot, asked_for, channel=row.get("channel"))

    if stored_filename:
        target = os.path.join(directory, stored_filename)
        if not dcc.is_safe_path(directory, target):
            print(f"[WEBUI] Refused to delete {stored_filename!r}: outside FETCHED_FILES_DIR.")
            return 500, {"error": "Refused: the stored path is outside the fetch directory."}
        try:
            os.remove(platform_compat.long_path(target))
        except FileNotFoundError:
            pass
        except OSError as remove_err:
            print(f"[WEBUI] Could not delete fetched file {stored_filename!r}: {remove_err}")

    # Right away, not up to 2s later on check_fetch_queue()'s own polling
    # tick (dcc_fetch._persist_fetch_history_locked()) - a crash in that
    # window would otherwise bring this just-deleted row back on the next
    # boot, pointing at a file that no longer exists.
    dcc_fetch.persist_fetch_history()

    result = {"deleted": request_id}
    if at_the_bot:
        result["removed_at_bot"] = removed_at_bot
    return 200, result


def build_fetch_delete_many_result(request_ids, only_states=None):
    """Delete several rows in ONE pass (#1246 review), for `dlcancel all` /
    `dlcancel <id> <id> ...` - dccore.mrc's own "Remove all" calls this
    through the same console command.

    Before this, each id went through build_fetch_delete_result() on its
    own: a separate acquire-and-release of the fetch lock, a separate
    another_row_wants_locked() SCAN of the whole queue, and a separate
    persist_fetch_history() REWRITE OF THE WHOLE HISTORY FILE - all of it
    repeated once per id. Measured: 500 pending + 500 finished took 6.0s;
    2000 pending took 39.8s, the console taking no other command meanwhile
    (dict mutation and the history write both run under a lock a console
    command is also subject to). The same pattern build_fetch_clear_result()
    already uses for the Downloads page's Clear buttons, generalised to an
    explicit id list instead of a state-selected one: one hold of the lock
    for the whole batch, one scan for "is anything else still waiting on
    this exact file" per row but all under that SAME hold rather than a
    fresh acquire each time, and one persist at the very end.

    Deletes a file from disk for a row that has one (same reasoning as
    build_fetch_delete_result() - unreachable for the current caller's
    "pending"/"offered"/"queued" states, since none of them has received a
    byte yet, but kept here so this function's own contract does not
    silently depend on which states a caller happens to pass today).

    Returns (http_status, {"cancelled": [ids], "refused": [{"id":, "error":}]})
    - always 200: a per-id refusal (already in flight, gone, or wrong
    state) is reported per id, not failed as a whole request, the same as
    build_fetch_clear_result()'s own "nothing to clear is not an error".
    """
    import dcc
    import dcc_fetch

    cancelled = []
    refused = []
    still_held = []
    removed_files = []
    with dcc_fetch._fetch_lock():
        queue = dcc_fetch._ensure_fetch_queue()
        for request_id in dict.fromkeys(request_ids):
            row = queue.get(request_id)
            if row is None:
                refused.append({"id": request_id, "error": "Unknown fetch request."})
                continue
            if only_states is not None and row.get("state") not in only_states:
                refused.append({"id": request_id, "error": "That download is no longer waiting."})
                continue
            if row.get("state") not in ("complete", "failed", "pending", "queued", "offered"):
                refused.append({"id": request_id, "error": "A fetch already in progress cannot be deleted."})
                continue
            stored_filename = row.get("stored_filename")
            at_the_bot = ((row.get("state") in ("offered", "queued")
                          or (row.get("state") == "failed" and row.get("reason") == "no response"))
                         and row.get("request_type", "file") == "file")
            bot = row.get("bot")
            asked_for = row.get("requested_filename") or row.get("filename")
            channel = row.get("channel")
            del queue[request_id]
            never_sent = dcc_fetch.take_back_unsent_request(row)
            if at_the_bot and not never_sent:
                still_held.append((bot, asked_for, channel))
            directory = dcc_fetch.fetched_dir_of(row)
            if stored_filename and directory is not None:
                removed_files.append((directory, stored_filename))
            cancelled.append(request_id)
        # Never a file a row left in the queue still names as its own
        # (#1269) - checked once the whole batch is popped, so two rows of
        # this batch sharing one file still remove it.
        # Per folder (#1423): the same name in two bots' folders is two files.
        by_folder = {}
        for directory, name in dict.fromkeys(removed_files):
            by_folder.setdefault(directory, []).append(name)
        removed_files = []
        for directory, names in by_folder.items():
            still_named = dcc_fetch.files_other_rows_hold_locked(queue, directory, names)
            removed_files.extend((directory, name) for name in names if name not in still_named)
        # Never for a file a newer row (one of THIS batch's own survivors,
        # or one outside it) still waits on there (#1083) - checked once
        # the whole batch has already been popped, under the SAME lock
        # hold, not a fresh another_row_wants_locked() scan per id.
        still_held = [(bot, asked_for, channel) for bot, asked_for, channel in still_held
                     if not dcc_fetch.another_row_wants_locked(queue, bot, asked_for)]
        # Two of THIS batch's own rows can both be "at_the_bot" for the same
        # bot and file (the exact #1083 shape, both sides now gone): the
        # check above lets both through since neither sees the other in the
        # queue by the time it runs. One "@bot-remove" clears our entry
        # there regardless of how many local rows pointed at it, so only
        # the first is kept.
        seen = set()
        deduped = []
        for bot, asked_for, channel in still_held:
            key = (bot, asked_for)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((bot, asked_for, channel))
        still_held = deduped

    for directory, stored_filename in removed_files:
        target = os.path.join(directory, stored_filename)
        if not dcc.is_safe_path(directory, target):
            print(f"[WEBUI] Refused to delete {stored_filename!r}: outside FETCHED_FILES_DIR.")
            continue
        try:
            os.remove(platform_compat.long_path(target))
        except FileNotFoundError:
            pass
        except OSError as remove_err:
            print(f"[WEBUI] Could not delete fetched file {stored_filename!r}: {remove_err}")

    for bot, asked_for, channel in still_held:
        dcc_fetch.drop_our_request_at(bot, asked_for, channel=channel)

    if cancelled:
        dcc_fetch.persist_fetch_history()

    return 200, {"cancelled": cancelled, "refused": refused}


# Which finished rows POST /api/fetch/clear may forget, by what the operator asked for.
FETCH_CLEAR_STATES = {
    "finished": ("complete", "failed"),
    "complete": ("complete",),
    "failed": ("failed",),
}


def build_fetch_clear_result(payload):
    """POST /api/fetch/clear (#1019): forget every finished row of one kind at
    once - the Downloads page's Clear buttons - instead of one Delete click
    each or waiting out FETCH_HISTORY_DAYS.

    `which` is "finished" (downloaded and failed), "complete" or "failed".
    Only those rows ever go: anything pending, queued or in flight is left
    exactly where it is, for the reason build_fetch_delete_result() gives.

    The FILES stay on disk. This forgets the list of what was fetched, the
    same thing the history limit does when a row ages out
    (dcc_fetch.prune_fetch_history_locked()); deleting a few hundred files
    from one button is not what a "clear the list" click says it does, and
    a row's own Delete is still there for the file.
    """
    import dcc_fetch
    which = str((payload or {}).get("which") or "finished") if isinstance(payload, dict) else "finished"
    states = FETCH_CLEAR_STATES.get(which)
    if states is None:
        return 400, {"error": "Clear which rows? Use finished, complete or failed."}
    # A failed row the other bot may still hold - one given up on for silence
    # (#1047) - is let go there too, as its own Delete does: forgotten here
    # only, the other bot sent the file when its turn came and it was refused
    # as unsolicited, its send slot wasted. drop_our_request_at() only ever
    # tells a DCCore peer; a request that never left is not mentioned to it.
    still_held = []
    with dcc_fetch._fetch_lock():
        queue = dcc_fetch._ensure_fetch_queue()
        doomed = [rid for rid, row in queue.items() if row.get("state") in states]
        for rid in doomed:
            row = queue.pop(rid)
            never_sent = dcc_fetch.take_back_unsent_request(row)
            if (row.get("state") == "failed" and row.get("reason") == "no response"
                    and row.get("request_type", "file") == "file" and not never_sent):
                still_held.append((row.get("bot"), row.get("requested_filename") or row.get("filename"), row.get("channel")))
        # Never for a file a newer row still waits on there (#1083): the
        # remove would take that request's place too.
        still_held = [(bot, asked_for, channel) for bot, asked_for, channel in still_held
                      if not dcc_fetch.another_row_wants_locked(queue, bot, asked_for)]
    for bot, asked_for, channel in still_held:
        dcc_fetch.drop_our_request_at(bot, asked_for, channel=channel)
    if doomed:
        dcc_fetch.persist_fetch_history()
    return 200, {"cleared": len(doomed)}


def build_fetched_list_purge_result(source):
    """POST /api/filelists/<source>/purge: forget one bot's fetched list(s).

    Thin on purpose. list_fetch.purge_fetched_list() owns the store, its lock
    and the three things that have to go together; this decides only what an
    HTTP caller is told, the same split every other builder in this file
    keeps.

    409 rather than 400 for a fetch in flight: the request is well formed and
    would be valid in a moment, which is what that code means and what tells
    the page to say "try again" rather than "that was wrong".
    """
    import list_fetch

    ok, detail = list_fetch.purge_fetched_list(source)
    if ok:
        return 200, {"purged": True, "detail": detail}
    if "in progress" in detail:
        return 409, {"error": detail}
    if detail.startswith("Nothing is held"):
        return 404, {"error": detail}
    return 400, {"error": detail}
