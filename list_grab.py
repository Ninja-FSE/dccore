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
  not answered three times is not going to;
- bots below AUTO_GRAB_MIN_FILES files or AUTO_GRAB_MIN_SPEED_KB, and bots in
  "servers only" mode, are skipped - a servers-only bot would refuse us.

A list the operator removed by hand is not grabbed again: removing it was the
answer. Fetching it by hand is how to change that answer.

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

        record = _state()["tries"].setdefault(key, {"tries": 0})
        record["tries"] = int(record.get("tries") or 0) + 1
        record["last"] = now
        runtime.list_grab_last = now
        _save()
        tries = record["tries"]

    # Off the lock: the enqueue takes the fetch queue's own.
    status, result = webserver.build_list_fetch_enqueue_result(nick)
    if status == 200:
        _log_both(nick, f"Asking {nick} for its list automatically (try {tries} of {GRAB_TRIES}).", log)
        return "asked"
    log(f"[LIST-GRAB] Did not ask {nick}: {result.get('error', 'refused')}")
    return "refused"


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
