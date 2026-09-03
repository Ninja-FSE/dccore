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


def is_admin(user):
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

    KNOWN LIMITATION: this is still nick-based, and an Undernet nick is not owned without
    services auth - anyone can take the nick while the real admin is offline and gain
    every admin command, now including the destructive !clearqueue. Closing that properly
    means matching ident@host, which irc.py does not currently capture: its PRIVMSG regex
    keeps only the nick. That is a separate change to irc.py plus this file.
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
    return str(user).lower() in allowed


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
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n", is_vip=True)

    print(f"[HELP] Sent the usage notice to {user} (asked in {target}).")


def handle_queue_check(s, user, target):
    """Count one user's queued files and answer them through the VIP queue."""
    user_key = user.lower()
    oserve = sys.modules.get('oserve')
    import list
    import dcc
    
    # 1. How many files this particular user has queued
    file_count = 0
    if hasattr(config, 'dcc_queue') and user_key in config.dcc_queue:
        file_count = len(config.dcc_queue[user_key])
        
    # 2. Live statistics, for the fuller empty-queue notice
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
        oserve.queue_message(user, msg, is_vip=True)
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

def handle_admin_clear_queue(user, target_chan, msg_text, authorised=False):
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

    if not authorised and not is_admin(user):
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

def handle_ping_request(irc_sock, user, target_chan):
    """Start the timer and send a unique latency PING to the IRC server."""
    import time
    import defaults as config
    
    # Keep the measurement in shared memory so the pong handler can read it later
    config.ping_start_time = time.time()
    config.ping_triggered_by = user
    config.ping_channel_source = target_chan
    
    # Send the probe straight to the server's raw socket
    try:
        irc_sock.send(b"PING :OSERVE_LATENCY_CHECK\r\n")
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
#   user_processing_lock  packer that wedged; !rehash is the only way to clear them
PRESERVE_RUNTIME = (
    'active_transfers',   # losing this reports 0 active slots while transfers run,
                          # so the bot admits work beyond MAX_DCC_SLOTS
    'banned_users',       # every timed ban silently released
    'frozen_queues',      # freeze timers lost, so departed users' queues never expire
    'muted_until',        # flood mutes released
    'whois_status',
    'user_requests',      # flood history, so a flooder gets a clean slate
    'failed_transfers',   # per-file retry counters
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
)


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

    Kept general rather than assuming `value is current` always holds (true
    today for every name in PRESERVE_RUNTIME, since all of them are
    runtime.py-bound) - if a future preserved key ever is NOT bound that
    way, reload really would hand back a fresh, empty container, and the
    merge below still produces the right content; only the final write
    changes, from rebind to in-place mutation.
    """
    restored = set()
    for key, value in preserved_runtime.items():
        current = getattr(cfg, key, None)
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

    reloaded = []
    for mod_name in modules:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
            reloaded.append(mod_name)

    # Last, and separately: reloading this module rebinds the very names the
    # caller is executing out of. The running frame keeps its old code object,
    # which is what makes this safe here and would not be if it ran first.
    if reload_self and 'commands' in sys.modules:
        importlib.reload(sys.modules['commands'])
        reloaded.append('commands')
    return reloaded


def handle_rehash_request(user, target_chan, authorised=False):
    """Reload the modules live, in memory, without reading anything back from disk."""
    import importlib
    import sys
    import defaults as config
    import announce
    import copy
    
    if not authorised and not is_admin(user):
        print(f"[REHASH SECURITY] Ignored a rehash attempt from an unauthorised user: {user}")
        return

    # =====================================================================
    # 1. Back EVERY piece of live state up into local variables
    # =====================================================================
    # A. The channel user lists (from NAMES)
    ram_backup_users = {}
    backed_up_users = False
    with runtime.channel_users_lock():
        if hasattr(config, 'channel_users') and isinstance(config.channel_users, dict):
            ram_backup_users = copy.deepcopy(config.channel_users)
            backed_up_users = True
    if backed_up_users:
        print(f"[REHASH RAM] Backed up the user lists for {len(ram_backup_users)} channel(s).")

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

    # Keep the old channel list, for the JOIN/PART comparison
    old_chans = [c.strip().lower() for c in config.CHANNEL.split(",") if c.strip()]

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

    try:
        # 2. REHASH: reload every core module live, in memory. The ORDER matters
        # and is documented on reload_modules_in_order() itself.
        reload_modules_in_order()

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
            _nick_line = rehash_nick_change_line(baseline_nick, _cfg.NICKNAME)
            if _nick_line:
                _oserve_for_nick = sys.modules.get('oserve')
                _live_sock_for_nick = getattr(_oserve_for_nick, 'irc_connection', None) if _oserve_for_nick else None
                if _live_sock_for_nick:
                    try:
                        _live_sock_for_nick.send(_nick_line.encode())
                        print(f"[REHASH NICK] Sent a live NICK change to {_cfg.NICKNAME!r}.")
                    except Exception as _nick_err:
                        print(f"[REHASH NICK ERROR] Could not send the live nick change: {_nick_err}")
                else:
                    print("[REHASH NICK] No live socket available; the new nickname "
                          "will take effect on the next reconnect instead.")

        import announce as _ann
        # Only reinstate the token if nothing newer claimed it. A reconnect completing inside
        # the reload window starts a fresh advert worker and stamps a higher id; restoring
        # blindly would retire that new worker and leave the channels silent.
        if live_worker_id and not getattr(_ann, 'current_worker_id', 0):
            _ann.current_worker_id = live_worker_id
            print("[REHASH RAM] Advert worker kept alive across the reload.")
            print("[REHASH NOTE] announce_worker's own code is NOT re-entered by a rehash; "
                  "restart the daemon to pick up changes to the advert loop itself.")

        # Reinstate every console session's debug sink - see reattach_debug_sinks()'s
        # docstring for why this is not optional.
        _reattached_sinks = reattach_debug_sinks(_ann, live_debug_sinks)
        if _reattached_sinks:
            print(f"[REHASH RAM] Reattached {len(_reattached_sinks)} admin console debug sink(s).")

        # Read the freshly reloaded config
        import defaults as config
        import announce
        announce.is_ready = True
        
         # =====================================================================
        # 3. RESTORE: write every value back into the newly loaded modules
        # =====================================================================
        # Restore the users
        # In place: rebinding would detach config.channel_users from the
        # object runtime.py holds, and ram_backup_users is a deep COPY, so
        # the two would diverge from here on (see runtime.py's docstring).
        with runtime.channel_users_lock():
            config.channel_users.clear()
            if ram_backup_users:
                config.channel_users.update(ram_backup_users)
            restored_count = len(config.channel_users)
        print(f"[REHASH RAM] Restored {restored_count} channel list(s) into the new modules.")

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
            
        # ---------------------------------------------------------------------
        # 4. FULLY AUTOMATIC CHANNEL SYNC (JOIN NEW / PART REMOVED)
        # ---------------------------------------------------------------------
        oserve = sys.modules.get('oserve')
        irc_sock = getattr(oserve, 'irc_connection', None)
        
        if irc_sock:
            new_chans = [c.strip().lower() for c in config.CHANNEL.split(",") if c.strip()]
            
            for chan in new_chans:
                if chan not in old_chans:
                    irc_sock.send(f"JOIN {chan}\r\n".encode())
                    announce.send_debug(f"Joining channel {chan} due to new configuration layout!", category="JOIN")
                    with runtime.channel_users_lock():
                        if chan.lower() not in config.channel_users:
                            config.channel_users[chan.lower()] = set()
            
            for chan in old_chans:
                if chan not in new_chans:
                    # Not a channel name. config.py declares DEBUG_CHANNEL as
                    # "" (blank) since #193, so a literal "#example-debug" here was a second
                    # source of truth that disagreed with the first - and the
                    # one place it would have been consulted is the one place it
                    # decides whether to PART a channel.
                    debug_chan = str(getattr(config, 'DEBUG_CHANNEL', '') or '').lower()
                    if chan != debug_chan:
                        irc_sock.send(f"PART {chan} :Removed from DDCore\r\n".encode())
                        announce.send_debug(f"Parting channel {chan} due to new configuration layout!", category="PART")
                        with runtime.channel_users_lock():
                            if chan.lower() in config.channel_users:
                                del config.channel_users[chan.lower()]
            
            print("[REHASH SYNC] Sending a background NAMES to keep the lists fresh...")
            for chan in new_chans:
                irc_sock.send(f"NAMES {chan}\r\n".encode())
                
            print(f"[REHASH SYNC] Channel sync completed successfully.")
        else:
            print("[REHASH WARNING] Could not sync the channels: no raw socket was available.")
        # ---------------------------------------------------------------------
        
        # 5. Confirm, through the VIP express lane
        announce.send_debug(f"Rehash completed! RAM-Memory preserved seamlessly without disk-paging.", category="INFO")
        
        # Clear any stale locks and ghost blocks left over before the rehash
        config.rar_inprogress = False
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock = set()

        # Take the real, live network socket straight from memory
        oserve_mod = sys.modules.get('oserve')
        live_socket = getattr(oserve_mod, 'irc_connection', None) if oserve_mod else None
        
        if live_socket:
            import dcc
            import threading
            print("[REHASH-WAKE] Letting queued users into the free slots...")
            threading.Thread(
                target=dcc.check_queue_and_send, 
                args=(live_socket, "system_next_trigger_fallback"), 
                daemon=True
            ).start()
        else:
            print("[REHASH ERROR] Could not wake the queue automatically: live_socket was not in memory.")
        
    except Exception as e:
        import announce
        announce.is_ready = True
        print(f"[REHASH CRITICAL ERROR] The files could not be reloaded live: {e}")
        announce.send_debug(f"Rehash FAILED (Notices Resumed for safety): {e}", category="INFO")


def handle_hard_ban_request(user, target_chan, msg_text, authorised=False):
    """Add a permanent wildcard pattern to hard_bans.txt, straight from IRC."""
    import defaults as config
    import announce
    
    if not authorised and not is_admin(user):
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

def handle_hard_unban_request(user, target_chan, msg_text, authorised=False):
    """Remove a permanent wildcard pattern from hard_bans.txt, straight from IRC."""
    import defaults as config
    import announce
    import os
    
    if not authorised and not is_admin(user):
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

def subprocess_failure_message(stderr, stdout):
    """The best available explanation for a failed subprocess run.

    Pulled out as a pure function so this is unit-testable without mocking
    subprocess.run() itself - matching rehash_nick_change_line()'s own
    reason for existing as a standalone function.

    #162 finding #4: update_list.py's own error handling prints via plain
    print() - stdout, not stderr - so a script-level failure (a directory
    walk that raised, a write that failed) left stderr empty, and the
    admin saw "Unknown script error" with no filename and no reason at
    all. Falls back to stdout, and takes its LAST line - where
    update_list.py's own "[LIST-GEN ERROR] ..." summary lands - mirroring
    how update_list.py's own _write_rar_artifact() already reports a
    subprocess failure.
    """
    output = (stderr or stdout or "").strip()
    lines = output.splitlines()
    return lines[-1] if lines else "Unknown script error"


# Reads ONLY line 1 of the real master list, so it costs almost nothing
def count_from_master_list():
    """The file count on line 1 of the newest master list, or 0.

    Lifted out of handle_list_update_request() so it can be called directly.
    It was a closure over nothing - it reads config and the disk - and being
    one meant the only way to reach it was to run a real !update, subprocess
    and all. A glob-escaping fix to this very function could not be proved
    by any test until it moved out here.

    The three imports are local because they were local in the enclosing
    function, and the closure was reading them out of ITS scope - lifting the
    body out without them raised NameError('os') on the first call, caught by
    the broad except below and reported as a count of 0. Which is to say the
    extraction reproduced the exact bug it was made to prove was fixed.
    """
    import glob
    import os
    import re

    try:
        # Find every text file in the directory matching the bot's name
        # LIST_BASE_NAME, not NICKNAME: irc.py rebinds NICKNAME on a 433 fallback, and
        # update_list.py names the files with LIST_BASE_NAME. Keyed off the live nick this
        # counted 0 both before and after a rebuild, so !update reported "0 files, added 0"
        # while the advert reported the real total. Matches list.find_latest_list().
        # glob.escape both halves: "[" and "]" are a character class to glob, and
        # both are ordinary in the two values interpolated here. Bot[GR] is a
        # standard IRC nick, and LIST_BASE_NAME follows NICKNAME by default; a
        # music share under D:\Lists[FLAC]\ is the same bug from the other side.
        # Unescaped, the pattern matched nothing and never errored: @find answered
        # "No MasterList found" and the advert published "0 Files" forever.
        pattern = os.path.join(glob.escape(config.LOCAL_LIST_DIR),
                               f"{glob.escape(config.LIST_BASE_NAME)}-*.txt")
        all_txt_files = sorted(glob.glob(pattern))
        
        # Both markers, matching list.find_latest_list(). Excluding only
        # "-RAR-" left the DELIVERED "-FULL-" copy in the running, and it sorts
        # after any plain date suffix - so this counted a different file from the
        # one @find and the advert read. It agreed only because that copy happens
        # to carry the same header (#234).
        import list as _list_mod
        true_master_lists = [f for f in all_txt_files
                             if "-RAR-" not in f
                             and _list_mod.FULL_LIST_MARKER not in f]
        
        if true_master_lists:
            list_path = true_master_lists[-1]  # The very newest master list
            if os.path.exists(list_path):
                with open(list_path, "r", encoding="utf-8", errors="ignore") as f:
                    first_line = f.readline().strip()
                    
                    # Look for the "List of X Files" pattern
                    match = re.search(r"List of\s+([\d,.]+)\s+Files", first_line, re.IGNORECASE)
                    if match:
                        raw_num = match.group(1).replace(",", "").replace(".", "")
                        if raw_num.isdigit():
                            return int(raw_num)
    except Exception as e:
        print(f"[LIST READ ERROR] Could not read line 1: {e}")
    return 0


def handle_list_update_request(user, target_chan, authorised=False):
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
    
    if not authorised and not is_admin(user):
        print(f"[SECURITY] Unauthorised user {user} tried to run !update.")
        return

    # #162 finding #17: this used to be the ONLY re-entrancy guard, and it lived
    # inside the PAUSE_ON_UPDATE branch below - with PAUSE_ON_UPDATE=False there was
    # no guard at all, and three consecutive !update calls launched three concurrent
    # subprocesses all writing the same .new temp paths. config.update_inprogress is
    # set unconditionally a few lines down and cleared only in async_list_updater's
    # finally, regardless of PAUSE_ON_UPDATE, so it is the right flag to gate on here.
    if getattr(config, 'update_inprogress', False) is True:
        announce.send_debug(f"List update request from {user} denied: An update is already running.", category="INFO")
        return

    # The global maintenance lock is only taken if the switch is True in config
    if getattr(config, 'PAUSE_ON_UPDATE', True) is True:
        if getattr(config, 'search_inprogress', False) is True:
            announce.send_debug(f"List update request from {user} denied: Another system scan is already running.", category="INFO")
            return
        config.search_inprogress = True
        print(f"[MAINTENANCE START] {user} ran !update. Searching and sharing are now PAUSED.")
        announce.send_debug(f"System maintenance initiated by {user}. MasterList is rebuilding, file requests temporarily paused...", category="INFO")
    else:
        print(f"[UPDATE START] {user} ran !update. The pause switch is False, so sharing continues meanwhile.")
        announce.send_debug(f"List update triggered by {user} from {target_chan}. Indexing the music directory...", category="INFO")

    # 1. Take the previous real file count from line 1
    old_count = count_from_master_list()
    announce.send_debug(f"List update triggered by {user} from {target_chan}. Indexing the music directory, bot paused...", category="INFO")
    def async_list_updater():
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
                return
                
            # 2. Threaded run, bounded by LIST_UPDATE_TIMEOUT (default 1800s, shaped
            # like dcc.py's RAR_TIMEOUT) rather than waiting forever. A full NFS walk
            # legitimately takes minutes, so this is generous rather than tight - the
            # point is only that a hung mount cannot wedge config.search_inprogress /
            # config.update_inprogress permanently. subprocess.run() kills the child
            # itself when the timeout fires.
            list_update_timeout = getattr(config, 'LIST_UPDATE_TIMEOUT', 1800)
            process = subprocess.run([sys.executable, script_path], capture_output=True, text=True, timeout=list_update_timeout)
            
            if process.returncode == 0:
                # ---------------------------------------------------------------------
                # NFS and disk sync: wait two seconds after the process closes.
                # That gives the NAS and the network buffer time to flush the files.
                # ---------------------------------------------------------------------
                print(f"[UPDATE-SYNC] The script finished. Waiting 2.0s for the disk to sync before reading...")
                time.sleep(2.0)
                # ---------------------------------------------------------------------
                
                # 3. Read the new file count from line 1
                new_count = count_from_master_list()

                # Work out the exact difference
                added_files = new_count - old_count

                # #230: clamping straight to zero made a SHRUNK library read
                # identically to an unchanged one - "Added 0 new file(s)" - even
                # though files were lost. generate_master_list()'s own zero-file
                # guard only refuses a scan that found literally nothing; a
                # partial mount failure that still returns SOME files (fewer
                # than before) passes that guard, publishes a truncated index,
                # and the operator who just lost real files from their share was
                # told nothing changed.
                if added_files < 0:
                    # No dedicated warning category exists in send_debug() (see
                    # its own category list) - "INFO" here, same as the normal
                    # path, since the wording itself is what carries the
                    # warning; inventing a category that falls through to the
                    # same [INFO] tag anyway would only look distinct without
                    # being distinct.
                    announce.send_debug(
                        f"List update completed, but the file count DROPPED from "
                        f"{old_count:,} to {config.C_BOLD}{new_count:,}{config.C_RESET} "
                        f"({-added_files:,} fewer). Check the music directory/mount "
                        f"before trusting this list.",
                        category="INFO"
                    )
                else:
                    # 4. Confirm, through the VIP express lane
                    announce.send_debug(
                        f"List update successfully completed! MasterList now contains {config.C_BOLD}{new_count:,}{config.C_RESET} files. "
                        f"Added {added_files:,} new file(s) since last index.",
                        category="INFO"
                    )
                # The script itself succeeded either way - #230's shrink
                # warning above is a caution about the RESULT, not a failure
                # of the rebuild process #224 is about.
                config.last_list_update_ok = True
                config.last_list_update_error = None

            else:
                error_msg = subprocess_failure_message(process.stderr, process.stdout)
                announce.send_debug(f"External update_list.py failed (Exit Code {process.returncode}): {error_msg}", category="INFO")
                config.last_list_update_ok = False
                config.last_list_update_error = error_msg

        except subprocess.TimeoutExpired:
            announce.send_debug(
                f"List update FAILED: Script execution timed out after {list_update_timeout} seconds.",
                category="INFO")
            config.last_list_update_ok = False
            config.last_list_update_error = f"timed out after {list_update_timeout}s"
        except Exception as e:
            print(f"[UPDATE ERROR] The list update could not be run: {e}")
            announce.send_debug(f"List update FAILED critical error: {e}", category="INFO")
            config.last_list_update_ok = False
            config.last_list_update_error = str(e)
        finally:
            # Release the global pause lock again
            config.search_inprogress = False

            # Clear the maintenance flag, so list.py knows the list is ready
            config.update_inprogress = False
            print("[MAINTENANCE END] Sharing and searching have been restarted automatically.")

    # Raise the maintenance flag, so the whole daemon knows an update is starting
    config.update_inprogress = True

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
    if shared_count or list_date != "No List":
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
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n", is_vip=True)

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
        oserve.queue_message(user, f"NOTICE {user} :{line}\r\n", is_vip=True)

    print(f"[TOP] Sent the most-requested notice to {user} (asked in {target}).")
