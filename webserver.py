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

import collections
import os
import sys
import threading
import time

import adminchat
import defaults as config
import platform_compat
import runtime

try:
    from flask import (Flask, jsonify, redirect, request, send_file,
                       send_from_directory, session)
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

# A ceiling on the served-folder list, for the same reason every other list
# this module accepts has one: a POST body is not a promise. Far above any
# real library - an operator with more than this has a drive letter problem,
# not a folder list - and low enough that the O(n^2) pairwise nesting check in
# library.problems() cannot be turned into a way to occupy the process.
MAX_SERVED_FOLDERS = 64

# And one path. library.problems() embeds each offending path verbatim in up
# to two messages per entry, so without this a 400 response is roughly twice
# the request that caused it. Every other web input in this module is bounded
# (IRC_LINE_FIELD_MAX_LEN, FETCH_ENQUEUE_MAX_ITEMS, FOLDER_BROWSE_MAX_ENTRIES);
# this one was not. Far longer than any real path - Windows stops at 32767
# even through the \\?\ prefix - so it bounds the absurd without touching the
# possible.
MAX_FOLDER_PATH_LEN = 4096

# One directory listing. A music library's top level can hold thousands of
# artist folders, and every one of them would be rendered into a panel nobody
# can scroll usefully - so the listing is capped and SAYS it was capped, which
# is the part that stops an operator concluding a folder is missing.
FOLDER_BROWSE_MAX_ENTRIES = 500


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


# The ceilings the import refuses beyond. A stray digit from a hand-edited
# text file is far likelier than a genuine value up here, and importing one
# silently is worse than refusing it - the operator can retype a number, but
# they cannot tell that a total is wrong once it looks plausible.
#
# 2^63 is where a 64-bit counter stops meaning anything; a byte total past a
# petabyte and a speed record past 10 GB/s are both past any real DCC link by
# orders of magnitude, and both are what a mis-parsed field looks like.
MAX_IMPORT_FILES = 2 ** 63 - 1
MAX_IMPORT_BYTES = 1 << 50           # 1 PiB
MAX_IMPORT_SPEED = 10 * 1000 ** 3    # 10 GB/s

_IMPORT_LIMITS = {
    "total_files": (MAX_IMPORT_FILES, "files sent"),
    "total_bytes": (MAX_IMPORT_BYTES, "bytes sent"),
    "speed_record": (MAX_IMPORT_SPEED, "speed record"),
}


def current_importable_stats():
    """What the three importable figures are right now.

    Read through the same accessors the Stats page uses, so the "before" column
    of the preview cannot disagree with the page the operator is looking at.
    """
    import db

    row = db.load_advanced_stats() or [0] * 7
    try:
        total_files = int(row[0])
    except (IndexError, TypeError, ValueError):
        total_files = 0
    try:
        total_bytes = int(row[1])
    except (IndexError, TypeError, ValueError):
        total_bytes = 0
    try:
        record = int(db.get_speed_record() or 0)
    except (TypeError, ValueError):
        record = 0
    return {"total_files": total_files, "total_bytes": total_bytes,
            "speed_record": record}


def validate_import_values(raw):
    """(clean, errors) for the three figures an import may carry.

    Every field optional - an operator missing an add-on imports what they
    have - but a field that IS present must be a whole number in range.
    Checked HERE and not only in the page: the page is convenience, this is
    the boundary, and everything arriving is from a text file somebody may
    have hand-edited.
    """
    clean = {}
    errors = []
    for name, (ceiling, label) in _IMPORT_LIMITS.items():
        if name not in (raw or {}):
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors.append(f"{label}: expected a whole number.")
            continue
        try:
            # str() first so "45902" works and 45902.0 does not silently
            # truncate a fraction somebody meant to be there.
            number = int(str(value).strip())
        except (TypeError, ValueError):
            errors.append(f"{label}: {value!r} is not a whole number.")
            continue
        if number < 0:
            errors.append(f"{label}: cannot be negative.")
            continue
        if number > ceiling:
            errors.append(f"{label}: {number:,} is beyond anything real "
                          f"(the limit is {ceiling:,}). A stray digit in a "
                          f"hand-edited file looks exactly like this.")
            continue
        clean[name] = number
    return clean, errors


def build_stats_import_preview(text):
    """POST /api/stats/import/preview: what a vars.ini would do, changing nothing.

    Separate from the write for the reason the issue gives: on a fresh install
    nobody reads the sentence about overwriting, and on a used one it is the
    only thing that matters. The page shows before and after side by side and
    the operator confirms.
    """
    import omenserve_import

    found = omenserve_import.read_install(text)
    clean, errors = validate_import_values(found.get("values") or {})
    return {
        "rows": found.get("rows", []),
        "notes": list(found.get("notes", [])) + errors,
        "current": current_importable_stats(),
        "values": clean,
        # OVERWRITTEN, NOT COMBINED, and said where the page can put it in
        # front of the operator rather than buried in prose they will not read
        # on a fresh install and cannot miss on a used one.
        "replaces": {name: value for name, value in clean.items()
                     if current_importable_stats().get(name)},
    }


def apply_stats_import(raw):
    """(http_status, payload) for POST /api/stats/import.

    Writes through db.save_advanced_stats() and db.save_speed_record(), which
    already take the disk lock and write atomically - a caller, not a second
    implementation of either.

    Returns the before and after, so the page reports what actually happened
    rather than what it asked for.
    """
    import db

    clean, errors = validate_import_values(raw)
    if errors:
        return 400, {"error": errors[0], "errors": errors}
    if not clean:
        return 400, {"error": "Nothing to import - no recognised figure was "
                              "supplied."}

    before = current_importable_stats()

    if "total_files" in clean or "total_bytes" in clean:
        # The 7-column row read-modify-written as a whole: the day columns and
        # the date belong to the daemon's own rotation and are not this
        # feature's to touch. Importing a lifetime total must not reset what
        # the bot did today.
        row = list(db.load_advanced_stats() or [0] * 7)
        while len(row) < 7:
            row.append(0)
        if "total_files" in clean:
            row[0] = clean["total_files"]
        if "total_bytes" in clean:
            row[1] = clean["total_bytes"]
        db.save_advanced_stats(row)

    if "speed_record" in clean:
        db.save_speed_record(clean["speed_record"])

    # WHAT ACTUALLY LANDED, compared against what was asked for. Both writers
    # swallow their own errors and return None - correct for them, since a
    # failed stats write must not take the daemon down - which meant this
    # returned 200 and "imported: [...]" for figures that never reached the
    # disk. The operator would have been told their history came across and
    # found half of it, with nothing to say which half.
    after = current_importable_stats()
    landed = [name for name in sorted(clean) if after.get(name) == clean[name]]
    missing = [name for name in sorted(clean) if name not in landed]
    if missing:
        return 500, {
            "error": "Some figures could not be written: "
                     + ", ".join(missing)
                     + ". Check the daemon log and the data directory.",
            "before": before, "after": after, "imported": landed,
            "failed": missing,
        }

    return 200, {"before": before, "after": after, "imported": landed}


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
    of the next file, how many are waiting, the FULL list of what is waiting,
    and - for whoever is sending - the file in flight and its progress) -
    what the Queue table shows. With `user`, the full file list queued for
    that one user instead - what a click-through or `?user=<nick>` on
    /api/queue returns.

    "preview"/"count"/"files" describe the QUEUE only - what is waiting
    behind whatever is currently sending, if anything, never included in it.
    dcc.py never puts the in-flight file into config.dcc_queue; it lives in
    config.active_transfers instead, which is why a sending user's progress
    needs its own fields rather than being folded into the queue ones -
    an operator asked to see the whole queue, not just its head, and a
    progress bar for the transfer actually running, not for whatever is
    queued behind it.

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
        elif user_key in frozen:
            status = "frozen"
        elif entries:
            status = "queued"
        else:
            status = "empty"
        files = [e.get("file", "?") if isinstance(e, dict) else str(e) for e in entries]
        current_file, bytes_sent, size = _progress_fields(user_key)
        return {"user": user_key, "status": status, "count": len(entries), "files": files,
                "current_file": current_file, "bytes_sent": bytes_sent, "size": size}

    # #220: a user sent with a free slot and nothing already queued never
    # enters dcc_queue at all - dcc.py's admission check appends straight to
    # active_transfers and returns (see start_dcc_send()). Iterating only
    # queue.items(), as this used to, left them off the Queue table entirely
    # while /api/stats (which reads active_transfers directly) showed the
    # transfer correctly - the two disagreeing about the same page. The
    # single-user branch above already got this right by checking
    # sending_users regardless of whether entries exist; this does the same.
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
        files = [e.get("file", "?") if isinstance(e, dict) else str(e) for e in entries]
        first = entries[0] if entries else None
        if first is not None:
            preview = first.get("file", "?") if isinstance(first, dict) else str(first)
        elif user_key in active_by_user:
            preview = active_by_user[user_key].get("file", "?")
        else:
            preview = ""
        current_file, bytes_sent, size = _progress_fields(user_key)
        rows.append({"user": user_key, "preview": preview, "count": len(entries), "status": status,
                     "files": files, "current_file": current_file,
                     "bytes_sent": bytes_sent, "size": size})
    return rows


def split_list_search_words(query):
    """A raw query string into the pre-split, lower-cased word list
    list.find_matching_entries() takes - the same splitting rule
    execute_search() has always used for @find, so a phrase means the same
    thing here, on the Search tab, and in the List Browser's per-list search
    (#399's follow-up) rather than three separate ideas of "contains".

    An empty result (no words survived, or the query was empty/blank) is
    NOT the same as "no filter" to every caller - find_matching_entries()
    treats an empty list as "match everything", which build_filelists_payload()
    and build_fetched_bot_list_payload() want but build_search_payload()
    historically did not (a term that stripped to nothing returned no
    results, not the whole list) - so each caller decides what to do with an
    empty return rather than this function guessing for all of them.
    """
    import re

    raw_clean = str(query or "")
    clean_term = re.sub(r'[-*_.]', ' ', raw_clean)
    return [w.strip().lower() for w in clean_term.split() if w.strip()]


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

    search_words = split_list_search_words(q)
    entries, _total = list_mod.find_matching_entries(search_words, limit=None, name=name)
    rows = list_mod.entries_to_filelist_rows(entries, getattr(config, "NICKNAME", "?"))
    groups = list_mod.group_rows_by_folder(rows)
    page, total_folders, total_rows, row_capped = list_mod.page_folder_groups(
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
        # #477: distinguishes "this page is short because the safety valve
        # cut it" from "this page is short because it is the last one" - a
        # page of one huge folder otherwise reads as the pager being broken.
        "row_capped": row_capped,
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

    absent = bot_not_here_error(bot)
    if absent:
        return 409, {"error": absent}

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

    absent = bot_not_here_error(bot)
    if absent:
        return 409, {"error": absent}

    if dcc_fetch.has_outstanding_bot_alone_request(bot):
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}

    request_id = dcc_fetch.enqueue_fetch(bot, f"!rar {folder}", request_type="folder")
    if request_id is None:
        # Defense in depth - see build_list_fetch_enqueue_result()'s
        # identical comment above.
        return 409, {"error": BOT_ALONE_FETCH_CONFLICT_ERROR}
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


def build_crosslist_search_payload(term, limit=None):
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

    empty_payload = {
        "term": str(term or ""),
        "terms": terms,
        "folders": [],
        "total": 0,
        "total_files": 0,
        "returned": 0,
        "truncated": False,
        "matched": [],
        "empty": sorted(held),
    }
    if not terms or not held:
        return empty_payload

    names = list(held.values())
    rows = list_index.search(terms, limit=limit, bots=names)
    matched, empty = list_index.bots_with_a_match(terms, names)

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
                "nick": _display_nick(bot, present),
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
            "nick": _display_nick(bot, present),
            "list": "",
            "label": bot,
            "held": False,
            # Named by the operator rather than seen advertising (#376):
            # the page marks it, and offers to forget it.
            "hand_entered": bool(entry.get("hand_entered")),
            "online": (bot.lower() in present) if present else None,
            "fetched_at": 0,
            "count": now.get("files"),
            "freshness": "not_held",
            "advert_then": {},
            "advert_now": now,
        })

    rows.sort(key=lambda row: str(row["bot"]).lower())
    return rows


def _display_nick(bot, present):
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
    irc._flush_known_bots(force=True)
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
    irc._flush_known_bots(force=True)
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


# JavaScript's Number.MAX_SAFE_INTEGER. Past this a JSON number no longer
# survives the trip into the page unchanged.
_MAX_SAFE_JS_INT = 2 ** 53 - 1


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
    re-parses the stored list_path FRESH on every call, via
    list_fetch.get_fetched_bot_page(), then returns one page of the result.
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
        # PER ITEM, not per request: a bulk paste is routinely several bots at
        # once, and one of them having signed off is no reason to refuse the
        # rest. It joins `errors`, which this route already reports beside
        # whatever it did manage to queue.
        absent = bot_not_here_error(bot)
        if absent:
            errors.append({"error": absent, "item": raw})
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
    payload = {
        "running": bool(getattr(config, "update_inprogress", False)),
        "ok": getattr(config, "last_list_update_ok", None),
        "error": getattr(config, "last_list_update_error", None),
        # How long the LAST finished rebuild took, whole seconds. None until
        # one has finished in this process - the same "never run yet is not a
        # claim about the last run" rule `ok` follows just above.
        "seconds": getattr(config, "last_list_update_seconds", None),
    }
    progress = read_list_progress()
    if progress:
        payload["progress"] = progress
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
# REGROUPED, and the old grouping is worth recording because it is how any
# grouping ends up: by accretion. Each feature put its settings wherever there
# was room, so "Slots & queue" had grown to 25 and "Paths & storage" to 31 -
# between them two thirds of the page - while the two settings that tune a
# transfer sat in DIFFERENT categories.
#
# From the operator: "Settings pages at webpage are a mess. Need better
# grouping, hiding some that are never used like folder locations under
# advanced settings etc. Also why is packet size on different page than buffer
# size."
#
# THREE RULES, applied here and worth keeping:
#
#   * Settings that are read together live together. DCC_BLOCK_SIZE and
#     DCC_SEND_BUFFER are the transfer-tuning pair - the 3 MB/s investigation
#     needed both - and were a category apart.
#   * A category is named for what an operator came looking for, not for the
#     part of the code it configures. The colour theme was under "Advertising
#     & search" because announce.py draws it.
#   * The things set once at install and never again go last, in their own
#     section. Seventeen file paths at the same level as MAX_DCC_SLOTS is
#     seventeen chances to wonder whether you should be changing one.
#
# NOTHING MOVES ON DISK. settings.conf is flat; a category is a grouping for
# this page and nothing else, so this reorders what an operator reads without
# touching a single stored value.
#
# Every name here is checked against config.py's actual annotations by
# SettingsPayloadTests' completeness guard, so a setting added later and never
# slotted in fails a test instead of silently never showing up.
SETTINGS_CATEGORIES = (
    ("identity",      "Identity & network",    ["SERVER", "PORT", "NICKNAME", "ALT_NICKNAME", "REJOIN_ATTEMPTS",
                                                "ADMIN_NICK", "CHANNEL", "DEBUG_CHANNEL"]),
    ("sharing",       "Sharing & queue",       ["MAX_DCC_SLOTS", "MAX_USER_QUEUE",
                                                "MAX_GLOBAL_QUEUE", "MAX_SEARCH_RESULTS",
                                                "PAUSE_ON_UPDATE", "REHASH_TRANSFER_WAIT"]),
    # The transfer-tuning pair, together. Anyone reaching for one wants the
    # other in front of them.
    ("transfers",     "Transfers",             ["DCC_BLOCK_SIZE", "DCC_SEND_BUFFER",
                                                "DCC_PORT_START", "DCC_PORT_END",
                                                "MAX_SEND_FAILS"]),
    ("your-list",     "Your list",             ["FILE_DIRECTORY", "LIST_BASE_NAME",
                                                "LIST_FORMAT", "LIST_IGNORED_EXTENSIONS",
                                                "SEPARATE_VIDEO_LIST", "LIST_VIDEO_EXTENSIONS",
                                                "LIST_VIDEO_COMPANION_EXTENSIONS",
                                                "RAR_ENABLED", "RAR_EXTENSIONS", "RAR_BINARY",
                                                "MAX_RAR_FOLDER_SIZE", "RAR_TIMEOUT",
                                                "LIST_UPDATE_TIMEOUT",
                                                "LIST_UPDATE_STALL_SECONDS",
                                                "LIST_HEADER_FILE",
                                                "LIST_HEADER_MAX_BYTES"]),
    ("fetching",      "Fetching from bots",    ["MAX_FETCH_SLOTS", "AUTO_REFETCH_LISTS",
                                                "AUTO_REFETCH_INTERVAL_HOURS",
                                                "AUTO_REFETCH_MAX_PER_RUN",
                                                "FETCH_OFFER_TIMEOUT",
                                                "FETCH_TRANSFER_TIMEOUT",
                                                "FETCH_FOLDER_OFFER_TIMEOUT",
                                                "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED",
                                                "FETCH_FOLDER_TRANSFER_TIMEOUT",
                                                "MAX_FETCH_FILE_SIZE",
                                                "MAX_FETCH_FOLDER_FILE_SIZE",
                                                "MAX_FETCH_LIST_FILE_SIZE",
                                                "MAX_LIST_TEXT_SIZE",
                                                "FETCH_HISTORY_DAYS",
                                                "FETCH_HISTORY_MAX_ROWS"]),
    ("advertising",   "Advertising & search",  ["ANNOUNCE_INTERVAL", "ANNOUNCE_TRANSFERS",
                                                "BROADCAST_SEARCH_CHANNEL",
                                                "BROADCAST_SEARCH_COOLDOWN", "CTCP_VERSION_REPLY",
                                                "MSG_DELAY", "DEBUG_MSG_DELAY"]),
    # Its own category, not a corner of "Advertising & search". The theme
    # lived there because announce.py is what draws it - which is a fact about
    # the code, not about what an operator came looking for.
    ("appearance",    "Appearance",            ["THEME", "CUSTOM_THEME_BORDER",
                                                "CUSTOM_THEME_SEPARATOR", "CUSTOM_THEME_TEXTBOX",
                                                "CUSTOM_THEME_VALUE", "CUSTOM_THEME_ALERT",
                                                "CUSTOM_THEME_ACCENT"]),
    ("anti-flood",    "Anti-flood",            ["MAX_REQUESTS", "REQUEST_WINDOW", "MUTE_TIME",
                                                "FLOOD_BAN_SECONDS"]),
    # Its own category for the reason Appearance gives above. The cooldown
    # below is an anti-flood setting by mechanism, but an operator looking
    # for it is thinking about private messages, not about flooding.
    ("private-messages", "Private messages",   ["PRIVATE_MESSAGES_ENABLED",
                                                "PRIVATE_MESSAGE_COOLDOWN_SECONDS",
                                                "PRIVATE_MESSAGE_DECLINE_TEXT",
                                                "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS",
                                                "PRIVATE_MESSAGE_DECLINE_BURST",
                                                "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS"]),
    ("admin-console", "Admin console",         ["ADMIN_HOSTMASKS", "ADMIN_CHAT_MODE",
                                                "ADMIN_CHANNEL_COMMANDS"]),
    ("web-dashboard", "Web dashboard",         ["WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT",
                                                "WEBUI_CONSOLE_ENABLED", "WEBUI_OPEN_BROWSER",
                                                "WEBUI_FOLDER_BROWSER_ENABLED"]),
    ("console-feed",  "Console feed",          ["CONSOLE_SHOW_REQUESTS", "CONSOLE_SHOW_QUEUE",
                                                "CONSOLE_SHOW_SENDS", "CONSOLE_SHOW_FAILURES",
                                                "CONSOLE_SHOW_SEARCHES", "DEBUG_CHANNEL_FEED"]),
    ("debug",         "Debug & logging",       ["DEBUG_MODE", "DEBUG_TO_CHANNEL",
                                                "DEBUG_TO_CONSOLE",
                                                "CONSOLE_TIMESTAMP_FORMAT", "PROJECT_URL"]),
    # LAST, and named so nobody opens it by accident. Set once at install, and
    # a wrong value here loses a queue or a statistics file rather than
    # mis-tuning something. They were interleaved with the settings changed
    # weekly.
    ("advanced",      "Advanced: file locations",
                                               ["TMP_ZIP_DIR", "LOCAL_LIST_DIR",
                                                "FETCHED_FILES_DIR", "BANS_FILE", "HARD_BANS_FILE",
                                                "STATS_FILE", "KNOWN_BOTS_FILE",
                                                "FETCHED_BOT_LISTS_FILE", "LIST_INDEX_FILE",
                                                "FETCH_HISTORY_FILE", "DOWNLOAD_COUNTS_FILE",
                                                "LIST_SIZE_FILE", "LIST_RAWBYTES_FILE",
                                                "LIST_PROGRESS_FILE", "LIBRARY_FOLDERS_FILE",
                                                "LISTS_FILE", "ON_CONNECT_FILE",
                                                "NOTICES_FILE",
                                                "PRIVATE_MESSAGES_FILE"]),
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
    "MAX_FETCH_FILE_SIZE": "Max fetch file size",
    "MAX_LIST_TEXT_SIZE": "Largest list text accepted from a peer",
    "DCC_BLOCK_SIZE": "Packet size",
    "DCC_SEND_BUFFER": "Socket send buffer (0 = the default for your platform)",
    "REHASH_TRANSFER_WAIT": "Seconds a rehash waits for transfers to finish",
    "AUTO_REFETCH_LISTS": "Re-fetch a held list when its bot advertises a new one",
    "AUTO_REFETCH_INTERVAL_HOURS": "Least time between re-fetches of one bot (hours)",
    "AUTO_REFETCH_MAX_PER_RUN": "Most lists to re-fetch in one sweep",
    "FETCH_TRANSFER_TIMEOUT": "Fetch transfer timeout (seconds)",
    # Named from the operator's side, like the pill in the Downloads table:
    # this is the wait AFTER we send a request, not a timeout on an offer
    # anybody made us.
    "FETCH_OFFER_TIMEOUT": "Wait for a reply to a fetch request (seconds)",
    "FETCH_FOLDER_OFFER_TIMEOUT": "Wait for a reply to a folder (.rar) request (seconds)",
    "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED":
        "...from a bot that publishes no .rar list (seconds)",
    "MAX_FETCH_FOLDER_FILE_SIZE": "Max folder (.rar) fetch size",
    "MAX_FETCH_LIST_FILE_SIZE": "Max fetched master-list zip size",
    "FETCH_FOLDER_TRANSFER_TIMEOUT": "Folder (.rar) fetch transfer timeout (seconds)",

    "LIST_BASE_NAME": "List base name",
    "PAUSE_ON_UPDATE": "Pause sharing during !update",
    # Named for what it now IS. From an operator: "under Paths & Storage, this is not
    # needed anymore" - not quite, it is still the fallback for an install
    # with no folder list, which is most of them. But presenting it as a
    # plain "Music directory" beside a folder editor that overrides it is
    # what made it read as the setting that matters.
    "FILE_DIRECTORY": "Music directory (used only when no folders are set)",
    "LIST_FORMAT": "List delivery format",
    "LIST_IGNORED_EXTENSIONS": "File types to leave out of the list",
    "SEPARATE_VIDEO_LIST": "Publish film and series as a separate list",
    "LIST_VIDEO_EXTENSIONS": "File types that go in the film list",
    "LIST_VIDEO_COMPANION_EXTENSIONS": "File types that follow a film into its list (subtitles, .nfo, .sfv)",
    "RAR_EXTENSIONS": "File types a folder needs to be !rar-packable",
    "RAR_ENABLED": "Enable !rar folder packing",
    "RAR_BINARY": "RAR binary path",
    "TMP_ZIP_DIR": "Temp archive directory",
    "MAX_RAR_FOLDER_SIZE": "Largest folder !rar will pack (0 = no limit)",
    "LOCAL_LIST_DIR": "Master list directory",
    "FETCHED_FILES_DIR": "Fetched files directory",
    "BANS_FILE": "Bans file",
    "STATS_FILE": "Stats file",
    "HARD_BANS_FILE": "Hard bans file",
    "KNOWN_BOTS_FILE": "Known bots file",
    "LIST_INDEX_FILE": "Cross-list search index",
    "DOWNLOAD_COUNTS_FILE": "Download counts file",
    "FETCHED_BOT_LISTS_FILE": "Fetched bot lists file",
    "FETCH_HISTORY_FILE": "Fetch history file",
    "NOTICES_FILE": "Operator notices file",
    "PRIVATE_MESSAGES_FILE": "Private messages file",
    "PRIVATE_MESSAGE_COOLDOWN_SECONDS": "Record one private message per sender every (seconds)",
    "PRIVATE_MESSAGES_ENABLED": "Keep private messages (off: keep none, reply once instead)",
    "PRIVATE_MESSAGE_DECLINE_TEXT": "That reply's wording (%admin becomes the admin nick)",
    "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS": "Reply to the same sender once every (seconds)",
    "PRIVATE_MESSAGE_DECLINE_BURST": "Most replies to send in one burst window",
    "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS": "How long that burst window is (seconds)",
    "LIST_SIZE_FILE": "List size file",
    "LIST_PROGRESS_FILE": "List rebuild progress file",
    "LIST_RAWBYTES_FILE": "List raw bytes file",
    "LIST_HEADER_FILE": "List banner file",
    "LIST_HEADER_MAX_BYTES": "List banner size limit",
    "LIBRARY_FOLDERS_FILE": "Served folders file",
    "LISTS_FILE": "Served lists file",
    "ON_CONNECT_FILE": "On-connect commands file",

    "THEME": "Colour theme",
    "CUSTOM_THEME_BORDER": "Custom theme: border colour",
    "CUSTOM_THEME_SEPARATOR": "Custom theme: separator colour",
    "CUSTOM_THEME_TEXTBOX": "Custom theme: text box colour",
    "CUSTOM_THEME_VALUE": "Custom theme: value colour",
    "CUSTOM_THEME_ALERT": "Custom theme: alert colour",
    "CUSTOM_THEME_ACCENT": "Custom theme: accent colour",
    "ANNOUNCE_TRANSFERS": "Announce finished transfers in the channel",
    "REJOIN_ATTEMPTS": "Rejoin attempts after a kick (0 = never)",
    "ANNOUNCE_INTERVAL": "Advert interval (seconds)",
    "BROADCAST_SEARCH_CHANNEL": "Broadcast search channel",
    "BROADCAST_SEARCH_COOLDOWN": "Broadcast search cooldown (seconds)",
    "CTCP_VERSION_REPLY": "Answer CTCP VERSION",

    "MAX_REQUESTS": "Max requests per window",
    "REQUEST_WINDOW": "Request window (seconds)",
    "MUTE_TIME": "Mute duration (seconds)",
    "FLOOD_BAN_SECONDS": "Ban after flooding while muted (seconds)",
    "MAX_SEND_FAILS": "Max send failures",
    "RAR_TIMEOUT": "RAR pack timeout (seconds)",
    "LIST_UPDATE_TIMEOUT": "List rebuild hard cap (seconds, 0 = none)",
    "LIST_UPDATE_STALL_SECONDS": "Give up if a rebuild reports nothing for (seconds)",

    "ADMIN_HOSTMASKS": "Admin hostmasks",
    "ADMIN_CHAT_MODE": "DCC chat connection mode",
    "ADMIN_CHANNEL_COMMANDS": "Allow admin commands in channel",

    "WEBUI_ENABLED": "Enable web dashboard",
    "WEBUI_HOST": "Host",
    "WEBUI_PORT": "Port",
    "WEBUI_FOLDER_BROWSER_ENABLED": "Folder picker on the Settings page",
    "WEBUI_CONSOLE_ENABLED": "Enable the Console page (remote admin)",
    "WEBUI_OPEN_BROWSER": "Open the dashboard in a browser at startup",

    "DEBUG_MODE": "Debug mode",
    "DEBUG_TO_CHANNEL": "Send debug lines to channel",
    "DEBUG_TO_CONSOLE": "Send debug lines to admin console",
    "CONSOLE_SHOW_REQUESTS": "Show requests (who asked for what)",
    "CONSOLE_SHOW_QUEUE": "Show queue positions",
    "CONSOLE_SHOW_SENDS": "Show transfers starting, resuming and completing",
    "CONSOLE_SHOW_FAILURES": "Show failed transfers",
    "CONSOLE_SHOW_SEARCHES": "Show searches and their result counts",
    "DEBUG_CHANNEL_FEED": "Also send requests, queue positions, starts and searches to the IRC debug channel",
    "CONSOLE_TIMESTAMP_FORMAT": "Time prefix on every console line (strftime; blank = none)",
    "PROJECT_URL": "Project URL",
}


# A size an operator reads in the unit they think in, while the file keeps
# bytes. Counting the zeros in 10737418240 to check it says ten gigabytes is
# not work anybody should be doing, and the sizes here span 8 KB to 10 GB.
#
# STORED IN BYTES, UNCHANGED. settings.conf, admin_config.py and every reader
# in the daemon keep the number they have always had, so nothing migrates and
# an operator who edits the file by hand sees exactly what they saw before.
# Only the dashboard divides, and only for display.
#
# The unit is per setting rather than picked from the magnitude: a value that
# changed unit as it grew would move under the operator mid-edit, and 0 -
# which several of these use for "no limit" - has no magnitude to read.
# KB where MB would print 0.0078.
SETTINGS_UNITS = {
    "MAX_RAR_FOLDER_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_FILE_SIZE": ("MB", 1024 * 1024),
    "MAX_LIST_TEXT_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_FOLDER_FILE_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_LIST_FILE_SIZE": ("MB", 1024 * 1024),
    "DCC_SEND_BUFFER": ("KB", 1024),
    "LIST_HEADER_MAX_BYTES": ("KB", 1024),
}

# DCC_BLOCK_SIZE is a menu, not a number to type, so it keeps its byte values
# as the stored choice and gains readable text beside each one.
CHOICE_LABELS = {
    "DCC_BLOCK_SIZE": {str(1024 * n): f"{n} KB"
                       for n in (4, 8, 16, 32, 64, 128)},
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
        labels = CHOICE_LABELS.get(name)
        if labels:
            field["choice_labels"] = [labels.get(str(c), str(c))
                                      for c in field["choices"]]

    # A COLOUR THE OPERATOR CAN READ, AND PICK. config holds the decoded
    # bytes - a role of "\x0313" is four characters, two of them a control
    # code - and the page was being handed them raw. A text input cannot show
    # 0x03, so the field for that accent read "13": the code was present,
    # invisible, and what the operator could see was not what was set. Typing
    # back what they read would have put a literal "13" in every advert.
    #
    # So the value crosses as the escape text settings.conf.sample documents,
    # which is also what the save writes back - and `irc_colour` tells the
    # page it may offer the sixteen colours as menus instead of asking for a
    # control code to be typed into a box that cannot display one.
    if name.startswith("CUSTOM_THEME_"):
        import settings_file as _settings_file
        field["value"] = _settings_file.encode_irc_escapes(value or "")
        field["irc_colour"] = True

    unit = SETTINGS_UNITS.get(name)
    if unit:
        field["unit"], field["unit_factor"] = unit

    # WHAT THE SETTING MEANS (#528). "A lot of settings are not easy to
    # understand" - and the explanation for every one of them already
    # existed, as the comment block beside it in defaults.py, which
    # settings.conf.sample is generated from. settings_help reads the same
    # block, so the code, the sample and the page can never disagree. The
    # page draws a "?" beside the label that shows this on hover; a setting
    # with nothing to say gets no "?" rather than an empty box.
    import settings_help
    help_text = settings_help.help_text(name)
    if help_text:
        field["help"] = help_text

    # A TRI-STATE NEEDS A THIRD ANSWER. WEBUI_CONSOLE_ENABLED is declared
    # `bool = None`, and None does not mean False: console_is_enabled() reads
    # it as "yes if nobody else can reach it", so a stock loopback install has
    # the Console ON. The page renders a bool as a checkbox from `!!value`,
    # which drew None as unchecked - telling the operator that ban, unban,
    # clearqueue, rehash and update were NOT reachable behind the dashboard
    # password when they were.
    #
    # A note rather than sending the effective value as `value`: that would
    # make the checkbox truthful, but the next save would then write an
    # explicit True, and an operator who later moved the dashboard onto the
    # LAN would keep a Console that should have switched itself off. The
    # value stays unset; what changes is that the page says what unset means
    # here and now.
    if name == "WEBUI_CONSOLE_ENABLED" and value is None:
        field["note"] = ("Not set: on while the dashboard is loopback-only. "
                         "Currently " + ("ON" if console_is_enabled() else "OFF") + ".")
    return field
# The roles a theme has, and the one sample line each of them shows up in.
# Named here so the page can say which setting it is that the operator just
# changed nothing visible with - and so a role added later without a sample is
# a failing test rather than a silent gap.
THEME_PREVIEW_ROLES = {
    "border": "both", "separator": "both", "textbox": "both",
    "value": "both", "alert": "both", "accent": "notice",
}


def build_theme_preview(overrides=None):
    """The two lines a theme is judged by, rendered with `overrides`.

    WHY A PREVIEW EXISTS. The six CUSTOM_THEME_* settings hold raw mIRC colour
    codes, typed into text boxes. Finding out what a change did meant saving
    it, rehashing, and watching the channel for the next advert - and the next
    advert is up to ANNOUNCE_INTERVAL away, on a bot other people are using.

    WHY BOTH LINES. Between them they use all six roles, and neither uses all
    six alone: the advert never touches `accent`, only the completion notice
    does. A single sample would leave one setting looking like it does
    nothing, which is the confusion this is meant to end.

    Built by announce.py's own builders, not by a copy of their templates. A
    copy drifts the first time one of them changes, and then the preview lies
    with a straight face.

    `overrides` is what the operator has typed and not saved. It never reaches
    config: a live daemon is serving a channel while they are choosing
    colours.

    Returns the RAW lines, colour codes and all. Rendering them is the page's
    job - it is the one that knows how wide the box is.
    """
    import announce

    settings = dict(overrides or {})
    nick = str(getattr(config, "NICKNAME", "") or "DCCore")
    channel = (str(getattr(config, "CHANNEL", "") or "#channel")
               .split(",")[0].strip() or "#channel")

    # Plausible rather than real. Reading the live figures would make the
    # preview flicker as transfers come and go, and a sample that changes
    # while you are comparing two colours is a sample you cannot compare.
    advert = announce.build_advert_line(
        channel, nick, "719,041", "5.48 TB", "Sep 7th", "3/3", "0",
        "2.4MB/s", "24.7MB/s", "1,204 Files (8.9 TB)",
        str(config.SCRIPT_VERSION), settings=settings)
    notice = announce.build_transfer_complete_line(
        channel, "someuser", "Some Artist - Some Album - 01 - A Track.flac",
        "1,204 Files (8.9 TB)", "37", "12", "3:04 pm", "24.7MB/s",
        settings=settings)

    # The PRIVMSG envelope is protocol, not something anybody sees in a
    # channel. Showing it would put "PRIVMSG #chan :" at the front of a
    # preview of what the channel looks like.
    def body(line):
        return line.split(" :", 1)[1].rstrip("\r\n") if " :" in line else line

    return {
        "advert": body(advert),
        "notice": body(notice),
        "roles": THEME_PREVIEW_ROLES,
    }


def theme_preview_overrides(payload):
    """The subset of a posted body that may steer a preview.

    A whitelist, not a filter: this reaches theme.palette(), and everything
    else on the settings page has nothing to say about colour. THEME is
    checked against the presets that exist, because an unknown name would
    otherwise pick the configured one and quietly preview the wrong thing.

    AND IT COERCES THE WAY THE FILE WOULD. A colour crosses the wire as the
    escape text `\x0313` - see _settings_field() for why it must, and
    settings_file.encode_irc_escapes() for the other half of that trip - while
    theme.palette() deals in the decoded BYTE, which is what theme.CLASSIC and
    every other preset holds.

    Nothing was decoding it here, so the preview rendered the operator's typed
    text as nine literal characters. The advert sample began with a visible
    `\x0309,07` and overflowed to the right, which is exactly the failure the
    preview exists to show them they are about to cause. Reported against the
    colour picker, but it arrived with the preview itself and would have hit
    anyone who typed a code by hand.

    Through settings_file.coerce(), not a private copy of the rule: the
    preview's whole claim is that it shows what saving would produce, and two
    functions that merely happen to agree today are two functions that can
    stop agreeing.
    """
    import settings_file
    import theme

    body = json_object(payload)
    wanted = {}
    name = str(body.get("THEME", "") or "").strip().lower()
    if name in theme.THEMES:
        wanted["THEME"] = name
    for role in theme.ROLES:
        key = f"CUSTOM_THEME_{role.upper()}"
        if key in body:
            value = body.get(key)
            wanted[key] = (settings_file.coerce(key, value, "", str)
                           if isinstance(value, str) else "")
    return wanted


def build_notices_payload():
    """GET /api/notices: what the operator has not been told, and the rest.

    Newest FIRST here, and oldest first in storage. The store appends, which
    is the cheap end to write; a panel is read from the top, which is where
    the thing that just happened belongs. Reversing at the boundary keeps both
    ends natural rather than making one of them pay for the other.

    `unread` and `severity` are what the badge draws, and they are computed
    here rather than in the page: the page would have to know the read marker
    to work them out, and two places computing "is there anything new" is two
    places that can disagree about whether to light up.
    """
    import announce

    count, worst = announce.unread_notices()
    with runtime.notices_lock:
        rows = list(reversed(config.notices))
        seen = int(config.notice_state.get("seen_id", 0) or 0)

    return {
        "notices": rows,
        "unread": count,
        "severity": worst,
        "seen_id": seen,
    }


def mark_notices_read_result():
    """POST /api/notices/read: the operator has looked. Returns the payload
    the page would have got from a fresh GET, so the badge clears from the
    same answer that cleared it rather than from a second round trip that
    could race a notice arriving in between."""
    import announce

    announce.mark_notices_read()
    return build_notices_payload()


def messages_are_off():
    """True when this bot keeps no private messages at all."""
    return not getattr(config, "PRIVATE_MESSAGES_ENABLED", True)


def messages_payload_or_404():
    """(payload, status) for GET /api/messages.

    404, NOT an empty list. An empty list means "nobody has messaged you",
    which is a fact about the world; this is "there is no such page here",
    which is a fact about the bot. web/app.js hides the Messages nav item on
    exactly this status - the same signal the Console has used since it got
    an off-switch.

    Out here rather than inside the route because Flask is an optional
    dependency: a bot running without it still serves nothing, and logic that
    lives in a route body is logic no test in this repo can reach.
    """
    if messages_are_off():
        return {"error": "Private messages are turned off."}, 404
    return build_messages_payload(), 200


def messages_read_or_404():
    """(payload, status) for POST /api/messages/read. Same gate: turned off
    while somebody had the page open must not be a button that silently does
    nothing."""
    if messages_are_off():
        return {"error": "Private messages are turned off."}, 404
    return mark_messages_read_result(), 200


def build_messages_payload():
    """GET /api/messages: private messages nobody answered.

    Newest FIRST here and oldest first in storage, for the reason
    build_notices_payload() gives: appending is the cheap end to write, and a
    list is read from the top.

    No reply field and no per-message action that sends anything. The bot has
    no conversation path at all - see announce.record_private_message() - and
    an API that looked like it could answer would be a promise the daemon
    cannot keep.
    """
    import announce

    with runtime.private_messages_lock:
        rows = list(reversed(config.private_messages))
        seen = int(config.private_message_state.get("seen_id", 0) or 0)

    return {
        "messages": rows,
        "unread": announce.unread_private_messages(),
        "seen_id": seen,
    }


def mark_messages_read_result():
    """POST /api/messages/read: the operator has looked. Returns what a fresh
    GET would say, so the count and the list cannot disagree."""
    import announce

    announce.mark_private_messages_read()
    return build_messages_payload()


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

    Read under runtime.config_reload_lock, because this page is what found the
    reload window (see that lock's comment). Saving a setting from here starts
    a rehash on a background thread and returns immediately; the browser then
    re-fetches this endpoint at once and, without the lock, read config while
    importlib.reload(defaults) was part way through re-executing it - so the
    Settings page rendered Nickname, Admin nick(s) and Channels blank on a
    daemon whose settings.conf had all three. The lock makes this wait for the
    reload it just triggered rather than reporting its halfway state.
    """
    import settings_file
    with runtime.config_reload_lock:
        return _settings_payload_unlocked(settings_file)


def _settings_payload_unlocked(settings_file):
    """build_settings_payload()'s body, split out only so the lock above wraps
    one expression and cannot be left un-taken by a later edit adding an early
    return."""
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
# Settings a running daemon cannot pick up, whatever it reloads.
#
# NOT "everything to do with the network" - most of it IS live, and listing
# those here would train an operator to ignore the notice. The DCC port range
# is read per send, so a rehash applies it to the very next transfer; the
# channel list is compared and JOIN/PARTed by the rehash itself.
#
# What is genuinely stuck is anything read ONCE, against a socket already open:
#
#   WEBUI_*        oserve.startup() starts the dashboard thread once, bound to
#                  the host and port it read then. Changing them from inside
#                  that dashboard cannot move it.
#   SERVER / PORT  irc.py reads these in connect(), and the connection the bot
#                  is on was made with the old ones. #302 named this exactly -
#                  "when a user changes ports ... the changes do not take
#                  effect immediately" - and they were the two missing here, so
#                  the save said nothing and the operator was left to work out
#                  why the bot was still on the old server.
#
# A restart is PROMPTED, not performed. #302 offered either; restarting a
# process from inside itself while it holds live transfers is a different order
# of risk from telling the operator what to do next.
SETTINGS_RESTART_ONLY = {"WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT",
                         "SERVER", "PORT"}


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


def build_on_connect_payload():
    """GET /api/on-connect: the commands sent once registered, and the gap.

    THE COMMANDS COME BACK IN FULL, and that is a decision rather than an
    oversight. One of them is very likely an X login with a password in it, so
    the alternative is a box an operator can overwrite but never read - which
    means never correcting a typo without retyping the lot.

    They are already behind the dashboard login, and anybody who has that can
    read data/on_connect.json off the disk anyway. What must NOT happen is the
    text reaching a log or a debug channel, and that is enforced where it would
    happen - see on_connect.redacted().
    """
    import on_connect

    commands, delay = on_connect.load()
    return {
        "commands": commands,
        "delay_seconds": delay,
        "max_commands": on_connect.MAX_COMMANDS,
        "max_delay_seconds": on_connect.MAX_DELAY_SECONDS,
    }


def apply_on_connect_changes(payload):
    """POST /api/on-connect: validate the whole set, then write it.

    Returns (http_status, payload_dict).

    NO REHASH. These are read fresh at every connect, so the next one picks
    them up - and rehashing to apply them would be misleading, because what
    they need is a reconnect, not a reload. The response says so.
    """
    import on_connect

    if not isinstance(payload, dict):
        return 400, {"error": "Expected an object with 'commands'."}

    raw = payload.get("commands")
    if isinstance(raw, str):
        # The page sends a textarea, one command per line, because that is what
        # an operator pastes. Splitting here rather than in the browser keeps
        # the API usable by hand.
        raw = raw.splitlines()
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return 400, {"error": "'commands' must be a list or a block of text."}

    delay = payload.get("delay_seconds", on_connect.DEFAULT_DELAY_SECONDS)

    try:
        written = on_connect.save(raw, delay)
    except ValueError as err:
        return 400, {"error": str(err)}
    except OSError as err:
        return 500, {"error": f"Could not write the commands: {err}"}

    return 200, {
        "commands": written,
        "delay_seconds": float(delay),
        "reconnect_required": bool(written),
        "message": ("Saved. They run at the next connection - use them now "
                    "by reconnecting." if written
                    else "Cleared. Nothing is sent on connect."),
    }


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
            "folders": [{"name": f.name, "path": f.path} for f in entry.folders],
        })
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
            folders))

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

    AN EMPTY LIST IS ALLOWED and means "go back to the single Music
    directory". The file is REMOVED rather than written as [], because
    library.load_folders() already returns None for an empty list and falls
    back - so writing [] would leave a file on disk that does nothing, and the
    next operator to read it would have to work that out.

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

    target = library.folders_file()
    if not entries:
        try:
            if os.path.exists(target):
                os.remove(target)
        except OSError as err:
            return 400, {"error": f"Could not remove {os.path.basename(target)}: {err}"}
        return 200, dict(build_folders_payload(), written=0,
                         rebuild_required=True)

    try:
        library.save_folders(entries)
    except (ValueError, OSError) as err:
        return 400, {"error": str(err)}

    return 200, dict(build_folders_payload(), written=len(entries),
                     rebuild_required=True)




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


# ---------------------------------------------------------------------
# CONSOLE
#
# A web-native alternative to the DCC CHAT admin console (adminchat.py) and
# the debug channel, for an operator who wants neither: no second IRC client
# to keep open, no channel broadcasting the daemon's internals to whoever
# joins it.
#
# Two halves, kept separate on purpose:
#   - the LOG is the ambient stream every announce.send_debug() call already
#     produces. Registering below as another consumer of the same fan-out
#     adminchat.py's own DCC sessions use (announce.add_debug_sink) - nothing
#     downstream of send_debug() has to learn a new destination exists, and
#     DEBUG_TO_CONSOLE (already on by default, independent of DEBUG_TO_CHANNEL
#     / DEBUG_CHANNEL) is the only switch that gates it - see send_debug()'s
#     own "ROUTING" comment. An operator who wants no debug channel at all
#     still gets this for free.
#   - a COMMAND is request/response: POST one line, get back exactly the
#     lines that one command produced, not interleaved with the ambient log.
#
# adminchat.COMMANDS is reused directly rather than re-implemented: a second
# copy of "queue", "status", "rehash" and the rest would drift the moment one
# of them changed - the same shape as every drift this codebase has already
# been bitten by (PRESERVE_RUNTIME, the two check-setup.py copies).
#
# announce.py is reloaded on every !rehash (it is in commands.CORE_MODULES),
# which resets announce._debug_sinks to [] - but commands.py's rehash handler
# already snapshots every live sink before that reload and reattaches the
# same objects after (see reattach_debug_sinks()), generically, regardless of
# which module registered them. webserver.py is NOT itself in CORE_MODULES -
# it is never reloaded - so _console_log and _console_next_id need none of
# the "survive my own reload" handling announce._debug_queue uses for itself.
# ---------------------------------------------------------------------

_console_log = collections.deque(maxlen=500)
_console_log_lock = threading.Lock()
_console_next_id = 1
_console_sink_registered = False


def _console_debug_sink(msg_text, category="INFO"):
    """announce.add_debug_sink's callback shape: (plain text, category).

    Must not block and must not raise - announce.py's own comment on the sink
    contract says why: send_debug() is called from the IRC read thread. A
    plain append under a short lock, the same shape as
    adminchat.Session.send().

    strip_irc_formatting(), not a fresh copy of it: some send_debug() callers
    embed mIRC control characters of their own inside msg_text for the
    channel's benefit, and adminchat.Session.debug_sink() already strips them
    for the identical reason - a console is read as a log, not rendered by an
    IRC client.
    """
    global _console_next_id
    with _console_log_lock:
        _console_log.append({
            "id": _console_next_id,
            "category": str(category),
            "time": time.time(),
            "text": adminchat.strip_irc_formatting(msg_text),
        })
        _console_next_id += 1


def _open_in_browser(host, port, opener=None, log=print):
    """Open the dashboard in the default browser, if that was asked for.

    LOOPBACK ONLY, and not because of security - because of what the machine
    probably is. A dashboard bound to the LAN is as likely to be running on a
    headless box as on somebody's desktop, and a daemon that tries to spawn a
    browser there is doing something nobody asked for and nobody will see.

    Never fatal. A machine with no browser, no display, or a wayward
    BROWSER variable is not a reason to stop the bot starting - the address
    was printed a line above either way.
    """
    if not getattr(config, "WEBUI_OPEN_BROWSER", True):
        return False
    if not dashboard_is_loopback_only(host):
        return False
    try:
        import webbrowser

        (opener or webbrowser.open)(f"http://{host}:{port}/")
        return True
    except Exception as err:
        log(f"[WEBUI] Could not open a browser ({err}); the dashboard is "
            f"still running at http://{host}:{port}/")
        return False


def dashboard_is_loopback_only(host=None):
    """Is the dashboard reachable only from the machine it runs on?

    The whole question the Console's default turns on. Anything that is not a
    loopback address means somebody else can reach the login page, and from
    there the Console is admin behind one factor.

    Unparseable is treated as NOT loopback. A host this cannot read is a host
    this cannot vouch for, and the safe answer to "is this exposed" is yes.
    """
    import ipaddress

    text = str(host if host is not None
               else getattr(config, "WEBUI_HOST", "127.0.0.1")).strip()
    if not text:
        return False
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def console_is_enabled():
    """Whether the dashboard's Console is available.

    An explicit True or False in the operator's own configuration always wins:
    somebody who has already made this choice does not get it made again for
    them on upgrade.

    With no explicit value the answer is "yes if nobody else can reach it".
    The Console puts ban/unban/clearqueue/rehash/update behind the dashboard
    password alone - one factor, over plain HTTP - which is a real widening
    when the dashboard is on the LAN and no widening at all when it is on
    loopback, where the only person who can reach it already has the files and
    admin_config.py.
    """
    configured = getattr(config, "WEBUI_CONSOLE_ENABLED", None)
    if configured is not None:
        return bool(configured)
    return dashboard_is_loopback_only()


def _quiet_the_request_log():
    """Stop werkzeug printing a line per HTTP request onto the operator's
    console, unless DEBUG_MODE asks for it.

    From the beta: "maybe those lines shouldnt be visible on cmd.exe except
    you run dccore on something like debug mode. you miss the important lines
    like search results etc".

    Exactly that. The dashboard polls five endpoints every two seconds, so an
    idle bot with one page open writes on the order of a hundred lines a
    minute:

        127.0.0.1 - - [...] "GET /api/console/log?since=33 HTTP/1.1" 200 -

    Every one of them says the same thing - the dashboard is still open - and
    together they push the lines that matter (a search, a transfer, a
    disconnect) off the screen faster than anyone can read them. The console
    is the operator's only view on a daemon with no window; filling it with
    the dashboard's own heartbeat costs them that view.

    SILENCED, NOT REDIRECTED. Werkzeug's log is a development convenience,
    and every request it reports is one this process just served itself - the
    information is not lost, it was never news. ERROR is left through so a
    genuine failure inside the server still reaches the console.

    Never raises: a logging tweak is not a reason for the dashboard not to
    start.
    """
    if getattr(config, "DEBUG_MODE", False):
        return
    try:
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    except Exception as err:
        print(f"[WEBUI] Could not quieten the request log: {err}")


def _ensure_console_sink():
    """Register the console's debug sink, at most once.

    Safe to call from more than one place - the log route and start() both
    do, since a test that calls build_console_log_payload() directly never
    runs start() - because announce.add_debug_sink() is itself idempotent
    (a no-op when the sink is already present). The module-level flag just
    avoids importing announce on every poll.
    """
    global _console_sink_registered
    if _console_sink_registered:
        return
    import announce
    announce.add_debug_sink(_console_debug_sink)
    _console_sink_registered = True


def build_console_log_payload(since=0):
    """Every console log line newer than `since`, plus the cursor to poll
    from next.

    A cursor rather than an offset/limit pair: this is a live stream a client
    polls repeatedly, and an offset would either replay lines already shown
    or, worse, silently skip ones that arrived between two polls. since=0 -
    the first poll - returns whatever is currently buffered (up to 500
    lines): a newly opened console showing recent history is strictly more
    useful than one that starts blank, and costs nothing extra since the
    buffer already exists.
    """
    _ensure_console_sink()
    try:
        since = int(since)
    except (TypeError, ValueError):
        since = 0
    with _console_log_lock:
        lines = [line for line in _console_log if line["id"] > since]
        cursor = _console_next_id - 1
    return {"lines": lines, "cursor": cursor}


# Commands that make no sense over a stateless HTTP request. "quit" closes a
# DCC CHAT session (session.close()) - _WebConsoleSession below has no socket
# to close and no persistent identity for that to mean anything about.
_CONSOLE_UNSUPPORTED_COMMANDS = frozenset({"quit"})


class _WebConsoleSession:
    """Just enough of adminchat.Session's shape for COMMANDS' handlers, which
    is only ever .send() plus the two attributes handle_command() itself
    touches (.nick, .last_activity) - checked directly against adminchat.py,
    not guessed at. Never a real Session: no socket, no writer thread,
    nothing to close.
    """

    def __init__(self, nick):
        self.nick = nick
        self.last_activity = time.time()
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))


def build_console_command_result(command_text, remote_addr=None):
    """Run one admin command through adminchat.handle_command() and return
    exactly the lines it produced.

    Reuses the real command set rather than a second copy of it - see the
    CONSOLE section's own comment above for why.

    ASYNC COMMANDS RETURN AN ACKNOWLEDGEMENT, NOT THE RESULT. ban, unban,
    clearqueue, rehash and update each run the real work on a background
    thread (adminchat._run_detached) and reply immediately with only a
    "Banning ..." / "Rehashing ..." line - the same as a DCC CHAT session
    sees. Their eventual result is not lost: the handlers underneath all call
    announce.send_debug() when they finish, which reaches this same page
    through the LOG half above, a few seconds later. A caller of this
    function only ever gets what handle_command() produced synchronously.
    """
    stripped = str(command_text or "").strip()
    if not stripped:
        return 400, {"error": "No command given."}

    name = stripped.split(None, 1)[0].lower()
    if name in _CONSOLE_UNSUPPORTED_COMMANDS:
        return 200, {"lines": ["'quit' closes a DCC CHAT session; there is not "
                               "one here. Just close this tab."]}

    session = _WebConsoleSession(f"web:{remote_addr or 'unknown'}")
    adminchat.handle_command(session, stripped)
    return 200, {"lines": session.lines}


if HAVE_FLASK:

    def create_app():
        app = Flask(__name__, static_folder="web", static_url_path="")
        app.secret_key = os.urandom(32)
        # The mutating routes (broadcast search, fetch enqueue, list fetch)
        # all POST; Lax is the app's own decision instead of whatever the
        # visitor's browser happens to default to.
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        # A CEILING ON THE REQUEST BODY. Without one, Flask reads whatever is
        # sent into memory before any route decides what to do with it, and
        # this daemon shares a machine with transfers it must not starve.
        # 8MB is far above every legitimate body here - the largest by a wide
        # margin is a pasted vars.ini, a text file of a few hundred lines -
        # and Flask answers anything past it with 413 rather than buffering
        # it. The login page is behind this too, which is the point: the
        # ceiling applies before authentication, where the daemon has the
        # least reason to trust what it is being handed.
        app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

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

        @app.route("/api/stats/import/variables")
        def api_stats_import_variables():
            # Served rather than written into app.js so the page's filter and
            # the parser cannot drift: a field added to omenserve_import.FIELDS
            # is kept by the page without the JavaScript changing at all. If
            # they disagreed, the page would strip out a counter the parser
            # was waiting for, and the operator would be told their file has
            # nothing in it.
            import omenserve_import
            return jsonify({"variables": list(omenserve_import.variable_names())})

        @app.route("/api/stats/import/preview", methods=["POST"])
        def api_stats_import_preview():
            # Reads and writes nothing. POST rather than GET because the
            # vars.ini text travels in the body - it is a file's contents,
            # not an identifier.
            body = json_object(request.get_json(silent=True))
            return jsonify(build_stats_import_preview(body.get("text", "")))

        @app.route("/api/stats/import", methods=["POST"])
        def api_stats_import():
            body = json_object(request.get_json(silent=True))
            status, result = apply_stats_import(body)
            return jsonify(result), status

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
            # ?list= names one of OUR served lists; absent means the primary.
            # ?q= narrows it to rows matching every word in it (#399's
            # follow-up) - absent or blank means unfiltered, same as before.
            return jsonify(build_filelists_payload(
                offset, limit, name=requested_own_list(request.args.get("list")),
                q=request.args.get("q", "")))

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

        @app.route("/api/settings/theme-preview", methods=["POST"])
        def api_theme_preview():
            # POST because it carries what the operator has typed. It changes
            # nothing - see build_theme_preview() - and sits behind the same
            # login as every other route here.
            return jsonify(build_theme_preview(
                theme_preview_overrides(request.get_json(silent=True))))

        @app.route("/api/filelists/bots")
        def api_filelists_bots():
            # EVERY source the List Browser can show: ours, then theirs.
            #
            # Ours first and in the operator's own order rather than
            # alphabetically - they arranged that order themselves, and the
            # primary being first is the one piece of it the page should not
            # reshuffle. Composed HERE rather than inside either builder,
            # because each of those answers a question its own name states and
            # neither should start answering the other's.
            return jsonify(build_own_list_summaries()
                           + build_fetched_bot_list_summaries())

        @app.route("/api/filelists/sources", methods=["POST"])
        def api_filelists_add_source():
            body = json_object(request.get_json(silent=True))
            status, result = build_add_source_result(body.get("bot", ""))
            return jsonify(result), status

        @app.route("/api/filelists/sources/<nick>/remove", methods=["POST"])
        def api_filelists_remove_source(nick):
            # POST .../remove rather than DELETE, the shape /api/fetch/<id>/
            # delete already uses: one verb everywhere the page mutates.
            status, result = build_remove_source_result(nick)
            return jsonify(result), status

        @app.route("/api/filelists/purge-offline", methods=["POST"])
        def api_filelists_purge_offline():
            # POST, not DELETE: it does not name what to remove - the caller
            # asks "clear whatever is offline right now" rather than naming a
            # specific bot, so there is no resource URL for DELETE to name.
            status, result = build_purge_offline_fetched_lists_result()
            return jsonify(result), status

        @app.route("/api/filelists/search")
        def api_filelists_search():
            # Read-only, and it stays that way: this answers a question about
            # lists already on disk and queues nothing. Selecting a result and
            # queueing it goes through the existing POST /api/fetch/enqueue,
            # which is where the admission control for that already lives.
            _offset, limit = parse_pagination_params(
                None, request.args.get("limit"))
            return jsonify(build_crosslist_search_payload(
                request.args.get("q", ""), limit))

        @app.route("/api/filelists/bot/<nick>")
        def api_filelists_bot(nick):
            offset, limit = parse_pagination_params(
                request.args.get("offset"), request.args.get("limit"))
            status, result = build_fetched_bot_list_payload(
                nick, offset, limit, list_marker=request.args.get("list", ""),
                q=request.args.get("q", ""))
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

            # NOT send_from_directory(), and the reason is specific.
            #
            # dcc_fetch fits a stored name to MAX_NAME_BYTES (255) and touches
            # every path through platform_compat.long_path(). This route was
            # the one that did not, so on Windows a fetch with a long
            # remote-chosen name was written, marked "complete", listed in the
            # UI - and its Download button answered 404 for ever, while the
            # file sat there the whole time. Found by audit.
            #
            # Handing send_from_directory() the WRAPPED directory does not fix
            # it either: werkzeug's safe_join() joins with a FORWARD SLASH, and
            # a \\?\ path is the one kind Windows will not accept those in. So
            # the path is built here, with a backslash, and wrapped after.
            #
            # The containment safe_join() was providing is kept explicitly:
            # dcc.is_safe_path() is the same gate every request path in the
            # project uses, and it resolves symlinks, which safe_join() does
            # not. `import dcc` is deferred exactly like `import dcc_fetch`
            # above - see tests/test_import_graph.py.
            import dcc

            candidate = os.path.join(directory, stored_filename)
            if not dcc.is_safe_path(directory, candidate):
                print(f"[WEB SECURITY] Refused a fetch download outside "
                      f"{directory}: {stored_filename!r}")
                return jsonify({"error": "Unknown, incomplete, or failed fetch."}), 404

            wrapped = platform_compat.long_path(candidate)
            if not os.path.isfile(wrapped):
                return jsonify({"error": "The fetched file is no longer on disk."}), 404

            return send_file(wrapped, as_attachment=True,
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

        @app.route("/api/folders")
        def api_folders():
            return jsonify(build_folders_payload())

        # 404 rather than 403 when it is off, for the reason the console
        # routes give: 403 confirms the route exists and is merely disabled,
        # which tells anyone probing that this build has one worth returning
        # for.
        def _browser_is_available():
            return bool(getattr(config, "WEBUI_FOLDER_BROWSER_ENABLED", False))

        @app.route("/api/folders/browse")
        def api_folders_browse():
            if not _browser_is_available():
                return jsonify({"error": "not found"}), 404
            payload = build_folder_browse_payload(request.args.get("path", ""))
            return jsonify(payload), (400 if "error" in payload else 200)

        @app.route("/api/folders", methods=["POST"])
        def api_folders_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_folder_changes(body)
            return jsonify(result), status

        @app.route("/api/lists")
        def api_lists():
            return jsonify(build_lists_payload())

        @app.route("/api/lists", methods=["POST"])
        def api_lists_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_list_changes(body)
            return jsonify(result), status

        @app.route("/api/on-connect")
        def api_on_connect():
            return jsonify(build_on_connect_payload())

        @app.route("/api/on-connect", methods=["POST"])
        def api_on_connect_save():
            body = json_object(request.get_json(silent=True))
            status, result = apply_on_connect_changes(body)
            return jsonify(result), status

        @app.route("/api/settings/password", methods=["POST"])
        def api_settings_password():
            body = json_object(request.get_json(silent=True))
            status, result = build_password_change_result(
                body.get("new_password", ""), body.get("confirm_password", ""))
            return jsonify(result), status

        # 404, not 403, when the Console is off. 403 would confirm the routes
        # exist and are merely disabled, which tells anyone probing that this
        # build has an admin console worth coming back for. Not found is the
        # honest answer to "is there a console here" when there is not one
        # reachable, and the operator who wants it is looking at the setting,
        # not at the URL.
        #
        # Both routes check independently rather than sharing a decorator: they
        # are the whole attack surface of this feature, and a decorator applied
        # to one and forgotten on the other is a silent hole. Two lines each is
        # cheap enough to not be clever about.
        def _console_is_available():
            # ONE answer to "is the Console on", shared with the startup
            # notice and with anything else that asks. Reading the setting
            # directly here would have kept the old flat default alive on the
            # only paths that actually gate the feature - the routes - while
            # everything else moved.
            return console_is_enabled()

        @app.route("/api/filelists/<path:source>/purge", methods=["POST"])
        def api_filelists_purge(source):
            # POST, and <path:source> because a bot's other lists are named
            # "<nick>/<marker>" - a plain <source> stops at the slash and
            # would 404 on exactly those rows.
            status, result = build_fetched_list_purge_result(source)
            return jsonify(result), status

        @app.route("/api/messages")
        def api_messages():
            payload, status = messages_payload_or_404()
            return jsonify(payload), status

        @app.route("/api/messages/read", methods=["POST"])
        def api_messages_read():
            # POST because it changes what the operator has acknowledged.
            payload, status = messages_read_or_404()
            return jsonify(payload), status

        @app.route("/api/notices")
        def api_notices():
            return jsonify(build_notices_payload())

        @app.route("/api/notices/read", methods=["POST"])
        def api_notices_read():
            # POST because it changes what the operator has acknowledged.
            return jsonify(mark_notices_read_result())

        @app.route("/api/console/log")
        def api_console_log():
            if not _console_is_available():
                return jsonify({"error": "not found"}), 404
            return jsonify(build_console_log_payload(request.args.get("since")))

        @app.route("/api/console/command", methods=["POST"])
        def api_console_command():
            if not _console_is_available():
                return jsonify({"error": "not found"}), 404
            body = json_object(request.get_json(silent=True))
            status, result = build_console_command_result(
                body.get("command", ""), request.remote_addr)
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
    _ensure_console_sink()
    app = create_app()
    print(f"[WEBUI] Dashboard starting on http://{host}:{port}/ (login required).")
    if console_is_enabled():
        print("[WEBUI] The Console is on. It reaches ban, rehash and update "
              "behind this one password - see WEBUI_CONSOLE_ENABLED.")
    _quiet_the_request_log()
    _open_in_browser(host, port)
    try:
        # use_reloader=False is NOT optional: Flask's reloader re-execs the whole
        # process, and this runs on an already-live daemon thread - a re-exec
        # here would take the entire bot down with it, not just the dashboard.
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as run_err:
        print(f"[WEBUI] Dashboard stopped: {run_err}")
