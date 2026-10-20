"""The dashboard's view of other bots' lists (#1315): asking a bot for its
list or a packed folder, the List Browser's held lists and their sources,
the cross-list search, and purging what is held.

Split out of webserver.py, step 2 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back. The fetch section uses this module's fetch_feature_error(),
bot_not_here_error(), _preferred_fetch_channel() and
_enqueue_folder_request(), which is why this section moved before it.
"""

import os

import defaults as config
import runtime
from web_common import (
    FETCH_ENQUEUE_MAX_ITEMS,
    FILELISTS_DEFAULT_PAGE_SIZE,
    reject_if_unsafe_for_irc_line,
    split_list_search_words,
)


# ==========================================================================
# Cross-bot fetched file lists (mutating enqueue + read-only lookups). Another
# bot's full list, fetched via dcc_fetch.py's request_type="list" rows,
# extracted and parsed by list_fetch.py, and kept switchable in
# config.fetched_bot_lists (keyed by lowercased bot nick, one entry per bot -
# a later fetch for the same nick REPLACES it, see list_fetch.py). Pure logic
# here, same reasoning as every other build_*_payload()/build_*_result()
# function in this module: no Flask import, fully unit testable.
# ==========================================================================

def fetch_feature_error():
    """Why the cross-bot fetch feature will not accept work, or None.

    oserve.startup() sets config.fetch_feature_disabled when FETCHED_FILES_DIR
    could not be created - there is nowhere to put a fetched file, so
    dcc_fetch.check_fetch_queue() refuses to promote any row past `pending`.
    The two HTTP routes that CREATE those rows have to refuse for the same
    reason, or the dashboard accepts a request, reports it queued, and it then
    sits pending forever with nothing said about why.

    Absent means DISABLED, deliberately. The attribute exists from the moment
    startup() has run, so the only way to read it missing is to ask before
    then - and of the two guesses available at that point, accepting a fetch
    with nowhere to put the file is the worse one. That also holds the
    behaviour steady if the dashboard is ever started earlier in the boot
    sequence than it is today.
    """
    if getattr(config, "fetch_feature_disabled", True):
        return ("Cross-bot file fetch is unavailable: the fetch directory "
                "(FETCHED_FILES_DIR) could not be created when the bot "
                "started. Check the path and its permissions, then restart "
                "the bot.")
    return None


BOT_ALONE_FETCH_CONFLICT_ERROR = (
    "A list or folder request is already in progress for this bot - wait "
    "for it to finish before starting another."
)


def bot_not_here_error(bot):
    """Why we will not ask this bot, or None if we will.

    FROM THE BETA. A list was requested from a nick that was not on the
    network - the server answered the operator's own WHOIS with "No such
    nick" - and the fetch sat in the queue holding a slot until it timed out,
    then reported "no response". Which was true, and useless: nobody was there
    to respond, and that was knowable before a line went out.

    The daemon already knows. config.channel_users is synced from 353/JOIN/
    PART for every channel it is in, and dcc.py has read it as proof of
    presence before dispatching a send since long before this. A request is a
    PRIVMSG into a channel: a nick that is not in one of ours cannot see it,
    so this is not a guess about whether they would answer - it is the
    observation that they were not asked.

    Returns None when we have no channel membership at all, rather than
    refusing everything: that is a bot which has not finished joining, and
    "wait" is a better answer than "nobody exists".
    """
    import dcc

    nick = str(bot or "").strip()
    if not nick:
        return None
    with runtime.channel_users_lock():
        known = any(users for users in
                    (getattr(config, "channel_users", {}) or {}).values())
    if not known:
        return None
    if dcc.user_is_present_in_ram(nick):
        return None
    return (f"{nick} is not in any channel this bot is in, so a request "
            f"would go nowhere. They may have signed off - the dot beside "
            f"their name says which.")


def build_list_fetch_enqueue_result(bot_raw, channel_raw=None, secondary_raw=False):
    """POST /api/filelists/fetch's pure logic: validate the bot nick and
    enqueue a request_type="list" row.

    `channel_raw` (#1240) asks for this bot's list from a SPECIFIC one of our
    channels rather than letting the dispatcher pick one. INTERNAL ONLY: the
    HTTP route below never reads a "channel" field from its body, and no
    console command accepts one either - an operator cannot ask for one by
    hand (that capability existed briefly in #1239's draft and was reverted;
    see its PR thread for the real-world data loss that caused it, now fixed
    at the storage layer instead - see list_fetch._install_secondary_channel_
    lists()).

    When not given and this is not a secondary fetch (see `secondary_raw`
    below), it defaults to held_list_channel(bot) - the channel this bot's
    list is already known to answer in, if any (#1240 review). Left to the
    dispatcher's own fallback (the bot's last advert channel, which may be
    ANY channel we share with it) an ordinary refresh could re-ask in a
    different shared channel than the one its list actually came from,
    which - before this - created a second, duplicate marker for content
    already held under the first: confirmed on review, real data from
    #1239's own measurement (38 of 49 bots share more than one channel with
    us).

    `secondary_raw` is explicit (#1240 review), and True only for the one
    caller that ran the discovery confidence gate: list_grab.
    secondary_channel_tick(), once list_grab._secondary_channel_candidates()
    has already decided a genuinely different, STABLE list is worth asking
    for. Carried all the way to list_fetch.process_fetched_list_zip() so the
    completion handler knows which kind of fetch this was without having to
    guess from `channel_raw` after the fact - guessing is what let a bot
    held from before #1232 (no channel on record at all) look "secondary" on
    EVERY ordinary refresh forever, a real incident caught on review.

    Deliberately reuses dcc_fetch.enqueue_fetch() (extended with a
    request_type parameter) rather than build_fetch_enqueue_result() above:
    that function's shape - a {"bot","filename"} object or a list of them -
    is the file-fetch multi-select shape the Search view's "Download
    selected" and the Download tab's bulk-paste box both produce, and a
    list-fetch request has no filename at all (we do not know what the
    target bot will name its list zip - see dcc_fetch.py's module docstring
    for why request_type="list" matches on bot alone). Bolting an optional
    "type" field onto that shape would make every existing caller of it - and
    every existing test of it - reason about a case that never applies to
    them. The two HTTP-facing validators stay separate; the actual queue,
    dispatcher, admission control, size cap and transfer code they both feed
    into is the exact same one, all the way through (see dcc_fetch.py).

    Returns (http_status, payload_dict) with "created" (the new request id,
    as a one-element list, so the frontend can treat this the same shape as
    the file-fetch enqueue response) - or 409 if this bot already has a
    "list" or "folder" request outstanding (see
    dcc_fetch.has_outstanding_bot_alone_request()'s docstring for why the
    two request_types can never safely coexist for the same bot: neither
    convention's response filename is predictable ahead of time, so both
    match incoming DCC SEND offers on bot alone, and a second one racing the
    first would create an offer no admission-control branch could correctly
    attribute).
    """

    unavailable = fetch_feature_error()
    if unavailable:
        return 503, {"error": unavailable}
    import dcc_fetch

    bot_err = reject_if_unsafe_for_irc_line(bot_raw, "bot")
    if bot_err:
        return 400, {"error": bot_err}

    bot = bot_raw.strip()
    if not bot:
        return 400, {"error": "'bot' is required."}

    channel_raw = str(channel_raw or "")
    channel_err = reject_if_unsafe_for_irc_line(channel_raw, "channel") if channel_raw.strip() else None
    if channel_err:
        return 400, {"error": channel_err}
    channel = channel_raw.strip() or None
    secondary = bool(secondary_raw)
    if channel is None and not secondary:
        channel = held_list_channel(bot)

    absent = bot_not_here_error(bot)
    if absent:
        return 409, {"error": absent}

    if dcc_fetch.has_outstanding_bot_alone_request(bot):
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}

    request_id = dcc_fetch.enqueue_fetch(bot, "", request_type="list", channel=channel,
                                         secondary_channel=secondary)
    if request_id is None:
        # Defense in depth: enqueue_fetch() enforces this same invariant
        # itself (see its docstring), so this should be unreachable given
        # the pre-check just above - but never surface it as a fabricated
        # success if some future race or caller change makes it reachable.
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}
    return 200, {"created": [request_id]}


def build_folder_rar_fetch_enqueue_result(bot_raw, folder_raw, marker_raw=None, channel_raw=None):
    """POST /api/filelists/fetch-folder-rar's pure logic: validate the bot
    nick and folder path, then enqueue a request_type="folder" row asking
    that bot to pack the whole folder/album as a .rar via its own "!rar"
    convention (see dcc.py's own "!rar" handler, which this mirrors - the
    same wire syntax this bot itself answers to on its own nick) and receive
    it back through the exact same fetch-queue admission control, dispatcher,
    size cap and transfer code every other cross-bot fetch already goes
    through (see dcc_fetch.py).

    Unlike build_list_fetch_enqueue_result()'s `bot_raw` alone, this route
    has a second attacker-reachable argument - `folder_raw` becomes real
    content on the wire ("!<bot> !rar <folder>"), not an absent filename like
    "list" - so both fields are run through reject_if_unsafe_for_irc_line()
    here, the same as build_fetch_enqueue_result()'s bot/filename pair.

    Returns (http_status, payload_dict) with "created" (the new request id,
    as a one-element list, matching the same response shape every other
    fetch-enqueue route already returns) - or 409 if this bot already has a
    "list" or "folder" request outstanding (see build_list_fetch_enqueue_
    result()'s docstring and dcc_fetch.has_outstanding_bot_alone_request()
    for why).
    """

    unavailable = fetch_feature_error()
    if unavailable:
        return 503, {"error": unavailable}
    import dcc_fetch

    bot_err = reject_if_unsafe_for_irc_line(bot_raw, "bot")
    if bot_err:
        return 400, {"error": bot_err}
    folder_err = reject_if_unsafe_for_irc_line(folder_raw, "folder")
    if folder_err:
        return 400, {"error": folder_err}

    bot = bot_raw.strip()
    folder = folder_raw.strip()
    if not bot or not folder:
        return 400, {"error": "Both 'bot' and 'folder' are required."}

    absent = bot_not_here_error(bot)
    if absent:
        return 409, {"error": absent}

    return _enqueue_folder_request(dcc_fetch, bot, folder,
                                   raw={"marker": marker_raw, "channel": channel_raw})


def build_folder_rar_batch_enqueue_result(payload):
    """POST /api/filelists/fetch-folder-rar with a LIST of {"bot","folder"}
    objects (#1233): what Download selected sends for the ticked rows of a RAR
    list. Every folder is queued, in the order given, and the dispatcher sends
    them one at a time per bot. Returns (status, {"created": [ids], "errors":
    [...]}); a body that created nothing carries the first error's status."""
    if not isinstance(payload, list) or not payload:
        return 400, {"error": 'Expected a non-empty list of {"bot": .., "folder": ..} objects.'}
    if len(payload) > FETCH_ENQUEUE_MAX_ITEMS:
        return 413, {"error": f"At most {FETCH_ENQUEUE_MAX_ITEMS} items per "
                              f"request; this one had {len(payload)}."}
    created = []
    errors = []
    first_status = 400
    for raw in payload:
        if not isinstance(raw, dict):
            errors.append({"error": "Each item must be an object with bot/folder.", "item": raw})
            continue
        status, result = build_folder_rar_fetch_enqueue_result(
            raw.get("bot", ""), raw.get("folder", ""), raw.get("marker", ""), raw.get("channel", ""))
        if status == 200:
            created.extend(result.get("created", []))
        else:
            if not errors:
                first_status = status
            errors.append({"error": result.get("error", ""), "item": raw})
    if not created and errors:
        return first_status, {"error": errors[0]["error"], "created": [], "errors": errors}
    return 200, {"created": created, "errors": errors}


# An mxrarserver pack row that could not be told apart from a request already
# waiting on that bot (#1209) - see dcc_fetch._pack_request_conflicts_locked().
PACK_FETCH_CONFLICT_ERROR = (
    "A list request, or a folder that would arrive under the same name, is "
    "already in progress for this bot - wait for it to finish before asking "
    "for this one."
)


def _preferred_fetch_channel(raw, bot, marker_field="marker"):
    """The channel a file/folder enqueue item should prefer (#1240): an
    explicit "channel" on the item itself wins first - a Downloads-page
    retry already knows exactly which channel its row went out in before
    and must keep asking there, not fall back to a marker lookup it has no
    marker for - then the item's own marker (held_list_channel()), then
    nothing, same fallback chain dcc_fetch._resolve_fetch_channel() applies
    from there regardless. A non-string or unsafe "channel" is ignored
    rather than rejecting the whole item over it - this is only ever a
    preference, never a requirement, so a bad value just loses the
    preference instead of losing the request too.
    """
    channel = raw.get("channel") if isinstance(raw, dict) else None
    if isinstance(channel, str) and channel.strip() and not reject_if_unsafe_for_irc_line(channel, "channel"):
        return channel.strip()
    return held_list_channel(bot, marker=(raw or {}).get(marker_field) if isinstance(raw, dict) else None)


def held_list_channel(bot, marker=None):
    """The channel the list we already hold for `bot` was fetched in, or None
    (#1232) - read by its nick alone so most callers (a re-fetch, a request
    with no marker of its own) need no extra field to carry.

    `marker` (#1240), when given, asks the narrower question: not "the bot's
    primary channel" but "the channel THIS marker's own list came from" -
    fetched_bot_lists[bot]["lists"][marker]["channel"], checked FIRST. A
    secondary channel's own marker has a different channel than the bot's
    primary one by construction (that is what makes it secondary - see
    list_fetch._install_secondary_channel_lists()), so a request for a file or
    folder on a secondary marker's list that fell back to the primary's
    channel would go out in the wrong place - found live: a file ticked on a
    bot's "-VIDEO" marker's list went to the bot's ordinary channel, where
    the file both was never advertised. Falls back to the bot's own
    "channel" (today's rule) when no marker is given, the marker is "" (the
    primary itself), or that marker is not actually held.

    None for a bot with no held list, or a marker whose own channel could
    not be resolved when it was fetched either - dcc_fetch._resolve_fetch_
    channel()'s own fallback chain applies from there, same as always.
    """
    store = getattr(config, "fetched_bot_lists", None) or {}
    entry = store.get(str(bot).strip().lower())
    if not isinstance(entry, dict):
        return None
    marker = str(marker or "").strip()
    if marker:
        info = (entry.get("lists") or {}).get(marker)
        if isinstance(info, dict) and info.get("channel"):
            return info["channel"]
    return entry.get("channel")


def _enqueue_folder_request(dcc_fetch, bot, folder, trigger=None, raw=None):
    """Enqueue one folder request: (http_status, payload_dict).

    TWO SHAPES (#1209). A "!rar" folder - DCCore's and its relatives' - is
    asked for as "!<bot> !rar <folder>" and admitted on the bot alone, so
    only one may be outstanding per bot. An mxrarserver pack row is asked for
    exactly as its list wrote it, "!<trigger> <path>.rar", with no "!rar",
    and its RAR is admitted by the name it will arrive under, so several may
    wait together as long as those names cannot clash.

    `raw` (#1240) is the original item dict, read by _preferred_fetch_
    channel() for an explicit "channel" (a Downloads-page retry, which
    already knows exactly which channel this row went out in before) or a
    "marker" (a List Browser request, which knows which of the bot's lists
    the row came from but not its channel directly) - either way, never
    both at once in practice, and neither is required.
    """
    import list as list_mod

    if list_mod.pack_path_of(folder):
        if dcc_fetch.pack_request_conflicts(bot, folder):
            return 409, {"error": PACK_FETCH_CONFLICT_ERROR}
        request, conflict = folder, PACK_FETCH_CONFLICT_ERROR
    else:
        if dcc_fetch.has_outstanding_non_folder_request(bot):
            return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}
        request, conflict = f"!rar {folder}", BOT_ALONE_FETCH_CONFLICT_ERROR

    request_id = dcc_fetch.enqueue_fetch(bot, request, request_type="folder", trigger=trigger,
                                         channel=_preferred_fetch_channel(raw or {}, bot))
    if request_id is None:
        # Defense in depth - see build_list_fetch_enqueue_result()'s
        # identical comment above.
        return 409, {"error": conflict}
    return 200, {"created": [request_id]}


def fetch_marks_by_bot():
    """{bot: {filename: "requested"|"received"}}, both keys lower-cased.

    What #133 calls "mark what you already requested", and it wants two states
    rather than one:

      requested  the row is still in flight - dcc_fetch groups exactly these
                 as _UNRESOLVED_FETCH_STATES (pending, offered, listening,
                 receiving), and reusing that tuple is what stops this drifting
                 the day a state is added there.
      received   complete.
      nothing    FAILED, on purpose. A failed fetch is not a thing you have,
                 and marking it would discourage the one useful action left,
                 which is to ask again.

    Only file requests are marked. A "list" or "folder" row is a request for
    the whole list or a packed archive, not for any row shown in the table, so
    marking a filename from one would claim something that never happened.
    """
    import dcc_fetch

    # Under the SAME LOCK the writers use, exactly as
    # build_fetch_status_payload() does: enqueue_fetch() inserts rows from the
    # transfer thread while this runs on Flask's, and iterating a dict being
    # inserted into raises "dictionary changed size during iteration" - which
    # here is a 500 on the List Browser at the precise moment somebody starts
    # a fetch, which is the moment they are most likely to be looking at it.
    with dcc_fetch._fetch_lock():
        queued = [dict(row) for row in (getattr(config, "fetch_queue", {}) or {}).values()
                  if isinstance(row, dict)]

    marks = {}
    for row in queued:
        if not isinstance(row, dict):
            continue
        if row.get("request_type") != "file":
            continue
        bot = str(row.get("bot") or "").strip().lower()
        filename = str(row.get("requested_filename") or "").strip().lower()
        if not bot or not filename:
            continue
        state = row.get("state")
        if state in dcc_fetch._UNRESOLVED_FETCH_STATES:
            mark = "requested"
        elif state == "complete":
            mark = "received"
        else:
            continue
        # "received" wins over "requested" for the same file: asking twice and
        # having it once is a thing you HAVE, and that is the more useful of
        # the two answers to show.
        if marks.setdefault(bot, {}).get(filename) != "received":
            marks[bot][filename] = mark
    return marks


def mark_rows_with_fetch_state(rows, marks, bot=None):
    """Stamp each row with what we have already asked that bot for.

    `bot` when every row belongs to one list (a fetched bot's page); left out
    when they do not (the cross-list filter), where each row's own "source"
    says which bot it came from.

    Rows with no mark get one anyway, as "". The frontend then has one field to
    read rather than having to know whether its absence means "not requested"
    or "this payload does not carry marks".
    """
    for row in rows:
        owner = str(bot if bot is not None else row.get("source", "")).strip().lower()
        name = str(row.get("title") or "").strip().lower()
        row["mark"] = marks.get(owner, {}).get(name, "")
    return rows


def build_crosslist_search_payload(term, limit=None, online_only=False):
    """GET /api/filelists/search: one term against every list we hold.

    Returns the SAME shape as GET /api/filelists - {"folders": [...]} of
    {"folder", "count", "entries"} - plus "matched"/"empty" for the sidebar.
    Deliberately the same: the browser's folder rendering, its checkboxes and
    its "Download selected" all work on that shape already, and the row's
    "source" field is the bot, so selecting across several lists at once needs
    no new plumbing. A second row shape here would have been a second renderer
    and a second selection path to keep in step.

    GROUPED BY BOT AND FOLDER, not by folder alone. list.group_rows_by_folder()
    keys on the folder string, which is right for one list and wrong across
    several: two bots can easily both have "D:\\MUSIC\\Metallica\\", and
    merging those would put one bot's files under another's heading and point
    the folder's !rar button at whichever bot happened to be first.

    "matched" and "empty" are asked separately rather than derived from the
    rows, because the page is capped: a bot whose matches all fall past the cap
    would look empty when it is not. See list_index.bots_with_a_match() for why
    the second question is cheap enough to ask on every keystroke.

    Scoped to the lists actually held, never to whatever is in the index. The
    two can drift - a list file removed by hand, a reset store - and a row for
    a list we no longer have offers a file that cannot be requested.

    `online_only` (#926, AutoGet's "Online Only"): only the lists of bots in
    one of our channels right now - the ones a request can reach today. The
    others are reported with the empty ones, so the sidebar dims them.
    """
    import list as list_mod
    import list_index

    import list_fetch

    # PHRASES, not loose words - see list_index.filter_segments(). What the
    # operator typed is one phrase unless they separated it with "*", and the
    # page needs the same pieces to highlight what matched, so they are
    # parsed once here and returned in the payload rather than parsed again
    # in JavaScript.
    terms = list_index.filter_segments(term)

    # EVERY LIST, not every bot. A bot's archive can hold several - its loose
    # files and its packed albums, or music and film - and each is indexed
    # under its own name, "<nick>" for the main one and "<nick>/<marker>" for
    # the rest. Asking about the NICK alone had two consequences, both
    # reported from a beta looking at a bot with two lists:
    #
    #   * the other lists were never searched, so a match inside one could
    #     not be found by the filter at all; and
    #   * they came back in neither `matched` nor `empty`, and the sidebar
    #     only dims what it was TOLD is empty - so a list with no matches was
    #     left bright, reading as the one list that had them.
    #
    # These keys are exactly the ones build_fetched_bot_list_summaries()
    # gives its rows, which is what makes the dimming line up.
    held = {}
    for key, entry in (dict(getattr(config, "fetched_bot_lists", {}) or {})
                       ).items():
        name = str(entry.get("bot") or key).strip()
        if not name:
            continue
        stored = entry.get("lists")
        markers = (list(stored) if isinstance(stored, dict) and stored
                   else [""])
        for marker in markers:
            source = list_fetch.index_key(name, marker)
            held[source.lower()] = source

    offline = []
    if online_only:
        # PRESENT UNDER THE NICK ITS ROW IS SHOWN AS (#975). A bot #376 merged
        # into its new nick keeps its list under the old one, which is not
        # here by definition - asking about that nick alone left out exactly
        # the renamed bots that are online and reachable.
        present = present_nicks()
        merges = _ident_merges(dict(getattr(runtime, "known_bots", {}) or {}), present)
        for key, source in list(held.items()):
            nick = source.split("/", 1)[0]
            if nick.lower() in present or _display_nick(nick, present, merges).lower() in present:
                continue
            offline.append(key)
            del held[key]

    empty_payload = {
        "term": str(term or ""),
        "terms": terms,
        "folders": [],
        "total": 0,
        "total_files": 0,
        "returned": 0,
        "truncated": False,
        "matched": [],
        "empty": sorted(list(held) + offline),
    }
    if not terms or not held:
        return empty_payload

    # THE INDEX IS STILL BEING FILLED (#1416): the startup backfill has not
    # reached every list yet. Said, not waited for - and no held list is
    # called empty, which the sidebar would dim as "no match" when the
    # index simply has not got to it. Only the offline ones, as always.
    if list_index.index_filling():
        payload = dict(empty_payload)
        payload.update({"empty": sorted(offline), "filling": True})
        return payload

    names = list(held.values())
    rows = list_index.search(terms, limit=limit, bots=names)
    matched, empty = list_index.bots_with_a_match(terms, names)
    empty = list(empty) + offline

    # One group per (bot, folder), in the order the index returned them so
    # that a bot's own list order survives rather than being re-sorted into
    # one the operator does not recognise.
    order = []
    groups = {}
    for row in rows:
        # The index stores a bot under one NORMALISED key, so that a refetch
        # under different capitalisation replaces the list rather than
        # doubling it. The operator should still see the nick as that bot
        # spells it, and the fetch should be addressed that way - so the
        # display name comes back from `held`, which is keyed the same way and
        # holds the real spelling.
        bot = held.get(str(row.get("bot") or "").strip().lower(),
                       str(row.get("bot") or ""))
        folder = str(row.get("folder") or "")
        key = (bot.lower(), folder.lower())
        group = groups.get(key)
        if group is None:
            group = {"folder": folder, "bot": bot, "count": 0, "entries": []}
            groups[key] = group
            order.append(key)
        filename = str(row.get("filename") or "")
        group["entries"].append({
            "title": filename,
            "size": str(row.get("size") or ""),
            "format": os.path.splitext(filename)[1].lstrip(".").upper(),
            # The BOT, which is what makes cross-list selection work: the
            # checkbox takes its bot from here, so a page of results from
            # four different bots queues correctly without the frontend
            # knowing anything about which list a row came from.
            "source": bot,
            "folder": folder,
        })
        group["count"] += 1

    marks = fetch_marks_by_bot()
    for group in groups.values():
        mark_rows_with_fetch_state(group["entries"], marks)

    folders = [groups[key] for key in order]
    payload = dict(empty_payload)
    payload.update({
        "folders": folders,
        "total": len(folders),
        "total_files": sum(group["count"] for group in folders),
        "returned": len(folders),
        # The cap was reached, so there are probably more. Said plainly rather
        # than implied by a count the reader would need to know the cap to
        # interpret.
        "truncated": len(rows) >= (limit or list_index.DEFAULT_SEARCH_LIMIT),
        "matched": sorted(matched),
        "empty": sorted(empty),
    })
    return payload


# The source key for our own lists. "__own__" alone still means the PRIMARY,
# which is what GET /api/filelists with no ?list= has always returned - so
# every existing caller, and every install that serves one list, is unchanged.
OWN_SOURCE = "__own__"


def own_list_source(served_list):
    """The List Browser source key for one of OUR served lists.

    The primary keeps the bare "__own__" it has always had. Naming it as well
    would have been tidier and would have changed the meaning of a URL that
    predates lists having names at all.
    """
    if served_list.primary:
        return OWN_SOURCE
    return f"{OWN_SOURCE}:{served_list.name}"


def own_list_name(source):
    """The served-list name a source key refers to, or None if it is not ours.

    None for the bare "__own__" too: that means the primary, and the primary
    is what every list function already resolves `name=None` to. Returning its
    name instead would be the same answer by a longer route, and would break
    the moment an operator renamed it.
    """
    text = str(source or "")
    if text == OWN_SOURCE:
        return None
    if text.startswith(OWN_SOURCE + ":"):
        return text[len(OWN_SOURCE) + 1:] or None
    return None


def source_is_ours(source):
    text = str(source or "")
    return text == OWN_SOURCE or text.startswith(OWN_SOURCE + ":")


def requested_own_list(name):
    """Validate a ?list= against the lists we actually serve.

    Returns the served list's own name, or None for "the primary" - which
    covers an absent parameter and a name we do not serve alike. Deliberately
    not an error: this route is polled continuously, and a list renamed or
    removed between one poll and the next would otherwise turn the table into
    an error message without anybody having touched it.

    It also keeps an arbitrary string away from list.find_latest_list(), which
    joins the name into a directory path.
    """
    wanted = str(name or "").strip()
    if not wanted:
        return None
    try:
        import library
        for entry in library.lists():
            if entry.name == wanted:
                return None if entry.primary else entry.name
    except Exception as err:
        print(f"[WEBUI] Could not resolve the requested list: {err}")
    return None


def present_nicks():
    """Every nick the daemon can currently see, lower-cased.

    Synced from 353/JOIN/PART for every channel this bot is in - the same
    mirror dcc.py has read as proof of presence before dispatching a send
    since long before this. An empty set means we have not finished joining
    yet, which callers have to tell apart from "nobody is here".
    """
    with runtime.channel_users_lock():
        return {str(nick).lower()
                for users in (getattr(config, "channel_users", {}) or {}).values()
                for nick in users}


def build_own_list_summaries():
    """One row per list THIS bot serves, in the operator's own order.

    FOUND IN A BETA. An operator built a second list through the dashboard,
    put every film in it, and then could not find it: "in list browser i dont
    see all my lists ... video list isnt there". The list was being served
    correctly and advertised correctly in its own channel - only the page that
    browses lists stopped at the primary, because build_filelists_payload()
    resolved `name=None` and the sidebar hard-coded a single row.

    Multi-list is this release's headline feature, and the dashboard is where
    the operator created the list. A page that offers to make a thing and then
    will not show it is a round trip that does not close.

    LABELLED "Our own list" WHEN THERE IS ONLY ONE. Every install today has
    exactly one, called "Main" by default - a name the operator never chose
    and has no reason to recognise. Showing it would be a change with no
    information in it. With several, each carries its own name, which is
    exactly what the operator typed.

    No count: it would mean parsing every list on every poll of this route,
    which the File Lists tab makes every few seconds, on libraries measured in
    hundreds of thousands of files. The row existing at all is the fix.
    """
    import library

    try:
        served = library.lists()
    except Exception as err:
        # This route is polled continuously; a malformed lists.json must cost
        # the extra rows, not the whole sidebar.
        print(f"[WEBUI] Could not read the served lists: {err}")
        served = []

    if not served:
        return [{"bot": OWN_SOURCE, "nick": OWN_SOURCE, "list": "",
                 "label": "Our own list", "held": True, "freshness": "own",
                 "online": True,
                 "own": True, "fetched_at": 0, "count": None,
                 "advert_then": {}, "advert_now": {}}]

    single = len(served) == 1
    rows = []
    for entry in served:
        rows.append({
            "bot": own_list_source(entry),
            "nick": own_list_source(entry),
            "list": "",
            "label": "Our own list" if single else entry.name,
            "held": True,
            "online": True,
            "freshness": "own",
            "own": True,
            "fetched_at": 0,
            "count": None,
            "advert_then": {},
            "advert_now": {},
        })
    return rows


def build_fetched_bot_list_summaries():
    """GET /api/filelists/bots payload: one row per source the List Browser
    can show, for the list of bots above its table.

    Two kinds, told apart by "held". A bot whose list we hold can be browsed,
    and carries a real freshness verdict and the count we parsed at fetch
    time. A bot we have only seen advertising cannot - it is here because
    #133 makes "not downloaded" one of the states the list shows, and its
    count is what THAT BOT claims, absent when it did not say.

    "count" on a held row reads the "entry_count" field process_fetched_list_zip() computes
    ONCE at fetch time (issue #76, option 2) rather than the actual row list -
    which is no longer stored at all - so this stays a cheap dict lookup even
    though the File Lists tab's switcher polls this route every
    FILELISTS_BOTS_POLL_MS (see web/app.js), for every fetched bot, forever.
    Re-parsing each bot's whole list from disk just to report a count on every
    poll would defeat much of the point of no longer retaining it in memory.
    """
    import runtime

    store = dict(getattr(config, "fetched_bot_lists", {}) or {})
    known = dict(getattr(runtime, "known_bots", {}) or {})

    rows = []
    import list_fetch

    # WHO IS ACTUALLY THERE, built ONCE for the whole payload. This route is
    # polled every few seconds and a busy channel has dozens of advertisers;
    # asking user_is_present_in_ram() per row would rescan every channel's
    # membership per row, per poll.
    present = present_nicks()
    # #376 option B, also once per payload: which absent nicks are the same
    # bot as one that is here, by ident.
    merges = _ident_merges(known, present)

    for key, entry in store.items():
        bot = entry.get("bot", key)
        then = dict(entry.get("advert_when_fetched") or {})
        now = _advert_now(known, bot)
        freshness = _freshness(then, now)

        # ONE ROW PER LIST IN THE ARCHIVE. A peer routinely publishes more
        # than one - loose files and packed albums, or music and films - and
        # until the fetch kept them all, everything but the largest was
        # discarded on the way in. `lists` is absent on an entry stored before
        # that, and a one-list archive is still the ordinary case, so the
        # fallback here is the single row this always produced.
        held = entry.get("lists")
        if not isinstance(held, dict) or not held:
            held = {"": {"entry_count": entry.get("entry_count", 0)}}

        # The main list first, then the rest by name - a stable order, and one
        # that keeps a bot's principal list where the eye already looks.
        for marker in sorted(held, key=lambda m: (m != "", str(m).lower())):
            info = held[marker] if isinstance(held[marker], dict) else {}
            rows.append({
                # The IDENTITY, which is no longer just a nick: a bot's other
                # lists are "<nick>/<marker>". Everything that acts on the BOT
                # rather than the list - re-fetching, packing a folder, the
                # freshness verdict - reads "nick" below instead.
                #
                # "bot" stays the REAL nick this list is actually keyed under
                # - re-fetching, purging and the online check all still target
                # it untouched. "nick" is the one field #376's alt-nick
                # merge may substitute, and only for grouping/labelling the
                # sidebar row - see runtime.resolve_display_nick()'s own
                # comment for why nothing else is allowed to change here.
                "bot": list_fetch.index_key(bot, marker),
                "nick": _display_nick(bot, present, merges),
                "list": marker,
                "label": f"{bot} - {marker}" if marker else bot,
                "held": True,
                # PRESENCE, which is a different question from freshness and
                # was being answered by the same dot. A list can be perfectly
                # current from a bot that signed off an hour ago, and asking
                # that bot for anything is a request nobody will see.
                #
                # None, not False, when we know nothing yet: an empty
                # membership mirror is a bot still joining, and "offline" is a
                # claim we have not earned.
                "online": (bot.lower() in present) if present else None,
                "fetched_at": entry.get("fetched_at", 0),
                "count": info.get("entry_count", 0),
                # PER BOT, not per list. One advert covers the archive and one
                # "@<nick>" fetches all of it, so every list a bot published
                # is exactly as fresh as the fetch that brought them.
                "freshness": freshness,
                "advert_then": then,
                "advert_now": now,
                "advert_live": _advert_live(known, bot, present),
                # Fetched and not opened since (#926 item 6). Only an entry
                # that carries seen_at - one fetched since this existed.
                "unseen": ("seen_at" in entry
                           and float(entry.get("seen_at") or 0) < float(entry.get("fetched_at") or 0)),
            })

    # AND THE BOTS WE HAVE ONLY SEEN ADVERTISING. #133's colour rule makes
    # "never downloaded" one of the three states, so the list has to contain
    # bots there is no list for - that is the whole point of showing it in
    # red. They cannot be browsed, and the page says so rather than offering
    # a source that would come back empty.
    #
    # `count` is what THEY advertise, not something we parsed, and is absent
    # when they did not say - the same "did not say is not zero" rule the
    # freshness comparison follows.
    for key, entry in known.items():
        if key in store or not isinstance(entry, dict):
            continue
        bot = entry.get("nick") or key
        now = _advert_now(known, bot)
        rows.append({
            "bot": bot,
            "nick": _display_nick(bot, present, merges),
            "list": "",
            "label": bot,
            "held": False,
            # Named by the operator rather than seen advertising (#376):
            # the page marks it, and offers to forget it.
            "hand_entered": bool(entry.get("hand_entered")),
            "advert_live": _advert_live(known, bot, present),
            "online": (bot.lower() in present) if present else None,
            "fetched_at": 0,
            "count": now.get("files"),
            "freshness": "not_held",
            "advert_then": {},
            "advert_now": now,
        })

    rows.sort(key=lambda row: str(row["bot"]).lower())
    return rows


def _ident_merges(known, present):
    """{departed nick (lower): current nick} for a bot seen again under
    another nick, by its ident (#376, option B). Display only.

    All of these have to hold, and each one fails safe - no merge, the rows
    stay two:

      * the old nick's departure was OBSERVED (a QUIT, PART, KICK or NICK -
        runtime.bot_departures), and it is not here now;
      * the new nick is here now;
      * both have the SAME IDENT, seen this session (runtime.bot_idents,
        memory only - no host or IP is kept at all);
      * both advertise the SAME FILE COUNT - two libraries almost never
        match to the file;
      * they were never ADVERTISING at the same time: the old nick's last
        advert came before the new nick was first seen this session. A ghost
        still sitting in the channel after its connection died cannot
        advertise, so the ordinary alt-nick reconnect passes this, and two
        live bots that happen to share an ident and a count do not;
      * the departure and the new nick's first sighting are within
        irc.IDENT_MERGE_WINDOW_SECONDS of each other, either way round - a
        ghost pings out after its owner is back, a clean QUIT comes before.
        Hours apart, the same ident and count are left as a coincidence;
      * exactly one current nick matches. Two candidates is not an answer.
    """
    import irc

    with runtime.bot_idents_lock:
        idents = {key: dict(record) for key, record in runtime.bot_idents.items()}
        departed = dict(runtime.bot_departures)
    if not present or not departed:
        return {}
    window = irc.IDENT_MERGE_WINDOW_SECONDS

    def files_of(key):
        files = (known.get(key) or {}).get("files")
        return files if isinstance(files, int) and not isinstance(files, bool) else None

    here = {}
    for key, record in idents.items():
        files = files_of(key)
        if key in present and files is not None:
            nick = (known.get(key) or {}).get("nick") or key
            here.setdefault((record.get("ident"), files), []).append(
                (key, nick, float(record.get("first_seen") or 0)))

    merges = {}
    for key, left_at in departed.items():
        record = idents.get(key)
        files = files_of(key)
        if key in present or record is None or files is None:
            continue
        # Never itself: a nick that came back matches its own record.
        matches = [m for m in here.get((record.get("ident"), files), []) if m[0] != key]
        if len(matches) != 1:
            continue
        _other, nick, first_seen = matches[0]
        last_advert = float((known.get(key) or {}).get("last_seen") or 0)
        if last_advert >= first_seen:
            continue
        if abs(first_seen - float(left_at or 0)) > window:
            continue
        merges[key] = nick
    return merges


def _display_nick(bot, present, merges=None):
    """resolve_display_nick(), minus any alias the network is CURRENTLY
    disproving.

    #376's alt-nick merge bounds how long it TRUSTS a departure
    (ALT_NICK_RECONNECT_WINDOW_SECONDS in irc.py) but not how long the
    resulting alias is APPLIED: runtime.nick_aliases never expires, so a
    merge made from good evidence fifteen seconds ago is still asserted
    indefinitely afterwards - including at a moment the network has since
    disproved it.

    Two nicks present at the same instant are two connections; one bot
    cannot be both. So if the alias's primary and the row's own real nick
    are BOTH currently online, that is not weaker evidence than the
    departure/rejoin pattern that created the alias - it is evidence of the
    opposite, arriving later, and it is preferred. `present` is already
    computed once for the whole payload, so this costs nothing extra and
    stays exactly as "display only" as resolve_display_nick() itself: it
    changes what one row is grouped under, never fetched_bot_lists,
    known_bots, or a download counter.
    """
    primary = runtime.resolve_display_nick(bot)
    # #376 option B: no alias from a NICK or a reconnect, but the same bot by
    # ident - see _ident_merges(). Only ever maps a nick that is NOT here to
    # one that is, so the presence check below never needs to undo it.
    if primary == bot and merges:
        return merges.get(str(bot).lower(), bot)
    if primary == bot or not present:
        return primary
    if primary.lower() in present and bot.lower() in present:
        return bot
    return primary


def build_add_source_result(bot_raw):
    """POST /api/filelists/sources payload: (http_status, payload_dict).

    A BOT THAT NEVER ADVERTISES IS INVISIBLE (#376, part 2). The List
    Browser's sidebar is built from adverts we have seen - runtime.known_bots
    - so a bot that answers "@nick" perfectly well but does not advertise on
    a channel we are in has no row, and there was no way to fetch its list
    from the dashboard at all. This lets the operator name one.

    It goes into the SAME registry, flagged "hand_entered", rather than a
    second list: every reader of the sidebar (rows, freshness, presence, the
    fetch button, the alt-nick display) then works on it unchanged, and if
    the bot ever does advertise, irc._record_bot() merges the advert into
    the same entry and the flag survives. What the flag changes is only
    irc._prune_known_bots(): an advert-only entry is forgotten a week after
    its last advert, and a hand-entered one has no adverts to age on, so it
    stays until the operator forgets it (build_remove_source_result).

    Presence and freshness are not claimed here: the row shows the grey
    "cannot tell" until an advert or a fetch says otherwise, exactly as an
    advert-only row does, and the fetch itself still goes through
    bot_not_here_error() like every other.
    """
    import irc

    err = reject_if_unsafe_for_irc_line(bot_raw, "bot")
    if err:
        return 400, {"error": err}
    bot = str(bot_raw).strip()
    if not bot:
        return 400, {"error": "'bot' is required."}
    if not _looks_like_a_nick(bot):
        return 400, {"error": "'bot' must be a nick: letters, digits and "
                              "the usual nick punctuation, no spaces, not "
                              "starting with a channel prefix."}
    if bot.lower() == str(getattr(config, "NICKNAME", "") or "").lower():
        return 400, {"error": "That is this bot's own nick."}

    # No lock, the same as irc._record_bot(): a copy-merge-rebind, where the
    # rebind is one dict store under the GIL, and every reader takes its own
    # dict() snapshot first.
    key = bot.lower()
    entry = dict(runtime.known_bots.get(key) or {})
    already = bool(entry)
    entry.setdefault("nick", bot)
    entry.setdefault("last_seen", 0)
    entry["hand_entered"] = True
    runtime.known_bots[key] = entry
    # Said when the row did not reach the disk (#691): it is in the registry
    # and on the page, and gone at the next restart unless a later flush
    # lands - a 200 that said nothing left the operator believing otherwise.
    if not irc._flush_known_bots(force=True):
        return 200, {"added": entry["nick"], "already_known": already,
                     "warning": "Added, but the bot registry could not be written to disk "
                                "- the row is kept until the next restart unless a later "
                                "save succeeds. Check the daemon log and the data directory."}
    return 200, {"added": entry["nick"], "already_known": already}


def build_remove_source_result(bot_raw):
    """POST /api/filelists/sources/<nick>/remove payload.

    Only a HAND-ENTERED row can be forgotten here, and only while no list is
    held for it. A bot that advertises is not the operator's to forget - it
    would be back at its next advert, and pretending otherwise is a row that
    reappears - and a held list is forgotten by the purge routes, which know
    about requests in flight. So this answers 409 for both, and says which.
    """
    import irc

    err = reject_if_unsafe_for_irc_line(bot_raw, "bot")
    if err:
        return 400, {"error": err}
    bot = str(bot_raw).strip()
    key = bot.lower()
    if not key:
        return 400, {"error": "'bot' is required."}
    if key in (getattr(config, "fetched_bot_lists", {}) or {}):
        return 409, {"error": f"A list is held for {bot}; purge that first."}
    entry = runtime.known_bots.get(key)
    if not isinstance(entry, dict):
        return 404, {"error": f"{bot} is not a known source."}
    if not entry.get("hand_entered"):
        return 409, {"error": f"{bot} is here because it advertises; it "
                              "would be back at its next advert."}
    runtime.known_bots.pop(key, None)
    if not irc._flush_known_bots(force=True):
        return 200, {"removed": bot,
                     "warning": "Removed, but the bot registry could not be written to disk "
                                "- the row is back at the next restart unless a later save "
                                "succeeds. Check the daemon log and the data directory."}
    return 200, {"removed": bot}


_NICK_RE = None


def _looks_like_a_nick(text):
    """RFC 2812's nick shape, loosely: a letter or one of the special
    characters first, then letters, digits, specials and '-'. Refuses a
    channel prefix and anything with whitespace - the value becomes a
    registry key and a PRIVMSG target."""
    global _NICK_RE
    import re
    if _NICK_RE is None:
        _NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,63}$")
    return bool(_NICK_RE.match(str(text)))


def build_purge_offline_fetched_lists_result():
    """POST /api/filelists/purge-offline payload: forget every held list
    whose bot is showing the List Browser's red dot right now.

    "Red dot" is `online is False` from build_fetched_bot_list_summaries()'s
    own rule - present_nicks() answered and this bot was not in it. A bot
    this daemon has not finished joining channels for yet reads `online:
    None` (the grey dot, "cannot tell yet"), and is left alone: an empty
    presence mirror is not evidence the bot is gone, it is evidence nobody
    has looked yet, and issue #385 asked for the red dot specifically.

    A bot with any request still outstanding (list, folder, or a plain file)
    is skipped even if it is showing red - see dcc_fetch.
    has_any_outstanding_request()'s docstring for why forgetting it under a
    reply in flight is unsafe, not just untidy.

    A currently-online bot is never a candidate at all, by construction: this
    only iterates config.fetched_bot_lists, which is what list_fetch.
    forget_bot() removes from, and never consults `known`/advert-only rows -
    there is nothing held for those to purge.
    """
    import dcc_fetch
    import list_fetch

    store = dict(getattr(config, "fetched_bot_lists", {}) or {})
    present = present_nicks()

    purged = []
    skipped_in_flight = []
    for key, entry in store.items():
        bot = entry.get("bot", key) if isinstance(entry, dict) else key
        online = (bot.lower() in present) if present else None
        if online is not False:
            continue
        if dcc_fetch.has_any_outstanding_request(bot):
            skipped_in_flight.append(bot)
            continue
        if list_fetch.forget_bot(bot):
            purged.append(bot)

    return 200, {"purged": purged, "count": len(purged),
                 "skipped_in_flight": skipped_in_flight}


def build_purge_all_fetched_lists_result():
    """POST /api/filelists/purge-all payload: forget EVERY held list, online
    or not - purge-offline above only ever touches the offline ones (#1260).

    For an operator upgrading from before #1232/#1240: a bot's list held
    from back then never had a real `preferred_channel` of its own, so the
    first ordinary re-fetch after upgrading resolves one from a weaker
    fallback (dcc_fetch._resolve_fetch_channel()'s advert-channel or
    first-configured-channel steps) - and for a peer that advertises the
    same list identically in several channels, whichever one that fallback
    happens to land on gets stamped and reused from then on. Found live: a
    folder request (and its retry) both went out in a channel the peer
    bot served nothing in, with no answer, because this had landed wrong
    once. Clearing every held list lets each one rebuild from scratch, with
    a channel resolved the same way a BRAND NEW bot's first fetch already
    is - correctly, by construction, since there is no stale stamp left to
    prefer over it.

    A bot with any request still outstanding is skipped, same safety
    purge-offline already applies and for the same reason - forgetting its
    list while an answer for it is in flight is unsafe regardless of
    whether this is clearing one bot or every bot.
    """
    import dcc_fetch
    import list_fetch

    store = dict(getattr(config, "fetched_bot_lists", {}) or {})

    purged = []
    skipped_in_flight = []
    for key, entry in store.items():
        bot = entry.get("bot", key) if isinstance(entry, dict) else key
        if dcc_fetch.has_any_outstanding_request(bot):
            skipped_in_flight.append(bot)
            continue
        if list_fetch.forget_bot(bot):
            purged.append(bot)

    return 200, {"purged": purged, "count": len(purged),
                 "skipped_in_flight": skipped_in_flight}


# JavaScript's Number.MAX_SAFE_INTEGER. Past this a JSON number no longer
# survives the trip into the page unchanged.
_MAX_SAFE_JS_INT = 2 ** 53 - 1


_ADVERT_LIVE_FIELDS = ("slots_free", "slots_in_use", "slots_total", "queued", "speed", "mode")


def _advert_live(known, bot, present):
    """The live figures `bot` last advertised - slots, queue, speed, mode -
    or {} (#926 item 8). Only for a bot that is online now: figures from a bot
    that left are about a moment that is over. Numbers too large to carry
    faithfully to JavaScript are left out, like _advert_now()'s."""
    if not present or str(bot).strip().lower() not in present:
        return {}
    entry = known.get(str(bot).strip().lower())
    entry = entry if isinstance(entry, dict) else {}
    live = {}
    for field in _ADVERT_LIVE_FIELDS:
        value = entry.get(field)
        if value in (None, ""):
            continue
        if isinstance(value, int) and abs(value) > 2 ** 53:
            continue
        live[field] = value if isinstance(value, int) else str(value)[:24]
    return live


def _advert_now(known, bot):
    """The fields `bot` is advertising at this moment, or {}.

    Same shape and same rule as list_fetch._advert_snapshot(): only what that
    bot actually published, and a missing key means "did not say".
    """
    # isinstance rather than `or {}`: a malformed entry is not falsy, and
    # dict(5) raises. The held rows read this too, so a hand-edited registry
    # would take the whole List Browser down and not just its own row.
    entry = known.get(str(bot).strip().lower())
    entry = entry if isinstance(entry, dict) else {}
    current = {}
    for field in ("files", "list_date"):
        value = entry.get(field)
        if value in (None, "", 0):
            continue
        # A COUNT WE CANNOT REPEAT FAITHFULLY IS ONE WE DO NOT REPEAT. This
        # is another bot's advert text, parsed with irc._as_int(), which
        # builds a Python int of any size at all. JSON has no such limit and
        # JavaScript does: JSON.parse() turns anything past 2**53 into the
        # nearest float before the page ever sees it, so a bot advertising
        # twenty-three digits had a DIFFERENT twenty-three digit number
        # rendered beside its nick, in thousands separators, looking exact.
        # Dropped rather than clamped: this module's rule throughout is that
        # an absent field means "they did not say", which is the truthful
        # reading of a number we cannot carry.
        if field == "files" and isinstance(value, int) and abs(value) > _MAX_SAFE_JS_INT:
            continue
        current[field] = value
    return current


def _freshness(then, now):
    """"current", "changed", or "unknown" for one held list.

    DATE FIRST, COUNT SECOND. #133 settled this against the obvious
    alternative: a count can coincidentally match after an edit, a date
    cannot - and 31 of the 32 bots observed advertising in one channel publish
    a date, so it is very nearly universal.

    "unknown" whenever either side is missing the field being compared, and
    that is not a hedge: we fetch from bots whose advert we have not seen, and
    a bot that publishes no date at all should show no freshness claim rather
    than an invented one. Saying "we cannot tell" is the honest answer and the
    page renders it as such.
    """
    # No early return for an empty advert: the loop below already reaches
    # "unknown" for one, because every field is missing on one side, every
    # iteration continues, and the fall-through says so. A guard here changed
    # no answer a test could see, which is what a mutation run showed.
    for field in ("list_date", "files"):
        before, after = then.get(field), now.get(field)
        if before in (None, "") or after in (None, ""):
            continue
        return "current" if str(before) == str(after) else "changed"

    return "unknown"


def _fetched_list_entry(entry, marker):
    """`entry` re-pointed at one of the bot's lists.

    A shallow copy with list_path and entry_count swapped for the chosen
    list's, so get_fetched_bot_page() needs to know nothing about markers - it
    reads the two fields it always read. Returns the entry unchanged for the
    main list, which is what those two fields already describe.
    """
    wanted = str(marker or "")
    if not wanted:
        return entry
    held = entry.get("lists")
    info = held.get(wanted) if isinstance(held, dict) else None
    if not isinstance(info, dict) or not info.get("list_path"):
        return entry
    picked = dict(entry)
    picked["list_path"] = info["list_path"]
    picked["entry_count"] = info.get("entry_count", 0)
    return picked


def build_fetched_bot_list_payload(nick, offset=0, limit=None, list_marker="", q=""):
    """GET /api/filelists/bot/<nick> payload: (http_status, payload_dict).

    Issue #76, options 2 and 3 together: the fetched bot's rows are no longer
    kept in memory at all (see list_fetch.process_fetched_list_zip()) - this
    reads one page of the stored list_path on every call, via
    list_fetch.get_fetched_bot_page(), which parses only that page's folders
    (#1128).
    "entries" is in the EXACT same row shape build_filelists_payload() returns
    for this bot's own list (both go through list.entries_to_filelist_rows()),
    so the frontend's File Lists table rendering needs no changes to display
    either one - only the data source (which endpoint it polled) differs.

    `q` is the same per-list search build_filelists_payload() takes (#399's
    follow-up) - split here, once, rather than inside get_fetched_bot_page(),
    so that function keeps taking pre-split words the way find_matching_entries()
    itself does everywhere else it is called.

    A 404 covers "never fetched" (no entry in config.fetched_bot_lists at
    all); a 502 covers "was fetched, but the on-disk file behind it can no
    longer be read" (get_fetched_bot_page() returned a non-None error - see
    its docstring) - both are failures a human reads on the dashboard, so the
    exact code matters less than that a route handler never lets either case
    raise an unhandled exception.
    """
    import list_fetch

    store = getattr(config, "fetched_bot_lists", {}) or {}
    entry = store.get(str(nick).strip().lower())
    if not entry:
        return 404, {"error": f"No fetched list is available for {nick!r} yet."}

    if limit is None:
        limit = FILELISTS_DEFAULT_PAGE_SIZE

    # WHICH OF THAT BOT'S LISTS. An archive holds more than one often enough
    # that a peer's albums or films used to be dropped on the way in; they are
    # all kept now, and this picks the one being browsed. An absent or unknown
    # marker resolves to the main list rather than erroring, for the same
    # reason ?list= does on our own: the sidebar is polled continuously and a
    # peer's next archive need not carry the same lists as the last.
    entry = _fetched_list_entry(entry, list_marker)

    search_words = split_list_search_words(q)
    page, total_folders, total_rows, row_capped, error = list_fetch.get_fetched_bot_page(
        entry, offset, limit, search_words=search_words)
    if error:
        return 502, {"error": error}

    # What we have already asked this bot for. One list, so the bot is passed
    # in rather than read per row - every row here belongs to it.
    marks = fetch_marks_by_bot()
    for group in page:
        mark_rows_with_fetch_state(group.get("entries", []), marks,
                                   bot=entry.get("bot", nick))

    return 200, {
        "bot": entry.get("bot", nick),
        "fetched_at": entry.get("fetched_at", 0),
        "folders": page,
        "total": total_folders,
        "total_files": total_rows,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "row_capped": row_capped,
    }
