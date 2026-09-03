# =====================================================================
# IRC.PY - THE IRC NETWORK MODULE FOR UNDERNET (PART 1 OF 3)
# =====================================================================
import socket
import functools
import threading
import time
import re
import sys
import os
import traceback
import urllib.request

# The bot's own modules
import defaults as config
import platform_compat
import runtime
import list
import dcc
import runtime
import security

# Tracks whether the channels have been joined
bot_joined_channel = False


def _release_socket():
    """Clear the shared network reference as soon as a socket has closed.

    The queue_mgr pump and announce.send_debug both check `if current_sock:` before
    writing. Without this reset the reference still pointed at a CLOSED socket for
    the whole reconnect, so those guards did nothing and every write raised
    OSError for no reason.
    """
    import sys
    oserve_mod = sys.modules.get('oserve')
    if oserve_mod:
        oserve_mod.irc_connection = None


def is_server_numeric(line, code):
    """True only when `line` is a genuine server numeric with this code.

    A server numeric is always ':<prefix> <code> <target> ...'. Testing for the
    bare substring instead lets any user forge one by typing it in a channel,
    because the read loop sees every PRIVMSG as raw text before the PRIVMSG
    parser and the ban check ever run.

    That is not theoretical for this bot. `"001" in line` matched an ordinary
    music request - "!DCCore 001 - Enter Sandman.flac" - and `" 513 " in line
    and "PONG" in line` let anyone make the daemon emit unthrottled raw PONGs
    straight to the socket, bypassing queue_mgr's pacing entirely.
    """
    return re.match(r"^:\S+\s+" + code + r"\s+\S+", line) is not None


def is_user_event(line, command):
    """True only when `line` is a genuine user event with this command.

    A user event is always ':<nick>!<user>@<host> <COMMAND> ...' - the command
    sits in the command position, immediately after the prefix. Testing for the
    bare substring instead matches the command name ANYWHERE, including inside a
    PRIVMSG body, a PART or QUIT reason, or a channel TOPIC.

    That was reachable by accident, not only by attack: `" QUIT " in line` matched
    an ordinary search like "@find QUIT PLAYING GAMES", and the QUIT handler then
    removed the searcher from every channel in config.channel_users - which
    freezes their queue and hands it to the five-minute delete timer, for the
    crime of looking for a song.
    """
    return re.match(r"^:\S+!\S+\s+" + command + r"(\s|$)", line) is not None


def event_source_nick(line):
    """The nick a user event came FROM, read out of the prefix.

    Never search the whole line for a nick. A QUIT reason is free text, so
    ':attacker!u@h QUIT :bye :DCCore!x@y' made `":DCCore!" in line.lower()` true
    and handed anyone the handlers that key off who an event came from.
    """
    match = re.match(r"^:([^!\s]+)!", line)
    return match.group(1).lower() if match else None


def event_source_host(line):
    """The host a line came FROM, read out of the prefix. Lowercased, or None.

    Anchored at the start for the same reason as event_source_nick: a message
    body is free text, and a hostmask quoted inside one must never be mistaken
    for the sender's own.

    This is what makes the admin console possible. On Undernet a user who has
    logged into X and set +x is given the host "<account>.users.undernet.org",
    which only the server can issue - so the host, unlike the nick, is proof of
    who someone is. The PRIVMSG parser below used to discard it.
    """
    match = re.match(r"^:[^!\s]+!\S*@(\S+)", line)
    return match.group(1).lower() if match else None


def parse_privmsg(line):
    """(nick, ident_host, target, message) for a well-formed PRIVMSG line,
    or None.

    Pulled out of irc_loop() so this parsing itself is unit-testable
    without the real socket loop - matching is_user_event()'s own reason
    for existing as a standalone function above.

    Anchored the same way is_user_event()/event_source_nick() already are,
    for the same two reasons: `[^!\\s]+` for the nick (a server numeric's
    prefix has no "!" but does have spaces, and an unanchored nick group
    ran straight across them into the numeric's own text - the reported
    repro was a 332 topic line parsing as
    user='irc.undernet.org 332 DCCore #chan :Welcome'); and a `\\S*\\s+`
    bridge to the literal "PRIVMSG" instead of a greedy `.* ` (which let a
    PRIVMSG's own free-text BODY containing a second
    " PRIVMSG <target> :" be matched instead of the real one - `\\S*` can
    never cross the whitespace before the real command word, so it cannot
    skip forward looking for a second, attacker-supplied match).

    `ident_host` is the raw "ident@host" between "!" and the command word,
    for security.check_user_status()'s hostmask-pattern matching - not the
    same as event_source_host(), which strips the ident and lowercases.
    """
    match = re.match(r"^:([^!\s]+)!(\S*)\s+PRIVMSG\s+([#\w\-]+)\s+:(.+)$", line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3), match.group(4)


def parse_notice(line):
    """(nick, target, message) for a well-formed NOTICE line, or None. See
    parse_privmsg()'s docstring for why the anchoring matters - the same
    greedy-`.* ` and unanchored-nick problems applied here identically."""
    match = re.match(r"^:([^!\s]+)!\S*\s+NOTICE\s+([#\w\-]+)\s+:(.+)$", line)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


# Anchored to the START of the line, deliberately.
#
# Every bot that answers an @find replies with a header line and then one
# line per match, and the HEADER also contains a "!" token - it is telling
# the user what to type:
#
#   Search Result 1 Match For X   Copy And Paste !Echosonic FILENAME To ...
#   !Echosonic 50 Oldies Party - ... .mp3  ::INFO:: 4.6MB
#
# Searching anywhere in the line matched the header too, and produced a
# Download button for a file literally named "FILENAME To The Channel To
# Request. (25/25) Free Slots...". Ordinary channel chatter arriving during
# the window was worse: "Thank You !!! I have now received 1 file(s)..."
# yielded bot="!!" and offered to fetch from it.
#
# Both would have sent a nonsense "!" request into a live channel on click.
# A real result line always begins with its token; a sentence mentioning one
# never does.
_FETCH_TOKEN_RE = re.compile(r'^!(\S+)\s+(.+)$')


# ---------------------------------------------------------------------
# Reading the HEADER line other bots send before their matches
# ---------------------------------------------------------------------
# An @find reply arrives as a header line and then one line per match. The
# header is not a result - it is the bot introducing itself - and it carries
# the things worth showing above a group of results: how many matches it
# found, how busy it is, and what it runs.
#
# Two families answer in these channels.
#
# OmenServe (v2.60 and v2.71 both seen) is the common one:
#
#   Search Result 3 Matches For X  Copy And Paste !bot FILENAME To The
#   Channel To Request. (4/4) Free Slots, 0 Queued OmeNServE v2.60
#
# Operators pick their own separator between sections - ":", "~", "*", or
# none at all - so nothing below anchors on punctuation, only on the words.
# When a search matches more than it will send, it says so instead of
# listing slots:
#
#   Search Result 12 Matches For X   Get My List Of 94,952 Files By Typing
#   @Beezer In The Channel Or Refine Your Search. Sending first 5 Results
#
# SPQR is an older, less widely used mIRC script - a minority of operators
# still run it. Different shape, no version string, no match count, and its
# RESULT lines carry no "::INFO:: <size>" tag either:
#
#   Matches for *X*  Copy and Paste in Channel to Request a File
#   (Slot:0/) (Que:0/16) in Use
#
# Anything unrecognised returns None rather than a wrong guess. The grouped
# view falls back to counting the result lines actually received, which
# works for every family including ones nobody here has seen.
_HDR_MATCHES    = re.compile(r'(\d[\d,]*)\s+Match(?:es)?\b', re.I)
_HDR_SLOTS      = re.compile(r'\((\d+)\s*/\s*(\d+)\)\s*Free\s*Slots', re.I)
_HDR_QUEUED     = re.compile(r'(\d+)\s+Queued\b', re.I)
_HDR_SERVER     = re.compile(r'\b(Omen\s*Serve?\s*v?\s*[\d.]+)', re.I)
_HDR_LIST_SIZE  = re.compile(r'List\s+Of\s+([\d,]+)\s+Files', re.I)
_HDR_SENDING    = re.compile(r'Sending\s+first\s+(\d+)', re.I)
_HDR_SPQR_SLOTS = re.compile(r'\(Slot:\s*(\d+)\s*/\s*(\d*)\)', re.I)
_HDR_SPQR_QUEUE = re.compile(r'\(Que:\s*(\d+)\s*/\s*(\d+)\)', re.I)


def _as_int(text):
    """"94,952" -> 94952. Returns None for anything that is not a number."""
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_search_header(text):
    """Stats out of another bot's @find header line, or None if it is not one.

    Returns a dict with whatever that family actually publishes - callers
    must treat every key as optional. A missing key means "this bot did not
    say", never "zero": showing 0 free slots for a bot that simply does not
    report them would be worse than showing nothing.
    """
    if not text:
        return None

    # A RESULT line is never a header, even though it often ends in the same
    # version string inside its "::INFO:: 4.6MB OmeNServE v2.60" tail. Gating
    # here rather than at the call site means the function cannot be misused
    # into decorating a result row with the sender's stats.
    if _FETCH_TOKEN_RE.search(text):
        return None

    stats = {}

    slots = _HDR_SLOTS.search(text)
    if slots:
        stats["family"] = "omenserve"
        stats["slots_free"] = _as_int(slots.group(1))
        stats["slots_total"] = _as_int(slots.group(2))
    else:
        spqr = _HDR_SPQR_SLOTS.search(text)
        if spqr:
            stats["family"] = "spqr"
            # SPQR reports slots IN USE, and often leaves the total blank -
            # the opposite sense from OmenServe's "free". Kept as its own
            # key so the frontend never has to guess which one it holds.
            stats["slots_in_use"] = _as_int(spqr.group(1))
            total = _as_int(spqr.group(2))
            if total is not None:
                stats["slots_total"] = total

    queued = _HDR_QUEUED.search(text)
    if queued:
        stats["queued"] = _as_int(queued.group(1))
    else:
        spqr_q = _HDR_SPQR_QUEUE.search(text)
        if spqr_q:
            stats["family"] = stats.get("family", "spqr")
            stats["queued"] = _as_int(spqr_q.group(1))
            stats["queue_total"] = _as_int(spqr_q.group(2))

    server = _HDR_SERVER.search(text)
    if server:
        stats["server"] = " ".join(server.group(1).split())
        stats.setdefault("family", "omenserve")

    matches = _HDR_MATCHES.search(text)
    if matches:
        stats["matches"] = _as_int(matches.group(1))

    list_size = _HDR_LIST_SIZE.search(text)
    if list_size:
        stats["list_size"] = _as_int(list_size.group(1))

    sending = _HDR_SENDING.search(text)
    if sending:
        # "12 Matches ... Sending first 5 Results" - it found more than it
        # sent, which is worth saying above the group so the operator knows
        # to refine rather than assuming five is all there is.
        stats["sending"] = _as_int(sending.group(1))

    return stats or None


# ---------------------------------------------------------------------------
# Reading the periodic advert other bots send to the channel
# ---------------------------------------------------------------------------
#
# Every file-serving bot on this network announces itself on a timer. Captured
# from a busy public file-sharing channel: 392 lines, 33 bots with a readable
# advert, in four different wordings and no two decorated alike. The capture
# is the specification, and every line of it is a fixture in
# tests/test_advert_listener.py.
#
# THE ADVERT IS NOT ALWAYS ONE MESSAGE
#
# Five of the thirty-one split their advert across two PRIVMSGs, because it
# does not fit IRC's 512-byte line limit. The break lands wherever the bot ran
# out of room, mid-field and sometimes mid-value:
#
#   Echosonic, line 1:  ... Served: 16,399 <> List:
#   Echosonic, line 2:  Aug 25th <> Search: ON <> Mode: Normal <>
#
# Read on its own, line 1 says the bot published no list date and line 2 is not
# an advert at all. Both are wrong, and "no date" is the answer that matters -
# it is the whole freshness signal. So a line that is not an advert, from a bot
# that advertised moments ago, is stitched onto what it sent before and the
# pair is re-read as one. See _capture_channel_advert().
#
# THREE WORDINGS, PLUS OUR OWN
#
#   OmenServe / OmenTweak / DCCore - 28 of the 33, the "Type: @nick" wording:
#     Type: @Zkx For My List Of: 719,041 Files <> Slots: 10/10 <> Queued: 0
#     <> Speed: 0cps <> Served: 3,456,016 <> List: Aug 10th <> Mode: Normal
#
#   SPQR - BigRig and outlook, a different sentence entirely, and the only
#   family that puts a size in the PRIVMSG advert:
#     For My List(19527files:163812MB) and DCC Status, type @BigRig and
#     @BigRig-stats. [(0/7) Slots (0/216) Ques Taken]
#
#   RAR folders - a SECOND, separate list some bots serve beside their loose
#   files, under a "^"-suffixed trigger, with its own count and its own size:
#     Type @Zkx^ to get my list of 39,454 (5.48 TB) RAR folders
#   Zkx publishes both: 719,041 loose files AND 39,454 RAR folders. They are
#   two different lists and are kept apart in the registry for that reason.
#
# WHAT THE SAMPLE SETTLES
#
#   * The LABELS are stable, the layout is not. Across the sample the field
#     separator is a yen sign, a tilde, a colon, a copyright sign, a registered
#     sign, an asterisk, a control character, plain spacing, or - for five bots
#     - a byte that is not valid UTF-8 and so never survives the socket's
#     errors="ignore" decode at all. Nothing here keys on position or on what
#     sits between fields.
#   * Preamble is normal. Bots open with an album count, a sentence of French,
#     a note about an OP. The advert is not the whole line and cannot be
#     anchored to its start.
#   * Nicks can carry brackets and backticks - @[rigserv], @`Glider - so a
#     word-characters-only pattern would miss them.
#   * A list DATE is published by every bot in the OmenServe family. A list
#     SIZE is published only by SPQR, by the RAR-folder advert, and by DCCore
#     itself - so nothing may depend on a size being there.
#
# Same rule as parse_search_header() above: return whatever that bot actually
# published and treat every field as optional. A missing key means "this bot
# did not say", never zero.

# How long after an advert a following line may still be its continuation.
# Every split in the sample arrived within 3 seconds; the window is wider than
# that so a lagged server does not lose the tail, and short enough that a bot's
# ordinary chatter minutes later is never mistaken for one.
ADVERT_CONTINUATION_SECONDS = 15.0

# A stitched advert cannot grow without bound: a bot that chats steadily would
# otherwise accumulate every line it sends inside the window.
ADVERT_MAX_STITCHED_CHARS = 1200

KNOWN_BOTS_FLUSH_SECONDS = 30.0

# The registry is filled from unauthenticated channel text: anyone can create an
# entry by advertising once under a nick nobody has used, and a nick-change loop
# creates one per change. Measured at 20,000 distinct nicks: 20,000 entries,
# 1.7 MB, and 29ms of json.dumps on every flush - and it is persisted, so it
# comes back after a restart.
#
# The sibling buffer in the same capture path, _advert_tails, has been
# time-pruned since it was written. This one collected the timestamp to prune
# by - "last_seen" - and never used it.
#
# A week, because a bot that has not advertised in seven days is not one this
# bot is going to be asked about, and because the interesting case (a channel
# full of servers you might fetch a list from) refreshes every few minutes. The
# cap is the backstop for a burst that arrives faster than the TTL retires it.
KNOWN_BOTS_TTL_SECONDS = 7 * 24 * 60 * 60
KNOWN_BOTS_MAX = 2000

_ADVERT_NICK_RE = re.compile(r"Type:\s*@(\S+)", re.IGNORECASE)
_ADVERT_COUNT_RE = re.compile(
    r"For\s+My\s+List\s+Of:?\s*([\d,]+)\s*Files", re.IGNORECASE)
_ADVERT_SIZE_RE = re.compile(r"Files\s*\(([^)]{1,20})\)", re.IGNORECASE)
_ADVERT_DATE_RE = re.compile(
    r"(?:List:|Date:|created)\s*([A-Za-z]{3,9}\s*\d{1,2}(?:st|nd|rd|th)?)",
    re.IGNORECASE)

# SPQR: "For My List(19527files:163812MB) ... type @BigRig and @BigRig-stats."
_SPQR_LIST_RE = re.compile(
    r"For\s+My\s+List\s*\(\s*([\d,]+)\s*files\s*:\s*([\d,.]+\s*[KMGT]?B)\s*\)", re.IGNORECASE)
_SPQR_NICK_RE = re.compile(r"type\s+@(\S+)", re.IGNORECASE)

# The separate RAR-folder list: "Type @Zkx^ to get my list of 39,454 (5.48 TB)
# RAR folders". The trigger carries a "^" the bot's own nick does not.
_RAR_RE = re.compile(
    r"Type\s+@(\S+?)\^\s+to\s+get\s+my\s+list\s+of\s+([\d,]+)\s*\(([^)]{1,20})\)\s*RAR\s+folders",
    re.IGNORECASE)

# nick.lower() -> [first_seen, text_so_far] for an advert that may still be
# continued on a following line. Module level rather than in runtime.py because
# it is worth seconds and nothing else: !rehash reloads what is named in
# commands.CORE_MODULES and not this module, and even if that changed,
# losing this costs one advert cycle's continuation and nothing more.
#
# The list used to be spelled out here as "dcc/config/oserve/queue_mgr/list".
# oserve and queue_mgr have never been in it, and config is now defaults - so
# it named two modules that are not reloaded and one that no longer exists.
# Naming the constant instead cannot drift (#232).
_advert_tails = {}


def _parse_omenserve_advert(clean):
    """The "Type: @nick For My List Of: n Files" wording - OmenServe, OmenTweak
    and DCCore, 28 of the 33 bots in the sample.

    Identified by the trigger and the count TOGETHER. Either alone is ordinary
    chatter: an op telling a newcomer what to type writes the trigger exactly
    as a bot would.
    """
    nick = _ADVERT_NICK_RE.search(clean)
    count = _ADVERT_COUNT_RE.search(clean)
    if not nick or not count:
        return None

    advert = {
        "family": "omenserve",
        "nick": nick.group(1),
        "files": _as_int(count.group(1).replace(",", "")),
    }

    size = _ADVERT_SIZE_RE.search(clean)
    if size:
        advert["list_size"] = size.group(1).strip()

    date = _ADVERT_DATE_RE.search(clean)
    if date:
        advert["list_date"] = re.sub(r"\s+", " ", date.group(1)).strip()

    return advert


def _parse_spqr_advert(clean):
    """SPQR's wording: "For My List(19527files:163812MB) ... type @BigRig".

    No colon after "Type", no "Of:", and the count and size share one
    parenthesis - so none of the patterns above see it, and both bots running
    it were invisible to this daemon until it was added. It publishes no list
    date, which is exactly why absent has to mean "did not say".
    """
    listing = _SPQR_LIST_RE.search(clean)
    if not listing:
        return None

    nick = _SPQR_NICK_RE.search(clean)
    if not nick:
        return None

    return {
        "family": "spqr",
        "nick": nick.group(1),
        "files": _as_int(listing.group(1).replace(",", "")),
        "list_size": re.sub(r"\s+", "", listing.group(2)),
    }


def _parse_rar_folder_advert(clean):
    """The separate RAR-folder list: "Type @Zkx^ to get my list of 39,454
    (5.48 TB) RAR folders".

    A different list from the same bot, not a different bot - Zkx advertises
    719,041 loose files in one message and 39,454 RAR folders in another. The
    "^" belongs to the trigger, not to the nick, so it is stripped before the
    sender check: Zkx sends this, "Zkx^" does not exist.
    """
    found = _RAR_RE.search(clean)
    if not found:
        return None

    return {
        "family": "rar",
        "nick": found.group(1),
        "rar_trigger": found.group(1) + "^",
        "rar_folders": _as_int(found.group(2).replace(",", "")),
        "rar_size": re.sub(r"\s+", "", found.group(3)),
    }


# Order matters only in that each wording is distinct enough not to overlap:
# SPQR has no "Of:", the RAR advert has no colon after "Type" and no "For".
_ADVERT_PARSERS = (_parse_omenserve_advert, _parse_spqr_advert, _parse_rar_folder_advert)

# What each family is entitled to write into a registry entry. A bot's RAR
# advert must not overwrite the count of its loose-file list, and the other way
# round - they are two lists and Zkx publishes both.
_ADVERT_FIELDS = {
    "omenserve": ("files", "list_date", "list_size"),
    "spqr": ("files", "list_size"),
    "rar": ("rar_folders", "rar_size", "rar_trigger"),
}


def parse_channel_advert(text):
    """Stats out of another bot's channel advert, or None if it is not one.

    Returns {"family", "nick", ...} where every key but those two is optional -
    absent means the bot did not publish it. See _ADVERT_FIELDS for what each
    family can carry.
    """
    if not text:
        return None

    clean = list.strip_control_codes(text)
    for parser in _ADVERT_PARSERS:
        advert = parser(clean)
        if advert:
            return advert
    return None


def _record_bot(key, user, target, advert, now):
    """Merge one parsed advert into the registry entry for `user`."""
    entry = dict(runtime.known_bots.get(key) or {})
    entry.update({
        "nick": user,
        "channel": target,
        "last_seen": now,
    })
    for field in _ADVERT_FIELDS.get(advert.get("family"), ()):
        if field in advert:
            entry[field] = advert[field]
    runtime.known_bots[key] = entry
    _prune_known_bots(now)


def never_breaks_the_read_loop(capture):
    """Wrap an observational capture so it cannot take the connection down.

    The two capture functions below run on EVERY channel line, before the ban
    check and before the anti-flood gate, because they observe rather than
    dispatch and a foreign bot is not subject to our ban list. That placement
    is right, and it is also what makes an exception in either of them so
    expensive: it escapes the whole per-line block, and the only handler that
    catches it closes the socket and drops into the reconnect loop.

    So a stranger who can make one of these raise can hold the daemon in a
    reconnect loop for as long as they care to keep typing - unauthenticated,
    unmetered, and with a ban unable to stop it. That is what a malformed
    number in a SLOTS line did until the parser above was fixed.

    Fixing the parser removes the one known way in. This removes the CLASS:
    whatever else these two learn to read, a line they cannot parse costs
    exactly that line.
    """
    @functools.wraps(capture)
    def guarded(*args, **kwargs):
        try:
            return capture(*args, **kwargs)
        except Exception as err:
            print(f"[CAPTURE ERROR] {capture.__name__} gave up on one line "
                  f"({type(err).__name__}: {err}). The connection is unaffected.")
            return None
    return guarded


@never_breaks_the_read_loop
def _capture_channel_advert(user, target, msg, now=None):
    """Record another bot's periodic advert in runtime.known_bots.

    Observational only: it never dispatches, never replies, and never gates
    anything. Called from the channel PRIVMSG path beside the broadcast
    capture, and for the same reason - a foreign bot advertising in a channel
    we sit in is not subject to our ban list and has nothing for a ban to
    meaningfully gate.

    THE SENDER IS THE AUTHORITY ON IDENTITY, NOT THE TEXT

    "Type: @Someone" is just characters in a message, and any user can type
    them. Registering what the text claims would let anyone impersonate any
    bot - and the whole point of this registry is deciding whether the list we
    hold for a nick is current, so a poisoned entry means showing a list as
    that bot's current one when it was never theirs.

    So an advert whose claimed nick does not match the nick that sent it is
    dropped. All 33 bots in the capture agree with their own sender, so
    nothing legitimate is lost.

    A CONTINUATION IS TRUSTED ONLY AFTER THAT CHECK HAS PASSED

    The stitching below only ever appends to text a verified advert from that
    same sender left behind, so a stranger cannot feed a tail to someone else's
    advert: the buffer is keyed by sender and written only on a matching one.
    """
    if not user or not target or not target.startswith("#"):
        return
    if msg and msg.startswith("\x01"):
        # Not channel text, and never eligible for the stitch below - but
        # it is where the exact library size lives. See
        # parse_advert_slots() for why it cannot be read by position.
        _capture_advert_slots(user, target, msg,
                              time.time() if now is None else now)
        return

    now = time.time() if now is None else now
    key = user.lower()
    _prune_advert_tails(now)

    advert = parse_channel_advert(msg)
    if advert:
        if advert["nick"].lower() != key:
            print(f"[ADVERT] {user} advertised as {advert['nick']!r} - ignoring; "
                  f"the sender is the authority on who a bot is.")
            return
        _advert_tails[key] = [now, list.strip_control_codes(msg)]
        _record_bot(key, user, target, advert, now)
        _flush_known_bots()
        return

    # Not an advert on its own. It may be the rest of one - see this section's
    # comment on Echosonic, whose date arrives here and nowhere else.
    pending = _advert_tails.get(key)
    if not pending:
        return

    started, so_far = pending
    stitched = (so_far + " " + list.strip_control_codes(msg)).strip()
    if len(stitched) > ADVERT_MAX_STITCHED_CHARS:
        del _advert_tails[key]
        return

    merged = parse_channel_advert(stitched)
    if not merged or merged["nick"].lower() != key:
        return

    # Keep the ORIGINAL timestamp: a bot that talks steadily must not be able
    # to walk the window forward one line at a time.
    _advert_tails[key] = [started, stitched]
    _record_bot(key, user, target, merged, now)
    _flush_known_bots()


def _prune_known_bots(now):
    """Forget bots not seen inside the TTL, then cap what is left.

    Eviction is by last_seen ascending, so a burst of one-off nicks is what
    goes and the bots that actually advertise are what stays - the opposite of
    dropping whatever the dict happened to hold last.

    An entry with no last_seen at all (an older file, a hand edit) is treated
    as infinitely old rather than kept for ever: the field is written on every
    single capture, so a missing one means the entry predates this and is not
    being refreshed.
    """
    registry = runtime.known_bots
    for key in [k for k, entry in registry.items()
                if now - float((entry or {}).get("last_seen") or 0) > KNOWN_BOTS_TTL_SECONDS]:
        del registry[key]

    if len(registry) > KNOWN_BOTS_MAX:
        by_age = sorted(registry.items(),
                        key=lambda kv: float((kv[1] or {}).get("last_seen") or 0))
        for key, _entry in by_age[:len(registry) - KNOWN_BOTS_MAX]:
            del registry[key]


def _prune_advert_tails(now):
    """Forget adverts too old to still be continued."""
    for key in [k for k, (when, _text) in _advert_tails.items()
                if now - when > ADVERT_CONTINUATION_SECONDS]:
        del _advert_tails[key]


def _flush_known_bots(now=None, force=False):
    """Persist the registry, at most once per KNOWN_BOTS_FLUSH_SECONDS.

    Written on an interval rather than on every advert: with fifty bots
    announcing every few minutes this fires dozens of times a minute, and
    nothing reads the file until a dashboard is opened.
    """
    now = time.time() if now is None else now
    if not force and now - runtime.known_bots_flushed_at < KNOWN_BOTS_FLUSH_SECONDS:
        return False
    try:
        import db
        db.save_known_bots(runtime.known_bots)
        runtime.known_bots_flushed_at = now
        return True
    except Exception as err:
        print(f"[ADVERT] Could not save the bot registry: {err}")
        return False


# ---------------------------------------------------------------------------
# The CTCP a bot sends a few seconds after its advert
# ---------------------------------------------------------------------------
#
# Every OmenServe-family bot follows its channel advert with a CTCP, and it is
# the only place the EXACT size of a bot's library is published. announce.py
# builds ours a few hundred lines away:
#
#   SLOTS 10 10 NOW 0 999 0 719041 3894929430520 0 1786359244 25511 OmenServe v2.73
#                             ^files  ^bytes = 3.54 TB
#
# We have been sending that line for as long as the daemon has existed and
# never once read anyone else's. 27 of the 33 bots in the capture send one.
#
# READING IT BY POSITION DOES NOT WORK
#
# There are at least three layouts in one channel:
#
#   24 bots    14 fields, count at 7, bytes at 8
#   fallback77 13 fields - one shorter, so count at 6 and bytes at 7
#   SPQR       8 fields, count LAST, no byte figure at all
#
# Index 7 on fallback77's line is 14,247,378,895,149 - which as a file count
# is fourteen trillion files, and would have gone straight into the registry
# and onto the dashboard. Nothing in the line says which layout it is.
#
# SO IT IS NOT READ BY POSITION
#
# The advert already told us how many files that bot has. So: find the field
# that equals that number, and the size is the field beside it. The layout
# does not have to be known, only self-consistent - and a line that does not
# agree with the bot's own advert is refused rather than guessed at.
#
# That also means the CTCP is only ever read for a bot that has already
# advertised and passed the sender check in _capture_channel_advert(). A CTCP
# on its own registers nobody: without an advert there is no count to
# calibrate against, which is exactly the case where a wrong reading would go
# unnoticed.

# A library larger than this is a misread, not a library. The largest in the
# captured channel is 24 TB; ten petabytes is far past any of them and still
# far below the fourteen-trillion-file misreading above.
SLOTS_MAX_PLAUSIBLE_BYTES = 10 ** 16


def plain_int(text):
    """The integer this field holds, or None if it is not a plain number.

    str.isdigit() is True for characters int() REFUSES - superscript two is
    the shortest example - so guarding a field with isdigit() and then calling
    int() on it is not a guard at all. It is also True for digits from other
    scripts that int() does accept, where an Arabic-Indic zero would be read
    as 0. Neither belongs in a field another bot's script wrote as a decimal
    number, so this asks the one question that field is actually posing.
    """
    text = str(text)
    return int(text) if text.isascii() and text.isdigit() else None


def parse_advert_slots(text, known_files):
    """Size and software out of a bot's CTCP SLOTS line, calibrated against the
    file count its own advert already published.

    Returns {"list_bytes", "software"}, both optional - so {} is a real answer,
    and the one SPQR gives: its line agrees with the advert and simply carries
    nothing beyond the count. None is the other answer, and a different one:
    this is not a SLOTS line, or it does not agree with `known_files` and
    therefore cannot be read at all. Callers treat both as "record nothing";
    they are kept apart because "published nothing" and "could not be trusted"
    are not the same fact about a bot.

    See this section's comment for why nothing here is read by position.
    """
    if not text or not known_files:
        return None

    payload = text.strip("\x01").strip()
    if not payload.upper().startswith("SLOTS"):
        return None

    fields = payload.split()
    at = [i for i, field in enumerate(fields) if field == str(known_files)]
    if len(at) != 1:
        # Nothing matched, or the count is ambiguous because some other field
        # happens to carry the same number. Either way there is no way to tell
        # which neighbour is the size, and a guess here is a wrong size on the
        # dashboard rather than a missing one.
        return None

    found = {}
    index = at[0] + 1
    size = plain_int(fields[index]) if index < len(fields) else None
    if size is not None:
        # A library cannot hold fewer bytes than it holds files. SPQR's line
        # ends at the count and has no size at all, which lands here too.
        if known_files <= size < SLOTS_MAX_PLAUSIBLE_BYTES:
            found["list_bytes"] = size

    # The version string is whatever trails the numbers - "OmenServe v2.73",
    # "DCCore v1.10.0-RC4", and for one bot a truncated "OmeNServE v". Read as
    # a suffix rather than an index, so a layout with more or fewer numeric
    # fields makes no difference.
    software = []
    for field in reversed(fields):
        # isdigit() and not plain_int() here, deliberately, and the two are
        # asking different questions. The size field above asks "can int()
        # convert this", so isdigit() was the wrong test and raised on a
        # superscript two. This asks "is this field number-shaped, marking
        # where the numeric region ends" - and a garbage numeral IS part of
        # that region, so treating it as a boundary keeps it out of the
        # software string rather than letting it leak in as text.
        if field.isdigit():
            break
        software.append(field)
    if software:
        found["software"] = " ".join(reversed(software))

    return found


def _capture_advert_slots(user, target, msg, now):
    """Fold a bot's CTCP SLOTS line into the registry entry its advert built.

    Silent for a bot that has not advertised: the count that makes this line
    readable comes from the advert, so without one there is nothing to check
    the numbers against.
    """
    entry = runtime.known_bots.get(user.lower())
    if not entry:
        return

    extra = parse_advert_slots(msg, entry.get("files"))
    if not extra:
        return

    entry.update(extra)
    entry["last_seen"] = now
    _flush_known_bots()


@never_breaks_the_read_loop
def _capture_broadcast_search_reply(user, target, msg):
    """Cross-bot search broadcast capture (webserver.py's POST
    /api/search/broadcast starts the window this reads).

    Only fires for a line sent DIRECTLY TO OUR OWN NICK (`target`), never
    channel chatter - a PRIVMSG or NOTICE-to-self, which is how @find-style
    replies normally arrive (list.execute_search() itself only ever replies
    to the requester, never the channel). No-ops instantly outside an open
    broadcast window, so this costs nothing on the overwhelmingly common case
    of no broadcast in progress.

    Deliberately bypasses security.is_flooding(): that gate exists to meter
    OUR users against OUR command surface. Applying it here would mean the
    daemon could start ignoring or muting a foreign bot we explicitly asked
    to reply, for the crime of answering the broadcast we just sent it -
    quite apart from the fact that this capture never dispatches a command or
    a reply of our own, so there is nothing here for a flood gate to protect.
    """
    if str(target).lower() != str(getattr(config, 'NICKNAME', '')).lower():
        return
    if not getattr(config, 'broadcast_search_inprogress', False):
        return
    if time.time() >= getattr(config, 'broadcast_search_deadline', 0):
        return

    cleaned = list.strip_control_codes(msg)
    entry = {"from": user, "text": cleaned, "received_at": time.time()}

    # Best-effort extraction of the one convention that is actually
    # standardised across file-sharing bots: "!<botnick> <filename>", the
    # same syntax this bot itself answers to (see get_bot_aliases() and
    # dcc.handle_download_request()). Deliberately does NOT attempt to parse
    # size, format, or anything else out of arbitrary bots' bespoke
    # colour/box formatting - that is unrealistic and out of scope. If no
    # such token is found, the raw cleaned text is still recorded above; the
    # frontend just shows it without a Download button.
    token_match = _FETCH_TOKEN_RE.search(cleaned)
    if token_match:
        entry["bot"] = token_match.group(1)
        # A reply that is itself a master-list line - the overwhelmingly
        # common case, since this is what most file bots echo back for a
        # match - carries a trailing "  ::INFO:: <size>" tag. Strip it here
        # (list.strip_info_suffix(), the same split update_list.py's own
        # writer/reader pair uses) so the stored filename is the bare name a
        # later `!<bot> <filename>` request can actually be answered against -
        # the real DCC SEND offer that comes back never includes the size tag,
        # so leaving it in place made every such fetch fail admission control
        # as "unsolicited" (filenames never matched).
        filename, _size = list.strip_info_suffix(token_match.group(2).strip())
        entry["filename"] = filename
    else:
        # Not a result, so it may be the header the bot sends before its
        # matches - the one line that says how many it found, how busy it is
        # and what it runs. Parsed here, once, rather than in the dashboard on
        # every poll. None for anything unrecognised, which is most channel
        # chatter that happens to land inside the window.
        header = parse_search_header(cleaned)
        if header:
            entry["header"] = header

    # config.broadcast_search_results is bound from runtime.py at import time
    # and always exists as a real list - never rebind it, see runtime.py's
    # docstring.
    config.broadcast_search_results.append(entry)


def get_bot_aliases():
    """Every name this bot should answer to, lowercased, current nick first.

    config.NICKNAME, config.ORIGINAL_NICK and config.LIST_BASE_NAME are normally the same
    string, so this collapses to ONE entry and the triggers built from it are byte-identical
    to the previous hardcoded ones. They only diverge after a 433 nick collision.

    That divergence is why this exists. The master list is generated by update_list.py as a
    SUBPROCESS, so its request lines are always stamped with the config.py default nick
    (update_list.py's _write_text_artifact). The dispatcher matched only the LIVE nick, so while the bot was
    running as the alternate nick every pasted "!DCCore Song.flac" was dropped with no reply,
    no error and no log line - and the flood counter never even saw it. Users had no way to
    tell the bot was ignoring them.

    DELIBERATELY USED FOR ONE TRIGGER ONLY: the "!<nick> <file>" request, whose text comes
    from a list the user cannot be expected to retype. Everything typed live - "@<nick>",
    "@<nick>-que", "@<nick>-remove" - stays on the LIVE nick, because the advert publishes
    the live nick (announce.py's channel advert) so the user can see what to type. Widening those would
    make this bot act on messages addressed to whichever client currently holds the main
    nick, and for "-remove" that means destroying the queue of someone who was talking to
    somebody else.

    config.LIST_BASE_NAME is intentionally NOT included: it is a filename constant, and it
    should not become a live public IRC trigger. ORIGINAL_NICK already covers the real case,
    since the subprocess stamps the list with the config.py default nick.
    """
    aliases = []
    for candidate in (getattr(config, 'NICKNAME', None),
                      getattr(config, 'ORIGINAL_NICK', None),
                      getattr(config, 'PREVIOUS_NICK', None)):
        if not candidate:
            continue
        low = str(candidate).strip().lower()
        if low and low not in aliases:
            aliases.append(low)
    return aliases


def take_complete_lines(buffer, chunk):
    """Add `chunk` to `buffer` and return (leftover_bytes, [decoded lines]).

    Both the IRC read loop and the pre-auth NICK loop need this, and both used
    to do it inline as:

        data = s.recv(2048).decode("utf-8", errors="ignore")   # decode FIRST
        buffer += data                                         # then accumulate

    which is the wrong order for any character that is not ASCII. A UTF-8
    character is 2-4 bytes and recv() returns whatever the kernel had, so the
    boundary regularly falls INSIDE one - and a decoder handed half a character
    can only drop it. The character vanishes, the line arrives one character
    short, and nothing anywhere reports a problem: a request for a Greek,
    Cyrillic or CJK filename simply stops matching the library and the user is
    told the file does not exist. Intermittent by nature, since it depends on
    where the chunk boundary happened to land.

    Accumulating BYTES and decoding only whole \r\n-terminated lines means the
    decoder is never shown a partial character. `buffer` is bytes and the
    returned lines are str.

    "replace" rather than "ignore" for the decode: once only complete lines
    reach it, a failure means the line really did carry bytes that are not UTF-8
    (an IRC client sending CP1251, say), which is a different thing from having
    been cut in half. A visible U+FFFD says something arrived and could not be
    read; silently dropping the bytes makes it look like nothing was sent. Same
    choice update_list.py's _sanitise() already documents for filenames.
    """
    buffer += chunk
    raw_lines = buffer.split(b"\r\n")
    return raw_lines.pop(), [raw.decode("utf-8", errors="replace")
                             for raw in raw_lines]


def resolve_dcc_address(lookup=None, log=print):
    """The address every DCC offer carries, for the remote client to dial.

    Lifted out of irc_loop() so it can be tested: while it was inline, the only
    way to check "a pinned address is not overwritten" was to grep irc.py for
    the word, and that assertion passes on `if False:` with the word still in
    the file. Both branches are exercised directly now.

    An operator who set MY_IP_OR_DOCK keeps it. docs/WINDOWS.md documents doing
    exactly that for a static address or a forwarded router, and the lookup used
    to run unconditionally and overwrite it, so the documented setting silently
    did nothing.

    Otherwise ask ipify. On any failure this returns "" and NOT "127.0.0.1",
    which is what it used to do. A loopback address in a DCC offer sends every
    leecher to their own machine: they receive nothing, while the bot reports
    "Active Transfer Started" and holds a slot for each of them. Nothing caught
    it because the only guard was `if ip_long == 0`, and 127.0.0.1 converts to
    2130706433. Returning "" makes dcc.is_offerable_to_strangers() refuse, with
    a message naming the setting the operator has to fill in.

    `lookup` exists for the tests; production passes nothing and gets ipify.
    """
    pinned = str(getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    if pinned:
        log(f"[IP CHECK] Using the address configured in MY_IP_OR_DOCK: {pinned}")
        return pinned

    log("[IP CHECK] Fetching the public address from the ipify API...")
    try:
        if lookup is None:
            req = urllib.request.Request(
                "https://api.ipify.org",
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            found = urllib.request.urlopen(req, timeout=5.0).read().decode("utf-8").strip()
        else:
            found = str(lookup() or "").strip()
        log(f"[IP CHECK] Address detected. DCC IP set to: {found}")
        return found
    except Exception as e:
        log(f"[WARNING] Could not reach the ipify API ({e}).")
        log("[WARNING] No public address is known, so DCC sends will be refused "
            "rather than offered to nobody. Set MY_IP_OR_DOCK in admin_config.py "
            "or settings.conf to your public address to serve without this lookup.")
        return ""


def irc_loop():
    """The connection, PING/PONG, and every incoming PRIVMSG from Undernet."""
    global bot_joined_channel
    import announce
    oserve = sys.modules.get('oserve')
    
    config.MY_IP_OR_DOCK = resolve_dcc_address()
    
     # ---------------------------------------------------------------------
    
    # Set HERE before anything else, so the variable always exists in memory
    if not hasattr(config, 'ORIGINAL_NICK'):
        config.ORIGINAL_NICK = getattr(config, 'NICKNAME', None)

    # Per-connection epoch. Threads spawned for one connection must not act on a later
    # one; they compare this token before touching any shared state.
    if not hasattr(config, 'connection_epoch'):
        config.connection_epoch = 0

    # THE RECONNECT LOOP: makes sure this thread never dies on a split or a disconnect
    while True:
        # Every connection starts by trying to claim the bot's real original nick
        config.NICKNAME = config.ORIGINAL_NICK

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(70.0)
        
        print(f"[CONNECT] Attempting to connect to {config.SERVER}:{config.PORT} as {config.NICKNAME}...")
        
        try:
            s.connect((config.SERVER, config.PORT))
            # The three timing knobs are Linux-only; platform_compat guards them so
            # Windows still gets SO_KEEPALIVE with system defaults.
            platform_compat.apply_keepalive(s, idle=10, interval=2, count=3)
                
            oserve_mod = sys.modules.get('oserve')
            if oserve_mod:
                oserve_mod.irc_connection = s
            print(f"[CONNECT] Connected to socket successfully!")
        except Exception as e:
            print(f"[ERROR] Connection failed: {e}. Reconnecting in 10 seconds...")
            # `joined` lives in the irc_loop frame and is only reset after a SUCCESSFUL
            # handshake, so a failed attempt used to leave it latched True from the
            # previous connection - which is what let a stale watchdog pass its guard.
            joined = False
            time.sleep(10)
            continue
            
        # Send the handshake immediately; the server decides the nick via real 433 replies
        try:
            s.send(f"NICK {config.NICKNAME}\r\n".encode())
            
            auth_buffer = b""
            while True:
                auth_data = s.recv(1024)
                if not auth_data:
                    break
                auth_buffer, auth_lines = take_complete_lines(auth_buffer, auth_data)
                
                for a_line in auth_lines:
                    if " 433 " in a_line or "erroneous nickname" in a_line.lower():
                        alt_nick = getattr(config, 'ALT_NICKNAME', f"{config.ORIGINAL_NICK}`")
                        print(f"[SERVER 433] The nick {config.NICKNAME} was taken. Switching CURRENT_NICK to: {alt_nick}")
                        s.send(f"NICK {alt_nick}\r\n".encode())
                        config.NICKNAME = alt_nick
                    
                    if " 001 " in a_line or " 002 " in a_line or "PING" in a_line or "NOTICE" in a_line:
                        ident_str = getattr(config, 'IDENT', 'dccore')
                        real_str = getattr(config, 'REALNAME', 'dccore bot')
                        s.send(f"USER {ident_str} 0 * :{real_str}\r\n".encode())
                        break
                else:
                    continue
                break
                
            print(f"[INFO] Handshake complete. CURRENT_NICK settled as: {config.NICKNAME}. Starting the reader...")

            # SHORT RECV TIMEOUT (see the clock logic below): recv() lets go every
            # 20 seconds so we can run the keepalive and the silence timer ourselves.
            # The old 70-second timeout tore down HEALTHY links during quiet periods.
            s.settimeout(20.0)
        except Exception as auth_err:
            print(f"[ERROR] Error during the server handshake: {auth_err}")
            try: s.close()
            except: pass
            _release_socket()
            joined = False
            # BACKOFF: without a pause here the loop spun as fast as TCP could connect.
            # Undernet's connection throttle closes the link immediately when you come
            # back too fast, which gave tens of attempts a second and guaranteed we
            # stayed throttled - or got K-lined - instead of recovering.
            time.sleep(10)
            continue

        # This connection is now live: claim a fresh epoch. Any thread still running from
        # a previous connection holds an older token and will bail out on its next check.
        config.connection_epoch += 1
        my_epoch = config.connection_epoch

        # Bytes - see take_complete_lines() for why this must not be str.
        buffer = b""
        joined = False
        bot_joined_channel = False
        announce.is_ready = False
        
        # FIXED (issue #9): tracks WHICH channels were confirmed by 366, not just
        # a loose count. The old code counted every 366 line it saw, including the
        # debug channel's, so the threshold could be reached even if a real channel
        # never answered - and if two or more failed the threshold was never reached,
        # which silenced ALL advertising permanently, with no error message.
        target_channels = set(c.strip().lower() for c in config.CHANNEL.split(",") if c.strip())
        channels_confirmed = set()
        ACTIVATION_TIMEOUT = 20.0  # seconds after JOIN before giving up on the rest
        last_recv_time = time.time()

        # ---------------------------------------------------------------------
        # TWO SEPARATE CLOCKS, replacing the old 45s check plus 70s timeout:
        #
        # The old keepalive check sat at the top of the loop, but the loop was blocked
        # in recv() for up to 70 seconds. So recv() ALWAYS timed out before the 45
        # seconds could be checked - the keepalive PING was never reachable, and a
        # quiet channel was torn down as though the link were dead. The bot
        # reconnected, rejoined seven channels, cleared channel_users and restarted
        # the advert thread - over and over, for as long as the channels were quiet.
        #
        # Silence is now measured separately from the keepalive: recv() lets go every
        # 20 seconds, we PING the server after 45s of silence, and only tear the link
        # down if the server has not been heard from for SILENCE_LIMIT. Genuinely dead
        # links are still caught by TCP_KEEPIDLE/INTVL/CNT above, in about 16 seconds.
        # ---------------------------------------------------------------------
        SILENCE_LIMIT = 180.0    # only at this point is the link considered dead
        KEEPALIVE_AFTER = 45.0   # this much silence is allowed before we PING
        last_ping_sent = 0.0

        # HOISTED (issue #9): delayed_activate lives here now, once, instead of being
        # nested inside the 366 handler - so both the ordinary NAMES path AND
        # the timeout watchdog below can trigger the same activation logic.
        def delayed_activate(sock=s, epoch=my_epoch):
            # `sock` and `epoch` are bound as DEFAULT ARGUMENTS on purpose. Both were
            # previously free variables resolved through irc_loop's frame, and that frame
            # is rebound by every reconnect - so a thread from a dead connection read the
            # CURRENT connection's values and published the wrong socket.
            import sys
            import time
            import announce
            import threading

            if config.connection_epoch != epoch:
                return

            time.sleep(5)

            # Re-check after sleeping: the connection can die inside this window.
            if config.connection_epoch != epoch:
                print("[ACTIVATE] Connection changed while settling. Abandoning stale activation.")
                return

            # Only claim channel sync if NAMES actually populated something. The watchdog
            # path can reach here without any 353 having arrived, and dcc.py's stale-freeze
            # sweep treats bot_joined_channel as proof that channel_users is authoritative -
            # with it empty, every frozen user looks absent and their queue gets reaped.
            if getattr(config, 'channel_users', None):
                config.bot_joined_channel = True
            else:
                print("[ACTIVATE] No channel members known yet; advertising without claiming channel sync.")

            oserve_mod = sys.modules.get('oserve')
            if oserve_mod:
                oserve_mod.bot_joined_channel = config.bot_joined_channel
                oserve_mod.irc_connection = sock
                
            announce.is_ready = True
            if hasattr(announce, 'last_announce_time'):
                announce.last_announce_time = time.time()
                
            # Watches for the main nick to free up after a netsplit, and reclaims it
            def background_nick_monitor(sock_inst):
                main_nick = getattr(config, 'ORIGINAL_NICK', 'DCCore')
                # Check every 10 seconds, for up to 5 minutes after JOIN
                for _ in range(30):
                    if str(config.NICKNAME).lower() == main_nick.lower():
                        break  # Already on the main nick; stop watching
                        
                    main_nick_active = False
                    with runtime.channel_users_lock():
                        if hasattr(config, 'channel_users') and isinstance(config.channel_users, dict):
                            for chan_name, users_set in config.channel_users.items():
                                if main_nick.lower() in [u.lower() for u in users_set]:
                                    main_nick_active = True
                                    break
                                
                    if not main_nick_active:
                        print(f"\n[NICK RECOVERY] The ghost nick {main_nick} timed out. Changing nick...")
                        try:
                            sock_inst.send(f"NICK {main_nick}\r\n".encode())
                            config.NICKNAME = main_nick
                            break
                        except:
                            break
                    time.sleep(10)
            
            # Start the persistent timer in the background
            threading.Thread(target=background_nick_monitor, args=(s,), daemon=True).start()
                
            print("[CONNECT FIX] Restarting the channel advert automatically...")
            threading.Thread(target=announce.announce_worker, daemon=True).start()
            config.announce_thread_alive = True

        def activation_watchdog(epoch=my_epoch):
            """NEW (issue #9): forces activation after a reasonable wait, even if
            some channels never answered the JOIN - banned, invite-only, misspelled.
            Without this watchdog, two or more broken channels could silence ALL
            advertising for the whole life of the connection, with no error at all."""
            time.sleep(ACTIVATION_TIMEOUT)
            # Bail if this connection is gone. config.activation_triggered alone is not a
            # staleness guard: the disconnect epilogue RESETS it, which re-armed exactly
            # the threads it should have invalidated.
            if config.connection_epoch != epoch:
                print("[WATCHDOG] Connection changed before the timeout elapsed. Standing down.")
                return
            if joined and not getattr(config, 'activation_triggered', False):
                missing = target_channels - channels_confirmed
                config.activation_triggered = True
                if missing:
                    print(f"[WARNING] Activating the advert despite {len(missing)} unconfirmed channel(s): {', '.join(missing)}")
                    try:
                        announce.send_debug(
                            f"Activated after {int(ACTIVATION_TIMEOUT)}s with {config.C_BOLD}{len(missing)}{config.C_RESET} channel(s) never confirmed via NAMES: {', '.join(missing)}",
                            category="PART")
                    except Exception as watchdog_debug_err:
                        print(f"[WARNING] Could not send the watchdog debug notice: {watchdog_debug_err}")
                else:
                    print(f"[INFO] Watchdog: every channel confirmed just in time.")
                threading.Thread(target=delayed_activate, daemon=True).start()

        while True:
            try:
                try:
                    data = s.recv(2048)
                except socket.timeout:
                    now = time.time()
                    quiet_for = now - last_recv_time

                    if quiet_for > SILENCE_LIMIT:
                        print(f"[TIMEOUT] The server has been silent for {int(quiet_for)}s. Dropping the link to reconnect.")
                        try: s.close()
                        except: pass
                        _release_socket()
                        break

                    if quiet_for > KEEPALIVE_AFTER and (now - last_ping_sent) > KEEPALIVE_AFTER:
                        try:
                            s.send(b"PING :lagcheck\r\n")
                            last_ping_sent = now
                        except Exception as ping_err:
                            print(f"[TIMEOUT] The keepalive PING did not get through ({ping_err}). Dropping the link to reconnect.")
                            try: s.close()
                            except: pass
                            _release_socket()
                            break
                    continue
                except socket.error as net_err:
                    print(f"[DISCONNECT FIX] TCP keepalive detected a dead network ({net_err}). Dropping the link to reconnect.")
                    try: s.close()
                    except: pass
                    _release_socket()
                    break
                except Exception as e:
                    print(f"[IRC READ ERROR] Unexpected error while reading from the network: {e}")
                    try: s.close()
                    except: pass
                    _release_socket()
                    break

                if not data:
                    print("[DISCONNECT] Server closed connection. Breaking to reconnect motor...")
                    try: s.close()
                    except: pass
                    _release_socket()
                    break
                    
                last_recv_time = time.time()
                buffer, lines = take_complete_lines(buffer, data)
                
                for line in lines:
                    if not line.strip():
                        continue
                    if getattr(config, 'DEBUG_MODE', False):
                        is_channel_traffic = " PRIVMSG #" in line
                        is_for_me = f"PRIVMSG {config.NICKNAME}" in line or f" {config.NICKNAME} " in line or f"@{config.NICKNAME.lower()}" in line.lower()
                        if not is_channel_traffic or is_for_me or "ERROR" in line:
                            print(f"[RAW IN] {line.strip()}")
                    if line.startswith("PING"):
                        parts = line.split()
                        if len(parts) > 1:
                            pong_code = parts[1].lstrip(':')
                            s.send(f"PONG {pong_code}\r\n".encode())
                    
                    if " PONG " in line and "OSERVE_LATENCY_CHECK" in line:
                        import commands
                        commands.handle_pong_response(category="INFO")
                        continue
                            
                    # Anchored: this writes a raw PONG straight to the socket with no
                    # pacing, ahead of the ban check and the flood gate, so an unanchored
                    # test was a one-paste Excess Flood disconnect for any user in any
                    # channel - banned or not.
                    if is_server_numeric(line, "513") and "PONG" in line:
                        parts = line.split()
                        pong_code = parts[-1].strip()
                        s.send(f"PONG {pong_code}\r\n".encode())
                    # Catches ONLY official server collisions; channel chatter is ignored
                    # Anchored: " 433 " matched those digits anywhere in the line, and the
                    # PRIVMSG/NOTICE exclusion under it did not cover PART or QUIT reasons
                    # or a channel TOPIC - so parting with "bye 433" pushed the bot off its
                    # main nick and wrote an unpaced NICK straight to the socket.
                    # "erroneous nickname" was matching numeric 432 by its English wording;
                    # match the numeric itself, which no server translates and no user can
                    # forge. The PRIVMSG/NOTICE test is gone because anchoring makes it not
                    # merely dead but harmful: a genuine 433 whose text happened to contain
                    # the word PRIVMSG would have been discarded.
                    if is_server_numeric(line, "433") or is_server_numeric(line, "432"):
                        main_nick = getattr(config, 'ORIGINAL_NICK', 'DCCore')
                        if str(config.NICKNAME).lower() == main_nick.lower():
                            alt_nick = getattr(config, 'ALT_NICKNAME', f"{main_nick}`")
                            print(f"[LIVE NICK COLLISION] The server reported a genuine collision for {main_nick}. Fallback nick: {alt_nick}")
                            s.send(f"NICK {alt_nick}\r\n".encode())
                            config.NICKNAME = alt_nick

                    # Reclaim the main nick the moment the other client releases it
                    # Anchored twice over. The old test matched " QUIT "/" PART " anywhere,
                    # then looked for ":<mainnick>!" anywhere - and a QUIT reason is free
                    # text. Together with the 433 handler above that was a two-line flood:
                    # forge a 433 to push the bot onto its alt nick, forge a QUIT to pull it
                    # back, repeat. Two unpaced NICK commands per round, from channel text.
                    if is_user_event(line, "QUIT") or is_user_event(line, "PART"):
                        main_nick = getattr(config, 'ORIGINAL_NICK', 'DCCore')
                        if str(config.NICKNAME).lower() != main_nick.lower():
                            if event_source_nick(line) == main_nick.lower():
                                print(f"[NICK RECOVERY] The main nick {main_nick} logged out. Reclaiming it now...")
                                try:
                                    s.send(f"NICK {main_nick}\r\n".encode())
                                    config.NICKNAME = main_nick
                                except Exception as recovery_err:
                                    print(f"[NICK RECOVERY ERROR] Could not reclaim the nick: {recovery_err}")


                    # Anchored: "001" in line matched any message containing those three
                    # digits anywhere, including a perfectly ordinary track request.
                    if not joined and (is_server_numeric(line, "001") or is_server_numeric(line, "376")):
                        joined = True
                        print(f"[INFO] Connected to the server. Waiting 5 seconds to settle before JOIN...")
                        
                        def delayed_join(socket_conn, channels):
                            time.sleep(5)
                            try:
                                socket_conn.send(f"JOIN {channels}\r\n".encode())
                                # An empty fallback rather than a channel name, and then an
                                # actual check: "JOIN \r\n" is a malformed line, and joining
                                # some channel the operator never configured is worse than
                                # joining none at all.
                                debug_chan = str(getattr(config, 'DEBUG_CHANNEL', '') or '').strip()
                                if debug_chan:
                                    socket_conn.send(f"JOIN {debug_chan}\r\n".encode())
                                    print(f"[JOIN] Joined the main channels and the debug channel: {debug_chan}")
                                else:
                                    print("[JOIN] Joined the main channels. No DEBUG_CHANNEL is set, so none was joined.")
                                # NEW (issue #9): start the watchdog HERE, right after the JOIN
                                # has actually been sent, so the timeout starts from the right moment.
                                threading.Thread(target=activation_watchdog, daemon=True).start()
                            except Exception as join_err:
                                print(f"[ERROR] Could not send JOIN: {join_err}")
                                
                        threading.Thread(target=delayed_join, args=(s, config.CHANNEL), daemon=True).start()

                    if joined and not getattr(config, 'activation_triggered', False) and " 366 " in line:
                        # FIXED (issue #9): parses WHICH channel the 366 line refers to instead
                        # of just counting them. Activates only once every real target channel
                        # is confirmed - the debug channel's 366 can no longer mask a broken
                        # huvudkanal.
                        # Anchored to the server prefix: the old unanchored search matched
                        # anywhere in the line, so a user could PRIVMSG " 366 x #chan" and
                        # forge a channel confirmation, activating the bot early.
                        m366 = re.match(r"^:\S+ 366 \S+ ([#\w\-]+)", line)
                        if m366:
                            confirmed_chan = m366.group(1).lower()
                            channels_confirmed.add(confirmed_chan)
                            print(f"[INFO] Received End of NAMES for {confirmed_chan} ({len(channels_confirmed & target_channels)}/{len(target_channels)} target channels confirmed)")
                        
                        if target_channels.issubset(channels_confirmed):
                            config.activation_triggered = True
                            print(f"[INFO] All channels joined successfully! Waiting 5 seconds for settle...")
                            threading.Thread(target=delayed_activate, daemon=True).start()



                    # Anchored: ".* NICK :" let the command sit anywhere, so "hey NICK
                    # :victim" typed in a channel renamed the SPEAKER's send_queue key onto
                    # the victim - and the assignment below overwrites, so it destroyed
                    # whatever that victim had pending.
                    if is_user_event(line, "NICK"):
                        nick_match = re.match(r"^:([^!\s]+)!\S*\s+NICK\s+:?(\S+)", line)
                        if nick_match:
                            old_nick = nick_match.group(1).lower()
                            new_nick = nick_match.group(2).strip()
                            if old_nick in config.send_queue:
                                import queue_mgr
                                queue_mgr.config.send_queue[new_nick.lower()] = queue_mgr.config.send_queue.pop(old_nick)
                            
                    # Anchored: this writes straight into config.whois_status.
                    if is_server_numeric(line, "352"):
                        parts = line.split()
                        if len(parts) > 7:
                            target_nick = parts[7].lower()
                            config.whois_status[target_nick] = True
                    # Anchored: this populates config.channel_users, which dcc.py treats as
                    # proof a user is present when deciding whether to thaw a frozen queue
                    # and dispatch to them. A forged line injected fake presence.
                    if is_server_numeric(line, "353"):
                        name_match = re.search(r" 353 [^#]+([#\w\-]+) :(.+)$", line)
                        if name_match:
                            chan = name_match.group(1).lower()
                            names = [n.strip("@+~&%").lower() for n in name_match.group(2).split()]
                            with runtime.channel_users_lock():
                                if chan not in config.channel_users:
                                    config.channel_users[chan] = set()
                                config.channel_users[chan].update(names)

                            # -------------------------------------------------
                            # RECONNECT THAW - this is what saves the queues:
                            # After a reconnect, everyone already in the channel comes back via
                            # NAMES (353) and NOT via JOIN. Without this, their queues stayed
                            # frozen and were deleted by the 5-minute timer despite never leaving.
                            # -------------------------------------------------
                            thawed_users = [n for n in names if n in getattr(config, 'frozen_queues', {})]
                            for frozen_user in thawed_users:
                                del config.frozen_queues[frozen_user]
                                files_in_q = len(config.dcc_queue.get(frozen_user, []))
                                print(f"[DCC RECONNECT THAW] {frozen_user} was still in {chan} at the NAMES sync. Thawing {files_in_q} file(s).")
                                threading.Thread(target=dcc.check_queue_and_send, args=(s, frozen_user), daemon=True).start()

                            if thawed_users:
                                announce.send_debug(f"Reconnect sync in {chan}: thawed {config.C_BOLD}{len(thawed_users)}{config.C_RESET} queue(s) for users who never left.", category="JOIN")

                    # Anchored: " JOIN " matched the word anywhere, so a PRIVMSG containing
                    # it thawed the speaker's own frozen queue on demand, and let them insert
                    # themselves into config.channel_users for a channel they are not in -
                    # which dcc.py reads as proof of presence before it dispatches.
                    elif is_user_event(line, "JOIN") and event_source_nick(line) != config.NICKNAME.lower():
                        join_match = re.search(r"^:([^!]+)!.* JOIN :?([#\w\-]+)", line)
                        if join_match:
                            joined_user = join_match.group(1)
                            joined_chan = join_match.group(2)
                            j_key = joined_user.lower()
                            
                            with runtime.channel_users_lock():
                                if joined_chan.lower() not in config.channel_users:
                                    config.channel_users[joined_chan.lower()] = set()
                                config.channel_users[joined_chan.lower()].add(j_key)
                            
                            if hasattr(config, 'frozen_queues') and j_key in config.frozen_queues:
                                del config.frozen_queues[j_key]
                                print(f"[DCC REALTIME THAW] {joined_user} rejoined {joined_chan}. Thawing their queue.")
                                files_in_q = len(config.dcc_queue.get(j_key, [])) if hasattr(config, 'dcc_queue') else 0
                                announce.send_debug(f"User {config.C_BOLD}{joined_user}{config.C_RESET} returned to {joined_chan}, continuing queue of {config.C_BOLD}{files_in_q}{config.C_RESET} file(s)", category="JOIN")
                                threading.Thread(target=dcc.check_queue_and_send, args=(s, joined_user), daemon=True).start()
                                
                    # Anchored: as JOIN. This one removes people from channel_users, which
                    # freezes their queue and starts the five-minute delete timer.
                    elif is_user_event(line, "PART"):
                        part_match = re.search(r"^:([^!]+)!.* PART ([#\w\-]+)", line)
                        if part_match:
                            p_user = part_match.group(1).lower()
                            p_chan = part_match.group(2).lower()
                            with runtime.channel_users_lock():
                                if p_chan in config.channel_users and p_user in config.channel_users[p_chan]:
                                    config.channel_users[p_chan].remove(p_user)

                    # Anchored: the worst of the three, because it removes the user from
                    # EVERY channel at once. "@find QUIT PLAYING GAMES" is an ordinary
                    # search that silently cost the searcher their whole queue five minutes
                    # later.
                    elif is_user_event(line, "QUIT"):
                        quit_match = re.search(r"^:([^!]+)!", line)
                        if quit_match:
                            q_user = quit_match.group(1).lower()
                            with runtime.channel_users_lock():
                                for chan in config.channel_users:
                                    if q_user in config.channel_users[chan]:
                                        config.channel_users[chan].remove(q_user)

                    # Cross-bot search broadcast capture, NOTICE half. NOTICE
                    # lines are not parsed anywhere else in this loop - many
                    # file-sharing bots reply to @find via NOTICE rather than
                    # PRIVMSG, so without this branch every such reply would be
                    # silently dropped exactly like a PRIVMSG-to-self used to
                    # be before the branch below existed. Read-only: this never
                    # dispatches a command, so it needs none of the PRIVMSG
                    # branch's ban/flood/dispatch machinery.
                    #
                    # See parse_notice()'s own docstring (top of this file)
                    # for why the anchoring matters.
                    notice_parsed = parse_notice(line)
                    if notice_parsed:
                        notice_user, notice_target, notice_text = notice_parsed
                        notice_text = notice_text.strip()
                        if notice_user.lower() != config.NICKNAME.lower():
                            _capture_broadcast_search_reply(
                                notice_user, notice_target, notice_text)
                            if notice_target.lower() == config.NICKNAME.lower():
                                # A private NOTICE addressed to us, from
                                # another bot - the shape a foreign bot's own
                                # "!rar is disabled here" refusal takes (see
                                # dcc_fetch.handle_refusal_notice()'s own
                                # docstring). Read-only here too: this only
                                # ever fails a "folder" row that would
                                # otherwise sit "offered" for the full
                                # FETCH_FOLDER_OFFER_TIMEOUT against one of
                                # only MAX_FETCH_SLOTS fetch slots.
                                import dcc_fetch
                                dcc_fetch.handle_refusal_notice(notice_user, notice_text)

                    # See parse_privmsg()'s own docstring (top of this file)
                    # for why the anchoring matters, and what user_host is
                    # for (security.check_user_status()'s hostmask-pattern
                    # matching, a few lines below).
                    privmsg_parsed = parse_privmsg(line)
                    if privmsg_parsed:
                        user, user_host, target_chan, msg = privmsg_parsed
                        msg = msg.strip()
                        if user.lower() == config.NICKNAME.lower():
                            continue

                        # Cross-bot search broadcast capture, PRIVMSG half (see
                        # the NOTICE branch above and _capture_broadcast_search_reply()'s
                        # own docstring). Placed before the ban check on
                        # purpose: a foreign bot answering a broadcast we asked
                        # for is not subject to OUR channel's ban list, and
                        # this never dispatches anything of its own for a ban
                        # to meaningfully gate.
                        _capture_broadcast_search_reply(user, target_chan, msg)

                        # Bot registry, same placement and the same reasoning:
                        # observational, dispatches nothing, and a foreign bot
                        # advertising in a channel we sit in is not subject to
                        # our ban list.
                        _capture_channel_advert(user, target_chan, msg)

                        if not security.check_user_status(user, hostmask=user_host):
                            continue
                            
                        msg_lower = msg.lower()
                        bot_aliases = get_bot_aliases()
                        # FIXED (issue #35): the gate previously covered only 4 of the ~11
                        # dispatch paths below, so -que, -remove, both CTCP variants, !list,
                        # !debugnames and !ping could each spawn a thread or send a NOTICE
                        # for ANY user with no rate limit at all. Admin commands (!ban,
                        # !unban, !rehash, !update, !clearqueue) are deliberately left out:
                        # each self-checks against ADMIN_NICK in its own handler already, so
                        # gating them here would only meter the operator against themselves.
                        #
                        # FIXED (#219): a private CTCP DCC SEND - the cross-bot fetch reply
                        # path below - was not in this set either. Its own admission control
                        # only decides whether an OFFER is one we solicited; deciding that
                        # costs a thread and a fetch-queue scan per message regardless, and
                        # nothing bounded how many any single, non-banned user could send.
                        # Self-contained, like the QUE/REMOVE clause above it: this whole
                        # expression is lifted out of this file's source text and evaluated
                        # by tests/test_irc_dispatch.py, so a local computed outside it (a
                        # `ctcp_upper = ...` line before this block, tried first) is invisible
                        # to that harness and breaks it with a NameError.
                        is_bot_command = (
                            msg_lower == f"@{config.NICKNAME.lower()}"
                            or msg_lower == f"@{config.NICKNAME.lower()}-help"
                            or msg_lower == f"@{config.NICKNAME.lower()}-stats"
                            or msg_lower == f"@{config.NICKNAME.lower()}-top"
                            or msg_lower == f"@{config.NICKNAME.lower()}-que"
                            or msg_lower == f"@{config.NICKNAME.lower()}-remove"
                            or msg.startswith("@find ")
                            or msg.startswith("@locator ")
                            or any(msg_lower.startswith(f"!{alias} ") for alias in bot_aliases)
                            or msg_lower in ("!list", "!debugnames", "!ping")
                            or (msg.startswith("\x01") and msg.strip("\x01").strip().upper() in ("QUE", "REMOVE"))
                            or (msg.startswith("\x01")
                                and msg.strip("\x01").strip().upper().startswith("DCC SEND ")
                                and target_chan.lower() == config.NICKNAME.lower())
                        )
                        if is_bot_command and security.is_flooding(user):
                            continue 
                            
                        try:
                            import commands
                            import db
                            db.check_and_rotate_day()
                            
                            if msg.startswith("\x01") and msg.endswith("\x01"):
                                ctcp_cmd = msg.strip("\x01").strip().upper()
                                # Admin console. Only ever from a private message -
                                # a DCC CHAT offer addressed to a channel is
                                # meaningless, and accepting one would mean acting
                                # on a line every user in that channel can send.
                                if (ctcp_cmd.startswith("DCC CHAT ")
                                        and target_chan.lower() == config.NICKNAME.lower()):
                                    import adminchat
                                    adminchat.handle_dcc_chat(
                                        s, line, user, msg.strip("\x01").strip())
                                    continue
                                # Cross-bot file fetch: a DCC SEND offer answering
                                # a fetch WE requested (see dcc_fetch.py). Private
                                # only, same reasoning as DCC CHAT above - a DCC
                                # SEND offer addressed to a channel is meaningless
                                # and would mean acting on a line any channel
                                # member could send. Admission control lives
                                # entirely inside handle_incoming_offer(): it only
                                # proceeds if this matches a row we ourselves
                                # marked 'offered' a moment ago, so an unsolicited
                                # offer from anyone is dropped there, not here.
                                if (ctcp_cmd.startswith("DCC SEND ")
                                        and target_chan.lower() == config.NICKNAME.lower()):
                                    import dcc_fetch
                                    threading.Thread(
                                        target=dcc_fetch.handle_incoming_offer,
                                        args=(s, user, msg.strip("\x01").strip()),
                                        daemon=True).start()
                                    continue
                                if ctcp_cmd == "QUE":
                                    threading.Thread(target=commands.handle_queue_check, args=(s, user, target_chan), daemon=True).start()
                                    continue
                                elif ctcp_cmd == "REMOVE":
                                    threading.Thread(target=commands.handle_queue_remove, args=(s, user, target_chan), daemon=True).start()
                                    continue
                            elif msg_lower == f"@{config.NICKNAME.lower()}":
                                threading.Thread(target=list.send_file_list, args=(s, user, target_chan),
                                                 daemon=True).start()
                            elif msg_lower == f"@{config.NICKNAME.lower()}-help":
                                threading.Thread(target=commands.handle_help_request, args=(s, user, target_chan), daemon=True).start()
                                continue
                            elif msg_lower == f"@{config.NICKNAME.lower()}-stats":
                                threading.Thread(target=commands.handle_stats_request, args=(s, user, target_chan), daemon=True).start()
                                continue
                            elif msg_lower == f"@{config.NICKNAME.lower()}-top":
                                threading.Thread(target=commands.handle_top_request, args=(s, user, target_chan), daemon=True).start()
                                continue
                            elif msg_lower == f"@{config.NICKNAME.lower()}-que":
                                threading.Thread(target=commands.handle_queue_check, args=(s, user, target_chan), daemon=True).start()
                                continue
                            elif msg_lower == f"@{config.NICKNAME.lower()}-remove":
                                threading.Thread(target=commands.handle_queue_remove, args=(s, user, target_chan), daemon=True).start()
                                continue
                            elif msg.startswith("@find ") or msg.startswith("@locator "):
                                parts = msg.split(" ", 1)
                                if len(parts) > 1:
                                    search_term = parts[1].strip()
                                    if search_term:
                                        threading.Thread(target=list.execute_search, args=(s, user, search_term, target_chan), daemon=True).start()
                            elif msg_lower == "!list":
                                list.send_list_trigger_info(s, user)
                            elif msg.lower() == "!debugnames":
                                with runtime.channel_users_lock():
                                    have_count = hasattr(config, 'channel_users') and target_chan.lower() in config.channel_users
                                    if have_count:
                                        current_qty = len(config.channel_users[target_chan.lower()])
                                if have_count:
                                    s.send(f"NOTICE {user} :[RAM-CHECK] Currently tracking {current_qty} user(s) live via 353-numeric in {target_chan}.\r\n".encode())
                                else:
                                    s.send(f"NOTICE {user} :[RAM-CHECK] Critical: No 353 names loaded yet for {target_chan} in config structure.\r\n".encode())
                            elif msg.lower() == "!ping":
                                threading.Thread(target=commands.handle_ping_request, args=(s, user, target_chan), daemon=True).start()
                            # Admin commands in channel. ADMIN_CHANNEL_COMMANDS retires these
                            # once the DCC console is trusted; the console reaches the same
                            # handlers with authorised=True. User commands are unaffected.
                            elif (getattr(config, 'ADMIN_CHANNEL_COMMANDS', True)
                                  and (msg.lower() in ('!rehash', '!update')
                                       or msg.startswith('!ban ') or msg.startswith('!unban ')
                                       or msg_lower == '!clearqueue'
                                       or msg_lower.startswith('!clearqueue '))):
                                if msg.lower() == "!rehash":
                                    threading.Thread(target=commands.handle_rehash_request, args=(user, target_chan), daemon=True).start()
                                elif msg.startswith("!ban "):
                                    threading.Thread(target=commands.handle_hard_ban_request, args=(user, target_chan, msg), daemon=True).start()
                                elif msg.startswith("!unban "):
                                    threading.Thread(target=commands.handle_hard_unban_request, args=(user, target_chan, msg), daemon=True).start()
                                elif msg.lower() == "!update":
                                    threading.Thread(target=commands.handle_list_update_request, args=(user, target_chan), daemon=True).start()
                                elif msg_lower == "!clearqueue" or msg_lower.startswith("!clearqueue "):
                                    # commands.handle_admin_clear_queue was added in #16 but never
                                    # wired into this dispatch chain, so the command had no caller
                                    # anywhere and typing it did nothing at all. The handler does
                                    # its own admin check.
                                    threading.Thread(target=commands.handle_admin_clear_queue, args=(user, target_chan, msg), daemon=True).start()
                            elif any(msg_lower.startswith(f"!{alias} ") for alias in bot_aliases):
                                # Split on the first space only, so "!DCCore !rar Artist/Album"
                                # still hands "!rar Artist/Album" to the download handler.
                                parts = msg.split(" ", 1)
                                if len(parts) > 1:
                                    requested_file = parts[1].strip()
                                    threading.Thread(target=dcc.handle_download_request,
                                                    args=(s, user, requested_file, target_chan),
                                                    daemon=True).start()
                                    
                        except Exception as cmd_err:
                            print(f"[ERROR] Error handling a bot command from {user}: {cmd_err}")

            except Exception as inner_loop_err:
                print(f"[IRC INTERNAL ERROR] Unexpected error inside the message loop: {inner_loop_err}")
                try: s.close()
                except: pass
                _release_socket()
                break

        # Reset every flag before the next pass through the reconnect loop
        print("[CONNECT] Lost the connection. Reconnecting to the IRC server in 10 seconds...")
        config.bot_joined_channel = False
        
        # FIXED: clears the in-memory channel lists on a crash, so the bot does not block its own nick next time
        with runtime.channel_users_lock():
            if hasattr(config, 'channel_users') and isinstance(config.channel_users, dict):
                config.channel_users.clear()
            
        config.activation_triggered = False
        # Invalidate every thread spawned for the connection that just died.
        config.connection_epoch += 1
        oserve_mod = sys.modules.get('oserve')
        if oserve_mod:
            oserve_mod.bot_joined_channel = False
        _release_socket()
        announce.is_ready = False
        import queue_mgr
        if "channel_announce" in queue_mgr.config.send_queue:
            queue_mgr.config.send_queue["channel_announce"] = []
        time.sleep(10)
