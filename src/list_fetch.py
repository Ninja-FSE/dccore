# list_fetch.py - safe extraction and parsing of ANOTHER bot's fetched
# master-list zip. Same "we are not the trusted party here" posture as
# dcc_fetch.py's own module docstring, one layer further in: by the time
# process_fetched_list_zip() below is called, dcc_fetch.py has already
# received a complete, size-verified zip file from a third-party bot into
# FETCHED_FILES_DIR - but a zip file is a container, and nothing about a
# successful byte-for-byte transfer proves anything about what is safe to do
# with its CONTENTS. That is what this module is responsible for, and
# nothing else:
#
#   * "zip slip" (path traversal): a member whose name is something like
#     "../../../etc/cron.d/evil" or an absolute path, which - if extracted
#     naively - writes OUTSIDE the intended extraction directory. Every
#     member's resolved destination is validated against dcc.is_safe_path()
#     (the same containment check used elsewhere in this codebase for exactly
#     this class of problem) BEFORE anything is extracted; the whole archive
#     is rejected, not just the offending member, and nothing is written for
#     a rejected archive.
#   * zip bombs: a small file that decompresses to an enormous one. Guarded
#     before touching disk, by summing the zip's own declared uncompressed
#     sizes against MAX_FETCH_FILE_SIZE (the same ceiling dcc_fetch.py
#     applies to a raw DCC SEND offer) - a zip that under-declares a
#     member's size to sneak past this sum still cannot over-deliver at
#     extraction time, because Python's own zipfile.ZipExtFile already
#     truncates every read to that member's declared file_size regardless of
#     its real compressed payload (see _extract_member()'s docstring for the
#     detail). _extract_member() also tracks a running byte budget as it
#     copies, but given that stdlib truncation, that tracking is inert
#     defense-in-depth today, not an independently load-bearing second
#     barrier - the pre-check above is what actually makes this safe.
#   * a zip with an implausible number of entries - a real master-list
#     archive (see update_list.py) contains at most two files (the master
#     list and the RAR/album-folder list); MAX_LIST_ZIP_ENTRIES rejects
#     anything wildly beyond that shape outright, before either guard above
#     even runs.
#
# Not every list is a zip. A RAR list is opened with the rar program under
# these same guards (see "RAR lists (#1200)" below), a plain .txt list is
# taken as it is once it looks like one (_accept_plain_text_list()), and
# anything else - a 7z, binary data - is refused, and the list already held
# for that bot stays as it was (_hold_existing_list()).
#
# Extraction lands in a per-bot subdirectory - <FETCHED_FILES_DIR>/lists/<bot
# nick>/ - deliberately never alongside dcc_fetch.py's own raw fetched files:
# those are "a file I fetched to listen to", this is "a list archive I
# extracted to browse", and conflating the two on disk would make it easy to
# mistake one for the other later.
#
# Parsing reuses list.py's existing pipeline (find_matching_entries() with
# its new `list_path` parameter, which reuses _split_entry_line() and
# strip_info_suffix() under the hood) rather than writing a second parser -
# the same "::INFO::" tolerance this project already added for OTHER bots'
# formatting variance (see strip_info_suffix()'s own docstring) applies here
# automatically, for free.
import hashlib
import io
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

import defaults as config
import db
import dcc
import platform_compat
import runtime
import list as list_mod
import list_index

# A real master-list zip (update_list.py's generate_master_list()) contains
# at most two files. A few hundred is a generous ceiling that still rejects
# anything shaped like an attempt to smuggle a large number of small files
# past the total-size guard below (many tiny files can add up to a large
# total while each individually looking innocuous) - a module constant, not a
# config.py tunable, same reasoning as webserver.WEBUI_MAX_SEARCH_RESULTS:
# this is an internal safety bound, not an operator-facing knob.
MAX_LIST_ZIP_ENTRIES = 300

# Issue #76: every guard up to this point counts bytes or zip members - none
# of them counts LINES, and every "!" line in the extracted text becomes a
# permanently-retained dict once parsed. Checked on the EXTRACTED file's real
# size on disk, before it is parsed - not the zip's declared/compressed size,
# which is exactly what let a small download expand into hundreds of megabytes
# of retained rows in the first place.
#
# THE FIRST NUMBER WAS WRONG, and wrong in the way a guess about other
# people's data usually is. It was 20MB, reasoned as "5x headroom over the
# largest real list anyone here has actually seen" - that list being this
# operator's own 4MB one, from a 1.21TB/47,420-file library. Three lists in
# one channel then arrived at 25.7MB, 26.8MB and 31.5MB and were all refused,
# which is not a guard doing its job; it is a guard set from a sample of one.
#
# A FLAC library with long filenames produces a far bigger text list than a
# similarly sized MP3 one, and the ceiling has to hold for libraries this
# operator will never see. 128MB is four times the largest observed, and at
# roughly 80 bytes a row that is ~1.6M rows - inside the four million the
# cross-list index is measured against, so it is a size this project already
# knows it can hold.
#
# A SETTING rather than a constant now, which reverses the earlier reasoning
# deliberately. "An internal safety bound, not an operator-facing knob" holds
# when the right value is knowable here. It is not: it depends on the
# libraries of bots in somebody else's channel, and the last time this was
# fixed at a number it cost three real lists silently.
DEFAULT_MAX_LIST_TEXT_SIZE = 128 * 1024 * 1024


def max_list_text_size():
    """The ceiling, read through config so an operator can raise it.

    Resolved per call rather than captured at import, for the same reason
    every other path in this project is: !rehash reloads config.
    """
    try:
        value = int(getattr(config, "MAX_LIST_TEXT_SIZE",
                            DEFAULT_MAX_LIST_TEXT_SIZE))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LIST_TEXT_SIZE
    return value if value > 0 else DEFAULT_MAX_LIST_TEXT_SIZE

_COPY_CHUNK = 65536

# How much of a plain-text list is read to decide it IS one. A real list
# reaches its first request line within a header plus a banner, and the
# banner is capped at 8KB, so this is comfortable headroom over the point
# the answer is knowable.
_PLAUSIBLE_LIST_PREFIX = 64 * 1024

# Control characters a text list never holds (#1200). Left out of the set:
# tab, the line ends, vertical tab and form feed, DOS's end-of-file mark
# (\x1a), and IRC's formatting codes (bold, colour, reset, monospace,
# reverse, italic, strikethrough, underline), which a banner copied out of
# an IRC client can carry. Random bytes are about 7% these; text is none.
_BINARY_CONTROL_BYTES = bytes(
    code for code in range(32)
    if code not in b"\t\n\r\x0b\x0c\x1a\x02\x03\x0f\x11\x16\x1d\x1e\x1f")

# What a list archive DCCore cannot use as plain text starts with. RAR is
# "Rar!\x1a\x07" for both RAR4 (then \x00) and RAR5 (then \x01\x00); 7z is
# its own six bytes.
_RAR_SIGNATURE = b"Rar!\x1a\x07"
_SEVEN_ZIP_SIGNATURE = b"7z\xbc\xaf\x27\x1c"

# How long one rar child may run while a fetched RAR list is read: listing
# it, or unpacking one member. A list of the biggest size allowed unpacks in
# seconds; a rar that is still going after this is hung, and a hung child
# here would hold the fetch lock that every other list fetch waits on.
_RAR_LIST_TIMEOUT = 300

# The longest line of rar's listing read in one go. RAR caps a name at 2048
# bytes; a "line" longer than this is not a listing this code knows.
_RAR_LISTING_LINE_LIMIT = 16384


_FALLBACK_LOCK = threading.Lock()


def _lock():
    """The dedicated lock oserve.py allocates at startup for
    config.fetched_bot_lists, or a module-level fallback - same idiom as
    dcc_fetch._fetch_lock(), needed so tests (and any other caller that never
    ran oserve.startup()) still have something to synchronise on.

    The fallback is allocated once, at import, rather than per call: returning
    a fresh Lock() each time would hand every caller a different object and so
    would serialise nothing at all."""
    return getattr(config, "fetched_bot_lists_lock", None) or _FALLBACK_LOCK


def _ensure_fetched_bot_lists():
    # config.fetched_bot_lists is bound from runtime.py at import time and
    # always exists as a real dict - never rebind it here, see runtime.py's
    # docstring.
    return config.fetched_bot_lists


# What may appear in a directory name built from a bot's nick. Mirrors
# dcc_fetch._FILENAME_CHARSET_RE, minus the space and parentheses a filename
# needs: a nick has neither, and a directory name with fewer moving parts is
# easier to recognise in a file manager.
_BOT_DIR_CHARSET_RE = re.compile(r'[^\w\-\.\[\]{}^`]')


def _advert_snapshot(bot):
    """What `bot` is advertising right now, as far as we have seen.

    Only the fields that bot actually published. A missing key means "this bot
    did not say", never zero - the rule irc.parse_channel_advert() already
    follows, and the one that keeps a bot which publishes no date from being
    permanently marked stale against an invented one.

    {} when we have never seen an advert from them, which is an ordinary state:
    a list can be fetched from a bot whose advert has not come round yet.
    """
    entry = dict(runtime.known_bots.get(str(bot).strip().lower()) or {})
    snapshot = {}
    for field in ("files", "list_date"):
        value = entry.get(field)
        if value not in (None, "", 0):
            snapshot[field] = value
    return snapshot


def _sanitize_bot_dir_name(bot):
    """Never trust a bot nick as a literal path component either - the same
    discipline dcc_fetch._sanitize_offer_filename() applies to a filename,
    applied here to what becomes a directory name instead.

    A WHITELIST, which is what that claim always meant and what this was not.
    It used to strip a blacklist - NUL, the two separators, ".." and
    surrounding dots - and pass everything else through. But "|" is a perfectly
    ordinary IRC nick character (RFC 2812's specials are []\\`_^{|}, and
    "Bot|Away" is one of the commonest nick shapes on the network) and is
    ILLEGAL in a Windows path.

    So os.makedirs() on the extraction directory failed with WinError 123 -
    AFTER the zip had already been fetched over DCC. The transfer worked, the
    bytes were on disk, and the fetch failed at the last step, every time, for
    that bot. Found by audit.

    Same charset as dcc_fetch, for the same reason: what is legal in a nick and
    what is legal in a path are different sets, and only one of them is ours to
    choose.
    """
    name = list_mod.strip_control_codes(str(bot))
    name = name.replace('\x00', '')
    name = name.replace('/', '_').replace('\\', '_')
    name = name.replace('..', '')
    name = _BOT_DIR_CHARSET_RE.sub('_', name)
    name = name.strip().strip('.').strip()
    # A Windows-reserved nick and one already spelled with windows_safe_
    # name()'s own "_" suffix collide (#1249 review): "AUX" and "AUX_" both
    # become "AUX_", one directory for two different bots' held lists.
    # Lower-cased before hashing, since every caller lower-cases the result
    # anyway (list_extract_dir() and friends) - two spellings of the same
    # nick, "AUX" and "Aux", must still land on one directory, not two.
    if platform_compat.is_windows_reserved(name):
        digest = hashlib.sha1(name.strip().lower().encode("utf-8", "replace")).hexdigest()[:6]
        name = f"{name}-{digest}"
    return platform_compat.windows_safe_name(name) or "unknown_bot"


def _current_channel_signature(bot, channel):
    """A snapshot of what `channel` is advertising for `bot` RIGHT NOW
    (#1240) - the same shape irc._record_channel_signature() stores,
    {"files", "list_date", "last_seen", "since"} (whichever of the first two
    are known). Stamped onto a marker as "advert_signature" when it is
    fetched, so a later comparison (list_grab._secondary_channel_candidates())
    can tell a channel's list has moved on since WITHOUT needing a second,
    separate freshness mechanism - the same signature this already is.
    """
    channel = str(channel or "").strip().lower()
    if not channel:
        return {}
    registry = (getattr(config, "known_bots", {}) or {}).get(str(bot).strip().lower())
    channels = registry.get("channels") if isinstance(registry, dict) else None
    signature = channels.get(channel) if isinstance(channels, dict) else None
    return dict(signature) if isinstance(signature, dict) else {}


def _channel_marker_name(channel):
    """A channel turned into something usable as a list marker (#1240):
    "#video" becomes "video". Never empty - list_marker()'s own rule
    that an empty marker means "the main list" must never apply to a channel
    name by accident, so a channel that somehow sanitises to nothing falls
    back to a fixed word instead.
    """
    name = str(channel or "").strip().lstrip("#")
    name = _BOT_DIR_CHARSET_RE.sub("_", name)
    return name.strip("-_ .") or "channel"


def _disambiguate_marker(candidate, channel, reserved_lower):
    """`candidate`, unchanged unless it collides - compared case-
    insensitively - with a marker name `reserved_lower` already lists
    (#1240 review). The common case, no collision, returns it untouched.

    Confirmed on review: "#RAR" sanitises to the exact name a filename-
    derived "RAR" marker already uses (`kept_lists.update(fresh)` would
    silently replace the primary's own RAR list); "#rar" and "#RAR" differ
    only by case, which the dict tolerates as two distinct keys but
    index_bot_list() folds to the same search-index entry regardless; "#a|b"
    and "#a_b" both sanitise to "a_b" outright, a flat dict-key collision
    that would let the second channel's fetch silently overwrite the
    first's.

    The suffix is a short hash of `channel` itself, not of `candidate` - two
    different channels that collide on the same sanitised name need two
    DIFFERENT suffixes to stop colliding with EACH OTHER, which hashing the
    (identical, by definition of a collision) candidate string could never
    produce - and the same channel must always land on the same
    disambiguated name across repeated fetches, which a running counter
    could not promise (its result would depend on fetch order).
    """
    if candidate.lower() not in reserved_lower:
        return candidate
    digest = hashlib.sha1(str(channel).strip().lower().encode("utf-8", "replace")).hexdigest()
    return f"{candidate}-{digest[:6]}"


def secondary_channel_extract_dir(bot, channel):
    """Where a SECONDARY channel's fetch for `bot` extracts to (#1240): a
    directory OUTSIDE the bot's own (list_extract_dir()), never a
    subdirectory of it.

    A real incident, confirmed on review: an earlier version of this put it
    at <bot's own dir>/_channels/<chan> - inside the exact directory
    _hold_existing_list()/_release_held_list() rename and rmtree whole on
    every ordinary refresh of the PRIMARY channel. The marker and its index
    rows survived (they live in config.fetched_bot_lists, untouched by an
    ordinary refresh - see _install_fetched_list()), but the files a
    secondary channel's fetch had just extracted did not: a ordinary refresh
    of the primary deleted them outright, leaving a marker that pointed at
    nothing. Sibling to list_extract_dir(bot), not nested in it, so nothing
    that ever touches the primary's own directory can reach this one.
    """
    base = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))
    channels_root = os.path.join(base, "lists", "_channels")
    candidate = os.path.join(channels_root, _sanitize_bot_dir_name(bot).lower(),
                             _channel_marker_name(channel).lower())
    if not dcc.is_safe_path(channels_root, candidate):
        candidate = os.path.join(channels_root, "unknown_bot", "channel")
    return candidate


def _extract_dir_for(bot, channel, secondary=False):
    """Which directory THIS fetch extracts into - list_extract_dir(bot) for
    an ordinary/primary fetch, secondary_channel_extract_dir() for a
    CONFIRMED secondary one (#1240) so it can never collide with - or share
    a directory tree that gets rmtree'd alongside - what the primary
    channel's lists already point at.

    `secondary` is explicit, passed down from whichever caller already knows
    which kind of fetch this is (see new_fetch_row()'s docstring in
    dcc_fetch.py) - never inferred here from `channel` alone. An earlier
    version guessed "secondary" from a channel mismatch against whatever was
    already on record; a bot held from before #1232 has no channel on
    record at all, so every ordinary refresh of it looked like a mismatch
    and was treated as secondary forever, a real incident on the live bot.
    """
    if secondary:
        return secondary_channel_extract_dir(bot, channel)
    return list_extract_dir(bot)


def list_extract_dir(bot):
    """Where `bot`'s fetched list gets extracted to:
    <FETCHED_FILES_DIR>/lists/<sanitised bot nick, lowercased>/.
    """
    base = os.path.abspath(getattr(config, "FETCHED_FILES_DIR", "./data/fetched"))
    lists_root = os.path.join(base, "lists")
    candidate = os.path.join(lists_root, _sanitize_bot_dir_name(bot).lower())
    if not dcc.is_safe_path(lists_root, candidate):
        # Defense-in-depth, expected to be unreachable given the sanitiser
        # above already strips "/", "\\" and "..": fall back to a fixed,
        # definitely-safe name rather than ever extracting somewhere
        # unintended.
        candidate = os.path.join(lists_root, "unknown_bot")
    return candidate


def _fetch_file_size_budget():
    """MAX_FETCH_FILE_SIZE, resolved the way dcc_fetch.py's own admission
    check already reads it: 0 means no limit (#302), not a real zero-byte
    ceiling. Read here rather than left as the raw setting because this
    module uses the value twice - once as the zip-bomb sum cap in
    _validate_zip_members(), once as the running extraction budget in
    process_fetched_list_zip() - and a `budget` that starts at a literal 0
    would fail the very first byte written, the same bug either call site
    would have on its own (#937 was the sibling of this one, on
    MAX_FETCH_LIST_FILE_SIZE).

    BUT 0 IS NOT "NO BOUND" FOR A LIST ARCHIVE (#945). Here the value is the
    zip-bomb guard: _validate_zip_members()'s sum of declared sizes is the
    only thing bounding extraction (ZipExtFile truncates each member to what
    it declares, and max_list_text_size() is checked after extraction). #940
    returned float("inf"), so with the setting at 0 a small zip of zeros
    unpacked without limit. An operator lifting the cap on FILES is not
    asking for that. So 0 falls back to what real lists can hold -
    max_list_text_size() per list, MAX_LISTS_PER_ARCHIVE of them: any list
    bigger than that is refused after extraction anyway, so this refuses
    nothing that would have been kept."""
    raw = int(getattr(config, "MAX_FETCH_FILE_SIZE", 200 * 1024 * 1024))
    if raw > 0:
        return raw
    return max_list_text_size() * MAX_LISTS_PER_ARCHIVE


def _fetch_file_size_budget_name():
    """Which ceiling _fetch_file_size_budget() applied, for the rejection
    message - "MAX_FETCH_FILE_SIZE (0 bytes)" named a limit that is not
    the one refusing the list."""
    if int(getattr(config, "MAX_FETCH_FILE_SIZE", 200 * 1024 * 1024)) > 0:
        return "MAX_FETCH_FILE_SIZE"
    return f"the list archive ceiling (MAX_LIST_TEXT_SIZE x {MAX_LISTS_PER_ARCHIVE})"


def _member_parts(filename):
    """The path components a list-archive member is written under.

    The archive's own separators and "." dropped, and every component made
    into a name Windows can create (platform_compat.windows_safe_name()). A
    peer chooses these names, and on Windows os.path.abspath() turns a
    component like "NUL" - or, before Windows 11, "COM1" or "con.txt" - into
    the device itself ("\\\\.\\NUL"), which long_path() then makes a UNC
    path: the write went to a device, and a serial port could hold the fetch
    thread. "NUL" becomes "NUL_", as everywhere else a peer's name is used;
    _pick_list_file() looks for the list by what is in the folder, not by the
    member's name, so nothing is lost.
    """
    parts = [p for p in str(filename).replace("\\", "/").split("/")
             if p not in ("", ".")]
    return [platform_compat.windows_safe_name(p) or "_" for p in parts]


def _validate_zip_members(infolist, extract_dir, kind="zip"):
    """Check EVERY member before anything is extracted. Returns a short
    rejection reason string, or None if the whole archive is clear to
    extract. Never partial: the caller only proceeds if this returns None.

    `kind` names the archive in the reason. A RAR list (#1200) is held to
    these same guards, through members shaped like ZipInfo - see _RarMember.
    """
    if not infolist:
        return f"{kind} archive is empty"
    if len(infolist) > MAX_LIST_ZIP_ENTRIES:
        return (f"{kind} contains {len(infolist)} entries, more than the "
                f"{MAX_LIST_ZIP_ENTRIES} a real master-list archive should "
                f"ever need (zip-bomb-shaped guard)")

    max_total = _fetch_file_size_budget()
    total_uncompressed = 0
    for info in infolist:
        if info.is_dir():
            continue
        total_uncompressed += info.file_size
        if total_uncompressed > max_total:
            return (f"{kind}'s declared total uncompressed size exceeds "
                     f"{_fetch_file_size_budget_name()} ({max_total} bytes) - "
                     f"refusing to extract (zip-bomb guard)")

        member_name = info.filename.replace('\\', '/')
        # An absolute path (POSIX "/etc/..." or a Windows drive letter like
        # "C:/...") smuggled into a zip entry name - checked explicitly and
        # BEFORE the join below, rather than relying on is_safe_path() to
        # catch every possible form of it after the fact.
        if member_name.startswith('/') or (len(member_name) > 1 and member_name[1] == ':'):
            return f"{kind} entry {info.filename!r} has an absolute path"

        parts = [p for p in member_name.split('/') if p not in ('', '.')]
        if not parts:
            continue

        # Both the name as sent and the name written (_member_parts()) must
        # stay inside: the renaming trims dots, so only the first still
        # shows a ".." for what it is.
        dest_path = os.path.join(extract_dir, *parts)
        written_path = os.path.join(extract_dir, *_member_parts(info.filename))
        if not (dcc.is_safe_path(extract_dir, dest_path)
                and dcc.is_safe_path(extract_dir, written_path)):
            return (f"{kind} entry {info.filename!r} would extract outside the "
                     f"target directory (path traversal / zip-slip)")

        # A component made only of dots - "..", "...", "...." and so on.
        #
        # ".." is caught by is_safe_path() below, because it genuinely
        # resolves outside. Longer runs are NOT: "...." is a legal directory
        # name that resolves INSIDE the target, so the containment check
        # passes it, correctly.
        #
        # The problem is what Win32 does with it afterwards. Trailing dots are
        # stripped during path parsing, so "<extract>/...." resolves to
        # "<extract>" itself - a path that names a child but operates on the
        # parent. Extraction then fails, and every later attempt to prepare
        # that directory fails too:
        #
        #   [WinError 145] The directory is not empty: ...\lists\<bot>\....
        #
        # so one hostile archive permanently disables list fetching from that
        # bot until somebody deletes it by hand. An extended-length "\\?\\"
        # path does not rescue the cleanup either - it returns
        # ERROR_INVALID_NAME. Refusing the name is the fix.
        #
        # Nothing legitimate is lost: no master-list archive has a member whose
        # directory is called "....".
        for part in parts:
            if set(part) == {'.'}:
                return (f"{kind} entry {info.filename!r} has a path component "
                         f"made only of dots ({part!r})")

    return None


def _extract_member(zf, info, dest_path, budget):
    """Copy one zip member to `dest_path`, tracking bytes written against
    `budget` (the remaining slice of MAX_FETCH_FILE_SIZE after every earlier
    member in this archive) and raising ValueError if it is ever exceeded,
    which the caller treats as a hard abort of the whole archive.

    In practice this loop cannot actually observe more than `info.file_size`
    bytes per member: `zf.open(info)` returns a stdlib `ZipExtFile`, whose
    own `read()`/`_read1()` already truncates to the entry's declared
    `file_size` internally, regardless of how much compressed data the entry
    actually contains - so a member that lies about its size (over- or
    under-declaring) can never make this loop copy more than it declared.
    The real protection against a size-lying entry is
    _validate_zip_members()'s caller summing every declared file_size against
    MAX_FETCH_FILE_SIZE BEFORE any bytes are copied (see
    process_fetched_list_zip() below) - that pre-check, combined with this
    stdlib truncation behaviour, is what actually makes a zip bomb via a
    false declared size impossible here. This function's own `written >
    budget` check is therefore inert defense-in-depth against a
    hypothetical future change to how this module reads zip members (e.g.
    reading raw compressed bytes instead of through `ZipExtFile`), not an
    independently-necessary second barrier today - it is kept because it is
    cheap and correct, not because it currently catches anything the
    pre-check doesn't already rule out. Returns the number of bytes written.
    """
    written = 0
    # Every path below goes through platform_compat.long_path(), the same
    # way dcc.py wraps each path it touches. A zip member name is chosen by
    # the remote bot and is never truncated, so a perfectly legal 240-
    # character name pushes the destination past Windows' 260-character
    # MAX_PATH and the write fails with "No such file or directory" - for a
    # file this code is itself trying to create.
    os.makedirs(platform_compat.long_path(os.path.dirname(dest_path)),
                exist_ok=True)
    long_dest = platform_compat.long_path(dest_path)
    with zf.open(info) as src, open(long_dest, "wb") as dst:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > budget:
                raise ValueError(
                    "zip entry decompressed past its declared-size budget "
                    "(zip-bomb guard tripped during extraction)")
            dst.write(chunk)
    return written


# A DATE IN A LIST FILENAME, in the shapes peers actually publish:
# "-2026-09-07" (ours), "(2026-01-02)" (OmenServe's), and the separator
# variants around them. Stripped when deriving a list's marker, because the
# marker is an IDENTITY and a date changes on every rebuild - key a stored
# list on the filename and each re-fetch becomes a new list, orphaning the old
# one, growing the sidebar forever and leaving the freshness LED nothing
# stable to compare.
_LIST_DATE_RE = re.compile(r"[\(\[\-_ ]?\d{4}[-_.]\d{2}[-_.]\d{2}[\)\]]?")

# What some bots put after the date. "-OS" is OmenServe's; it says who built
# the list, not which list it is.
_LIST_TRAILER_RE = re.compile(r"[-_ ]*(?:OS|OmenServe)\s*$", re.IGNORECASE)

# mxrarserver's (#1209): "-Files(<x>)-MX", "-Folders(<x>)-MX". The "-MX" says
# who built it and the parenthesis changes from one build to the next, so
# both come off. A separator before "MX" is required: "TOPMX" keeps its MX.
_MX_LIST_TRAILER_RE = re.compile(r"\s*(?:\([^)]*\))?\s*[-_ ]+MX\s*$", re.IGNORECASE)

# An mxrarserver FOLDERS list: one row per folder it packs into a RAR on
# request - its pack list, the counterpart of DCCore's "-RAR-" list.
_MX_FOLDERS_LIST_RE = re.compile(r"-Folders\s*(?:\([^)]*\))?\s*-MX\.txt$", re.IGNORECASE)

# How many lists to keep out of one archive. A peer's zip is untrusted, and
# "keep exactly one" was what bounded this before - without a ceiling, an
# archive of five hundred small .txt files becomes five hundred parses, five
# hundred sidebar rows and five hundred index writes, all comfortably under
# the existing byte cap.
MAX_LISTS_PER_ARCHIVE = 8


def _shared_list_prefix(stems):
    """The part every one of these filenames begins with.

    Derived from the files rather than assumed from the nick. A peer's list is
    named after its own LIST_BASE_NAME, which need not be the nick we asked -
    and for a bot whose nick contains a hyphen ("Some-Bot"), splitting on
    the first separator would cut the name in half.

    Trimmed back to a separator so the prefix cannot end mid-word: with
    "SomeBot-RAR-..." and "SomeBot-README-..." the raw common prefix is
    "SomeBot-R", and the markers would come out "AR" and "EADME".
    """
    if not stems:
        return ""
    prefix = os.path.commonprefix([stem.lower() for stem in stems])
    cut = max(prefix.rfind(sep) for sep in ("-", "_", " ", "."))
    return stems[0][:cut + 1] if cut >= 0 else ""


def list_marker(file_name, shared_prefix=""):
    """The short, STABLE name for one list inside an archive.

        SomeBot-2026-09-07.txt          ->  ""        (the master)
        SomeBot-RAR-2026-09-07.txt      ->  "RAR"
        SomeBot-VIDEO-2026-09-07.txt    ->  "VIDEO"
        SomeBot-Default(2026-01-02)-OS  ->  "Default"

    Empty means the archive's main list - what a bare "@<nick>" is understood
    to be offering, and what every reader of a fetched entry meant before an
    archive could hold more than one.
    """
    stem = os.path.splitext(os.path.basename(str(file_name or "")))[0]
    if shared_prefix and stem.lower().startswith(shared_prefix.lower()):
        stem = stem[len(shared_prefix):]
    stem = _LIST_DATE_RE.sub("", stem)
    stem = _LIST_TRAILER_RE.sub("", stem)
    stem = _MX_LIST_TRAILER_RE.sub("", stem)
    return stem.strip("-_ .")


def _list_txt_files(extract_dir):
    """Every .txt in the extracted archive, long-path wrapped like the rest of
    this module - see _pick_list_file() for why both halves of that matter on
    Windows."""
    long_root = platform_compat.long_path(extract_dir)
    found = []
    for root, _dirs, files in os.walk(long_root):
        for fname in files:
            if fname.lower().endswith(".txt"):
                found.append(os.path.join(root, fname))
    return found


def pick_list_files(extract_dir, main):
    """Every list in the archive, as [(marker, path), ...], the main one first.

    A peer's archive routinely holds more than one list, and until now exactly
    one of them survived: _pick_list_file() skipped anything matching the
    "-rar-"/"-video-" conventions and took the largest of what remained. So a
    bot offering its albums as a separate RAR list, or its films as a separate
    video list, had that half silently dropped - and an operator whose own
    content lived in the second file saw an empty catalogue for a bot that
    plainly advertises thousands.

    THE MAIN ONE KEEPS THE EMPTY MARKER, and is passed IN - decided once, by
    the rule that has always decided it, at the point that already had to make
    the choice. Asking _pick_list_file() a second time here would repeat its
    log line, and would call a function a concurrency test deliberately hooks
    to block on its first invocation. Everything a stored entry meant before
    an archive could hold more than one still means it, because the empty
    marker IS what it meant.

    CAPPED, and the cap is not silent. A peer's zip is untrusted and "keep
    exactly one" was what bounded this; see MAX_LISTS_PER_ARCHIVE.
    """
    txt_files = _list_txt_files(extract_dir)
    if not txt_files or main is None:
        return []
    stems = [os.path.splitext(os.path.basename(p))[0] for p in txt_files]
    prefix = _shared_list_prefix(stems)

    ordered = [main] + sorted(p for p in txt_files if p != main)
    if len(ordered) > MAX_LISTS_PER_ARCHIVE:
        # NAMED, but not all of them. A cap that says nothing reads as "we
        # covered everything"; a cap that names thirty-two files is a wall of
        # text nobody finishes. The count is the fact, and a few names make it
        # recognisable.
        dropped = [os.path.basename(p) for p in ordered[MAX_LISTS_PER_ARCHIVE:]]
        shown = ", ".join(dropped[:3])
        if len(dropped) > 3:
            shown += f", and {len(dropped) - 3} more"
        print(f"[LIST-FETCH] Keeping {MAX_LISTS_PER_ARCHIVE} of "
              f"{len(ordered)} lists in this archive; ignored {shown}. "
              f"Raise MAX_LISTS_PER_ARCHIVE if a peer publishes more.")
        ordered = ordered[:MAX_LISTS_PER_ARCHIVE]

    kept = []
    seen = set()
    for path in ordered:
        # THE MAIN LIST HAS NO MARKER, decided here rather than derived. That
        # also settles the single-file archive: with nothing to contrast a
        # name against, list_marker() would find the date or the base name
        # distinguishing and invent a sub-list the archive does not have. The
        # only file in an archive is the main one, so it never reaches that.
        marker = "" if path == main else list_marker(path, prefix)
        # A marker has to be unique within the archive - it is half the key
        # the list is stored and browsed under. Two files deriving the same
        # one is not a reason to drop either, so the later gets its filename
        # instead of a guess at what makes it different.
        if not marker or marker.lower() in seen:
            if path != main:
                marker = os.path.splitext(os.path.basename(path))[0]
        if marker.lower() in seen:
            continue
        seen.add(marker.lower())
        kept.append((marker, path))
    return kept


def _pick_list_file(extract_dir):
    """Find the extracted master-list .txt file.

    The real naming convention (see update_list.py) is
    "<LIST_BASE_NAME>-<date>.txt", but a fetched zip came from someone else's
    bot running its own base name - not necessarily ours - so this does not
    hardcode config.LIST_BASE_NAME. Instead:

      * excludes anything matching update_list.py's own "-RAR-" convention
        for the separate album-folder list, the same way list.find_latest_list()
        already excludes it from search;
      * if exactly one plausible .txt remains, uses it;
      * if more than one remains (ambiguous), picks the LARGEST one - a real
        master list enumerates every track and is by far the biggest text
        file in a list archive - and logs a clear warning that it had to
        guess, rather than silently choosing wrong or crashing;
      * if none remain, returns None so the caller can report "no
        recognisable list file" instead of guessing at all.
    """
    # long_path()-wrapped, like every other path in this module (see the
    # comment at the top of _write_member()). This was the one function
    # without it, and both halves bite on Windows: os.walk() silently returns
    # nothing for a directory past MAX_PATH, and the getsize() below raises
    # FileNotFoundError out of a function whose caller documents it as never
    # raising. A fetched list lands under a temp directory plus the sending
    # bot's own nick plus whatever it called its file, so the depth is not
    # this bot's to control.
    #
    # The walk root is wrapped and the results are joined onto that same
    # wrapped root, so nothing downstream mixes a prefixed path with an
    # unprefixed one - the mistake that turns a silent omission into a
    # ValueError.
    long_root = platform_compat.long_path(extract_dir)
    txt_files = []
    for root, _dirs, files in os.walk(long_root):
        for fname in files:
            if fname.lower().endswith(".txt"):
                txt_files.append(os.path.join(root, fname))

    if not txt_files:
        return None

    # "-video-" alongside "-rar-", and for the same reason twice over. Since
    # the film-and-series split, THIS bot's own archive carries two .txt
    # files - the master and "<base>-VIDEO-<date>.txt" - so a peer running
    # DCCore is the ORDINARY case here, not an exotic one. Without this the
    # largest-wins tiebreak below decides which is "the" list, and a bot whose
    # films outweigh its music hands us its film list as its master: we would
    # index the films, show them as that bot's whole catalogue, and report its
    # music as absent.
    #
    # Excluding it drops those films from the fetched copy rather than
    # merging them in, which is the same thing find_latest_list() does locally
    # with the album list. Reading both into one fetched list is a change to
    # what this function returns and to the size ceiling that guards it; it is
    # recorded in docs/FUTURE.md rather than smuggled in here.
    skip = ("-rar-", f"-{list_mod.VIDEO_LIST_MARKER.lower()}-")
    # mxrarserver's Folders list is its pack list, as "-RAR-" is ours (#1209):
    # in a "Complete" archive beside its Files list, Files is the main one.
    candidates = [p for p in txt_files
                  if not any(m in os.path.basename(p).lower() for m in skip)
                  and not _MX_FOLDERS_LIST_RE.search(os.path.basename(p))]
    if not candidates:
        candidates = txt_files

    if len(candidates) == 1:
        return candidates[0]

    # A member that vanished between the walk and here (an antivirus quarantine
    # mid-fetch is the realistic one) sorts last rather than taking the whole
    # fetch down: this function's caller documents it as never raising.
    #
    # The long_path() here is belt to the walk's braces and currently
    # redundant - every path in txt_files was joined onto the already-wrapped
    # root, so it arrives prefixed and the call is idempotent. It stays so the
    # two do not have to be reasoned about together: a later change that
    # unwraps the walk would otherwise reintroduce half the bug silently.
    def _size(path):
        try:
            return os.path.getsize(platform_compat.long_path(path))
        except OSError as err:
            print(f"[LIST-FETCH] Could not size {os.path.basename(path)!r} "
                  f"while picking the list file: {err}")
            return -1

    candidates.sort(key=_size, reverse=True)
    # Short, and said once. This used to be a WARNING about a guess with
    # something to lose - the others were discarded. They are all kept now
    # (see pick_list_files()), so all this decides is which one a bare
    # "@<nick>" is understood to mean, and a multi-list archive is the
    # ordinary case rather than something to warn about.
    print(f"[LIST-FETCH] {len(candidates)} lists here; "
          f"{os.path.basename(candidates[0])} is the main one.")
    return candidates[0]


def _looks_binary(head):
    """True when `head` (raw bytes) cannot be the start of a text list.

    One NUL settles it: no text list holds one, and every archive format
    has them in its first few bytes. Failing that, control characters from
    _BINARY_CONTROL_BYTES making up more than a 32nd of the head - random
    data is about 7% of them, so a short binary with no NUL is still caught,
    while a stray one in a real list is not enough to refuse it.
    """
    if b"\x00" in head:
        return True
    controls = len(head) - len(head.translate(None, _BINARY_CONTROL_BYTES))
    return controls * 32 > len(head)


def _accept_plain_text_list(source_path, extract_dir):
    """Take a list that arrived as plain text, returning (path, reason).

    The archive guards it skips are all guards about ARCHIVES - member counts,
    traversal in member names, a compressed size that expands - and none of
    them has anything to say about a single file that is already on disk at a
    size we have measured. The one guard that does apply, the text-size
    ceiling, runs where it always did: on the file this returns, in the caller.

    Copied into the extraction directory rather than parsed where it landed,
    so everything downstream sees the same shape from both routes and the
    caller's cleanup covers both.
    """
    # IT STILL HAS TO LOOK LIKE A LIST. The zip route gets its plausibility
    # from the archive guards and _pick_list_file(); this route has neither, so
    # without a check here any file at all that is not a zip would be stored as
    # a bot's list - parsing to zero rows, reported as a successful fetch, and
    # answering every filter with nothing.
    #
    # The property is the one the parser needs: a request line. Read from a
    # BOUNDED prefix rather than the whole file, because the file may be
    # 128MB and the answer is in the first few lines - after a header and a
    # banner, which is itself capped at 8KB.
    try:
        with io.open(platform_compat.long_path(source_path), "rb") as handle:
            head = handle.read(_PLAUSIBLE_LIST_PREFIX)
    except OSError as err:
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, f"could not read the fetched list: {err}"

    # BINARY NEVER PASSES (#1200). The request-line test below was the only
    # check, and it read the head with str.splitlines(), which also breaks on
    # \x0b, \x0c, \x1c-\x1e and \x85: compressed data fell apart into
    # thousands of short "lines", one of them started with "!", and 64KB of
    # random bytes passed 50 times out of 50. A RAR or 7z list was installed
    # as the bot's list - zero rows, reported as arrived - in place of the
    # good one already held.
    #
    # Judged on the raw bytes, not on U+FFFD after decoding: a list written
    # in a legacy 8-bit code page decodes to as many replacement characters
    # as random bytes do (a Greek cp1253 list is about 45% of them, random
    # data about 44%), so that ratio cannot tell the two apart. A NUL, or
    # control characters no text list contains, can.
    if _looks_binary(head):
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, ("the file is not a zip, a RAR archive or a text list - "
                      "it holds binary data - so it is not a file list")

    # Lines are split where the list parser splits them: text mode ends a
    # line at "\n", "\r\n" or a lone "\r", and nowhere else.
    text = head.decode("utf-8", "replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not any(line.lstrip().startswith("!") for line in lines):
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, ("the file is not a zip and holds no request lines, so it "
                      "is not a file list")

    try:
        destination = os.path.join(extract_dir, os.path.basename(source_path))
        shutil.copyfile(platform_compat.long_path(source_path),
                        platform_compat.long_path(destination))
    except OSError as err:
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, f"could not read the fetched list: {err}"
    return destination, None


# ==========================================================================
# RAR lists (#1200).
#
# A peer running DCCore with LIST_FORMAT = "rar" sends its list as a RAR
# archive, and before this the archive itself went down the plain-text route
# and was installed as the list. Python cannot read RAR, so the configured
# rar program does - the same one dcc.py packs albums with, found the same
# way (platform_compat.rar_command()).
#
# The archive is held to the zip route's guards, in the same order: every
# member is LISTED and checked before a single byte is unpacked - the entry
# count, the declared sizes against the zip-bomb budget, absolute paths,
# drive letters, traversal and all-dots components (_validate_zip_members()
# itself), then the text ceiling on the list that is picked.
#
# rar never chooses where anything is written. Each .txt member is printed
# to a pipe ("rar p") and copied by this code to a path built here from the
# checked name, stopping the moment it passes the size the member declared.
# So a link member, a lying size or a name rar would resolve differently
# from this code can never put a byte anywhere but the extraction directory,
# and never more of them than the listing allowed for.
# ==========================================================================

class _RarMember(object):
    """One entry of a RAR listing, shaped like the ZipInfo that
    _validate_zip_members() reads: filename, file_size and is_dir()."""

    __slots__ = ("filename", "file_size", "_is_dir")

    def __init__(self, filename, file_size, is_dir):
        self.filename = filename
        self.file_size = file_size
        self._is_dir = is_dir

    def is_dir(self):
        return self._is_dir


def _archive_signature(path):
    """"rar" or "7z" when the file starts with that format's signature,
    else None. Read from the content, never the offered name - the same
    reason the zip check reads the archive's own end record."""
    try:
        with io.open(platform_compat.long_path(path), "rb") as handle:
            start = handle.read(8)
    except OSError:
        return None
    if start.startswith(_RAR_SIGNATURE):
        return "rar"
    if start.startswith(_SEVEN_ZIP_SIGNATURE):
        return "7z"
    return None


def _rar_argv(rar_bin, args):
    """The command line for one rar child. A seam of its own so the tests
    can put a stand-in rar behind every real code path below on a machine
    with no rar installed."""
    return [rar_bin] + list(args)


class _RarChild(object):
    """One rar process, read through its stdout, and killed by its own
    handle - never by name: the operator may be running rar themselves - if
    it outlives _RAR_LIST_TIMEOUT.

    The watchdog is a timer rather than a timeout on a wait, because the
    reader blocks in read() on the pipe, and a hung rar never returns from
    that. Killing it closes the pipe, which ends the read.
    """

    def __init__(self, rar_bin, args):
        self.timed_out = False
        # A list of arguments and never a shell, no stdin (a password prompt
        # would otherwise wait for ever; -p- says the same to rar), and
        # stderr discarded so a chatty rar cannot fill a pipe nobody reads.
        self.process = subprocess.Popen(_rar_argv(rar_bin, args),
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        **platform_compat.no_console_window())
        self._watchdog = threading.Timer(_RAR_LIST_TIMEOUT, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _expire(self):
        self.timed_out = True
        self._kill()

    def _kill(self):
        try:
            self.process.kill()
        except OSError:
            pass

    def finish(self, completed):
        """Wait for the child and return its exit code. `completed` says the
        caller read its output to the end; otherwise the caller stopped
        early, and the child is killed rather than waited on."""
        try:
            if not completed:
                self._kill()
            try:
                return self.process.wait(timeout=_RAR_LIST_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._kill()
                return self.process.wait()
        finally:
            self._watchdog.cancel()
            self.process.stdout.close()


def _decode_rar_listing_line(raw):
    """One line of rar's listing as text, or None if it cannot be read.

    On Windows rar is asked for UTF-8 (-scfr) and anything else is refused.
    Elsewhere it prints names in the locale's encoding, as the file system
    hands them over, so they are decoded the way Python decodes file names -
    and go back to rar unchanged when a member is asked for by name.
    """
    if platform_compat.IS_WINDOWS:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return os.fsdecode(raw)


def _list_rar_members(rar_bin, archive):
    """Every entry of `archive`, from rar's technical listing ("rar lt").
    Returns (members, None), or (None, reason).

    Read a line at a time, so an archive of a million tiny entries is
    refused at entry MAX_LIST_ZIP_ENTRIES + 1 instead of being held whole in
    memory first. -c- keeps the archive comment out of the listing: the
    sender writes the comment, and it can hold lines shaped like entries.
    """
    unreadable = "rar's listing of the archive could not be read"
    args = ["lt", "-p-", "-cfg-", "-c-"]
    if platform_compat.IS_WINDOWS:
        args.append("-scfr")
    args += ["--", archive]
    child = _RarChild(rar_bin, args)
    entries = []
    reason = None
    completed = False
    try:
        while True:
            raw = child.process.stdout.readline(_RAR_LISTING_LINE_LIMIT)
            if not raw:
                completed = True
                break
            if len(raw) >= _RAR_LISTING_LINE_LIMIT and not raw.endswith(b"\n"):
                reason = unreadable + " (a line of it is too long)"
                break
            line = _decode_rar_listing_line(raw)
            if line is None:
                reason = unreadable + " (it is not UTF-8)"
                break
            line = line.rstrip("\r\n").lstrip(" ")
            # "Name: <the name>" - the name exactly as rar printed it, spaces
            # and all, because it is handed back to rar to ask for the member.
            key, sep, value = line.partition(": ")
            if key == "Name" and sep:
                if len(entries) >= MAX_LIST_ZIP_ENTRIES:
                    reason = (f"RAR contains more than {MAX_LIST_ZIP_ENTRIES} "
                              f"entries, more than a real master-list archive "
                              f"should ever need (zip-bomb-shaped guard)")
                    break
                entries.append({"Name": value})
                continue
            # Everything before the first entry is rar's banner and the
            # archive's own details.
            if not entries or not line:
                continue
            if not sep or key in entries[-1]:
                reason = unreadable
                break
            entries[-1][key] = value
    finally:
        code = child.finish(completed=completed)

    if child.timed_out:
        return None, (f"rar took more than {_RAR_LIST_TIMEOUT} seconds to "
                      f"list the archive")
    if reason:
        return None, reason
    if code != 0:
        return None, (f"rar could not read the archive (exit code {code}) - "
                      f"it is damaged, encrypted, or not a whole RAR archive")

    members = []
    for entry in entries:
        name = entry["Name"]
        kind = entry.get("Type", "").strip()
        if kind == "Directory":
            members.append(_RarMember(name, 0, True))
        elif kind == "File":
            size = entry.get("Size", "").strip()
            if not re.fullmatch(r"[0-9]+", size):
                return None, unreadable
            members.append(_RarMember(name, int(size), False))
        else:
            # A link, a hard link or a "file copy" points at something else,
            # and a list archive has no reason to hold one.
            return None, (f"RAR entry {name!r} is not a file or a folder "
                          f"({kind or 'no type given'})")
    return members, None


def _validate_rar_names(members):
    """The checks a RAR member's NAME needs beyond the zip route's, because
    rar is asked for each member by that name and reads it as a pattern.
    Returns a reason, or None.

    "*" and "?" would match other members, a leading "@" or "-" reads as a
    list file or a switch, and two names differing only in case are one
    name to rar on Windows. Control characters cannot be told apart from the
    listing's own line breaks. A real list archive has none of these.
    """
    seen = set()
    for member in members:
        name = member.filename
        if (any(ch in name for ch in "*?") or name.startswith(("@", "-"))
                or any(ord(ch) < 32 for ch in name)):
            return (f"RAR entry {name!r} has a name rar would read as a "
                    f"pattern or a switch")
        folded = name.replace("\\", "/").rstrip("/").lower()
        if folded in seen:
            return f"RAR entry {name!r} appears in the archive twice"
        seen.add(folded)
    return None


def _unpack_rar_member(rar_bin, archive, member, dest_path):
    """Copy one member out of `archive` to `dest_path`, through a pipe.
    Returns None, or the reason the whole archive is refused.

    Never more than the size the listing declared for it: that is the
    number _validate_zip_members() summed against the budget, so reading
    past it is the zip bomb those guards exist for - stopped here on the
    real bytes, without trusting rar to stop on its own.
    """
    os.makedirs(platform_compat.long_path(os.path.dirname(dest_path)),
                exist_ok=True)
    child = _RarChild(rar_bin, ["p", "-inul", "-p-", "-cfg-", "--",
                                archive, member.filename])
    written = 0
    completed = False
    try:
        with open(platform_compat.long_path(dest_path), "wb") as dst:
            while True:
                chunk = child.process.stdout.read(_COPY_CHUNK)
                if not chunk:
                    completed = True
                    break
                written += len(chunk)
                if written > member.file_size:
                    break
                dst.write(chunk)
    finally:
        code = child.finish(completed=completed)

    if child.timed_out:
        return (f"rar took more than {_RAR_LIST_TIMEOUT} seconds to unpack "
                f"{member.filename!r}")
    if written > member.file_size:
        return (f"RAR entry {member.filename!r} unpacked past its declared "
                f"size of {member.file_size} bytes (zip-bomb guard)")
    if code != 0:
        return (f"rar could not unpack {member.filename!r} (exit code {code})")
    return None


def _extract_rar_list(rar_path, extract_dir):
    """The RAR counterpart of the zip branch of
    _extract_and_locate_list_file(), with the same (list_path, reason)
    contract and the same rule: refused means nothing of it is left on disk.
    """
    def refuse(reason):
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, reason

    rar_bin = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
    if not rar_bin:
        return refuse("the list arrived as a RAR archive, and no rar program "
                      "was found to open it - install rar, or set RAR_BINARY "
                      "to where it is")
    archive = os.path.abspath(rar_path)
    try:
        members, reason = _list_rar_members(rar_bin, archive)
        if reason:
            return refuse(reason)
        reason = (_validate_zip_members(members, extract_dir, kind="RAR")
                  or _validate_rar_names(members))
        if reason:
            return refuse(reason)
        # Only the .txt members: they are all _pick_list_file() and
        # pick_list_files() ever look at, so nothing else is worth a child.
        for member in members:
            if member.is_dir() or not member.filename.lower().endswith(".txt"):
                continue
            reason = _unpack_rar_member(rar_bin, archive, member,
                                        os.path.join(extract_dir,
                                                     *_member_parts(member.filename)))
            if reason:
                return refuse(reason)
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return refuse(f"extraction aborted: {err}")
    except Exception as err:
        # Same promise as the zip branch: never an exception loose in the
        # fetch thread over bytes a peer chose.
        return refuse(f"extraction aborted: {type(err).__name__}: {err}")
    return _pick_list_file(extract_dir), None


def _extract_and_locate_list_file(zip_path, extract_dir):
    """Validate, then safely extract, `zip_path` into `extract_dir` (wiped
    and recreated first, so a previous fetch's leftovers can never be
    mistaken for this one's), and return (list_path, reason):

      * (path, None) - extraction succeeded and a plausible list file was
        found at `path`.
      * (None, None) - extraction succeeded but no plausible list .txt was
        found anywhere inside the archive.
      * (None, reason) - the archive was rejected outright (path traversal,
        zip bomb, too many entries, not a valid zip, ...); nothing from it
        was left on disk, including any partial extraction from before the
        rejection was detected.
    """
    try:
        if os.path.exists(platform_compat.long_path(extract_dir)):
            # The directory itself is short, but a previous archive may have
            # left long-named members inside it; rmtree cannot delete what
            # it cannot open, and the prefix is inherited by every child.
            shutil.rmtree(platform_compat.long_path(extract_dir))
        os.makedirs(platform_compat.long_path(extract_dir), exist_ok=True)
    except OSError as err:
        return None, f"could not prepare the extraction directory: {err}"

    # #162 finding #10: the entry-count/size guards below all run on
    # zf.infolist(), which zipfile.ZipFile() has ALREADY eagerly built (one
    # ZipInfo per entry, plus a NameToInfo dict) by the time this code can see
    # it - the cost those guards exist to prevent is paid before they can
    # refuse anything. dcc_fetch.handle_incoming_offer() now refuses an
    # oversized "list" offer before ever connecting (MAX_FETCH_LIST_FILE_SIZE),
    # which is what actually prevents this; this is the belt to that braces -
    # a cheap check on the file already sitting on disk, before opening it,
    # in case that admission-time cap is ever bypassed or misconfigured.
    try:
        on_disk_size = os.path.getsize(platform_compat.long_path(zip_path))
    except OSError as err:
        return None, f"could not stat the fetched zip: {err}"
    list_zip_cap = int(getattr(config, "MAX_FETCH_LIST_FILE_SIZE", 10 * 1024 * 1024))
    # 0 MEANS NO LIMIT, same as dcc_fetch.py's own admission check on this
    # setting (#302) - missing here meant a fetch that setting explicitly
    # allowed through was thrown away right after a successful download,
    # since `on_disk_size > 0` is true of any real file (#937).
    if list_zip_cap > 0 and on_disk_size > list_zip_cap:
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, (f"fetched zip is {on_disk_size} bytes, more than "
                       f"MAX_FETCH_LIST_FILE_SIZE ({list_zip_cap}) - refusing "
                       f"to open it")

    # NOT EVERY LIST IS A ZIP. A bot that publishes its list as a plain .txt
    # sends exactly that, and this refused it with "extraction aborted: File
    # is not a zip file" - a real fetch, completed at 100%, thrown away at the
    # last step. update_list.py has published .txt as a LIST_FORMAT since #201;
    # there was never a reason to expect only archives back.
    #
    # Detected by CONTENT, not by the offered filename: the name comes from
    # the sending bot and a peer calling a zip "list.txt" must not skip the
    # archive guards. zipfile.is_zipfile() reads the file's own end-of-archive
    # record.
    #
    # A RAR list is not a zip either, and is opened with rar (#1200). A 7z is
    # named in the refusal rather than called binary data, so the operator
    # knows what the peer sent.
    if not zipfile.is_zipfile(platform_compat.long_path(zip_path)):
        signature = _archive_signature(zip_path)
        if signature == "rar":
            return _extract_rar_list(zip_path, extract_dir)
        if signature == "7z":
            shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
            return None, ("the list arrived as a 7z archive, which DCCore "
                          "cannot open - it reads .txt, .zip and .rar lists")
        return _accept_plain_text_list(zip_path, extract_dir)

    try:
        with zipfile.ZipFile(platform_compat.long_path(zip_path), "r") as zf:
            infolist = zf.infolist()
            reason = _validate_zip_members(infolist, extract_dir)
            if reason:
                shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
                return None, reason

            budget = _fetch_file_size_budget()
            for info in infolist:
                if info.is_dir():
                    continue
                parts = _member_parts(info.filename)
                if not parts:
                    continue
                dest_path = os.path.join(extract_dir, *parts)
                written = _extract_member(zf, info, dest_path, budget)
                budget -= written
    except (zipfile.BadZipFile, ValueError, OSError) as err:
        # Covers a corrupt/non-zip file, the zip-bomb guard tripping mid-copy
        # (ValueError from _extract_member), and any filesystem error - every
        # one of them means "abort the whole extraction", never "keep what
        # extracted so far".
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, f"extraction aborted: {err}"
    except Exception as err:
        # zipfile leaks more than those three for a hand-crafted archive:
        # zlib.error for a corrupt deflate stream, NotImplementedError for a
        # compression method it has no decompressor for, and RuntimeError for
        # a member flagged encrypted. None of them subclass the cases above.
        #
        # A remote peer chooses these bytes, and process_fetched_list_zip()
        # promises callers it never raises, so anything that gets past the
        # specific cases still means "abort this extraction" - never an
        # exception loose in the fetch thread. The type name goes into the
        # reason because, unlike the cases above, it is not self-describing.
        #
        # long_path()-wrapped like every other rmtree() in this function
        # (#222): this branch is the one a hand-crafted archive with long
        # member paths actually reaches, so on Windows it is also the one
        # most likely to be cleaning up a directory over the 260-character
        # limit - unwrapped, ignore_errors=True would swallow that failure
        # and leave the rejected, partially-extracted contents on disk
        # permanently, under FETCHED_FILES_DIR.
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return None, f"extraction aborted: {type(err).__name__}: {err}"

    return _pick_list_file(extract_dir), None


# ==========================================================================
# Keeping held lists current (#302).
# ==========================================================================

def _hours_to_seconds(hours):
    try:
        return max(0.0, float(hours) * 3600.0)
    except (TypeError, ValueError):
        return 0.0


# A list whose bot gives no evidence of change - no date in its advert, or no
# advert seen - is refreshed once it is this old (#926 item 6), the way
# AutoGet expired lists after N days. Age is the only evidence such a bot
# leaves; a bot that does publish a date is still refreshed on that alone.
UNKNOWN_LIST_MAX_AGE_DAYS = 14


def lists_worth_refetching(now=None):
    """The bots whose held list their own advert says has moved on.

    Returns a list of nicks, oldest fetch first, so a run that is capped takes
    the most stale ones. Empty when the feature is off, when nothing is held,
    or when nothing has changed.

    THE ADVERT DECIDES, not a timer. #286 already worked out what "moved on"
    means and why: their advert THEN against their advert NOW, date first and
    count second, because bots count differently and an off-by-a-few would
    mark a list permanently stale. Re-fetching on a timer alone would ask
    every bot for a list we already have, every interval, for ever - which is
    other people's bandwidth and other people's transfer slots.

    "unknown" is not "changed". A bot that publishes no date, or one whose
    advert we have not seen since starting, gives no evidence either way, and
    acting on no evidence is what makes an automatic feature untrustworthy.
    EXCEPT AGE (#926): such a list is refreshed once it is older than
    UNKNOWN_LIST_MAX_AGE_DAYS - otherwise it is never refreshed at all, which
    is the one thing sure to be wrong about it.
    """
    import webserver

    if not getattr(config, "AUTO_REFETCH_LISTS", False):
        return []

    now = time.time() if now is None else now
    interval = _hours_to_seconds(getattr(config, "AUTO_REFETCH_INTERVAL_HOURS", 24))

    held = dict(getattr(config, "fetched_bot_lists", {}) or {})
    due = []
    for entry in held.values():
        if not isinstance(entry, dict):
            continue
        bot = str(entry.get("bot") or "").strip()
        if not bot:
            continue

        # NOT MORE OFTEN THAN THE INTERVAL, whatever the advert says. A bot
        # rebuilding its list hourly would otherwise be re-fetched hourly.
        fetched_at = entry.get("fetched_at") or 0
        # The floor runs from the LATER of the last completed fetch and the
        # last time we automatically asked. Measured from the fetch alone, a
        # bot whose list never arrives (it is not answering, or we were
        # offline when it did) keeps its old fetched_at for ever, so every
        # hourly sweep - and every restart - asked it again.
        last_asked = entry.get("last_attempt") or 0
        try:
            last_asked = float(last_asked)
        except (TypeError, ValueError):
            last_asked = 0.0
        if interval and (now - max(float(fetched_at or 0), last_asked)) < interval:
            continue

        rows = [row for row in webserver.build_fetched_bot_list_summaries()
                if str(row.get("bot", "")).strip().lower() == bot.lower()]
        if not rows:
            continue
        freshness = rows[0].get("freshness")
        too_old = (freshness == "unknown"
                   and now - float(fetched_at or 0) >= UNKNOWN_LIST_MAX_AGE_DAYS * 86400)
        if freshness != "changed" and not too_old:
            continue
        due.append((float(fetched_at or 0), bot))

    due.sort()
    return [bot for _when, bot in due]


def mark_seen(bot):
    """The operator opened this bot's list (#926 item 6): it is no longer
    new. Returns whether anything changed."""
    key = str(bot or "").strip().lower()
    with _lock():
        store = _ensure_fetched_bot_lists()
        entry = store.get(key)
        if not isinstance(entry, dict) or "seen_at" not in entry:
            return False
        if float(entry.get("seen_at") or 0) >= float(entry.get("fetched_at") or 0):
            return False
        entry["seen_at"] = time.time()
        snapshot = dict(store)
    db.save_fetched_bot_lists(snapshot)
    return True


def _tell_the_console(bot, action, text):
    """One `LISTFETCH` line for a console or the mIRC window (#750). Never raises:
    a console that cannot be told must not fail a fetch."""
    try:
        import announce
        announce.feed_event("LISTFETCH", text, bot=bot, action=action)
    except Exception as err:
        print(f"[LIST-FETCH] Could not tell the console about {bot}: {err}")


def _note_auto_attempt(bot, when):
    """Remember that a sweep just asked `bot` for its list, on disk.

    Only an AUTOMATIC ask is recorded: what limits the sweep is how often it
    has bothered a bot, and a click on Re-download list is the operator's own
    decision and is not the sweep's to count. A completed fetch replaces the
    entry, so the mark goes with it - by then fetched_at is newer anyway.
    Persisted, because a restart is exactly when a failing bot used to be
    asked again at once."""
    key = str(bot).strip().lower()
    with _lock():
        store = _ensure_fetched_bot_lists()
        entry = store.get(key)
        if not isinstance(entry, dict):
            return
        entry["last_attempt"] = when
        snapshot = dict(store)
    db.save_fetched_bot_lists(snapshot)


def _freshness_of(bot):
    """The List Browser's freshness for `bot`'s held list, or None."""
    import webserver

    for row in webserver.build_fetched_bot_list_summaries():
        if str(row.get("bot", "")).strip().lower() == str(bot).strip().lower():
            return row.get("freshness")
    return None


def refetch_due_lists(log=print, now=None):
    """Ask again for the held lists their own adverts say have changed.

    Returns the nicks actually enqueued. Bounded per run by
    AUTO_REFETCH_MAX_PER_RUN: a bot that has been offline for a month comes
    back to thirty stale lists, and asking all thirty at once is a burst of
    outbound requests nobody asked for - the rest are picked up next time
    round, oldest first. Only bots in one of our channels are asked, and only
    the asks that went out count toward the bound (#966).

    Goes through the SAME enqueue the dashboard's own Refresh uses, so the
    slot limits, the duplicate guard and the queue ceiling all apply exactly
    as they do to a fetch an operator started by hand.

    Refuses outright while the bot has not settled into its channels yet
    (config.bot_joined_channel) - same gate dcc.py's own presence decisions
    already use, and the same reason: channel_users is empty or half-synced
    before that, so nothing here can tell a bot that is actually gone from
    one we simply have not heard from yet. Without this, the very first
    sweep - started from oserve.startup() before the IRC socket has even
    finished registering, let alone joined anything - queued PRIVMSGs that
    went out (via the same outbound queue every other message uses) while
    still mid-handshake, landing in whatever channel happened to be first in
    config.CHANNEL rather than one the bot was actually in. The target bot
    never saw them, and the request just timed out as "no response" -
    indistinguishable from the target genuinely being unreachable.
    """
    if not getattr(config, "bot_joined_channel", False):
        return []

    import webserver

    # ONLY BOTS THAT ARE HERE, and the cap counts only what was asked (#966).
    # The oldest lists come first, and the oldest are the likeliest to belong
    # to bots long gone: past UNKNOWN_LIST_MAX_AGE_DAYS with their adverts
    # aged out, three of them took the three places of every sweep, were
    # refused as "not here" - which is not an ask, so last_attempt never
    # moved them back - and a bot online with a changed list was never
    # reached. An absent bot is left for a sweep that finds it back.
    here = webserver.present_nicks()
    due = [bot for bot in lists_worth_refetching(now=now) if bot.lower() in here]
    if not due:
        return []

    try:
        cap = int(getattr(config, "AUTO_REFETCH_MAX_PER_RUN", 3))
    except (TypeError, ValueError):
        cap = 3

    started = []
    for bot in due:
        if cap > 0 and len(started) >= cap:
            break
        # build_list_fetch_enqueue_result(bot_raw) wants the nick ITSELF -
        # see its own docstring and the real HTTP route's call
        # (build_list_fetch_enqueue_result(body.get("bot", ""))) - not a
        # dict wrapping it. A dict here failed reject_if_unsafe_for_irc_line()'s
        # isinstance(value, str) check on every single call, so this feature
        # rejected every bot with "'bot' must be a string." and never
        # actually re-fetched a list (#535).
        status, result = webserver.build_list_fetch_enqueue_result(bot)
        if status == 200:
            started.append(bot)
            _note_auto_attempt(bot, time.time() if now is None else now)
            if _freshness_of(bot) != "changed":
                # #926: no date to compare, so age was the reason.
                why = f"{bot}'s list is over {UNKNOWN_LIST_MAX_AGE_DAYS} days old"
                log(f"[LIST-FETCH] {why} and its advert shows no date "
                    f"- asking again automatically.")
            else:
                why = f"{bot}'s list has changed"
                log(f"[LIST-FETCH] {why} since we took our copy "
                    f"- asking again automatically.")
            _tell_the_console(bot, "auto", f"{why} - asking again automatically")
        else:
            # Not an error worth stopping for: the usual reason is that a
            # fetch for that bot is already outstanding, which is the right
            # outcome and needs no announcement.
            log(f"[LIST-FETCH] Did not re-ask {bot}: "
                f"{result.get('error', 'refused')}")
    return started


def ensure_auto_refetch_worker(start=None):
    """Start the hourly loop below if AUTO_REFETCH_LISTS is on and it is not
    already running. Returns True only when this call started it.

    Called from oserve.startup() AND from the rehash body (#625). The worker
    used to be started by startup() alone, so ticking the setting on the
    dashboard - a save that fires a rehash - reported "rehash started" with
    no restart notice and started nothing: held lists went stale until the
    next restart, and the only live effect was the one-shot sweep irc.py runs
    on a reconnect.

    ONCE. The lock and the flag are runtime.py's, not this module's, for the
    reason every start guard in this project lives there: a module a rehash
    reloads gets a fresh flag and a fresh lock, and whether this module is on
    that list today is not something "one worker, never two" should depend
    on - a flag reset by the very rehash about to consult it would start one
    more worker per Settings save, each asking bots for lists. Turning the
    setting OFF needs no stop: refetch_due_lists() reads the flag on every
    pass and does nothing while it is off, so the worker simply idles.

    `start` is the thread starter, injectable so a test can watch the
    decision without a real thread outliving it.
    """
    if not getattr(config, "AUTO_REFETCH_LISTS", False):
        return False
    with runtime.auto_refetch_guard:
        if runtime.auto_refetch_started:
            return False
        starter = start or (lambda: threading.Thread(
            target=auto_refetch_worker, daemon=True).start())
        starter()
        runtime.auto_refetch_started = True
    return True


def auto_refetch_worker(sleep=None):
    """The loop. Started by ensure_auto_refetch_worker() above, from boot or
    from a rehash, once AUTO_REFETCH_LISTS is on.

    Deliberately its own thread and not a branch of the fetch dispatcher: that
    one runs every two seconds and only touches the queue, while this reads
    every held list and talks to webserver. Sharing it would make a slow
    read here delay every fetch promotion.
    """
    import time as time_mod

    naptime = sleep or (lambda seconds: time_mod.sleep(seconds))
    print("[LIST-FETCH] Automatic list refresh is on.")
    while True:
        try:
            refetch_due_lists()
        except Exception as err:
            print(f"[LIST-FETCH] Automatic refresh error: {err}")
        # A fixed hour between sweeps, not the configured interval: the
        # interval is how STALE a list may be before it is re-asked for, and
        # checking more often than that costs one pass over a dict.
        naptime(3600.0)



def process_fetched_list_zip(bot, zip_path, channel=None, secondary=False):
    """Entry point, called by dcc_fetch.py once a request_type="list" fetch
    reaches 'complete'. Safely extracts `zip_path`, locates the master-list
    .txt inside it, and stores a REFERENCE to it - not its parsed contents -
    in config.fetched_bot_lists keyed by lowercased bot nick, REPLACING any
    previous entry for the same bot, per the operator's explicit
    "switchable, not accumulating" requirement.

    `secondary` (#1240 review) is explicit, carried on the fetch_queue row
    from the moment it was enqueued (dcc_fetch.new_fetch_row()) all the way
    to here - never re-derived from `channel` at this end. Only
    list_grab.secondary_channel_tick() ever enqueues with it True; every
    other caller (a manual fetch, AUTO_REFETCH_LISTS, a Downloads-page
    retry) is always a primary refresh of this bot's own list, whatever
    channel it actually went out in.

    Issue #76, option 2: earlier versions of this function parsed the whole
    extracted list here and stored the resulting row list permanently in
    memory - alongside the byte-size cap above, that meant the single largest
    cost of a fetched list (every "!" line, forever, until the next fetch or
    a process restart) was paid once at fetch time and then never freed. This
    still does ONE parse+dedup pass below (to prove the file is genuinely
    parseable - a file that merely LOOKS like a valid master list but is
    actually garbage must still be caught here, same as before - and to get
    an accurate post-dedup row count for the dashboard switcher) but keeps
    only that count afterward. get_fetched_bot_page() below reads each later
    page from `list_path` on disk - no rows of a fetched list are retained in
    memory between views, only this small summary dict and, since #1128, a
    table of where each folder starts (about 38 bytes per folder, and never
    more than half the list's size or 1 MB, whichever is more: past that the
    pages read the list whole).

    Returns (success, reason): reason is None on success, otherwise a short
    human-readable string suitable for logging/dashboard display. Never
    raises - every anticipated failure mode (bad zip, zip bomb, path
    traversal, no recognisable list file) is handled here.

    The whole extract -> parse -> store sequence runs under the lock, not just
    the store write at the end. list_extract_dir() keys on the bot nick alone,
    and extraction opens by rmtree-ing that directory, so two fetches for the
    same bot would otherwise delete each other's files mid-extraction - and
    the fetch slot pool allows several transfers to complete at once.

    The lock is module-wide rather than per bot, which also serialises fetches
    for DIFFERENT bots. That is deliberate: it needs no nick-keyed registry to
    grow, and it means only one list is ever being parsed into memory at a
    time, so the peak cost of a parse is one list instead of one per slot.
    Extraction is a background step measured in seconds, so the wait costs
    nothing that matters.

    get_fetched_bot_page() below acquires this SAME lock around its read, for
    exactly the reason this docstring already gives for writers: extraction
    reuses the same on-disk list_path a concurrent read could be parsing
    (list_extract_dir() keys on the bot nick, not on any per-fetch id), and
    rewrites it via rmtree+open("wb") rather than write-then-rename. Without
    the read side sharing this lock, a same-bot re-fetch could race a read
    into a torn, partially-rewritten file - not an exception, a silently
    wrong `total` and row set. The bounded stall this adds to a read (waiting
    out an in-progress fetch, itself already a background step measured in
    seconds) is the same accepted tradeoff as above, extended to reads.
    """
    extract_dir = _extract_dir_for(bot, channel, secondary=secondary)
    with _lock():
        result = _process_fetched_list_zip_unlocked(bot, zip_path, channel=channel,
                                                     secondary=secondary)
        # The files under THIS fetch's directory were rewritten or put back:
        # its folder tables describe what was there (#1128). Under the lock,
        # so no page builds one from the files half way through. Keyed on
        # mtime and size as well, but a same-size rewrite inside one tick of
        # a coarse clock would keep both.
        list_mod.forget_folder_tables(under=extract_dir)
    # Outside the lock: telling a console can take a moment and nothing
    # else should wait for it (#750).
    try:
        succeeded, reason = result
    except (TypeError, ValueError):
        succeeded, reason = bool(result), ""
    if succeeded:
        entry = (getattr(config, "fetched_bot_lists", {}) or {}).get(str(bot).strip().lower())
        # A secondary channel's own list, just merged in, reports ITS count -
        # entry["entry_count"] is the PRIMARY channel's, untouched by this
        # fetch, and would misreport what just actually arrived (#1240).
        if secondary and isinstance(entry, dict):
            # Not just kept_lists[_channel_marker_name(channel)]: a channel
            # that sub-splits INSIDE its own archive (its own "-VIDEO-" file,
            # say) does not keep that base name at all - the base marker's
            # own file can even turn out empty and be dropped, as happened
            # live (the console reported 0 files for a channel that had
            # really just arrived with thousands, because the one marker it
            # looked up by name was never stored). Sum every marker this
            # fetch actually touched - the ones tagged with THIS channel -
            # instead of assuming there is exactly one, named after it.
            count = sum(int(info.get("entry_count") or 0)
                       for info in (entry.get("lists") or {}).values()
                       if isinstance(info, dict) and info.get("channel") == channel)
        else:
            count = int((entry or {}).get("entry_count") or 0) if isinstance(entry, dict) else 0
        _tell_the_console(bot, "arrived", f"{bot}'s list arrived: {count:,} files")
        # Automatic grabbing starts this bot over (#967). Never raises into
        # a list that has already been stored.
        try:
            import list_grab
            list_grab.note_list_arrived(bot)
            if secondary:
                list_grab.note_secondary_channel_list_arrived(bot, channel)
        except Exception as err:
            print(f"[LIST-FETCH] Could not reset {bot}'s automatic grab record: {err}")
    else:
        _tell_the_console(bot, "unusable",
                          f"{bot}'s list could not be used" + (f": {reason}" if reason else ""))
    return result


def _hold_existing_list(extract_dir):
    """Move the list we already hold aside, and say where it went.

    _extract_and_locate_list_file() wipes extract_dir as its FIRST action, and
    that directory is not scratch space - it is where the list we are already
    serving lives. Every validation comes after: the size cap, is_zipfile(),
    the member checks, the plausible-list sniff, the line-count ceiling.

    So a re-fetch that turned out to be a RAR, an oversized archive, or a
    peer's error page destroyed a perfectly good list on its way to rejecting
    the replacement - and refetch_due_lists() runs unattended, so the operator
    would find the list simply gone.

    Same shape as update_list.py publishing through `final + ".new"`: build
    the new one somewhere else, and only replace the live one once it is known
    to be good.
    """
    held = extract_dir + ".previous"
    try:
        if os.path.exists(platform_compat.long_path(held)):
            shutil.rmtree(platform_compat.long_path(held), ignore_errors=True)
        if os.path.exists(platform_compat.long_path(extract_dir)):
            os.rename(platform_compat.long_path(extract_dir),
                      platform_compat.long_path(held))
            return held
    except OSError as err:
        # Not fatal, and deliberately not a refusal to fetch: the worst case
        # is the behaviour this function was added to improve on.
        print(f"[LIST-FETCH] Could not set the held list aside before "
              f"re-fetching ({err}); continuing without a rollback copy.")
    return None


def _release_held_list(held, extract_dir, succeeded):
    """Drop the held copy, or put it back."""
    if not held:
        return
    if succeeded:
        shutil.rmtree(platform_compat.long_path(held), ignore_errors=True)
        return
    try:
        if os.path.exists(platform_compat.long_path(extract_dir)):
            shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        os.rename(platform_compat.long_path(held),
                  platform_compat.long_path(extract_dir))
        print("[LIST-FETCH] The re-fetch was rejected; the list already held "
              "has been put back.")
    except OSError as err:
        print(f"[LIST-FETCH] Could not restore the previously held list "
              f"({err}); it is still on disk at {held!r}.")


def _process_fetched_list_zip_unlocked(bot, zip_path, channel=None, secondary=False):
    """The body of process_fetched_list_zip. Caller must hold _lock().

    Wraps the real work so that a rejected re-fetch leaves the list we were
    already serving exactly where it was - see _hold_existing_list(). The
    directory itself is _extract_dir_for(bot, channel, secondary) (#1240):
    the bot's own for an ordinary or primary-channel fetch, a directory
    outside it entirely for a confirmed secondary channel - see that
    function's docstring. Holding/releasing THAT directory (never the
    primary's) is what keeps a secondary channel's own re-fetch from ever
    touching the primary's files, the same safety _hold_existing_list()
    already gives the primary.
    """
    extract_dir = _extract_dir_for(bot, channel, secondary=secondary)
    held = _hold_existing_list(extract_dir)
    succeeded = False
    try:
        succeeded, reason = _install_fetched_list(bot, zip_path, extract_dir, channel=channel,
                                                   secondary=secondary)
        return succeeded, reason
    finally:
        _release_held_list(held, extract_dir, succeeded)


def _measure_extra_list(bot, marker, path):
    """Parse and index one NON-MAIN list, or None if it cannot be used.

    Same ceiling and the same courtesy parse the main list gets - a second
    list is no more trustworthy for being second - but a failure here returns
    None instead of failing the fetch. The main list is already stored by the
    time this runs, and losing a good list because a sibling was bad is the
    behaviour this whole change exists to end.
    """
    try:
        text_size = os.path.getsize(platform_compat.long_path(path))
    except OSError as err:
        print(f"[LIST-FETCH] Skipping {bot}'s '{marker}' list: {err}")
        return None
    if text_size > max_list_text_size():
        print(f"[LIST-FETCH] Skipping {bot}'s '{marker}' list: {text_size} "
              f"bytes, over the {max_list_text_size()}-byte ceiling.")
        return None

    # Streamed into the index rather than built in memory first (#1134); see
    # _install_fetched_list(). The first row is read here, so a list that
    # cannot be parsed at all is caught before anything is written.
    try:
        rows = list_mod.CountedRows(list_mod.iter_filelist_rows(
            platform_compat.long_path(path), str(bot).strip()))
        has_rows = rows.any_rows()
    except Exception as err:
        print(f"[LIST-FETCH] Skipping {bot}'s '{marker}' list: could not "
              f"parse it ({err}).")
        return None

    # A .txt with no request lines in it is a readme, a banner or a header -
    # not a catalogue. Keeping it would put a row in the sidebar that opens on
    # nothing, which is the noise this change is otherwise removing. The MAIN
    # list is exempt: it is the archive's identity, and an empty one is a fact
    # about that bot worth seeing rather than a file to ignore.
    if not has_rows:
        print(f"[LIST-FETCH] Skipping {bot}'s '{marker}' list: no entries in "
              f"it.")
        return None

    # Indexed under its own name, so the cross-list filter can say WHICH of a
    # bot's lists a match came from - and so re-fetching replaces that list's
    # rows rather than the whole bot's.
    list_index.index_bot_list(index_key(bot, marker), rows)
    try:
        # Every row, whether or not the index took them: CountedRows drains
        # what it did not, and raises what the list itself raised.
        entry_count = rows.total()
    except Exception as err:
        print(f"[LIST-FETCH] Skipping {bot}'s '{marker}' list: could not "
              f"parse it ({err}).")
        return None
    return {"list_path": path, "entry_count": entry_count,
            "file_name": os.path.basename(path)}


# Markers that name a pack list. A bot names its own files, so this is a
# recognition rather than a rule - "rar" is the convention every packer in this
# family follows, ours included.
_RAR_MARKERS = ("rar",)


def bot_publishes_a_rar_list(bot):
    """True if `bot` is known to pack whole folders on request.

    TWO INDEPENDENT SIGNALS, either one enough:

      * a RAR list of theirs is one of the lists we hold - the strongest kind
        of evidence there is, since it is their own list of the folders they
        will pack; and
      * they advertise one. irc.py has parsed the "@<nick>^ ... RAR folders"
        wording into known_bots since #133, and nothing outside the registry
        had ever read it.

    Used to decide how long to wait for an answer, NOT whether to ask. A bot
    can pack folders without either signal - we may simply never have seen the
    advert, and may hold only its main list - so refusing on this would take
    away something that works. Waiting a shorter time for a bot with no sign
    of packing anything costs nothing when we are wrong and half an hour of a
    fetch slot when we are right.
    """
    key = str(bot or "").strip().lower()
    if not key:
        return False

    entry = (getattr(config, "fetched_bot_lists", {}) or {}).get(key)
    held = entry.get("lists") if isinstance(entry, dict) else None
    if isinstance(held, dict):
        for marker, info in held.items():
            if str(marker).strip().lower() in _RAR_MARKERS:
                return True
            # mxrarserver's Folders list (#1209), by its file's name: as the
            # only list in a Folders-only archive it is the main one and has
            # no marker at all.
            if (isinstance(info, dict)
                    and _MX_FOLDERS_LIST_RE.search(str(info.get("file_name") or ""))):
                return True

    advert = runtime.known_bots.get(key)
    if isinstance(advert, dict):
        if advert.get("rar_folders") is not None or advert.get("rar_trigger"):
            return True
    return False


# How far into a list to look for its first request line. mxrarserver's
# banner is an operator-written header of a few dozen lines.
_TRIGGER_SCAN_LINES = 2000


def _first_request_token(path):
    """The word after "!" on the first request line of the list at `path`, or
    None. Reads no further than _TRIGGER_SCAN_LINES lines."""
    try:
        with open(platform_compat.long_path(path), "r", encoding="utf-8",
                  errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= _TRIGGER_SCAN_LINES:
                    return None
                text = list_mod.strip_control_codes(line).strip()
                if text.startswith("!") and len(text) > 1:
                    return text[1:].split(None, 1)[0]
    except OSError:
        return None
    return None


def _mx_list_trigger(kept_lists):
    """The trigger the rows of the first mxrarserver list among `kept_lists`
    are addressed to, or None - see _install_fetched_list()."""
    import dcc_fetch
    for info in kept_lists.values():
        if not list_mod.is_mxrarserver_list(info.get("file_name")):
            continue
        trigger = dcc_fetch._sendable_trigger(_first_request_token(info.get("list_path")))
        if trigger:
            return trigger
    return None


def index_key(bot, marker):
    """How one list is named in the search index and on the wire.

    The bare nick for a bot's MAIN list - the name it has always had, so an
    index written before archives could hold more than one still resolves -
    and "<nick>/<marker>" for the rest.
    """
    nick = str(bot).strip()
    return f"{nick}/{marker}" if marker else nick


def split_index_key(key):
    """(nick, marker) from an index key. The inverse of index_key()."""
    text = str(key or "").strip()
    nick, sep, marker = text.partition("/")
    return (nick, marker) if sep else (text, "")


def _install_secondary_channel_lists(bot, channel, extract_dir, list_path):
    """Merge a SECONDARY channel's archive into the bot's existing entry,
    rather than replace it (#1240).

    Called only when `secondary` is explicitly True - an entry
    for this bot exists, and this fetch's channel differs from the one its
    "" (main) marker came from. Every file this archive held - `list_path`,
    whatever _pick_list_file() called "main" for lack of anything better to
    call it, included - is stored as its OWN marker, named after `channel`
    rather than "" or whatever filename convention it happened to match, so
    it can never collide with or overwrite the primary channel's own markers.
    A sub-split within THIS one archive (its own "-RAR-" file, say) keeps its
    relative name, joined to the channel's: "video", "video-RAR".

    Only this channel's own previously-held markers are ever replaced or
    dropped by a fetch from it; every other marker in the entry - the primary
    channel's "" and anything else, and any OTHER secondary channel's - is
    left exactly as it was. (bool, reason), the same contract as
    _install_fetched_list().

    `channel_marker` is disambiguated against every OTHER marker already in
    the entry, compared case-insensitively (#1240 review): "#RAR" sanitises
    to the same name a filename-derived "RAR" marker already uses, "#rar"
    and "#RAR" differ only by case - which index_bot_list() folds away in
    the search index regardless of their (distinct) dict keys - and "#a|b"
    and "#a_b" both sanitise to "a_b" outright. Left alone, any of these
    would overwrite (in the dict, the index, or both) a marker that belongs
    to someone else entirely. See _disambiguate_marker()'s own docstring for
    why a hash of the CHANNEL, not of the name, is what breaks the tie.
    """
    store = _ensure_fetched_bot_lists()
    key = str(bot).strip().lower()
    previous = store.get(key) or {}
    kept_lists = dict(previous.get("lists") or {})
    if "" not in kept_lists and previous.get("list_path"):
        # A bot held from before #1209's multi-list-per-archive feature has
        # no "lists" dict at all - its one list lives directly in list_path/
        # entry_count on the entry itself. Backfilled here as the "" marker
        # so the loop just below (which only ever looks at kept_lists) still
        # finds and preserves it, exactly as if it had always been stored
        # this way - the alternative is losing it the moment this runs.
        kept_lists[""] = {
            "list_path": previous["list_path"],
            "entry_count": previous.get("entry_count") or 0,
            "file_name": os.path.basename(str(previous["list_path"])),
            "channel": previous.get("channel"),
        }
    this_channels_markers = {marker for marker, info in kept_lists.items()
                             if isinstance(info, dict) and info.get("channel") == channel}
    reserved_lower = {str(marker).lower() for marker in kept_lists
                      if marker not in this_channels_markers}
    channel_marker = _disambiguate_marker(_channel_marker_name(channel), channel, reserved_lower)

    signature = _current_channel_signature(bot, channel)
    fresh = {}
    for marker, path in pick_list_files(extract_dir, list_path):
        effective = channel_marker if not marker else f"{channel_marker}-{marker}"
        info = _measure_extra_list(bot, effective, path)
        if info:
            info["channel"] = channel
            info["advert_signature"] = signature
            fresh[effective] = info

    if not fresh:
        reason = f"no usable list in {channel}'s answer (empty, or could not be parsed)"
        print(f"[LIST-FETCH] {bot}'s fetch from {channel}: {reason}")
        return False, reason

    # This channel's own markers from before are replaced wholesale by what
    # it answered with just now - the same "switchable, not accumulating"
    # rule _install_fetched_list() always applied to the whole bot, now
    # scoped to just the one channel being re-fetched. A marker THIS channel
    # held before but did not reproduce this time (a sub-list it stopped
    # publishing) leaves the index, the same cleanup _install_fetched_list()
    # already does for a whole-bot replace.
    for marker in this_channels_markers - set(fresh):
        list_index.drop_bot(index_key(bot, marker))
    for marker in this_channels_markers:
        kept_lists.pop(marker, None)
    kept_lists.update(fresh)

    entry = dict(previous)
    entry["lists"] = kept_lists
    store[key] = entry
    db.save_fetched_bot_lists(dict(store))

    detail = ", ".join(f"{marker}: {info['entry_count']}" for marker, info in fresh.items())
    print(f"[LIST-FETCH] Stored {len(fresh)} list(s) from {bot}'s {channel} "
          f"({detail}), alongside what was already held for this bot.")
    return True, None


def _install_fetched_list(bot, zip_path, extract_dir, channel=None, secondary=False):
    """Extract, validate and publish one fetched list. (bool, reason).

    `channel` (#1232) is the channel this particular fetch actually went out
    in - dcc_fetch.py's dispatcher resolves and stamps it onto the row before
    the request is even sent, so by the time a fetch completes it is the real
    answer, not a guess. Stored on the entry so a later request for this bot
    (a file, a folder, a re-fetch) can use the same channel instead of
    dcc.channel_containing_user()'s plain "first channel we share" rule.

    `secondary` (#1240 review) is explicit, carried straight through from
    process_fetched_list_zip() - see its own docstring. Decides which of the
    two branches below runs; never re-derived from `channel` here.
    """
    # Normalised once, used everywhere below it matters.
    channel = (str(channel).strip() or None) if channel else None

    list_path, reason = _extract_and_locate_list_file(zip_path, extract_dir)
    if reason:
        print(f"[LIST-FETCH] Rejected list zip from {bot}: {reason}")
        return False, reason
    if list_path is None:
        reason = "no recognizable master-list .txt file was found inside the zip"
        print(f"[LIST-FETCH] {bot}'s list zip extracted, but {reason}.")
        return False, reason

    # Issue #76: nothing before this point bounds the number of LINES the
    # extracted text file contains, only the zip's own byte/member counts -
    # and every line becomes a permanently-retained dict below. Checked here,
    # on the real extracted size, before a single line is parsed.
    try:
        text_size = os.path.getsize(platform_compat.long_path(list_path))
    except OSError as err:
        reason = f"could not stat the extracted list file: {err}"
        print(f"[LIST-FETCH] Rejected list zip from {bot}: {reason}")
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return False, reason
    if text_size > max_list_text_size():
        reason = (f"the extracted list is {text_size} bytes, over the "
                  f"{max_list_text_size()}-byte ceiling for a real master "
                  f"list (raise MAX_LIST_TEXT_SIZE if your peers publish "
                  f"bigger ones)")
        print(f"[LIST-FETCH] Rejected list zip from {bot}: {reason}")
        shutil.rmtree(platform_compat.long_path(extract_dir), ignore_errors=True)
        return False, reason

    # A CONFIRMED SECONDARY CHANNEL (#1240) is never this bot's "main" list
    # by construction, however _pick_list_file() above happened to label the
    # one file it found - that label only ever meant "nothing else survived
    # the -rar-/-video- exclusion", not "this is the bot's default list", and
    # treating it as the real main would overwrite the PRIMARY channel's own
    # main list both on disk (it does not - see _extract_dir_for() - but in
    # config.fetched_bot_lists it still would) and in the search index (every
    # row indexed under the bare bot key, below, IS that bot's main list as
    # far as the cross-list filter is concerned). So this branches before any
    # of that happens, and merges every file this archive held into the
    # existing entry's "lists" dict instead, named after the channel rather
    # than main/rar/video - see _install_secondary_channel_lists()'s own
    # docstring for the rest.
    if secondary:
        return _install_secondary_channel_lists(bot, channel, extract_dir, list_path)

    # list.py opens this path directly and does not wrap it itself, so the
    # prefix goes on here - at the point of use, the same idiom dcc.py uses.
    # Without it, widening the write path above would only move the failure:
    # extraction would succeed and the parse would raise FileNotFoundError,
    # outside the extraction guard, on a file that is plainly there.
    #
    # This is the ONE courtesy parse - see process_fetched_list_zip()'s
    # docstring. The rows are only ever counted and written to the index, so
    # they are STREAMED into it (#1134): this built a dict per row with the
    # raw line in it and then a second dict per row, and held both lists for
    # the whole write - about 412 MB at 378k rows, where streaming peaks at
    # 143 MB. CountedRows counts them as the index takes them.
    rows = list_mod.CountedRows(list_mod.iter_filelist_rows(
        platform_compat.long_path(list_path), str(bot).strip()))

    # THE SEARCH INDEX (#133 step 5), written from the parse that was already
    # happening. The dashboard's filter bar searches every held list at once,
    # and re-reading the files to do it is out of reach rather than merely
    # slow - #133 measured ten held lists at about eleven seconds a keystroke.
    #
    # Deliberately here and not in a pass of its own: this walk of the whole
    # file is a cost already paid, and the rows are about to be discarded.
    #
    # Best-effort by design. index_bot_list() swallows its own failures and
    # returns 0, because an index that cannot be written costs the filter bar
    # and nothing else - the list is on disk, the browser still pages it, and
    # the next fetch tries again. A fetch that succeeded must not be reported
    # as failed over it.
    indexed = list_index.index_bot_list(str(bot).strip(), rows)
    # Taken AFTER the write, which is what consumed the rows. total() is
    # still every row in the list when the index took none of them
    # (unavailable) or stopped part-way: it drains the rest. A list that
    # could not be parsed raises here, as the up-front parse did.
    entry_count = rows.total()
    if indexed != entry_count:
        print(f"[LIST-FETCH] {bot}'s list was stored but only {indexed} of "
              f"{entry_count} entries reached the search index; the "
              f"cross-list filter may not show it until the next fetch.")

    # THE REST OF THE ARCHIVE. Everything above concerns the MAIN list, which
    # is the one this function has always handled and the one every existing
    # reader means. The others are kept beside it now rather than discarded.
    #
    # Their failures are not the fetch's failures: the main list is already
    # parsed, counted and indexed by this point, and a second file that is
    # oversized or unreadable costs that list alone. Reporting the whole fetch
    # as failed over it would throw away a list that is sitting there, correct.
    kept_lists = {"": {"list_path": list_path, "entry_count": entry_count,
                       "file_name": os.path.basename(list_path), "channel": channel,
                       "advert_signature": _current_channel_signature(bot, channel)}}
    for marker, path in pick_list_files(extract_dir, list_path):
        if not marker:
            continue
        info = _measure_extra_list(bot, marker, path)
        if info:
            info["channel"] = channel
            info["advert_signature"] = _current_channel_signature(bot, channel)
            kept_lists[marker] = info

    store = _ensure_fetched_bot_lists()
    previous = store.get(str(bot).strip().lower())
    # A SECONDARY CHANNEL'S OWN MARKERS, if this bot has any, are not this
    # fetch's to touch (#1240) - this whole function only ever regenerates
    # the CHANNEL this fetch itself used (None here means "unspecified",
    # which is still its own channel, distinct from a real one on record for
    # some other marker). _install_secondary_channel_lists() is the only
    # place a marker with a DIFFERENT channel is ever added or replaced; carry
    # every one of those forward untouched; a name this fetch wants to use is
    # never also a secondary-channel marker name (those are always prefixed
    # by a channel, see _channel_marker_name()), so there is nothing to
    # resolve a clash with.
    if isinstance(previous, dict):
        for marker, info in (previous.get("lists") or {}).items():
            if (marker not in kept_lists and isinstance(info, dict)
                    and info.get("channel")
                    and str(info["channel"]).strip().lower() != str(channel or "").strip().lower()):
                kept_lists[marker] = info
    store[str(bot).strip().lower()] = {
        "bot": str(bot).strip(),
        "fetched_at": time.time(),
        # The channel THIS fetch actually went out in (#1232), or None for a
        # fetch dispatched before this existed, or one whose channel could
        # not be resolved at all. Read by webserver.py to steer a later file
        # or folder request for this bot into the same channel its list
        # answers in, without the operator having to say so again.
        "channel": channel,
        # The plain, already-absolute path _pick_list_file() returned -
        # NOT long_path()-wrapped here. Every reader of this field (the parse
        # call just above, and get_fetched_bot_page() below) wraps it with
        # platform_compat.long_path() itself, at the point of use - the same
        # "wrap on use, not on store" idiom the rest of this module already
        # follows for `list_path`/`extract_dir`. Storing the plain path keeps
        # it portable to whatever wraps it next, rather than baking in
        # Windows' "\\\\?\\" prefix (a no-op on Linux, but still a form this
        # value should not permanently commit to).
        "list_path": list_path,
        "entry_count": entry_count,
        "source_zip": os.path.basename(zip_path),
        # WHAT THEY WERE ADVERTISING WHEN WE TOOK THIS COPY (#133).
        #
        # Freshness is "their advert then vs their advert now", never "their
        # advert vs our parsed row count": bots count differently - some
        # include the header lines, some count album rows separately - and an
        # off-by-a-few would leave a list permanently marked stale with
        # nothing actually wrong. Comparing a bot against its own earlier
        # claim has no such problem.
        #
        # Absent when they were not in the registry at fetch time (we can
        # fetch from a bot whose advert we have not seen yet), and that
        # absence is the honest answer rather than a zero - see
        # _advert_snapshot().
        "advert_when_fetched": _advert_snapshot(bot),
        # Not looked at yet (#926 item 6): the List Browser marks it "New"
        # until the operator opens it - mark_seen(). Set on EVERY fetch, so a
        # refreshed list is new again. An entry from before this has no
        # seen_at at all and is not marked: nothing says it is new.
        "seen_at": 0,
        # EVERY LIST THE ARCHIVE HELD, keyed by a short stable marker. The
        # main one keeps the empty marker and is also mirrored in list_path
        # and entry_count above - which is what every reader written before an
        # archive could hold more than one already means, so nothing migrates
        # and nothing that reads an entry today has to learn about this.
        #
        # Keyed on a MARKER, never the filename: a peer's list file carries a
        # date, so a filename key would make every re-fetch a new list -
        # orphaning the old one, growing the sidebar forever, and leaving the
        # freshness LED nothing stable to compare against.
        "lists": kept_lists,
    }
    # WHAT ITS ROWS ARE ADDRESSED TO (#1209). An mxrarserver bot answers to a
    # trigger of the operator's choosing, every row of its list begins
    # "!<trigger>", and in "request only" mode that list is the only place it
    # is ever said. Only for an mxrarserver list: any other bot's rows carry
    # the nick the list was built under, which is stale once it changes
    # nick, and requests to it keep going to the nick it has now.
    trigger = _mx_list_trigger(kept_lists)
    if trigger:
        store[str(bot).strip().lower()]["trigger"] = trigger

    # Persisted immediately, not on a timer: unlike the bot registry (updated
    # on every advert, throttled for exactly that reason), a list fetch
    # completing is already an infrequent, deliberate event. Without this, the
    # extracted files under FETCHED_FILES_DIR survived a restart untouched
    # while the daemon's memory of which bots they belonged to did not, and
    # the File Lists switcher went blank until the next fetch.
    db.save_fetched_bot_lists(dict(store))

    # A LIST THIS FETCH DID NOT KEEP LEAVES THE INDEX. The last copy may have
    # held one this archive does not - or holds empty, or over the ceiling -
    # and nothing removed its rows: not this, which indexes only the lists it
    # keeps, and not forget_bot(), which went by the markers this entry
    # names. They stayed for good, and as "<nick>/<marker>" they also match
    # the bare nick's `bot:"<nick>"` pre-filter in every search.
    # Compared as index names, which the index stores lower-cased: a marker
    # that only changed case is the list just indexed, not one to drop.
    old_lists = previous.get("lists") if isinstance(previous, dict) else None
    if isinstance(old_lists, dict):
        kept_names = {index_key(bot, marker).lower() for marker in kept_lists}
        for marker in old_lists:
            if index_key(bot, marker).lower() not in kept_names:
                list_index.drop_bot(index_key(bot, marker))

    if len(kept_lists) > 1:
        detail = ", ".join(f"{marker or 'main'}: {info['entry_count']}"
                           for marker, info in kept_lists.items())
        print(f"[LIST-FETCH] Stored {len(kept_lists)} lists from {bot}'s "
              f"archive ({detail}) - each parsed fresh from disk on view, "
              f"not retained in memory.")
    else:
        print(f"[LIST-FETCH] Stored a reference to {entry_count} entries from "
              f"{bot}'s fetched list ({os.path.basename(list_path)}) - parsed "
              f"fresh from disk on each view, not retained in memory.")
    return True, None


def get_fetched_bot_page(entry, offset, limit, search_words=None):
    """Issue #76, option 2's on-demand reader: given one
    config.fetched_bot_lists[...] entry (the dict process_fetched_list_zip()
    above builds - "bot", "fetched_at", "list_path", "entry_count",
    "source_zip"), read one page of its `list_path`. Unfiltered, from
    list.page_of_list_files() (#1128): a table of where each folder is, built
    once per version of the file, so a page parses only its own folders. It
    used to re-parse the whole file for every page - 11 s and 412 MB at
    378k rows. No rows are kept between calls, only the table. A search, or
    a file the table cannot answer for, re-parses the whole file as before
    via list.find_matching_entries() + list.entries_to_filelist_rows().

    `search_words`, when given, is passed straight through to
    find_matching_entries() - the same pre-split word list @find and the
    Search tab already build from a raw query, so "search this list" (#399's
    follow-up) means the same thing as every other search in this project
    rather than a second implementation of "contains".

    Returns (page_rows, total_folders, total_rows, row_capped, error):
    `error` is None on success, otherwise a short, human-readable string
    (e.g. the file having gone missing from disk since the fetch - an
    operator manually clearing data/fetched/, or some other bug entirely)
    and `page_rows`/`total`/`row_capped` are ([], 0, False). Never raises -
    the caller (webserver.build_fetched_bot_list_payload) turns a non-None
    `error` into an HTTP error response, the same "pure logic returns a
    result, the route just serialises it" shape as every other
    build_*_payload() function in webserver.py.

    `row_capped` (#477) is True when list.page_folder_groups()'s own
    FILELISTS_MAX_PAGE_ROWS safety valve is the reason this page came back
    with fewer folders than `limit` asked for - never when the page is
    merely the last, shorter one. See that function's own docstring: a page
    cut down to one outsized folder otherwise reads as the pager being
    broken rather than the valve doing its job.

    Five values, not the four this said until #477 - the same warning #232
    left here the first time this grew a value: a new caller written from
    the docstring alone would unpack it wrong.

    `offset`/`limit` are applied to the deduped row list, after re-parsing -
    the same slicing webserver.py applies to this bot's own list, so the two
    endpoints share one pagination contract even though only one of them
    shares this module's parsing code.

    Held under the same module-wide _lock() process_fetched_list_zip() uses
    around its own extract->parse->store sequence, for the read (the
    existence check and the parse below) - not just the dict lookups, which
    are plain, fast, GIL-atomic reads and stay unlocked either way. Without
    this, a same-bot re-fetch racing a read here is a genuine torn-read
    hazard, not a hypothetical one: _extract_and_locate_list_file() reuses the
    exact same list_path (list_extract_dir() keys only on the bot nick) and
    rewrites it via rmtree+open("wb") rather than write-then-rename, so a read
    that lands mid-rewrite can see a truncated file and silently return a
    wrong `total` instead of raising - no exception, no "file missing", just
    a plausible-looking short page. Taking the same lock here makes such a
    read block briefly until the in-progress fetch finishes, instead of
    reading a half-written file - the same "one thing touches the on-disk
    representation at a time" guarantee process_fetched_list_zip() already
    gives writers, extended to readers.

    The folder table's own lock (runtime.list_folder_table_lock, #1128) is
    taken inside this one, here and in the two places that drop tables under
    it, and never the other way round: the own list's pages take it alone.

    No deadlock risk: this is the only other place in the codebase that
    acquires this lock, dcc_fetch.py's call into process_fetched_list_zip()
    happens with no other lock held (see _handle_completed_list_fetch()'s
    docstring), and nothing this function calls (list_mod.find_matching_entries,
    entries_to_filelist_rows) ever acquires config.fetched_bot_lists_lock
    itself - so there is no cycle and no re-entrant acquisition of this
    plain, non-reentrant Lock.
    """
    bot = entry.get("bot", "?")
    list_path = entry.get("list_path")
    if not list_path:
        reason = f"no list file is on record for {bot}'s fetched list"
        print(f"[LIST-FETCH] {reason}.")
        return [], 0, 0, False, reason

    resolved_path = platform_compat.long_path(list_path)
    with _lock():
        if not os.path.exists(resolved_path):
            reason = (f"{bot}'s fetched list file is no longer on disk "
                       f"({os.path.basename(list_path)!r} is missing - it may have "
                       f"been cleared manually since the fetch); fetch the list again")
            print(f"[LIST-FETCH] {reason}.")
            return [], 0, 0, False, reason

        # Under the lock, the table's build and the stat that keys it too: a
        # same-bot refetch rewrites this path in place, and a table built
        # from a half-written file would be filed under the new file's key.
        # A crafted list cannot hold the lock long with it: its build stops
        # at the table's budget (list._folder_table_budget()) and the page is
        # read whole, as before the table. Building outside this lock would
        # not let a fetch install meanwhile either: the build holds the
        # table lock, and an install drops tables under that lock while it
        # holds this one.
        if not search_words:
            answer = list_mod.page_of_list_files(
                [resolved_path], offset, limit, bot,
                max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS)
            if answer is not None:
                page, total_folders, total_rows, row_capped = answer
                return page, total_folders, total_rows, row_capped, None
        try:
            entries, _total = list_mod.find_matching_entries(
                search_words or [], limit=None, list_path=resolved_path)
            rows = list_mod.entries_to_filelist_rows(entries, bot)
        except OSError as err:
            # Caught here, not left to propagate into the Flask route: a file
            # that exists (the check above passed) but became unreadable between
            # that check and this open() - permissions changed, a network mount
            # dropped - is the same class of "gone since the fetch" problem as
            # the missing-file case above, just caught a moment later.
            reason = f"could not read {bot}'s fetched list file: {err}"
            print(f"[LIST-FETCH] {reason}")
            return [], 0, 0, False, reason

    # Grouped and paged by FOLDER, the same contract as this bot's own list -
    # see list.FILELISTS_MAX_PAGE_ROWS for why a folder count alone is not a
    # sufficient bound.
    groups = list_mod.group_rows_by_folder(rows)
    page, total_folders, total_rows, row_capped = list_mod.page_folder_groups(
        groups, offset, limit, max_rows=list_mod.FILELISTS_MAX_PAGE_ROWS)
    return page, total_folders, total_rows, row_capped, None


def forget_bot(bot):
    """Remove everything held for `bot`'s fetched list: the registry entry,
    the extracted files on disk, and its rows in the cross-list search index.

    True if there was an entry to remove, False if `bot` was not held at all
    - the caller's own decision about whether to ask (is this bot offline,
    is a fetch for it in flight) happens before this is ever called; this
    function only does the removing, unconditionally, once asked.

    Same lock as process_fetched_list_zip()/get_fetched_bot_page(): a forget
    racing a fetch that is about to replace the same entry must not interleave
    with either the dict write or the directory rewrite.
    """
    name = str(bot).strip().lower()
    if not name:
        return False

    with _lock():
        store = _ensure_fetched_bot_lists()
        entry = store.pop(name, None)
        if entry is None:
            return False
        db.save_fetched_bot_lists(dict(store))
        # Its folder tables go with it (#1128): nothing can page this list
        # again, and they would only hold a place among the few kept.
        list_mod.forget_folder_tables(under=list_extract_dir(bot))

    # Off the lock: a slow rmtree on a network-mounted FETCHED_FILES_DIR must
    # not hold up an unrelated fetch that only needs the dict, and the entry
    # is already gone from the dict either way - nothing left can read it
    # back mid-delete.
    # Wrapped AT the call, not stored wrapped - the module's own "wrap on use"
    # idiom, and tests/test_list_fetch.py asserts it with an AST walk over
    # every rmtree() in this file, so a hoisted variable fails that guard.
    extract = list_extract_dir(bot)
    shutil.rmtree(platform_compat.long_path(extract), ignore_errors=True)
    # ignore_errors hides a directory that would not go - a file held open on
    # Windows, a permission problem on a mounted share - and reporting
    # "purged" with the files still there is the one outcome an operator
    # cannot act on. The entry still goes either way: leaving a row nobody can
    # remove is worse than leaving files somebody can delete by hand.
    if os.path.exists(platform_compat.long_path(extract)):
        print(f"[LIST-FETCH] Forgot {bot}'s list, but {extract!r} could not "
              f"be removed - delete it by hand to reclaim the space.")

    # EVERY LIST THE ARCHIVE HELD, not just the main one. _measure_extra_list()
    # indexes each further list under index_key(bot, marker) - "<nick>/<marker>"
    # - so dropping the bare nick alone leaves the films/series rows behind
    # forever, pointing at files this call has just deleted.
    #
    # search() answers only from lists currently held, so nothing wrong is
    # returned from them. It is above all a DISK problem, and the whole point
    # of purging - the index runs roughly as large
    # again as the lists it describes, so on a multi-list bot the leak is most
    # of the space the purge just claimed to free.
    #
    # By every name in the index under this nick, not by the markers the
    # entry names: a list an earlier refetch left out is not in them, and
    # its rows outlived the Forget too. That also covers an entry written
    # before an archive could hold more than one list, which has no "lists"
    # key at all.
    list_index.drop_every_list_of(bot)

    real_nick = entry.get("bot", bot) if isinstance(entry, dict) else bot
    print(f"[LIST-FETCH] Forgot {real_nick}'s fetched list.")
    return True


def purge_fetched_list(source):
    """Forget one bot, named by any of its rows in the List Browser.

    The removing itself is forget_bot()'s, and the "is a fetch in flight"
    question is dcc_fetch.has_any_outstanding_request()'s - both landed in
    #388 for the bulk purge, and there is no reason for either to exist twice.
    What is here is only what a PER-LIST purge needs and a bulk one does not:
    working out which bot a clicked row belongs to, and refusing the rows that
    are not a fetched list at all.

    Returns (ok, detail).

    THE WHOLE BOT, not one list. `fetched_bot_lists` is keyed by nick and a
    single entry carries every list that bot's archive held - and they came out
    of one zip into one directory, so there is no per-list thing to remove even
    if the store were shaped for it. A "<nick>/<marker>" source is therefore
    resolved to its nick rather than refused: the row the operator clicked is a
    list, the thing that can be deleted is the bot.

    Refused for our OWN lists, which are not fetched from anywhere and whose
    files are the library itself. `__own__` reaching this function at all would
    mean a UI bug, so it answers rather than assuming.
    """
    import dcc_fetch

    text = str(source or "").strip()
    if not text:
        return False, "No list was named."
    if text == "__own__" or text.startswith("__own__:"):
        return False, "That is one of your own lists, not a fetched one."

    nick, _marker = split_index_key(text)
    key = nick.strip().lower()
    if not key:
        return False, "No list was named."

    if key not in _ensure_fetched_bot_lists():
        return False, f"Nothing is held from {nick}."

    # A fetch in flight will write into the very directory being removed, and
    # there is no cancellation path for a transfer thread already running -
    # the same reason build_fetch_delete_result() refuses an in-flight row. So
    # the answer is "not now", not a race.
    #
    # This includes a PENDING fetch, deliberately unlike
    # build_fetch_delete_result(), which allows a pending row to be deleted.
    # Different questions: deleting a pending row removes the thing that would
    # have started, while purging leaves it queued and pointed at a directory
    # that has just gone - it would recreate what was purged a moment later,
    # which reads as the purge having silently failed.
    if dcc_fetch.has_any_outstanding_request(nick):
        return False, (f"{nick} has a fetch in progress. Wait for it to "
                       f"finish, then purge.")

    if not forget_bot(nick):
        return False, f"Nothing is held from {nick}."

    # Removed by hand: automatic list grabbing (#926) must not fetch it
    # straight back. The bulk purge of offline bots does not do this - that
    # is tidying, not an answer about the bot.
    import list_grab
    list_grab.note_removed_by_hand(nick)

    # forget_bot() answers a bool and logs the detail, which is right for the
    # bulk purge that calls it in a loop. A per-list purge has one status line
    # to fill and an operator watching it, so it asks the question again here
    # rather than reporting a success the disk does not agree with.
    if os.path.exists(platform_compat.long_path(list_extract_dir(nick))):
        return True, (f"Removed {nick} from the list browser, but some files "
                      f"could not be deleted - see the log.")
    return True, f"Purged everything held from {nick}."
