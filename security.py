# security.py - Flood protection and ban enforcement
import threading
import time
import os
import sys
import defaults as config
import db

# Nicks we have already sent a debug notice about - one notice per nick.
# send_debug sleeps 0.5s while holding a lock and is called from here by the IRC
# reader thread, so one notice per BLOCKED MESSAGE would freeze the network loop
# and tear down the connection.
#
# This used to be a plain set(), which grew one PERMANENT entry per distinct
# denied nick and was never pruned. The two removal paths - a timed ban expiring
# and a nick later being seen clean - are both unreachable for a nick matched by
# a wildcard pattern in hard_bans.txt, because the "seen clean" branch only runs
# when the user is NOT banned. So an operator with any wildcard entry (the
# documented purpose of that file) plus somebody cycling nicks against it grows
# this set without limit, at roughly 95 bytes a nick, for the life of the
# process.
#
# WHY NOT A PLAIN LRU: capping by size and evicting the oldest is the obvious
# fix and it is the wrong one. An attacker cycling through more nicks than the
# cap would evict each nick before it came back, so every message would earn a
# fresh notice - and each notice costs 0.5s of read-thread stall. That converts
# a slow memory leak into the exact network freeze the notice-suppression exists
# to prevent.
#
# Expiring by TIME instead has no such edge: eviction is driven by the clock,
# not by pressure, so an attacker cannot force an entry out in order to be
# re-notified. A nick that returns inside the window is still suppressed however
# many other nicks have been seen meanwhile. The size cap below is only a
# backstop for a rate nobody has managed.
class _NotifiedNicks:
    """Nicks already notified about, forgotten again after a while.

    Deliberately exposes the same operations the previous set() did - `in`,
    add(), discard() and clear() - so the call sites did not have to change.

    Expiry is by TIME ONLY, with no size cap, and that is the whole design.
    An earlier version of this had a cap that evicted the oldest entry under
    pressure, which reintroduced the very problem described above: cycling more
    nicks than the cap pushes a recently-notified nick out, it gets a fresh
    notice, and every notice is 0.5s of read-thread stall. A test below pins
    that behaviour so the cap cannot come back.

    What this does and does not promise: memory is bounded by RATE x WINDOW
    rather than absolutely. At the rate Undernet's nick-change lag actually
    allows - roughly one nick every two seconds - an hour's window holds about
    1,800 entries, some 170KB, against a set that previously grew for the life
    of the process. Bounding it absolutely would mean rate-limiting the notices
    themselves rather than remembering nicks, which is a larger change than
    this finding warrants.
    """

    def __init__(self, ttl=3600.0, sweep_every=60.0):
        self._seen = {}                  # nick -> when we last notified about it
        self._ttl = float(ttl)
        self._sweep_every = float(sweep_every)
        self._last_sweep = time.time()
        self._lock = threading.Lock()

    def __contains__(self, nick):
        with self._lock:
            stamp = self._seen.get(nick)
            if stamp is None:
                return False
            if time.time() - stamp > self._ttl:
                del self._seen[nick]
                return False
            return True

    def add(self, nick):
        with self._lock:
            now = time.time()
            self._seen[nick] = now
            # Sweeping on every add would be O(n) per denied message on the IRC
            # reader thread. Once a minute is plenty for an hour-long window.
            if now - self._last_sweep >= self._sweep_every:
                self._sweep_locked(now)

    def discard(self, nick):
        with self._lock:
            self._seen.pop(nick, None)

    def clear(self):
        with self._lock:
            self._seen.clear()
            self._last_sweep = time.time()

    def _sweep_locked(self, now):
        self._last_sweep = now
        for nick in [n for n, stamp in self._seen.items() if now - stamp > self._ttl]:
            del self._seen[nick]

    def __len__(self):
        with self._lock:
            return len(self._seen)


_ban_notified = _NotifiedNicks()

# Set once check_user_status() has told the console that hard_bans.txt is
# missing, so that warning prints once per process rather than once per
# message - this check runs on every single command.
_hard_bans_missing_warned = False


def is_over_broad_hard_ban_pattern(pattern):
    """True when `pattern` reduces to nothing once wildcards and mask
    separators are stripped - a pattern that would match every user on the
    network, banning the whole channel rather than the one it was meant for.

    The check was originally "is anything left after removing '*'", correct
    where it was first written: adminchat.is_admin_host() matches a HOST
    pattern, which contains no "!" or "@", so "*" was the only way to spell
    "everything" there (see adminchat.py's own is_admin_host() docstring for
    #218, which found that reasoning missed the dot too). Here a pattern can
    be a full hostmask, and #168 made those actually match for the first
    time: "*!*@*" leaves the residue "!@" - truthy, so it sailed through and
    banned every user on the network. So did "*!*", "*@*" and "*!*@*.*".

    Lifted out of check_user_status()'s enforcement loop below so
    handle_hard_ban_request() (#225) can refuse an over-broad pattern at the
    point it is CONFIRMED rather than only at the point it is enforced - the
    two used to disagree silently: !ban would report success and this check
    would then decline to enforce it, logging only to stdout, which the admin
    who typed the command never sees.
    """
    residue = pattern
    for separator in "*!@.":
        residue = residue.replace(separator, "")
    return not residue


def check_user_status(user, hostmask=None):
    """Check the user against the timed bans in memory and against hard_bans.txt.

    `hostmask`, if given, is the sender's "ident@host" straight off the
    wire (irc.py's PRIVMSG regex now captures it) - not the nick, not the
    "!". A hard-ban pattern containing "!" or "@" is hostmask-shaped and is
    matched against "<nick>!<hostmask>" lowercased instead of the bare
    nick: a nick can never contain "!" or "@", so a pattern like
    "*!*@spammer.net" - the exact form _cmd_ban's own usage line has
    always told operators to type - was unmatchable by construction until
    this parameter existed. Without `hostmask` (the default), every
    pattern still falls back to nick-only matching, exactly as before.

    Returns False if the user should be ignored entirely, otherwise True.
    """
    import os
    import re
    import time
    import defaults as config
    import announce

    user_lower = user.lower()
    full_mask_lower = f"{user_lower}!{hostmask.lower()}" if hostmask else None

    def _deny(reason, category):
        """Always logs to the console, but sends ONE debug notice per nick."""
        print(f"[SECURITY BLOCK] Denied {user}: {reason}")
        if user_lower not in _ban_notified:
            _ban_notified.add(user_lower)
            try:
                announce.send_debug(
                    f"Access denied for {config.C_BOLD}{user}{config.C_RESET} ({reason}).",
                    category=category
                )
            except Exception as notify_err:
                print(f"[SECURITY ERROR] Could not send the ban notice: {notify_err}")
        return False

    # ---------------------------------------------------------------------
    # 1. TIMED BANS (the day-bans the flood protection issues)
    # These live in config.banned_users as {nick: expiry} and are read from
    # bans.txt at boot. They are TIMESTAMPED ROWS, not wildcard patterns, so they
    # must never be regex-matched the way the old code did.
    # ---------------------------------------------------------------------
    expire_ts = config.banned_users.get(user_lower)
    if expire_ts is not None:
        try:
            expire_ts = float(expire_ts)
        except (TypeError, ValueError):
            expire_ts = 0.0

        if time.time() < expire_ts:
            until = time.strftime("%H:%M:%S", time.localtime(expire_ts))
            return _deny(f"temporary ban active until {until}", "TBAN")

        # The ban has expired - clear it from memory and from disk.
        del config.banned_users[user_lower]
        _ban_notified.discard(user_lower)
        try:
            import db
            db.save_bans_to_file()
        except Exception as save_err:
            print(f"[SECURITY ERROR] Could not save the expired ban: {save_err}")

    # ---------------------------------------------------------------------
    # 2. PERMANENT WILDCARD BANS (hard_bans.txt, one pattern per line)
    # ---------------------------------------------------------------------
    hard_file = getattr(config, "HARD_BANS_FILE", "./data/hard_bans.txt")
    # Tracks whether the hard-ban list was actually READ. If the file is missing or the
    # read raised, this stays False and we must not treat "no match" as "definitely clean".
    hard_check_ok = False
    matched_pattern = None
    hard_file_existed = os.path.exists(hard_file)
    if hard_file_existed:
        try:
            # #162 finding #25: the match used to be reported (return _deny(...),
            # which prints and calls announce.send_debug()) from INSIDE this
            # `with` block, holding the file handle open across that work. On
            # Windows, db._atomic_write()'s os.replace() during an admin's
            # concurrent !ban/!unban raises PermissionError against any handle
            # still open on this same path - so the longer this one stayed open,
            # the likelier a write landed inside that window. During exactly
            # that window, hard_check_ok below would end up False (the read that
            # is happening right now would itself fail on its NEXT open, not
            # this one - see the loop below) and a hard-banned nick would be
            # admitted for that one message: this scan fails OPEN by design (see
            # hard_check_ok's own comment), so any read failure - not just a
            # missing file - takes that path. Closing the handle (leaving the
            # `with` block) before ever calling _deny() shrinks that window to
            # exactly the file read itself, nothing more.
            with open(hard_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    pattern = line.strip().lower()
                    if not pattern or pattern.startswith("#"):
                        continue

                    # BREADTH GUARD: a pattern made only of stars/separators
                    # would lock out the whole channel. Skip it and say so
                    # loudly in the log - see is_over_broad_hard_ban_pattern()'s
                    # own docstring for why the check is a full hostmask's
                    # worth of separators, not just "*".
                    if is_over_broad_hard_ban_pattern(pattern):
                        print(f"[SECURITY WARNING] Ignored an over-broad pattern in {hard_file}: {pattern!r}")
                        continue

                    regex_pattern = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
                    # A nick can never contain "!" or "@", so a pattern
                    # containing either is a hostmask form and must be
                    # matched against the full mask, not the bare nick -
                    # falls back to the nick if no hostmask was supplied
                    # (an older/other caller), same as before this existed.
                    is_hostmask_pattern = "!" in pattern or "@" in pattern
                    candidate = (full_mask_lower if is_hostmask_pattern and full_mask_lower
                                 else user_lower)
                    if re.match(regex_pattern, candidate):
                        matched_pattern = pattern
                        break
            hard_check_ok = True
        except Exception as e:
            print(f"[SECURITY ERROR] Could not read {hard_file}: {e}")

    if matched_pattern is not None:
        return _deny(f"matched banned pattern '{matched_pattern}'", "BAN")

    if not hard_file_existed:
        # Fails open (see the comment on hard_check_ok above) - and previously
        # did so silently. This path is relative, so it also degrades this way
        # if the daemon is ever started from the wrong working directory, not
        # only when the operator genuinely has no hard bans configured yet.
        global _hard_bans_missing_warned
        if not _hard_bans_missing_warned:
            _hard_bans_missing_warned = True
            print(f"[SECURITY WARNING] {hard_file} not found - "
                  f"permanent wildcard bans are not being enforced.")

    # This user was seen clean, so drop any stale "already notified" mark: a LATER ban on
    # the same nick then notifies once more. Previously the mark was only cleared on the
    # timed-ban expiry path, so after an !unban and a re-!ban the debug channel stayed
    # silent and the admin never saw the pattern take effect.
    #
    # Only clear when the hard-ban list was genuinely read. The scan fails open - a missing
    # file, or a read landing inside the truncate window of an !unban rewrite - and clearing
    # on that path would re-arm the notice, costing another 0.5s read-thread stall the next
    # time the same still-banned nick speaks.
    #
    # Note this does not otherwise prune the set: a nick is only removed if it speaks again
    # while unbanned.
    if hard_check_ok:
        _ban_notified.discard(user_lower)

    return True  # The user is clear to use the bot


# ---------------------------------------------------------------------
# Flood-tracking state is forgotten once it can no longer change a decision.
#
# #162 finding #30. Both dicts below are keyed by a nick anybody can choose,
# and both grew one PERMANENT entry per distinct nick:
#
#   user_requests  the timestamps INSIDE an entry are pruned to REQUEST_WINDOW
#                  on every call, but the key itself never is - so a nick that
#                  made one request months ago still owns an empty list.
#   muted_until    entries ARE deleted when the mute expires, but only on the
#                  next request FROM THAT SAME NICK. A flooder who is muted and
#                  does not come back leaves its entry for good - and that is
#                  the normal case, because the mute is what made them leave.
#
# commands.py's PRESERVE_RUNTIME carries both across every !rehash, correctly:
# dropping them would release live mutes and hand a flooder a clean slate. So
# a restart has been the only thing that ever cleared them.
#
# The expiry needs no new setting and deliberately does not get one. An entry
# stops being able to affect any decision at a moment the data already defines:
# a user_requests entry is dead REQUEST_WINDOW seconds after its newest
# timestamp, because that is the window is_flooding() measures against, and a
# muted_until entry is dead once its own expiry passes - the same test the code
# below already applies when it deletes one. This is not a new policy, only the
# existing one applied to the nicks that never return.
#
# No size cap, but for a different reason than _NotifiedNicks above. There a cap
# was actively harmful. Here it would just be redundant: with REQUEST_WINDOW at
# 5s and MUTE_TIME at 30s an entry is prunable within seconds of being made, so
# time alone already bounds this by rate.
_FLOOD_SWEEP_EVERY = 60.0
_last_flood_sweep = 0.0


def _prune_flood_tracking(now):
    """Drop flood-tracking entries that can no longer affect a decision.

    Both loops materialise their key list BEFORE deleting anything: mutating a
    dict while iterating it raises RuntimeError, and this runs on the IRC read
    thread, where that would take the connection down.

    Returns how many entries were dropped. The tests asserts on that number
    because a sweep that runs and removes nothing is indistinguishable from
    outside from one that never ran at all.
    """
    window = float(getattr(config, "REQUEST_WINDOW", 5) or 5)

    dropped = 0
    for nick in [nick for nick, stamps in config.user_requests.items()
                 if not stamps or now - max(stamps) >= window]:
        del config.user_requests[nick]
        dropped += 1

    for nick in [nick for nick, until in config.muted_until.items() if now >= until]:
        del config.muted_until[nick]
        dropped += 1

    return dropped


def _sweep_flood_tracking_if_due(now):
    """Throttle. Sweeping on every request would be O(nicks) per message on the
    read thread; once a minute is ample for windows measured in seconds."""
    global _last_flood_sweep
    if now - _last_flood_sweep < _FLOOD_SWEEP_EVERY:
        return 0
    _last_flood_sweep = now
    return _prune_flood_tracking(now)


def format_ban_duration(seconds):
    """A short human phrase for a ban length: "45 minutes", "1 hour".

    Every message about the escalation ban is built from this, so the console
    line, the debug notice and the NOTICE the banned user reads cannot drift
    apart from each other or from config.FLOOD_BAN_SECONDS. They all used to
    say "until midnight", which stopped being true the moment the duration
    became fixed (#227) - and the one a stranger sees is the one that matters.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second" + ("" if seconds == 1 else "s")
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    hours = seconds / 3600.0
    if hours == int(hours):
        whole = int(hours)
        return f"{whole} hour" + ("" if whole == 1 else "s")
    return f"{hours:.1f} hours"


def is_flooding(user):
    """Flood protection: clears the queue on a ban, applies the escalation ban, and logs it all."""
    import time
    import sys
    import defaults as config
    import db
    import announce
    
    now = time.time()
    user_key = user.lower()
    oserve = sys.modules.get('oserve')

    # Before the checks below, not after: a nick whose mute has already expired
    # should be treated the same whether it is swept here or deleted by the
    # expiry branch further down. Both paths leave it unmuted.
    _sweep_flood_tracking_if_due(now)
    
    # ---------------------------------------------------------------------
    # STEP 2: the user kept hammering while muted -> a fixed-length ban
    # ---------------------------------------------------------------------
    if user_key in config.muted_until:
        if now < config.muted_until[user_key]:
            # A FIXED duration, not midnight (#227). The old arithmetic made the
            # sentence depend on the time of day: 00:01 bought nearly 24 hours,
            # 23:59 bought seconds, for identical behaviour. Arbitrary is worse
            # than short.
            ban_seconds = int(getattr(config, "FLOOD_BAN_SECONDS", 3600))
            config.banned_users[user_key] = now + ban_seconds
            db.save_bans_to_file()
            
            if user_key in config.muted_until:
                del config.muted_until[user_key]
            
            if user_key in config.send_queue:
                del config.send_queue[user_key]
            
            spell = format_ban_duration(ban_seconds)
            print(f"[SECURITY BAN] Banned {user} for {spell}. Saved to {config.BANS_FILE} via db.py.")
            
            # VIP log: send the day-ban notice straight out, with no queue delay
            announce.send_debug(
                f"User {user} ignored warnings and flooded during mute. Upgraded to a {spell} ban! Saved to disk layout.", 
                category="TBAN"
            )
            
            if oserve:
                oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}[WARNING]{config.C_RESET} You flooded the server, you are now banned for {spell}\r\n")
            return True
        else:
            del config.muted_until[user_key]
            
    # ---------------------------------------------------------------------
    # Drop the requests that have fallen outside the rolling window
    # ---------------------------------------------------------------------
    if user_key not in config.user_requests:
        config.user_requests[user_key] = []
        
    config.user_requests[user_key] = [ts for f, ts in enumerate(config.user_requests[user_key]) if now - ts < config.REQUEST_WINDOW]
    config.user_requests[user_key].append(now)
    
    # ---------------------------------------------------------------------
    # STEP 1: the user is going too fast -> a temporary mute and a warning
    # ---------------------------------------------------------------------
    if len(config.user_requests[user_key]) > config.MAX_REQUESTS:
        config.muted_until[user_key] = now + config.MUTE_TIME
        
        if user_key in config.send_queue:
            del config.send_queue[user_key]
            
        print(f"[FLOOD CONTROL] Temporarily muted {user} for {config.MUTE_TIME} seconds. Queue cleared.")
        
        # VIP log: send the warning notice straight out, with no queue delay
        announce.send_debug(
            f"User {user} moving too fast! Triggered temporary mute for {config.MUTE_TIME} seconds. Queue cleared.", 
            category="MUTE"
        )
        
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}[WARNING]{config.C_RESET} You are moving too fast! Ignored and queue cleared for {config.MUTE_TIME} seconds.\r\n")
        return True
        
    return False
