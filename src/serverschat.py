"""DCCore Chat, relayed by the bot (#371).

Public chat between DCCore bots' operators, over an ordinary channel message
whose first word is the tag `[ServersChat]`. The bot is the relay: it reads a
tagged message from ANOTHER DCCore bot in the channels it is in and hands it
to the admin session (dccore.mrc's DCCore Chat window), and it says what the
operator types there as a tagged message of its own. The operator's mIRC
does not have to be in any channel.

WHO IS A DCCore BOT: its realname says so. registration_names() (irc.py)
puts REALNAME_MARK first in the USER line's realname, and the bot asks WHO
for each of its channels now and then (refresh_peers(), on the server's own
PINGs) and remembers the nicks whose realname starts with the mark
(note_who_reply()). Nothing is sent to those nicks: WHO is answered by the
server. The realname can be written by anybody, so this is a filter, not
proof - fine for a chat that is public anyway.

A JOIN carries no realname (no extended-join capability is negotiated), so a
stranger arriving is asked about individually and at once (note_join()) - a
single-nick WHO, not a full round - rather than wait up to WHO_EVERY to find
out a DCCore bot reconnected. A nick already known is left alone there: only
strangers are worth an extra WHO the moment they join.

WHERE IT IS SAID: `chat #chan text` says it in that channel; `chat * text`
says it once in as few channels as reach every peer seen (cover()), never in
a channel with no other DCCore bot in it; `chat nick text` says it privately
to that one peer instead - only ever a nick WHO has already found, since
that is the only proof this bot has that the other end can see it.

THE WIRE IS `[ServersChat] <text>`, nothing else. The sender is the nick that
sent the message, which the server vouches for. No name goes inside the
text: a name there is a claim anybody can write, and the format cannot
change once people use it. It is a plain PRIVMSG on purpose: a channel
NOTICE is what eggdrops and channel bots kick for.

WHAT ARRIVES is other people's text, on the IRC read loop, so it is taken
the way every capture there is (irc._capture_chat_message() wraps it in
never_breaks_the_read_loop):

- the tag is the FIRST word, and the sender is a known peer - the bots'
  own adverts and search replies carry the realname too, and never the tag;
- only channels the bot is in, never our own nick;
- control codes stripped, the text capped;
- a per-nick limit, so one sender cannot fill the operator's window;
- memory bounded, and IN MEMORY ONLY: the recent lines are a record of what
  other people said, and none of it is ever written to disk;
- it never reaches dispatch, and it never answers: a received line is never
  sent on, so two bots cannot echo each other.

WHAT IS SENT is what an authenticated operator typed, through the same paced
outbound queue as everything else the bot says - on its express lane, so it
does not wait behind a line for each of the bot's other channels - with a
cap of its own so a chatty moment cannot delay the queue's own messages.

A private line's `chan` (in the recorded line, and on the feed) is
"@<nick>" - never a real channel, so it never collides with one, and it is
how the window knows to show it regardless of what is ticked to listen on
and to feed dccore.mrc's side-listbox of known peers (`DCCORE PEERS`).

State is runtime.py's - see the chat_* names there - so a rehash that
reloads this module keeps the recent lines, the limits and the peers.
"""

import re
import sys
import time

import defaults as config
import runtime

TAG = "[ServersChat]"

# The first word of the realname of every DCCore bot (irc.registration_names()).
REALNAME_MARK = "DCCore/sc"

# How often the channels are asked WHO, and how long a peer sighting counts.
WHO_EVERY = 600
# A stranger's JOIN is asked about at once (note_join), but paced: one nick at
# most once a WHO_EVERY, and at most JOIN_WHO_MOST such questions in
# JOIN_WHO_PER seconds - past that the regular round finds them. They go on a
# send-queue key of their own: no nick contains '*', so they never queue
# ahead of a line for a channel, the dashboard's @find among them.
JOIN_WHO_MOST = 5
JOIN_WHO_PER = 60.0
JOIN_WHO_QUEUE = "*chat-who*"
_JOIN_WHO_ALL = "*join-who*"
PEER_FRESH = WHO_EVERY * 2.5
# Tracked peers, and the channels one `chat *` says it in.
PEER_MAX = 200
SEND_CHANNELS_MAX = 5

# What one arriving line may carry, in characters, after stripping.
MAX_TEXT = 400
# What one sent line may carry, in BYTES: "NOTICE #channel :[ServersChat] "
# plus this must stay inside IRC's 512-byte line.
MAX_SEND_BYTES = 350

# Arriving: more than INBOUND_MAX lines in INBOUND_PER seconds from one nick
# hides that nick for INBOUND_HIDE seconds.
INBOUND_MAX = 5
INBOUND_PER = 10
INBOUND_HIDE = 60
# And from everyone together (#958 review): a hundred nicks each inside
# their own limit are still a flood. Past INBOUND_ALL_MAX lines in
# INBOUND_PER seconds the rest are dropped, said once per window.
INBOUND_ALL_MAX = 30
# The key the all-senders count is kept under - never a nick, since a nick
# cannot contain "*".
_ALL = "*"

# Sent: at most OUTBOUND_MAX lines per OUTBOUND_PER seconds per session.
OUTBOUND_MAX = 6
OUTBOUND_PER = 60

# The recent lines a window that (re)connects is shown.
RECENT_MAX = 50

# Past this many tracked nicks the stale ones are dropped.
_TRACK_MAX = 200

_FORMATTING = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1e\x1f]")
# The characters that change the DIRECTION text is drawn in (#958 review):
# U+202E alone reverses the rest of a line in the operator's window, so a
# line could show something other than what was sent. None of them is ever
# needed in a chat line.
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_CHANNEL_PREFIXES = ("#", "&", "+", "!")


def _plain(text):
    """Colour and control codes out, every other control character a space."""
    text = _BIDI.sub("", _FORMATTING.sub("", str(text or "")))
    return "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text).strip()


def chat_text(message):
    """The text of a chat message, or None when it is not one. The tag has to
    be the first word - anywhere else, it is somebody's sentence."""
    words = str(message or "").split(None, 1)
    if not words or words[0].lower() != TAG.lower():
        return None
    return words[1] if len(words) > 1 else ""


def channels():
    """The channels the bot is in, which are the ones it relays."""
    with runtime.channel_users_lock():
        names = list((getattr(config, "channel_users", {}) or {}).keys())
    return sorted(str(name).lower() for name in names
                  if str(name)[:1] in _CHANNEL_PREFIXES)


def _next_id(now):
    """A line's id: its time in milliseconds, strictly increasing. The window
    remembers the last one it drew, so a replay after a reconnect is not
    drawn twice - and ids that are times keep increasing across a restart."""
    with runtime.chat_lock:
        candidate = int(now * 1000)
        if candidate <= runtime.chat_last_id:
            candidate = runtime.chat_last_id + 1
        runtime.chat_last_id = candidate
    return candidate


def _record(chan, nick, text, now):
    line = {"id": _next_id(now), "chan": chan, "nick": nick, "text": text}
    with runtime.chat_lock:
        runtime.chat_recent.append(line)
        del runtime.chat_recent[:-RECENT_MAX]
    return line


def _chat_channel_token(value):
    """Like adminchat._channel_token(), but a chat line's channel field also
    has two tokens that helper does not know: "@<nick>" (a private line) and
    "-" (one line said in several channels at once)."""
    import adminchat
    text = str(value or "")
    if text == "-" or text[:1] == "@":
        return adminchat._clean(text, token=True)
    return adminchat._channel_token(text)


def structured_line(line):
    """`DCCORE CHAT <id> <chan> <nick> <text>` - the text last, as every
    feed line's free text is."""
    import adminchat
    return (f"DCCORE CHAT {int(line['id'])} {_chat_channel_token(line['chan'])} "
            f"{adminchat._clean(line['nick'], token=True)} {adminchat._clean(line['text'])}")


def channels_line():
    return ("DCCORE CHANNELS " + " ".join(channels())).rstrip()


def recent_lines():
    with runtime.chat_lock:
        recent = list(runtime.chat_recent)
    return [structured_line(line) for line in recent]


def _deliver(line):
    """To the operator's console session, if there is one. A pure append to
    its outbox - never the socket - since this runs on the IRC read loop."""
    try:
        import adminchat
        session = adminchat.active_session()
    except Exception:
        return
    if session is None or not getattr(session, "authenticated", False):
        return
    if getattr(session, "structured", False):
        session.send(structured_line(line))
    else:
        session.send(f"[CHAT] {line['chan']} <{line['nick']}> {line['text']}")


def _limited(table, key, most, per, now, count=1):
    """Count `count` events for `key` in `table` ({key: [window_start, n]});
    True when that takes it past `most` in `per` seconds."""
    record = table.get(key)
    if record is None or now - record[0] >= per:
        table[key] = [now, count]
        return count > most
    record[1] += count
    return record[1] > most


def _prune(now):
    """Keep the limit tables bounded: past _TRACK_MAX nicks, drop the ones
    whose window is over, and if that is not enough - many nicks inside one
    window - the oldest, until it is back at _TRACK_MAX. Forgetting a nick's
    count early only ever lets it through; the all-senders cap still holds."""
    if len(runtime.chat_rate) > _TRACK_MAX:
        for key in [k for k, (start, _n) in runtime.chat_rate.items()
                    if now - start >= INBOUND_PER and k not in (_ALL, _JOIN_WHO_ALL)]:
            runtime.chat_rate.pop(key, None)
    if len(runtime.chat_rate) > _TRACK_MAX:
        oldest = sorted((start, k) for k, (start, _n) in runtime.chat_rate.items()
                        if k not in (_ALL, _JOIN_WHO_ALL))
        for _start, key in oldest[:len(runtime.chat_rate) - _TRACK_MAX]:
            runtime.chat_rate.pop(key, None)
    for key in [k for k, until in runtime.chat_muted.items() if until <= now]:
        runtime.chat_muted.pop(key, None)


def _is_peer_realname(real):
    words = str(real or "").split(None, 1)
    return bool(words) and words[0] == REALNAME_MARK


def is_known_peer(nick):
    with runtime.chat_lock:
        return str(nick or "").lower() in runtime.chat_peers


_WHO_REPLY = re.compile(
    r"^:\S+\s+352\s+\S+\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)\s+\S+\s+:\d+\s+(.*)$")


def note_who_reply(line, now=None):
    """One 352 (WHO reply): `:srv 352 me #chan ident host srv nick H :0 real`.
    A nick whose realname starts with the mark is another DCCore bot, seen in
    that channel. Returns the nick, or None."""
    found = _WHO_REPLY.match(str(line or "").strip())
    if not found:
        return None
    chan, nick, real = found.group(1).lower(), found.group(2), found.group(3)
    if chan[:1] not in _CHANNEL_PREFIXES or not _is_peer_realname(real):
        return None
    if nick.lower() == str(getattr(config, "NICKNAME", "")).lower():
        return None
    now = time.time() if now is None else now
    with runtime.chat_lock:
        if nick.lower() not in runtime.chat_peers and len(runtime.chat_peers) >= PEER_MAX:
            return None
        runtime.chat_peers.setdefault(nick.lower(), {})[chan] = now
        # Only if a round is actually open for this channel (refresh_peers()
        # started one) - a WHO the operator's own client asked for, say,
        # must not feed this.
        if chan in runtime.chat_who_round:
            runtime.chat_who_round[chan].add(nick.lower())
    _deliver_peers(chan, now)
    return nick


def note_join(nick, chan):
    """A nick just joined one of the bot's channels: if it is not already a
    known peer, ask WHO for just that nick right away, rather than leave it
    to sit out of the sidebar for up to WHO_EVERY - a DCCore bot reconnecting
    (after a QUIT, say) is otherwise invisible here for up to ten minutes
    even though the server already told us it arrived.

    Already-known peers are left alone: this is only about finding out FAST
    that a stranger MIGHT be one of ours, not about refreshing someone
    already confirmed, so a busy channel's ordinary join/part churn does not
    turn into one extra WHO per join once the regulars are known.

    A single-nick WHO, not a full round: no round-tracking needed for this
    one either, the same way note_who_reply() already tolerates a WHO
    nobody opened a round for - a plain JOIN carries no realname (this bot
    negotiates no extended-join capability), so asking is the only way to
    tell."""
    if is_known_peer(nick):
        return
    # PACED, AND OUT OF THE CHANNEL'S WAY. Every stranger's JOIN used to
    # queue a WHO on the channel's own send-queue key - one line per pass for
    # that key, MSG_DELAY each - so joins and parts in a loop, or a netjoin,
    # held back what else the bot had to say there: the dashboard's @find
    # waited behind them past its window and came back empty.
    now = time.time()
    key = str(nick or "").lower()
    with runtime.chat_lock:
        _prune(now)
        if _limited(runtime.chat_rate, "who:" + key, 1, WHO_EVERY, now):
            return
        if _limited(runtime.chat_rate, _JOIN_WHO_ALL, JOIN_WHO_MOST, JOIN_WHO_PER, now):
            return
    _enqueue(JOIN_WHO_QUEUE, f"WHO {nick}\r\n")


def note_gone(nick, chan=None, now=None):
    """A peer left `chan` (or the network, with no channel).

    Prints either way (#982 follow-up): a departure this never even
    considered a peer is as useful to see, live, as one it removed - the
    one way to tell "was never known" from "removed but nothing reflects
    it" apart without guessing."""
    key = str(nick or "").lower()
    touched = []
    with runtime.chat_lock:
        seen = runtime.chat_peers.get(key)
        if seen is None:
            print(f"[CHAT] {key} left{f' {chan}' if chan else ' the network'}, "
                  f"but was not a known DCCore Chat peer - nothing to remove.")
            return
        if chan is None:
            touched = list(seen.keys())
            runtime.chat_peers.pop(key, None)
        else:
            chan = str(chan).lower()
            if chan in seen:
                seen.pop(chan, None)
                touched = [chan]
                if not seen:
                    runtime.chat_peers.pop(key, None)
    print(f"[CHAT] {key} is gone from DCCore Chat peers: "
          f"{', '.join(touched) if touched else '(nothing changed - it was not in ' + str(chan) + ')'}.")
    for one in touched:
        _deliver_peers(one, now)


def finish_who_round(chan, now=None):
    """The server's own "End of /WHO list" for `chan`: every peer this round
    did NOT reconfirm is gone from it, whether or not a QUIT or PART for
    them ever reached the read loop - a net split, a client that vanishes
    without one, or a QUIT line this capture missed for any reason all look
    the same from here: WHO no longer finds them. note_who_reply() only
    ever ADDS a sighting, so without this, a peer WHO stops finding would
    never be removed until PEER_FRESH quietly time it out, up to
    WHO_EVERY * 2.5 later.

    A silent no-op when no round is open for `chan` - a WHO the operator's
    own client asked for outside refresh_peers() must not read as one."""
    chan = str(chan or "").lower()
    with runtime.chat_lock:
        seen = runtime.chat_who_round.pop(chan, None)
        if seen is None:
            return
        gone = [nick for nick, per_chan in runtime.chat_peers.items()
                if chan in per_chan and nick not in seen]
        for nick in gone:
            per_chan = runtime.chat_peers.get(nick)
            if per_chan is None:
                continue
            per_chan.pop(chan, None)
            if not per_chan:
                runtime.chat_peers.pop(nick, None)
    if gone:
        _deliver_peers(chan, now)


def peer_channels(now=None):
    """{channel: set of peer nicks} for the channels the bot is in, from
    sightings that are still fresh."""
    now = time.time() if now is None else now
    ours = set(channels())
    out = {}
    with runtime.chat_lock:
        for nick, seen in list(runtime.chat_peers.items()):
            for chan, when in list(seen.items()):
                if now - when > PEER_FRESH:
                    seen.pop(chan, None)
                elif chan in ours:
                    out.setdefault(chan, set()).add(nick)
            if not seen:
                runtime.chat_peers.pop(nick, None)
    return out


def peer_nicks(now=None):
    nicks = set()
    for members in peer_channels(now).values():
        nicks |= members
    return sorted(nicks)


def cover(now=None):
    """The fewest channels that reach every peer at least once, greedily
    (most peers first, then by name), at most SEND_CHANNELS_MAX."""
    remaining = {chan: set(members) for chan, members in peer_channels(now).items()}
    picked = []
    while remaining and len(picked) < SEND_CHANNELS_MAX:
        best = max(sorted(remaining), key=lambda chan: len(remaining[chan]))
        if not remaining[best]:
            break
        picked.append(best)
        covered = remaining.pop(best)
        for chan in list(remaining):
            remaining[chan] -= covered
            if not remaining[chan]:
                del remaining[chan]
    return picked


def refresh_peers(now=None, force=False):
    """Ask WHO for every channel, at most every WHO_EVERY seconds (or at once
    with `force`). Through the paced queue like everything the bot says.
    Returns how many channels were asked.

    Opens a round in runtime.chat_who_round for each channel asked - see
    finish_who_round(), which closes it and reconciles who was actually
    seen once the server's own "End of /WHO list" for that channel
    arrives."""
    now = time.time() if now is None else now
    with runtime.chat_lock:
        if not force and now - runtime.chat_peers_meta.get("last", 0.0) < WHO_EVERY:
            return 0
        chans = channels()
        if not chans:
            return 0
        runtime.chat_peers_meta["last"] = now
        for chan in chans:
            runtime.chat_who_round[chan] = set()
    for chan in chans:
        _enqueue(chan, f"WHO {chan}\r\n")
    return len(chans)


def peers_line(now=None):
    """The operator's answer to `chat peers`."""
    members = peer_channels(now)
    if not members:
        return "No other DCCore bot seen yet (asked WHO every %d min)." % (WHO_EVERY // 60)
    parts = [f"{chan}: {', '.join(sorted(nicks))}" for chan, nicks in sorted(members.items())]
    return "DCCore bots seen - " + "; ".join(parts)


def peers_channel_line(chan, now=None):
    """`DCCORE PEERS <chan> <nick1> <nick2> ...` - the DCCore bots seen in
    one channel right now, for dccore.mrc's side-listbox. No nicks is a
    valid line: it says the channel's list is now empty."""
    chan = str(chan or "").lower()
    members = peer_channels(now).get(chan, set())
    return ("DCCORE PEERS " + chan + " " + " ".join(sorted(members))).rstrip()


def _deliver_peers(chan, now=None):
    """Same path as _deliver(): a pure append to the console session's
    outbox, only if one is authenticated and structured. `now` has to be the
    SAME clock the caller already resolved - peer_channels() prunes stale
    sightings against whatever `now` it is given, and a second, real clock
    here would prune a sighting a test just recorded with its own fixed one."""
    try:
        import adminchat
        session = adminchat.active_session()
    except Exception:
        return
    if session is None or not getattr(session, "authenticated", False):
        return
    # Only to a script that reads them (#1045): an older one prints each as text.
    if getattr(session, "structured", False) and getattr(session, "reads_peers", False):
        session.send(peers_channel_line(chan, now))


def capture(nick, target, message, now=None):
    """A channel message, or a private one addressed to us, arrived. Relay it
    if it is chat, from a known DCCore bot. Returns the recorded line, or
    None. Sends nothing, ever."""
    text = chat_text(message)
    if text is None:
        return None
    own = str(getattr(config, "NICKNAME", "")).lower()
    target_l = str(target or "").lower()
    if target_l[:1] in _CHANNEL_PREFIXES:
        if target_l not in channels():
            return None
        chan = target_l
    elif target_l and target_l == own:
        chan = None  # a private line - filled in below, once nick is known
    else:
        return None
    nick = str(nick or "").strip()
    if not nick or nick.lower() == own:
        return None
    if not is_known_peer(nick):
        return None
    if chan is None:
        chan = "@" + nick
    text = _plain(text)[:MAX_TEXT]
    if not text:
        return None
    now = time.time() if now is None else now
    key = nick.lower()
    with runtime.chat_lock:
        _prune(now)
        if runtime.chat_muted.get(key, 0) > now:
            return None
        if _limited(runtime.chat_rate, _ALL, INBOUND_ALL_MAX, INBOUND_PER, now):
            # Said once per window: the first line over the cap.
            first_over = runtime.chat_rate[_ALL][1] == INBOUND_ALL_MAX + 1
            too_many = True
        else:
            too_many = False
        if not too_many and _limited(runtime.chat_rate, key, INBOUND_MAX, INBOUND_PER, now):
            runtime.chat_muted[key] = now + INBOUND_HIDE
            hidden = True
        else:
            hidden = False
    if too_many:
        if first_over:
            print(f"[CHAT] More than {INBOUND_ALL_MAX} chat lines in {INBOUND_PER}s: the rest are dropped.")
            _deliver({"id": _next_id(now), "chan": chan, "nick": "*",
                      "text": f"More than {INBOUND_ALL_MAX} lines in {INBOUND_PER} seconds from everyone together: the rest are not shown."})
        return None
    if hidden:
        print(f"[CHAT] {nick} is sending too fast in {chan}: hidden for {INBOUND_HIDE}s.")
        _deliver({"id": _next_id(now), "chan": chan, "nick": "*",
                  "text": f"{nick} is sending too fast: hidden for {INBOUND_HIDE} seconds."})
        return None
    line = _record(chan, nick, text, now)
    _deliver(line)
    return line


def _enqueue(key, line, vip=False):
    """Onto the bot's paced outbound queue. The standard lane is one line per
    user per pass, so a line for one channel waits behind a line for each of
    the bot's other channels - a minute or more with a dozen channels, which
    is no way to chat. What an operator TYPED goes on the express lane
    (`vip`, as an advert does; the cap on `say()` keeps it from crowding
    anything); WHO, which nobody waits for, takes the standard lane."""
    oserve = sys.modules.get("oserve")
    if oserve is not None and hasattr(oserve, "queue_message"):
        oserve.queue_message(key, line, is_vip=vip)
        return
    if vip:
        config.vip_queue.append(line)
        return
    with runtime.send_queue_lock:
        config.send_queue.setdefault(key.lower(), []).append(line)


def say(sender, channel, text, now=None):
    """Say what an operator typed, as a tagged PRIVMSG. `channel` is one of
    the bot's channels, `*` for the fewest channels that reach every other
    DCCore bot seen, or a nick - only ever one WHO has already found, since
    that is the only proof this bot has that the other end can see it.
    (ok, message)."""
    raw = str(channel or "").strip()
    chan = raw.lower()
    private_to = None
    if chan == "*":
        targets = cover(now)
        if not targets:
            return False, ("No other DCCore bot seen in the bot's channels yet, so "
                           "there is nobody to say it to (chat peers, chat who).")
    elif chan[:1] in _CHANNEL_PREFIXES:
        if chan not in channels():
            return False, f"Not in {chan}: the bot can only chat in its own channels ({', '.join(channels()) or 'none yet'})."
        targets = [chan]
    elif chan and chan != str(getattr(config, "NICKNAME", "")).lower() and is_known_peer(chan):
        targets = [raw]
        private_to = raw
    elif chan:
        return False, (f"{raw} is not a DCCore bot this one has seen (chat peers, chat who) - "
                       f"only a known peer can be messaged privately.")
    else:
        return False, "Usage: chat #channel <text>, chat nick <text>, or chat * <text>, or just chat, for the channels."
    clean = _plain(text)
    clean = clean.encode("utf-8")[:MAX_SEND_BYTES].decode("utf-8", "ignore").strip()
    if not clean:
        return False, "Nothing to send."
    now = time.time() if now is None else now
    with runtime.chat_lock:
        # Every CHANNEL LINE counts, not every line typed: `chat *` says the
        # same words in several channels at once, which is what channel bots
        # take for spam, and the express lane should not carry more than the
        # cap says (#958 follow-up).
        if _limited(runtime.chat_outbound, str(sender or "console").lower(),
                    OUTBOUND_MAX, OUTBOUND_PER, now, count=len(targets)):
            return False, (f"Slow down: {OUTBOUND_MAX} chat lines a minute, a line said "
                           f"in several channels counting once for each, so chat "
                           f"never holds up the queue's own messages.")
    for target in targets:
        _enqueue(target, f"PRIVMSG {target} :{TAG} {clean}\r\n", vip=True)
    # The server does not send a message back to whoever sent it, so the
    # operator's own line is recorded and shown from here.
    record_chan = ("@" + private_to) if private_to else (targets[0] if len(targets) == 1 else "-")
    _deliver(_record(record_chan, str(getattr(config, "NICKNAME", "") or "?"), clean, now))
    return True, (f"Sent privately to {private_to}." if private_to else f"Sent to {', '.join(targets)}.")
