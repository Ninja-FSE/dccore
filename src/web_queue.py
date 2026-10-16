"""The dashboard's Queue page (#1318): who is waiting for what, and the
operator's actions on it - moving a nick or a file, removing or clearing
rows, cancelling a pack, ignoring and un-ignoring a nick.

Split out of webserver.py, step 4 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back.
"""

import defaults as config


def _security_seconds_left(user_key):
    """How long a timed ignore or ban on this nick has to run, 0 for none -
    what turns a Queue row's Ignore button into "Ignored ... Lift" (#1206)."""
    import security
    return security.ban_seconds_left(user_key)


def build_queue_payload(user=None):
    """The Queue view's data.

    With no `user`, one summary row per queued user (their status, a preview
    of the next file, how many are waiting, the FULL list of what is waiting,
    and - for whoever is sending - the file in flight and its progress) -
    what the Queue table shows. With `user`, the full file list queued for
    that one user instead - what a click-through or `?user=<nick>` on
    /api/queue returns.

    "preview"/"count"/"files" describe the QUEUE: config.dcc_queue's rows.
    A send that found a free slot at once never has a row; a queued file
    keeps its row while it is sent, until the send settles it (#1245). The
    progress lives in config.active_transfers either way, which is why a
    sending user's progress needs its own fields rather than being folded
    into the queue ones -
    an operator asked to see the whole queue, not just its head, and a
    progress bar for the transfer actually running, not for whatever is
    queued behind it.

    "file_ids" is dcc.queue_row_id() of each of "files", in step: what a move
    or a removal sends back to name the file it means (#1245).

    No lock is taken, matching adminchat.py's _cmd_queue/_cmd_status idiom: a
    shallow dict()/list() copy of the live containers, read without a lock,
    is good enough for a status display and cannot deadlock against the
    daemon's own queue processing.
    """
    queue = dict(getattr(config, "dcc_queue", {}))
    frozen = dict(getattr(config, "frozen_queues", {}))
    active = list(getattr(config, "active_transfers", []))
    sending_users = {str(tx.get("user", "")).lower() for tx in active}

    active_by_user = {}
    for tx in active:
        active_by_user.setdefault(str(tx.get("user", "")).lower(), tx)

    # The user a folder is being packed for (#1202): without it their row read
    # "queued" for as long as rar ran. The folder's name, never its path.
    import dcc
    pack = None
    try:
        pack = dcc.pack_status()
    except Exception as err:
        print(f"[WEBSERVER] The running pack could not be read for the queue: {err}")
    packing_user = str(pack["user"]).lower() if pack else ""

    def _queued_status(entries, active):
        # "large-wait" while the next file is large and another large file
        # is being sent (#1387): the page says why the nick is passed over.
        return "large-wait" if dcc.waits_for_a_large_send(list(entries), active) else "queued"

    def _progress_fields(user_key):
        # None, not 0, when there is nothing sending or the size is not
        # known yet (dcc.start_dcc_send() writes "size" onto the row the
        # moment it has read the file's real size off disk - a fresh
        # dispatch's row does not have it for the brief window before that).
        # The dashboard tells "no bar to draw" apart from "0% so far" by
        # this, not by a size of 0, which a genuinely empty file would also
        # report honestly.
        tx = active_by_user.get(user_key)
        if tx is None:
            return None, None, None
        return tx.get("file"), tx.get("bytes_sent"), tx.get("size")

    if user:
        user_key = str(user).strip().lower()
        entries = queue.get(user_key, [])
        if user_key in sending_users:
            status = "sending"
        elif user_key == packing_user:
            status = "packing"
        elif user_key in frozen:
            status = "frozen"
        elif entries:
            status = _queued_status(entries, active)
        else:
            status = "empty"
        files = [e.get("file", "?") if isinstance(e, dict) else str(e) for e in entries]
        current_file, bytes_sent, size = _progress_fields(user_key)
        result = {"user": user_key, "status": status, "count": len(entries), "files": files,
                  "file_ids": [dcc.queue_row_id(e) for e in entries],
                  "current_file": current_file, "bytes_sent": bytes_sent, "size": size,
                  "ignored_seconds": _security_seconds_left(user_key)}
        if status == "packing":
            result["pack"] = _pack_fields(pack)
        return result

    # #220: a user sent with a free slot and nothing already queued never
    # enters dcc_queue at all - dcc.py's admission check appends straight to
    # active_transfers and returns (see start_dcc_send()). Iterating only
    # queue.items(), as this used to, left them off the Queue table entirely
    # while /api/stats (which reads active_transfers directly) showed the
    # transfer correctly - the two disagreeing about the same page. The
    # single-user branch above already got this right by checking
    # sending_users regardless of whether entries exist; this does the same.
    rows = []
    # In the order the dispatcher gives out free slots (#1206): the page shows
    # who is next, and Move up/down on a row changes that. The dispatcher's
    # own key (#1245): ranked by the wait alone, a waiting list was shown
    # last while it went first, and the arrows moved against a line the
    # page did not show.
    try:
        waiting_order = dcc.slot_order(queue, dcc.a_list_may_go_first(active))
        waiting_order += [key for key in queue if not queue[key]]
    except Exception as err:
        print(f"[WEBSERVER] The queue's order could not be read: {err}")
        waiting_order = list(queue.keys())
    for user_key in dict.fromkeys(waiting_order + list(sending_users) + ([packing_user] if packing_user else [])):
        # sending_users comes from active_transfers, whose rows are built by
        # dcc.py rather than keyed by it - an entry missing its "user" field
        # contributes "" to that set and used to reach the page as a blank row
        # with a "?" preview. Iterating dcc_queue alone could not produce that,
        # because its keys are always real nicks; taking senders from a list of
        # dicts is what introduced the possibility.
        if not user_key:
            continue
        entries = queue.get(user_key, [])
        if user_key in sending_users:
            status = "sending"
        elif user_key == packing_user:
            status = "packing"
        else:
            status = "frozen" if user_key in frozen else _queued_status(entries, active)
        files = [e.get("file", "?") if isinstance(e, dict) else str(e) for e in entries]
        first = entries[0] if entries else None
        if first is not None:
            preview = first.get("file", "?") if isinstance(first, dict) else str(first)
        elif user_key in active_by_user:
            preview = active_by_user[user_key].get("file", "?")
        else:
            preview = ""
        current_file, bytes_sent, size = _progress_fields(user_key)
        row = {"user": user_key, "preview": preview, "count": len(entries), "status": status,
               "files": files, "file_ids": [dcc.queue_row_id(e) for e in entries],
               "current_file": current_file,
               "bytes_sent": bytes_sent, "size": size,
               "ignored_seconds": _security_seconds_left(user_key)}
        if status == "packing":
            row["pack"] = _pack_fields(pack)
        rows.append(row)
    return rows


def _pack_fields(pack):
    """The running pack as the Queue page draws it: the folder as the list
    shows it, how long, and the archive so far against the folder's size."""
    import announce
    return {"name": pack["name"], "elapsed": pack["elapsed"],
            "done": pack["done"], "total": pack["total"],
            "done_text": announce.format_size_human(pack["done"]),
            "total_text": announce.format_size_human(pack["total"]) if pack["total"] else "",
            "cancelling": bool(pack.get("cancelled"))}


def build_pack_cancel_result():
    """POST /api/queue/pack/cancel (#1202): stop the folder pack that is
    running. 200 with who and what it was; 404 when nothing is packing."""
    import dcc
    cancelled = dcc.cancel_pack()
    if cancelled is None:
        return 404, {"error": "Nothing is being packed."}
    return 200, {"cancelled": {"user": cancelled["user"], "name": cancelled["name"]}}


def build_ignore_result(body):
    """POST /api/ignore (#1206): ignore a nick for some minutes. 200 with
    how long, 400 with the reason when security.ignore_user() refuses."""
    import security
    nick = str(body.get("nick") or "").strip()
    ok, message = security.ignore_user(nick, body.get("minutes"))
    if not ok:
        return 400, {"error": message}
    return 200, {"user": nick.lower(), "message": message,
                 "seconds_left": security.ban_seconds_left(nick)}


def build_unignore_result(body):
    """POST /api/unignore (#1206): end a timed ignore or ban now. 404 when
    the nick has none."""
    import security
    nick = str(body.get("nick") or "").strip()
    ok, message = security.lift_ban(nick)
    if not ok:
        return 404, {"error": message}
    return 200, {"user": nick.lower(), "message": message}


def _queue_nick(body):
    return str(body.get("nick") or "").strip()


def build_queue_move_user_result(body):
    """POST /api/queue/move-user (#1206): one place up or down the line for a
    free slot. 200 with the new place, 400 with the reason."""
    import commands
    nick = _queue_nick(body)
    ok, message = commands.move_waiting_user(nick, body.get("direction"))
    if not ok:
        return 400, {"error": message}
    return 200, {"user": nick.lower(), "message": message}


def _queue_row_id(body):
    """The `id` of the file a move or a removal means - one of the queue
    payload's "file_ids" - or None when it is missing. Required: without it
    nothing says the file is still the one the page showed (#1245).
    """
    row_id = str(body.get("id") or "").strip()
    return row_id or None


def build_queue_move_file_result(body):
    """POST /api/queue/move-file (#1206): one place up or down inside the
    nick's own queue. `position` is 1-based and `id` the file the page saw
    there, so a queue that moved meanwhile is refused, not misread."""
    import commands
    nick = _queue_nick(body)
    row_id = _queue_row_id(body)
    if row_id is None:
        return 400, {"error": "Which file? The page sent no id - reload it."}
    ok, message = commands.move_queued_file(nick, body.get("position"), body.get("direction"), row_id)
    if not ok:
        return 400, {"error": message}
    return 200, {"user": nick.lower(), "message": message}


def build_queue_remove_file_result(body):
    """POST /api/queue/remove-file (#1206): the one file out of the queue, with
    the same notice to the nick as their own `@<bot>-remove <file>`. `id`
    names the file, as for a move."""
    import commands
    nick = _queue_nick(body)
    row_id = _queue_row_id(body)
    if row_id is None:
        return 400, {"error": "Which file? The page sent no id - reload it."}
    ok, message = commands.remove_queued_file(nick, body.get("position"), row_id)
    if not ok:
        return 400, {"error": message}
    return 200, {"user": nick.lower(), "message": message}


def build_queue_clear_result(body):
    """POST /api/queue/clear (#1206): everything queued for the nick, as the
    console's `clearqueue` does. 404 when nothing was queued."""
    import commands
    nick = _queue_nick(body)
    if not nick or any(ch.isspace() for ch in nick):
        return 400, {"error": "Give the nick."}
    key = nick.lower()
    import dcc
    with dcc.queue_lock:
        count = len(getattr(config, "dcc_queue", {}).get(key) or [])
    if not count:
        return 404, {"error": f"{nick} has nothing queued."}
    commands.handle_admin_clear_queue("dashboard", "", f"!clearqueue {nick}", authorised=True)
    return 200, {"user": key, "removed": count, "message": f"Cleared {count} file(s) for {nick}."}
