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
import platform_compat

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
            # Shared with queue_mgr.py's queue_worker - see runtime.OutboundPacer.
            # A debug line can be paced slower than ordinary traffic (a bigger
            # DEBUG_MSG_DELAY than MSG_DELAY), but never faster: the two used to
            # sleep on separate, unrelated clocks, and the server only ever saw
            # their sum. Reserved before the send for the same reason the other
            # lane does it before, not after: a failed send still costs its slot.
            # The floor is the point, and the setting's help now says so
            # (#650): a DEBUG_MSG_DELAY below MSG_DELAY - the shipped 0.5
            # was one - changed nothing, while three texts described it as
            # the debug channel's own pace. 0 means "the same as MSG_DELAY".
            runtime.outbound_pacer.wait_for_slot(
                max(config.MSG_DELAY, getattr(config, 'DEBUG_MSG_DELAY', 0)))
            try:
                irc_sock.sendall(msg.encode("utf-8", errors="ignore"))
            except Exception as send_err:
                print(f"[DEBUG SEND ERROR] Could not write debug line: {send_err}")
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


# THE FEED AS FIELDS (#550, step 2). send_debug() carries prose; a client
# that wants to draw a window wants the nick, the size, the count. Every
# feed emitter calls feed_event(kind, text, **fields), which sends the prose
# through send_debug() exactly as before AND hands the fields to these
# sinks - a second registry so the (text, category) contract every existing
# sink relies on is untouched. adminchat's structured session is the first
# taker; the shape is client-agnostic on purpose.
_event_sinks = []


def add_event_sink(sink):
    with _debug_sinks_lock:
        if sink not in _event_sinks:
            _event_sinks.append(sink)


def remove_event_sink(sink):
    with _debug_sinks_lock:
        if sink in _event_sinks:
            _event_sinks.remove(sink)


def feed_event(_kind, _text, **fields):
    """One feed event, told twice: the prose to send_debug() under the kind
    as its category, and the fields to every event sink. The console
    tickboxes (console_wants) and the console switch itself
    (DEBUG_TO_CONSOLE) gate the fields exactly as they gate the prose, so
    a kind the operator unticked - or a console they switched off -
    reaches no client either way.

    The two positionals are underscored so no field can collide with them:
    REQUEST carries a field called `kind` ("file" or "folder"), and a plain
    `kind` parameter made that call a TypeError - caught by a test that
    expected a dispatch and saw none.
    """
    # Counted before anything can refuse the line: what the bot has seen since it
    # started is a fact about the bot, whatever a client chooses to show (#754).
    try:
        import runtime
        if _kind in ("FAIL", "SEARCH"):
            runtime.feed_counts[_kind] = runtime.feed_counts.get(_kind, 0) + 1
    except Exception:
        pass
    send_debug(_text, category=_kind)
    # DEBUG_TO_CONSOLE was checked on the prose path alone (#678, audit
    # L14): with it off the plain console went quiet as documented, while
    # a structured session kept getting every REQUEST/QUEUED/SENDING/SENT/
    # FAIL/SEARCH line - only LOG stopped - and the stdout floor printed
    # the same event as undelivered at the same time.
    if not getattr(config, "DEBUG_TO_CONSOLE", True) or not console_wants(_kind):
        return
    with _debug_sinks_lock:
        sinks = _event_sinks[:]
    for sink in sinks:
        try:
            sink(_kind, dict(fields), _text)
        except Exception as sink_err:
            print(f"[ANNOUNCE] Event sink raised and was dropped: {sink_err}")


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

# ==========================================================================
# THE TWO LINES A THEME IS ACTUALLY JUDGED BY
#
# Lifted out of the advert loop and the completion notice so the dashboard can
# render exactly what the channel will see - see webserver.theme_preview().
# A preview built from a COPY of these templates would drift the first time one
# of them changed, and would then tell the operator a lie with a straight face.
# Being the same function is the only thing that keeps it true.
#
# Pure: every value is an argument, nothing is read from config here except
# the palette, and nothing is sent. The callers below still gather the figures
# exactly as they did.
#
# BETWEEN THEM THEY USE ALL SIX ROLES, which is why there are two samples and
# not one. The advert never uses `accent` - only the completion notice does, on
# its "[as of ...]" - so an advert-only preview would leave one of the six
# settings with no visible effect, which is the problem a preview exists to
# solve.
# ==========================================================================

def build_advert_line(channel, nickname, file_count, total_size, list_date,
                      slots, queued, speed, record, total_sent, version,
                      settings=None):
    """The periodic channel advert, as one outbound PRIVMSG line.

    `settings` is passed straight to theme.blocks() - see there. The advert
    loop never passes it; the dashboard's preview does, so it can show colours
    that have been typed and not yet saved.
    """
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks(settings)
    return (
        f"PRIVMSG {channel} :"
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Type: {V}@{nickname}{R}{BG_TEXT_BOX} For My List Of: {A}{file_count}{R}{BG_TEXT_BOX} Files ({total_size}) created {V}{list_date} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Slots: {slots} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Queued: {queued} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Speed: {speed} / Record: {record} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Total Sent: {total_sent} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Search: {V}ON{R}{BG_TEXT_BOX} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} {version} {BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
    )


def build_transfer_complete_line(channel, user, shown_name, total_sent,
                                 yesterday, today, at_time, speed,
                                 settings=None):
    """The notice posted when a send finishes, as one outbound PRIVMSG line.

    The central block theme, an exact copy of the channel advert's framing.
    `settings` as in build_advert_line().
    """
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks(settings)
    return (
        f"PRIVMSG {channel} :"
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} {V}Sent{BG_TEXT_BOX}: {shown_name} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} To: {V}{user} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Total Sent: {V}{total_sent} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Yesterday: {A}{yesterday} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Today: {A}{today} {X}[as of {at_time}] "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Speed: {V}{speed} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
    )


def send_transfer_complete(channel, user, file_name, file_size, start_time, actual_speed, duration=None):
    """Send the block-styled transfer notice once a file has finished.

    `duration` (seconds) is optional and only feeds the structured SENT
    event (#550); the notice's own speed figure comes from `actual_speed`.
    """
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

    # None means the transfer was over before it could be timed - see
    # stats_mgr.speed_is_measurable(). Saying so is the honest answer, and
    # this line goes into the channel: a figure nobody should act on is worse
    # published than absent.
    if actual_speed is None:
        speed_str = "n/a (<1s)"
    elif actual_speed > 0:
        speed_str = stats_mgr.format_speed(actual_speed)
    else:
        speed_str = "0k/s"
    current_time_str = time.strftime("%I:%M %p").lower().lstrip("0")
    
    def _build(shown_name):
        return build_transfer_complete_line(
            channel, user, shown_name, total_sent_str, yesterday_str,
            today_str, current_time_str, speed_str)

    # The filename is the only unbounded field here and it comes straight off the disk. A
    # long classical track name pushed this line past 512 bytes and the server truncated it
    # mid-colour-code, so the channel saw the announcement smear into background colour.
    # THE ONLY PUBLIC MESSAGE A TRANSFER PRODUCES, and the only one an
    # operator may not want. The other three - the queue position, the
    # "Sending" notice and the DCC handshake itself - are private to the
    # person who asked, so they are not covered by this and turning it off
    # does not make a request go unanswered.
    #
    # The gate is around the CHANNEL send alone. The debug line below still
    # goes out: an operator who does not want the channel told is not an
    # operator who wants their own log to stop saying what was sent, and the
    # figures this function reads are used by both.
    if getattr(config, "ANNOUNCE_TRANSFERS", True):
        msg = fit_irc_line(_build, file_name)
        if oserve:
            oserve.queue_message("channel_announce", msg)
        print(f"[ANNOUNCE] Queued the block transfer complete notice for {channel}, "
              f"user {user} ({speed_str})")
    else:
        print(f"[ANNOUNCE] Transfer complete for {user} ({speed_str}) - the "
              f"channel notice is off (ANNOUNCE_TRANSFERS).")

    # The closing line, using the live 'speed_str' safely
    try:
        safe_file = str(file_name)
        # category SENT, not INFO. It was INFO from the start, so the [SENT]
        # tag the channel line has rendered since #526 never fired for the
        # one line it was for, and #528's "sends" tickbox never governed it.
        feed_event("SENT", f"Sent: \"{safe_file}\" to {user} [{speed_str}]",
                   nick=user, channel=channel, bytes=file_size, seconds=duration,
                   bytes_per_s=actual_speed, name=file_name)
    except Exception as debug_err:
        print(f"[DEBUG-SENT ERROR] Could not send the closing notice to the debug channel: {debug_err}")

def send_dcc_sending_notice(user, file_name, path=None, channel=None):
    """Send the user a matching private NOTICE when a transfer starts or is queued.

    `path` is optional and only feeds the structured SENDING event's size
    (#550): the notice itself never needed it, and a caller without it in
    hand reports 0 rather than guessing.
    """
    import os
    import sys
    import defaults as config
    oserve = sys.modules.get('oserve')

    # The console feed (#528): every dispatch path calls this the moment a
    # slot is taken, so it is the one place a "transfer started" line can
    # come from without a copy per path. The slot figure is what the
    # dispatcher just made it - this transfer is already counted.
    busy = len(getattr(config, "active_transfers", []) or [])
    slots = getattr(config, "MAX_DCC_SLOTS", 0)
    size = 0
    if path:
        try:
            size = os.path.getsize(platform_compat.long_path(path))
        except OSError:
            size = 0
    feed_event("SENDING", f'Sending "{file_name}" to {user} (slot {busy}/{slots})',
               nick=user, channel=channel, slot=busy, slots=slots, bytes=size, name=file_name)
    
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
            f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Sending: {shown_name} "
            f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Status: {V}Active Transfer Started "
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
                # #435: bound HERE, once per cycle, not 80-odd lines down
                # inside the per-channel loop. oserve has no `global`
                # declaration and no module-level binding in this file, which
                # makes it a plain local to announce_worker() - and a local
                # that is only ever ASSIGNED inside the per-channel loop is
                # unbound everywhere ABOVE that loop until some channel
                # reaches it. The rejoin block a few lines down reads it
                # before that point on every cycle, raising
                # UnboundLocalError - caught by its own broad except and
                # printed as one line, so the rejoin was silently skipped on
                # cycle 1 of every worker start (every reconnect), and
                # skipped FOREVER on an install where every channel hits one
                # of the two `continue`s before the loop ever reaches the old
                # binding site (no list bound, or "No List"/"Error" - a fresh
                # install before its first !update).
                oserve = sys.modules.get('oserve')

                # Through the accessor rather than a fourth hand-rolled split.
                # config.CHANNEL is None for part of every rehash - defaults.py
                # re-executes its literals before settings.conf is re-applied -
                # and `None.split(",")` is an AttributeError on the advert
                # thread. is_ready gates this loop during a rehash, but is_ready
                # is itself a module global the same reload rebinds.
                import irc
                channels_to_spam = irc.configured_channels()
                # The sampling itself now lives in stats_mgr.live_speed(), so the
                # dashboard can show the same figure without reimplementing it or
                # importing the daemon to get at dcc.queue_lock. It caches for a
                # second, which is what stops two callers stealing each other's
                # measurement window - see its docstring.
                speed_bytes_per_sec = stats_mgr.live_speed()
                speed_str = stats_mgr.format_speed(speed_bytes_per_sec)

                # ASK TO COME BACK, at the moment we were about to speak
                # there anyway. An instant rejoin after a kick reads as a
                # fight with whoever did it, and is how a kick becomes a ban;
                # the advert interval is slow enough to be polite and is
                # already the bot's own rhythm, so it needs no clock of its
                # own. Suggested exactly this way from a live channel.
                #
                # irc.py owns the rule and the counting - this only sends what
                # it is told to, so the advert worker holds no lock and knows
                # nothing about kicks.
                try:
                    import irc as irc_mod
                    for waiting in irc_mod.channels_to_rejoin():
                        if oserve:
                            oserve.queue_message(
                                "channel_announce", f"JOIN {waiting}\r\n")
                            print(f"[REJOIN] Asking to rejoin {waiting}.")
                except Exception as rejoin_err:
                    print(f"[REJOIN ERROR] Could not attempt a rejoin: {rejoin_err}")

                # Once per cycle, not per channel, and through the same
                # module that owns the rule - see channels_we_are_out_of().
                try:
                    import irc as irc_mod
                    out_of = irc_mod.channels_we_are_out_of()
                except Exception as membership_err:
                    print(f"[ANNOUNCE] Could not read which channels we are in: {membership_err}")
                    out_of = set()

                for chan in channels_to_spam:
                    chan = chan.strip()
                    if not chan:
                        continue
                    # Not in there (#631): kicked, refused, or never
                    # confirmed. The server would answer 404 to both lines,
                    # nothing reads that, and each costs a pacer slot the
                    # channels the bot IS in are waiting for. The rejoin
                    # above keeps asking for as long as it is allowed to.
                    if chan.lower() in out_of:
                        continue

                    # #432: one channel's failure must cost that channel, not
                    # the whole cycle. Before this, an exception anywhere in
                    # the body below (get_total_queued_count() raising on an
                    # unlocked dict mutation was the one actually seen, but
                    # any of them would do it) escaped this for-loop entirely,
                    # skipped every channel after the one that failed, AND
                    # skipped the time.sleep(ANNOUNCE_INTERVAL) below - so the
                    # outer except caught it, slept 10s instead of the
                    # configured interval, and restarted from the first
                    # channel: channels before the failure point got a
                    # duplicate advert 10s early, channels after it got
                    # nothing that cycle.
                    try:
                        # THIS CHANNEL'S OWN LIST (#26). The loop already read the
                        # figures once per channel; it just read the same ones every
                        # time. Now each channel advertises the list it actually
                        # serves - a count and a size from another channel's library
                        # is a claim nobody there can act on.
                        #
                        # None means no list is bound here and the primary is not
                        # the catch-all, which is #26's "a channel with no list
                        # bound gets no advert". The advert is where that rule is
                        # most visible: a bot silently present in a channel it does
                        # not serve, rather than one announcing a library it will
                        # refuse to send from.
                        import library
                        wanted = library.list_name_for_request(chan)
                        if wanted is None:
                            continue

                        # Read the live figures at this exact moment
                        file_count, list_date, total_size, raw_bytes = list.get_file_count_date_size_and_raw_bytes(wanted)

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
                        #
                        # #433: "Error" is the SECOND sentinel this same function
                        # can answer with - an OSError reading any list file
                        # (a permission change, a vanished path, or another
                        # process holding one open with no sharing) collapses
                        # its whole return to (0, "Error", "0B", 0). "Error" !=
                        # "No List", so this check alone let it straight through:
                        # every channel got "For My List Of: 0 Files (0B) created
                        # Error" plus a CTCP SLOTS line claiming 0 files and 0
                        # bytes, every ANNOUNCE_INTERVAL, into every other bot's
                        # registry too.
                        if list_date in ("No List", "Error"):
                            continue

                        formatted_count = f"{file_count:,}"

                        # oserve is now bound once, at the top of this cycle
                        # (see the #435 comment there) - no re-lookup needed.
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

                        # #434: the one outbound template with no budget
                        # enforcement - announce.py's other four all go
                        # through fit_irc_line(). A large library in a long
                        # channel name (or a CUSTOM_THEME_* override, which
                        # interpolates each role 8-9 times) can push this
                        # past IRC_LINE_BUDGET; a recipient with a long
                        # enough hostmask then has the SERVER cut the line,
                        # and the cut can land inside a colour code, smearing
                        # the background to the end of the line - exactly
                        # what fit_irc_line() exists to prevent. total_sent
                        # is what gives way: it costs the most (it only ever
                        # grows) and informs the least of anything on the
                        # line - an operator watching this channel already
                        # saw it, in full, in every earlier advert.
                        announce_msg = fit_irc_line(
                            lambda ts: build_advert_line(
                                chan, config.NICKNAME, formatted_count, total_size,
                                list_date, slots_str, queued_str, speed_str,
                                record_str, ts, config.SCRIPT_VERSION),
                            total_sent_str)

                        if oserve:
                            oserve.queue_message("channel_announce", announce_msg)

                        raw_stats_bytes = stats_mgr.get_total_sent_bytes()
                        sent_mbs = int(raw_stats_bytes / 1024 / 1024)

                        ctcp_payload = f"SLOTS {config.MAX_DCC_SLOTS} {free_slots} {queue_status} {queued_count} 999 {int(speed_bytes_per_sec)} {file_count} {raw_bytes} {fails_count} {sent_mbs} {raw_stats_bytes} {config.SCRIPT_VERSION}"
                        ctcp_msg = f"PRIVMSG {chan} :\x01{ctcp_payload}\x01\r\n"
                        if oserve:
                            oserve.queue_message("channel_announce", ctcp_msg)
                    except Exception as chan_err:
                        print(f"[ANNOUNCE ERROR] Could not advertise to {chan}: {chan_err}")
                        continue

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
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} Search Result: {V}ON "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Found: {A}{match_count} Match(es) For {V}{shown_term} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Sending: {A}{sending_count} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Slots: {V}{free_slots}/{config.MAX_DCC_SLOTS} Free "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Queued: {V}{queued_count} "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
        )
    

    # search_term is supplied by any user in the channel and is only checked for a MINIMUM
    # length, so a long one pushed this private notice past 512 bytes and the server cut it
    # mid-colour-code.
    msg = fit_irc_line(_build, search_term)
    if oserve:
        # The requester's OWN lane, in front of the rows that follow it - not
        # the shared VIP lane. This is a private message to one user, and its
        # rows are queued in that user's lane by the caller; the two lanes take
        # strict turns (queue_mgr), so a header sitting in the VIP lane behind
        # the channel advert's lines came out AFTER its own results: seen live
        # while an advert cycle was running, "Found: 3 Match(es)" arrived last.
        oserve.queue_message(user, msg)
    print(f"[SEARCH RESULTS] Found {match_count} sending {sending_count} to {user} in {channel} for '{search_term}'")

def send_dcc_error(user, error_type):
    """Send the standard DCC error messages to the user."""
    oserve = sys.modules.get('oserve')
    errors = {
        "invalid_path": "Error: Invalid path.",
        "file_not_found": "Error: File not found.",
        "busy": "Error: Busy looking up other files - try again in a moment.",
        "global_full": f"Error: The server's global queue is full ({config.MAX_GLOBAL_QUEUE} max).",
        "user_full": f"Error: You have reached your personal queue limit of {config.MAX_USER_QUEUE} files.",
        "rar_disabled": "Error: Folder packing (!rar) is disabled on this bot.",
        "not_configured": "Error: This bot's music library is not configured yet - ask the operator to set it up.",
        "ambiguous_list": "Error: That folder name is served by more than one of this bot's lists - request it in the channel it was advertised in.",
    }
    msg_text = errors.get(error_type, "Error: Unknown transfer issue.")
    msg = f"NOTICE {user} :{config.C_BOLD}{msg_text}{config.C_RESET}\r\n"
    if oserve:
        oserve.queue_message(user, msg)

def send_dcc_queue_notice(user, file_name, position, channel=None):
    """Send the user their queue position privately, in the same colour theme."""
    import sys
    import defaults as config
    oserve = sys.modules.get('oserve')

    # The console feed (#528): both request paths (a file, a !rar folder)
    # come through here when the request queues rather than sends.
    busy = len(getattr(config, "active_transfers", []) or [])
    slots = getattr(config, "MAX_DCC_SLOTS", 0)
    feed_event("QUEUED", f'Queued "{file_name}" for {user} at #{position} ({busy}/{slots} slots busy)',
               nick=user, channel=channel, pos=position, busy=busy, slots=slots, name=file_name)
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


# The two severities a notice can have, and the whole vocabulary on purpose.
#
#   "warning" - it happened, and it is over. Kicked and rejoined; a fetch that
#               used up its retries. Worth knowing, nothing to do right now.
#   "error"   - it is still true. Gave up rejoining; the list will not build.
#               Something is not working until somebody acts.
#
# A third level would be a level nobody can tell apart from the other two at a
# glance, which is the only moment a badge gets read.
NOTICE_SEVERITIES = ("warning", "error")

# WHAT EARNS A NOTICE, written down once (#708, audit L44). record_notice()
# is reached only through send_debug(notice=...), by the call sites that
# know they are one - and that relied on every author remembering, with no
# list of the intended events anywhere and a help text that promised
# "disconnects" when no code path raised one. Each entry names the event,
# the module that emits it and a piece of the line it emits, so a test can
# find the emitter and check it passes notice=; a new kind of notice is
# added here and there together.
NOTICE_EVENTS = (
    ("a list rebuild failed",           "commands.py", "External update_list.py failed",   "error"),
    ("a list rebuild stalled",          "commands.py", "The library it",                    "error"),
    ("a list rebuild timed out",        "commands.py", "Script execution timed out",       "error"),
    ("activated with channels missing", "irc.py",      "channel(s) never confirmed via NAMES", "error"),
    ("rejoined after a kick",           "irc.py",      "Rejoined {back.group(1)}",         "warning"),
    ("kicked from a channel",           "irc.py",      "Kicked from {kicked_chan}",        "warning"),
    ("the server allows fewer channels", "irc.py",     "more than the server",             "error"),
    ("a join was refused",              "irc.py",      "Attempt {count}/{limit}",          "error"),
    ("gave up rejoining",               "irc.py",      "gave up after {count} attempt(s)", "error"),
    ("the connection was lost",         "irc.py",      "Lost the connection to the IRC server", "warning"),
)

NOTICES_MAX = 200


def record_notice(text, severity="warning"):
    """Add one thing the operator should be told about, and return it.

    NOT A LOG. send_debug() already writes everything to the Console and the
    debug channel, and everything is what makes a log useful and a badge
    useless. This is the short list of events meaning the bot's ability to do
    its job changed - so it is written to explicitly, by the handful of call
    sites that know they are one, rather than inferred from a category.
    Inferring would be wrong in both directions: BAN and MUTE are DCCore
    banning USERS, which is routine and would flood the badge, while the
    things worth surfacing all sit under INFO.

    Ids are monotonic and never reused, because the dashboard remembers the
    highest one it has shown and asks "anything above this". A count would
    break the moment two events arrived between polls.
    """
    import time

    if severity not in NOTICE_SEVERITIES:
        severity = "warning"

    with runtime.notices_lock:
        highest = config.notices[-1]["id"] if config.notices else 0
        entry = {"id": highest + 1, "at": time.time(),
                 "severity": severity, "text": str(text)}
        config.notices.append(entry)
        # Oldest first, and the cap is on the LIST rather than on age: an
        # operator who has not looked in a month should still find the last
        # two hundred rather than an empty panel that once held something.
        while len(config.notices) > NOTICES_MAX:
            config.notices.pop(0)

    try:
        import db
        db.save_notices(config.notices, config.notice_state)
    except Exception as err:
        # A notice that cannot be written down is still worth showing until
        # the process ends. Never let persistence take the event with it.
        print(f"[NOTICE] Could not save notices: {err}")
    return entry


# How many unanswered messages are kept. Smaller than NOTICES_MAX on purpose:
# a notice is a thing that needs doing and is worth keeping a long backlog of,
# while a message somebody sent last month is a conversation that has moved on
# without us.
PRIVATE_MESSAGES_MAX = 50

# The last time each sender had a message recorded, for the cooldown. RAM
# only, deliberately - the point of it is to stop one person filling the panel
# in one sitting, and an operator restarting the bot is entitled to see that
# somebody is still trying.
_pm_last_recorded = {}


def record_private_message(nick, text):
    """Somebody sent the bot something it does not understand. Returns the
    entry, or None if it was throttled.

    NOT A REPLY, and this must never become one. The bot stays silent: an
    unrecognised private message gets no answer, because a bot that responds
    to every stray line is one that can be made to flood itself off the
    network - which is why the rate limiter upstream exists. This only writes
    it down.

    THROTTLED PER SENDER. Somebody typing four lines because the first got no
    answer is one person trying to ask something, not four events; recording
    all four buries the next person who tries. The first is kept and the rest
    are dropped for PRIVATE_MESSAGE_COOLDOWN_SECONDS.

    The text is kept, not just a count. "Three people messaged you" is not
    something an operator can act on; "can you send me the new album" is - and
    the message was addressed to their bot, by somebody who wanted something
    from it.
    """
    import time

    name = str(nick or "").strip()
    body = str(text or "").strip()
    if not name or not body:
        return None

    key = name.lower()
    now = time.time()
    cooldown = getattr(config, "PRIVATE_MESSAGE_COOLDOWN_SECONDS", 300)
    last = _pm_last_recorded.get(key, 0)
    if cooldown and (now - last) < cooldown:
        return None
    _pm_last_recorded[key] = now

    with runtime.private_messages_lock:
        highest = (config.private_messages[-1]["id"]
                   if config.private_messages else 0)
        entry = {"id": highest + 1, "at": now, "nick": name,
                 # Trimmed, not summarised. A message far longer than this is
                 # somebody pasting, and the first part of it says what they
                 # wanted just as well.
                 "text": body[:400]}
        config.private_messages.append(entry)
        while len(config.private_messages) > PRIVATE_MESSAGES_MAX:
            config.private_messages.pop(0)

    try:
        import db
        db.save_private_messages(config.private_messages,
                                 config.private_message_state)
    except Exception as err:
        print(f"[PM] Could not save the private messages: {err}")
    print(f"[PM] {name} sent something the bot does not answer; recorded.")
    return entry


def _decline_text_for(nick):
    """The line to send, or "" for send nothing.

    An operator who blanks PRIVATE_MESSAGE_DECLINE_TEXT has asked for a bot
    that is completely silent to strangers - no record AND no reply - which is
    a real third position and wants no special setting of its own.
    """
    template = str(getattr(config, "PRIVATE_MESSAGE_DECLINE_TEXT", "") or "").strip()
    if not template:
        return ""
    # "the bot's owner" rather than ADMIN_NICK's None, which would otherwise
    # reach a stranger as the literal word None and teach them nothing.
    admin = str(getattr(config, "ADMIN_NICK", "") or "").strip() or "the bot's owner"
    return template.replace("%admin", admin)


def decline_private_message(nick, now=None):
    """Tell somebody, once, that this bot does not keep private messages.

    Only reached when PRIVATE_MESSAGES_ENABLED is off. Returns True if a reply
    was actually queued.

    THIS IS THE ONLY PART OF THE FEATURE THAT PUTS A LINE ON THE WIRE, and an
    auto-reply to anyone who messages you is the classic way to be flooded off
    a network by strangers - so it has four brakes, each covering something
    the others do not:

    1. A NOTICE, never a PRIVMSG. RFC 1459 forbids a client auto-replying to a
       NOTICE, it is what every other user-facing answer here uses, and - the
       decisive one - irc.py's own parser matches PRIVMSG alone, so two bots
       running this cannot answer each other into a loop. That protection is
       structural rather than a check somebody can forget to write.
    2. Once per sender per PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS, persisted,
       so a restart is not a way to make the bot repeat itself.
    3. PRIVATE_MESSAGE_DECLINE_BURST across every sender together, which the
       per-sender rule cannot give: two hundred nicks messaging at once are
       two hundred FIRST messages, all of them individually owed a reply.
    4. Queued on the ordinary lane, never VIP - so it waits behind the
       transfer notices people are actually waiting on, and goes out one
       outbound_pacer slot apart like everything else the bot says.
    """
    import sys
    import time

    now = time.time() if now is None else now
    name = str(nick or "").strip()
    if not name:
        return False
    text = _decline_text_for(name)
    if not text:
        return False

    key = name.lower()
    interval = getattr(config, "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS", 86400)
    burst = getattr(config, "PRIVATE_MESSAGE_DECLINE_BURST", 20)
    burst_window = getattr(config, "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS", 600)

    with runtime.private_messages_lock:
        declined = config.private_message_state.setdefault("declined", {})
        # None, not 0. A sender who has never been told is not a sender who
        # was told at the epoch, and with any injected clock smaller than the
        # interval the second reading silences the bot completely.
        last = declined.get(key)
        if interval and last is not None and (now - float(last)) < interval:
            return False

        # Pruned to the window before it is measured, so the ceiling is
        # "this many recently" and not "this many ever".
        sends = config.private_message_decline_sends
        sends[:] = [when for when in sends if (now - float(when)) < burst_window]
        if burst and len(sends) >= burst:
            print(f"[PM] {name} messaged a bot that does not accept private "
                  f"messages, but {len(sends)} replies have already gone out "
                  f"in the last {burst_window}s - staying quiet.")
            return False
        sends.append(now)

        declined[key] = now
        # Anything past the interval can never stop a reply again, so keeping
        # it is keeping a fact that has stopped meaning anything.
        for old_name in [n for n, when in declined.items()
                         if interval and (now - float(when or 0)) > interval]:
            declined.pop(old_name, None)

    oserve = sys.modules.get("oserve")
    if oserve:
        oserve.queue_message(name, f"NOTICE {name} :{text}\r\n")
    try:
        import db
        db.save_private_messages(config.private_messages,
                                 config.private_message_state)
    except Exception as err:
        print(f"[PM] Could not save who has been told: {err}")
    print(f"[PM] {name} messaged a bot that does not accept private messages "
          f"- told once where to go instead.")
    return True


def unread_private_messages():
    """How many have arrived since the operator last looked."""
    with runtime.private_messages_lock:
        seen = int(config.private_message_state.get("seen_id", 0) or 0)
        return sum(1 for row in config.private_messages
                   if int(row.get("id", 0)) > seen)


def mark_private_messages_read():
    """Acknowledge everything recorded so far. Returns the id acknowledged."""
    with runtime.private_messages_lock:
        highest = (config.private_messages[-1]["id"]
                   if config.private_messages else 0)
        config.private_message_state["seen_id"] = highest
    try:
        import db
        db.save_private_messages(config.private_messages,
                                 config.private_message_state)
    except Exception as err:
        print(f"[PM] Could not save the private messages: {err}")
    return highest


def unread_notices():
    """(count, worst severity) above what the operator has acknowledged."""
    with runtime.notices_lock:
        seen = int(config.notice_state.get("seen_id", 0) or 0)
        fresh = [n for n in config.notices if int(n.get("id", 0)) > seen]
    worst = "error" if any(n.get("severity") == "error" for n in fresh) else (
        "warning" if fresh else "")
    return len(fresh), worst


def mark_notices_read():
    """Acknowledge everything recorded so far. Returns the id acknowledged."""
    with runtime.notices_lock:
        highest = config.notices[-1]["id"] if config.notices else 0
        config.notice_state["seen_id"] = highest
    try:
        import db
        db.save_notices(config.notices, config.notice_state)
    except Exception as err:
        print(f"[NOTICE] Could not save notices: {err}")
    return highest


# THE CONSOLE FEED (#528). Which Settings switch governs each event category
# on the console side, and which categories are the feed's own - new with
# #528, and kept off the IRC channel unless DEBUG_CHANNEL_FEED says otherwise.
#
# Only categories that name one KIND of event are here. PART, JOIN and QUIT
# are not: dcc.py and irc.py use them as "bad news" / "good news" tags for
# config errors, pack failures and rejoins as much as for presence, so a
# "presence" tickbox over them would silence errors. A category not in this
# table is never muted.
FEED_SWITCHES = {
    "REQUEST": "CONSOLE_SHOW_REQUESTS",
    "QUEUED":  "CONSOLE_SHOW_QUEUE",
    "SENDING": "CONSOLE_SHOW_SENDS",
    "RESUMED": "CONSOLE_SHOW_SENDS",
    "SENT":    "CONSOLE_SHOW_SENDS",
    "FAIL":    "CONSOLE_SHOW_FAILURES",
    "SEARCH":  "CONSOLE_SHOW_SEARCHES",
}
FEED_ONLY_CATEGORIES = frozenset({"REQUEST", "QUEUED", "SENDING", "RESUMED", "SEARCH", "LISTFETCH"})


def console_wants(category, config=None):
    """Does the operator want this category on the console? True for any
    category the Settings switches do not cover."""
    if config is None:
        import defaults as config
    switch = FEED_SWITCHES.get(str(category).upper())
    if switch is None:
        return True
    return bool(getattr(config, switch, True))


def channel_wants(category, config=None):
    """Does this category go to the IRC debug channel at all? The feed's own
    events only under DEBUG_CHANNEL_FEED; everything else as before."""
    if config is None:
        import defaults as config
    if str(category).upper() in FEED_ONLY_CATEGORIES:
        return bool(getattr(config, "DEBUG_CHANNEL_FEED", False))
    return True


def category_tag(category, palette=None):
    """(label, colour code) for one debug category - what the channel line's
    tag block and the admin console's `[TAG]` are both drawn from.

    `palette` is theme.blocks()'s tuple. send_debug() passes the one it has
    already unpacked, so this adds no second palette read to announce.py -
    tests/test_theme.py counts those against its golden fixture. A caller
    outside this module (adminchat) passes theme.blocks() itself.

    One table, read by both, because until #550 the console had no colours
    at all and the channel had this as an elif chain nobody else could
    reach. The label is not always the category: BAN renders [HARDBAN]
    (an admin confirming a !ban), and HARDBAN renders [SECURITY] (dcc.py's
    blocked path traversal or poisoned queue entry) - the two must not look
    alike, since one is routine administration and the other is someone
    probing the filesystem. MUTE is [MUTED] and TBAN [TEMPBAN] for the same
    reason: the 30-second mute and the escalation ban used to share a tag.
    The feed's own categories (#528) take the [SENT] block colour: they are
    the same story told from the start rather than the end.
    """
    import defaults as config
    if palette is None:
        raise TypeError("category_tag() needs the theme palette - pass theme.blocks()")
    _border, _sep, _box, _reset, _bold, value, alert, _accent = palette
    cat = str(category or "INFO").upper()
    table = {
        "SENT":    ("SENT", value),
        "REQUEST": ("REQUEST", value),
        "QUEUED":  ("QUEUED", value),
        "SENDING": ("SENDING", value),
        "RESUMED": ("RESUMED", value),
        "SEARCH":  ("SEARCH", value),
        "LISTFETCH": ("LISTS", value),
        "FAIL":    ("FAIL", alert),
        "PART":    ("PART", alert),
        "QUIT":    ("QUIT", config.C_PURPLE),
        "JOIN":    ("JOIN", config.C_CYAN),
        "BAN":     ("HARDBAN", alert),
        "HARDBAN": ("SECURITY", alert),
        "MUTE":    ("MUTED", config.C_PURPLE),
        "TBAN":    ("TEMPBAN", config.C_PURPLE),
    }
    return table.get(cat, ("INFO", config.C_GREY))


def send_debug(msg_text, category="INFO", notice=None):
    """Send a colour-block log line to the debug channel over a raw socket, undelayed.

    `notice` is the severity to ALSO record this as something the operator
    should be told about - "warning" or "error", or None for the overwhelming
    majority of lines, which belong in the log and nowhere else.

    Taken here rather than as a separate call so a caller cannot log an event
    and forget to raise it, or raise one and word it differently from the log
    line beside it. The SINK contract is unchanged - sinks still receive
    (text, category) - so the Console and the admin chat need to know nothing
    about any of this.
    """
    import sys
    import time
    import defaults as config
    
    if notice:
        record_notice(msg_text, notice)

    current_time = time.strftime("%H:%M:%S")
    
    # ---------------------------------------------------------------------
    # The extended block theme: bright white background, guarded against colour clashes
    # ---------------------------------------------------------------------
    BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X = theme.blocks()
    
    # 1. The opening block: the timestamp, framed in white
    msg = f"PRIVMSG {config.DEBUG_CHANNEL} :{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} [{current_time}] DEBUG "
    
    # 2. The tag block, colour-coded by event - the same label and colour the
    # admin console shows (category_tag(), below), so the two can never
    # disagree about what a category looks like.
    label, colour = category_tag(category, (BG_RED_BLOCK, BG_CYAN_BLOCK, BG_TEXT_BOX, R, B, V, A, X))
    tag_str = f"{colour}[{label}]{R}{BG_TEXT_BOX}"

    msg += f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Category: {tag_str} "
    
    # 3. The text block, stripped of any colour codes that would clash, and
    # 4. the closing block, ending the line with the colour separators -
    # rendered together through fit_irc_line() (#694, audit L30), as every
    # other outbound builder is. The framing is ~170 bytes on its own, and a
    # long folder name, hostmask or exception text pushed the line past what
    # the server relays: it was cut at 512 bytes, inside the text, with the
    # background colour smeared to the end and the closing block gone. The
    # text is shrunk with an ellipsis until the whole line fits, and a colour
    # code is never sliced.
    clean_text = msg_text.replace(config.C_BOLD, "").replace(config.C_RESET, "").replace("\x02", "").replace("\x0f", "")
    head = msg

    def _build(text):
        return (head + f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Log: {text} "
                + f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {R}\r\n")

    msg = fit_irc_line(_build, clean_text)
    
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

    # #424: DEBUG_CHANNEL ships blank ("no debug channel", per defaults.py's
    # own comment), and DEBUG_TO_CHANNEL defaults on regardless. Queuing
    # anyway sent "PRIVMSG  :<...>" - two spaces, no recipient - to the
    # server on a stock install, which the server silently rejects, so
    # `delivered` counted a send that never reached anyone and the stdout
    # floor below never fired. Same guard irc.py already applies before the
    # debug-channel JOIN, so a blank value is treated identically everywhere
    # it is used rather than needing to be re-learned at each call site.
    debug_chan = str(getattr(config, "DEBUG_CHANNEL", "") or "").strip()

    # THE FEED SWITCHES (#528) sit on top of the two routing switches. A
    # category the operator has unticked in Settings is not "undelivered" -
    # it is declined, so it must not fall through to the stdout floor either;
    # the floor is for a line nobody was there to take, not one nobody asked
    # for. The feed's own categories additionally stay off the channel unless
    # DEBUG_CHANNEL_FEED is on (see defaults.py for why that is the default).
    wanted_on_console = console_wants(category, config)

    if debug_chan and getattr(config, "DEBUG_TO_CHANNEL", True) and channel_wants(category, config):
        _debug_queue.append(msg)
        _ensure_debug_drain()
        delivered += 1

    if getattr(config, "DEBUG_TO_CONSOLE", True) and wanted_on_console:
        delivered += _fan_out_to_sinks(msg_text, category)

    if not delivered and wanted_on_console:
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
        f"{BG_RED_BLOCK} {BG_CYAN_BLOCK} {BG_TEXT_BOX} DCC-PACK: Access Denied "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} {BG_TEXT_BOX} Error: Artist root folders cannot be requested. Please select a specific album sub-folder. "
        f"{BG_CYAN_BLOCK} {BG_RED_BLOCK} \r\n"
    )
    
    try:
        # Send it straight out through oserve, if that module is loaded
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, msg, is_vip=True)
        else:
            irc_sock.sendall(msg.encode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"[DCC NOTICE ERROR] Could not send the colour-block error message: {e}")
