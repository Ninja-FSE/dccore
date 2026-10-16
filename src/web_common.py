"""The dashboard's shared limits and helpers (#1311).

The first piece split out of webserver.py, which had grown to 7,000 lines
(docs/FUTURE.md, "Splitting the largest functions"). Its sections - stats,
other bots' lists, the search broadcast, fetching, settings, and the Flask
app - each used these, so no section could move out before they did without
importing webserver back. They are here, unchanged; webserver imports every
name back, so `webserver.<name>` still works for every caller and test.

No Flask here, and nothing that needs a request: what is here is plain
Python, testable on a machine without Flask installed, as webserver's own
build_*/apply_* functions always were.
"""

import os


# This module moved into src/ (#959); the web/ folder it serves as the
# dashboard's static root, and reads its language files from, did not - it
# stays a sibling of oserve.py, one directory up from here.
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


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
    # One rule, not a copy of it (#774): a "quoted phrase" means the same
    # here as in @find, and the two cannot drift apart.
    import list as list_mod
    return list_mod.split_search_term(query)


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


# Attributed nick for admin actions the dashboard dispatches on an operator's
# behalf - nobody is logged into IRC as "WEB-DASHBOARD", so this can never
# collide with (or impersonate) a real admin nick in a log line or in
# commands.py's own messaging. Shared by every such action below (rehash,
# list update): one identity, not a fresh one invented per route.
WEB_DASHBOARD_SOURCE = "WEB-DASHBOARD"
