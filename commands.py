# commands.py - User commands, mostly queue handling
import sys
import defaults as config
import db
import runtime

# The five admin handlers below take authorised=False. The DCC CHAT console
# passes authorised=True: a console session has already proved the operator's
# Undernet services login via their +x host and then a password, which is a
# STRONGER claim than this nick comparison, and re-checking the nick here would
# refuse them unless their current nick also happened to be in ADMIN_NICK. Every
# channel caller keeps the default and the nick check.


def is_admin(user, host=None):
    """Return True if `user` may run admin commands.

    Centralises a check that was duplicated across five handlers, so the eventual
    hostmask-based gate is a change in one place instead of five.

    Two deliberate changes from the copies this replaces:

    * The hardcoded `or user.lower() == "sysop"` fallback is gone. It made the literal nick
      "sysop" an admin regardless of what config.ADMIN_NICK was set to - an undocumented
      second account nobody could turn off. It is a no-op today because ADMIN_NICK is
      already "SysOp", so removing it changes nothing until that value is edited.
    * ADMIN_NICK may now be a comma-separated list, so a second operator can be added
      without reintroducing a hardcoded name.

    An Undernet nick is not owned without services auth: anyone can take it while the
    real admin is offline. So when ADMIN_HOSTMASKS is set, the nick alone is not enough
    (#579) - the sender's HOST must match one of those patterns too, the same test the
    DCC console applies. `host` is the "ident@host" half of the sender's prefix, as
    irc.parse_privmsg() returns it (a bare host works too); a caller that cannot say
    where the line came from passes nothing, and then it is refused - an authorisation
    check has one safe direction when it does not know. With ADMIN_HOSTMASKS empty
    (the default) nothing changes: the check is the nick, as it always was, and the
    channel commands can be switched off with ADMIN_CHANNEL_COMMANDS.
    """
    import defaults as config

    # The fallback is '' and not a nick, deliberately. This function's own
    # docstring above records removing `or user.lower() == "sysop"` because it
    # "made the literal nick sysop an admin regardless of what config.ADMIN_NICK
    # was set to - an undocumented second account nobody could turn off". A
    # default of 'SysOp' four lines later was the same account through a
    # different door: with the setting absent for any reason, that nick - which
    # anyone on Undernet can simply take - would hold every admin command.
    #
    # An authorisation check has one safe direction when it does not know the
    # answer, and it is to refuse.
    raw = getattr(config, 'ADMIN_NICK', '') or ''
    allowed = {n.strip().lower() for n in str(raw).split(',') if n.strip()}
    if str(user).lower() not in allowed:
        return False

    import adminchat
    if not adminchat.admin_host_patterns():
        return True
    # adminchat reads the host half of an ident@host; a bare host has none.
    if host and "@" not in str(host):
        host = "x@" + str(host)
    return adminchat.is_admin_host(host)


def handle_help_request(s, user, target):
    """Answer "@<nick>-help" with how to actually use this bot.

    The gap this closes: somebody sees the advert in a busy channel, and the
    advert has no room to explain anything beyond the trigger. Every other
    serving script on the network answers a help request; this one had no way
    to be asked.

    Answered by NOTICE to the person who asked, like every other user command
    here, so a stranger working out how the bot works costs the channel
    nothing.

    Sent as several short lines rather than one long one. IRC drops anything
    past 512 bytes including the prefix the server adds, and announce.py keeps
    a 420-byte budget for exactly that reason - a single help message would sit
    right on that edge and lose its tail on the nicks with the longest
    hostmasks, which is the least predictable way to break.

    What it lists follows what the bot actually serves: with
    config.RAR_ENABLED off, !rar is refused, so telling somebody to type it
    would be handing out the same instruction the album list stopped shipping.
    """
    oserve = sys.modules.get('oserve')
    if not oserve:
        return

    import list as list_mod

    nick = config.NICKNAME
    bold, red, reset = config.C_BOLD, config.C_RED, config.C_RESET

    lines = [
        f"I am a file server. To see what I have, type: {bold}{red}@{nick}{reset} "
        f"- I will send you my list as a {list_mod.list_format()} file.",
        f"To request a file, paste a line from that list, for example: "
        f"{bold}{red}!{nick} Artist - Song.mp3{reset}",
    ]

    if getattr(config, "RAR_ENABLED", True):
        lines.append(
            f"To request a whole album packed as one .rar, use the album list "
            f"that came with it: {bold}{red}!{nick} !rar Artist\\Album\\{reset}")

    lines.append(
        f"What I have and what I have sent: {bold}{red}@{nick}-stats{reset}. "
        f"What people request most: {bold}{red}@{nick}-top{reset}.")

    lines.append(
        f"Your queue: {bold}{red}@{nick}-que{reset} to see it, "
        f"{bold}{red}@{nick}-remove{reset} to cancel it. "
        f"To search every bot at once, type: {bold}{red}@find <words>{reset}")

    for line in lines:
        # #426: NOT is_vip=True. The VIP lane is strict-priority with no
        # aging - a channel advert waiting behind it is the reason it
        # exists, but being ahead of the advert does not require being
        # ahead of every OTHER user's own reply. A single user typing
        # -help repeatedly, well inside the ordinary flood limits, used to
        # be able to fill the 200-line VIP cap and starve config.send_queue
        # completely - @find results, "Preparing full list" and queue
        # position notices for everyone else stopped reaching anyone.
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n")

    print(f"[HELP] Sent the usage notice to {user} (asked in {target}).")


def handle_queue_check(s, user, target):
    """Count one user's queued files and answer them.

    #426: NOT the VIP lane, despite what this docstring used to claim - see
    handle_help_request()'s own comment for why a per-user reply does not
    belong in the lane channel adverts exist for.
    """
    user_key = user.lower()
    oserve = sys.modules.get('oserve')
    import list
    import dcc
    
    # 1. How many files this particular user has queued
    file_count = 0
    if hasattr(config, 'dcc_queue') and user_key in config.dcc_queue:
        file_count = len(config.dcc_queue[user_key])
        
    # 2. Live statistics - read ONLY where they are used (#457).
    #
    # These feed the fuller notice shown when somebody has nothing queued,
    # and nothing in the other branch touches them. They were fetched for
    # every -que regardless, and get_file_count_date_size_and_raw_bytes()
    # reads every published list file end to end - so the person who DOES
    # have files queued paid a full read of the library index for four
    # values that were then thrown away, on a command they are likely to
    # repeat while they wait.
    formatted_total_files = ""
    list_date = ""
    if file_count <= 0:
        file_count_total, list_date, total_size, raw_bytes = list.get_file_count_date_size_and_raw_bytes()
        formatted_total_files = f"{file_count_total:,}"
    
    active_dl = oserve.active_downloads if oserve else 0
    free_slots = max(0, config.MAX_DCC_SLOTS - active_dl)
    slots_str = f"{free_slots}/{config.MAX_DCC_SLOTS}"
    
    queued_count = dcc.get_total_queued_count()
    queue_str = f"{queued_count}/{config.MAX_QUEUE_LIMIT}" if hasattr(config, 'MAX_QUEUE_LIMIT') else f"{queued_count}"

    # 3. Pick the layout depending on whether the queue is empty
    if file_count > 0:
        # Layout when they do have files queued; only the number and trigger are bold
        msg = (
            f"NOTICE {user} :You have {config.C_BOLD}{config.C_RED}{file_count}{config.C_RESET} files in queue. "
            f"To remove your entire queue, type: {config.C_BOLD}{config.C_RED}@{config.NICKNAME}-remove{config.C_RESET} "
            f"or send CTCP: {config.C_BOLD}{config.C_GREEN}REMOVE{config.C_RESET}\r\n"
        )
    else:
        # The fuller layout: only numbers, the trigger and values are bold and coloured
        msg = (
            f"NOTICE {user} :"
            f"You have {config.C_BOLD}{config.C_RED}0{config.C_RESET} files in queue. "
            f"Download my list with {config.C_BOLD}{config.C_GREEN}@{config.NICKNAME}{config.C_RESET} "
            f"of {config.C_BOLD}{config.C_RED}{formatted_total_files}{config.C_RESET}. "
            f"Slots {config.C_BOLD}{config.C_GREEN}{slots_str}{config.C_RESET}. "
            f"Queue {config.C_BOLD}{config.C_GREEN}{queue_str}{config.C_RESET}. "
            f"List {config.C_BOLD}{config.C_RED}{list_date}{config.C_RESET}. "
            f"({config.SCRIPT_VERSION})\r\n"
        )

        
    if oserve:
        oserve.queue_message(user, msg)
    print(f"[COMMANDS] {user} checked their queue status ({file_count} files).")

def handle_queue_remove(s, user, target):
    """Clear the user's queue from memory and remove it from dcc_queue.txt on disk."""
    user_key = user.lower()
    oserve = sys.modules.get('oserve')
    import dcc

    removed_archives = []
    with dcc.queue_lock:
        # Remove them from the ordinary queue
        if hasattr(config, 'dcc_queue') and user_key in config.dcc_queue:
            # BEFORE dropping the rows: they are the only record that the temp
            # archives exist. The freeze sweep, the freeze timer and !clearqueue
            # all cleaned up here; this path - the one users actually type - did
            # not, so every archive it orphaned stayed in TMP_ZIP_DIR until
            # somebody noticed the disk filling.
            removed_archives = dcc.discard_orphaned_temp_archives(user_key)
            del config.dcc_queue[user_key]
            db.save_dcc_queue()  # Write the cleared queue straight to disk

        # Also drop them from the freezer, in case they were frozen
        if hasattr(config, 'frozen_queues') and user_key in config.frozen_queues:
            del config.frozen_queues[user_key]

    msg = f"NOTICE {user} :Your queue has been completely removed. \r\n"
    if oserve:
        oserve.queue_message(user, msg)
    if removed_archives:
        print(f"[COMMANDS] Removed {len(removed_archives)} orphaned temp archive(s) with {user}'s queue.")
    print(f"[COMMANDS] {user} removed their entire queue from the disk layout.")

def handle_admin_clear_queue(user, target_chan, msg_text, authorised=False, user_host=None):
    """Force-clear ANOTHER user's queue entirely - admin only (issue #15).

    For a ghost nick left behind by a netsplit or a reconnect.
    handle_queue_remove above is the version a user can run, and only ever
    against themselves; this gives the admin the same power over anyone, from
    one line of text, without editing dcc_queue.txt by hand.
    """
    import defaults as config
    import announce
    import db
    import dcc

    if not authorised and not is_admin(user, user_host):
        print(f"[SECURITY] Unauthorised user {user} tried to run !clearqueue.")
        return

    parts = msg_text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        announce.send_debug("Syntax error! Usage: !clearqueue <nick>", category="INFO")
        return

    target_nick = parts[1].strip()
    target_key = target_nick.lower()

    removed_count = 0
    was_frozen = False

    with dcc.queue_lock:
        if hasattr(config, 'dcc_queue') and target_key in config.dcc_queue:
            removed_count = len(config.dcc_queue[target_key])

            # Shared with @<nick>-remove and, in a cruder form, the two freeze
            # sweeps in dcc.py. Must run before the rows are dropped, because they
            # are the only record that these archives exist.
            for temp_path in dcc.discard_orphaned_temp_archives(target_key):
                print(f"[ADMIN CLEARQUEUE] Removed orphaned temp archive: {temp_path}")

            del config.dcc_queue[target_key]
            db.save_dcc_queue()

        if hasattr(config, 'frozen_queues') and target_key in config.frozen_queues:
            del config.frozen_queues[target_key]
            was_frozen = True

    if removed_count > 0 or was_frozen:
        extra = " (was frozen too)" if was_frozen else ""
        announce.send_debug(
            f"Admin {config.C_BOLD}{user}{config.C_RESET} force-cleared queue for {config.C_BOLD}{target_nick}{config.C_RESET}: {config.C_BOLD}{removed_count}{config.C_RESET} file(s) removed{extra}.",
            category="INFO")
        print(f"[ADMIN CLEARQUEUE] {user} force-cleared {target_nick}'s queue ({removed_count} files, frozen={was_frozen}).")
    else:
        announce.send_debug(
            f"Clearqueue: {config.C_BOLD}{target_nick}{config.C_RESET} had no queue or frozen entry to remove.",
            category="INFO")
        print(f"[ADMIN CLEARQUEUE] {user} tried to clear {target_nick}, but no queue or frozen entry was found.")

def diagnostics_are_for_the_admin(user, host=None):
    """Whether `user` may run the bot's diagnostics - !ping and !debugnames.

    They used to answer ANYONE in the channel. Seen live by the user: another
    operator typed !ping to check their own bot, and every DCCore in the
    channel ran a latency check, each spending a paced server line (the slot
    the adverts share) and each reporting into its own admin console - so
    one person's ping showed up in a stranger's console as if the stranger
    had asked. Neither command even answers the person who typed it: !ping
    reports only to the operator, !debugnames is a RAM-CHECK notice about the
    bot's own membership mirror. They are the operator's tools, so they
    answer the operator - the same is_admin() the admin commands use.

    Not !list: that is the discovery command every serving bot answers with
    its trigger, on purpose, and it stays public.
    """
    return is_admin(user, host)


def handle_ping_request(irc_sock, user, target_chan):
    """Start the timer and send a unique latency PING to the IRC server.

    Answers only the admin - see diagnostics_are_for_the_admin(). Checked
    here as well as at the dispatch in irc.py so that no other caller can
    make the bot ping on a stranger's behalf.

    #425: this used to write straight to the socket, the last responder in
    the dispatch chain that never touched the shared outbound clock #406
    installed - any nick can trigger it, and several clone connections doing
    so aggregate into unthrottled traffic the server sees as one burst.

    NOT routed through oserve.queue_message() the way the CTCP VERSION reply
    and the "!debugnames" RAM-CHECK notice were (see
    tests/test_a_shared_outbound_pace.py's TheThirdUnpacedWriterIsFixedToo):
    this command measures its own round-trip latency, and queueing the PING
    behind the VIP lane would fold however long it waited there into the
    number the command exists to report. A direct wait_for_slot() still puts
    the send on the one shared clock without touching the measurement's
    honesty - the timer starts only once the wait is over and the byte is
    about to go out, not before.
    """
    import time
    import defaults as config
    import runtime

    if not diagnostics_are_for_the_admin(user):
        return

    runtime.outbound_pacer.wait_for_slot(config.MSG_DELAY)

    # Keep the measurement in shared memory so the pong handler can read it later
    config.ping_start_time = time.time()
    config.ping_triggered_by = user
    config.ping_channel_source = target_chan

    # Send the probe straight to the server's raw socket
    try:
        irc_sock.sendall(b"PING :OSERVE_LATENCY_CHECK\r\n")
        print(f"[PING COMMAND] Latency measurement started by {user} in {target_chan}.")
    except Exception as e:
        print(f"[PING ERROR] Could not send the PING packet: {e}")

def handle_pong_response(category="INFO"):
    """Catch the server's reply, work out the latency to three decimals, and report it."""
    import time
    import defaults as config
    import announce
    
    start_time = getattr(config, 'ping_start_time', None)
    if start_time:
        # The value is kept in seconds rather than milliseconds
        latency_sec = time.time() - start_time
        
        trigger_user = getattr(config, 'ping_triggered_by', config.NICKNAME)
        source_chan = getattr(config, 'ping_channel_source', config.CHANNEL)
        
        # The :.3f format always shows exactly three decimals (e.g. 0.129)
        announce.send_debug(
            f"Latency Check triggered by {trigger_user} from {source_chan} -> IRC Server Response Time: {latency_sec:.3f} sec", 
            category=category
        )
        
        config.ping_start_time = None

# ---------------------------------------------------------------------------
# Runtime state that must survive a !rehash.
#
# Module level on purpose. This used to be a local inside
# handle_rehash_request(), which is part of why it fell out of date: a list
# that has to be updated whenever config.py gains a container is useless if
# nothing can see it. tests/test_commands.py now derives the set of
# containers from config.py and asserts each one is either in here or
# explicitly excused, so the next omission fails a test instead of silently
# emptying something on the next rehash.
# ---------------------------------------------------------------------------
# =====================================================================
# 1b. RUNTIME STATE THAT MUST SURVIVE THE RELOAD
# =====================================================================
# importlib.reload re-executes a module body, so every name config.py assigns in its
# "GLOBALT LIVE-MINNE" section is reset to an empty container. The block above already
# rescues dcc_queue and channel_users; everything else in that section was being
# silently destroyed on every !rehash.
#
# NOT preserved, deliberately - these are cleared ON PURPOSE and that behaviour is kept:
#   send_queue          - blanked below so stale text cannot collide after the reload
#   rar_inprogress      - the documented "lock-clearing rehash" escape hatch for a
#   user_processing_lock  packer that wedged; !rehash is the only way to clear them.
#                         Kept instead while the packer's thread is alive (#651) -
#                         see clear_or_keep_pack_interlocks().
PRESERVE_RUNTIME = (
    'feed_counts',        # failures and searches since the process started (#754); the
                          # panel's Since box would go back to zero on every Save
    'active_transfers',   # losing this reports 0 active slots while transfers run,
                          # so the bot admits work beyond MAX_DCC_SLOTS
    'banned_users',       # every timed ban silently released
    'frozen_queues',      # freeze timers lost, so departed users' queues never expire
    'muted_until',        # flood mutes released
    'whois_status',
    'user_requests',      # flood history, so a flooder gets a clean slate
    'failed_transfers',   # per-file retry counters
    'kicked_channels',    # losing this restarts the rejoin count at zero on every
                          # rehash, so a channel that has refused us three times
                          # gets tried three more for each setting the operator saves
    'fetch_queue',        # the cross-bot fetch pool. Losing it is the same failure the
                          # active_transfers comment above describes, in the other slot
                          # pool: count_active_fetches() counts rows here, so an empty
                          # dict reports 0 active while transfers are still moving bytes
                          # and the next batch runs past MAX_FETCH_SLOTS. Orphaned rows
                          # also strand their finished files - the transfer completes,
                          # writes state='complete' onto a row nothing reads, and
                          # /api/fetch/<id>/download 404s for a file plainly on disk.
    'fetched_bot_lists',  # every fetched cross-bot list, each one a real multi-MB DCC
                          # transfer from another bot. Losing it empties the Download
                          # tab with no log line saying why.
    'known_bots',         # every other bot seen advertising. Rebuilds itself, but only
                          # at the pace those bots advertise - five minutes or more per
                          # entry, in advert order - so a rehash would empty the
                          # dashboard's bot list and refill it a stranger at a time.
    'dcc_send_offers',    # DCC SEND offers waiting for a receiver to connect, and the
                          # byte each one has agreed to resume from. Losing this does
                          # not merely forget an offer: the sending thread reads the
                          # agreed offset AFTER accept() returns, so an emptied dict
                          # gives it zero, and it sends the file from the start to a
                          # receiver that has already been told to append from the
                          # middle. That is a silently corrupted download rather than
                          # a failed one - the transfer "succeeds" at both ends.
    'notices',            # what the operator has not read yet. A rehash is not an
                          # acknowledgement, and losing these would clear the badge
                          # without anybody having looked at what it was for
    'notice_state',       # and the "how much of it have I seen" marker beside them
    'recent_departures',  # #376's alt-nick reconnect window - a PART/QUIT seen
                          # moments before a rehash would otherwise be forgotten,
                          # so the alt-nick's join right after loses the merge it
                          # would have earned
    'nick_aliases',       # the merges already inferred. Losing this un-merges
                          # every bot the List Browser had already combined,
                          # for no reason connected to the setting that changed
    'private_messages',   # somebody spoke to the bot and it said nothing back. A
                          # rehash is not a reply, and losing these would drop the
                          # only record that anybody tried
    'private_message_state',
    'private_message_decline_sends',  # and the burst window, so a rehash is not
                                      # a way to get the bot talking again
)


def clear_or_keep_pack_interlocks(cfg, pack_running, packing_for, log=print):
    """The rehash's "lock-clearing" step, made conditional (#651, audit M49).

    Cleared: config.rar_inprogress and user_processing_lock, the folder
    packer's two process-wide interlocks. That has always been the
    documented escape hatch for a packer that died without releasing them -
    and it was unconditional, so a rehash whose quiesce wait timed out
    under a RUNNING pack (a big album takes minutes; the wait gives up after
    REHASH_TRANSFER_WAIT) cleared them anyway and woke the queue. The user's
    still-queued row then passed both interlocks on the next trigger and a
    second rar started on the same archive path, unlinking the file the
    first was writing.

    So: `pack_running` (the packer THREAD is alive - dcc.a_pack_is_running())
    keeps them. The reload has already reset rar_inprogress to False, so it
    is put back, and the lock keeps the users it held. The packer's own
    finally releases both when it finishes, as it always did. Not running
    means the flags, if set, are stale - the wedged case - and the hatch
    works as before. Returns True if the interlocks were cleared.
    """
    if pack_running:
        cfg.rar_inprogress = True
        if not hasattr(cfg, 'user_processing_lock') or cfg.user_processing_lock is None:
            cfg.user_processing_lock = set()
        cfg.user_processing_lock.update(packing_for)
        who = ", ".join(sorted(packing_for)) or "someone"
        log(f"[REHASH] A folder pack is still running for {who}; the pack interlocks "
            f"are kept and release when it finishes. Nothing was interrupted.")
        return False
    cfg.rar_inprogress = False
    if hasattr(cfg, 'user_processing_lock'):
        cfg.user_processing_lock = set()
    return True


def rehash_nick_change_line(old_baseline_nick, new_nickname):
    """The raw "NICK <new>\\r\\n" line to send live if a rehash just renamed
    the bot in config.py, or None if the nickname did not actually change.

    Pulled out of handle_rehash_request() as a pure function purely so this
    one decision is unit-testable: that function does a real
    importlib.reload() of half the daemon's modules, which nothing else in
    this suite calls directly either, for the same reason - rebinding
    module-level objects (a fresh threading.Lock(), a fresh {}) out from
    under a shared test process is exactly the class of bug PRESERVE_RUNTIME
    exists to guard against in the running daemon, and running it for real
    here would risk the identical thing happening to test state instead.
    """
    if old_baseline_nick and str(new_nickname).lower() != old_baseline_nick.lower():
        return f"NICK {new_nickname}\r\n"
    return None


def reattach_debug_sinks(announce_module, live_sinks):
    """Re-add every debug sink that was live right before a rehash's
    importlib.reload(announce) reset announce._debug_sinks to []. Returns
    the sinks actually appended (skipping any reload already re-added,
    which cannot happen today but costs nothing to guard).

    Pulled out of handle_rehash_request() for the same reason
    rehash_nick_change_line() and restore_preserved_runtime() above were:
    unit-testable without the real reload the rest of that function does.

    A sink is a promoted admin console session's send_debug-bound method -
    the ONLY channel _cmd_ban/_cmd_rehash/_cmd_update etc. report their
    result through (adminchat.py's Session.close() is what normally calls
    remove_debug_sink(); a rehash must not be indistinguishable from that).
    Losing it silently left an authenticated operator's console accepting
    further commands while never hearing back from any of them again,
    including this very rehash's own "Rehash completed!" line.
    """
    reattached = []
    with announce_module._debug_sinks_lock:
        for sink in live_sinks:
            if sink not in announce_module._debug_sinks:
                announce_module._debug_sinks.append(sink)
                reattached.append(sink)
    return reattached


def reattach_event_sinks(announce_module, live_sinks):
    """The structured feed's half of reattach_debug_sinks() (#576).

    importlib.reload(announce) also resets announce._event_sinks to [], the
    registry a structured console session (the mIRC script after `hello`)
    receives every REQUEST/QUEUED/SENDING/SENT/FAIL/SEARCH/RESUMED and the
    event-driven STATUS burst through. Only _debug_sinks was put back, and a
    structured session drops those kinds from its debug sink because it
    expects them as fields - so after any rehash it heard neither, while its
    LOG lines kept arriving and made it look alive. Returns the sinks
    actually appended.
    """
    reattached = []
    with announce_module._debug_sinks_lock:
        for sink in live_sinks:
            if sink not in announce_module._event_sinks:
                announce_module._event_sinks.append(sink)
                reattached.append(sink)
    return reattached


def restore_preserved_runtime(cfg, preserved_runtime):
    """Merge `preserved_runtime` (captured from `cfg` right before a reload)
    back into `cfg`'s current attributes - MUTATING each container in
    place, never rebinding it. Returns the set of keys actually restored,
    for the caller's log line.

    Pulled out of handle_rehash_request() for the same reason
    rehash_nick_change_line() above was: so this one decision is
    unit-testable without the real importlib.reload() the rest of that
    function does.

    Why mutate rather than assign: config.py's runtime containers
    (dcc_queue, active_transfers, fetch_queue, ...) are bound to the SAME
    objects runtime.py holds - see runtime.py's own docstring - and
    importlib.reload(config) just re-executes those binding statements, so
    `getattr(cfg, key)` before and after a reload is normally the identical
    object, not a fresh empty one the old "reload rebinds to a fresh empty
    container" assumption this function used to carry along with it.
    `setattr(cfg, key, merged)` here would rebind cfg's name to a brand-new
    container, silently detaching it from runtime.py's object forever -
    every other module that reads cfg.<key> would keep seeing this one
    frozen snapshot while the real object drifted on, unnoticed, until the
    next full process restart. Reproduced exactly that way: a transfer's
    removal from active_transfers on completion never reached the detached
    copy, and came back as a permanent phantom DCC slot on the very next
    rehash - two rehashes in a row is all it took.

    When `value is current` - true today for every name in PRESERVE_RUNTIME,
    since all of them are runtime.py-bound - there is nothing to merge: the
    snapshot IS the live object, and the reload never emptied it. That key
    is counted as restored (the operator's log line says what survived) and
    the container is not touched. It used to be merged anyway, which came
    out as `current.clear(); current.update(copy_of_current)` on the live
    dict and `current[:] = copy_of_current` on the live list - run on the
    rehash thread with no lock held (#604). Every other writer of these
    containers holds queue_lock / the fetch lock / the offers lock, so the
    copy could go stale between being taken and being written back: a
    transfer that finished and removed itself in between came back as a
    phantom slot; a dict was momentarily empty between clear() and update(),
    which count_active_fetches() and check_user_status() could see; and
    clear() under another thread's iteration raised RuntimeError on the IRC
    read thread, whose outer handler closes the socket.

    Kept general below that check rather than assuming identity always
    holds - if a future preserved key ever is NOT runtime.py-bound, reload
    really would hand back a fresh, empty container, and the merge produces
    the right content; only the final write changes, from rebind to in-place
    mutation. (That merge is the only path that writes, and it runs with no
    lock; a key that needs it should take the container's own lock here.)
    """
    restored = set()
    for key, value in preserved_runtime.items():
        current = getattr(cfg, key, None)
        if value is current:
            restored.add(key)
            continue
        if isinstance(value, dict) and isinstance(current, dict):
            merged = dict(value)
            merged.update(current)      # window writes win
            current.clear()
            current.update(merged)
            restored.add(key)
        elif isinstance(value, list) and isinstance(current, list):
            merged = list(value)
            for row in current:
                if row not in merged:
                    merged.append(row)
            current[:] = merged
            restored.add(key)
        else:
            setattr(cfg, key, value)
            restored.add(key)
    return restored


CORE_MODULES = ('admin_config', 'defaults', 'list', 'dcc', 'announce',
                'security', 'db', 'stats_mgr')


def _channels_to_sync(config):
    """Every channel the bot should be in: CHANNEL, plus the debug channel.

    Lowercased, because the JOIN/PART comparison this feeds is by name and IRC
    channel names are case-insensitive.

    DELEGATED TO irc.py (#510), which is where the connect path asks the same
    question. This used to be a second, hand-written answer to "where does
    this bot belong" - and the file three modules away already carries a
    comment about exactly that hazard, for exactly this setting (#193): a
    second source of truth that disagrees with the first, in the one place it
    decides whether to PART a channel.

    Imported here rather than at module scope because irc.py imports commands
    (lazily, inside its own handlers) and this file is reloaded by !rehash.

    `config` is now unused and kept deliberately. The rehash calls this twice,
    before and after the reload, passing the config module both times - and it
    is the SAME module object either way, reloaded in place, which is what
    makes reading it through irc.py equivalent. Dropping the parameter would
    make the two call sites look like they are asking different questions.
    """
    import irc
    return [name.lower() for name in irc.channels_we_should_be_in()]


# One IRC line is 512 bytes including the trailing CRLF, so 510 for the
# command itself - the same number on_connect.MAX_COMMAND_BYTES uses, for the
# same reason.
MAX_SYNC_LINE_BYTES = 510


def comma_batched(verb, channels, tail=""):
    """`verb` lines carrying as many channels each as an IRC line will hold.

    JOIN, PART and NAMES all take a comma-separated channel list. The connect
    path has always used that (irc.py joins every channel with ONE JOIN); the
    rehash sync never did, and sent one line per channel per verb (#440).

    A single channel whose line is already over the limit goes out alone and
    over-length rather than being dropped: RFC 2812 caps a channel name at 50
    characters so it cannot happen with anything a server would accept, and
    sending it is the only answer that leaves the operator something to see.
    """
    lines = []
    batch = []

    def rendered(names):
        return f"{verb} {','.join(names)}{tail}"

    for chan in channels:
        if batch and len(rendered(batch + [chan]).encode("utf-8")) > MAX_SYNC_LINE_BYTES:
            lines.append(rendered(batch) + "\r\n")
            batch = []
        batch.append(chan)
    if batch:
        lines.append(rendered(batch) + "\r\n")
    return lines


def sync_channels(oserve_mod, old_chans, new_chans, log=print,
                  previous_debug_channel=None):
    """JOIN what is new, PART what is gone, NAMES the lot - through the pacer.

    Returns the lines queued, which is what the caller reports.

    LIFTED OUT OF _handle_rehash_request() so it can be driven. While it was
    inline, reaching it meant reloading every module in the daemon, so the only
    thing a test could do was read the source - and "the word JOIN appears in
    commands.py" passes with the loop behind `if False:`. Same reason
    irc.resolve_dcc_address() is a function of its own.

    THE PACER, AND WHY THIS WAS THE ONE PATH WITHOUT IT (#440)

    These three loops wrote JOIN/PART/NAMES straight to the socket with no
    pacer slot and no gap between them, so the burst scaled with the channel
    count: 15 channels meant 15 back-to-back lines, and replacing the channel
    list outright meant JOIN + PART + NAMES for every channel in one go. It
    was the only outbound path in the daemon that both scaled with the channel
    count and ignored runtime.outbound_pacer.

    Not remotely reachable - a rehash is refused from anyone who is not an
    admin, and the dashboard is authenticated - so this is an operator burst
    rather than a vector. But the dashboard fires a rehash on EVERY settings
    save, including a theme change, and 15 short lines is a burst an operator
    did not ask for and cannot see.

    Two things fix it together. The lines are comma-batched, the way the
    connect path already batches its JOIN, which turns the realistic case into
    two lines and the full-swap case into three. And they go through
    oserve.queue_message() like every other line the bot says, so they take a
    pacer slot instead of racing whatever else is being sent. A NAMES refresh
    is latency-insensitive; the DCC negotiation lines that deliberately bypass
    the queue are not.
    """
    import defaults as config
    import announce
    import runtime

    debug_chan = str(getattr(config, 'DEBUG_CHANNEL', '') or '').lower()
    previous_debug = str(previous_debug_channel or '').strip().lower()

    joining = [chan for chan in new_chans if chan not in old_chans]

    # The debug channel is never parted by a sync. config.py declares
    # DEBUG_CHANNEL as "" (blank) since #193, so a literal channel name here
    # was a second source of truth that disagreed with the first - and the one
    # place it would have been consulted is the one place it decides whether to
    # PART a channel.
    protected = {debug_chan} - {""}

    # AND THE VALUE THE RELOAD MAY HAVE JUST LOST (#511).
    #
    # debug_chan is read AFTER the reload, so the guard above worked whenever
    # DEBUG_CHANNEL survived and did nothing in the one case it exists for:
    # the reload handing back a blank. Then the channel is in old_chans, absent
    # from new_chans, and `chan != ""` is true - so the bot PARTed the one
    # channel whose whole purpose is telling the operator what it is doing.
    #
    # A blank is ambiguous and the two readings want opposite things. The
    # operator may have cleared the setting on purpose; or settings.conf was
    # briefly unreadable, admin_config.py failed to import, or the reload
    # caught a value mid-flight - the window tests/test_audit_high_findings.py
    # already documents. The dashboard fires a rehash on EVERY settings save,
    # so the second is not rare.
    #
    # Staying is the recoverable answer. An operator who meant to clear it
    # loses nothing they can see - a blank DEBUG_CHANNEL already stops
    # send_debug() writing there - and the bot leaves on the next reconnect.
    # An operator who did not mean it keeps the channel they are reading.
    #
    # NOT the same shape as the REQUIRED-setting restore in
    # reload_modules_in_order(), and deliberately not: that one puts the value
    # BACK, which is only safe because a blank NICKNAME/CHANNEL/ADMIN_NICK is
    # never legitimate. A blank DEBUG_CHANNEL is perfectly legitimate, so this
    # changes nothing about the setting - only about whether a PART is sent on
    # the strength of it.
    if previous_debug and not debug_chan:
        protected.add(previous_debug)
        log(f"[REHASH SYNC] DEBUG_CHANNEL came back blank from the reload. "
            f"Staying in {previous_debug} rather than parting it - if you did "
            f"mean to clear it, the bot leaves on the next reconnect.")

    # A DELIBERATE CHANGE still parts. DEBUG_CHANNEL going from one channel to
    # a different one is an edit nobody makes by accident, so the old one is
    # not protected and the new one is joined - which is the behaviour this has
    # always had, and the half a blanket "never part the debug channel" rule
    # would have quietly broken.
    parting = [chan for chan in old_chans
               if chan not in new_chans and chan not in protected]

    # The membership map moves with the decision, not with the send. These
    # lines are now queued rather than written, so waiting for them to go out
    # would leave dcc.py reading a stale channel_users for as long as the
    # queue takes to drain - and it treats that map as proof a user is present.
    for chan in joining:
        with runtime.channel_users_lock():
            if chan.lower() not in config.channel_users:
                config.channel_users[chan.lower()] = set()
    for chan in parting:
        with runtime.channel_users_lock():
            if chan.lower() in config.channel_users:
                del config.channel_users[chan.lower()]

    lines = (comma_batched("JOIN", joining)
             + comma_batched("PART", parting, tail=" :Removed from DCCore")
             + comma_batched("NAMES", new_chans))

    for line in lines:
        oserve_mod.queue_message("channel_sync", line)

    # ONE debug line per verb, not one per channel. send_debug() goes to a
    # CHANNEL through the VIP lane, so a line per channel was a second burst
    # sitting behind the first - paced, but still 15 lines of the operator's
    # debug channel saying the same thing 15 times.
    if joining:
        announce.send_debug(f"Joining {', '.join(joining)} due to new "
                            f"configuration layout!", category="JOIN")
    if parting:
        announce.send_debug(f"Parting {', '.join(parting)} due to new "
                            f"configuration layout!", category="PART")
    if not joining and not parting:
        log("[REHASH SYNC] No channel changes - refreshing the name lists only.")

    return lines


def _restore_advert_worker_token(live_worker_id):
    """Put the advert worker's generation token back after a reload.

    Returns True if this call restored it.

    announce.current_worker_id is how a running advert worker knows it is
    still the current one: it compares its own id against the module's and
    retires if something newer has taken over. importlib.reload() re-executes
    announce's body, which resets that to 0 - so between the reload and this
    call, a worker waking on its five-second cycle reads 0, concludes it has
    been replaced, and stops. Nothing starts another, and the channels go
    quiet until the next reconnect.

    ONLY IF NOTHING NEWER CLAIMED IT. A reconnect completing inside the reload
    window starts a fresh worker and stamps a higher id; restoring blindly
    would retire that one instead and cause the very silence this is
    preventing.
    """
    import announce as _ann

    if not live_worker_id or getattr(_ann, "current_worker_id", 0):
        return False
    _ann.current_worker_id = live_worker_id
    print("[REHASH RAM] Advert worker kept alive across the reload.")
    print("[REHASH NOTE] announce_worker's own code is NOT re-entered by a rehash; "
          "restart the daemon to pick up changes to the advert loop itself.")
    return True


def reload_modules_in_order(modules=CORE_MODULES, reload_self=True):
    """Reload the daemon's modules in place and return the names reloaded.

    ORDER IS LOAD-BEARING, which is why this is a named constant rather than a
    literal inside the handler. defaults.py ends with `from admin_config import
    *`; reloading defaults re-executes that statement, but the import system
    serves the CACHED sys.modules['admin_config'] rather than re-reading the
    file. Reloading defaults alone therefore picked up nothing an operator had
    written in admin_config.py - and !rehash reported "Rehash completed!"
    regardless.

    The case that makes it worth fixing is REVOCATION. A hostmask deleted from
    ADMIN_HOSTMASKS stayed live until the process was restarted, while the
    operator had been told the change was applied.

    A module that is not loaded is skipped: admin_config.py is optional and
    gitignored, so it is frequently absent.

    importlib is imported here rather than at the top of the file because it
    was imported inside handle_rehash_request(), and this body was reading it
    out of that scope. `sys` really is module-level; `importlib` was not.
    """
    import importlib
    import settings_file

    # What the operator's configuration actually resolved to BEFORE the reload.
    # Two separate jobs below: the lock stops other threads seeing this window,
    # and this snapshot is the backstop for when the window does not close
    # cleanly.
    live = sys.modules.get('defaults')
    before = ({name: getattr(live, name, None) for name in settings_file.REQUIRED}
              if live is not None else {})

    # Everything from here to the end of the reload is one window as far as any
    # other thread is concerned - see runtime.config_reload_lock's own comment
    # for what is observable inside it and how it was found.
    with runtime.config_reload_lock:
        reloaded = []
        for mod_name in modules:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
                reloaded.append(mod_name)

        # The lock hides the window; it cannot help if the window never closes.
        # settings_file.apply_to() is deliberately forgiving - an unreadable
        # settings.conf is logged and the built-in defaults are kept, which is
        # right at STARTUP (oserve.startup()'s REQUIRED gate then refuses to
        # boot and the operator is told why) and wrong here, because a rehash
        # has no such gate: the daemon is already running and would simply
        # carry on with no nickname, no channels and no admin. A file that was
        # readable a moment ago and is not now - antivirus holding it open,
        # a network share blinking, an editor mid-save - would silently
        # de-configure a live bot.
        #
        # settings_file.REQUIRED ONLY, and only a value that WAS set and came
        # back blank. That narrowness is what stops this becoming a ratchet
        # that prevents a rehash from unsetting anything: every other setting
        # reverts to its shipped default normally, which is what a rehash is
        # for. A blank REQUIRED setting is the one state that is never
        # legitimate - save() refuses to write one (SettingsWriteError) and
        # oserve.startup() refuses to boot with one - so restoring it can
        # never overwrite something an operator actually asked for.
        for name, value in before.items():
            if value and not getattr(live, name, None):
                setattr(live, name, value)
                print(f"[REHASH CONFIG] {name} came back blank from the reload, so the "
                      f"value that was already running was kept. Check that settings.conf "
                      f"is readable - the next restart will use whatever it says.")

        # Last, and separately: reloading this module rebinds the very names the
        # caller is executing out of. The running frame keeps its old code object,
        # which is what makes this safe here and would not be if it ran first.
        if reload_self and 'commands' in sys.modules:
            importlib.reload(sys.modules['commands'])
            reloaded.append('commands')
    return reloaded


def handle_rehash_request(user, target_chan, authorised=False, user_host=None):
    """Reload the modules live, in memory - one rehash at a time.

    THE SERIALISATION IS THE POINT OF THIS WRAPPER. The body below reloads
    the modules and then compares the channel list it reads AFTERWARDS
    against the one it read before, to decide what to JOIN and what to
    PART. Two rehashes overlapping is not merely wasteful: the second
    one's reload puts config.CHANNEL back to its literal None for about a
    millisecond (see runtime.config_reload_lock), and a first rehash that
    reads its "new" channel list inside that window sees NO CHANNELS - so
    every channel the bot is in falls into the PART branch.

    Measured by audit: the bot PARTed every channel including the debug
    channel, sent no JOIN and no NAMES, emptied channel_users, and logged
    "[REHASH SYNC] Channel sync completed successfully." dcc.py treats
    channel_users as proof a user is present, so every queue then froze.
    Nothing raised. Reproduced with nothing patched in 4 of 60 runs.

    Reachable without trying: irc.py spawns an unguarded thread per
    "!rehash", adminchat.py does the same from the console, and
    webserver.py fires one on EVERY Settings save and every password
    change - two separate endpoints.

    WAITS, rather than dropping the second one. A second rehash is often
    the one that matters: a dashboard save writes settings.conf and THEN
    triggers it, and the rehash already running may have read the file
    before that write landed. Dropping it would silently lose the
    operator's change; waiting applies it.
    """
    if not authorised and not is_admin(user, user_host):
        print(f"[REHASH SECURITY] Ignored a rehash attempt from an unauthorised user: {user}")
        return

    if not runtime.rehash_lock.acquire(blocking=False):
        print("[REHASH] Another rehash is still running - waiting for it "
              "to finish before starting this one.")
        runtime.rehash_lock.acquire()
    try:
        return _handle_rehash_request(user, target_chan)
    finally:
        runtime.rehash_lock.release()


def _handle_rehash_request(user, target_chan):
    """The rehash itself. Only ever called with runtime.rehash_lock held."""
    import importlib
    import sys
    import defaults as config
    import announce

    # =====================================================================
    # 1. Back EVERY piece of live state up into local variables
    # =====================================================================
    # channel_users is NOT snapshotted here (#429), for the identical reason
    # dcc_queue is not (see that removed snapshot's own comment below): it is
    # a runtime.py-bound container, so importlib.reload() below never empties
    # or replaces it - the object irc.py sees after reload_modules_in_order()
    # is the SAME one it saw before, with every JOIN/PART/QUIT the read
    # thread applied during the reload window already on it.
    #
    # A snapshot-and-restore used to run here anyway, capturing channel_users
    # before the reload and overwriting it with that snapshot afterwards -
    # deep-copied, so the restore could not even see writes made to the live
    # dict while the copy was held. dcc.wait_for_transfers_to_finish() blocks
    # this function for up to REHASH_TRANSFER_WAIT (120s) before the restore
    # runs, which is a two-minute window for the read thread to apply real
    # JOINs and PARTs that the restore then silently discarded. A JOIN lost
    # this way heals itself at the next NAMES resync; a PART or QUIT does
    # not, because the resync can only ADD names back (irc.py's 353 handler
    # does config.channel_users[chan].update(names), never a removal) - so a
    # user who left during the window came back to life as far as this bot
    # was concerned, for the rest of the connection. dcc.user_is_present_in_
    # ram() then thaws their frozen queue and the presence gate admits
    # dispatch to a nick that is not on the network, burning a DCC slot on
    # every attempt until MAX_SEND_FAILS deletes the row - destroying the
    # departed user's queue instead of the freezer preserving it, which is
    # the entire reason the freezer exists. Reachable on every dashboard
    # Settings save and every password change, not just a manual !rehash.

    # B. The active DCC slots
    ram_backup_slots = 0
    ram_user_slots = {}
    for mod_name in ['dcc', 'defaults', 'oserve']:
        mod = sys.modules.get(mod_name)
        if mod:
            for attr in ['active_downloads', 'current_sends', 'total_sends']:
                if hasattr(mod, attr) and getattr(mod, attr) > 0:
                    ram_backup_slots = getattr(mod, attr)
            for attr in ['user_slots', 'active_users', 'current_user_slots']:
                if hasattr(mod, attr) and isinstance(getattr(mod, attr), dict):
                    raw_slots = getattr(mod, attr)
                    ram_user_slots = {k.lower(): v for k, v in raw_slots.items()}

    # dcc_queue is NOT snapshotted here, unlike channel_users/slots above. It is
    # a runtime.py-bound container (see that module's docstring), so a reload
    # never actually empties or replaces it - the object dcc.py/defaults.py see
    # after reload_modules_in_order() below is the identical one they saw before
    # it, with every write that happened during the reload window already on it.
    #
    # A snapshot-and-restore used to run here anyway, capturing dcc_queue before
    # the reload and overwriting it with that snapshot afterwards. Since the live
    # object never actually changes, that restore was strictly destructive: a
    # request that arrived during the reload window was wiped by the snapshot's
    # `.clear()`, and a transfer that COMPLETED during the window - removed from
    # the live queue - came back from the stale snapshot and was sent again.
    # rar_queue and download_queue, probed in the same removed loop, were never
    # real container names anywhere in this codebase.

    # Keep the old channel list, for the JOIN/PART comparison.
    #
    # The debug channel belongs in BOTH sides of that comparison, and used to be
    # in neither. irc.py joins it once, at connect, right after the main
    # channels; the sync below built its list from CHANNEL alone and mentioned
    # DEBUG_CHANNEL only to avoid PARTing it. So an operator who set a debug
    # channel on the dashboard was told "Rehash started", watched the setting
    # save correctly, and the bot never joined - nothing to see in any log,
    # because nothing failed. Reported from a real install.
    #
    # Symmetry is what keeps it quiet: present on both sides when it has not
    # changed, so an unchanged debug channel is not re-JOINed with a "due to new
    # configuration layout!" line on every single rehash.
    old_chans = _channels_to_sync(config)
    # Captured HERE, beside old_chans and for the same reason (#511): both are
    # what the operator's configuration actually said before the reload, and
    # the sync needs them together to tell "you cleared this" from "the reload
    # did not bring it back".
    old_debug_chan = str(getattr(config, 'DEBUG_CHANNEL', '') or '').strip()

    # Pause the advert for the moment
    announce.is_ready = False
    announce.send_debug(f"Rehash triggered by {user} from {target_chan}. Pausing the channel advert "
        f"(command replies keep working)...", category="INFO")
    
    # vip_queue is deliberately NOT preserved, for the same reason send_queue is not: it is
    # transient OUTPUT, not state. Restoring it would also replay lines addressed to channels
    # this very handler is about to PART, which the server answers with 404.
    preserved_runtime = {}
    for _key in PRESERVE_RUNTIME:
        if hasattr(config, _key):
            preserved_runtime[_key] = getattr(config, _key)

    # config.NICKNAME is BOTH a setting and runtime state: irc.py rebinds it to ALT_NICKNAME
    # after a 433. Reloading config resets it to the file value, so a rehash while running as
    # the alternate nick left the bot answering to a name the server no longer knew it by -
    # every @trigger dead until the next reconnect.
    live_nick = getattr(config, 'NICKNAME', None)
    baseline_nick = getattr(config, 'ORIGINAL_NICK', None)

    # announce.current_worker_id is the advert worker's generation token. Reload resets it to
    # 0, the running worker sees the mismatch and retires itself, and nothing starts a new one
    # until the next reconnect - so !rehash silently stopped all channel advertising.
    live_worker_id = getattr(announce, 'current_worker_id', None)

    # announce._debug_sinks is how a promoted admin console session receives every command's
    # result (send_debug is its ONLY channel - see _cmd_ban/_cmd_rehash/_cmd_update). Reload
    # resets it to [], and unlike an explicit remove_debug_sink() call, the session is never
    # told: it keeps accepting commands, just silently stops hearing back from any of them,
    # including this very rehash's own "Rehash completed!" line.
    with announce._debug_sinks_lock:
        live_debug_sinks = list(announce._debug_sinks)
        live_event_sinks = list(announce._event_sinks)

    try:
        # 1b. QUIESCE FIRST (#310). A reload swaps the modules a
        # running transfer is executing inside, so the safe order is: stop
        # starting new sends, let the ones in flight finish, reload, then
        # start again. Without it a rehash lands in the middle of somebody's
        # download.
        #
        # Bounded, and it carries on if the bound is reached - see
        # wait_for_transfers_to_finish(). A bot that cannot be reconfigured
        # while one stuck peer holds a socket is worse than one that
        # occasionally interrupts a transfer, and the log says which happened.
        import dcc as _dcc_quiesce
        _dcc_quiesce.wait_for_transfers_to_finish()
        # Whether a folder pack is STILL running when the wait gives up
        # (#651): the wait counts one as busy, but after REHASH_TRANSFER_WAIT
        # it carries on regardless, and the reload below resets
        # config.rar_inprogress to False. Read before the reload, acted on
        # after it - see clear_or_keep_pack_interlocks().
        _pack_still_running = _dcc_quiesce.a_pack_is_running()
        _packing_for = set(getattr(config, 'user_processing_lock', set()) or set())

        # 2. REHASH: reload every core module live, in memory. The ORDER matters
        # and is documented on reload_modules_in_order() itself.
        reload_modules_in_order()

        # THE ADVERT TOKEN GOES BACK FIRST, not seventy lines further down
        # where it used to. importlib.reload(announce) re-executes that
        # module's body, which resets current_worker_id to 0, and the live
        # advert worker wakes every five seconds to compare its own id against
        # it. A wake landing anywhere in the window between the reload and the
        # restore saw 0, concluded that something newer had replaced it, and
        # retired - leaving no advert worker at all and the channels silent
        # until the next reconnect.
        #
        # The "only if nothing newer claimed it" rule below is unchanged and
        # still needed: a reconnect completing inside this window starts a
        # fresh worker and stamps a higher id, and restoring blindly would
        # retire that one instead.
        _restore_advert_worker_token(live_worker_id)

        print(f"[REHASH SUCCESS] Every Python module was reloaded in memory by {user}.")

        # Put the live state back before anything else runs against the fresh modules. The
        # explicit dcc_queue / channel_users restores further down still run afterwards and
        # win for those two keys, so this does not fight them.
        #
        # MERGE rather than overwrite - see restore_preserved_runtime()'s own docstring for
        # why it mutates each container in place instead of ever rebinding config's name to
        # a new one. The reload window is only the few milliseconds of eight module reloads,
        # but a transfer finishing inside it is exactly the case that matters: its removal
        # from active_transfers would otherwise be undone and the finished entry would come
        # back as a phantom holding a DCC slot.
        import defaults as _cfg
        restored = restore_preserved_runtime(_cfg, preserved_runtime)

        if restored:
            print(f"[REHASH RAM] Restored {len(restored)} live runtime structures "
                  f"({', '.join(sorted(restored))}).")

        # Name the restored slots explicitly - purely diagnostic. An operator
        # staring at "3/3 slots busy" with nothing moving needs to be able to
        # see whose they are, and this is the only place that prints it.
        if getattr(_cfg, 'active_transfers', None):
            _holders = ', '.join(sorted({str(t.get('user', '?')) for t in _cfg.active_transfers}))
            print(f"[REHASH RAM] {len(_cfg.active_transfers)} DCC slot(s) still held by: {_holders}")

        # Nickname: if config.py's value is unchanged, the LIVE nick wins - it may be the 433
        # fallback and the server knows us by it. If the admin edited NICKNAME in the file,
        # adopt the new value and re-baseline ORIGINAL_NICK so the fallback logic follows it.
        if baseline_nick and _cfg.NICKNAME == baseline_nick:
            if live_nick and live_nick != _cfg.NICKNAME:
                _cfg.NICKNAME = live_nick
                print(f"[REHASH RAM] Kept the live nickname {live_nick!r} (alternate nick in use).")
        else:
            # The admin renamed the bot in config.py. Re-baseline so the rename survives the
            # next reconnect (irc.py resets NICKNAME from ORIGINAL_NICK on every connect), but
            # keep the OLD name addressable: the published master list was stamped with it
            # (update_list.py writes "!<nick> <file>" lines), so without this every request
            # pasted from a list users already downloaded would be dropped with no reply.
            # irc.get_bot_aliases picks this up; config.py never assigns it, so like
            # ORIGINAL_NICK it survives future reloads.
            if baseline_nick:
                _cfg.PREVIOUS_NICK = baseline_nick
            _cfg.ORIGINAL_NICK = _cfg.NICKNAME
            print(f"[REHASH RAM] config.py changed the nickname to {_cfg.NICKNAME!r}; "
                  f"re-baselined, still answering to {baseline_nick!r}.")
            # The new name is the TARGET, not yet the bot's name on the wire
            # (#635): config.NICKNAME stays what the server calls us until
            # its NICK event says otherwise - a rename refused with 438
            # ("too fast") or 433 used to leave config saying a name the
            # server never gave. ORIGINAL_NICK carries the target, so a
            # reconnect asks for it and the reclaim path chases it.
            _wanted_nick = _cfg.NICKNAME
            if live_nick:
                _cfg.NICKNAME = live_nick

            # Also change it LIVE, right now, over the connection that is
            # already open - the same live-sync treatment a CHANNEL edit
            # already gets a few lines below. Without this, a rename saved
            # through the web dashboard's Settings page (or a plain !rehash)
            # only re-baselined bookkeeping and did nothing to the actual
            # on-the-wire nick until some UNRELATED reconnect happened to
            # occur next - which could be minutes away, or days. An operator
            # watching the change happen in the dashboard has every reason to
            # expect the bot to answer to the new name immediately, the way
            # a channel add/remove already does.
            _nick_line = rehash_nick_change_line(baseline_nick, _wanted_nick)
            if _nick_line:
                _oserve_for_nick = sys.modules.get('oserve')
                _live_sock_for_nick = getattr(_oserve_for_nick, 'irc_connection', None) if _oserve_for_nick else None
                if _live_sock_for_nick:
                    try:
                        _live_sock_for_nick.sendall(_nick_line.encode("utf-8", errors="ignore"))
                        print(f"[REHASH NICK] Sent a live NICK change to {_wanted_nick!r}.")
                    except Exception as _nick_err:
                        print(f"[REHASH NICK ERROR] Could not send the live nick change: {_nick_err}")
                else:
                    print("[REHASH NICK] No live socket available; the new nickname "
                          "will take effect on the next reconnect instead.")

        import announce as _ann
        # The token itself went back immediately after the reload - see
        # _restore_advert_worker_token() and the comment at that call - because
        # the worker wakes every five seconds and a wake inside this stretch
        # used to find it zeroed and retire.

        # Reinstate every console session's debug sink - see reattach_debug_sinks()'s
        # docstring for why this is not optional.
        _reattached_sinks = reattach_debug_sinks(_ann, live_debug_sinks)
        if _reattached_sinks:
            print(f"[REHASH RAM] Reattached {len(_reattached_sinks)} admin console debug sink(s).")
        # ...and the structured feed's own registry (#576), which the same
        # reload emptied and which nothing else refills.
        _reattached_events = reattach_event_sinks(_ann, live_event_sinks)
        if _reattached_events:
            print(f"[REHASH RAM] Reattached {len(_reattached_events)} structured feed sink(s).")

        # Read the freshly reloaded config
        import defaults as config
        import announce
        announce.is_ready = True
        
         # =====================================================================
        # 3. RESTORE: write every value back into the newly loaded modules
        # =====================================================================
        # No channel_users restore here - see the removed snapshot's comment
        # above (#429). It is runtime.py-bound and was never actually touched
        # by the reload, so there is nothing to put back, and the restore
        # that used to run here was purely destructive: it discarded every
        # JOIN/PART/QUIT applied during the up-to-120s transfer-wait window.

        # Restore the slots
        for mod_name in ['dcc', 'defaults', 'oserve']:
            mod = sys.modules.get(mod_name)
            if mod:
                if ram_backup_slots > 0:
                    for attr in ['active_downloads', 'current_sends', 'total_sends']:
                        if hasattr(mod, attr): setattr(mod, attr, ram_backup_slots)
                if ram_user_slots:
                    for attr in ['user_slots', 'active_users', 'current_user_slots']:
                        if hasattr(mod, attr):
                            combined_slots = {k.lower(): v for k, v in ram_user_slots.items()}
                            for k, v in ram_user_slots.items(): combined_slots[k.upper()] = v
                            setattr(mod, attr, combined_slots)

        # No queue restore here - see the removed snapshot's comment above.
        # dcc_queue is runtime.py-bound and was never actually touched by the
        # reload, so there is nothing to put back.

        # Reset the text queues (send_queue) to empty dicts so they cannot clash
        for mod_name in ['queue_mgr', 'defaults', 'oserve', 'irc']:
            mod = sys.modules.get(mod_name)
            if mod:
                for attr in ['send_queue', 'msg_queue', 'out_queue']:
                    if hasattr(mod, attr): setattr(mod, attr, {})
        print(f"[REHASH RAM] The outgoing text queues (send_queue) were reset.")

        # Reset the advert timer, so it waits a fresh five minutes
        if hasattr(announce, 'last_announce_time'):
            import time
            announce.last_announce_time = time.time()

        # The automatic list refresh (#625). oserve.startup() starts its
        # worker only when AUTO_REFETCH_LISTS is on at boot, and a dashboard
        # save that ticks it on lands here - so this is where it has to
        # start, or the setting is "rehash started" and nothing else until
        # the next restart. Idempotent: runtime.py remembers a worker already
        # running, and a rehash that changed nothing starts nothing.
        try:
            import list_fetch as _list_fetch
            if _list_fetch.ensure_auto_refetch_worker():
                print("[REHASH] AUTO_REFETCH_LISTS is on: the automatic list "
                      "refresh has started.")
        except Exception as refetch_err:
            print(f"[REHASH] Could not start the automatic list refresh: "
                  f"{refetch_err}")

        # ---------------------------------------------------------------------
        # 4. FULLY AUTOMATIC CHANNEL SYNC (JOIN NEW / PART REMOVED)
        # ---------------------------------------------------------------------
        oserve = sys.modules.get('oserve')
        irc_sock = getattr(oserve, 'irc_connection', None)
        
        if irc_sock:
            # THE SYNC IS TAIL WORK, AND MUST NOT REPORT THE RELOAD AS FAILED.
            #
            # Everything this handler exists to do has already happened by
            # here: the modules are reloaded and the live state is merged
            # back. The channel sync is bookkeeping on top - so anything it
            # raises used to reach the handler at the bottom, which prints
            # "[REHASH CRITICAL ERROR] The files could not be reloaded live"
            # and tells the operator their rehash failed when it succeeded.
            # Same reasoning as update_list.py's point of no return (#442):
            # once the thing is done, the error path has to stop claiming it
            # is not.
            try:
                queued = sync_channels(oserve, old_chans, _channels_to_sync(config),
                                       previous_debug_channel=old_debug_chan)
                print(f"[REHASH SYNC] Channel sync queued as {len(queued)} paced "
                      f"line(s); they go out at MSG_DELAY like everything else "
                      f"the bot says.")
            except Exception as sync_err:
                print(f"[REHASH SYNC ERROR] The reload itself completed; the "
                      f"channel sync did not: {sync_err}. The channels the bot "
                      f"is in are unchanged until the next rehash or "
                      f"reconnect.")
        else:
            print("[REHASH WARNING] Could not sync the channels: no raw socket was available.")
        # ---------------------------------------------------------------------
        
        # 5. Confirm, through the VIP express lane
        announce.send_debug(f"Rehash completed! RAM-Memory preserved seamlessly without disk-paging.", category="INFO")
        
        # Clear any stale locks and ghost blocks left over before the rehash -
        # unless the packer that holds them is still running (#651).
        import dcc as _dcc_pack
        clear_or_keep_pack_interlocks(
            config, _pack_still_running and _dcc_pack.a_pack_is_running(), _packing_for)

        # ADMIN_HOSTMASKS may have just changed; a very broad entry is
        # accepted but said out loud, here as at boot (#669).
        try:
            import adminchat as _adminchat_rehash
            _adminchat_rehash.report_broad_host_patterns()
        except Exception as hostmask_err:
            print(f"[REHASH] Could not check ADMIN_HOSTMASKS: {hostmask_err}")

        # Take the real, live network socket straight from memory
        oserve_mod = sys.modules.get('oserve')
        live_socket = getattr(oserve_mod, 'irc_connection', None) if oserve_mod else None
        
        # Sends are allowed again BEFORE the queue is woken - waking it while
        # still paused would have every dispatch refused by the gate the wait
        # put up, and the wake is the thing that restarts the queue the
        # operator asked for.
        import dcc as _dcc_resume
        _dcc_resume.resume_transfers()

        if live_socket:
            import dcc
            import threading
            print("[REHASH-WAKE] Letting queued users into the free slots...")
            # One look per free slot, not one pass (#668): a pass dispatches
            # at most one user, and requests made during the quiesce are
            # queued now rather than refused, so several users may be
            # waiting on this wake with nothing else due to wake them.
            threading.Thread(
                target=dcc.wake_restored_queues,
                args=(live_socket,),
                daemon=True
            ).start()
        else:
            print("[REHASH ERROR] Could not wake the queue automatically: live_socket was not in memory.")
        
    except Exception as e:
        import announce
        announce.is_ready = True
        # THE PAUSE MUST NOT OUTLIVE THE REHASH. If the reload raised anywhere
        # after the quiesce, the bot would sit refusing every send for ever,
        # with the only clue a notice telling users to try again in a moment.
        try:
            import dcc as _dcc_unpause
            _dcc_unpause.resume_transfers()
        except Exception:
            pass
        print(f"[REHASH CRITICAL ERROR] The files could not be reloaded live: {e}")
        announce.send_debug(f"Rehash FAILED (Notices Resumed for safety): {e}", category="INFO")


def handle_hard_ban_request(user, target_chan, msg_text, authorised=False, user_host=None):
    """Add a permanent wildcard pattern to hard_bans.txt, straight from IRC."""
    import defaults as config
    import announce
    
    if not authorised and not is_admin(user, user_host):
        print(f"[SECURITY] Unauthorised user {user} tried to run !ban.")
        return

    parts = msg_text.split(" ", 1)
    if len(parts) < 2:
        announce.send_debug("Syntax error! Usage: !ban <pattern*>", category="INFO")
        return
        
    pattern = parts[1].strip().lower()
    if not pattern:
        return

    # #225: security.py's enforcement loop silently declines an over-broad
    # pattern - one that reduces to nothing once wildcards and mask
    # separators are stripped, which would ban every user on the network -
    # logging only to stdout, which the admin who typed the command never
    # sees. Without this, "!ban *" was confirmed as added and was a
    # permanent no-op: the admin believed a ban was live when it was not,
    # which is the direction that matters.
    import security
    if security.is_over_broad_hard_ban_pattern(pattern):
        announce.send_debug(
            f"Refused - {config.C_BOLD}{pattern}{config.C_RESET} reduces to nothing "
            f"once wildcards are stripped, which would ban every user on the network.",
            category="INFO")
        print(f"[HARD BAN REFUSED] {user} tried to add over-broad pattern: {pattern}")
        return

    # db.add_hard_ban does the whole read-modify-write under the disk lock and
    # replaces the file atomically. The old code appended with no newline check,
    # so on a hand-edited file whose last line lacked one it glued two patterns
    # into one and silently unbanned both; and two !ban threads could interleave
    # and lose whichever entry lost the race.
    import db
    try:
        added = db.add_hard_ban(pattern)
    except Exception as ban_err:
        announce.send_debug(f"Could not write hard_bans.txt: {ban_err}", category="INFO")
        print(f"[HARD BAN ERROR] {user} could not add {pattern}: {ban_err}")
        return

    if added:
        announce.send_debug(f"Added permanent wildcard to hard_bans.txt: {config.C_BOLD}{pattern}{config.C_RESET}", category="BAN")
        print(f"[HARD BAN] {user} added a permanent pattern: {pattern}")
    else:
        announce.send_debug(f"Pattern {pattern} is already banned permanently.", category="INFO")

def handle_hard_unban_request(user, target_chan, msg_text, authorised=False, user_host=None):
    """Remove a permanent wildcard pattern from hard_bans.txt, straight from IRC."""
    import defaults as config
    import announce
    import os
    
    if not authorised and not is_admin(user, user_host):
        # Logged, like !ban / !clearqueue / !rehash / !update all are. This
        # returned silently, so an operator auditing attempted privilege abuse
        # had a blind spot on exactly one command (#234).
        print(f"[SECURITY] Unauthorised user {user} tried to run !unban.")
        return

    parts = msg_text.split(" ", 1)
    if len(parts) < 2:
        announce.send_debug("Syntax error! Usage: !unban <pattern*>", category="INFO")
        return
        
    pattern = parts[1].strip().lower()
    if not pattern:
        return
        
    filename = config.HARD_BANS_FILE
    if not os.path.exists(filename):
        announce.send_debug(f"No permanent ban file yet ({filename} does not exist), "
                            f"so there is nothing to remove.", category="INFO")
        return
        
    # db.remove_hard_ban rewrites the file through a temp + os.replace. The old
    # code truncated it with open(..., "w") and wrote the survivors back one at a
    # time: a crash, a full disk or a kill in between left it short or EMPTY.
    #
    # That fails OPEN. security.check_user_status re-reads this file on every
    # command and only distrusts a "no match" when the read RAISED - a file that
    # is readable but truncated is indistinguishable from one with no bans in it,
    # so every hard-banned user is admitted until somebody notices.
    import db
    try:
        removed = db.remove_hard_ban(pattern)
    except Exception as unban_err:
        announce.send_debug(f"Could not write hard_bans.txt: {unban_err}", category="INFO")
        print(f"[HARD UNBAN ERROR] {user} could not lift {pattern}: {unban_err}")
        return

    if removed:
        announce.send_debug(f"Removed permanent wildcard from hard_bans.txt: {config.C_BOLD}{pattern}{config.C_RESET}", category="BAN")
        print(f"[HARD UNBAN] {user} lifted the permanent pattern: {pattern}")
    else:
        announce.send_debug(f"Pattern {pattern} was not found in hard_bans.txt.", category="INFO")

# The banners update_list.py's __main__ prints AFTER everything else, and the
# reason the "last line" rule below stopped working. They say whether the run
# failed, which the exit code already said, and never why.
_GENERIC_RESULT_LINES = ("--- ERROR: could not generate the list. ---",
                         "--- The list was updated successfully. ---")

# The tags update_list.py puts on a real explanation.
_ERROR_TAGS = ("[LIST-GEN ERROR]", "[CRITICAL]", "[LIST ERROR]", "[UPDATE ERROR]")


def subprocess_failure_message(stderr, stdout):
    """The best available explanation for a failed subprocess run.

    Pulled out as a pure function so this is unit-testable without mocking
    subprocess.run() itself - matching rehash_nick_change_line()'s own
    reason for existing as a standalone function.

    #162 finding #4: update_list.py's own error handling prints via plain
    print() - stdout, not stderr - so a script-level failure (a directory
    walk that raised, a write that failed) left stderr empty, and the admin
    saw "Unknown script error" with no filename and no reason at all. Hence
    the fall back to stdout.

    TAKING THE LAST LINE STOPPED WORKING, and reported the one line that
    never explains anything. That rule was written when update_list.py's own
    "[LIST-GEN ERROR] ..." summary really was last. Its __main__ now prints
    a generic banner after it:

        [LIST-GEN ERROR] 'Music' failed: <the actual reason>
        [LIST-GEN ERROR] These lists were not rebuilt and are still serving...
        --- ERROR: could not generate the list. ---      <- always last

    so the dashboard reliably showed "--- ERROR: could not generate the
    list. ---" while the reason sat in the output above it, captured and
    discarded. Reported from a live install, where it replaced an earlier
    failure that at least said which encoding gave up.

    THE FIRST TAGGED LINE, not the last: later ones are consequences of the
    first. "These lists were not rebuilt" is true and follows from whatever
    broke the first one, and only the first names it.
    """
    output = (stderr or stdout or "").strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "Unknown script error"

    for line in lines:
        if any(tag in line for tag in _ERROR_TAGS):
            return line

    # Nothing tagged - a traceback, or a failure from somewhere that does not
    # use the tags. The last line is still the best guess, minus the banner
    # that would otherwise always win.
    useful = [line for line in lines if line not in _GENERIC_RESULT_LINES]
    return useful[-1] if useful else lines[-1]


# Reads ONLY line 1 of the real master list, so it costs almost nothing
def count_from_master_list():
    """How many files this bot is currently offering, across every list.

    Lifted out of handle_list_update_request() so it can be called directly.
    It was a closure over nothing - it reads config and the disk - and being
    one meant the only way to reach it was to run a real !update, subprocess
    and all. A glob-escaping fix to this very function could not be proved by
    any test until it moved out here.

    ONE FINDER, not a second copy of one. This used to glob the lists
    directory and filter for itself, and every time list.py learned to exclude
    another name this did not. #234 was that story with the delivered "-FULL-"
    copy. The film list was it again, and worse: "<base>-VIDEO-<date>.txt"
    sorts after a plain date ("V" > "2"), so this picked the film list, whose
    header reads "List of N Films & Series" and does not match the "List of N
    Files" pattern it was looking for. Every !update reported "0 files, added
    0" while the advert published the real total.

    Worse than a wrong number. The #230 shrink guard in the caller fires on
    `added_files < 0`, and 0 - 0 is never negative - so the one warning that
    catches a partial mount failure publishing a truncated index could never
    fire again. update_list.py's decision to let a missing folder cost only
    its own contents is safe BECAUSE that warning exists; breaking it removed
    the net underneath a deliberate choice, silently.

    Counting rows across every list rather than reading the master list's own
    header is the other half: with film in a list of its own, the header
    reports the music half alone, and !update would announce a smaller number
    than the advert publishes for the same library.
    """
    import list as _list_mod

    try:
        return _list_mod.get_file_count_date_size_and_raw_bytes()[0]
    except Exception as e:
        print(f"[LIST READ ERROR] Could not count the list: {e}")
        return 0


def count_by_list():
    """[(name, files)] for every configured list, in the operator's order.

    count_from_master_list() reads the PRIMARY list only - a call without a name
    means the primary - so a second list (films, series, whatever the operator
    called it) was never counted: the rebuild report gave the music's total and
    said nothing of the rest (#873). The names are the ones the operator gave
    the lists in lists.json; nothing here knows what they are.
    """
    import library
    import list as _list_mod

    counts = []
    for entry in library.lists():
        try:
            files = _list_mod.get_file_count_date_size_and_raw_bytes(entry.name)[0]
        except Exception as err:
            print(f"[LIST READ ERROR] Could not count the list {entry.name!r}: {err}")
            files = 0
        counts.append((entry.name, int(files or 0)))
    return counts


def describe_list_counts(old, new):
    """(sentence, shrunk) - what the rebuild report says about the lists.

    One list keeps the wording it always had. Several are named one by one, each
    with what it gained, so a second list's files are not lost in a total that
    covers only the first. `shrunk` is the [(name, was, now)] of the lists that
    ended up smaller: a folder that lost its mount does not look like "0 new".
    """
    before = dict(old)
    shrunk = [(name, before.get(name, 0), count) for name, count in new
              if count < before.get(name, 0)]
    if len(new) == 1:
        name, count = new[0]
        added = count - before.get(name, 0)
        return (f"MasterList now contains {count:,} files. "
                f"Added {added:,} new file(s) since last index."), shrunk
    parts = []
    for name, count in new:
        added = count - before.get(name, 0)
        parts.append(f"{name}: {count:,} files ({added:+,} new)")
    return "; ".join(parts) + ".", shrunk


class ListUpdateStalled(Exception):
    """The child stopped reporting for longer than the stall window.

    Its own exception rather than subprocess.TimeoutExpired, because the two
    now mean different things and the operator is told different things: this
    one says the rebuild went silent, which points at the library; the other
    that it ran past an absolute cap the operator set deliberately.
    """

    def __init__(self, silent_for):
        self.silent_for = silent_for
        super().__init__(f"no progress for {int(silent_for)}s")


def last_progress_at():
    """When the running rebuild last reported, or None if we cannot tell.

    None is "cannot tell", never "a long time ago". A rebuild that cannot
    write its progress file - a full disk, a read-only data/ - is not evidence
    of a rebuild that is stuck, and killing one for it would turn a cosmetic
    failure into the loss of an eight-hour run.
    """
    import json
    import platform_compat

    path = getattr(config, "LIST_PROGRESS_FILE", None)
    if not path:
        return None
    try:
        with open(platform_compat.long_path(path), encoding="utf-8") as handle:
            loaded = json.load(handle)
        return float(loaded["at"])
    except Exception:
        return None


def run_watching_for_a_stall(argv, ceiling=0, stall=900, tick=5.0):
    """Run the list builder, killing it only when it stops making progress.

    Returns the same CompletedProcess shape subprocess.run() did, so the
    caller's success path is unchanged.

    THREE RULES:

      * The child reports to LIST_PROGRESS_FILE roughly twice a second while
        scanning. While that timestamp is advancing the rebuild is working -
        for as many hours as it needs - and nothing here interferes.
      * If it has not advanced for `stall` seconds, the rebuild is wedged and
        is killed. Fifteen minutes by default: more generous than the old flat
        thirty-minute cap for a long run, and quicker than it for a genuinely
        hung one.
      * `ceiling` is an absolute cap for an operator who wants one anyway.
        0, the default, means none.

    A rebuild that has never written a progress file is never killed for
    stalling. That is the case where working and stuck cannot be told apart,
    and the honest answer is to let the ceiling decide - which is what an
    operator who set one asked for, and no limit at all otherwise.
    """
    import subprocess
    # Imported here like every other use in this module - commands.py has no
    # module-level `time`, and this function is called from a nested one that
    # imports its own.
    import time

    begun = time.time()
    process = subprocess.Popen(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace")
    while True:
        try:
            # communicate() is the only safe way to wait while draining the
            # pipes: a child that fills its stderr buffer blocks forever
            # otherwise, and this one prints a line per folder. Calling it
            # again after TimeoutExpired is the documented way to keep
            # waiting - it resumes rather than restarting.
            out, err = process.communicate(timeout=tick)
            return subprocess.CompletedProcess(argv, process.returncode,
                                               out, err)
        except subprocess.TimeoutExpired:
            pass

        now = time.time()
        if ceiling and (now - begun) >= ceiling:
            process.kill()
            process.communicate()
            raise subprocess.TimeoutExpired(argv, ceiling)

        reported_at = last_progress_at()
        if reported_at is None:
            # Cannot tell. Never kill for it.
            continue
        silent_for = now - reported_at
        if silent_for >= stall:
            process.kill()
            process.communicate()
            raise ListUpdateStalled(silent_for)


def describe_duration(seconds):
    """A rebuild's runtime, for a person rather than for arithmetic.

    Whole seconds under a minute, "2m 04s" above it, "1h 12m" above an hour -
    a rebuild of a large library on a mapped drive runs into the hours, and
    "4331s" is a number nobody converts in their head.

    The smaller unit is zero-padded so consecutive rebuilds line up when they
    are read one under the other in a log.
    """
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "an unknown time"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def handle_list_update_request(user, target_chan, authorised=False, user_host=None):
    """Run update_list.py, wait for it, and read the file count from line 1 of the list."""
    import subprocess
    import sys
    import os
    import re
    import defaults as config
    import announce
    import glob
    import threading
    import time
    
    if not authorised and not is_admin(user, user_host):
        print(f"[SECURITY] Unauthorised user {user} tried to run !update.")
        return

    # #162 finding #17: this used to be the ONLY re-entrancy guard, and it lived
    # inside the PAUSE_ON_UPDATE branch below - with PAUSE_ON_UPDATE=False there was
    # no guard at all, and three consecutive !update calls launched three concurrent
    # subprocesses all writing the same .new temp paths. config.update_inprogress is
    # set unconditionally a few lines down and cleared only in async_list_updater's
    # finally, regardless of PAUSE_ON_UPDATE, so it is the right flag to gate on here.
    # CHECKED AND SET AS ONE STEP (#444). This used to read the flag here and
    # set it 178 lines below, with the PAUSE_ON_UPDATE wait for a running
    # search in between - so two !update requests arriving in that window both
    # passed the guard and both started a rebuild, two subprocesses writing
    # the same .new temp paths.
    #
    # Every path that returns after this point must put the flag back, or the
    # bot refuses every future update until it restarts. There is exactly one
    # such path today - the search-already-running denial below - and it does.
    #
    # The search flag is checked and set inside the SAME gate (#607). It used
    # to be read and raised just below it, outside the lock - and every @find
    # runs on its own thread and raises the same flag, so a search arriving in
    # that window passed its own guard while this request passed this one,
    # and the rebuild's finally then cleared a flag the searcher still relied
    # on. list.execute_search() takes this gate for its check-and-set too, so
    # whichever of the two gets the lock first is the one the other sees.
    paused_searches = False
    with runtime.list_update_gate:
        if getattr(config, 'update_inprogress', False) is True:
            announce.send_debug(f"List update request from {user} denied: An update is already running.", category="INFO")
            return
        config.update_inprogress = True

        # The global maintenance lock is only taken if the switch is True in config
        if getattr(config, 'PAUSE_ON_UPDATE', True) is True:
            if getattr(config, 'search_inprogress', False) is True:
                announce.send_debug(f"List update request from {user} denied: Another system scan is already running.", category="INFO")
                # The flag was raised by the gate above and this request is not
                # going to use it. Leaving it set would deny every later update
                # for the life of the process.
                config.update_inprogress = False
                return
            config.search_inprogress = True
            paused_searches = True

    if paused_searches:
        print(f"[MAINTENANCE START] {user} ran !update. Searching and sharing are now PAUSED.")
        announce.send_debug(f"System maintenance initiated by {user}. MasterList is rebuilding, file requests temporarily paused...", category="INFO")
    else:
        print(f"[UPDATE START] {user} ran !update. The pause switch is False, so sharing continues meanwhile.")
        announce.send_debug(f"List update triggered by {user} from {target_chan}. Indexing the music directory...", category="INFO")

    # 1. Take the previous real file count from line 1
    old_counts = count_by_list()
    announce.send_debug(f"List update triggered by {user} from {target_chan}. Indexing the music directory, bot paused...", category="INFO")
    def async_list_updater():
        # HOW LONG THE WHOLE THING TOOK, from here rather than from the
        # subprocess's own progress file. That file reports the scan; this
        # measures what the operator actually waited for, which also covers
        # spawning python, the NFS sync pause after the child exits, and the
        # re-count afterwards. On a slow mount those are not rounding.
        #
        # Recorded on EVERY exit below - success, failure, timeout and the
        # unexpected - because a duration left over from the last run is worse
        # than none: it would be attached to a rebuild it did not measure.
        started = time.time()
        try:
            base_path = os.path.dirname(os.path.abspath(__file__))
            script_path = os.path.join(base_path, "update_list.py")
            
            if not os.path.exists(script_path):
                announce.send_debug(f"Critical Error: Could not find update_list.py", category="INFO")
                # #224: build_update_list_status_payload() used to report only
                # {"running": bool} - nothing recorded whether the rebuild that
                # just finished actually succeeded, so the dashboard's poll
                # showed "Done. Check Stats for the new file count." the moment
                # `running` flipped false, whether the rebuild worked or not.
                config.last_list_update_ok = False
                config.last_list_update_error = "update_list.py not found"
                config.last_list_update_seconds = int(time.time() - started)
                return
                
            # 2. Threaded run. A hung mount must not wedge
            # config.search_inprogress / config.update_inprogress permanently -
            # but a rebuild that is still working is NOT hung, and a wall clock
            # cannot tell the two apart. This was a flat 1800s, which is a bet
            # that no library takes longer than half an hour to walk; an 80 TB
            # library on a mapped drive takes hours, and lost the whole run at
            # the thirty-minute mark every single time.
            #
            # So the child is watched rather than timed: it reports what it is
            # doing to LIST_PROGRESS_FILE about twice a second, and going
            # silent is the thing that means something. See
            # run_watching_for_a_stall() for the rules.
            list_update_timeout = getattr(config, 'LIST_UPDATE_TIMEOUT', 0)
            # utf-8 with errors="replace", NOT the locale code page.
            # text=True alone decodes the child with whatever the console
            # uses - cp1253 on a Greek install - and one byte outside it
            # kills subprocess's own reader thread with UnicodeDecodeError.
            # The run then reports "Unknown script error" because the
            # output that would have explained it is what could not be
            # read. The child guards its own writes now (see
            # update_list.py); this guards our reading of them, so a
            # future child that does not still cannot take the daemon's
            # report away with it.
            process = run_watching_for_a_stall(
                [sys.executable, script_path],
                ceiling=list_update_timeout,
                stall=getattr(config, 'LIST_UPDATE_STALL_SECONDS', 900))
            
            if process.returncode == 0:
                # ---------------------------------------------------------------------
                # NFS and disk sync: wait two seconds after the process closes.
                # That gives the NAS and the network buffer time to flush the files.
                # ---------------------------------------------------------------------
                print(f"[UPDATE-SYNC] The script finished. Waiting 2.0s for the disk to sync before reading...")
                time.sleep(2.0)
                # ---------------------------------------------------------------------
                
                # 3. Read the new file count from line 1
                new_counts = count_by_list()

                # Work out the exact difference, list by list (#873)
                summary, shrunk = describe_list_counts(old_counts, new_counts)

                # #230: clamping straight to zero made a SHRUNK library read
                # identically to an unchanged one - "Added 0 new file(s)" - even
                # though files were lost. generate_master_list()'s own zero-file
                # guard only refuses a scan that found literally nothing; a
                # partial mount failure that still returns SOME files (fewer
                # than before) passes that guard, publishes a truncated index,
                # and the operator who just lost real files from their share was
                # told nothing changed.
                if shrunk:
                    # No dedicated warning category exists in send_debug() (see
                    # its own category list) - "INFO" here, same as the normal
                    # path, since the wording itself is what carries the
                    # warning; inventing a category that falls through to the
                    # same [INFO] tag anyway would only look distinct without
                    # being distinct.
                    for name, was, now in shrunk:
                        # Named by the list, and the folder it says to check
                        # is that list's own: "the music directory" is a
                        # wrong pointer when it is the films that vanished.
                        several = len(new_counts) > 1
                        which = f" of the list {name!r}" if several else ""
                        where = "that list's folder/mount" if several else "the music directory/mount"
                        announce.send_debug(
                            f"List update completed, but the file count{which} DROPPED from "
                            f"{was:,} to {config.C_BOLD}{now:,}{config.C_RESET} "
                            f"({was - now:,} fewer). Check {where} "
                            f"before trusting this list.",
                            category="INFO"
                        )
                    if len(new_counts) > 1:
                        # A list that shrank must not hide what the others
                        # gained: with several lists the summary follows the
                        # warning. One list keeps the warning alone, as before.
                        announce.send_debug(
                            f"List update completed in "
                            f"{describe_duration(time.time() - started)}. {summary}",
                            category="INFO"
                        )
                else:
                    # 4. Confirm, through the VIP express lane
                    announce.send_debug(
                        f"List update successfully completed in "
                        f"{describe_duration(time.time() - started)}! {summary}",
                        category="INFO"
                    )
                # The script itself succeeded either way - #230's shrink
                # warning above is a caution about the RESULT, not a failure
                # of the rebuild process #224 is about.
                config.last_list_update_ok = True
                config.last_list_update_error = None
                config.last_list_update_seconds = int(time.time() - started)

            else:
                error_msg = subprocess_failure_message(process.stderr, process.stdout)
                # A rebuild that failed is still failed tomorrow: the list
                # being served is the last good one and nothing retries on its
                # own, so this is the operator's to act on.
                announce.send_debug(
                    f"External update_list.py failed (Exit Code "
                    f"{process.returncode}): {error_msg}",
                    category="INFO", notice="error")
                config.last_list_update_ok = False
                config.last_list_update_error = error_msg
                config.last_list_update_seconds = int(time.time() - started)

        except ListUpdateStalled as stall_err:
            # Distinct from the timeout below on purpose: this one means the
            # rebuild went QUIET, which points at the library - a mount that
            # went away mid-walk - rather than at a limit the operator set.
            announce.send_debug(
                f"List update ABANDONED: nothing reported for "
                f"{describe_duration(stall_err.silent_for)}. The library it "
                f"was reading may have gone away; the previous list is still "
                f"being served.",
                category="INFO", notice="error")
            config.last_list_update_ok = False
            config.last_list_update_error = (
                f"stalled - nothing reported for "
                f"{describe_duration(stall_err.silent_for)}")
            config.last_list_update_seconds = int(time.time() - started)
        except subprocess.TimeoutExpired:
            announce.send_debug(
                f"List update FAILED: Script execution timed out after "
                f"{list_update_timeout} seconds.",
                category="INFO", notice="error")
            config.last_list_update_ok = False
            config.last_list_update_error = f"timed out after {list_update_timeout}s"
            config.last_list_update_seconds = int(time.time() - started)
        except Exception as e:
            print(f"[UPDATE ERROR] The list update could not be run: {e}")
            announce.send_debug(f"List update FAILED critical error: {e}", category="INFO")
            config.last_list_update_ok = False
            config.last_list_update_error = str(e)
            config.last_list_update_seconds = int(time.time() - started)
        finally:
            # Release the global pause lock again - but only if THIS request
            # raised it. With PAUSE_ON_UPDATE off the flag belongs to whatever
            # @find is running, and clearing it here let a second search in.
            if paused_searches:
                config.search_inprogress = False

            # Clear the maintenance flag, so list.py knows the list is ready
            config.update_inprogress = False
            print("[MAINTENANCE END] Sharing and searching have been restarted automatically.")

    # The maintenance flag was raised by the gate at the top of this function,
    # as one step with the check that guards it (#444). It is deliberately not
    # re-raised here: a second assignment would read as though something in
    # between might have cleared it.

    # Start the background thread
    threading.Thread(target=async_list_updater, daemon=True).start()


# ---------------------------------------------------------------------------
# @<nick>-stats and @<nick>-top
#
# The numbers the bot already knows, on demand and in private. The advert
# carries most of what -stats reports, but it fires on a timer into a busy
# channel: somebody who missed it, or who is in a PM, had no way to ask. -top
# is not published anywhere except the web dashboard, which only the operator
# can see.
#
# Both answer by NOTICE to whoever asked, like every other user command here,
# so a stranger looking the bot over costs the channel nothing.
# ---------------------------------------------------------------------------

# Long enough for a real "Artist - Title" to stay recognisable. In BYTES, not
# characters: a library of Bjork, Sigur Ros and Motley Crue costs two bytes a
# letter, and five names clamped to 44 CHARACTERS came to 596 bytes against a
# 420 budget - the same trap announce.fit_irc_line() measures in bytes for.
TOP_NAME_BYTES = 44

# Five each. Ten would need a second line per section, and four notices for a
# command a stranger can repeat is more of the channel's flood budget than a
# curiosity is worth.
TOP_ROWS = 5


def _clamp_name(name, limit=None):
    """A display name short enough to line up, measured in bytes.

    The limit is read on every call rather than bound as a default argument.
    A default is evaluated once, at import, so `limit=TOP_NAME_BYTES` made the
    constant look tunable while ignoring every later change to it - including
    the one a test needs to reach the "not even one entry fits" branch in
    section() below.

    The head is kept, not the tail: "Bach - Goldberg Variations - Aria da
    Capo" tells you what it is and "...ria da Capo" does not.

    errors="ignore" on the decode is what drops a multi-byte character the cut
    landed in the middle of. Half a character is not a shorter name, it is a
    mojibake box in everybody's client.
    """
    limit = TOP_NAME_BYTES if limit is None else limit
    text = " ".join(str(name).split())
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit - 3].decode("utf-8", "ignore").rstrip() + "..."


def handle_stats_request(s, user, target):
    """Answer "@<nick>-stats" with what this bot has and what it has sent."""
    oserve = sys.modules.get('oserve')
    if not oserve:
        return

    import list as list_mod
    import dcc
    import stats_mgr

    bold, green, red, reset = (config.C_BOLD, config.C_GREEN,
                               config.C_RED, config.C_RESET)

    def figure(text, colour=green):
        return f"{bold}{colour}{text}{reset}"

    shared_count, list_date, shared_size, _raw = \
        list_mod.get_file_count_date_size_and_raw_bytes()

    total_sent = stats_mgr.get_total_sent()
    total_bytes = stats_mgr.get_total_sent_bytes()

    # _rolled, not the raw row: the day is only rotated on disk when a transfer
    # completes, so a bot that has sent nothing since midnight still has
    # yesterday's numbers in the Today columns. The dashboard reads it rolled
    # and so must this, or the same bot answers two different numbers depending
    # on which one you ask.
    yesterday_files = today_files = 0
    try:
        row = db.load_advanced_stats_rolled()
        if len(row) > 5:
            yesterday_files, today_files = row[2], row[4]
    except Exception as err:
        print(f"[COMMANDS] Could not read the day figures for {user}: {err}")

    active = oserve.active_downloads if oserve else 0
    free_slots = max(0, config.MAX_DCC_SLOTS - active)
    queued = dcc.get_total_queued_count()

    speed_now = stats_mgr.format_speed(stats_mgr.live_speed())
    record = stats_mgr.format_speed(db.get_speed_record())

    # get_file_count_date_size_and_raw_bytes() answers "No List" as the DATE
    # when there is nothing to read, which reads as a date in the sentence it
    # was going into. A bot that has not built its list yet is the state every
    # fresh install is in, and the first thing somebody would ask about.
    #
    # #433: "Error" is the other sentinel the same function can answer with -
    # an OSError reading any list file collapses its whole return to
    # (0, "Error", "0B", 0), and "Error" != "No List" let it straight through
    # here too: "Sharing 0 files (0B), list built Error." answered a direct
    # question with exactly the fault, not with "not built yet".
    if shared_count or list_date not in ("No List", "Error"):
        shared_line = (f"Sharing {figure(f'{shared_count:,}')} files "
                       f"({figure(shared_size)}), list built {figure(list_date, red)}.")
    else:
        shared_line = "No list has been built yet, so there is nothing to share."

    lines = [
        shared_line,

        f"Sent {figure(f'{total_sent:,}')} files "
        f"({figure(stats_mgr.format_size_human(total_bytes))}) all time - "
        f"{figure(yesterday_files, red)} yesterday, {figure(today_files, red)} today.",

        f"Slots {figure(f'{free_slots}/{config.MAX_DCC_SLOTS}')} free, "
        f"{figure(queued)} queued. Speed {figure(speed_now)}, "
        f"record {figure(record)}. Up {figure(_format_uptime(stats_mgr.get_uptime_seconds()))}.",
    ]

    for line in lines:
        # #426: see handle_help_request()'s own comment - a per-user reply
        # does not belong in the lane channel adverts exist for.
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n")

    print(f"[STATS] Sent the stats notice to {user} (asked in {target}).")


def _format_uptime(seconds):
    """Days and hours, or hours and minutes below a day."""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def handle_top_request(s, user, target):
    """Answer "@<nick>-top" with the most-requested files and albums."""
    oserve = sys.modules.get('oserve')
    if not oserve:
        return

    import announce

    bold, green, red, reset = (config.C_BOLD, config.C_GREEN,
                               config.C_RED, config.C_RESET)

    # What the envelope costs, so the entries are measured against what is
    # actually left of the line.
    envelope = len(f"NOTICE {user} :\r\n".encode("utf-8"))

    def section(label, rows):
        """As many rows as fit, in order.

        Clamping each name is not enough on its own: five clamped names plus
        five counts plus a label is still a sum, and a sum of things that each
        fit does not have to fit.
        """
        parts = []
        for position, row in enumerate(rows, 1):
            parts.append(f"{bold}{red}{position}.{reset} {_clamp_name(row['name'])} "
                         f"{bold}{green}({row['count']}){reset}")
            candidate = f"{label}: " + "  ".join(parts)
            if envelope + len(candidate.encode("utf-8")) > announce.IRC_LINE_BUDGET:
                parts.pop()
                break
        return f"{label}: " + "  ".join(parts) if parts else ""

    try:
        files = db.top_downloads(limit=TOP_ROWS, kind="file")
        # Albums are counted whether or not folder packing is on - the counter
        # records what was sent, and turning !rar off later does not unsend it.
        # They are only OFFERED here when the bot will still pack one: a list
        # of albums nobody can request reads as a menu, which is the mistake
        # #153 took out of the album list.
        albums = (db.top_downloads(limit=TOP_ROWS, kind="album")
                  if getattr(config, "RAR_ENABLED", True) else [])
    except Exception as err:
        print(f"[COMMANDS] Could not read the download counts for {user}: {err}")
        files, albums = [], []

    lines = [line for line in (section("Most requested files", files),
                               section("Most requested albums", albums)) if line]
    if not lines:
        lines.append(
            f"Nothing has been requested yet - the counter starts at the first "
            f"send. Get my list with {bold}{red}@{config.NICKNAME}{reset}.")

    for line in lines:
        # #426: see handle_help_request()'s own comment - a per-user reply
        # does not belong in the lane channel adverts exist for.
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n")

    print(f"[TOP] Sent the most-requested notice to {user} (asked in {target}).")
