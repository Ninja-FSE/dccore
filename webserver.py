# webserver.py - Web dashboard (Search / Queue / File Lists / cross-bot fetch).
"""A small, optional status page for the daemon.

FLASK IS OPTIONAL. The daemon has no external dependencies today, and CI never
installs Flask - so importing this module must never fail, and starting the
dashboard when Flask is missing must log and return, never crash the daemon.
That is why the import is guarded and why HAVE_FLASK exists.

The build_*_payload()/start_broadcast_search()/build_fetch_*() functions below
are pure: they read/write config (and, for search/file-lists, the master list
via list.py) and return plain dicts/lists or (status, dict) tuples. They never
import or touch flask, which is what lets tests/test_webserver.py exercise the
real data-shaping and mutation logic with plain unittest, no Flask install
required - keeping the "stdlib-only" property the rest of the test suite
relies on. create_app() and start() are the only things gated on HAVE_FLASK.

EVERY ROUTE REQUIRES A LOGIN, INCLUDING STATIC ASSETS. The dashboard used to
run with no authentication at all - a deliberate LAN-only decision, extended
on purpose even to /api/search/broadcast and /api/fetch/* despite those
routes mutating state (queuing an outbound IRC line, dialling an IP:port a
foreign bot supplies). That changed because WEBUI_HOST is no longer
guaranteed to stay LAN-only in practice: it shares one password with the DCC
CHAT admin console (config.ADMIN_PASSWORD_HASH, generated with `python
adminchat.py`) rather than a second credential to configure and forget about.
start() now refuses to run at all when that hash is unset - see the check
near the bottom of this file - so the dashboard is never reachable
unauthenticated, not even briefly on a fresh install.

The login route ("/login") is the ONLY exemption from the require_login()
before_request hook below; everything else, static files included, is behind
it. The session is a plain signed Flask cookie (app.secret_key, generated
fresh per process) - it does not survive a daemon restart, which is a
deliberate simplification: re-logging in after a restart costs nothing, and
it avoids a second secret to persist and protect. This is real
authentication, not the workaround kind the module docstring used to warn
against (an API key in a query string, a cookie nobody checks) - the password
is verified against ADMIN_PASSWORD_HASH via adminchat.verify_password() on
every login attempt, and nothing downstream trusts a request that has not
passed require_login(). Repeated failures from one address are temporarily
blocked (_note_bad_web_login()/_is_bad_web_ip(), same attempt-count and
block-duration policy as adminchat.py's own DCC CHAT console tracker, reused
from there directly) - in a POOL SEPARATE FROM adminchat.py's, on purpose:
since the password is shared, a shared block budget would let a web attacker
spend it down and lock the real operator out of the DCC console too.

What plain-HTTP session/password transmission still cannot fix: an attacker
already sharing the network segment can read the password and the session
cookie off the wire. The WEBUI_HOST comment in config.py's warning against
untrusted networks is about exactly that, and still applies with auth in
place - a password gate stops a stranger from finding the dashboard and
using it, not from a wire-level eavesdropper on a network the operator should
not have put this host on to begin with.

Nothing here is added to commands.py's CORE_MODULES. A !rehash reload
re-executes this module's body, which would try to re-bind a live listening
socket out from under app.run() - the same reasoning that already excludes
adminchat.py. Route handlers read config fresh via getattr() on every request
instead, so a rehash's new values (e.g. a changed WEBUI_* setting takes effect
only on the next daemon restart, but MAX_DCC_SLOTS, the queue, etc. are always
current) are visible without needing a reload of this module.
"""

import os
import sys
import threading
import time

import adminchat
import defaults as config
import platform_compat

try:
    from flask import Flask, jsonify, redirect, request, send_from_directory, session
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


# A browser tab is not an IRC channel: MAX_SEARCH_RESULTS (config.py, default 5)
# is sized to avoid flooding a channel and is the wrong number here. This is a
# module constant, not a config setting - it is a display cap on one page, not
# an operator-facing tunable like WEBUI_PORT.
WEBUI_MAX_SEARCH_RESULTS = 50

# Issue #76, option 3: GET /api/filelists and GET /api/filelists/bot/<nick>
# used to serialise every row of the list into one HTTP response (~36,208
# rows / ~12MB of JSON for this operator's own list, on every page view).
# These two constants bound how much of an already-parsed row list actually
# gets sent in one response - the parsing cost itself is unchanged (both
# endpoints already parsed everything every call; only what gets shipped over
# HTTP now differs).
#
# FILELISTS_DEFAULT_PAGE_SIZE (used when `?limit=` is omitted or invalid): 200
# rows is comfortably within the 100-300 range a plain HTML <table> renders
# instantly at, on any of this dashboard's supported screen sizes - a module
# constant, not a config.py tunable, same reasoning as WEBUI_MAX_SEARCH_RESULTS
# above: an internal display default, not an operator-facing knob.
FILELISTS_DEFAULT_PAGE_SIZE = 200

# FILELISTS_MAX_PAGE_SIZE: the ceiling `?limit=` is clamped to, regardless of
# what a caller asks for. Without this, a single `?limit=999999999` request
# would reintroduce exactly the problem this pagination feature exists to
# close - one response carrying the entire list again. 2000 is a generous 10x
# over the default (room for an operator who genuinely wants a bigger page,
# or a future "load more" control that fetches several pages at once) while
# still capping any one response to a small multiple of a real page, not the
# tens of thousands of rows a full list can contain.
FILELISTS_MAX_PAGE_SIZE = 2000

# The unit of `offset`/`limit` on both file-list routes is a FOLDER, not a row.
# Bots keep their libraries in folders and the dashboard groups by them, so a
# page that ends mid-album is a page that ends in the wrong place - and a
# folder is only useful expanded if all of it is there.
#
# The row ceiling that bounds such a page is list.FILELISTS_MAX_PAGE_ROWS,
# declared beside the paging function it bounds. It is not re-exported here:
# this module imports list lazily, inside the handlers, so that importing
# webserver.py does not drag in oserve/dcc/announce - which is what lets
# tests/test_webserver.py exercise these routes on their own.


def parse_pagination_params(raw_offset, raw_limit):
    """Turn `?offset=&limit=` query-string values - always strings, or None
    when the parameter was omitted entirely - into a validated (offset, limit)
    pair of ints, for GET /api/filelists and GET /api/filelists/bot/<nick>.

    Never raises: a missing, non-numeric, or negative value silently falls
    back to a sane default rather than erroring the route - the same "never
    trust a query parameter to already be a well-formed integer" discipline
    every other web-input boundary in this module already applies (see
    reject_if_unsafe_for_irc_line() above for the mutating-route equivalent).
    `limit` is additionally clamped to FILELISTS_MAX_PAGE_SIZE - see its
    comment for why - and a non-positive `limit` (0 or negative) is treated
    the same as "omitted", not as "give me nothing back".
    """
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0

    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = FILELISTS_DEFAULT_PAGE_SIZE
    if limit <= 0:
        limit = FILELISTS_DEFAULT_PAGE_SIZE
    if limit > FILELISTS_MAX_PAGE_SIZE:
        limit = FILELISTS_MAX_PAGE_SIZE

    return offset, limit


# #162 finding #13: reject_if_unsafe_for_irc_line() checked bytes only, never
# length - no route added its own cap either (start_broadcast_search enforces a
# MINIMUM term length and no maximum; the fetch-enqueue builders capped neither
# bot, filename nor folder). Every one of these values is eventually
# interpolated into a single raw outbound IRC line, and announce.IRC_LINE_BUDGET
# (420 bytes, the whole line) already exists as the real ceiling - a 5000-char
# search term queued a 5029-byte line; a 3000-char filename dispatched a
# 3032-byte PRIVMSG and left its own row stuck at "offered" until timeout with
# no indication why. Generous relative to any real value (a real IRC nick, file
# list entry or search phrase), and comfortably below IRC_LINE_BUDGET even
# before whatever fixed template text surrounds it at the actual emit site.
IRC_LINE_FIELD_MAX_LEN = 300

# FETCH_ENQUEUE_MAX_ITEMS: how many items one POST /api/fetch/enqueue body may
# carry. The route takes a list because multi-select is the point - but it took
# an unbounded one, and a single request could create thousands of rows.
#
# This is the payload-shaped half of the bound; dcc_fetch.MAX_UNRESOLVED_FETCHES
# is the queue-shaped half, and both are needed. Capping only the request lets N
# requests do what one could; capping only the queue accepts a 50,000-item body,
# validates every item, and only then discovers there was never room - having
# already spent the memory and the CPU that the cap exists to avoid.
#
# 500 clears every way the dashboard can actually produce a batch: the broadcast
# search table is capped at WEBUI_MAX_SEARCH_RESULTS (50), the file-list browser
# has no select-all so its batches are hand-ticked boxes, and the bulk-paste box
# is the only unbounded input - which is the one this is for.
FETCH_ENQUEUE_MAX_ITEMS = 500


def reject_if_unsafe_for_irc_line(value, field_name, max_len=IRC_LINE_FIELD_MAX_LEN):
    """Return an error string if `value` is not safe to interpolate into an
    outbound raw IRC line, or None if it is safe.

    Every mutating route in this module eventually hands web input to
    oserve.queue_message(), whose one job is to sit in a per-user list until
    queue_mgr.queue_worker() writes it straight to the live socket
    (`current_sock.send(msg.encode())`) - no re-validation, no re-splitting on
    the way out. A value that is not actually a string gets silently
    str()-coerced by a careless caller (see the bot/filename type-confusion
    bug this replaced), and a string containing an embedded \\r, \\n or \\x01
    lets the caller smuggle one or more ADDITIONAL raw IRC lines - QUIT,
    JOIN/PART an arbitrary channel, PRIVMSG/NOTICE as this bot - or close a
    CTCP wrapper early, past whatever single line/CTCP the route intended to
    send. All are rejected here, at the boundary, before the value goes
    anywhere near an outbound message.

    Also rejects a value longer than `max_len` - see IRC_LINE_FIELD_MAX_LEN's
    own comment for why an unbounded field is its own, separate bug: with no
    cap, a value long enough could make the actual emit site build a raw line
    that overflows the wire (the server truncates it, silently corrupting
    whatever trailing colour-reset code was there) or simply queue a request
    that can never succeed.

    The actual byte check is dcc_fetch.contains_unsafe_ctcp_bytes() - THE
    single canonical definition of "which bytes are unsafe for an outbound
    IRC/CTCP line" in this codebase (see that function's comment for why
    this used to be two independently-maintained copies, and no longer is).
    Imported locally, not at module level, to keep this module's "importing
    it must never fail, even with nothing but the stdlib installed" property
    (see the module docstring) independent of dcc_fetch's own import graph.

    Shared because it is already needed in >= 2 places (POST
    /api/search/broadcast's `term`, POST /api/fetch/enqueue's `bot` and
    `filename`, POST /api/filelists/fetch's `bot`) - any future route that
    builds an outbound IRC line from web input must run every such value
    through this too.
    """
    if not isinstance(value, str):
        return f"'{field_name}' must be a string."
    if len(value) > max_len:
        return f"'{field_name}' must be at most {max_len} characters."
    import dcc_fetch
    if dcc_fetch.contains_unsafe_ctcp_bytes(value):
        return f"'{field_name}' must not contain line breaks or control characters."
    return None


# ==========================================================================
# Pure data-building functions - no Flask, unit tested directly.
# ==========================================================================

def count_rar_album_folders():
    """How many album folders the RAR list offers, or None if there is no list.

    The file update_list.py writes opens with three lines of explanation and
    then one "!<nick> !rar <path>" row per folder, so the rows are exactly the
    lines starting with "!". Counting those rather than the file's length is
    what keeps the header out of the total.

    None rather than 0 when there is no list at all: a bot whose first list has
    not been built yet has an unknown album count, and zero is a different
    claim - it would read on the page as "this bot offers no albums".
    """
    import io

    directory = getattr(config, "LOCAL_LIST_DIR", "./lists")
    prefix = f"{getattr(config, 'LIST_BASE_NAME', 'DCCore')}-RAR-"
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(prefix) and name.endswith(".txt"))
    except OSError:
        return None
    if not names:
        return None

    try:
        with io.open(os.path.join(directory, names[-1]), encoding="utf-8",
                     errors="ignore") as handle:
            return sum(1 for line in handle if line.startswith("!"))
    except OSError:
        return None


def build_stats_payload():
    """Everything the Stats view shows, in one request.

    Every figure comes twice: the raw number, and the daemon's own rendering of
    it. The raw one is for anything that is not this page - a script, a future
    CTCP reply - which should not have to parse "1.21TB" back into an integer.
    The rendered one uses stats_mgr.format_speed(), format_size_human() and
    adminchat.format_uptime(), which are what the channel advert and the admin
    console already print, so the dashboard cannot disagree with the advert
    about how the same number reads.

    Nothing here takes a lock, matching build_queue_payload(): a shallow copy of
    the live containers is good enough for a status display and cannot deadlock
    against the daemon's own queue processing.

    Every source is guarded individually. This is a read-only status page and a
    missing stats file, an unbuilt list or a permissions error on lists/ must
    cost the tile that needs it and nothing else - a dashboard that 500s
    because one counter is unreadable is worse than one showing a gap.
    """
    # Imported here, not at module scope. tests/test_import_graph.py pins that
    # importing webserver.py pulls in none of the daemon, so that
    # tests/test_webserver.py can exercise every route without one running -
    # and db/list/stats_mgr are named in that test explicitly. Same reason the
    # File Lists payload imports `list` inside its own function.
    import db
    import list as list_mod
    import stats_mgr

    active = list(getattr(config, "active_transfers", []))
    queue = dict(getattr(config, "dcc_queue", {}))

    try:
        speed_now = int(stats_mgr.live_speed())
    except Exception:
        speed_now = 0

    try:
        record = int(db.get_speed_record())
    except Exception:
        record = 0

    try:
        uptime = int(stats_mgr.get_uptime_seconds())
    except Exception:
        uptime = 0

    # The 7-column row: total files, total bytes, yesterday's pair, today's
    # pair, and the date the day last rolled over. Read through
    # load_advanced_stats_rolled(), which rolls a COPY, so a bot that has sent
    # nothing since midnight does not show yesterday's figures labelled Today -
    # and so answering a GET does not write to disk.
    sent = {"total_files": 0, "total_bytes": 0, "today_files": 0,
            "today_bytes": 0, "yesterday_files": 0, "yesterday_bytes": 0}
    try:
        row = db.load_advanced_stats_rolled()
        names = ("total_files", "total_bytes", "yesterday_files",
                 "yesterday_bytes", "today_files", "today_bytes")
        for index, name in enumerate(names):
            try:
                sent[name] = int(str(row[index]).strip())
            except (IndexError, TypeError, ValueError):
                pass
    except Exception:
        pass

    library = {"files": 0, "size": None, "raw_bytes": 0, "list_date": None,
               "rar_folders": None}
    try:
        files, list_date, size, raw_bytes = list_mod.get_file_count_date_size_and_raw_bytes()
        library.update({"files": int(files or 0), "list_date": list_date or None,
                        "size": size or None, "raw_bytes": int(raw_bytes or 0)})
    except Exception:
        pass
    try:
        library["rar_folders"] = count_rar_album_folders()
    except Exception:
        pass

    for name in ("total", "today", "yesterday"):
        sent[name + "_text"] = stats_mgr.format_size_human(sent[name + "_bytes"])

    # Counted together, reported apart. A 700 MB album and a 4 MB track are not
    # comparable, so one merged table would rank by whichever kind this bot
    # happens to send more of - a fact about the library, not about demand.
    #
    # albums_enabled is config.RAR_ENABLED (#140). With folder packing off no
    # album can ever be sent, so the page says that instead of showing a table
    # that would stay empty for ever with no explanation. Any counts from
    # before it was switched off are still returned - they are history, and
    # deciding they never happened would be the wrong kind of tidy.
    top = {"files": [], "albums": [],
           "albums_enabled": bool(getattr(config, "RAR_ENABLED", True))}
    try:
        top["files"] = db.top_downloads(limit=10, kind="file")
        top["albums"] = db.top_downloads(limit=10, kind="album")
    except Exception:
        pass

    return {
        "top": top,
        "transfer": {
            "speed_now": speed_now,
            "speed_now_text": stats_mgr.format_speed(speed_now),
            "record": record,
            "record_text": stats_mgr.format_speed(record),
            "sending": len(active),
            "slots": int(getattr(config, "MAX_DCC_SLOTS", 0) or 0),
            "queued_files": sum(len(entries) for entries in queue.values()),
            "queued_users": len(queue),
            "uptime_seconds": uptime,
            "uptime_text": adminchat.format_uptime(uptime),
        },
        "sent": sent,
        "library": library,
        "version": str(getattr(config, "SCRIPT_VERSION", "")),
    }


def build_queue_payload(user=None):
    """The Queue view's data.

    With no `user`, one summary row per queued user (their status, a preview
    of the next file, and how many are waiting) - what the Queue table shows.
    With `user`, the full file list queued for that one user instead - what a
    click-through or `?user=<nick>` on /api/queue returns.

    No lock is taken, matching adminchat.py's _cmd_queue/_cmd_status idiom: a
    shallow dict()/list() copy of the live containers, read without a lock,
    is good enough for a status display and cannot deadlock against the
    daemon's own queue processing.
    """
    queue = dict(getattr(config, "dcc_queue", {}))
    frozen = dict(getattr(config, "frozen_queues", {}))
    active = list(getattr(config, "active_transfers", []))
    sending_users = {str(tx.get("user", "")).lower() for tx in active}

    if user:
        user_key = str(user).strip().lower()
        entries = queue.get(user_key, [])
        if user_key in sending_users:
            status = "sending"
        elif user_key in frozen:
            status = "frozen"
        elif entries:
            status = "queued"
        else:
            status = "empty"
        files = [e.get("file", "?") if isinstance(e, dict) else str(e) for e in entries]
        return {"user": user_key, "status": status, "count": len(entries), "files": files}

    # #220: a user sent with a free slot and nothing already queued never
    # enters dcc_queue at all - dcc.py's admission check appends straight to
    # active_transfers and returns (see start_dcc_send()). Iterating only
    # queue.items(), as this used to, left them off the Queue table entirely
    # while /api/stats (which reads active_transfers directly) showed the
    # transfer correctly - the two disagreeing about the same page. The
    # single-user branch above already got this right by checking
    # sending_users regardless of whether entries exist; this does the same.
    active_by_user = {}
    for tx in active:
        active_by_user.setdefault(str(tx.get("user", "")).lower(), tx)

    rows = []
    for user_key in dict.fromkeys(list(queue.keys()) + list(sending_users)):
        # sending_users comes from active_transfers, whose rows are built by
        # dcc.py rather than keyed by it - an entry missing its "user" field
        # contributes "" to that set and used to reach the page as a blank row
        # with a "?" preview. Iterating dcc_queue alone could not produce that,
        # because its keys are always real nicks; taking senders from a list of
        # dicts is what introduced the possibility.
        if not user_key:
            continue
        entries = queue.get(user_key, [])
        status = "sending" if user_key in sending_users else ("frozen" if user_key in frozen else "queued")
        first = entries[0] if entries else None
        if first is not None:
            preview = first.get("file", "?") if isinstance(first, dict) else str(first)
        elif user_key in active_by_user:
            preview = active_by_user[user_key].get("file", "?")
        else:
            preview = ""
        rows.append({"user": user_key, "preview": preview, "count": len(entries), "status": status})
    return rows


def build_search_payload(query):
    """The Search view's data: up to WEBUI_MAX_SEARCH_RESULTS matches for `query`.

    Delegates the actual scan to list.find_matching_entries() - the same
    IRC-agnostic function execute_search() now calls - so a web search and an
    IRC @find agree on what matches. This just applies a browser-sized limit
    and its own JSON shape instead of IRC formatting.
    """
    import list as list_mod
    import re

    raw_clean = str(query or "")
    clean_term = re.sub(r'[-*_.]', ' ', raw_clean)
    search_words = [w.strip().lower() for w in clean_term.split() if w.strip()]
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


def build_filelists_payload(offset=0, limit=None):
    """The File Lists view's data: a page of THIS bot's own master list.

    v1 scope is deliberately THIS BOT ONLY - it serves DCCore's own master
    list, decomposed into rows, with "source" hardcoded to config.NICKNAME.
    Real cross-bot deduplication (parsing other bots' adverts in the channel)
    is out of scope; this dedup is the trivial single-source case, collapsing
    only the same filename listed under two different folders.

    Issue #76, option 3: still parses the ENTIRE list every call, exactly as
    before (this bot's own list has no size cap of its own, so there is no
    cheaper way to know how many rows exist or to dedup correctly) - only
    what gets returned is now a page of it, `[offset:offset+limit]`, plus the
    `total` row count, instead of the whole thing. `limit=None` (the route's
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

    entries, _total = list_mod.find_matching_entries([], limit=None)
    rows = list_mod.entries_to_filelist_rows(entries, getattr(config, "NICKNAME", "?"))
    groups = list_mod.group_rows_by_folder(rows)
    page, total_folders, total_rows = list_mod.page_folder_groups(
        groups, offset, limit, max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS)
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
    }


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


def build_list_fetch_enqueue_result(bot_raw):
    """POST /api/filelists/fetch's pure logic: validate the bot nick and
    enqueue a request_type="list" row.

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

    if dcc_fetch.has_outstanding_bot_alone_request(bot):
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}

    request_id = dcc_fetch.enqueue_fetch(bot, "", request_type="list")
    if request_id is None:
        # Defense in depth: enqueue_fetch() enforces this same invariant
        # itself (see its docstring), so this should be unreachable given
        # the pre-check just above - but never surface it as a fabricated
        # success if some future race or caller change makes it reachable.
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}
    return 200, {"created": [request_id]}


def build_folder_rar_fetch_enqueue_result(bot_raw, folder_raw):
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

    if dcc_fetch.has_outstanding_bot_alone_request(bot):
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}

    request_id = dcc_fetch.enqueue_fetch(bot, f"!rar {folder}", request_type="folder")
    if request_id is None:
        # Defense in depth - see build_list_fetch_enqueue_result()'s
        # identical comment above.
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}
    return 200, {"created": [request_id]}


def build_fetched_bot_list_summaries():
    """GET /api/filelists/bots payload: one row per bot with a fetched list
    currently available, for the File Lists view's switcher control.

    "count" reads the "entry_count" field process_fetched_list_zip() computes
    ONCE at fetch time (issue #76, option 2) rather than the actual row list -
    which is no longer stored at all - so this stays a cheap dict lookup even
    though the File Lists tab's switcher polls this route every
    FILELISTS_BOTS_POLL_MS (see web/app.js), for every fetched bot, forever.
    Re-parsing each bot's whole list from disk just to report a count on every
    poll would defeat much of the point of no longer retaining it in memory.
    """
    store = dict(getattr(config, "fetched_bot_lists", {}) or {})
    rows = [
        {
            "bot": entry.get("bot", key),
            "fetched_at": entry.get("fetched_at", 0),
            "count": entry.get("entry_count", 0),
        }
        for key, entry in store.items()
    ]
    rows.sort(key=lambda row: str(row["bot"]).lower())
    return rows


def build_fetched_bot_list_payload(nick, offset=0, limit=None):
    """GET /api/filelists/bot/<nick> payload: (http_status, payload_dict).

    Issue #76, options 2 and 3 together: the fetched bot's rows are no longer
    kept in memory at all (see list_fetch.process_fetched_list_zip()) - this
    re-parses the stored list_path FRESH on every call, via
    list_fetch.get_fetched_bot_page(), then returns one page of the result.
    "entries" is in the EXACT same row shape build_filelists_payload() returns
    for this bot's own list (both go through list.entries_to_filelist_rows()),
    so the frontend's File Lists table rendering needs no changes to display
    either one - only the data source (which endpoint it polled) differs.

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

    page, total_folders, total_rows, error = list_fetch.get_fetched_bot_page(
        entry, offset, limit)
    if error:
        return 502, {"error": error}

    return 200, {
        "bot": entry.get("bot", nick),
        "fetched_at": entry.get("fetched_at", 0),
        "folders": page,
        "total": total_folders,
        "total_files": total_rows,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
    }


# ==========================================================================
# Cross-bot search broadcast (mutating - behind the same login as every other
# route, see the module docstring). Pure logic lives here, same reasoning as
# the build_*_payload() functions above: no Flask import, fully unit testable.
# ==========================================================================

BROADCAST_SEARCH_WINDOW = 30.0        # seconds the listening window stays open
BROADCAST_SEARCH_MIN_TERM_LEN = 3     # mirrors list.py's own @find minimum


def build_broadcast_status_payload():
    """GET /api/search/broadcast/status payload: whether a window is open
    right now, plus every result captured so far. `listening` (not just
    the raw `broadcast_search_inprogress` flag) also checks the deadline, so
    a window that flipped false only a poll-interval ago and one that is
    genuinely still counting down are never confused."""
    now = time.time()
    deadline = float(getattr(config, "broadcast_search_deadline", 0) or 0)
    inprogress = bool(getattr(config, "broadcast_search_inprogress", False))
    return {
        "listening": inprogress and now < deadline,
        "deadline": deadline,
        "term": getattr(config, "broadcast_search_term", ""),
        "results": list(getattr(config, "broadcast_search_results", []) or []),
    }


def json_object(body):
    """A parsed JSON request body as a dict, or an empty one.

    request.get_json(silent=True) returns whatever the body parsed to, and the
    `or {}` this replaces only substitutes for a FALSY result. A truthy
    non-dict - an array, a string, a number - passed straight through to
    .get() on the next line and raised AttributeError, which Flask turns into
    a 500 with a traceback, for input the routes should simply reject.

    An empty dict is the right substitute rather than an error of its own,
    because it is exactly what a missing body already produces: the route
    validators downstream turn that into their normal 400. Kept pure, and out
    of the request context, so tests can reach it with no Flask installed.
    """
    return body if isinstance(body, dict) else {}


def start_broadcast_search(term):
    """Validate and kick off a cross-bot @find broadcast. Returns
    (http_status, payload_dict); the Flask route below only parses the
    request body and calls this, so the actual behaviour - validation,
    cooldown, the in-memory state transition, queuing the outbound line - is
    exercised by tests/test_webserver.py with no Flask install required.

    Behind the same login as every other route in this app (see the module
    docstring) - a logged-in operator can make the daemon send an @find into
    a real, public IRC channel, which is why this route, unlike most others
    here, actually sends something rather than just reading state.
    """
    term = "" if term is None else term
    term_err = reject_if_unsafe_for_irc_line(term, "term")
    if term_err:
        return 400, {"error": term_err}

    clean_term = term.strip()
    if len(clean_term) < BROADCAST_SEARCH_MIN_TERM_LEN:
        return 400, {"error": f"Search term must be at least {BROADCAST_SEARCH_MIN_TERM_LEN} characters long."}

    now = time.time()
    if getattr(config, "broadcast_search_inprogress", False) and now < getattr(config, "broadcast_search_deadline", 0):
        return 409, {"error": "A broadcast search is already in progress.",
                     "deadline": config.broadcast_search_deadline}

    cooldown = float(getattr(config, "BROADCAST_SEARCH_COOLDOWN", 30))
    since_last = now - float(getattr(config, "last_broadcast_search_at", 0) or 0)
    if since_last < cooldown:
        return 429, {"error": f"Please wait {int(cooldown - since_last)}s before broadcasting another search."}

    oserve = sys.modules.get("oserve")
    if not oserve or not getattr(oserve, "irc_connection", None):
        return 503, {"error": "IRC connection is not up."}

    channel = (getattr(config, "BROADCAST_SEARCH_CHANNEL", None)
               or str(getattr(config, "CHANNEL", "")).split(",")[0].strip())
    if not channel:
        return 503, {"error": "No broadcast channel configured."}

    deadline = now + BROADCAST_SEARCH_WINDOW
    config.broadcast_search_inprogress = True
    config.broadcast_search_deadline = deadline
    config.broadcast_search_term = clean_term
    # In place, not `= []`: config.broadcast_search_results is bound from
    # runtime.py - see runtime.py's docstring on why this must never rebind.
    config.broadcast_search_results.clear()
    config.last_broadcast_search_at = now

    # reject_if_unsafe_for_irc_line() above already caps clean_term's length
    # (IRC_LINE_FIELD_MAX_LEN), so this is belt-and-braces: fit_irc_line()
    # shrinks against the real wire budget (announce.IRC_LINE_BUDGET) rather
    # than trusting that the boundary cap alone guarantees the built line
    # fits, the same defense-in-depth posture dcc_fetch.py's own dispatch
    # loop takes for the enqueue-time bot/filename check (#162 finding #13).
    import announce
    line = announce.fit_irc_line(lambda v: f"PRIVMSG {channel} :@find {v}\r\n", clean_term)
    oserve.queue_message(channel, line)

    def _close_window(expected_deadline=deadline):
        # Runs on its own daemon thread so the request thread returns
        # immediately - see the module docstring's "must not block the Flask
        # request thread for 30s" requirement.
        time.sleep(BROADCAST_SEARCH_WINDOW)
        # Only close OUR window. A fresh broadcast should never start before
        # this one's deadline given the cooldown above, but this guard means
        # a stale timer can never clobber a newer window if that invariant
        # is ever violated (a rehash resetting last_broadcast_search_at, say).
        if getattr(config, "broadcast_search_deadline", None) == expected_deadline:
            config.broadcast_search_inprogress = False

    threading.Thread(target=_close_window, daemon=True).start()

    return 200, {"status": "listening", "deadline": deadline, "term": clean_term}


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
        request_id = dcc_fetch.enqueue_fetch(bot, filename)
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
    """GET /api/fetch/status payload: every fetch_queue row, oldest first, so
    the dashboard's Downloads panel has a stable order to render.

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
    for request_id, row in sorted(queue.items(), key=lambda kv: kv[1].get("requested_at", 0)):
        row["id"] = request_id
        rows.append(row)
    return rows


def build_fetch_delete_result(request_id):
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
        if row.get("state") not in ("complete", "failed", "pending"):
            return 409, {"error": "A fetch already in progress cannot be deleted."}
        stored_filename = row.get("stored_filename")
        del config.fetch_queue[request_id]

    if stored_filename:
        directory = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))
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

    return 200, {"deleted": request_id}


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


# Attributed nick for admin actions the dashboard dispatches on an operator's
# behalf - nobody is logged into IRC as "WEB-DASHBOARD", so this can never
# collide with (or impersonate) a real admin nick in a log line or in
# commands.py's own messaging. Shared by every such action below (rehash,
# list update): one identity, not a fresh one invented per route.
WEB_DASHBOARD_SOURCE = "WEB-DASHBOARD"


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
    return {
        "running": bool(getattr(config, "update_inprogress", False)),
        "ok": getattr(config, "last_list_update_ok", None),
        "error": getattr(config, "last_list_update_error", None),
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
    if (bool(getattr(config, "search_inprogress", False))
            and bool(getattr(config, "PAUSE_ON_UPDATE", True))):
        return 409, {"error": "Another system scan is already in progress."}

    import commands
    commands.handle_list_update_request(
        WEB_DASHBOARD_SOURCE, WEB_DASHBOARD_SOURCE, authorised=True)
    return 200, {"update": "started"}


# ==========================================================================
# Settings (mutating - behind the same login as every other route). Pure
# logic here, same reasoning as every other build_*_payload()/apply_*()
# function in this module: no Flask import, fully unit testable.
# ==========================================================================

# Hand-written UX grouping of every setting config.py annotates (see
# declared_types() below) except ADMIN_PASSWORD_HASH, which never appears as
# a field - only the "admin_password_set" boolean does (see
# build_settings_payload()). Every name here is checked against config.py's
# actual annotations by SettingsPayloadTests' completeness guard, so a
# setting added to config.py later and never slotted in here fails a test
# instead of silently never showing up on the page.
SETTINGS_CATEGORIES = (
    ("identity",      "Identity & network",   ["SERVER", "PORT", "NICKNAME", "ALT_NICKNAME",
                                                "ADMIN_NICK", "CHANNEL", "DEBUG_CHANNEL"]),
    ("slots-queue",   "Slots & queue",         ["MAX_DCC_SLOTS", "MAX_USER_QUEUE", "MAX_GLOBAL_QUEUE",
                                                "MAX_SEARCH_RESULTS", "MSG_DELAY", "DEBUG_MSG_DELAY",
                                                "DCC_PORT_START", "DCC_PORT_END", "MAX_FETCH_SLOTS", "FETCH_HISTORY_DAYS", "FETCH_HISTORY_MAX_ROWS",
                                                "MAX_FETCH_FILE_SIZE", "FETCH_TRANSFER_TIMEOUT",
                                                "FETCH_OFFER_TIMEOUT", "FETCH_FOLDER_OFFER_TIMEOUT",
                                                "MAX_FETCH_FOLDER_FILE_SIZE", "MAX_FETCH_LIST_FILE_SIZE",
                                                "FETCH_FOLDER_TRANSFER_TIMEOUT"]),
    ("paths",         "Paths & storage",       ["LIST_BASE_NAME", "PAUSE_ON_UPDATE", "FILE_DIRECTORY",
                                                "LIST_FORMAT", "RAR_ENABLED", "RAR_BINARY", "TMP_ZIP_DIR", "LOCAL_LIST_DIR",
                                                "FETCHED_FILES_DIR", "BANS_FILE", "STATS_FILE",
                                                "HARD_BANS_FILE", "KNOWN_BOTS_FILE", "FETCHED_BOT_LISTS_FILE",
                                                "FETCH_HISTORY_FILE", "DOWNLOAD_COUNTS_FILE",
                                                "LIST_SIZE_FILE", "LIST_RAWBYTES_FILE"]),
    ("advertising",   "Advertising & search",  ["THEME", "CUSTOM_THEME_BORDER", "CUSTOM_THEME_SEPARATOR",
                                                "CUSTOM_THEME_TEXTBOX", "CUSTOM_THEME_VALUE",
                                                "CUSTOM_THEME_ALERT", "CUSTOM_THEME_ACCENT",
                                                "ANNOUNCE_INTERVAL", "BROADCAST_SEARCH_CHANNEL",
                                                "BROADCAST_SEARCH_COOLDOWN"]),
    ("anti-flood",    "Anti-flood",            ["MAX_REQUESTS", "REQUEST_WINDOW", "MUTE_TIME", "FLOOD_BAN_SECONDS",
                                                "MAX_SEND_FAILS", "RAR_TIMEOUT", "LIST_UPDATE_TIMEOUT"]),
    ("admin-console", "Admin console",         ["ADMIN_HOSTMASKS", "ADMIN_CHAT_MODE",
                                                "ADMIN_CHANNEL_COMMANDS"]),
    ("web-dashboard", "Web dashboard",         ["WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT"]),
    ("debug",         "Debug & logging",       ["DEBUG_MODE", "DEBUG_TO_CHANNEL", "DEBUG_TO_CONSOLE",
                                                "SCRIPT_VERSION"]),
)

# A human-readable label per setting, since the raw config.py name
# (MAX_DCC_SLOTS, DCC_PORT_START, ...) is what an operator edits in a text
# file, not what they expect to read on a form. Checked against
# SETTINGS_CATEGORIES by SettingsPayloadTests' completeness guard, same
# reasoning as that list itself: a setting added to config.py and slotted
# into a category but never given a label here would otherwise silently show
# its raw name instead of failing a test.
SETTINGS_LABELS = {
    "SERVER": "IRC server",
    "PORT": "Port",
    "NICKNAME": "Nickname",
    "ALT_NICKNAME": "Alt nickname",
    "ADMIN_NICK": "Admin nick(s)",
    "CHANNEL": "Channels",
    "DEBUG_CHANNEL": "Debug channel",

    "MAX_DCC_SLOTS": "Max simultaneous sends",
    "MAX_USER_QUEUE": "Max queue per user",
    "MAX_GLOBAL_QUEUE": "Max global queue",
    "MAX_SEARCH_RESULTS": "Max search results",
    "MSG_DELAY": "Message delay (seconds)",
    "DEBUG_MSG_DELAY": "Debug message delay (seconds)",
    "DCC_PORT_START": "DCC port range start",
    "DCC_PORT_END": "DCC port range end",
    "MAX_FETCH_SLOTS": "Max fetch slots",
    "FETCH_HISTORY_DAYS": "Keep finished downloads for (days)",
    "FETCH_HISTORY_MAX_ROWS": "Maximum finished downloads kept",
    "MAX_FETCH_FILE_SIZE": "Max fetch file size (bytes)",
    "FETCH_TRANSFER_TIMEOUT": "Fetch transfer timeout (seconds)",
    "FETCH_OFFER_TIMEOUT": "Fetch offer timeout (seconds)",
    "FETCH_FOLDER_OFFER_TIMEOUT": "Folder (.rar) fetch offer timeout (seconds)",
    "MAX_FETCH_FOLDER_FILE_SIZE": "Max folder (.rar) fetch size (bytes)",
    "MAX_FETCH_LIST_FILE_SIZE": "Max fetched master-list zip size (bytes)",
    "FETCH_FOLDER_TRANSFER_TIMEOUT": "Folder (.rar) fetch transfer timeout (seconds)",

    "LIST_BASE_NAME": "List base name",
    "PAUSE_ON_UPDATE": "Pause sharing during !update",
    "FILE_DIRECTORY": "Music directory",
    "LIST_FORMAT": "List delivery format",
    "RAR_ENABLED": "Enable !rar folder packing",
    "RAR_BINARY": "RAR binary path",
    "TMP_ZIP_DIR": "Temp archive directory",
    "LOCAL_LIST_DIR": "Master list directory",
    "FETCHED_FILES_DIR": "Fetched files directory",
    "BANS_FILE": "Bans file",
    "STATS_FILE": "Stats file",
    "HARD_BANS_FILE": "Hard bans file",
    "KNOWN_BOTS_FILE": "Known bots file",
    "DOWNLOAD_COUNTS_FILE": "Download counts file",
    "FETCHED_BOT_LISTS_FILE": "Fetched bot lists file",
    "FETCH_HISTORY_FILE": "Fetch history file",
    "LIST_SIZE_FILE": "List size file",
    "LIST_RAWBYTES_FILE": "List raw bytes file",

    "THEME": "Colour theme",
    "CUSTOM_THEME_BORDER": "Custom theme: border colour",
    "CUSTOM_THEME_SEPARATOR": "Custom theme: separator colour",
    "CUSTOM_THEME_TEXTBOX": "Custom theme: text box colour",
    "CUSTOM_THEME_VALUE": "Custom theme: value colour",
    "CUSTOM_THEME_ALERT": "Custom theme: alert colour",
    "CUSTOM_THEME_ACCENT": "Custom theme: accent colour",
    "ANNOUNCE_INTERVAL": "Advert interval (seconds)",
    "BROADCAST_SEARCH_CHANNEL": "Broadcast search channel",
    "BROADCAST_SEARCH_COOLDOWN": "Broadcast search cooldown (seconds)",

    "MAX_REQUESTS": "Max requests per window",
    "REQUEST_WINDOW": "Request window (seconds)",
    "MUTE_TIME": "Mute duration (seconds)",
    "FLOOD_BAN_SECONDS": "Ban after flooding while muted (seconds)",
    "MAX_SEND_FAILS": "Max send failures",
    "RAR_TIMEOUT": "RAR pack timeout (seconds)",
    "LIST_UPDATE_TIMEOUT": "List update timeout (seconds)",

    "ADMIN_HOSTMASKS": "Admin hostmasks",
    "ADMIN_CHAT_MODE": "DCC chat connection mode",
    "ADMIN_CHANNEL_COMMANDS": "Allow admin commands in channel",

    "WEBUI_ENABLED": "Enable web dashboard",
    "WEBUI_HOST": "Host",
    "WEBUI_PORT": "Port",

    "DEBUG_MODE": "Debug mode",
    "DEBUG_TO_CHANNEL": "Send debug lines to channel",
    "DEBUG_TO_CONSOLE": "Send debug lines to admin console",
    "SCRIPT_VERSION": "Script version",
}


def _settings_field(name, declared, value):
    """One field for the settings form.

    "choices" is present only for a setting that has a fixed few - LIST_FORMAT
    is the first. It carries the same tuple settings_file.CHOICES validates
    against, so the page cannot offer a value the save would then refuse, and
    an operator picks a format instead of typing one of three words correctly.
    """
    import settings_file
    field = {"name": name, "label": SETTINGS_LABELS.get(name, name),
             "type": declared.__name__, "value": value}
    if name in settings_file.CHOICES:
        field["choices"] = list(settings_file.CHOICES[name])
    return field


def build_settings_payload():
    """GET /api/settings payload: every editable setting, grouped for the
    Settings view's category rail, plus whether an admin password is set.

    Filters through settings_file.declared_types(vars(config)) - names
    config.py itself annotates - rather than vars(config) directly, so a
    runtime-only name (ORIGINAL_NICK, MY_IP_OR_DOCK, ...) can never appear
    here even before settings_file.save() would refuse to write it.

    ADMIN_PASSWORD_HASH never appears as a field's raw value anywhere - only
    the boolean admin_password_set at the top level. A "leftover" category is
    appended for any declared+overridable setting SETTINGS_CATEGORIES above
    forgot to mention, so a config.py addition that nobody categorised is
    still reachable rather than silently missing from the page (see
    SettingsPayloadTests' completeness guard, which currently keeps this at
    zero entries by keeping SETTINGS_CATEGORIES exhaustive).
    """
    import settings_file
    types = settings_file.declared_types(vars(config))
    categories = []
    seen = set()
    for cat_id, label, names in SETTINGS_CATEGORIES:
        fields = []
        for name in names:
            if name not in types:
                continue
            value = getattr(config, name, None)
            if not settings_file.is_overridable(name, value):
                continue
            fields.append(_settings_field(name, types[name], value))
            seen.add(name)
        categories.append({"id": cat_id, "label": label, "fields": fields})

    # A leftover has no entry in SETTINGS_LABELS either - by construction, it
    # was never in SETTINGS_CATEGORIES, and SETTINGS_LABELS only ever gets a
    # name added alongside slotting it into a category. Falls back to the raw
    # name rather than raising, matching how it already reaches the page at
    # all despite being uncategorised.
    leftover = [n for n in types if n not in seen and n != "ADMIN_PASSWORD_HASH"
                and settings_file.is_overridable(n, getattr(config, n, None))]
    if leftover:
        categories.append({"id": "other", "label": "Other", "fields": [
            _settings_field(n, types[n], getattr(config, n, None))
            for n in sorted(leftover)]})

    return {
        "categories": categories,
        "admin_password_set": bool(getattr(config, "ADMIN_PASSWORD_HASH", "")),
    }


# Settings that only take effect on a full daemon restart - webserver.py owns
# a live listening socket and is deliberately excluded from
# commands.py's CORE_MODULES, so a rehash after saving one of these three
# cannot apply it live. Surfaced in the save response's "restart_required" so
# the frontend can tell the operator, rather than implying "rehash" fixed it.
SETTINGS_RESTART_ONLY = {"WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT"}


def _save_settings_and_rehash(changes):
    """Write `changes` to settings.conf and dispatch a rehash on its own
    daemon thread. The shared tail of apply_settings_changes() (POST
    /api/settings) and build_password_change_result() (POST
    /api/settings/password): a plain function call does not skip a caller's
    own `if` checks, so build_password_change_result reaches this directly
    rather than through apply_settings_changes() - going through that
    function instead would always hit its own ADMIN_PASSWORD_HASH rejection,
    which exists to stop the *general* settings endpoint from being used to
    change the password, not to block the password endpoint's own write of
    the one setting it exists to change.

    The rehash runs detached rather than inline: commands.handle_rehash_request()
    does real socket I/O and importlib.reload()s several modules, which would
    freeze this request for its duration - the same reason adminchat.py's own
    _cmd_rehash wraps the identical call in _run_detached(). This module
    already has the same shape for a different slow/async action; see
    start_broadcast_search()'s threading.Thread(target=_close_window, ...).

    settings_file.save() holds its own lock around the read-modify-write of
    settings.conf, so two concurrent saves from here cannot race each other -
    see that function's docstring. Both settings_file.SettingsWriteError
    (save()'s own validation failures) and a plain OSError (e.g. an
    unwritable settings directory - the underlying atomic write can raise
    this too) are caught here and turned into a clean 400 JSON error; letting
    an OSError escape would surface as an unhandled 500 with a non-JSON body,
    which the frontend's postJson() cannot parse.

    Returns (http_status, payload_dict).
    """
    import settings_file
    try:
        result = settings_file.save(vars(config), changes)
    except (settings_file.SettingsWriteError, OSError) as err:
        return 400, {"error": str(err)}

    # Imported lazily, exactly like `list`/`dcc_fetch` above - see the module
    # docstring and tests/test_import_graph.py: `commands` must never load at
    # module scope, only from inside a handler that actually needs it.
    import commands
    threading.Thread(
        target=commands.handle_rehash_request,
        args=(WEB_DASHBOARD_SOURCE, WEB_DASHBOARD_SOURCE),
        kwargs={"authorised": True},
        daemon=True,
    ).start()

    restart_required = sorted(set(result["written"]) & SETTINGS_RESTART_ONLY)
    return 200, dict(result, rehash="started", restart_required=restart_required)


def apply_settings_changes(changes):
    """POST /api/settings's pure logic: validate `changes` (a flat
    {SETTING: "string value"} object - settings_file.save() coerces each
    value the same way settings.conf itself would be read), then hand off to
    _save_settings_and_rehash() for the actual write + dispatched rehash.

    Returns (http_status, payload_dict).
    """
    if not isinstance(changes, dict) or not changes:
        return 400, {"error": "Expected a non-empty object of {SETTING: value}."}
    if "ADMIN_PASSWORD_HASH" in changes:
        return 400, {"error": "Use POST /api/settings/password to change the "
                               "admin password."}

    return _save_settings_and_rehash(changes)


def build_password_change_result(new_password, confirm_password):
    """POST /api/settings/password's pure logic: validate the pair, hash the
    new password the same way `python adminchat.py` does, and write it
    through _save_settings_and_rehash() - the same save-then-dispatch-rehash
    tail apply_settings_changes() uses, without its ADMIN_PASSWORD_HASH
    rejection (see that helper's docstring for why this cannot go through
    apply_settings_changes() itself).
    """
    if not new_password:
        return 400, {"error": "Password cannot be empty."}
    if new_password != confirm_password:
        return 400, {"error": "Passwords do not match."}

    new_hash = adminchat.make_password_hash(new_password)
    return _save_settings_and_rehash({"ADMIN_PASSWORD_HASH": new_hash})


# ==========================================================================
# Flask app - only built/used when Flask is actually installed.
# ==========================================================================

# Self-contained on purpose: the login page must render before a session
# exists, so it cannot depend on web/style.css (that request would itself be
# behind the login it is trying to render) or on any operator data.
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>DCCore Dashboard - Login</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #0b0f12;
          color: #e6edf0; display: flex; align-items: center; justify-content: center;
          height: 100vh; margin: 0; }}
  form {{ background: #131a1f; padding: 2rem 2.25rem; border-radius: 10px;
          min-width: 260px; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 1.05rem; margin: 0 0 1.25rem; font-weight: 600; }}
  input {{ width: 100%; padding: 0.55rem 0.6rem; margin-bottom: 1rem; box-sizing: border-box;
           background: #0b0f12; border: 1px solid #2a343b; border-radius: 6px; color: #e6edf0; }}
  button {{ width: 100%; padding: 0.55rem; background: #2dd4c8; color: #06231f; border: 0;
            border-radius: 6px; font-weight: 600; cursor: pointer; }}
  .error {{ color: #f87171; font-size: 0.85rem; margin: -0.75rem 0 1rem; }}
</style></head>
<body>
  <form method="post" action="/login">
    <h1>DCCore Dashboard</h1>
    {error_html}
    <input type="password" name="password" placeholder="Admin password" autofocus>
    <button type="submit">Log in</button>
  </form>
</body></html>"""


# Failed-login tracking for THIS route, deliberately separate from
# adminchat.py's own _bad_ips pool even though the policy (attempt count,
# block duration) is identical and reused from there directly. The password
# is shared with the DCC CHAT admin console on purpose - but the block budget
# is not, because if it were, an attacker guessing at this HTTP form could
# spend down the same counter and lock the real operator out of the DCC
# console too. Same policy, separate pools: one abusive address costs it
# access to this route only.
_web_bad_ips = {}
_web_bad_ips_lock = threading.Lock()


def _note_bad_web_login(ip):
    if not ip:
        return
    with _web_bad_ips_lock:
        entry = _web_bad_ips.get(ip) or [0, 0.0]
        entry[0] += 1
        if entry[0] >= adminchat.MAX_PASSWORD_ATTEMPTS:
            entry[1] = time.time() + adminchat.BAD_IP_BLOCK_SECONDS
        _web_bad_ips[ip] = entry


def _is_bad_web_ip(ip):
    if not ip:
        return False
    with _web_bad_ips_lock:
        entry = _web_bad_ips.get(ip)
        if not entry:
            return False
        if entry[1] and time.time() >= entry[1]:
            del _web_bad_ips[ip]  # block expired; forget it so a typo is not permanent
            return False
        return bool(entry[1])


def _clear_bad_web_ip(ip):
    with _web_bad_ips_lock:
        _web_bad_ips.pop(ip, None)


if HAVE_FLASK:

    def create_app():
        app = Flask(__name__, static_folder="web", static_url_path="")
        app.secret_key = os.urandom(32)
        # The mutating routes (broadcast search, fetch enqueue, list fetch)
        # all POST; Lax is the app's own decision instead of whatever the
        # visitor's browser happens to default to.
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

        @app.before_request
        def require_login():
            # The sole exemption: everything else, static files included,
            # needs a session already marked authenticated by a prior POST
            # here. Endpoint rather than path, so a future route can't
            # accidentally slip past this by sharing a path prefix.
            if request.endpoint == "login":
                return None
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Authentication required."}), 401
                return redirect("/login")
            return None

        @app.route("/login", methods=["GET", "POST"])
        def login():
            error = None
            if request.method == "POST":
                ip = request.remote_addr
                if _is_bad_web_ip(ip):
                    error = "Too many failed attempts. Try again later."
                else:
                    stored = getattr(config, "ADMIN_PASSWORD_HASH", "")
                    supplied = request.form.get("password", "")
                    if adminchat.verify_password(stored, supplied):
                        _clear_bad_web_ip(ip)
                        session["authenticated"] = True
                        return redirect("/")
                    _note_bad_web_login(ip)
                    error = "Incorrect password."
            error_html = '<p class="error">{}</p>'.format(error) if error else ""
            status = 401 if error else 200
            return LOGIN_PAGE.format(error_html=error_html), status

        @app.route("/logout", methods=["GET", "POST"])
        def logout():
            session.clear()
            return redirect("/login")

        @app.route("/")
        def index():
            return app.send_static_file("index.html")

        @app.route("/api/queue")
        def api_queue():
            return jsonify(build_queue_payload(user=request.args.get("user")))

        @app.route("/api/stats")
        def api_stats():
            return jsonify(build_stats_payload())

        @app.route("/api/search")
        def api_search():
            return jsonify(build_search_payload(request.args.get("q", "")))

        @app.route("/api/filelists")
        def api_filelists():
            offset, limit = parse_pagination_params(
                request.args.get("offset"), request.args.get("limit"))
            return jsonify(build_filelists_payload(offset, limit))

        # ------------------------------------------------------------------
        # These routes below DO mutate state (queuing an outbound IRC line,
        # dialling an IP:port a foreign bot supplies) - which is exactly why
        # they sit behind the same require_login() as everything else, same
        # as every other route in this app. See the module docstring.
        # ------------------------------------------------------------------

        @app.route("/api/search/broadcast", methods=["POST"])
        def api_search_broadcast():
            body = json_object(request.get_json(silent=True))
            status, result = start_broadcast_search(body.get("term", ""))
            return jsonify(result), status

        @app.route("/api/search/broadcast/status")
        def api_search_broadcast_status():
            return jsonify(build_broadcast_status_payload())

        @app.route("/api/fetch/enqueue", methods=["POST"])
        def api_fetch_enqueue():
            payload = request.get_json(silent=True)
            status, result = build_fetch_enqueue_result(payload)
            return jsonify(result), status

        @app.route("/api/fetch/status")
        def api_fetch_status():
            return jsonify(build_fetch_status_payload())

        @app.route("/api/tools/verify-list")
        def api_tools_verify_list():
            return jsonify(build_verify_list_payload())

        @app.route("/api/tools/update-list", methods=["POST"])
        def api_tools_update_list():
            status, result = start_list_update()
            return jsonify(result), status

        @app.route("/api/tools/update-list/status")
        def api_tools_update_list_status():
            return jsonify(build_update_list_status_payload())

        @app.route("/api/filelists/fetch", methods=["POST"])
        def api_filelists_fetch():
            body = json_object(request.get_json(silent=True))
            status, result = build_list_fetch_enqueue_result(body.get("bot", ""))
            return jsonify(result), status

        @app.route("/api/filelists/fetch-folder-rar", methods=["POST"])
        def api_filelists_fetch_folder_rar():
            body = json_object(request.get_json(silent=True))
            status, result = build_folder_rar_fetch_enqueue_result(
                body.get("bot", ""), body.get("folder", ""))
            return jsonify(result), status

        @app.route("/api/filelists/bots")
        def api_filelists_bots():
            return jsonify(build_fetched_bot_list_summaries())

        @app.route("/api/filelists/bot/<nick>")
        def api_filelists_bot(nick):
            offset, limit = parse_pagination_params(
                request.args.get("offset"), request.args.get("limit"))
            status, result = build_fetched_bot_list_payload(nick, offset, limit)
            return jsonify(result), status

        @app.route("/api/fetch/<request_id>/download")
        def api_fetch_download(request_id):
            import dcc_fetch
            # Snapshot just the two fields needed, under the same lock the
            # writers use, so "complete"/stored_filename can never be read as
            # a torn pair - the lock is released before send_from_directory()
            # touches the filesystem.
            with dcc_fetch._fetch_lock():
                row = getattr(config, "fetch_queue", {}).get(request_id)
                if row and row.get("state") == "complete" and row.get("stored_filename"):
                    stored_filename = row["stored_filename"]
                    download_name = row.get("filename") or stored_filename
                else:
                    stored_filename = None
            if not stored_filename:
                return jsonify({"error": "Unknown, incomplete, or failed fetch."}), 404
            # Never build this path from the URL parameter - request_id only
            # selects a row, and the row's OWN already-validated
            # stored_filename (set by dcc_fetch.py after it ran the offer's
            # filename through dcc.is_safe_path()) is what actually gets
            # opened.
            directory = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))
            return send_from_directory(
                directory, stored_filename, as_attachment=True,
                download_name=download_name)

        @app.route("/api/fetch/<request_id>/delete", methods=["POST"])
        def api_fetch_delete(request_id):
            status, result = build_fetch_delete_result(request_id)
            return jsonify(result), status

        @app.route("/api/settings")
        def api_settings():
            return jsonify(build_settings_payload())

        @app.route("/api/settings", methods=["POST"])
        def api_settings_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_settings_changes(body)
            return jsonify(result), status

        @app.route("/api/settings/password", methods=["POST"])
        def api_settings_password():
            body = json_object(request.get_json(silent=True))
            status, result = build_password_change_result(
                body.get("new_password", ""), body.get("confirm_password", ""))
            return jsonify(result), status

        return app


def start():
    """Run the dashboard. Called on its own daemon thread from oserve.startup().

    Logs and returns rather than raising on any of: Flask missing, the feature
    disabled via config, or the port being unavailable - none of those may take
    the daemon down with them.
    """
    if not HAVE_FLASK:
        print("[WEBUI] Flask not installed; dashboard disabled.")
        return
    # See oserve.startup(): absent means off, the same way config.py ships it.
    if not getattr(config, "WEBUI_ENABLED", False):
        print("[WEBUI] Disabled via config.WEBUI_ENABLED = False.")
        return
    if not adminchat.password_is_configured():
        print("[WEBUI] ADMIN_PASSWORD_HASH is not set; refusing to start the dashboard "
              "without a login. Generate one with `python adminchat.py` and put the "
              "result in admin_config.py or settings.conf.")
        return

    # 127.0.0.1 when absent, matching config.py. 0.0.0.0 would bind every
    # interface and put the dashboard on the LAN, which is the opposite of
    # what a missing setting should buy anyone - even with a login gate, that
    # is a decision the operator should make explicitly, not by omission.
    host = getattr(config, "WEBUI_HOST", "127.0.0.1")
    port = getattr(config, "WEBUI_PORT", 8420)
    app = create_app()
    print(f"[WEBUI] Dashboard starting on http://{host}:{port}/ (login required).")
    try:
        # use_reloader=False is NOT optional: Flask's reloader re-execs the whole
        # process, and this runs on an already-live daemon thread - a re-exec
        # here would take the entire bot down with it, not just the dashboard.
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as run_err:
        print(f"[WEBUI] Dashboard stopped: {run_err}")
