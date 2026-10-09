"""Automatic list grabbing (#926 item 5): ask for the list of a bot that
advertises one and whose list we do not hold yet, on AutoGet's rules.

OFF by default (AUTO_GRAB_LISTS). Every grab spends another bot's bandwidth
and one of its transfer slots, and a channel full of clients all asking every
new bot at once is exactly the burst file servers ban for. So the rules are
the ones AutoGet learned the hard way:

- at most one automatic grab every AUTO_GRAB_EVERY_MINUTES;
- each one waits a random 5-360 seconds first, so clients that saw the same
  advert do not all ask in the same second;
- if someone else in the channel asks that bot for its list during the wait,
  ours is dropped - the bot is busy with theirs, and asking twice at once is
  what gets a client ignored;
- 3 tries per bot, 30 minutes apart, then it stops: AutoGet's own counter
  says only about a third of list requests ever arrive, and a bot that has
  not answered three times is not going to. A try is a request that went
  out - one the queue refused was not an ask (#967) - and a list that
  arrives starts the count over;
- bots below AUTO_GRAB_MIN_FILES files or AUTO_GRAB_MIN_SPEED_KB, and bots in
  "servers only" mode, are skipped - a servers-only bot would refuse us.

A list the operator removed by hand is not grabbed again: removing it was the
answer. Fetching it by hand is how to change that answer: once it arrives it
is an ordinary held list again (note_list_arrived()).

The grab itself goes through webserver.build_list_fetch_enqueue_result(), the
same path the List Browser's own button takes, so every slot limit and
duplicate guard applies to it exactly as to a fetch the operator started.

State lives in runtime.py (the plan, the last grab, the start guard) so a
rehash that reloads this module does not forget a wait or start a second
worker; the per-bot tries and the removed-by-hand set are on disk, so a
restart does not start the three tries over.
"""

import random
import re
import threading
import time

import db
import defaults as config
import runtime

# Seconds to wait before asking, picked at random per grab (AutoGet's 5-360).
GRAB_DELAY_SECONDS = (5.0, 360.0)
# Tries per bot, and the least time between two of them.
GRAB_TRIES = 3
GRAB_COOLDOWN_SECONDS = 30 * 60
# A bot someone else asked for its list this recently is left alone: it is
# busy sending theirs.
OTHERS_ASKED_SECONDS = 10 * 60
# How often the worker looks. Short: it only reads dicts, and the random wait
# is counted from when it noticed.
TICK_SECONDS = 15.0

_SPEED_RE = re.compile(r"^\s*([\d,.]+)\s*(cps|B/?s|KB/?s|MB/?s|GB/?s)?\s*$", re.IGNORECASE)
_SPEED_UNITS = {"cps": 1, "b/s": 1, "bs": 1, "kb/s": 1024, "kbs": 1024,
                "mb/s": 1024 ** 2, "mbs": 1024 ** 2, "gb/s": 1024 ** 3, "gbs": 1024 ** 3}


def advertised_speed(text):
    """Bytes per second from an advert's speed ("45000cps", "120KB/s"), or
    None when it cannot be read. A bare number is cps, as OmenServe writes it."""
    match = _SPEED_RE.match(str(text or ""))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "cps").lower()
    return number * _SPEED_UNITS.get(unit, 1)


def _setting(name, default):
    try:
        return int(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def _state():
    """The per-bot record, loaded from disk the first time it is needed."""
    if runtime.list_grab_state is None:
        loaded = db.load_list_grabs()
        runtime.list_grab_state = {
            "tries": {key: dict(value) for key, value in (loaded.get("tries") or {}).items()
                      if isinstance(value, dict)},
            "removed": set(str(key).lower() for key in (loaded.get("removed") or [])),
        }
    return runtime.list_grab_state


def _save():
    state = _state()
    db.save_list_grabs({"tries": state["tries"], "removed": sorted(state["removed"])})


def note_removed_by_hand(bot):
    """The operator removed `bot`'s list (list_fetch.purge_fetched_list()):
    do not grab it back."""
    key = str(bot or "").strip().lower()
    if not key:
        return
    with runtime.list_grab_lock:
        _state()["removed"].add(key)
        _save()


def note_list_arrived(bot):
    """`bot`'s list arrived (list_fetch.process_fetched_list_zip()), however it
    was asked for (#967). Its tries start over, and a removal by hand is
    undone - a removed list comes back only when the operator fetches it,
    since neither sweep asks for one, and that fetch is how the module
    docstring says the answer is changed.

    Without this the tries counted for a bot's whole life: a list grabbed,
    later cleared by the purge of offline bots (tidying, not an answer about
    the bot), grabbed again and cleared again was "gave up" for ever after
    the third time, though every one of those tries had been answered."""
    key = str(bot or "").strip().lower()
    if not key:
        return
    with runtime.list_grab_lock:
        state = _state()
        forgotten = state["tries"].pop(key, None) is not None
        if key in state["removed"]:
            state["removed"].discard(key)
            forgotten = True
        if forgotten:
            _save()


def note_secondary_channel_list_arrived(bot, channel):
    """`channel`'s list for `bot` arrived (#1240), however it was asked for.
    Its tries start over, same reasoning as note_list_arrived() above: three
    tries is a bar against a channel that never answers, not a ceiling on
    how many times a list that keeps answering may be refreshed."""
    key = str(bot or "").strip().lower()
    channel = str(channel or "").strip().lower()
    if not key or not channel:
        return
    with runtime.secondary_channel_lock:
        state = _secondary_channel_state()
        if state.pop(f"{key}:{channel}", None) is not None:
            _save_secondary_channel_state()


def note_someone_else_asked(user, msg, now=None):
    """Channel text "@Bot" from another user: they asked `Bot` for its list.
    Called from irc.py on every channel line, so cheap and quiet. Only a known
    bot is remembered, which keeps this bounded by the registry."""
    text = str(msg or "").strip()
    if not text.startswith("@") or " " in text:
        return
    key = text[1:].lower()
    if key and key in runtime.known_bots and key != str(user or "").lower():
        runtime.list_grab_others_asked[key] = time.time() if now is None else now


def _why_not(key, entry, present, now):
    """Why `key` may not be grabbed now, or None when it may."""
    if key in (getattr(config, "fetched_bot_lists", {}) or {}):
        return "held"
    if key == str(getattr(config, "NICKNAME", "")).lower():
        return "us"
    if key not in present:
        return "offline"
    files = entry.get("files")
    if not isinstance(files, int) or isinstance(files, bool):
        return "no list advertised"
    if files < _setting("AUTO_GRAB_MIN_FILES", 0):
        return "too few files"
    if "only" in str(entry.get("mode") or "").lower():
        return "servers only"
    min_speed = _setting("AUTO_GRAB_MIN_SPEED_KB", 0)
    speed = advertised_speed(entry.get("speed"))
    if min_speed > 0 and speed is not None and speed < min_speed * 1024:
        return "too slow"
    if now - float(runtime.list_grab_others_asked.get(key) or 0) < OTHERS_ASKED_SECONDS:
        return "someone else asked"
    state = _state()
    if key in state["removed"]:
        return "removed by hand"
    record = state["tries"].get(key) or {}
    if int(record.get("tries") or 0) >= GRAB_TRIES:
        return "gave up"
    if now - float(record.get("last") or 0) < GRAB_COOLDOWN_SECONDS:
        return "cooling down"
    return None


def _candidates(now):
    import webserver

    present = webserver.present_nicks()
    found = []
    for key, entry in list(runtime.known_bots.items()):
        if isinstance(entry, dict) and _why_not(key, entry, present, now) is None:
            found.append((-int(entry.get("files") or 0), key, entry.get("nick") or key))
    # The biggest list first: of the bots that qualify, that is the one most
    # worth one of the few grabs an hour this allows.
    found.sort()
    return [(key, nick) for _files, key, nick in found]


def _log_both(bot, text, log):
    log(f"[LIST-GRAB] {text}")
    import list_fetch
    list_fetch._tell_the_console(bot, "auto", text)


def tick(now=None, log=print, pick_delay=None):
    """One look. Plans a grab, drops one someone else beat us to, or makes one
    whose wait is over. Returns what it did, for tests and the log."""
    import webserver

    now = time.time() if now is None else now
    with runtime.list_grab_lock:
        # Keyed by known bots, but a bot the registry has since forgotten
        # would stay here for ever; nothing older than the window matters.
        for key, when in list(runtime.list_grab_others_asked.items()):
            if now - float(when or 0) >= OTHERS_ASKED_SECONDS:
                runtime.list_grab_others_asked.pop(key, None)
        if not getattr(config, "AUTO_GRAB_LISTS", False) or not getattr(config, "bot_joined_channel", False):
            runtime.list_grab_plan = None
            return "off"

        plan = runtime.list_grab_plan
        if plan is None:
            every = max(0, _setting("AUTO_GRAB_EVERY_MINUTES", 10)) * 60
            if now - float(runtime.list_grab_last or 0) < every:
                return "waiting"
            found = _candidates(now)
            if not found:
                return "nothing"
            key, nick = found[0]
            delay = (pick_delay or (lambda: random.uniform(*GRAB_DELAY_SECONDS)))()
            runtime.list_grab_plan = {"key": key, "nick": nick, "planned": now, "at": now + delay}
            return "planned"

        key, nick = plan["key"], plan["nick"]
        if float(runtime.list_grab_others_asked.get(key) or 0) >= plan["planned"]:
            # Not a try - we never asked. _why_not() leaves the bot alone for
            # OTHERS_ASKED_SECONDS from their ask.
            runtime.list_grab_plan = None
            _log_both(nick, f"Someone else asked {nick} for its list - not asking as well.", log)
            return "someone else asked"
        if now < plan["at"]:
            return "waiting"

        runtime.list_grab_plan = None
        present = webserver.present_nicks()
        entry = runtime.known_bots.get(key)
        reason = _why_not(key, entry, present, now) if isinstance(entry, dict) else "gone"
        if reason is not None:
            return f"skipped: {reason}"

        # Spaced from this attempt whatever comes of it: the next grab waits
        # AUTO_GRAB_EVERY_MINUTES and this bot GRAB_COOLDOWN_SECONDS, so a
        # refusal is not tried again on the next tick.
        _state()["tries"].setdefault(key, {"tries": 0})["last"] = now
        runtime.list_grab_last = now
        _save()

    # Off the lock: the enqueue takes the fetch queue's own.
    status, result = webserver.build_list_fetch_enqueue_result(nick)
    if status != 200:
        log(f"[LIST-GRAB] Did not ask {nick}: {result.get('error', 'refused')}")
        return "refused"

    # A TRY IS AN ASK (#967). Counted before the enqueue, a refusal was a
    # try: with FETCHED_FILES_DIR missing at boot (503), or an operator's
    # own fetch from that bot outstanding (409, and a queued one can last
    # hours), three refusals wrote every candidate to disk as "gave up",
    # and it stayed so after the cause was gone - though nothing was asked.
    with runtime.list_grab_lock:
        record = _state()["tries"].setdefault(key, {"tries": 0, "last": now})
        record["tries"] = int(record.get("tries") or 0) + 1
        tries = record["tries"]
        _save()
    _log_both(nick, f"Asking {nick} for its list automatically (try {tries} of {GRAB_TRIES}).", log)
    return "asked"


# ==========================================================================
# Automatic discovery of a bot's OTHER channel-bound lists (#1240).
#
# A separate, much rarer pass from the grab above: that one finds a bot we
# hold nothing from yet, on AutoGet's own rules; this one finds a SECOND,
# genuinely different list for a bot we already hold one from, bound to
# another of our channels (DCCore's own multi-list-per-channel feature, which
# another DCCore-family bot can equally run). OFF by default
# (AUTO_DISCOVER_CHANNEL_LISTS) - see its own comment in defaults.py for why.
# ==========================================================================

SECONDARY_CHANNEL_TICK_SECONDS = 300.0
SECONDARY_CHANNEL_TRIES = 3
SECONDARY_CHANNEL_COOLDOWN_SECONDS = 30 * 60


def _secondary_channel_state():
    """The per-(bot, channel) tries record, loaded from disk the first time
    it is needed - same shape and reason as _state() above, in its own file
    so a problem in one never costs the other."""
    if runtime.secondary_channel_tries is None:
        runtime.secondary_channel_tries = {
            key: dict(value) for key, value in db.load_secondary_channel_grabs().items()
            if isinstance(value, dict)
        }
    return runtime.secondary_channel_tries


def _save_secondary_channel_state():
    db.save_secondary_channel_grabs(_secondary_channel_state())


def _signature_has_content(signature):
    """True if `signature` actually carries something comparable - an int
    files count, or a list_date. A marker merged in before #1240 tracked
    this (or one whose channel's advert we have simply never parsed a count
    out of) has an empty {} here, which must never be treated as "known to
    be the same content" - that read exactly the opposite of what an absent
    signature means, and silently hid every OTHER candidate behind it."""
    if not isinstance(signature, dict):
        return False
    return isinstance(signature.get("files"), int) or bool(signature.get("list_date"))


def _signatures_differ(a, b):
    """True if advert signatures `a` and `b` (known_bots[...]["channels"]
    entries) plausibly describe two different lists, never a guess from
    only one of them - see _secondary_channel_candidates()'s own docstring
    for why both have to clear a stability bar first."""
    files_a, files_b = a.get("files"), b.get("files")
    if isinstance(files_a, int) and isinstance(files_b, int):
        return files_a != files_b
    date_a, date_b = a.get("list_date"), b.get("list_date")
    return bool(date_a) and bool(date_b) and date_a != date_b


def _held_marker_channels(bot_key):
    """{channel, lowercased: its marker's own "advert_signature" - what it
    was advertising when fetched} for every channel already held as a
    marker for this bot, whatever confirmed it (this detector, or an
    operator's own fetch). Read by _secondary_channel_candidates() to tell
    a channel it already holds apart from one it has never seen: the first
    is only worth asking again once its OWN signature has moved on; the
    second is worth asking at all.
    """
    entry = (getattr(config, "fetched_bot_lists", {}) or {}).get(bot_key)
    lists = (entry or {}).get("lists") if isinstance(entry, dict) else None
    if not isinstance(lists, dict):
        return {}
    return {str(info.get("channel")).strip().lower(): (info.get("advert_signature") or {})
            for info in lists.values()
            if isinstance(info, dict) and info.get("channel")}


def _secondary_channel_candidates(now=None):
    """[(bot_key, nick, channel), ...] worth fetching: a bot we already hold
    a list from, advertising in another of our channels with a file count or
    list date that genuinely differs from its own held channel's - and has
    held stable, on BOTH sides of the comparison, for at least
    MULTI_CHANNEL_LIST_STABLE_SECONDS. Never a bot's first list at all (that
    is list_grab's own job above).

    A channel ALREADY held as one of this bot's markers is offered again
    too, once ITS OWN advertised signature has moved on since it was last
    fetched - the ongoing upkeep a once-discovered secondary list needs to
    stay current, through the exact same confirm-then-fetch path as
    discovering it the first time; nothing else refreshes a secondary
    marker on its own schedule the way AUTO_REFETCH_LISTS already does for
    a bot's primary one.

    The bot must be in the candidate channel RIGHT NOW (#1240 review):
    channels[...] is never pruned, so a channel it left keeps its last
    advertised signature on record indefinitely - without this, that stale
    signature reads as a candidate exactly like a live one, and the confirmed
    fetch that follows gets no answer and spends one of the three tries on a
    channel nobody can actually ask.
    """
    import dcc_fetch

    now = time.time() if now is None else now
    stable_for = max(0, _setting("MULTI_CHANNEL_LIST_STABLE_SECONDS", 3600))
    held = getattr(config, "fetched_bot_lists", {}) or {}
    bots = getattr(config, "known_bots", {}) or {}
    found = []
    # Copied, not iterated live (#1240 review): the IRC reader thread mutates
    # both of these (a fetch arriving, _record_channel_signature() on every
    # advert) with no lock shared with this discovery pass. "dictionary
    # changed size during iteration" is a real, if intermittent, crash risk
    # otherwise - a plain copy is enough, nothing below mutates either dict.
    for bot_key, entry in list(held.items()):
        if not isinstance(entry, dict):
            continue
        registry = bots.get(bot_key)
        channels = registry.get("channels") if isinstance(registry, dict) else None
        if not isinstance(channels, dict) or not channels:
            continue
        channels = dict(channels)
        primary_channel = str(entry.get("channel") or "").strip().lower()
        primary_signature = channels.get(primary_channel)
        if primary_signature is not None:
            since = primary_signature.get("since")
            if not isinstance(since, (int, float)) or now - since < stable_for:
                # The channel we already hold is itself mid-change - comparing
                # anything against it right now would be comparing against a
                # moving target.
                continue
        already_held = _held_marker_channels(bot_key)
        # Every signature already known for this bot - the primary's, and
        # every secondary channel discovered so far. A candidate is only
        # worth a NEW fetch if it differs from ALL of them: comparing
        # against the primary alone let two channels that only differ from
        # it, but not from EACH OTHER, each get fetched as if they were
        # separate lists - a real incident, hit live: a bot with no known
        # primary signature (an entry held from before #1232) advertised
        # the same huge count in two other channels, and both were fetched
        # and stored as two markers with identical content, because neither
        # had anything but the (unknown) primary to be compared against.
        known_signatures = [sig for sig in already_held.values() if _signature_has_content(sig)]
        if primary_signature is not None:
            known_signatures.append(primary_signature)
        nick = registry.get("nick") or entry.get("bot") or bot_key
        for channel, signature in channels.items():
            if channel == primary_channel:
                continue
            if not isinstance(signature, dict):
                continue
            since = signature.get("since")
            if not isinstance(since, (int, float)) or now - since < stable_for:
                continue
            # The bot must be THERE RIGHT NOW (#1240 review), not just once
            # have been - channels[...] is never pruned, so a channel the bot
            # left long ago keeps its last advertised signature forever. Left
            # unchecked, that stale signature reads as a candidate exactly
            # like a live one: a confirmed fetch would go out, get no answer
            # (the bot is not there to send it), and spend one of the three
            # tries for nothing - repeated up to three times before the pair
            # is finally left alone.
            if not dcc_fetch.bot_in_our_channel(nick, channel):
                continue
            if channel in already_held:
                # Discovered already - only worth asking again if its own
                # advert has moved on since the marker now held was fetched.
                if not _signatures_differ(signature, already_held[channel]):
                    continue
            elif known_signatures:
                if any(not _signatures_differ(signature, known) for known in known_signatures):
                    continue
            else:
                # NOTHING TO COMPARE AGAINST AT ALL - the bot's primary
                # marker has no recorded channel (held from before #1232)
                # and nothing has been discovered for it yet either. This
                # used to let a candidate through anyway as long as its own
                # count looked concrete; real incidents (twice, on two
                # different bots) showed why not: a bot that genuinely
                # serves the SAME list in several channels has nothing here
                # to say so, so a channel showing the exact content the
                # primary already holds - just under a different name -
                # looked exactly as "new" as one that is genuinely
                # different, and was fetched and stored as a duplicate.
                # Skipping until the primary's own channel is on record
                # (its own ordinary AUTO_REFETCH_LISTS cycle backfills this
                # automatically, same as a manual refresh does) costs one
                # refresh interval of delay and nothing else - no real
                # secondary list goes undiscovered for longer than that.
                continue
            record = _secondary_channel_state().get(f"{bot_key}:{channel}") or {}
            if int(record.get("tries") or 0) >= SECONDARY_CHANNEL_TRIES:
                continue
            if now - float(record.get("last") or 0) < SECONDARY_CHANNEL_COOLDOWN_SECONDS:
                continue
            found.append((bot_key, nick, channel))
    return found


def secondary_channel_tick(now=None, log=print):
    """One look: fetch at most one confirmed secondary channel. Returns what
    it did, for tests and the log."""
    import webserver

    if not getattr(config, "AUTO_DISCOVER_CHANNEL_LISTS", False):
        return "off"
    now = time.time() if now is None else now
    with runtime.secondary_channel_lock:
        every = SECONDARY_CHANNEL_TICK_SECONDS
        if now - float(runtime.secondary_channel_last or 0) < every:
            return "waiting"
        found = _secondary_channel_candidates(now)
        if not found:
            runtime.secondary_channel_last = now
            return "nothing"
        bot_key, nick, channel = found[0]
        runtime.secondary_channel_last = now
        state = _secondary_channel_state()
        record = state.setdefault(f"{bot_key}:{channel}", {"tries": 0})
        record["last"] = now
        _save_secondary_channel_state()

    status, result = webserver.build_list_fetch_enqueue_result(nick, channel, secondary_raw=True)
    if status != 200:
        # NOT a try (#1240 review): nothing was actually asked of the bot -
        # a 409 (busy with another list/folder already) or the bot having
        # just left is a local refusal, not an unanswered attempt. Counting
        # it anyway meant a candidate could exhaust all three tries without
        # the bot ever having been asked once, and then be left alone for
        # good over something that had nothing to do with it. `record["last"]`
        # above still paces the retry via SECONDARY_CHANNEL_COOLDOWN_SECONDS.
        log(f"[LIST-GRAB] Did not ask {nick} for {channel}'s list: "
            f"{result.get('error', 'refused')}")
        return "refused"

    with runtime.secondary_channel_lock:
        record = _secondary_channel_state().setdefault(f"{bot_key}:{channel}", {"tries": 0})
        record["tries"] = int(record.get("tries") or 0) + 1
        tries = record["tries"]
        _save_secondary_channel_state()
    log(f"[LIST-GRAB] {nick} advertises a different list in {channel} - "
        f"asking for it (try {tries} of {SECONDARY_CHANNEL_TRIES}).")
    return "asked"


def ensure_secondary_channel_worker(start=None):
    """Start secondary_channel_worker() if AUTO_DISCOVER_CHANNEL_LISTS is on
    and it is not running. True only when this call started it. Same shape
    as ensure_worker() above, its own guard so the two never interfere."""
    if not getattr(config, "AUTO_DISCOVER_CHANNEL_LISTS", False):
        return False
    with runtime.secondary_channel_guard:
        if runtime.secondary_channel_started:
            return False
        starter = start or (lambda: threading.Thread(
            target=secondary_channel_worker, daemon=True).start())
        starter()
        runtime.secondary_channel_started = True
    return True


def secondary_channel_worker(sleep=None):
    """The loop. Turning the setting off needs no stop: the tick does
    nothing while it is off."""
    naptime = sleep or time.sleep
    print("[LIST-GRAB] Automatic discovery of channel-bound lists is on.")
    while True:
        try:
            secondary_channel_tick()
        except Exception as err:
            print(f"[LIST-GRAB] Automatic secondary-channel discovery error: {err}")
        naptime(60.0)


def ensure_worker(start=None):
    """Start the loop below if AUTO_GRAB_LISTS is on and it is not running.
    True only when this call started it. Called from oserve.startup() and the
    rehash, like list_fetch.ensure_auto_refetch_worker()."""
    if not getattr(config, "AUTO_GRAB_LISTS", False):
        return False
    with runtime.list_grab_guard:
        if runtime.list_grab_started:
            return False
        starter = start or (lambda: threading.Thread(target=worker, daemon=True).start())
        starter()
        runtime.list_grab_started = True
    return True


def worker(sleep=None):
    """The loop. Turning the setting off needs no stop: tick() does nothing
    while it is off."""
    naptime = sleep or time.sleep
    print("[LIST-GRAB] Automatic list grabbing is on.")
    while True:
        try:
            tick()
        except Exception as err:
            print(f"[LIST-GRAB] Automatic grab error: {err}")
        naptime(TICK_SECONDS)
