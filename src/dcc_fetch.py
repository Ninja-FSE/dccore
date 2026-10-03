# dcc_fetch.py - Cross-bot file fetch: RECEIVING bytes an untrusted third party hands us.
"""We are the client here, not the server.

dcc.py is exclusively the "we are the trusted server choosing what to share"
role: it decides what exists in FILE_DIRECTORY, offers it, and streams it out.
This module is the opposite role - dialling an IP:port a foreign bot handed us
in a channel, and writing whatever bytes arrive to disk - and that distinction
matters for anyone auditing the code later. Nothing in here is trusted by
default:

  * an inbound "DCC SEND" CTCP is only ever acted on if it matches a fetch we
    ourselves requested moments earlier (admission control, see
    handle_incoming_offer()) - an unsolicited offer from anyone in the channel
    is logged and dropped, never dialled;
  * the declared size is capped BEFORE we connect (MAX_FETCH_FILE_SIZE);
  * the filename is never trusted as a literal path component;
  * a lying peer that sends more than it declared gets the partial file
    deleted and the transfer marked failed, not silently truncated.

State machine, owned entirely by this module:

    pending -> offered -> receiving -> complete
                   |  |          ^  ^
                   |  |          |  |
                   |  `-> listening-'
                   |                |
                   `-> queued ------'   (#926: the other bot said it queued
                                         our request; its DCC SEND may come
                                         hours later)
                   \\-----------------------------> failed (any timeout/
                                                      admission-rejection/
                                                      size-mismatch/connect-
                                                      error - see the row's
                                                      "reason" field)

`listening` is the passive/reverse-DCC branch: a third-party bot that cannot
accept an inbound connection (usually firewalled) answers our request with
`DCC SEND <filename> <ip> 0 <size> <token>` - port 0 plus a token is the
standard convention meaning "you listen, and reply with your own ip:port plus
this same token" (mirrors adminchat.py's identical handling of passive DCC
CHAT). A row only ever reaches `listening` AFTER admission control has
already matched it to an `offered` row we ourselves created, exactly like the
active path - see handle_incoming_offer() and _serve_passive_offer(). Once
the offering bot connects back, the row moves to `receiving` and joins the
same bounded-transfer code (_run_transfer()) the active path uses; if nobody
ever connects, it fails with reason "passive offer: no connection received"
and the listening socket is closed, never leaked.

Rows live in config.fetch_queue (config.py section 8), keyed by a generated
request id. webserver.py's /api/fetch/* routes are the only thing that
creates `pending` rows (POST /api/fetch/enqueue); check_fetch_queue() below
promotes them to `offered`; handle_incoming_offer() (dispatched from irc.py's
CTCP branch) takes it from there.

A `"folder"` row (a whole album/discography, requested as another bot's own
"!<bot> !rar <folder>" packing convention) walks this exact same state
machine as `"file"`/`"list"` - the only differences are which
_claim_matching_offer_locked() branch admits it (bot-alone, like `"list"`,
since we cannot know what the target bot will name the resulting .rar), a
longer FETCH_FOLDER_OFFER_TIMEOUT (packing a whole album takes real time on
the other end), and a larger MAX_FETCH_FOLDER_FILE_SIZE cap (a packed
archive is bigger than any single file).
"""

import ipaddress
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid

import defaults as config
import db
import dcc
import runtime
import transfer_log
import list as list_mod
import platform_compat

# Connect timeout for dialling the offering bot, and the idle-recv timeout
# once connected. Not config knobs - dcc.py does not expose its own mirror-
# image idle timeout (conn.settimeout(60.0), dcc.py:1024) as one either, and
# these are exactly that same convention on the receiving side.
CONNECT_TIMEOUT = 15.0
IDLE_RECV_TIMEOUT = 60.0

# How long we wait, once we start listening for a passive/reverse DCC SEND,
# for the offering bot to actually connect back. Same value and same
# reasoning as adminchat.LISTEN_TIMEOUT (60s "waiting for the operator to
# accept our offer back") - this is the identical protocol shape, just for
# DCC SEND instead of DCC CHAT, so there is no reason to pick a different
# number.
PASSIVE_LISTEN_TIMEOUT = 60.0

RECV_CHUNK = 65536

# The one convention that is actually standardised across file-sharing bots:
# "!<botnick> <filename>", the same syntax this bot itself answers to (see
# irc.py's get_bot_aliases()/dcc.handle_download_request). Used both by
# irc.py's broadcast-search capture (to offer a "Download" button) and here,
# defensively, nowhere else - dcc_fetch never parses this out of anything,
# it only ever receives a filename we ourselves already chose when the fetch
# was enqueued.
_FILENAME_CHARSET_RE = re.compile(r'[^\w\-_\. \(\)]')  # mirrors dcc.py's _sanitize_rar_leaf_name()

# THE canonical definition of "which bytes are unsafe to interpolate into a
# raw outbound IRC/CTCP line" - webserver.reject_if_unsafe_for_irc_line()
# imports and calls contains_unsafe_ctcp_bytes() below rather than keeping
# its own copy of this regex. That merge happened after this exact bug class
# recurred a THIRD time (once in webserver.py's web-enqueue routes, once
# here in dcc_fetch's offer parsing, and a third time because
# reject_if_unsafe_for_irc_line() rejected \r/\n but not \x01, so it was not
# actually equivalent to this check) - see this project's CONVENTIONS.md rule
# against writing one fact in two places. If you are ever tempted to add a
# second copy of this regex anywhere else in the codebase: don't - import
# this one instead.
#
# A value that gets interpolated into a raw outbound IRC line lets an
# embedded line break smuggle one or more ADDITIONAL lines (QUIT, JOIN/PART
# an arbitrary channel, PRIVMSG/NOTICE as this bot, ...) past whatever single
# line was intended. \x01 is unsafe for the same reason PLUS one more: any of
# these values may end up wrapped in a CTCP (\x01...\x01) reply (see
# _serve_passive_offer()) - an embedded \x01 closes that CTCP early and lets
# the attacker inject arbitrary trailing content into what the receiving
# peer parses as a second CTCP or as plain text. Rejecting \x01 everywhere,
# even for values that only ever reach a plain PRIVMSG body (not a CTCP), is
# deliberately conservative: a PRIVMSG body containing \x01 can itself be
# interpreted as an inline CTCP by the receiving client.
#
# Checked at PARSE time (parse_dcc_send_offer(), below) for both
# offer["filename"] (active and passive alike) and offer["token"] (passive
# only) - not only at the one call site that currently echoes them back
# (_serve_passive_offer()) - because this bug class had already recurred once
# by the time that check was added (see the module-merge note above).
# Rejecting the whole offer here means any future call site that touches
# offer["filename"]/offer["token"] inherits the protection automatically,
# instead of depending on every future author remembering to sanitize again.
_UNSAFE_CTCP_BYTES_RE = re.compile(r'[\r\n\x01]')


def contains_unsafe_ctcp_bytes(value):
    """True if `value` contains a byte that must never reach a raw outbound
    IRC/CTCP line unsanitized - see _UNSAFE_CTCP_BYTES_RE's comment above.

    Public (no leading underscore) because webserver.py's
    reject_if_unsafe_for_irc_line() imports and calls this directly - see
    that comment for why the two checks were merged into this one function.
    """
    return bool(_UNSAFE_CTCP_BYTES_RE.search(str(value)))


_FALLBACK_FETCH_LOCK = threading.Lock()


def _fetch_lock():
    """The dedicated fetch_queue lock oserve.py allocates at startup, or a
    shared module-level fallback for any caller that never ran
    oserve.startup() (tests, most notably).

    The fallback is a single object reused on every call, not a fresh
    `threading.Lock()` built on the spot: constructing a new lock per call
    would let two concurrent callers each acquire a DIFFERENT lock and
    achieve no mutual exclusion at all, silently. Same pattern as
    list_fetch.py's `_lock()` and runtime.channel_users_lock().
    """
    return getattr(config, "fetch_queue_lock", None) or _FALLBACK_FETCH_LOCK


def _ensure_fetch_queue():
    # config.fetch_queue is bound from runtime.py at import time and always
    # exists as a real dict - never rebind it here, see runtime.py's docstring.
    return config.fetch_queue


def new_fetch_row(bot, filename, now=None, request_type="file"):
    """Build a fresh `pending` row in the shape every reader of
    config.fetch_queue expects. Does not insert it - callers decide the key.

    request_type is "file" (default - existing behaviour: admission control
    requires an exact bot+filename match, see _claim_matching_offer_locked()),
    "list" (a cross-bot list fetch: we send a bare "@<bot>", per irc.py's own
    @<nick> trigger, and cannot know in advance what the target bot will name
    its list zip - admission control for these rows matches on bot alone), or
    "folder" (a cross-bot folder-as-rar fetch: we send "!<bot> !rar <folder
    path>" - the same convention this bot's own dcc.py "!rar" handler answers
    on its own nick - and, just like "list", cannot know in advance what the
    target bot will name the resulting .rar, so admission control for these
    rows also matches on bot alone). Every existing call site that does not
    pass request_type keeps getting "file" rows, so nothing about today's
    behaviour changes.

    "requested_filename" is set once here, to the same (stripped) value as
    `filename`, and never touched again - it preserves the original request
    text (e.g. "!rar Artist/Album") since _claim_matching_offer_locked() will
    later overwrite row["filename"] with whatever name the responding bot
    actually sends, exactly as it already does for "list" rows.
    """
    now = time.time() if now is None else now

    # THE ::INFO:: SUFFIX COMES OFF HERE. A row copied out of another bot's
    # list reads "Some Track.mp3  ::INFO:: 79.53MB", and the dashboard sends
    # what the operator clicked - so the whole line, size and all, was being
    # stored as the filename and asked for on the wire.
    #
    # The serving side has stripped this since #234 (dcc.py, where a request
    # ARRIVES); the fetching side never learned to, and the failure was
    # invisible until a peer answered:
    #
    #   Requested 'BBCRadio - Under Milk Wood.mp3  ::INFO:: 79.53MB' ...
    #   Rejected unsolicited DCC SEND ('BBCRadio_-_Under_Milk_Wood.mp3'):
    #   no matching pending request.
    #
    # _normalize_filename_for_match() already handles the space/underscore
    # swap every DCC client applies - it could never bridge a size that only
    # one side was carrying. Stripped at row creation rather than at match
    # time, because this value is also what goes out on the wire: the peer
    # above coped with the suffix, and a stricter one would not have.
    clean_filename = str(filename).strip()
    if request_type == "file":
        # Only for "file" rows. A "folder" row's requested_filename is the
        # literal "!rar <path>" request text, which has no ::INFO:: and must
        # survive untouched - new_fetch_row()'s own docstring depends on it.
        import list as list_mod

        clean_filename = str(list_mod.strip_info_suffix(clean_filename)[0]).strip()
    return {
        "bot": str(bot).strip(),
        "filename": clean_filename,
        "requested_filename": clean_filename,
        "request_type": request_type if request_type in ("file", "list", "folder") else "file",
        "state": "pending",
        "requested_at": now,
        "offered_at": None,
        "bytes_received": 0,
        "total_size": None,
        "reason": "",
        "stored_filename": None,
    }


_UNRESOLVED_FETCH_STATES = ("pending", "offered", "queued", "listening", "receiving")

# The states in which the other bot's DCC SEND is still expected (#926).
# "offered": we asked and are waiting, holding a slot. "queued": the other bot
# told us our request is in its queue - the file comes when our turn does, so
# the row stops holding a slot and stops timing out after FETCH_OFFER_TIMEOUT,
# but an offer for it must still be admitted when it finally arrives. Before
# this state existed that arrival was refused as unsolicited: fetching from a
# bot with a queue could only ever succeed with an empty queue.
_AWAITING_OFFER_STATES = ("offered", "queued")
# How long after giving up on silence a bot's late DCC SEND is still taken.
_LATE_OFFER_GRACE = 1800
# Times a file is asked for when the other bot does not answer, before it fails.
OFFER_ASKS = 3

# The states that count against FETCH_MAX_PER_BOT (#926): everything asked of a
# bot and not yet finished - including "queued", which holds no slot of ours but
# holds a place in THEIR queue. A bot allows each user only so many; asking for
# more gets "queue full", which is why AutoGet kept a per-server maximum.
_BOT_LOAD_STATES = ("offered", "queued", "listening", "receiving")

# A bot that was gone and came back gets a minute before we ask it anything
# (#926): it has just connected and may still be loading its list, and every
# other fetcher in the channel is asking at the same moment. AutoGet waited 30
# to 180 seconds after a JOIN for the same reason.
RETURN_DELAY_SECONDS = 60

# "Busy" - queue full, maxed out, rebuilding - is not "never" (#926). Such a
# request is asked again this many times, this far apart, before it fails.
BUSY_RETRIES = 3
BUSY_RETRY_SECONDS = 600

# Bots we have seen ABSENT, and when each was next seen present again. Only a
# bot that went away gets RETURN_DELAY_SECONDS; one present since we started
# is asked at once. Module state on purpose: a rehash resetting it costs at
# most one early request, and it is not a lock (see runtime.py's rule).
_seen_absent = set()
_back_since = {}

# PAUSED BOTS (#926 item 4). A bot we could not connect to this many times in a
# row is paused - its requests wait, "Paused", until the operator resumes it -
# the way AutoGet disabled a nick after three "unable to connect" failures: a
# bot behind a firewall that cannot accept our connection fails every file the
# same way, and asking on burns its slot and ours. The operator can pause and
# resume any bot too. Kept in a small file beside the fetch history, so a pause
# survives a restart; the consecutive-failure count does not need to.
CONNECT_FAILURES_TO_PAUSE = 3
_paused = {}               # bot (lowercased) -> {"nick", "reason", "since", "by"}
_connect_failures = {}     # bot (lowercased) -> consecutive active-connect failures

# A FULL DISK (#926 item 4). No new fetch starts while FETCHED_FILES_DIR has
# less than this free; a transfer that runs out of space mid-way goes back to
# pending rather than failing, and everything resumes by itself once space is
# freed. AutoGet switched itself off when a write failed; waiting is kinder.
MIN_FREE_BYTES = 200 * 1024 * 1024
_disk_was_low = [False]


def _paused_path():
    """Beside the fetch history - so wherever that is redirected (tests,
    FETCH_HISTORY_FILE), this goes with it."""
    return os.path.join(os.path.dirname(os.path.abspath(db.FETCH_HISTORY_FILE)),
                        "fetch_paused_bots.json")


def load_paused_bots():
    """Read the paused bots back at startup. A missing or unreadable file is
    no pauses, not a refusal to start."""
    import json
    _paused.clear()
    try:
        with open(_paused_path(), "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return
    if isinstance(loaded, dict):
        _paused.update({str(key).lower(): value for key, value in loaded.items()
                        if isinstance(value, dict)})


def _save_paused_bots():
    import json
    try:
        with db._disk_lock:
            db._atomic_write(_paused_path(), json.dumps(_paused, indent=1, sort_keys=True))
    except Exception as err:
        print(f"[FETCH] Could not save the paused bots: {err}")


def paused_bots():
    """{bot (lowercased): {"nick", "reason", "since", "by"}}, a copy."""
    return {key: dict(value) for key, value in _paused.items()}


def pause_bot(bot, reason, by="operator"):
    nick = str(bot or "").strip()
    if not nick:
        return False
    _paused[nick.lower()] = {"nick": nick, "reason": str(reason), "since": time.time(), "by": by}
    _connect_failures.pop(nick.lower(), None)
    _save_paused_bots()
    print(f"[FETCH] Paused fetching from {nick}: {reason}")
    return True


def resume_bot(bot):
    key = str(bot or "").strip().lower()
    if _paused.pop(key, None) is None:
        return False
    _connect_failures.pop(key, None)
    _save_paused_bots()
    print(f"[FETCH] Resumed fetching from {bot}.")
    return True


def _note_connect_failure(bot):
    """One more time we could not connect to this bot; the third in a row
    pauses it, and says so where the operator looks."""
    key = str(bot or "").strip().lower()
    if not key or key in _paused:
        return
    _connect_failures[key] = _connect_failures.get(key, 0) + 1
    if _connect_failures[key] >= CONNECT_FAILURES_TO_PAUSE:
        pause_bot(bot, f"could not connect {CONNECT_FAILURES_TO_PAUSE} times in a row", by="auto")
        try:
            import announce
            announce.send_debug(f"Fetching from {bot} is paused: could not connect "
                                f"{CONNECT_FAILURES_TO_PAUSE} times in a row. Resume it on the "
                                f"Downloads page when it can take connections.", category="INFO")
        except Exception:
            pass


def _note_connect_success(bot):
    _connect_failures.pop(str(bot or "").strip().lower(), None)


def _free_bytes():
    """What is free where fetched files go, or None when it cannot be measured."""
    import shutil
    folder = getattr(config, "FETCHED_FILES_DIR", "") or "."
    try:
        return shutil.disk_usage(platform_compat.long_path(os.path.abspath(folder))).free
    except OSError:
        return None


def _room_left_locked(queue, free, exclude=None):
    """What is free once the transfers already under way have written the rest
    of what they declared - the room a new offer is weighed against (#964).
    Two offers that each fit alone do not both fit together. None when the
    disk cannot be measured. Caller holds _fetch_lock().

    `exclude` is the request whose own offer is being weighed (#1039): the
    claim has already moved it to "receiving", and a file row asked again
    after a restart or a disk-full hold still carries the total_size of its
    last offer - counted here, it was weighed against itself, held, asked
    again and held again, for as long as the disk had less than twice its
    size free."""
    if free is None:
        return None
    for rid, row in queue.items():
        if rid == exclude:
            continue
        if row.get("state") in ("listening", "receiving"):
            free -= max(0, int(row.get("total_size") or 0) - int(row.get("bytes_received") or 0))
    return free


def _has_room(size, room):
    """Whether a file of `size` fits in `room` and still leaves MIN_FREE_BYTES.
    Nothing to fit, or a disk that cannot be measured, is not held back - the
    write itself still fails safely."""
    return not size or room is None or room >= size + MIN_FREE_BYTES


def _hold_for_space(row, size, reason):
    """Back to pending until `size` fits (#964), asked for as it was asked
    (#963). row["needs_bytes"] is what the dispatcher waits for: without it a
    file bigger than the free space - but with more than MIN_FREE_BYTES free,
    so the disk never counts as low - was asked for again on the next tick,
    filled the disk to nothing, failed, and was asked for again, for ever."""
    row.update(state="pending", offered_at=None, bytes_received=0,
               reason=reason, waiting="disk-full", needs_bytes=int(size))
    _as_asked(row)
    # Its place in the other bot's queue is over too (#1043), as every other
    # way back to pending says: kept, the old queued_at made the next "Added
    # ... at position" look hours old, and FETCH_QUEUED_TIMEOUT failed the
    # fresh place soon after.
    for stale in ("queued_at", "queue_position", "reply"):
        row.pop(stale, None)


def _mb(size):
    return f"{max(0, int(size)) // (1024 * 1024)} MB"


def _disk_is_low():
    """Whether FETCHED_FILES_DIR has less than MIN_FREE_BYTES free. Said once
    when it becomes low and once when it recovers. A disk that cannot be
    measured is not called low - the write itself still fails safely."""
    free = _free_bytes()
    if free is None:
        return False
    low = free < MIN_FREE_BYTES
    if low != _disk_was_low[0]:
        _disk_was_low[0] = low
        message = (f"Fetching is waiting: under {MIN_FREE_BYTES // (1024 * 1024)} MB free where fetched "
                   f"files go. It carries on by itself once space is freed."
                   if low else "Fetching carries on: there is space for fetched files again.")
        print(f"[FETCH] {message}")
        try:
            import announce
            announce.send_debug(message, category="INFO")
        except Exception:
            pass
    return low


def _is_disk_full(err):
    """Whether an error is the disk running out of space (ENOSPC; Windows
    reports ERROR_DISK_FULL as the same errno)."""
    import errno
    return isinstance(err, OSError) and (err.errno == errno.ENOSPC
                                         or getattr(err, "winerror", None) in (112, 39))

# MAX_UNRESOLVED_FETCHES: the ceiling on how many rows may sit unresolved
# (pending or in flight) at once, across every requester.
#
# MAX_FETCH_SLOTS already bounds the rows actually MOVING - count_active_fetches()
# counts offered/listening/receiving, and check_fetch_queue() promotes only into
# free slots. Nothing bounded the rows WAITING. A `pending` row costs no socket
# and no slot, so the dispatcher was content to let the backlog behind those three
# slots grow without limit, and one bulk enqueue could park thousands of rows that
# then drain at MSG_DELAY seconds apiece - hours of outbound IRC the operator did
# not ask for a second time.
#
# A module constant rather than a config setting, matching webserver.py's own
# WEBUI_MAX_SEARCH_RESULTS/FILELISTS_MAX_PAGE_SIZE: this is a backstop against a
# mistake, not a knob anyone tunes. An operator who genuinely wants a fourth
# thousand-file batch queued can delete rows or wait for the first three to drain,
# and MAX_FETCH_SLOTS is the setting that actually governs throughput.
#
# 1000 is deliberately far above any real batch - the dashboard's largest
# hand-driven multi-select is a page of checkboxes - and far below the point where
# the queue's own size is the problem.
MAX_UNRESOLVED_FETCHES = 1000


def count_unresolved_fetches(queue=None):
    """Rows that have not reached a terminal state: pending plus in flight.

    The companion to count_active_fetches(), and deliberately a WIDER count:
    that one answers "how many slots are busy" (offered/listening/receiving)
    for the dispatcher's promotion decision, this one answers "how much work
    is outstanding" for admission control. `pending` is the whole difference
    between them, and it is exactly the state that used to be unbounded.

    Derived on demand rather than tracked as a counter, same as
    count_active_fetches() - see the comment on config.fetch_queue for why a
    second source of truth for a number already implied by the rows is worse
    than recomputing it.
    """
    queue = _ensure_fetch_queue() if queue is None else queue
    return sum(1 for row in queue.values()
               if row.get("state") in _UNRESOLVED_FETCH_STATES)


def _has_outstanding_bot_alone_request_locked(queue, bot):
    """True if `queue` already has an unresolved "list" or "folder" row for
    `bot`. Caller must hold _fetch_lock(). See has_outstanding_bot_alone_
    request() below for why this check exists at all.

    Bot comparison is stripped/lower-cased, the exact normalisation
    _claim_matching_offer_locked() already uses for the same field - two
    rows that would later collide at claim time must also collide here.
    """
    wanted_bot = str(bot).strip().lower()
    return any(
        row.get("request_type") in ("list", "folder")
        and row.get("state") in _UNRESOLVED_FETCH_STATES
        and str(row.get("bot", "")).strip().lower() == wanted_bot
        for row in queue.values()
    )


def has_outstanding_bot_alone_request(bot):
    """True if a "list" or "folder" fetch is already outstanding for `bot`
    (any state other than "complete"/"failed").

    "list" and "folder" rows both use bot-alone admission control (see
    _claim_matching_offer_locked()'s docstring) because neither convention's
    response filename is knowable ahead of time. That means a DCC SEND
    offer arriving while both a "list" row and a "folder" row are
    outstanding for the SAME bot cannot be told apart on receipt - whichever
    branch _claim_matching_offer_locked() checks first would claim it, even
    if it actually answers the other request. Rather than try to guess
    right at claim time, the ambiguity is refused at its source: only ever
    let one bot-alone row be outstanding for a given bot, checked here
    BEFORE a second one is created (see enqueue_fetch() and webserver.py's
    build_list_fetch_enqueue_result()/build_folder_rar_fetch_enqueue_
    result(), which call this to turn the conflict into a clear 409 instead
    of a silently misattributed transfer).

    Public (no leading underscore) so it is directly unit-testable and so
    webserver.py can call it to build a friendly error message before ever
    calling enqueue_fetch() itself.
    """
    queue = _ensure_fetch_queue()
    with _fetch_lock():
        return _has_outstanding_bot_alone_request_locked(queue, bot)


def has_any_outstanding_request(bot):
    """True if ANY row - file, list or folder - is unresolved for `bot`.

    Wider than has_outstanding_bot_alone_request() above, which only looks at
    "list"/"folder" rows because those are the ones ambiguous at claim time.
    A caller asking "is it safe to forget this bot entirely" (the List
    Browser's purge, see webserver.build_purge_offline_fetched_lists_result())
    cares about every request_type: forgetting a bot mid-download would not
    misattribute anything the way two bot-alone rows would, but it would
    still delete the fetched-list entry a "list" reply is about to be filed
    under, or the extract directory a running "folder" fetch is about to
    write into.
    """
    wanted_bot = str(bot).strip().lower()
    queue = _ensure_fetch_queue()
    with _fetch_lock():
        return any(
            row.get("state") in _UNRESOLVED_FETCH_STATES
            and str(row.get("bot", "")).strip().lower() == wanted_bot
            for row in queue.values()
        )


def enqueue_fetch(bot, filename, request_type="file"):
    """Append one `pending` row to config.fetch_queue and return its id, or
    None if the request was refused (see below) - callers must check for
    None, they can no longer assume this always succeeds.

    Does NOT dispatch anything - check_fetch_queue() (the background
    dispatcher) is what promotes pending rows, so this is safe to call from
    a Flask request thread without blocking on IRC pacing.

    A "list" or "folder" request_type is refused (returns None, no row is
    created) if a "list" or "folder" row is already outstanding for the
    same bot - see has_outstanding_bot_alone_request()'s docstring for why.
    This check is enforced HERE, not only in webserver.py's two callers, so
    the invariant holds no matter what calls this function in the future
    (defense in depth, the same reasoning this feature's CTCP-safety check
    already applies by recurring at both webserver enqueue-time and
    dcc_fetch dispatch-time - see check_fetch_queue()'s own comment on
    that). webserver.py still calls has_outstanding_bot_alone_request()
    itself first, so it can return a clear 409 instead of just observing
    None come back from here.

    "file" rows are never affected by THAT check - they use exact bot+filename
    admission control and were never ambiguous (see _claim_matching_offer_locked()).

    Every request_type, "file" included, is refused once the queue already
    holds MAX_UNRESOLVED_FETCHES unresolved rows. Enforced here for the same
    defense-in-depth reason as the check above, and additionally because it is
    the only place it CAN be exact: the count and the insert have to happen
    under one hold of the lock or two callers race.
    """
    queue = _ensure_fetch_queue()
    normalized_type = request_type if request_type in ("file", "list", "folder") else "file"
    request_id = uuid.uuid4().hex[:12]
    with _fetch_lock():
        if normalized_type in ("list", "folder") and _has_outstanding_bot_alone_request_locked(queue, bot):
            return None
        # Checked under the same lock that does the insert, so the count cannot
        # go stale between deciding there is room and taking it - two request
        # threads enqueueing at once cannot both read 999 and both create.
        if count_unresolved_fetches(queue) >= MAX_UNRESOLVED_FETCHES:
            return None
        while request_id in queue:  # practically never, but be certain
            request_id = uuid.uuid4().hex[:12]
        queue[request_id] = new_fetch_row(bot, filename, request_type=request_type)
    return request_id


def count_active_fetches(queue=None):
    """Rows currently occupying a slot. Derived, not tracked separately - see
    the comment on config.fetch_queue for why.

    'listening' counts too: a passive/reverse DCC SEND has already claimed a
    listening socket in the shared DCC port range and a slot in the fetch
    queue by the time it reaches this state, exactly like 'receiving' - it
    must count against MAX_FETCH_SLOTS or an offering bot that never connects
    would let listeners pile up unbounded.
    """
    queue = _ensure_fetch_queue() if queue is None else queue
    return sum(1 for row in queue.values()
               if row.get("state") in ("offered", "listening", "receiving"))


def _mark_failed_locked(row, reason):
    # One update(), not two statements - a caller reading row["state"] must
    # never observe "failed" with the old reason (or no reason) still on it.
    row.update(state="failed", reason=reason)


# The full content of every terminal row last written to FETCH_HISTORY_FILE,
# purely to skip a redundant write when nothing has changed - check_fetch_queue()
# calls _persist_fetch_history_locked() on every tick (every 2s, forever), and
# nothing else marks this dirty.
#
# #162 finding #9: this used to be just the SET of terminal ids. _run_transfer()
# sets row["state"] = "complete" and only THEN calls _handle_completed_list_fetch(),
# which is what sets row["list_processing_error"] when a fetched list zip is
# refused (zip-slip, zip-bomb, ...). A dispatcher tick landing between those two
# lines saw the id already in the terminal set - "nothing changed" - and skipped
# the write, so the annotation that says the archive is refused never reached
# disk. After a restart the row came back exactly as it looked at "state=complete"
# with no list_processing_error, and web/app.js derives "Rejected" solely from
# that field - so a refused hostile archive rendered as a clean, downloadable
# "Complete" fetch. Comparing full row CONTENT, not just which ids are present,
# closes that window: the annotation being added between two consecutive ticks is
# now itself a content change the dirty check sees.
_last_persisted_terminal_snapshot = {}


# Any requested_at below this is treated as absent rather than as a date. A
# fetch cannot predate the software, so a value down here is a default, a
# sentinel or corruption - and deleting a row because its timestamp is
# unreadable is the one direction this must not fail in. 1 Jan 2000.
_PLAUSIBLE_EPOCH = 946684800


def prune_fetch_history_locked(queue, now=None):
    """Forget terminal fetch rows that are too old, or too many. Returns the
    ids dropped. Must be called with the fetch lock already held.

    #221: complete/failed rows were never removed. MAX_UNRESOLVED_FETCHES only
    bounds pending and in-flight rows, and the only way to drop a finished one
    was the dashboard's delete button, one at a time. Meanwhile the whole
    history is reloaded into memory at every startup (oserve.py), returned in
    full by /api/fetch/status with no pagination - which web/app.js polls every
    4 seconds regardless of the active tab - and scanned linearly for every
    inbound DCC SEND. All three get slower for the life of the process.

    AGE FIRST, COUNT AS A BACKSTOP. An age cap matches what the Downloads table
    is for: a recent record of what happened, not an archive. A count cap alone
    discards arbitrarily - whichever rows happen to be oldest when the cap is
    hit, whether they are an hour old or a month. The count is kept as a
    backstop for a burst of activity inside the window, which age cannot bound.

    IN-FLIGHT ROWS ARE NEVER TOUCHED, at any age. A pending row that has sat
    for a month is a bug worth seeing, not history worth forgetting, and
    dropping it here would silently unbook work the dispatcher still owns.

    THE FILE ON DISK IS NOT DELETED. Only the daemon's memory of which fetch
    produced it. Removing somebody's downloaded album because a bookkeeping row
    expired would be a surprising thing for a retention setting to do; the file
    stays under FETCHED_FILES_DIR where the operator can see and remove it. The
    trade is that a pruned row's file is no longer deletable from the dashboard,
    which is the lesser of the two.
    """
    import time as _time

    now = _time.time() if now is None else now
    days = float(getattr(config, "FETCH_HISTORY_DAYS", 30) or 0)
    max_rows = int(getattr(config, "FETCH_HISTORY_MAX_ROWS", 500) or 0)

    terminal = [(rid, row) for rid, row in queue.items()
                if row.get("state") in ("complete", "failed")]
    dropped = []

    if days > 0:
        cutoff = now - (days * 86400)
        for rid, row in terminal:
            # A row with no USABLE timestamp is kept: it predates
            # requested_at, and treating "unknown" as "infinitely old" would
            # delete exactly the rows whose age cannot be established.
            #
            # Zero counts as unusable, not as 1970. Rows built before this
            # field existed - and every fixture that omits it - carry 0, and
            # the first version of this dropped all of them on sight.
            requested_at = row.get("requested_at")
            usable = (isinstance(requested_at, (int, float))
                      and requested_at >= _PLAUSIBLE_EPOCH)
            if usable and requested_at < cutoff:
                dropped.append(rid)

    if max_rows > 0:
        survivors = [(rid, row) for rid, row in terminal if rid not in dropped]
        excess = len(survivors) - max_rows
        if excess > 0:
            # Oldest first, and rows with no timestamp sort oldest - here that
            # is right: the count cap has to drop SOMETHING, and an undateable
            # row is the least useful thing to keep.
            survivors.sort(key=lambda pair: pair[1].get("requested_at") or 0)
            dropped.extend(rid for rid, _row in survivors[:excess])

    for rid in dropped:
        queue.pop(rid, None)
    if dropped:
        print(f"[FETCH-HISTORY] Pruned {len(dropped)} finished fetch row(s); "
              f"the files themselves are untouched.")
    return dropped


def prune_fetch_history():
    """prune_fetch_history_locked() for a caller that does not hold the lock -
    oserve.py's startup, which loads the history back from disk and would
    otherwise carry however much of it accumulated before this existed."""
    with _fetch_lock():
        dropped = prune_fetch_history_locked(config.fetch_queue)
    if dropped:
        persist_fetch_history()
    return dropped

def _persist_fetch_history_locked(queue):
    """Snapshot every 'complete'/'failed' row and write it to disk, if the
    CONTENT of that snapshot has changed since the last one written. Must be
    called with the fetch lock already held - queue is read directly, not
    copied under a lock of its own.

    Without this, config.fetch_queue was in-memory only: a finished fetch's
    row (the only thing the dashboard's Downloads table and its Delete
    button have to point at) vanished on every restart even though the file
    itself sat untouched on disk under FETCHED_FILES_DIR the whole time -
    same shape as the bug config.fetched_bot_lists persistence already
    fixed for a fetched LIST's registry entry, applied here to an
    individual fetch's own row.

    Every row, since #926 - see the comment in the body: an unfinished
    request survives a restart, and one that was mid-transfer is asked again.
    """
    global _last_persisted_terminal_snapshot
    # #221: on the same tick that already holds the lock and already walks the
    # dict, so retention costs one comparison per row and no new machinery.
    prune_fetch_history_locked(queue)
    # THE UNFINISHED ROWS TOO (#926), in the form they take after a restart:
    # a request still waiting here is kept, and one that was mid-flight -
    # offered, listening, receiving; its socket and thread die with the
    # process - or queued at another bot (#978) is written as pending, to be
    # asked again. Written
    # in that form rather than as-is so a transfer's bytes_received ticking
    # up does not rewrite the file every two seconds.
    snapshot = {rid: _restart_form(row) for rid, row in queue.items()}
    if snapshot == _last_persisted_terminal_snapshot:
        return
    _last_persisted_terminal_snapshot = snapshot
    db.save_fetch_history(snapshot)


# QUEUED TOO (#978). A restart QUITs, and a file server drops the queue of a
# user who quits - so a row kept as "queued" waited for a file that was never
# coming, and counted toward FETCH_MAX_PER_BOT while it did: every other
# request to that bot waited "their-turn" until FETCH_QUEUED_TIMEOUT (12 h)
# failed it. Asked again, a server that did keep it says so ("already in my
# queue"), and handle_bot_reply() puts the row straight back to queued.
_ASKED_AGAIN_AFTER_A_RESTART = ("offered", "queued", "listening", "receiving")


def _as_asked(row):
    """Put back what a claimed offer overwrote, for a row about to be asked
    again (#963). A "folder" or "list" row does not know the name the other
    bot will give its file until the offer arrives, and
    _claim_matching_offer_locked() then writes that name over row["filename"]
    - "Artist_-_Album.rar" in place of "!rar Artist - Album". Asked again
    with it, the other bot is sent "!Bot Artist_-_Album.rar", a request for a
    file of that name, not the folder pack or the list: it answers "not
    found" or nothing. requested_filename kept the original all along.
    What the offer said about the file (its size, where it was going) is
    dropped with it: the next offer says it again. A "file" row asked for its
    own name, so there is nothing to put back."""
    if row.get("request_type") in ("folder", "list"):
        row["filename"] = row.get("requested_filename") or ""
        row["total_size"] = None
        row["stored_filename"] = None


def _restart_form(row):
    """A row as it should come back after a restart (#926)."""
    row = dict(row)
    if row.get("state") in _ASKED_AGAIN_AFTER_A_RESTART:
        row.update(state="pending", offered_at=None, bytes_received=0)
        _as_asked(row)
        # Where it stood in the other bot's queue is theirs to say again.
        for volatile in ("listening_since", "queued_at", "queue_position", "reply",
                         "receiving_since", "request_line"):
            row.pop(volatile, None)
    return row


def persist_fetch_history():
    """Same as _persist_fetch_history_locked(), for a caller that does not
    already hold the fetch lock - webserver.py's delete route, most notably:
    it needs a just-deleted row gone from disk immediately, not up to 2s
    later on check_fetch_queue()'s own polling tick, or a crash in that
    window would bring the deleted row back on the next boot even though
    its file is already gone.
    """
    queue = _ensure_fetch_queue()
    with _fetch_lock():
        _persist_fetch_history_locked(queue)


# Substrings (lowercased) a private NOTICE must ALL contain before it is
# treated as a "!rar is disabled here" refusal rather than routine chatter -
# see handle_refusal_notice()'s own docstring for why both are required
# together. Covers this bot's own wording (dcc.py's handle_download_request()
# -> announce.send_dcc_error(user, "rar_disabled"): "Error: Folder packing
# (!rar) is disabled on this bot.") and the OmenServe-family wording
# ("Rar Server is currently disabled.") - both contain "rar" and "disabled".
_RAR_REFUSAL_MARKERS = ("disabled", "rar")


def handle_refusal_notice(bot, notice_text):
    """Called from irc.py's NOTICE handler for any private NOTICE addressed
    to us, from any other bot. Turns a peer's own "!rar is disabled here"
    reply into an immediate failure for the matching "folder" row, instead
    of waiting out the full FETCH_FOLDER_OFFER_TIMEOUT (1800s, see
    config.py) against one of only MAX_FETCH_SLOTS fetch slots for a request
    the peer already refused in its first second - "refused immediately"
    and "still packing a 40-minute discography" otherwise look identical to
    us, both sitting "offered" for the same half hour.

    Matches on bot alone, exactly like _claim_matching_offer_locked()'s own
    "folder" branch - the whole point of this hook is that a refused
    request never gets a DCC SEND, and therefore never gets a filename to
    match on either. Deliberately narrow in two ways: only "folder" rows
    still "offered" are eligible (a "list" refusal, if that ever happens,
    is not this wording and is left to its own timeout), and the notice
    text must contain every marker in _RAR_REFUSAL_MARKERS - a false match
    here would fail a row a moment before its real DCC SEND arrived, with
    no way back for that request.
    """
    text_lower = str(notice_text).lower()
    if not all(marker in text_lower for marker in _RAR_REFUSAL_MARKERS):
        return False
    wanted_bot = str(bot).strip().lower()
    queue = _ensure_fetch_queue()
    with _fetch_lock():
        candidates = [
            row for row in queue.values()
            if row.get("state") == "offered"
            and row.get("request_type") == "folder"
            and str(row.get("bot", "")).strip().lower() == wanted_bot
        ]
        if not candidates:
            return False
        # Oldest wins, same defence-in-depth tie-break
        # _claim_matching_offer_locked() uses - unreachable in the normal
        # case (enqueue_fetch() already refuses a second outstanding
        # "folder"/"list" request for the same bot), kept for the same
        # reason that guard's own tie-break is kept: not assumed impossible.
        row = min(candidates, key=lambda r: r.get("requested_at", 0))
        _mark_failed_locked(row, f"refused: {notice_text}".strip())
    print(f"[FETCH] {bot} refused a folder-rar request: {notice_text}")
    return True


def _match_name(row):
    return _normalize_filename_for_match(row.get("requested_filename") or row.get("filename") or "")


def _row_named_in(row, text):
    """Whether a reply names this row's file. Compared the way offers are,
    spaces and underscores alike, so "Some_Track.mp3" in a reply finds the row
    that asked for "Some Track.mp3"."""
    name = _match_name(row)
    return bool(name) and name in _normalize_filename_for_match(text)


def _the_longest_named(rows):
    """Of the rows a reply names, the ones it is about (#974): a name that is
    only part of a longer one it also carries does not count. "Sorry, but
    Band - Intro.mp3 is not found" contains "Intro.mp3" too, and the older
    request for that - named first - was failed in its place, while the one
    it was about waited out its timeout. No word boundary could tell them
    apart: "Intro.mp3" follows a space there."""
    names = [_match_name(row) for row in rows]
    return [row for row, name in zip(rows, names)
            if not any(name != other and name in other for other in names)]


def handle_bot_reply(bot, text):
    """Act on what another bot says about a request we sent it (#926).

    Called from irc.py for every private NOTICE, and every private message
    that is not a CTCP, addressed to us. fetch_replies.classify() says what
    the line means; this finds the request it is about and moves it:

      queued / duplicate  -> "queued", with the queue position when given.
                            The row stops holding a fetch slot and waits up to
                            FETCH_QUEUED_TIMEOUT for the DCC SEND.
      refused             -> failed at once, with their words as the reason.
      busy                -> failed at once, "busy: ..." - a request that will
                            not come now, instead of a minute's "no response".

    WHICH REQUEST. Only rows sent to THIS bot and still waiting for it
    (offered or queued) are candidates - nobody else's reply can touch them.
    A reply that names a file acts on that file's row. One that names none
    acts when there is exactly one candidate; with several, a queued or
    duplicate reply goes to the oldest still "offered" (servers answer in the
    order asked, and the worst a wrong pick does is wait longer), but a
    refusal or a busy reply is left alone - failing the wrong request has no
    way back, and the timeout still ends the right one.

    Returns the outcome acted on, or None.
    """
    if handle_refusal_notice(bot, text):
        return "refused"
    # Nothing waiting on this sender, nothing to read (audit of 2026-09-27):
    # every private line from anybody reaches here on the IRC read loop, and
    # classifying a stranger's text is work for nobody. Checked again under
    # the lock below, since the answer can change in between.
    wanted_bot = str(bot).strip().lower()
    with _fetch_lock():
        waiting = any(row.get("state") in _AWAITING_OFFER_STATES
                      and str(row.get("bot", "")).strip().lower() == wanted_bot
                      for row in _ensure_fetch_queue().values())
    if not waiting:
        return None
    import fetch_replies
    reply = fetch_replies.classify(text)
    if reply is None:
        return None
    wanted_bot = str(bot).strip().lower()
    queue = _ensure_fetch_queue()
    now = time.time()
    with _fetch_lock():
        candidates = sorted(
            (row for row in queue.values()
             if row.get("state") in _AWAITING_OFFER_STATES
             and str(row.get("bot", "")).strip().lower() == wanted_bot),
            key=lambda r: r.get("requested_at", 0))
        if not candidates:
            return None
        named = _the_longest_named([row for row in candidates if _row_named_in(row, reply.text)])
        if len({_match_name(row) for row in named}) > 1:
            # Two different names, neither part of the other: which one it
            # is about is a guess, and the same rule as an unnamed reply to
            # several requests applies - among these.
            candidates, named = named, []
        if named:
            row = named[0]
        elif len(candidates) == 1:
            row = candidates[0]
        elif reply.outcome in ("queued", "duplicate"):
            offered = [r for r in candidates if r.get("state") == "offered"]
            if not offered:
                return None
            row = offered[0]
        else:
            return None

        if reply.outcome in ("queued", "duplicate"):
            row["state"] = "queued"
            if row.get("queued_at") is None:
                row["queued_at"] = now
            if reply.position is not None:
                row["queue_position"] = reply.position
            row["reply"] = reply.text
        elif reply.outcome == "refused":
            _mark_failed_locked(row, f"refused: {reply.text}")
        elif int(row.get("busy_retries", 0)) < BUSY_RETRIES:
            # Asked again later (#926): back to pending with a time, so the
            # dispatcher leaves it until then and it keeps its place.
            row["busy_retries"] = int(row.get("busy_retries", 0)) + 1
            _as_asked(row)
            row.update(state="pending", offered_at=None, retry_at=now + BUSY_RETRY_SECONDS,
                       reason=f"busy: {reply.text}", waiting="retry")
            row.pop("queued_at", None)
            row.pop("queue_position", None)
        else:
            _mark_failed_locked(row, f"busy: {reply.text} (asked {BUSY_RETRIES + 1} times)")
        described = (f"position {row.get('queue_position')}"
                     if row.get("state") == "queued" and row.get("queue_position") else row.get("state"))
    print(f"[FETCH] {bot} answered our request for {row.get('requested_filename') or row.get('request_type')}: "
          f"{reply.outcome} ({described}).")
    return reply.outcome


def bot_is_known(bot):
    """Whether this nick is a file server we know: one we have seen advertise
    (runtime.known_bots) or whose list we hold. A request for such a bot can
    wait for it while it is away (#926); a nick we have never seen is still
    refused, since it is far more likely a typo than a server."""
    key = str(bot or "").strip().lower()
    if not key:
        return False
    if key in (getattr(runtime, "known_bots", None) or {}):
        return True
    return key in (getattr(config, "fetched_bot_lists", None) or {})


def _bot_readiness(bots, now):
    """{bot (lowercased): "" when we may ask it now, else why not ("joining",
    "offline", "just-back")}, for the bots with rows waiting. Read OUTSIDE the
    fetch lock: presence has its own lock, and holding both is an ordering to
    get wrong.

    NOBODY IS ASKED BEFORE THE CHANNELS ARE JOINED (#965). Until
    config.bot_joined_channel - at startup and after every reconnect, which
    empties channel_users - nothing can tell a bot that is gone from one not
    heard from yet. Asking anyway used to be harmless: a request made then
    was one just clicked. Since rows persist across a restart and wait for an
    offline bot (#926), it promoted every restored or waiting row at once,
    sent each to the first configured channel through a queue that holds it
    until the join, and FETCH_OFFER_TIMEOUT failed them as "no response" -
    on every reconnect. list_fetch.refetch_due_lists() waits for the same
    flag, for the same reason."""
    import dcc
    joined = bool(getattr(config, "bot_joined_channel", False))
    if joined:
        with runtime.channel_users_lock():
            joined = any(users for users in (getattr(config, "channel_users", {}) or {}).values())
    ready = {}
    for bot in bots:
        if not joined:
            ready[bot] = "joining"
            continue
        if not dcc.user_is_present_in_ram(bot):
            _seen_absent.add(bot)
            _back_since.pop(bot, None)
            ready[bot] = "offline"
            continue
        if bot in _seen_absent:
            _seen_absent.discard(bot)
            _back_since[bot] = now
        since = _back_since.get(bot)
        if since is not None and now - since < RETURN_DELAY_SECONDS:
            ready[bot] = "just-back"
        else:
            _back_since.pop(bot, None)
            ready[bot] = ""
    return ready


def check_fetch_queue():
    """Dispatcher: expire stale offers, then promote pending rows while a slot
    is free. Mirrors dcc.py's check_queue_and_send() claim-before-dispatch
    discipline (dcc.py has its own comments about the project having been
    bitten by queue races before) - the row is flipped to `offered` INSIDE
    the lock, before the outbound message is even built, so two overlapping
    calls to this function can never both claim the same pending row.

    Called periodically from fetch_dispatcher_worker() (a small dedicated
    thread started by oserve.startup(), mirroring how queue_mgr.queue_worker
    is started) rather than being wired into an unrelated existing loop.
    """
    # Absent means DISABLED: see webserver.fetch_feature_error() for why
    # the missing attribute has to fail toward not-fetching.
    if getattr(config, "fetch_feature_disabled", True):
        # FETCHED_FILES_DIR could not be created at startup (see
        # oserve.startup()) - leave rows sitting `pending` rather than ever
        # promoting them; there is nowhere safe to write a completed file.
        return

    # The rehash quiesce applies here too. wait_for_transfers_to_finish()
    # polls config.active_transfers, which is the SEND side only - a fetch has
    # its own queue and never appears there. So without this check the wait
    # would report a quiet bot while this dispatcher was still putting fresh
    # `@bot` and `!bot file` requests into the channel, each of which brings
    # back an inbound DCC SEND landing squarely in the reload window.
    #
    # Read through dcc rather than duplicating the flag name: dcc owns the
    # pause, and a second reader spelling `config.transfers_paused` by hand is
    # how a rename turns one of them into a no-op silently.
    import dcc as _dcc_pause
    if _dcc_pause.transfers_are_paused():
        return

    queue = _ensure_fetch_queue()
    max_slots = int(getattr(config, "MAX_FETCH_SLOTS", 3))
    max_per_bot = int(getattr(config, "FETCH_MAX_PER_BOT", 3) or 0)
    offer_timeout = float(getattr(config, "FETCH_OFFER_TIMEOUT", 60))
    folder_offer_timeout = float(getattr(config, "FETCH_FOLDER_OFFER_TIMEOUT", 1800))
    unadvertised_folder_timeout = float(
        getattr(config, "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED", 120))
    now = time.time()

    with _fetch_lock():
        waiting_bots = {str(row.get("bot", "")).strip().lower()
                        for row in queue.values() if row.get("state") == "pending"}
        held_for_space = any(row.get("needs_bytes") for row in queue.values()
                             if row.get("state") == "pending")
    # Measured only while a row waits for room of its own (#964).
    free = _free_bytes() if held_for_space else None
    readiness = _bot_readiness(waiting_bots, now)
    for bot in waiting_bots:
        if bot in _paused:
            readiness[bot] = "paused"
    disk_low = bool(waiting_bots) and _disk_is_low()

    to_dispatch = []
    to_take_back = []
    with _fetch_lock():
        # Expire offers nobody ever answered. A row stuck in "offered" forever
        # would otherwise hold a slot open permanently and starve every other
        # pending request behind it. A "folder" row gets its own, much longer
        # timeout (folder_offer_timeout) - the other bot has to run its own
        # !rar packing pipeline before it can even start the DCC SEND, which
        # plain file/list fetches never have to wait on.
        for row in queue.values():
            if row.get("state") == "offered" and row.get("offered_at") is not None:
                # The clock runs from the moment the request leaves us, not
                # from when it was queued to go (#1028).
                if _request_is_unsent(row):
                    row["offered_at"] = now
                    continue
                this_timeout = _offer_timeout_for(
                    row, offer_timeout, folder_offer_timeout,
                    unadvertised_folder_timeout)
                if (now - row["offered_at"]) > this_timeout:
                    # A busy bot answers minutes late, so a file is asked for
                    # OFFER_ASKS times before it fails, and only then is the
                    # request taken back from the bot. The row stays, failed,
                    # for the operator to see and ask again.
                    if (row.get("request_type", "file") == "file"
                            and int(row.get("silent_asks", 1)) < OFFER_ASKS):
                        row["silent_asks"] = int(row.get("silent_asks", 1)) + 1
                        row.update(state="pending", offered_at=None,
                                   reason=f"no response - asking again (attempt {row['silent_asks']} of {OFFER_ASKS})")
                    else:
                        _mark_failed_locked(row, "no response")
                        if row.get("request_type", "file") == "file":
                            to_take_back.append((row.get("bot"), row.get("requested_filename") or row.get("filename")))

        # Independent safety net for "listening" rows (passive DCC SEND).
        # _serve_passive_offer() already bounds its own accept() with
        # PASSIVE_LISTEN_TIMEOUT and marks the row 'failed' on the way out no
        # matter how it exits (timeout, OSError, or any other exception - see
        # its own comment on that last case). This second check exists for
        # the failure mode none of that can cover: the daemon thread running
        # _serve_passive_offer() dies or hangs BEFORE it gets that far (e.g.
        # thread-start failure), leaving the row 'listening' with nothing left
        # to ever revisit it. A generous multiple of PASSIVE_LISTEN_TIMEOUT
        # avoids racing a passive transfer that is still legitimately waiting.
        # A row the other bot queued waits for its turn there (#926), which
        # on a busy server is hours - but not for ever: a bot that restarted,
        # dropped its queue or forgot us never says so.
        queued_timeout = float(getattr(config, "FETCH_QUEUED_TIMEOUT", 43200) or 0)
        if queued_timeout > 0:
            for row in queue.values():
                if row.get("state") == "queued" and row.get("queued_at") is not None:
                    if (now - row["queued_at"]) > queued_timeout:
                        _mark_failed_locked(
                            row, f"still queued at {row.get('bot')} after "
                                 f"{int(queued_timeout // 3600)} h - nothing arrived")

        listen_timeout = PASSIVE_LISTEN_TIMEOUT * 3
        for row in queue.values():
            if row.get("state") == "listening" and row.get("listening_since") is not None:
                if (now - row["listening_since"]) > listen_timeout:
                    _mark_failed_locked(row, "listening row expired without a resolution")

        # After applying both expiry sweeps above (so a row that just timed
        # out this very tick is captured too) and before the free-slots
        # check below, which can return early - a completed transfer or any
        # other failure reached from outside this function (_run_transfer(),
        # handle_incoming_offer()) also lands here on the very next tick,
        # since both write into this same queue.
        for row in queue.values():
            if row.get("request_line") and row.get("state") != "offered":
                take_back_unsent_request(row)

        _persist_fetch_history_locked(queue)

        active = count_active_fetches(queue)
        free_slots = max(0, max_slots - active)

        pending_ids = sorted(
            (rid for rid, row in queue.items() if row.get("state") == "pending"),
            key=lambda rid: queue[rid].get("requested_at", 0),
        )
        # WHO MAY BE ASKED NOW (#926), oldest first. A row waits - saying why,
        # in row["waiting"], for the Downloads panel - while its bot is away
        # or just back, while that bot already has FETCH_MAX_PER_BOT of ours,
        # or until a busy bot's retry time. The oldest ready rows take the
        # free slots; everything else keeps its place for the next tick.
        load = {}
        for row in queue.values():
            if row.get("state") in _BOT_LOAD_STATES:
                key = str(row.get("bot", "")).strip().lower()
                load[key] = load.get(key, 0) + 1
        promoted = 0
        room = _room_left_locked(queue, free)
        for rid in pending_ids:
            row = queue[rid]
            key = str(row.get("bot", "")).strip().lower()
            # ONLY WHAT THIS TICK LOOKED AT (#970). Presence, pauses and the
            # disk were read for the bots with rows pending at the snapshot
            # above, outside the lock; a row enqueued since is not in it, and
            # taking it as ready sent it to a bot just paused, or onto a
            # nearly full disk when nothing else had been pending. It is
            # looked at properly on the next tick, a couple of seconds away.
            if key not in readiness:
                continue
            why = readiness[key] or ("paused" if key in _paused else "")
            if not why and disk_low:
                why = "disk-full"
            # A file already known not to fit waits until it does (#964),
            # weighed exactly as handle_incoming_offer() weighs the offer -
            # asked for sooner, its offer would only be held again.
            if not why and not _has_room(row.get("needs_bytes"), room):
                why = "disk-full"
            if not why and (row.get("retry_at") or 0) > now:
                why = "retry"
            if not why and max_per_bot > 0 and load.get(key, 0) >= max_per_bot:
                why = "their-turn"
            if not why and promoted >= free_slots:
                why = "slots"
            if why:
                row["waiting"] = why
                continue
            row.pop("waiting", None)
            needed = row.pop("needs_bytes", None)
            if needed and room is not None:
                # Spoken for until its offer arrives, so a second held row
                # is not let go on the same room in this tick.
                room -= int(needed)
            row["state"] = "offered"
            row["offered_at"] = now
            load[key] = load.get(key, 0) + 1
            promoted += 1
            to_dispatch.append((rid, row["bot"], row["filename"], row.get("request_type", "file")))

    for bot, filename in to_take_back:
        drop_our_request_at(bot, filename)

    if not to_dispatch:
        return

    oserve = sys.modules.get("oserve")
    # ONE FIXED FALLBACK, still - but no longer the only answer. Reported
    # live: a bot only in one of several configured channels had its fetch
    # dispatched into a different one, because every request used to go
    # into this single channel regardless of where the target bot actually
    # was - so it never saw the request, and every one of them failed with
    # "no response". webserver.bot_not_here_error() already checks presence
    # at enqueue time; this is that same check carried through to where the
    # PRIVMSG is actually built, per request rather than once for the whole
    # batch.
    default_channel = (getattr(config, "BROADCAST_SEARCH_CHANNEL", None)
                       or str(getattr(config, "CHANNEL", "")).split(",")[0].strip())
    for rid, bot, filename, request_type in to_dispatch:
        # The bot's own channel wins when we can find one - a stale fallback
        # is exactly the bug above. Only a bot that left between enqueue and
        # this dispatch tick (bot_not_here_error() already refused any that
        # were never seen at all) falls through to the fixed default, which
        # is no worse than what every request did before this fix.
        channel = dcc.channel_containing_user(bot) or default_channel
        # Defense-in-depth only, expected to be unreachable: `bot` (and, for
        # a "file" row, `filename`) already passed
        # webserver.reject_if_unsafe_for_irc_line() - which now delegates to
        # this exact same contains_unsafe_ctcp_bytes() check - at enqueue
        # time (see build_fetch_enqueue_result()/build_list_fetch_enqueue_
        # result() in webserver.py). This is one of several places this
        # exact injection class has recurred in this feature (see
        # contains_unsafe_ctcp_bytes()'s own comment above), so this
        # dispatch site - the one that actually interpolates these values
        # into a raw outbound IRC line - does not simply trust that the
        # enqueue-time check was applied; it refuses to build or send the
        # message at all if either value is still unsafe for some future
        # reason, mirroring _serve_passive_offer()'s identical re-check
        # right before IT builds its own outbound CTCP line.
        if contains_unsafe_ctcp_bytes(bot) or (
                request_type != "list" and contains_unsafe_ctcp_bytes(filename)):
            _mark_failed_locked(queue[rid], "unsafe characters in bot/filename this late")
            print(f"[FETCH] Refusing to dispatch request {rid} "
                  f"(bot={bot!r}, filename={filename!r}): unsafe characters "
                  f"this late (should be unreachable - see "
                  f"webserver.reject_if_unsafe_for_irc_line()).")
            continue
        if request_type == "list":
            # The exact same bare "@<bot>" trigger irc.py answers on this
            # bot's own nick (irc.py's `elif msg_lower == f"@{config.NICKNAME.lower()}":`
            # branch, dispatching to list.send_file_list()) - other
            # OmenServe-family bots on the network answer the same convention
            # the same way: a DCC SEND of their own list zip, filename
            # unknown to us ahead of time (see _claim_matching_offer_locked()
            # for how admission control handles that).
            message = f"PRIVMSG {channel} :@{bot}\r\n"
            log_desc = f"{bot}'s file list"
        elif request_type == "folder":
            # filename is already the literal string "!rar <folder path>" at
            # this point (see webserver.build_folder_rar_fetch_enqueue_result()),
            # so it falls into the same wire line the plain "file" branch below
            # builds - only the log line differs, purely cosmetic.
            #
            # webserver.reject_if_unsafe_for_irc_line() already caps filename's
            # length at enqueue time (IRC_LINE_FIELD_MAX_LEN); fit_irc_line()
            # here is belt-and-braces against the real wire budget, same
            # posture as the contains_unsafe_ctcp_bytes() re-check just above
            # (#162 finding #13).
            import announce
            message = announce.fit_irc_line(lambda v: f"PRIVMSG {channel} :!{bot} {v}\r\n", filename)
            log_desc = f"{filename!r} (folder pack) from {bot}"
        else:
            import announce
            message = announce.fit_irc_line(lambda v: f"PRIVMSG {channel} :!{bot} {v}\r\n", filename)
            log_desc = f"{filename!r} from {bot}"
        if oserve and hasattr(oserve, "queue_message"):
            # A request still waiting to go out is replaced, never added to
            # (#1028): asking again used to stack a second identical line
            # behind the first, and the bot then sent the file twice.
            #
            # Under the fetch lock, and only for a row that is still waiting
            # to be answered: the tick released it between promoting the row
            # and here, so a Delete or a finished transfer in that gap would
            # otherwise leave a line nobody takes back. Two rows for the same
            # file share one line; the later request replaces the earlier.
            with _fetch_lock():
                row = queue.get(rid)
                if row is None or row.get("state") != "offered":
                    print(f"[FETCH] Not requesting {log_desc}: request {rid} is no longer waiting.")
                    continue
                _take_back_unsent_line(message)
                row["request_line"] = message
                # Its own lane, sent ahead of everything: in the ordinary one a
                # request waited among every other user's replies, in the express
                # one behind the advert, and either way for minutes.
                oserve.queue_message(bot, message, is_vip=getattr(oserve, "FETCH_LANE", True))
        print(f"[FETCH] Requested {log_desc} (request {rid}).")


def _take_back_unsent_line(line):
    """Remove `line` from the outgoing express queue if it is still there.
    Returns whether it was."""
    try:
        config.fetch_request_queue.remove(line)
    except ValueError:
        return False
    return True


def take_back_unsent_request(row):
    """A row that is done with, or gone, must not have its request go out
    after it (#1028): the other bot would send a file nobody waits for.
    Returns whether a line was still waiting and was removed."""
    line = row.pop("request_line", None)
    return bool(line) and _take_back_unsent_line(line)


def _request_is_unsent(row):
    line = row.get("request_line")
    return bool(line) and line in config.fetch_request_queue


def folder_asked_for(row):
    """The folder a "folder" row asked to have packed: its request without the
    "!rar " the dispatcher puts in front (build_folder_rar_fetch_enqueue_result()
    stores it that way). What asking again by the folder route needs (#1040)."""
    asked = str(row.get("requested_filename") or "")
    return asked[5:].strip() if asked.lower().startswith("!rar ") else asked.strip()


def requests_not_sent(lines):
    """Request lines dropped before they went out (#1044): the rows that own
    them go back to pending, as asked, to be asked again - left "offered",
    they read the missing line as sent and timed out as "no response".
    Returns how many rows went back."""
    lines = set(lines or ())
    if not lines:
        return 0
    back = 0
    with _fetch_lock():
        for row in _ensure_fetch_queue().values():
            if row.get("state") == "offered" and row.get("request_line") in lines:
                row.pop("request_line", None)
                row.update(state="pending", offered_at=None)
                _as_asked(row)
                back += 1
    return back


def drop_our_request_at(bot, filename):
    """Ask `bot` to take `filename` out of our queue there: `@<bot>-remove
    <file>` in the channel, the per-file form of the command DCCore answers
    (commands.handle_queue_remove_file). Without it, cancelling on the
    dashboard forgot the row here while the other bot kept the file queued and
    sent it later, refused as unsolicited. Only a bot known to be DCCore is
    told: another server may match its trigger as `@<bot>-remove*`, where the
    per-file form is the bare one and clears everything we have queued there.
    Returns whether it was sent."""
    import announce
    import dcc
    import serverschat
    bot = str(bot or "").strip()
    filename = str(filename or "").strip()
    if (not bot or not filename or contains_unsafe_ctcp_bytes(bot)
            or contains_unsafe_ctcp_bytes(filename)):
        return False
    if not serverschat.is_known_peer(bot):
        return False
    oserve = sys.modules.get("oserve")
    if not (oserve and hasattr(oserve, "queue_message")):
        return False
    default_channel = (getattr(config, "BROADCAST_SEARCH_CHANNEL", None)
                       or str(getattr(config, "CHANNEL", "")).split(",")[0].strip())
    channel = dcc.channel_containing_user(bot) or default_channel
    if not channel:
        return False
    oserve.queue_message(bot, announce.fit_irc_line(
        lambda v: f"PRIVMSG {channel} :@{bot}-remove {v}\r\n", filename))
    print(f"[FETCH] Cancelled by hand: asked {bot} to remove {filename!r} from our queue there.")
    return True


# THE FETCH FEED (#1019). A file leeched from another bot used to reach the
# log and the Downloads page and nothing else: the @DCCore window told the
# operator about every send, request and search, and said nothing about what
# the bot itself took. The rows already record every step, so this watches
# them change instead of adding a call beside each of the dozen places that
# move one - a refusal, an expiry, a refused list zip and a finished
# transfer all end up as a state on a row.
#
# What was last told, by request id. The first pass only records: rows read
# back from FETCH_HISTORY_FILE at startup are old news, and telling all of
# them as "done" would fill the window every restart.
_fetch_feed_told = {}
_fetch_feed_seeded = [False]

_FETCH_FEED_STATES = ("offered", "queued", "receiving", "complete", "failed")


def _fetch_feed_name(row):
    name = row.get("filename") or row.get("requested_filename") or ""
    if str(name).startswith("!rar "):
        name = str(name)[5:]
    return str(name)


def _fetch_feed_event(row):
    """(action, prose) for the state a row is in, or None when it is one the
    feed does not tell (a wait, a list - lists have their own LISTFETCH)."""
    import announce
    state = row.get("state")
    bot = row.get("bot") or "?"
    name = _fetch_feed_name(row)
    if state == "offered":
        return "asked", f'Asked {bot} for "{name}"'
    if state == "queued":
        position = row.get("queue_position")
        where = f" at #{position}" if position else ""
        return "queued", f'{bot} put "{name}" in its queue{where}'
    if state == "receiving":
        size = row.get("total_size")
        sized = f" ({announce.format_size_human(size)})" if size else ""
        return "receiving", f'Receiving "{name}" from {bot}{sized}'
    if state == "complete":
        if row.get("list_processing_error"):
            return None
        got = announce.format_size_human(row.get("bytes_received") or 0)
        return "done", f'Fetched "{name}" from {bot} ({got})'
    if state == "failed":
        return "failed", f'Fetch failed: "{name}" from {bot} - {row.get("reason") or "no reason given"}'
    return None


def _stamp_finished(request_ids):
    """Note when a row was seen to finish (#1022), for the mIRC Downloads
    window's Finished list, which shows a time. Good to the dispatcher's two
    seconds; a row from before this existed has none, and readers fall back
    to when it was asked for. Not stamped for the rows of the first pass: they
    are history loaded at startup, not something that just finished."""
    if not request_ids:
        return
    queue = _ensure_fetch_queue()
    now = time.time()
    with _fetch_lock():
        for rid in request_ids:
            row = queue.get(rid)
            if row is not None and row.get("state") in ("complete", "failed"):
                row["finished_at"] = now


def tell_the_fetch_feed():
    """Send one FETCH feed line for every row whose state moved since the last
    call. Never raises: a console that cannot be told must not stop the
    dispatcher. Called from fetch_dispatcher_worker(), outside the fetch
    lock, so a slow console never holds it."""
    try:
        import announce
        queue = _ensure_fetch_queue()
        with _fetch_lock():
            snapshot = {rid: dict(row) for rid, row in queue.items()}
        seeded = _fetch_feed_seeded[0]
        _fetch_feed_seeded[0] = True
        for rid in [rid for rid in _fetch_feed_told if rid not in snapshot]:
            del _fetch_feed_told[rid]
        just_finished = []
        for rid, row in sorted(snapshot.items(), key=lambda kv: kv[1].get("requested_at", 0)):
            state = row.get("state")
            if _fetch_feed_told.get(rid) == state:
                continue
            _fetch_feed_told[rid] = state
            if seeded and state in ("complete", "failed"):
                just_finished.append(rid)
            if not seeded or row.get("request_type") == "list" or state not in _FETCH_FEED_STATES:
                continue
            event = _fetch_feed_event(row)
            if event is None:
                continue
            action, text = event
            announce.feed_event("FETCH", text, bot=row.get("bot"), action=action)
        _stamp_finished(just_finished)
    except Exception as feed_err:
        print(f"[FETCH] Could not tell the console about a fetch: {feed_err}")


def fetch_dispatcher_worker():
    """Small dedicated background loop, started as a daemon thread from
    oserve.startup() alongside queue_mgr.queue_worker. Kept separate rather
    than piggybacked onto queue_mgr's own loop: that loop paces OUTBOUND
    socket writes one at a time and is already dense; this one only ever
    touches config.fetch_queue and never blocks on the network itself.
    """
    print("[FETCH] Dispatcher worker started.")
    while True:
        try:
            check_fetch_queue()
        except Exception as dispatch_err:
            print(f"[FETCH] Dispatcher loop error: {dispatch_err}")
        tell_the_fetch_feed()
        time.sleep(2.0)


# ==========================================================================
# Inbound offer: parsing, admission control, and the actual transfer.
# ==========================================================================

def parse_dcc_send_offer(ctcp_text):
    """Parse 'DCC SEND <filename> <ip_long> <port> <size>' (optionally with a
    quoted filename containing spaces, mIRC's own convention for that case).

    Returns {"filename", "ip", "port", "size"} for a normal (active) offer,
    where the caller dials `ip`:`port` itself; or, for the passive/reverse
    form 'DCC SEND <filename> <ip_long> 0 <size> <token>',
    {"filename", "ip": None, "port": 0, "size", "token"} - port 0 plus a
    trailing token is the standard convention meaning the offering bot cannot
    accept an inbound connection (usually firewalled) and wants US to listen
    instead, then reply with our own ip:port plus the same token echoed back.
    Mirrors adminchat.parse_offer()'s identical (ip, port, token) shape for
    passive DCC CHAT - see that function's docstring for the same convention
    explained in more depth. Returns None for anything malformed, including
    a bare 'port 0' with no token (nothing to answer it with).

    The ip_long decode is the exact inverse of dcc.get_public_ip_long()
    (dcc.py).
    """
    text = str(ctcp_text).strip().strip("\x01").strip()
    if not text.upper().startswith("DCC SEND "):
        return None
    rest = text[len("DCC SEND "):].strip()
    if not rest:
        return None

    if rest.startswith('"'):
        m = re.match(r'"([^"]*)"\s+(.+)$', rest)
        if not m:
            return None
        filename, remainder = m.group(1), m.group(2)
    else:
        parts = rest.split()
        if len(parts) < 4:
            return None
        filename, remainder = parts[0], " ".join(parts[1:])

    fields = remainder.split()
    if len(fields) < 3:
        return None

    try:
        ip_long = int(fields[0])
        port = int(fields[1])
        size = int(fields[2])
    except (ValueError, TypeError):
        return None

    if not filename:
        return None
    if contains_unsafe_ctcp_bytes(filename):
        # CRLF/CTCP injection guard, checked here rather than only where the
        # filename is later used - see _UNSAFE_CTCP_BYTES_RE's comment above
        # for why. Applies to the active form too, even though only the
        # passive reply currently echoes the filename back: a filename this
        # hostile is not a real DCC client's output either way.
        return None
    if port < 0 or port > 65535:
        return None
    if ip_long < 0 or ip_long > 0xFFFFFFFF:
        return None
    if size <= 0:
        # Not just "not negative": a declared size of exactly 0 is just as
        # malformed as a negative one. `_run_transfer()`'s own loop is
        # `while bytes_received < total_size` - with total_size == 0 that
        # condition is 0 < 0, so it never runs even once, and the row would
        # mark 'complete' immediately after opening a connection but reading
        # zero bytes. Treat it the same as any other unusable offer: rejected
        # here, before a connection is ever made.
        return None

    if port == 0:
        # Passive/reverse DCC SEND. The token identifies this request and
        # MUST come back in our own reply offer, or the offering bot cannot
        # match the two and ignores us - so a "port 0" with no token is just
        # as unusable as any other malformed offer, not a valid passive one.
        # ip_long is deliberately NOT validated as a real address here (it
        # often is not one - some bots send 0): it is never dialled, so
        # nothing depends on it being well-formed.
        if len(fields) < 4 or not fields[3]:
            return None
        token = fields[3]
        if contains_unsafe_ctcp_bytes(token):
            # Same CRLF/CTCP injection guard as the filename above - the
            # token is echoed back into our own reply CTCP verbatim (see
            # _serve_passive_offer()), so it is just as much an injection
            # vector as the filename is.
            return None
        # Best-effort only (see _serve_passive_offer()'s comment above its
        # listener.accept() call): ip_long is still present on the wire for a
        # passive offer even though it is never dialled - real passive-DCC
        # senders typically fill it with their own detected address, the
        # same convention as the active-offer wire format. Decode it, if it
        # decodes to anything at all, purely so the eventual accept() can
        # compare the peer that actually connects against what the offer
        # itself claimed. Deliberately not validated/trusted any further
        # here (0 and other junk are explicitly tolerated) - nothing above
        # depends on it being well-formed, this is purely for that one later
        # best-effort comparison.
        try:
            claimed_ip = str(ipaddress.IPv4Address(ip_long)) if ip_long else None
        except (ipaddress.AddressValueError, ValueError):
            claimed_ip = None
        return {"filename": filename, "ip": None, "port": 0, "size": size,
                "token": token, "claimed_ip": claimed_ip}

    try:
        ip = str(ipaddress.IPv4Address(ip_long))
    except (ipaddress.AddressValueError, ValueError):
        return None

    # ADDRESSES THAT CANNOT BE A PEER AT ALL.
    #
    # Until now the only test on this field was that the integer fits in 32
    # bits, so whoever held the offering nick chose an address this daemon
    # would connect to. The concrete shape is `DCC SEND x 0 22 1` - ip_long 0
    # decodes to 0.0.0.0, which connect() treats as localhost, pointing the
    # fetcher at a port on its own host.
    #
    # DELIBERATELY NARROWER THAN dcc.is_offerable_to_strangers(), which is the
    # same field judged from the other side. That one also refuses loopback
    # and private ranges, and it is right to: an offer WE advertise carrying
    # one is an offer no stranger can dial. Refusing them on the way IN would
    # be wrong, because there the address is the peer's, not ours - two
    # DCCore bots on the same LAN exchanging lists over 192.168.x.y is an
    # ordinary setup, and the operator running both is not a stranger to
    # either. It would also refuse every local transfer this project's own
    # tests perform over 127.0.0.1.
    #
    # So this refuses only what can never name a real peer: the unspecified
    # address, multicast, and the reserved ranges. Refused at the parse rather
    # than at the connect, because this return value is what every later stage
    # acts on.
    if not _is_a_possible_peer(ip):
        print(f"[FETCH] Refusing an offer that names {ip}: the unspecified, "
              f"multicast and reserved ranges cannot be a bot offering a "
              f"file.")
        return None

    return {"filename": filename, "ip": ip, "port": port, "size": size}


def _is_a_possible_peer(ip_text):
    """Could a bot actually be offering a file from this address?

    Not "is it routable on the public internet" - see the comment at the call
    site for why that stricter question, which dcc.is_offerable_to_strangers()
    asks of our OWN address, gives the wrong answer for an address arriving
    from a peer. Loopback and private ranges are legitimate here: two bots on
    one LAN, or one machine talking to itself.

    What is left is what can never be a peer at all - 0.0.0.0, which connect()
    reads as localhost, plus multicast and the reserved ranges.
    """
    import ipaddress

    try:
        address = ipaddress.IPv4Address(str(ip_text).strip())
    except Exception:
        return False
    return not (address.is_unspecified or address.is_multicast
                or address.is_reserved)


# The states in which a row may be waiting on the other bot (#1083).
_STILL_WAITING = ("pending", "offered", "queued", "listening", "receiving")


def another_row_wants_locked(queue, bot, asked_for):
    """True if a row still waiting asks `bot` for the same file. Caller holds
    _fetch_lock, with the row being let go already out of `queue`.

    An "@bot-remove <file>" is matched by name on the other side and takes
    every entry of ours for it, so letting one row go there would cancel a
    newer request for the same file - "Download again", or asking again from
    the list - which then sat queued here with nothing coming (#1083)."""
    wanted_bot = str(bot or "").strip().lower()
    wanted = _normalize_filename_for_match(asked_for or "")
    for row in queue.values():
        if row.get("state") not in _STILL_WAITING or row.get("request_type", "file") != "file":
            continue
        if str(row.get("bot") or "").strip().lower() != wanted_bot:
            continue
        if _normalize_filename_for_match(row.get("requested_filename") or row.get("filename") or "") == wanted:
            return True
    return False


def _normalize_filename_for_match(name):
    """Loosen filename comparison enough to survive the one transformation
    every DCC client applies: replacing spaces with underscores (see dcc.py's
    own outbound SEND, dcc.py:1012, `file_name.replace(" ", "_")`). Treats
    runs of whitespace/underscore as equivalent and compares case-insensitively.
    """
    return re.sub(r'[\s_]+', ' ', str(name).strip()).strip().lower()


def _claim_matching_offer_locked(queue, from_nick, filename):
    """Find and claim (mark 'receiving') the 'offered' row this CTCP answers.

    Must be called with the fetch lock held. Returns (request_id, row), or
    (None, None) if nothing pending matches - which is the admission-control
    rejection path: an offer with no matching outbound request is never
    acted on.

    Branches on the candidate row's request_type, which is the ONLY thing
    that differs between the three - everything else (size cap, path
    containment, passive-vs-active handling) runs identically afterwards in
    handle_incoming_offer(), regardless of which branch matched here:

      * "file" (the original, default behaviour): exact bot+filename match,
        underscore/space-normalised - unchanged from before request_type
        existed.
      * "list": bot alone - we sent a bare "@<bot>" (see check_fetch_queue())
        and cannot know ahead of time what the target bot will name its list
        zip, so any filename from the right bot is acceptable.
        enqueue_fetch() now refuses to create a second "list"/"folder" row
        for a bot that already has one outstanding (see
        has_outstanding_bot_alone_request()), specifically so this branch
        and the "folder" branch below can never end up racing to claim the
        same ambiguous offer - but the queue is still just a dict any code
        could in principle mutate directly, so if more than one "list" row
        somehow ends up outstanding for the same bot anyway, the OLDEST one
        is claimed - the same requested_at tie-break the dispatcher itself
        already uses when promoting pending rows.
      * "folder": bot alone, identical reasoning and identical oldest-wins
        tie-break to "list" - we sent "!<bot> !rar <folder path>" and cannot
        know ahead of time what the target bot will name the resulting .rar
        either. On a match, row["filename"] is overwritten with the real
        advertised name (row["requested_filename"], set once at creation, is
        left untouched - see new_fetch_row()).

    The exact-match "file" check runs FIRST and independently of the "list"
    and "folder" checks below it (not as an either/or on the same row) - a
    "file" row can only ever be satisfied by an exact filename match, and a
    "list"/"folder" row can only ever be satisfied by its own bot-alone
    match; no branch can accidentally satisfy another request_type's
    requirement for a DIFFERENT row, because each loop only ever looks at
    rows of its own request_type - a "folder" row must never satisfy a
    "list" row's match (or vice versa) any more than either can satisfy a
    "file" row's, and vice versa.
    """
    wanted_bot = str(from_nick).strip().lower()
    wanted_name = _normalize_filename_for_match(filename)

    for rid, row in queue.items():
        if row.get("state") not in _AWAITING_OFFER_STATES:
            continue
        if row.get("request_type", "file") != "file":
            continue
        if str(row.get("bot", "")).strip().lower() != wanted_bot:
            continue
        if _normalize_filename_for_match(row.get("filename", "")) != wanted_name:
            continue
        row["state"] = "receiving"
        return rid, row

    # A LATE ANSWER TO A REQUEST WE GAVE UP ON. A bot with a busy queue holds
    # our request and sends minutes later; the row has by then failed as "no
    # response", and the file we asked for was refused as unsolicited. It is
    # still the answer to our own request, so a row that failed only for
    # silence, and only just, takes it.
    now = time.time()
    for rid, row in queue.items():
        if (row.get("state") == "failed" and row.get("reason") == "no response"
                and row.get("request_type", "file") == "file"
                and row.get("offered_at") is not None
                and 0 <= now - row["offered_at"] <= _LATE_OFFER_GRACE
                and str(row.get("bot", "")).strip().lower() == wanted_bot
                and _normalize_filename_for_match(row.get("filename", "")) == wanted_name):
            row.pop("reason", None)
            row["state"] = "receiving"
            return rid, row

    list_candidates = [
        (rid, row) for rid, row in queue.items()
        if row.get("state") in _AWAITING_OFFER_STATES
        and row.get("request_type") == "list"
        and str(row.get("bot", "")).strip().lower() == wanted_bot
    ]
    if list_candidates:
        rid, row = min(list_candidates, key=lambda pair: pair[1].get("requested_at", 0))
        # Record the actual advertised filename now that we know it - the row
        # was created with filename="" (see webserver.build_list_fetch_enqueue_result()),
        # since it genuinely was not knowable before this moment.
        row["filename"] = filename
        row["state"] = "receiving"
        return rid, row

    folder_candidates = [
        (rid, row) for rid, row in queue.items()
        if row.get("state") in _AWAITING_OFFER_STATES
        and row.get("request_type") == "folder"
        and str(row.get("bot", "")).strip().lower() == wanted_bot
    ]
    if folder_candidates:
        rid, row = min(folder_candidates, key=lambda pair: pair[1].get("requested_at", 0))
        # Same reasoning as the "list" branch above: the row was created with
        # filename="!rar <folder path>" (see
        # webserver.build_folder_rar_fetch_enqueue_result()), the literal
        # request text, not the name the target bot will actually give its
        # packed .rar - that is only known now. row["requested_filename"] was
        # set once at creation (new_fetch_row()) and is left untouched here,
        # so the original request text survives even after this overwrite.
        row["filename"] = filename
        row["state"] = "receiving"
        return rid, row

    return None, None


def _sanitize_offer_filename(raw_name):
    """Never trust an offer's filename as a literal path component.

    Strips control/colour codes (list.py's regex), path separators, `..`,
    null bytes, and anything outside dcc.py's own charset whitelist
    (dcc.py:762) - in that order, then falls back to a safe placeholder if
    nothing printable survives. The CALLER still must run the result through
    dcc.is_safe_path() against FETCHED_FILES_DIR before opening a file with
    it; this function only produces a plausible bare filename, it does not
    itself prove the final path is safe.
    """
    name = list_mod.strip_control_codes(raw_name)
    name = name.replace('\x00', '')
    name = name.replace('/', '_').replace('\\', '_')
    name = name.replace('..', '')
    name = _FILENAME_CHARSET_RE.sub('', name)
    name = name.strip().strip('.').strip()
    if not name:
        name = "fetched_file"
    return name


# One path COMPONENT, in bytes. NTFS allows 255 characters per name and ext4
# 255 bytes, so a byte budget of 255 satisfies both - measured in UTF-8,
# because a filename of accented characters costs two bytes each and Linux
# counts those.
#
# platform_compat.long_path() does NOT cover this. The `\\?\` prefix lifts the
# 260-character limit on the TOTAL PATH; the per-component limit is a
# filesystem rule underneath it and stays exactly where it was. The comment at
# the open() call in _receive_offer_bytes() said the wrap made the offered
# name's length safe, and it did not: 255 was still the wall, verified against
# a real filesystem.
MAX_NAME_BYTES = 255


def _truncate_utf8(text, limit):
    """`text` cut to at most `limit` UTF-8 bytes, never mid-character.

    errors="ignore" is what drops a trailing partial sequence: slicing encoded
    bytes can land inside a multi-byte character, and a name is decoded again
    before it is ever used.
    """
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _fit_name_component(name, limit=MAX_NAME_BYTES):
    """`name` shortened to fit one path component, keeping its extension.

    The STEM is what shrinks, the way announce.fit_irc_filename() shrinks a
    stem rather than cutting the end off: the extension is how the operator
    and their player recognise what the file is, and a name trimmed the other
    way arrives as "Symphony No 9 in D mino" with no extension at all.
    """
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, ext = os.path.splitext(name)
    ext_bytes = len(ext.encode("utf-8"))
    if ext_bytes >= limit:
        # A pathological "extension" longer than the whole budget is not an
        # extension worth preserving.
        return _truncate_utf8(name, limit)
    return _truncate_utf8(stem, limit - ext_bytes) + ext


def _resolve_destination_path(request_id, raw_filename):
    """Build the on-disk path a completed fetch will be written to, or None
    if it fails the path-containment check. The request id is folded into
    the stored filename so two fetches that happen to share a cleaned
    filename can never collide or overwrite each other.

    The finished component is length-fitted LAST, after the request id is
    prefixed, because that prefix is part of what has to fit: 12 hex
    characters plus an underscore, so an offered name of 243 characters was
    already over the line before this function returned. The offering bot
    chooses that length, and the failure it caused was an
    "[Errno 22] Invalid argument" from open() - caught and reported as
    "transfer error", which names neither the length nor the name.
    """
    dest_dir = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))
    clean_name = _sanitize_offer_filename(raw_filename)
    stored_name = _fit_name_component(f"{request_id}_{clean_name}")
    candidate = os.path.join(dest_dir, stored_name)
    if not dcc.is_safe_path(dest_dir, candidate):
        return None, None
    return dest_dir, stored_name


# Exactly what _resolve_destination_path() above prefixes: uuid.uuid4().hex[:12],
# always hex, always followed by the underscore joining it to the cleaned
# name. _fit_name_component() only ever shrinks from the END (it shrinks the
# STEM and keeps the extension - see its own docstring), so this prefix is
# never itself truncated away; it is always intact at the front of a stored
# name built by that function.
_REQUEST_ID_PREFIX_RE = re.compile(r"^[0-9a-f]{12}_")


def _promote_clean_filename(dest_dir, stored_name):
    """Rename a just-completed fetch from its ID-prefixed staging name to
    the plain name the peer offered, now that the collision the ID guarded
    against - two fetches racing for the same name while neither had
    finished yet - can no longer happen for this one. Returns the name
    actually on disk afterward (the plain one on success, `stored_name`
    unchanged otherwise).

    Reported live: an operator's Downloads folder and List Browser fetches
    both filling up with names like "058c4cc8ee9a_Some Track.mp3" and
    "ef31cd79d1f5_SomeBot-Default(2026-01-02)-OS.zip" - the ID never meant
    anything to a human, and for a fetched list it survived even further:
    downloading the zip and unzipping it on Windows produced a folder named
    after the ZIP FILE ITSELF (there is no folder recorded inside a plain
    list zip for Explorer to unpack "into", the way there would be for a
    folder packed with rar's -ep1 - see dcc.py's own use of that flag), so
    the id rode all the way onto the operator's own disk.

    Never overwrites an existing file at the plain name - a fetch of the
    same filename from a different bot, or an operator's own file already
    there - the same "log and leave both alone" rule note_nick_change()
    already applies for the identical reason (irc.py). No data is lost
    either way; the file just keeps its longer name on this one occasion.

    Never raises: a rename that fails for any OS-level reason costs a
    readable name, not the fetch, which has already succeeded by the time
    this runs.
    """
    match = _REQUEST_ID_PREFIX_RE.match(stored_name)
    if not match:
        return stored_name
    plain_name = stored_name[match.end():]
    if not plain_name:
        return stored_name

    old_path = os.path.join(dest_dir, stored_name)
    new_path = os.path.join(dest_dir, plain_name)
    if os.path.exists(platform_compat.long_path(new_path)):
        return stored_name
    try:
        os.rename(platform_compat.long_path(old_path), platform_compat.long_path(new_path))
        return plain_name
    except OSError as err:
        print(f"[FETCH] Could not drop the request id from {stored_name!r} "
              f"after completion ({err}); keeping the longer name.")
        return stored_name


def _already_fetched_row_locked(queue, from_nick, filename):
    """The id of a finished row for this bot and file, or None. Log wording
    only: an offer is never accepted on the strength of it."""
    wanted_bot = str(from_nick).strip().lower()
    wanted_name = _normalize_filename_for_match(filename)
    for rid, row in queue.items():
        if (row.get("state") == "complete" and row.get("request_type", "file") == "file"
                and str(row.get("bot", "")).strip().lower() == wanted_bot
                and _normalize_filename_for_match(row.get("filename", "")) == wanted_name):
            return rid
    return None


def handle_incoming_offer(irc_sock, from_nick, ctcp_payload):
    """Entry point, dispatched from irc.py's CTCP branch in a daemon thread.

    Parses the offer, enforces admission control (must match a row WE marked
    'offered' a moment ago), enforces the size cap BEFORE connecting or
    listening, then runs the bounded transfer. Every exit path that is not a
    clean 'complete' leaves the claimed row 'failed' with a short reason - it
    never leaves a row stuck in 'receiving'/'listening' forever.

    Admission control, the size cap and the destination-path check are
    IDENTICAL for the active and passive (port 0) forms and all run here,
    BEFORE either a socket is dialled or a listening socket is ever opened -
    an unsolicited passive offer is dropped in exactly the same place, and
    exactly as early, as an unsolicited active one.
    """
    offer = parse_dcc_send_offer(ctcp_payload)
    if offer is None:
        print(f"[FETCH] Unusable DCC SEND offer from {from_nick}: {ctcp_payload!r}")
        return

    is_passive = offer["port"] == 0

    queue = _ensure_fetch_queue()
    with _fetch_lock():
        # What each row was before the claim, which moves the one it takes to
        # "receiving": whether it was waiting in the other bot's queue decides
        # the slot check below.
        states_before = {rid: r.get("state") for rid, r in queue.items()}
        request_id, row = _claim_matching_offer_locked(queue, from_nick, offer["filename"])
        if row is None:
            # ADMISSION CONTROL: no matching outbound request. This is the
            # core safety guardrail - without it, any user or bot in the
            # channel could hand the daemon an arbitrary IP:port to connect
            # to (active) or make it open a listening socket and accept
            # arbitrary bytes (passive) just by sending an unsolicited DCC
            # SEND. Applies identically to both forms.
            again = _already_fetched_row_locked(queue, from_nick, offer["filename"])
            if again is not None:
                # The other bot sent a file we already have (its retry or a
                # duplicate in its own queue): refused all the same, but not
                # an unknown sender, so the log says which it is.
                print(f"[FETCH] Ignored a second DCC SEND from {from_nick} "
                      f"({offer['filename']!r}): already fetched (request {again}).")
                return
            print(f"[FETCH] Rejected unsolicited{' passive' if is_passive else ''} "
                  f"DCC SEND from {from_nick} ({offer['filename']!r}): "
                  f"no matching pending request.")
            return

        # A "folder" row packs a whole album/discography into one .rar, which
        # routinely dwarfs any single file - MAX_FETCH_FILE_SIZE (default
        # 200MB) would make this feature fail on its very first real use, so
        # it gets its own, larger cap instead. A "list" row is the opposite
        # case: a master-list zip is a small text index, never a real
        # download, and letting it use the general 200MB cap is what let
        # zipfile.ZipFile() eagerly parse a huge central directory before any
        # guard in list_fetch.py could refuse it (#162 finding #10) - refused
        # here, before we even connect, same as the other two.
        if row.get("request_type") == "folder":
            max_size = int(getattr(config, "MAX_FETCH_FOLDER_FILE_SIZE", 2147483648))
            cap_name = "MAX_FETCH_FOLDER_FILE_SIZE"
        elif row.get("request_type") == "list":
            max_size = int(getattr(config, "MAX_FETCH_LIST_FILE_SIZE", 10 * 1024 * 1024))
            cap_name = "MAX_FETCH_LIST_FILE_SIZE"
        else:
            max_size = int(getattr(config, "MAX_FETCH_FILE_SIZE", 200 * 1024 * 1024))
            cap_name = "MAX_FETCH_FILE_SIZE"
        # 0 MEANS NO LIMIT. From #302: "Files should never be rejected based
        # on size." The caps are not deleted, because deleting them would take
        # the choice away from everyone else - they are switchable off, which
        # is the same outcome for the operator who wants it and no change for
        # the operator who does not know they exist.
        #
        # Defensible here in a way it would not be on the serving side: a fetch
        # is SOLICITED. handle_incoming_offer() only accepts an offer matching
        # a row we created, so the thing that arrives is the thing this
        # operator asked for, onto their own disk. The cap protects against a
        # peer answering a request with something enormous, which is worth a
        # default - not against a stranger pushing files at us, which is
        # refused earlier and for a different reason.
        if max_size > 0 and offer["size"] > max_size:
            _mark_failed_locked(row, f"declared size {offer['size']} exceeds {cap_name} ({max_size})")
            print(f"[FETCH] Rejected oversized offer from {from_nick}: "
                  f"{offer['size']} > {max_size}. Never connected. "
                  f"Set {cap_name} to 0 for no limit.")
            return

        # ROOM FOR IT (#964), before connecting - as the size cap is. An offer
        # bigger than the free space was accepted, filled the disk to
        # nothing, failed, and was asked for again: with more than
        # MIN_FREE_BYTES back once the partial file went, the disk did not
        # count as low, and the loop never ended. Now it waits, pending,
        # until what it declared fits with MIN_FREE_BYTES to spare.
        room = _room_left_locked(queue, _free_bytes(), exclude=request_id)
        if not _has_room(offer["size"], room):
            _hold_for_space(row, offer["size"],
                            f"needs {_mb(offer['size'])} free and {_mb(room)} is - "
                            f"asking again once there is space")
            print(f"[FETCH] Not taking {offer['filename']} from {from_nick} yet: "
                  f"{offer['size']} bytes, {room} free. Never connected. "
                  f"It is asked for again once there is space.")
            return

        # A LISTENER FOR A QUEUED ROW TAKES A FREE SLOT. A row queued at
        # another bot holds no slot of ours (#926), so the dispatcher asks
        # other bots meanwhile, and its offer is admitted whenever its turn
        # comes - refusing it would throw its place in that queue away. An
        # ACTIVE offer still is: it costs a connection, bounded by
        # FETCH_MAX_PER_BOT per bot. But a PASSIVE one opens a listener in
        # the DCC port range the bot's own sends to its users share, and
        # queues at several bots coming due together could take every port
        # in it. Past MAX_FETCH_SLOTS a passive offer for a queued row is
        # not taken: the row is asked for again once a slot is free. A late
        # offer takes a failed row, which holds no slot either.
        max_slots = int(getattr(config, "MAX_FETCH_SLOTS", 3))
        if (is_passive and states_before.get(request_id) in ("queued", "failed")
                and count_active_fetches(queue) > max_slots):
            row.update(state="pending", offered_at=None,
                       reason="its turn came with every fetch slot in use - asking again once one is free")
            _as_asked(row)
            for stale in ("queued_at", "queue_position", "reply"):
                row.pop(stale, None)
            print(f"[FETCH] Not listening for {offer['filename']!r} from {from_nick} yet: "
                  f"all {max_slots} fetch slots are in use. It is asked for again once one is free.")
            return

        dest_dir, stored_name = _resolve_destination_path(request_id, offer["filename"])
        if stored_name is None:
            _mark_failed_locked(row, "unsafe destination path")
            print(f"[FETCH] Rejected offer from {from_nick}: sanitised filename escaped FETCHED_FILES_DIR.")
            return

        row["total_size"] = offer["size"]
        row["stored_filename"] = stored_name
        if is_passive:
            row["state"] = "listening"
            row["listening_since"] = time.time()

    if is_passive:
        _serve_passive_offer(irc_sock, from_nick, row, offer, dest_dir, stored_name)
        return

    _run_transfer(row, offer, dest_dir, stored_name)


def _open_fetch_listener():
    """Bind a listener inside the shared DCC port range, for answering a
    passive/reverse DCC SEND offer.

    Three different listeners already share this one small range (11 ports
    by default): dcc.py's own outbound SEND (start_dcc_send()) scans UPWARD
    from DCC_PORT_START, and adminchat._open_chat_listener()'s passive DCC
    CHAT scans DOWNWARD from DCC_PORT_END. An earlier version of this
    function also scanned downward from DCC_PORT_END - meaning it shared
    adminchat's exact first-probed port and every port after it, despite a
    docstring here claiming the two landed at "opposite ends" (that claim
    had only ever compared this function to dcc.py, never to adminchat, the
    listener it actually collides with).
    Starting from the MIDPOINT and scanning outward (then wrapping) instead
    means this function's first-probed port is never the same as either of
    the other two listeners' first-probed port, for as long as there is more
    than one free port in the range - all three still ultimately compete for
    the same finite pool once it's nearly full, which no ordering can avoid.
    Same range as every other DCC listener in this project either way, so no
    extra firewall rule is needed for this to work.
    """
    start = int(getattr(config, "DCC_PORT_START", 55000))
    end = int(getattr(config, "DCC_PORT_END", 55010))
    mid = (start + end) // 2
    ordered_ports = list(range(mid, end + 1)) + list(range(mid - 1, start - 1, -1))
    for port in ordered_ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR means the OPPOSITE thing on Windows - platform_compat
        # picks the right option per platform. Same call adminchat.py and
        # dcc.py's own listener setup already make.
        platform_compat.prepare_listener(sock)
        try:
            sock.bind(("0.0.0.0", port))
            # listen() HERE, before returning - see adminchat._open_chat_listener()'s
            # comment: on POSIX, SO_REUSEADDR lets a second socket bind the
            # same port while the first is bound but not yet listening, so a
            # merely-bound socket could be raced by a second passive offer.
            sock.listen(1)
            return sock, port
        except OSError:
            sock.close()
            continue
    return None, None


def _serve_passive_offer(irc_sock, from_nick, row, offer, dest_dir, stored_name):
    """Answer a passive/reverse DCC SEND offer: we become the listener.

    Mirrors adminchat._listen_and_serve()'s identical pattern for passive DCC
    CHAT - open a listener in the shared DCC port range, announce our own
    ip:port plus the offer's token through the CTCP reply, and wait with a
    bounded timeout for the offering bot to connect back. On a successful
    accept, hand off into the SAME bounded-transfer code (_run_transfer())
    the active path uses - only how the socket was obtained differs.

    Called only from handle_incoming_offer(), and only AFTER admission
    control, the size cap and the destination-path check have all already
    passed - this function is not itself a fresh trust boundary, it only
    ever runs for a request already matched to a row this bot created.
    """
    ip_long = dcc.get_public_ip_long()
    if not ip_long:
        _mark_failed_locked(row, "our own public IP is unknown")
        print(f"[FETCH] Cannot answer {from_nick}'s passive DCC SEND offer: "
              f"the bot's own public IP is unknown (config.MY_IP_OR_DOCK did not resolve).")
        return

    listener, port = _open_fetch_listener()
    if listener is None:
        start = getattr(config, "DCC_PORT_START", 55000)
        end = getattr(config, "DCC_PORT_END", 55010)
        _mark_failed_locked(row, "no free DCC port for the passive listener")
        print(f"[FETCH] No free port in {start}-{end} to answer {from_nick}'s "
              f"passive DCC SEND offer for {offer['filename']!r}.")
        return

    oserve = sys.modules.get("oserve")
    if not (oserve and hasattr(oserve, "queue_message")):
        # No paced outbound queue available. This should not happen outside a
        # very early boot race or a test that forgot to stub it, but leaving
        # the row stuck 'listening' with no way to ever answer it would be
        # worse than failing it outright - and the listener must not leak.
        _mark_failed_locked(row, "outbound message queue unavailable")
        print(f"[FETCH] Cannot answer {from_nick}'s passive DCC SEND offer: "
              f"oserve.queue_message is unavailable.")
        try:
            listener.close()
        except OSError:
            pass
        return

    try:
        listener.settimeout(PASSIVE_LISTEN_TIMEOUT)
        # dcc.py's own outbound SEND replaces spaces with underscores before
        # sending the handshake (dcc.py:1012) - the same transformation any
        # DCC client applies, and needed here too so the filename cannot
        # swallow the positional fields that follow it.
        safe_filename = offer["filename"].replace(" ", "_")
        token = offer["token"]
        # Defense-in-depth only, expected to be unreachable: parse_dcc_send_
        # offer() already rejects any offer whose filename/token contains
        # \r, \n or \x01 before it is ever turned into an `offer` dict (see
        # contains_unsafe_ctcp_bytes() and its callers there). This is one of
        # several places this exact injection class has recurred in this
        # feature (see contains_unsafe_ctcp_bytes()'s own comment above), so
        # this call site - the one that actually interpolates both values
        # into a raw outbound CTCP line - does not simply trust that
        # upstream check was applied; it refuses to build the message at all
        # if either value is still unsafe for some future reason.
        if contains_unsafe_ctcp_bytes(safe_filename) or contains_unsafe_ctcp_bytes(token):
            _mark_failed_locked(row, "unsafe characters in offer filename/token")
            print(f"[FETCH] Refusing to answer {from_nick}'s passive DCC SEND "
                  f"offer: filename/token contains control characters this "
                  f"late (should be unreachable - see parse_dcc_send_offer()).")
            return
        message = (f"PRIVMSG {from_nick} :\x01DCC SEND {safe_filename} "
                   f"{ip_long} {port} {offer['size']} {token}\x01\r\n")
        oserve.queue_message(from_nick, message)
        print(f"[FETCH] Answered {from_nick}'s passive DCC SEND offer for "
              f"{offer['filename']!r} on port {port}; waiting for the connection.")

        # SECURITY (accepted, narrowed risk - not an oversight, same spirit as
        # WEBUI_HOST's no-auth comment in config.py): accept() below takes the
        # FIRST TCP connection that arrives on this port, from ANYONE who can
        # reach it, and cannot itself verify that the peer is actually
        # `from_nick`'s bot. Passive DCC is passive precisely because the
        # offering bot's real source address is not reliably knowable ahead of
        # time (that is WHY it asked us to listen instead of dialling it) -
        # there is no WHO/WHOIS-derived address lookup in this codebase to
        # check the peer against, and the DCC SEND protocol itself has no
        # post-connect handshake to authenticate the peer with (unlike admin
        # DCC CHAT, which layers its own password auth over the accepted
        # socket - see adminchat.py's _serve()/AUTH handling - there is
        # nothing equivalent to layer on top of a raw file byte stream, which
        # IS the payload here).
        #
        # Mitigations actually in place:
        #   1. Admission control (handle_incoming_offer(), before this
        #      function is ever called) means a listener is only ever opened
        #      for a fetch WE explicitly requested - the residual risk is
        #      specifically WHO answers a request we made, not whether an
        #      attacker can make us ask in the first place.
        #   2. The listener is single-shot: one accept(), then closed in the
        #      `finally` below - never re-armed - and only open for
        #      PASSIVE_LISTEN_TIMEOUT (60s) inside the narrow, already-
        #      firewalled config.DCC_PORT_START..DCC_PORT_END range.
        #   3. Best-effort peer check, below, after the accept succeeds: if
        #      the offer's own ip_long field decoded to something usable
        #      (offer["claimed_ip"], from parse_dcc_send_offer()), the
        #      accepted peer's address is compared against it and a mismatch
        #      is logged and recorded on the row - but deliberately NOT
        #      treated as a hard rejection. A real offering bot behind NAT
        #      routinely advertises a private/internal address that
        #      legitimately differs from its outbound public address, and
        #      hard-rejecting on that basis would break real-world interop
        #      for exactly the deployments passive DCC exists to support.
        #
        # Residual, accepted risk: a same-LAN or otherwise well-positioned
        # attacker who races the real offering bot's connection within the
        # ~60s window, on this narrow/low-cardinality port range, can still
        # win and have their bytes accepted as the "fetched" file. There is
        # no robust fix for this without either inventing a nonstandard
        # protocol extension a real third-party bot would not speak, or a
        # WHO/WHOIS round trip this codebase does not otherwise perform -
        # both judged disproportionate to what a plain-text file-sharing
        # protocol with no authentication of its own can realistically offer.
        conn, addr = listener.accept()
    except socket.timeout:
        _mark_failed_locked(row, "passive offer: no connection received")
        print(f"[FETCH] {from_nick} never connected back within "
              f"{int(PASSIVE_LISTEN_TIMEOUT)}s for the passive DCC SEND offer "
              f"on port {port}; giving the port back.")
        return
    except OSError as err:
        _mark_failed_locked(row, f"passive listen error: {err}")
        print(f"[FETCH] Passive DCC SEND listener error for {from_nick}: {err}")
        return
    except Exception as err:
        # Defense-in-depth, expected to be unreachable: everything above this
        # point already has its own specific handling. This exists so that
        # NO exception - not just the two anticipated above - can ever leave
        # this row stuck in 'listening' forever. handle_incoming_offer() runs
        # in a bare daemon thread with nothing above it to catch a stray
        # exception, and check_fetch_queue()'s own expiry loop only re-checks
        # 'offered' rows (see its docstring), not 'listening' ones - so
        # without this, an unanticipated error here would silently and
        # permanently strand a MAX_FETCH_SLOTS slot until the process
        # restarts. (check_fetch_queue() ALSO now expires a stale 'listening'
        # row on a timer, as a second, independent safety net in case this
        # function's own thread dies or hangs before even reaching this
        # try block.)
        _mark_failed_locked(row, f"unexpected error: {err}")
        print(f"[FETCH] Unexpected error answering {from_nick}'s passive DCC "
              f"SEND offer for {offer['filename']!r}: {err!r}")
        return
    finally:
        try:
            listener.close()
        except OSError:
            pass

    peer_ip = addr[0] if addr else None
    claimed_ip = offer.get("claimed_ip")
    row["passive_peer_ip"] = peer_ip
    if claimed_ip and peer_ip and claimed_ip != peer_ip:
        # Best-effort only - see the long comment above listener.accept() for
        # why this is logged/recorded rather than treated as a hard reject.
        row["passive_peer_ip_mismatch"] = True
        print(f"[FETCH] WARNING: passive DCC SEND connection answering "
              f"{from_nick}'s offer of {offer['filename']!r} arrived from "
              f"{peer_ip}, but the offer itself claimed {claimed_ip}. "
              f"Accepting it anyway (best-effort check only - see the "
              f"comment above listener.accept() in dcc_fetch.py); treat a "
              f"mismatch here as suspicious.")

    with _fetch_lock():
        row["state"] = "receiving"

    _run_transfer(row, offer, dest_dir, stored_name, sock=conn)


def _handle_completed_list_fetch(row, zip_path):
    """Delegate a completed request_type="list" fetch to list_fetch.py for
    safe extraction/parsing, and record the outcome on the row for the
    dashboard - but never let a problem there affect the fetch itself, which
    already succeeded (the bytes arrived intact; this is purely about what is
    INSIDE them). Imported locally, not at module top, for the same reason
    dcc_fetch.py already imports `list as list_mod` and not e.g. `webserver`
    at top level: keeps this module's own import graph minimal and avoids a
    cycle (list_fetch.py imports dcc_fetch's sibling modules, not the other
    way around).

    THE ZIP IS REMOVED ON SUCCESS. List Browser reads exclusively from
    list_fetch.py's own extracted copy under FETCHED_FILES_DIR/lists/<bot>/
    (get_fetched_bot_page() re-parses that file fresh on every view - see
    its own docstring) - nothing anywhere re-opens the raw zip once
    process_fetched_list_zip() has returned True, including a re-fetch,
    which downloads a fresh one rather than touching the old. Reported
    live: an operator downloaded one of these zips to their own machine and
    unzipped it, and the request id ended up in the resulting filename too
    - there is no folder recorded inside a plain list zip for an unzip tool
    to extract "into" the way there would be for one packed with rar's
    -ep1 (see dcc.py's own use of that flag), so Explorer named the result
    after the zip itself. Removing the zip is the fix for that as much as
    for the disk space: the raw download was never the deliverable, the
    browsable list is, and it already exists on its own.

    On FAILURE the zip is left exactly where it was - the one piece of
    diagnostic evidence for why a peer's archive could not be read, and
    deleting it would trade that for nothing.
    """
    try:
        import list_fetch
        ok, reason = list_fetch.process_fetched_list_zip(row.get("bot", ""), zip_path)
        if not ok:
            row["list_processing_error"] = reason or "no recognizable list file found in the zip"
            print(f"[FETCH] {row.get('bot')}'s fetched list zip was received "
                  f"successfully but could not be processed: {row['list_processing_error']}")
            return
    except Exception as err:
        # Defense-in-depth, expected to be unreachable: list_fetch.py's own
        # entry point already catches everything it knows how to anticipate.
        # This exists so that an unanticipated error while processing an
        # untrusted third party's zip can never propagate back out of a
        # transfer that itself already completed successfully.
        row["list_processing_error"] = f"unexpected error: {err}"
        print(f"[FETCH] Unexpected error processing {row.get('bot')}'s fetched list zip: {err!r}")
        return

    try:
        os.remove(platform_compat.long_path(zip_path))
        # None, not the now-deleted name: webserver.api_fetch_download()
        # already answers a missing stored_filename with 404 either way, but
        # leaving the stale name would offer a Download button in the
        # dashboard for a file that no longer exists - browsing the list
        # itself is how this fetch is meant to be used from here on.
        row["stored_filename"] = None
    except OSError as err:
        # The list is already safely extracted and browsable either way -
        # only the now-redundant raw zip failed to go, which costs disk
        # space, not correctness.
        print(f"[FETCH] {row.get('bot')}'s list was extracted successfully, "
              f"but its zip could not be removed afterward ({err}).")


def _offer_timeout_for(row, offer_timeout, folder_timeout, unadvertised_timeout):
    """How long this particular offer is allowed to go unanswered.

    A "folder" row waits far longer than a file, because the other bot has to
    run its own packing pipeline before it can even start the DCC SEND. That
    is the right allowance for a bot that IS packing an album - and much too
    generous for one that never packs anything, where a non-answer is the
    expected outcome rather than a slow one. The wait is one of
    MAX_FETCH_SLOTS, so paying it needs a reason.

    See list_fetch.bot_publishes_a_rar_list() for what counts as a sign. Used
    to decide how long to WAIT, never whether to ask: a bot can pack folders
    with neither signal, and refusing on this would take away something that
    works, where waiting less costs nothing when the guess is wrong.
    """
    if row.get("request_type") != "folder":
        return offer_timeout
    try:
        import list_fetch
        if list_fetch.bot_publishes_a_rar_list(row.get("bot", "")):
            return folder_timeout
    except Exception:
        # This runs inside the queue lock on the sweep every tick. A failure
        # deciding which of two numbers to use is not worth stalling the queue
        # for; the longer one is the safe way to be wrong, since it only ever
        # waits, never gives up on something still coming.
        return folder_timeout
    return unadvertised_timeout


def _fetch_transfer_timeout(request_type):
    """The wall-clock ceiling (seconds) _run_transfer() gives a fetch of
    `request_type`, pulled out as a pure function so the decision itself is
    unit-testable without running a real transfer.

    #162 finding #11: FETCH_TRANSFER_TIMEOUT is sized for the 200MB
    MAX_FETCH_FILE_SIZE cap. A "folder" row's own MAX_FETCH_FOLDER_FILE_SIZE
    is 10x larger but used to inherit that SAME wall clock, so a legitimately
    slow transfer of a large discography could be aborted (no resume - every
    retry identical) well before it had any chance to finish.
    """
    if request_type == "folder":
        return getattr(config, "FETCH_FOLDER_TRANSFER_TIMEOUT", 6144)
    return getattr(config, "FETCH_TRANSFER_TIMEOUT", 600)


def _run_transfer(row, offer, dest_dir, stored_name, sock=None):
    """The actual bounded socket transfer. `row` has already been claimed
    ('receiving') and validated by handle_incoming_offer(); this just moves
    bytes, with three independent guards:

      * CONNECT_TIMEOUT on the dial itself (active offers only)
      * IDLE_RECV_TIMEOUT per recv() call (mirrors dcc.py:1024's conn.settimeout(60.0))
      * FETCH_TRANSFER_TIMEOUT as a wall-clock ceiling, for a slow-drip peer
        that keeps resetting the idle timer without ever finishing

    and aborts (deleting the partial file) if the peer sends more than it
    declared.

    `sock` is None for a normal (active) offer, in which case this dials
    offer["ip"]:offer["port"] itself exactly as before. For the passive/
    reverse form, _serve_passive_offer() has already listened and accepted
    the inbound connection, and hands the resulting socket in here directly -
    everything from this point on (size cap enforcement, idle/wall-clock
    timeouts, oversize-abort) is identical either way; only how the socket
    was obtained differs.
    """
    total_size = offer["size"]
    dest_path = os.path.join(dest_dir, stored_name)
    wall_deadline = time.time() + float(_fetch_transfer_timeout(row.get("request_type")))
    # For the panel's Downloading speed (#1019); this transfer's own clock.
    row["receiving_since"] = time.time()

    try:
        os.makedirs(platform_compat.long_path(dest_dir), exist_ok=True)
    except Exception as mkdir_err:
        _mark_failed_locked(row, f"could not create destination dir: {mkdir_err}")
        print(f"[FETCH] {mkdir_err}")
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
        return

    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((offer["ip"], offer["port"]))
        except Exception as connect_err:
            _mark_failed_locked(row, f"connect error: {connect_err}")
            print(f"[FETCH] Could not connect to {offer['ip']}:{offer['port']}: {connect_err}")
            # Only an ACTIVE connect counts (#926): we dialled them and could
            # not reach them. A passive offer that nobody connects back to is
            # about our side, not theirs.
            _note_connect_failure(row.get("bot"))
            try:
                sock.close()
            except Exception:
                pass
            return

    sock.settimeout(IDLE_RECV_TIMEOUT)

    # For a passive offer offer["ip"] is None (never dialled - see
    # parse_dcc_send_offer()); the peer's real address is only known once we
    # have actually accepted its connection.
    peer_desc = offer.get("ip")
    if peer_desc is None:
        try:
            peer_desc = sock.getpeername()[0]
        except OSError:
            peer_desc = "?"

    bytes_received = 0
    failure_reason = None
    disk_full = False
    handle = None
    try:
        # Two different limits, and long_path() only lifts one of them. The
        # `\\?\` wrap handles the 260-character TOTAL PATH limit, which is why
        # dcc.py wraps every path it touches; _resolve_destination_path() has
        # already fitted the NAME to MAX_NAME_BYTES, which the wrap does not
        # affect and which the offering bot would otherwise choose.
        handle = open(platform_compat.long_path(dest_path), "wb")
        acknowledging = True
        while bytes_received < total_size:
            if time.time() > wall_deadline:
                failure_reason = "overall transfer timeout"
                break
            try:
                chunk = sock.recv(RECV_CHUNK)
            except socket.timeout:
                failure_reason = "idle timeout"
                break
            if not chunk:
                # Peer closed early. Only acceptable if it happens to land
                # exactly on the declared size (some clients close instead
                # of lingering) - otherwise it is a short transfer.
                if bytes_received < total_size:
                    failure_reason = "connection closed before declared size was reached"
                break

            bytes_received += len(chunk)
            if bytes_received > total_size:
                # A lying offer: the peer is sending more than it declared.
                # Abort rather than silently keeping the overflow.
                failure_reason = "received more bytes than the declared size"
                handle.write(chunk[:max(0, len(chunk) - (bytes_received - total_size))])
                bytes_received = total_size
                break

            handle.write(chunk)
            row["bytes_received"] = bytes_received
            # DCC's acknowledgement: our running total as four bytes, big-
            # endian. A DCCore sender counts a file as sent only once the
            # whole of it is acknowledged, so without these every fetch from
            # one looked like a failed send to it and was offered again -
            # 15 seconds later, and again after a few minutes (#1019).
            # Given up on after the first failure: a sender that never reads
            # them fills our send buffer, and every further sendall would wait
            # out the socket's timeout.
            if acknowledging:
                try:
                    sock.sendall(struct.pack("!I", bytes_received & 0xFFFFFFFF))
                except OSError:
                    acknowledging = False   # closed after the last byte, or not listening
    except Exception as recv_err:
        failure_reason = f"transfer error: {recv_err}"
        disk_full = _is_disk_full(recv_err)
    finally:
        try:
            if handle:
                handle.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    if failure_reason is None and bytes_received == total_size:
        row["state"] = "complete"
        row["bytes_received"] = bytes_received
        _note_connect_success(row.get("bot"))
        transfer_log.record_received(
            {"list": transfer_log.KIND_LIST, "folder": transfer_log.KIND_ALBUM}.get(
                row.get("request_type"), transfer_log.KIND_FILE),
            bytes_received, nick=row.get("bot"))
        if row.get("request_type") == "list":
            # The DCC transfer itself succeeded (declared size matched what
            # arrived) - that is what "complete" above means, and is left
            # alone either way. What happens NEXT - safely unzipping and
            # parsing an untrusted third party's list archive - is a
            # genuinely separate trust boundary (see list_fetch.py's module
            # docstring: zip-slip, zip-bomb, "no recognisable list file
            # inside"), so it is handled by a dedicated module and never
            # allowed to raise back into this transfer's own success path.
            #
            # No _promote_clean_filename() call here: on success the raw zip
            # is about to be removed entirely (see _handle_completed_list_fetch()'s
            # own comment on why), so renaming it first would be wasted work.
            print(f"[FETCH] Complete: {stored_name} ({bytes_received} bytes) from {peer_desc}.")
            _handle_completed_list_fetch(row, dest_path)
        else:
            # The id in stored_name only ever existed to keep this transfer
            # from colliding with another one racing for the same cleaned
            # name - see _resolve_destination_path()'s own docstring. That
            # risk ends the moment the file is fully written, so the
            # operator's own copy does not have to go on carrying it.
            final_name = _promote_clean_filename(dest_dir, stored_name)
            row["stored_filename"] = final_name
            print(f"[FETCH] Complete: {final_name} ({bytes_received} bytes) from {peer_desc}.")
        return

    if failure_reason is None:
        failure_reason = f"incomplete transfer ({bytes_received}/{total_size} bytes)"

    if disk_full:
        # Not this request's fault (#926): it goes back to pending, and the
        # dispatcher holds everything until there is space again.
        # Held until the whole file fits (#964): the disk counting as low
        # again is not enough, because the partial file is about to go.
        _hold_for_space(row, total_size,
                        "the disk filled up - asking again once there is space")
        _disk_was_low[0] = False  # so the next check says it
        print(f"[FETCH] The disk filled up receiving {stored_name}; it will be asked again.")
    else:
        _mark_failed_locked(row, failure_reason)
        print(f"[FETCH] Failed ({failure_reason}): {stored_name}.")
    try:
        if os.path.exists(platform_compat.long_path(dest_path)):
            # Unwrapped, exists() answers False for a >260 path and the
            # partial file from a failed long-named transfer is never
            # cleaned up.
            os.remove(platform_compat.long_path(dest_path))
    except OSError as cleanup_err:
        # Logged, not swallowed. webserver.py's equivalent cleanup prints on
        # the same failure; here an antivirus or an open handle holding the
        # partial file left debris under FETCHED_FILES_DIR with no clue why
        # disk use was climbing (#234).
        print(f"[FETCH] Could not remove the partial file {dest_path}: "
              f"{cleanup_err}")
        pass
