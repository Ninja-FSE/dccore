# announce.py - Channel adverts and notices, reached through sys.modules
import time
import os
import datetime
import threading
import sys
import collections
import defaults as config
import list
import dcc
import db
import runtime
import stats_mgr
import theme

is_ready = False

# IRC lines are capped at 512 bytes INCLUDING the CRLF, and the server prepends
# ":nick!ident@host " when relaying to the channel - which counts against the same 512 for
# every recipient. Worst case on Undernet: 1 + nick(12) + 1 + ident(10) + 1 + host(63) + 1
# = 89 bytes of prefix, leaving 512 - 89 - 2 = 421 for what we send. Real hostmasks are
# usually far shorter, so this is deliberately the pessimistic figure: trimming a filename
# ourselves with an ellipsis is always better than the server cutting mid-colour-code, which
# smears background colour to the end of the line in the recipient's client.
IRC_LINE_BUDGET = 420


def fit_irc_line(build, value, budget=IRC_LINE_BUDGET):
    """Render build(value), shrinking `value` until the encoded line fits `budget` bytes.

    The line is re-rendered from the template on every attempt rather than being cut at the
    end, so a colour code can never be sliced in half. Byte length is measured, not
    character length - non-ASCII artist and album names cost 2-3 bytes each.
    """
    line = build(value)
    if len(line.encode("utf-8", errors="ignore")) <= budget:
        return line

    text = str(value)
    while text and len(build(text + "...").encode("utf-8", errors="ignore")) > budget:
        text = text[:-1]

    trimmed = build(text + "..." if text else "...")
    if len(trimmed.encode("utf-8", errors="ignore")) > budget:
        # The template's FIXED part alone exceeds the budget, so no amount of shrinking the
        # variable helps. Return it rather than looping; the caller's line is structurally
        # too long and that is a separate bug, not something to hang on.
        print(f"[IRC LINE] Fixed template exceeds {budget} bytes even with an empty field.")
    return trimmed


def fit_irc_filename(build, filename, budget=IRC_LINE_BUDGET):
    """Render build(filename), shrinking the filename's STEM until it fits.

    fit_irc_line() marks what it cut with an ellipsis, which is right for a line
    a person reads and wrong for one a program consumes. A DCC SEND handshake's
    filename is the name the receiving client SAVES THE FILE AS: trimmed the
    other way it arrives as "Symphony No 9 in D mino..." with no extension, and
    lands as a file the operating system will not open. So the extension is kept
    and the stem is what gives way.

    Worth being clear about what this trades. A shortened save-name is a real
    cost - the receiver does not get the name the operator chose. It is the
    smaller cost: the fields the transfer actually needs (address, port, size)
    sit AFTER the filename in the handshake, so a line the server truncates
    loses those instead, and the receiver gets a handshake it cannot act on at
    all. One of the two has to give, and a file that arrives under a shorter
    name beats a file that does not arrive.
    """
    line = build(filename)
    if len(line.encode("utf-8", errors="ignore")) <= budget:
        return line

    stem, extension = os.path.splitext(str(filename))
    # splitext() already takes only the last dot, so this is a sanity check on
    # the result rather than a second parse: a trailing component that long is
    # not an extension, and preserving it would eat the budget it exists to save.
    if len(extension) > 10:
        stem, extension = str(filename), ""

    while stem and len(build(stem + extension).encode("utf-8", errors="ignore")) > budget:
        stem = stem[:-1]

    fitted = build(stem + extension)
    if len(fitted.encode("utf-8", errors="ignore")) > budget:
        # Same reasoning as fit_irc_line's own guard: the fixed part of the
        # template is over budget by itself, so no amount of shrinking the
        # filename helps and looping forever would be worse than saying so.
        print(f"[IRC LINE] A DCC filename cannot be fitted into {budget} bytes "
              f"even with nothing left of the name.")
    return fitted


# Debug lines are produced by the IRC READ THREAD (security.check_user_status runs for every
# PRIVMSG) and by transfer threads. They used to be written straight to the socket with a
# blocking time.sleep(0.5) held under a lock, which stalled whoever called it: 30 banned
# nicks resuming after a netsplit meant 15 seconds during which the read loop answered no
# PING, parsed no NAMES and dispatched no request. They are now queued here and drained by
# one background thread, so every caller returns immediately.
# commands.py:283 runs importlib.reload on this module for every !rehash, and reload
# re-executes the module body in the SAME module dict. A plain assignment here would throw
# away every queued-but-unsent line - including the "Rehash triggered by ..." notice appended
# one statement earlier - so the existing deque is reused when one is already present.
_debug_queue = globals().get("_debug_queue") or collections.deque(maxlen=200)

# Generation token, same idea as current_worker_id below. A reload resets _debug_drain_started
# to False, so the next send_debug would start a SECOND drain while the first kept running
# against the module global - N rehashes leaving N+1 pumps, each on its own timer, multiplying
# the outbound rate on the one shared socket. A superseded drain now retires itself.
_debug_drain_id = 0
_debug_drain_started = False
# Bound to runtime.py's object rather than constructed here, for the same reason
# dcc.queue_lock is: announce.py is reloaded by !rehash, and a fresh
# threading.Lock() on every reload could let two callers both pass the
# "already started?" check below at once and start two drain workers.
_debug_drain_guard = runtime.debug_drain_guard


def _debug_drain_worker(my_id):
    """Drain the debug queue at a steady pace, one line at a time."""
    while True:
        try:
            if _debug_drain_id != my_id:
                print("[DEBUG DRAIN] Superseded by a newer drain. Retiring quietly.")
                return

            if not _debug_queue:
                time.sleep(0.2)
                continue

            # Two gates, both required. The socket alone is not enough: irc.py publishes
            # oserve.irc_connection immediately after connect(), several seconds before the
            # debug channel has been joined, so draining on that signal alone would flush the
            # whole reconnect backlog into a window where the server rejects it - and those
            # are precisely the lines describing the outage.
            oserve_mod = sys.modules.get('oserve')
            irc_sock = getattr(oserve_mod, 'irc_connection', None)
            if not irc_sock or not getattr(config, 'bot_joined_channel', False):
                # Check BEFORE removing anything: popping first and discovering there is
                # nowhere to send is how reconnects silently ate queued messages.
                time.sleep(0.5)
                continue

            msg = _debug_queue.popleft()
            try:
                irc_sock.sendall(msg.encode("utf-8", errors="ignore"))
            except Exception as send_err:
                print(f"[DEBUG SEND ERROR] Could not write debug line: {send_err}")

            time.sleep(getattr(config, 'DEBUG_MSG_DELAY', 0.5))
        except Exception as drain_err:
            print(f"[DEBUG DRAIN ERROR] {drain_err}")
            time.sleep(1.0)


# ---------------------------------------------------------------------
# EXTRA DEBUG SINKS
#
# send_debug() has always written one place: the IRC debug channel. The admin
# console needs the same lines, so it registers itself here rather than every
# one of the 44 call sites learning about it.
#
# A sink receives the PLAIN text and the category, not the mIRC-wrapped channel
# line, because each destination wraps it differently.
#
# HARD RULE: a sink must not block and must not raise. send_debug is called from
# the IRC read thread, so a sink that waits on a socket would freeze the daemon's
# network loop - which is exactly why the console's sink only appends to a
# bounded queue. Raising is caught here regardless, because losing a log line is
# survivable and losing the read loop is not.
# ---------------------------------------------------------------------
_debug_sinks = []
# Bound to runtime.py's object, not constructed here - see _debug_drain_guard
# above. commands.py's rehash handler reads live sinks out under this same
# lock (announce_module._debug_sinks_lock) before the reload and reattaches
# them after; a fresh lock on every reload would let that read race a
# concurrent add_debug_sink()/remove_debug_sink() call from a live console
# session.
_debug_sinks_lock = runtime.debug_sinks_lock


def add_debug_sink(sink):
    with _debug_sinks_lock:
        if sink not in _debug_sinks:
            _debug_sinks.append(sink)


def remove_debug_sink(sink):
    with _debug_sinks_lock:
        if sink in _debug_sinks:
            _debug_sinks.remove(sink)


def _fan_out_to_sinks(msg_text, category):
    """Hand the line to every registered sink. Returns how many took it.

    The count is what lets send_debug tell "nobody was listening" from "the
    console has it", so a line is never lost in silence.
    """
    with _debug_sinks_lock:
        # Sliced, not list(). announce.py imports the project's own list module,
        # which shadows the builtin here - list(...) calls the MODULE.
        sinks = _debug_sinks[:]
    delivered = 0
    for sink in sinks:
        try:
            sink(msg_text, category)
            delivered += 1
        except Exception as sink_err:
            print(f"[ANNOUNCE] Debug sink raised and was dropped: {sink_err}")
    return delivered


def _ensure_debug_drain():
    global _debug_drain_started, _debug_drain_id
    if _debug_drain_started:
        return
    with _debug_drain_guard:
        if _debug_drain_started:
            return
        _debug_drain_id = time.time()
        threading.Thread(target=_debug_drain_worker, args=(_debug_drain_id,), daemon=True).start()
        _debug_drain_started = True


def format_size_human(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PB"

# The stats.txt helpers that used to live here (load_advanced_stats,
# save_advanced_stats, check_and_rotate_day) were duplicates of the ones in
# db.py and were called by nothing but each other - every real caller,
# including the two below, already goes through db. They are removed rather
# than left dormant because this copy still wrote with a truncating
# open(STATS_FILE, "w"), the exact bug db.save_advanced_stats was fixed to
# avoid: a crash mid-write leaves a short row, which the loader discards,
# resetting every counter to zero. Wiring them back in would undo that fix.

def send_transfer_complete(channel, user, file_name, file_size, start_time, actual_speed):
    """Send the block-styled transfer notice once a file has finished."""
    import sys
    import db
    import stats_mgr
    import time
    import defaults as config
    oserve = sys.modules.get('oserve')
    
    # Read the live counters from the store and the statistics module
    total_sent = stats_mgr.get_total_sent()
    total_sent_bytes = stats_mgr.get_total_sent_bytes()
    total_sent_str = f"{total_sent} Files ({stats_mgr.format_size_human(total_sent_bytes)})"

    # Yesterday's and today's figures. Guarded so the name cannot collide with list.py
    #
    # _rolled, not the raw row: the daemon only rotates the day when a transfer
    # COMPLETES, so a bot that has sent nothing since midnight still has
    # yesterday's numbers sitting in the Today columns. Reading them raw
    # announced yesterday's total as today's, and the day before's as
    # yesterday's - both wrong, in the direction that flatters the bot, into
    # every channel. The dashboard has read them rolled since #144.
    yesterday_str = "0 Files"
    today_str = "0 Files"
    try:
        stats = db.load_advanced_stats_rolled()
        # type(stats) == list is used deliberately, to avoid colliding with the list module
        if (type(stats) == list or type(stats).__name__ == 'list') and len(stats) > 6:
            yesterday_str = f"{str(stats[2])} Files"
            today_str = f"{str(stats[4])} Files"
    except Exception as e:
        print(f"[ANNOUNCE ERROR] The figures clashed in memory: {e}")

    speed_str = stats_mgr.format_speed(actual_speed) if actual_speed > 0 else "0k/s"
    current_time_str = time.strftime("%I:%M %p").lower().lstrip("0")
    
    # ---------------------------------------------------------------------
    # The central block theme, an exact copy of the channel advert's framing.
    # ---------------------------------------------------------------------
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    def _build(shown_name):
        return (
        f"PRIVMSG {channel} :"
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} {B}{V}Sent{B}{BG_TEXT_BOX}: {B}{shown_name}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} To: {B}{V}{user}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Total Sent: {B}{V}{total_sent_str}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Yesterday: {B}{A}{yesterday_str}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Today: {B}{A}{today_str}{B} {X}[as of {current_time_str}] "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Speed: {B}{V}{speed_str}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
        )
    

    # The filename is the only unbounded field here and it comes straight off the disk. A
    # long classical track name pushed this line past 512 bytes and the server truncated it
    # mid-colour-code, so the channel saw the announcement smear into background colour.
    msg = fit_irc_line(_build, file_name)
    if oserve:
        oserve.queue_message("channel_announce", msg)
    print(f"[ANNOUNCE] Queued the block transfer complete notice for {channel}, "
          f"user {user} ({speed_str})")

    # The closing line, using the live 'speed_str' safely
    try:
        safe_file = str(file_name)
        send_debug(f"Sent: \"{safe_file}\" to {user} [{speed_str}]", category="INFO")
    except Exception as debug_err:
        print(f"[DEBUG-SENT ERROR] Could not send the closing notice to the debug channel: {debug_err}")

def send_dcc_sending_notice(user, file_name):
    """Send the user a matching private NOTICE when a transfer starts or is queued."""
    import sys
    oserve = sys.modules.get('oserve')
    
    # ---------------------------------------------------------------------
    # Private notice block, framed exactly like the channel one
    # ---------------------------------------------------------------------
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    # Through fit_irc_line, like send_transfer_complete's sibling notice three
    # functions up. The filename is the only variable part and it comes off the
    # operator's own disk, so this is not attacker-reachable - but a long
    # classical track name still pushes the line past the budget, and the
    # server's cut lands inside a colour code, smearing the background to the
    # end of every reader's line. #162 finding #31.
    def _build(shown_name):
        return (
            f"NOTICE {user} :"
            f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Sending: {B}{shown_name}{B} "
            f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Status: {B}{V}Active Transfer Started{B} "
            f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
        )

    msg = fit_irc_line(_build, file_name)
    
    if oserve:
        oserve.queue_message(user, msg, is_vip=True)
    print(f"[ANNOUNCE] Queued the custom block notice for {user}, '{file_name}'")

def get_formatted_stats_strings():
    """The advert's figures, read from stats.txt without writing to it.

    _rolled rolls a COPY, so this is still read-only - it must not touch the
    dates on disk, and it does not. What it does fix is the labels: the day is
    only rotated on disk when a transfer completes, so a bot idle since
    midnight had yesterday's figures under Today and the day before's under
    Yesterday. Every five minutes, in every channel.
    """
    oserve = sys.modules.get('oserve')
    
    stats = db.load_advanced_stats_rolled()
    
    # Index 1 = Total bytes
    total_bytes_count = stats[1]
    
    # Index 0 = total files, index 2 = yesterday's files, index 4 = today's files
    total_str = f"{stats[0]:,} Files ({format_size_human(total_bytes_count)})"
    yesterday_str = f"{stats[2]} Files"
    
    time_now_str = time.strftime("%I:%M %p").lower()
    if time_now_str.startswith("0"):
        time_now_str = time_now_str[1:]
        
    today_str = f"{stats[4]} Files [as of {time_now_str}]"
    return total_str, yesterday_str, today_str

# Live traffic statistics, measured in real time by dcc.py
# The id of the thread currently entitled to run
current_worker_id = 0

def announce_worker():
    """The standalone timer thread for the channel advert; safe across reconnects."""
    global current_worker_id
    import time
    import sys
    import defaults as config
    
    # A unique id for this particular thread start, based on the current time
    my_worker_id = time.time()
    current_worker_id = my_worker_id
    
    print(f"[ANNOUNCE] Multi-channel announce worker started (Thread ID: {my_worker_id}).")
    
    while True:
        try:
            # If a reconnect has started a newer thread, this one shuts itself down
            if current_worker_id != my_worker_id:
                print(f"[ADVERT-STOP] The stale thread (id {my_worker_id}) is shutting itself down quietly.")
                break
                
            if is_ready:
                channels_to_spam = config.CHANNEL.split(",")
                # The sampling itself now lives in stats_mgr.live_speed(), so the
                # dashboard can show the same figure without reimplementing it or
                # importing the daemon to get at dcc.queue_lock. It caches for a
                # second, which is what stops two callers stealing each other's
                # measurement window - see its docstring.
                speed_bytes_per_sec = stats_mgr.live_speed()
                speed_str = stats_mgr.format_speed(speed_bytes_per_sec)

                for chan in channels_to_spam:
                    chan = chan.strip()
                    if not chan:
                        continue
                        
                    # Read the live figures at this exact moment
                    file_count, list_date, total_size, raw_bytes = list.get_file_count_date_size_and_raw_bytes()

                    # #229: get_file_count_date_size_and_raw_bytes() answers the
                    # sentinel "No List" as the DATE when no master list exists
                    # yet - a fresh install before its first !update. Unguarded,
                    # that string was interpolated straight into the advert
                    # ("...created No List"), published into every channel every
                    # ANNOUNCE_INTERVAL until the first list build finished.
                    # commands.py's -stats reply already guards the same
                    # sentinel; the advert - far more publicly visible - never
                    # had the same treatment. Skipped rather than reworded: an
                    # advert with nothing to announce is not useful chatter.
                    if list_date == "No List":
                        continue

                    formatted_count = f"{file_count:,}"
                    
                    oserve = sys.modules.get('oserve')
                    active_dl = oserve.active_downloads if oserve else 0
                    fails_count = oserve.send_fails_count if oserve else 0
                    

                    free_slots = max(0, config.MAX_DCC_SLOTS - active_dl)
                    queue_status = "NOW" if active_dl < config.MAX_DCC_SLOTS else "0"
                    queued_count = dcc.get_total_queued_count()
                    queued_str = f"{queued_count}"
                    
                    total_sent_str, yesterday_str, today_str = get_formatted_stats_strings()
                    slots_str = f"{free_slots}/{config.MAX_DCC_SLOTS}"
                    
                    import db
                    raw_record = db.get_speed_record()
                    record_str = stats_mgr.format_speed(raw_record) if raw_record > 0 else "0k/s"

                    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()

                    announce_msg = (
                        f"PRIVMSG {chan} :"
                        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Type: {B}{V}@{config.NICKNAME}{R}{BG_TEXT_BOX} For My List Of: {B}{A}{formatted_count}{R}{BG_TEXT_BOX} Files ({total_size}) created {V}{list_date} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Slots: {slots_str} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Queued: {queued_str} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Speed: {speed_str} / Record: {record_str} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Total Sent: {total_sent_str} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Search: {B}{V}ON{R}{BG_TEXT_BOX} "
                        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} {config.SCRIPT_VERSION} {BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
                    )

                    if oserve:
                        oserve.queue_message("channel_announce", announce_msg)
                    
                    raw_stats_bytes = stats_mgr.get_total_sent_bytes()
                    sent_mbs = int(raw_stats_bytes / 1024 / 1024)
                    
                    ctcp_payload = f"SLOTS {config.MAX_DCC_SLOTS} {free_slots} {queue_status} {queued_count} 999 {int(speed_bytes_per_sec)} {file_count} {raw_bytes} {fails_count} {sent_mbs} {raw_stats_bytes} {config.SCRIPT_VERSION}"
                    ctcp_msg = f"PRIVMSG {chan} :\x01{ctcp_payload}\x01\r\n"
                    if oserve:
                        oserve.queue_message("channel_announce", ctcp_msg)
                
                time.sleep(config.ANNOUNCE_INTERVAL)
            else:
                time.sleep(5)
                
        except Exception as loop_error:
            print(f"[CRITICAL ANNOUNCE ERROR] The thread hit an error: {loop_error}")
            time.sleep(10)


def send_search_result_header(user, search_term, match_count, channel):
    """Send the search header as a private message, in the colour-block style."""
    import sys
    import dcc
    import defaults as config
    oserve = sys.modules.get('oserve')
    
    active_dl = oserve.active_downloads if oserve else 0
    free_slots = max(0, config.MAX_DCC_SLOTS - active_dl)
    queued_count = dcc.get_total_queued_count()
    sending_count = min(match_count, config.MAX_SEARCH_RESULTS)
    
    # The block theme with the white text box.
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    def _build(shown_term):
        return (
        f"PRIVMSG {user} :"
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Search Result: {B}{V}ON{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Found: {B}{A}{match_count}{B} Match(es) For {B}{V}{shown_term}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Sending: {B}{A}{sending_count}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Slots: {B}{V}{free_slots}/{config.MAX_DCC_SLOTS}{B} Free "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Queued: {B}{V}{queued_count}{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
        )
    

    # search_term is supplied by any user in the channel and is only checked for a MINIMUM
    # length, so a long one pushed this private notice past 512 bytes and the server cut it
    # mid-colour-code.
    msg = fit_irc_line(_build, search_term)
    if oserve:
        oserve.queue_message("channel_announce", msg)
    print(f"[SEARCH RESULTS] Found {match_count} sending {sending_count} to {user} in {channel} for '{search_term}'")

def send_dcc_error(user, error_type):
    """Send the standard DCC error messages to the user."""
    oserve = sys.modules.get('oserve')
    errors = {
        "invalid_path": "Error: Invalid path.",
        "file_not_found": "Error: File not found.",
        "global_full": f"Error: The server's global queue is full ({config.MAX_GLOBAL_QUEUE} max).",
        "user_full": f"Error: You have reached your personal queue limit of {config.MAX_USER_QUEUE} files.",
        "rar_disabled": "Error: Folder packing (!rar) is disabled on this bot.",
        "not_configured": "Error: This bot's music library is not configured yet - ask the operator to set it up.",
    }
    msg_text = errors.get(error_type, "Error: Unknown transfer issue.")
    msg = f"NOTICE {user} :{config.C_BOLD}{msg_text}{config.C_RESET}\r\n"
    if oserve:
        oserve.queue_message(user, msg)

def send_dcc_queue_notice(user, file_name, position):
    """Send the user their queue position privately, in the same colour theme."""
    import sys
    oserve = sys.modules.get('oserve')
    if oserve:
        # The mIRC colour blocks and separators
        BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
        
        # Build the message in the colour-block style
        def _build(shown_name):
            # config.MAX_USER_QUEUE, not a literal 100. The operator can set
            # this to anything, and send_dcc_error()'s "user_full" message
            # twenty lines above already reads it properly - so the two
            # messages about the same limit disagreed whenever it was changed.
            text_content = (f"Added {shown_name} to your personal queue at "
                            f"position #{position} of {config.MAX_USER_QUEUE}.")
            block_msg = (f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} "
                         f"{text_content}{R} {BG_CYAN_BLOCK} {BG_RED_BLOCK} ")
            return f"NOTICE {user} :{block_msg}\r\n"

        result_msg = fit_irc_line(_build, file_name)
        oserve.queue_message(user, result_msg)


def send_debug(msg_text, category="INFO"):
    """Send a colour-block log line to the debug channel over a raw socket, undelayed."""
    import sys
    import time
    import defaults as config
    
    current_time = time.strftime("%H:%M:%S")
    
    # ---------------------------------------------------------------------
    # The extended block theme: bright white background, guarded against colour clashes
    # ---------------------------------------------------------------------
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    # 1. The opening block: the timestamp, framed in white
    msg = f"PRIVMSG {config.DEBUG_CHANNEL} :{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} [{current_time}] {B}DEBUG{B} "
    
    # 2. The tag block, colour-coded by event
    if category.upper() == "SENT":
        tag_str = f"{V}[SENT]{R}{BG_TEXT_BOX}"
    elif category.upper() == "PART":
        tag_str = f"{A}[PART]{R}{BG_TEXT_BOX}"
    elif category.upper() == "QUIT":
        tag_str = f"{config.C_PURPLE}[QUIT]{R}{BG_TEXT_BOX}"
    elif category.upper() == "JOIN":
        tag_str = f"{config.C_CYAN}[JOIN]{R}{BG_TEXT_BOX}"
    elif category.upper() == "BAN":
        # A red block label for PERMANENT bans
        tag_str = f"{A}[HARDBAN]{R}{BG_TEXT_BOX}"
    elif category.upper() == "HARDBAN":
        # dcc.py raises this for a blocked path traversal and for a poisoned queue entry -
        # the two most serious alerts the daemon can produce. Without this branch they fell
        # through to the grey [INFO] tag, visually identical to routine chatter, so a
        # filesystem probing campaign looked like ordinary traffic in the debug channel.
        #
        # Labelled [SECURITY], not [HARDBAN]: the "BAN" category above already renders
        # [HARDBAN], and that one is an admin confirming a !ban. These two must not look
        # alike - one is routine administration, the other is someone probing the filesystem.
        tag_str = f"{A}[SECURITY]{R}{BG_TEXT_BOX}"
    elif category.upper() == "MUTE":
        # Its own tag. A 30-second mute and the escalation ban both used
        # TBAN, so both rendered [TEMPBAN] and an operator watching the
        # console could not tell a slap from a sentence (#234).
        tag_str = f"{config.C_PURPLE}[MUTED]{R}{BG_TEXT_BOX}"
    elif category.upper() == "TBAN":
        # A purple block label for TEMPORARY day-bans
        tag_str = f"{config.C_PURPLE}[TEMPBAN]{R}{BG_TEXT_BOX}"
    else:
        tag_str = f"{config.C_GREY}[INFO]{R}{BG_TEXT_BOX}"
  
    msg += f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} {B}Category{B}: {tag_str} "
    
    # 3. The text block, stripped of any colour codes that would clash
    clean_text = msg_text.replace(config.C_BOLD, "").replace(config.C_RESET, "").replace("\x02", "").replace("\x0f", "")
    msg += f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Log: {clean_text} "
        
    # 4. The closing block, ending the line with the colour separators
    msg += f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {R}\r\n"
    
    # ---------------------------------------------------------------------
    # NON-BLOCKING HAND-OFF. This used to hold config.debug_flood_lock across a
    # time.sleep(0.5) and write the socket directly, so every caller paid 0.5s - including
    # the IRC read thread, which calls this once per denied PRIVMSG. The queue is bounded
    # (deque maxlen), so a flood of alerts drops the oldest lines instead of growing without
    # limit or stalling the daemon. sendall() in the drain replaces send(), whose return
    # value was discarded so a short write truncated the line silently.
    # ---------------------------------------------------------------------
    # ROUTING. Two switches, and a floor underneath them.
    #
    # DEBUG_TO_CHANNEL sends the coloured line to the debug channel, as always.
    # DEBUG_TO_CONSOLE hands the plain text to any attached admin console.
    #
    # Both default on, so nothing changes for anyone who does not touch them.
    # Turning the channel off is the point of the console: the operator stops
    # publishing the daemon's internals to a channel other people can sit in.
    #
    # The floor matters more than either switch. If the channel is off and no
    # console is attached, the line would simply cease to exist - and the moment
    # that is most likely is exactly when something has gone wrong and nobody is
    # connected. So when nothing took it, it goes to stdout, which the LXC
    # console and the journal always have. Only then: printing every line
    # unconditionally would double up on the 44 call sites that already log.
    # ---------------------------------------------------------------------
    delivered = 0

    if getattr(config, "DEBUG_TO_CHANNEL", True):
        _debug_queue.append(msg)
        _ensure_debug_drain()
        delivered += 1

    if getattr(config, "DEBUG_TO_CONSOLE", True):
        delivered += _fan_out_to_sinks(msg_text, category)

    if not delivered:
        print(f"[DEBUG {str(category).upper()}] {msg_text}")

# ---------------------------------------------------------------------
# The advert thread's entry point, called by irc.py on a successful boot
# ---------------------------------------------------------------------
is_ready = False

def start_announce_thread():
    """Start the background timer for the channel advert and the hidden SLOTS line."""
    import threading
    threading.Thread(target=announce_worker, daemon=True).start()
    print("[ANNOUNCE] The advert and debug timer started in the background.")

def send_pack_error_notice(irc_sock, user):
    """Send the user a private NOTICE, in the same colour theme, when a request is refused."""
    import defaults as config
    import sys
    
    # Take the colour codes from the existing structure
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    # Build the message inside the standard frame
    msg = (
        f"NOTICE {user} :"
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} DCC-PACK: {B}Access Denied{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Error: {B}Artist root folders cannot be requested. Please select a specific album sub-folder.{B} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
    )
    
    try:
        # Send it straight out through oserve, if that module is loaded
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, msg, is_vip=True)
        else:
            irc_sock.send(msg.encode())
    except Exception as e:
        print(f"[DCC NOTICE ERROR] Could not send the colour-block error message: {e}")
