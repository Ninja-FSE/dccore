# runtime.py - live in-memory state, deliberately kept out of config.py.
"""The containers the daemon mutates while it runs.

WHY THIS MODULE EXISTS

`!rehash` calls importlib.reload() on config.py, which re-executes the module
body - and every `dcc_queue = {}` in it therefore rebinds to a brand new empty
container. Anything the daemon had accumulated was thrown away by the reload
itself.

commands.py grew a rescue for that: read the containers out before the reload,
write them back after. It works, but it is a list of names that has to be kept
in step with config.py by hand, and it fell out of step exactly once already -
the cross-bot fetch feature added two containers without touching the list, so
a !rehash silently emptied every fetched bot list and reported zero active
fetches while transfers were still moving bytes.

This module is never reloaded (see commands.py's CORE_MODULES). Its
containers therefore survive a reload for a structural reason rather than
because somebody remembered to add them to a list, and a container added here
in future is safe without anyone doing anything.

HOW config.py STILL SEES THEM

config.py binds the same objects:

    import runtime
    dcc_queue = runtime.dcc_queue      # the same dict, not a copy

so every existing `config.dcc_queue[user]` keeps working untouched - there are
several hundred such references and none of them had to change. After a reload
config.py re-runs those bindings and picks the same live objects back up.

THE ONE RULE

    Mutate these in place. Never rebind them.

    config.dcc_queue.clear()          # correct
    config.dcc_queue.update(rows)     # correct
    config.dcc_queue = {}             # WRONG - silently detaches config's name
                                      # from the object runtime.py still holds,
                                      # and the two drift apart from then on

That is an easy mistake: the rehash restore path made it twice before this
change. tests/test_runtime_state.py parses the source and fails on any
rebinding of a name defined here, so it cannot be made silently.

WHAT IS NOT HERE

Scalars. `search_inprogress` and `rar_inprogress` stay in config.py, because
the binding trick above only works for mutable objects - rebinding a bool in
config.py could never write through to this module, so moving them would
change nothing except to make it look as though it had. Their behaviour across
a rehash is unchanged.
"""

import threading
import time

# The thread running the folder packer, while one runs (#651). dcc.py sets
# it when it starts inline_rar_packer and clears it in that thread's finally;
# dcc.a_pack_is_running() reads it. Here and not in dcc.py because a !rehash
# reloads dcc.py, and this is exactly the moment the rehash needs the answer:
# config.rar_inprogress is a scalar the reload resets to False, so on its own
# it cannot say whether a pack is still running or merely left a stale flag.
packer_thread = None

# When the bot's own link went down, while it is down (#652). The freeze
# box's clock - frozen_queues holds the moment each absent user was frozen -
# must not run during the bot's own outage, so on the way back every frozen
# timestamp is moved forward by the time spent here. Kept in this module so a
# !rehash during the outage cannot lose it; None while the bot is up.
freeze_clock_paused_at = None

# Per-user bookkeeping -------------------------------------------------------
failed_transfers = {}    # Failed-transfer counter, per user
channel_users    = {}    # Users currently seen in the channels
banned_users     = {}    # Currently banned users, in memory
user_requests    = {}    # Command timestamps per user, for anti-flood
muted_until      = {}    # Timers for temporarily muted users
whois_status     = {}    # Online status via WHO reply (True = online)
frozen_queues    = {}    # Saved timestamps for users in the freezer

# The central queue structures ----------------------------------------------
dcc_queue        = {}    # The main sharing queue, as {username: [files]}
vip_queue        = []    # Isolated express queue for search headers and adverts
active_transfers = []    # Live DCC sends, one thread each

# Cross-bot search/fetch (beta-web) ------------------------------------------
# Added here, not in config.py, for the exact reason this module exists: a
# !rehash silently emptied these two the moment the feature landed, since
# they were plain config.py globals like everything above USED to be - the
# same bug class this file was created to remove structurally rather than by
# remembering to list every container. See tests/test_runtime_state.py's
# test_config_does_not_define_its_own_containers, which now catches this for
# any future addition too.
broadcast_search_results = []  # Captured replies during an open @find broadcast window
fetch_queue              = {}  # Cross-bot file/list fetch requests, keyed by request id
fetched_bot_lists        = {}  # Parsed lists fetched FROM other bots, keyed by lowercased nick

# channel_users is mutated from the IRC read thread (irc.py, on every
# JOIN/PART/QUIT/353) and iterated from other threads (dcc.py's queue
# dispatch, commands.py's rehash) with no lock at all until this one -
# unlike dcc_queue/fetch_queue/fetched_bot_lists, which have always had one.
# Lives here rather than being allocated onto config by oserve.py like those
# three: a Lock() object constructed in config.py would be a NEW lock every
# time !rehash reloads it, exactly the rebind trap the rest of this module
# exists to avoid - config.py never held a lock object directly for that
# reason, and this one shouldn't be the first.
_channel_users_lock = threading.Lock()

# Every other module-level lock a reloaded module used to allocate for itself.
#
# importlib.reload() re-executes a module body, and `queue_lock = threading.
# Lock()` at module level is rebound exactly like `dcc_queue = {}` was before
# this file existed - a thread already inside `with dcc.queue_lock:` goes on
# holding the OLD object, the next caller acquires the fresh one !rehash just
# created, and both proceed into the critical section at once. The trigger is
# routine: the web dashboard fires a rehash on every Settings save, so this is
# reachable by an operator clicking Save while a transfer is running, not by
# anything exotic.
#
# Same fix as _channel_users_lock above, generalised: allocate the lock HERE,
# where it is never reloaded, and have the owning module bind its name to
# this object (`queue_lock = runtime.queue_lock`) instead of constructing its
# own. A reload of that module re-runs the binding statement and picks the
# same live lock back up - the identical trick runtime.py already uses for
# containers, just for an object a rebind can silently break instead of empty.
queue_lock         = threading.Lock()  # dcc.py's transfer queue - see dcc.py's own comment
debug_drain_guard  = threading.Lock()  # announce.py's single-drain-worker start guard
debug_sinks_lock   = threading.Lock()  # announce.py's admin-console debug sink list
disk_lock          = threading.Lock()  # db.py's serialised on-disk writes

# The reload window, which is not only about rebinding.
#
# importlib.reload(defaults) re-executes defaults.py from the top, and that
# file is a list of literal assignments (NICKNAME = None, CHANNEL = None, ...)
# with `settings_file.apply_to(globals())` only at the very END. So for the
# whole of a reload every setting an operator configured is transiently back
# to its shipped default - not corrupted, just not applied yet.
#
# Measured on a real install's file: a reader looping on config.NICKNAME
# during !rehash saw it blank for 52% of the reload. That is not a narrow
# race to reason away; it is half the window.
#
# Found from the dashboard. An operator saved DEBUG_CHANNEL, the browser
# re-fetched /api/settings the instant the response said "Rehash started",
# and the Settings page came back with Nickname, Admin nick(s) and Channels
# EMPTY. Nothing was lost - settings.conf was intact the whole time and a
# refresh showed the real values - but the page an operator uses to check
# their configuration told them their configuration was gone.
#
# Only the three REQUIRED settings looked wrong, which is why it took a real
# install to notice: every other setting on that page has a shipped default
# that happens to match what most operators run (SERVER, WEBUI_HOST), so it
# renders identically whether or not settings.conf has been applied yet.
#
# RLock, not Lock: the rehash thread holds this across the reload and calls
# announce.send_debug() inside it, which fans out to the web console sink -
# webserver code, on the same thread, reaching for the same lock.
#
# Here rather than in commands.py for this module's founding reason: a lock
# allocated in a module that !rehash reloads is a NEW lock every rehash, and
# this one is held BY the rehash.
config_reload_lock = threading.RLock()

# Only one rehash at a time.
#
# handle_rehash_request() reloads modules AND then compares the channel list it
# reads afterwards against the one it read before, to work out what to JOIN and
# what to PART. Two of them overlapping is not merely wasteful: the second
# one's reload puts config.CHANNEL back to its literal None for the ~1ms window
# described on config_reload_lock above, and if the FIRST one reads its "new"
# channel list inside that window it sees no channels at all - so every channel
# the bot is in falls into the PART branch. Measured by audit: the bot PARTed
# every channel including the debug channel, sent no JOIN and no NAMES, emptied
# channel_users, and logged "[REHASH SYNC] Channel sync completed successfully."
# dcc.py treats channel_users as proof a user is present, so every queue then
# freezes. Reproduced with nothing patched in 4 of 60 overlapping runs.
#
# Overlapping rehashes are easy to reach: irc.py spawns an unguarded thread per
# "!rehash", adminchat.py does the same from the console, and webserver.py
# fires one on EVERY Settings save and every password change.
#
# SERIALISED, not skipped. A second rehash is often the one that matters - a
# dashboard save writes settings.conf and then triggers it, and the rehash
# already running may have read the file before that write. Dropping it would
# lose the operator's change; waiting applies it.
#
# Here rather than in commands.py because commands.py is one of the modules a
# rehash reloads, so a lock allocated there would be a new lock every time -
# the founding reason this module exists.
rehash_lock = threading.Lock()


# The cross-list search index's connection cache -----------------------------
# list_index.py keeps one sqlite3 connection open and reuses it; this guards
# that cache, not the database (sqlite3 serialises writers itself). Here for
# the reason every other lock in this file is: !rehash re-executes the module
# that would otherwise construct it, and a fresh Lock() on every reload lets
# two callers both believe they hold it.
list_index_lock = threading.Lock()

# list.count_request_lines()'s cache. Same reason as every other lock here:
# list.py is reloaded by !rehash, and a lock constructed there would be a new
# object after every reload while a counter mid-read still held the old one -
# two threads walking a 460 MB list at once, which is the exact cost the cache
# exists to remove. The cache dict itself stays in list.py: rebinding it on
# reload costs one recount, which is harmless, where rebinding the lock is not.
list_count_lock = threading.Lock()

# The automatic list refresh's start guard (#625). list_fetch.ensure_auto_
# refetch_worker() starts the hourly loop from wherever AUTO_REFETCH_LISTS is
# found on - boot, or the rehash a dashboard save fires - and must start it
# ONCE. Both halves of "once" live here: the lock for the reason every other
# lock in this file does, and the flag because a flag in a module a rehash
# reloads is reset by the very rehash that is about to consult it, and every
# Settings save would then start one more worker.
auto_refetch_guard   = threading.Lock()
auto_refetch_started = False

# Other bots advertising in our channels ------------------------------------
# nick.lower() -> {"nick", "channel", "files", "list_date", "list_size",
#                  "last_seen"}, built from the periodic advert every
# file-serving bot sends. Here rather than in config.py for the reason this
# module exists: a !rehash re-executes config.py's body and would empty it.
# Persisted to data/known_bots.json by irc._flush_known_bots().
known_bots = {}
known_bots_flushed_at = 0.0

# Offers waiting for the receiver to connect ---------------------------------
# Keyed by (nick_lower, port) -> {"filename", "size", "position"}, one entry
# per DCC SEND handshake that has gone out and not yet been picked up.
#
# It exists so a DCC RESUME can find the offer it belongs to. The RESUME
# arrives on the IRC read loop, in a different thread from the one blocked in
# accept(), so the two need somewhere to meet. Keyed by PORT and not by
# filename: the port is ours, unique, and unambiguous, while the offered name
# has already been through a space-to-underscore pass and may have been
# shortened to fit the IRC line.
#
# Here rather than in dcc.py for this module's usual reason: a !rehash
# re-executes that module's body and would drop every offer in flight.
# CHANNELS WE HAVE BEEN THROWN OUT OF, and how many times a rejoin has been
# refused since. Keyed by lowercase channel name.
#
#     {"#chan": {"refusals": 0, "kicked_at": 1788904212.0, "by": "someop"}}
#
# Live state, so it belongs here rather than in a module body: a !rehash
# re-executes those, and forgetting we were kicked would restart the retry
# count from zero every time the operator saved a setting - which is a rejoin
# loop with extra steps.
#
# A channel is removed from this map the moment a join succeeds, so its
# presence means "not in it, and still trying" and nothing else.
# THINGS THE OPERATOR SHOULD BE TOLD ABOUT, newest last. Not a log - the
# Console is the log, and it carries everything. This is the short list of
# events that mean the bot's ability to do its job changed, and that somebody
# may need to act on:
#
#     {"id": 7, "at": 1788904212.0, "severity": "error",
#      "text": "Cannot rejoin #chan - gave up after 3 attempts."}
#
# `notice_state["seen_id"]` is the highest id the operator has acknowledged,
# which is what the unread count is measured against. It lives INSIDE a dict
# rather than beside it as a plain int because only mutable objects can be
# bound onto config by reference - a scalar there would be a copy, and the
# dashboard marking notices read would update a number the daemon never sees.
notices = []
notice_state = {"seen_id": 0}
notices_lock = threading.Lock()

# SOMEBODY SPOKE TO THE BOT AND IT SAID NOTHING BACK, newest last.
#
# A private message that is not a recognised command is dropped in the read
# loop - no reply, and until now no record either. That silence is deliberate
# and mostly right: a bot that answers every stray message is a bot that can
# be made to flood itself off the network. But there is a real gap between
# "do not reply to strangers" and "the operator never finds out anyone spoke
# to it", and somebody messaging a file server is usually somebody who wants
# something from it and does not know the syntax.
#
#     {"id": 4, "at": 1788904212.0, "nick": "SomeUser",
#      "text": "can you send me the new album"}
#
# Kept apart from `notices` on purpose. A notice is something that went WRONG
# and has two severities; a message is neither wrong nor right, and giving it
# a severity would mean inventing a third one that nobody can tell apart at a
# glance - which the notice design says explicitly it will not do.
private_messages = []

# Also carries {"declined": {nick.lower(): when}} when PRIVATE_MESSAGES_ENABLED
# is off - who has already been told where to go instead.
#
# Kept HERE, in the state dict that is already persisted, rather than in a
# sixth state file: a restart that forgot this would tell everybody again,
# and "the bot repeats itself every time the operator restarts it" is most of
# what this record exists to prevent. It is pruned on write rather than left
# to grow, because an entry older than the interval can never stop a reply
# again and keeping it is keeping a fact that has stopped meaning anything.
private_message_state = {"seen_id": 0}

# When the last few declines went out, newest last, for the across-everyone
# ceiling. RAM only and deliberately so: this one is about a burst happening
# RIGHT NOW, and a bot that has just restarted is not in the middle of one.
private_message_decline_sends = []

# One lock for all three. They are written together by
# announce.decline_private_message() and read together by the Messages
# payload, so a second lock would buy nothing but an ordering question.
private_messages_lock = threading.Lock()

kicked_channels = {}
kicked_channels_lock = threading.Lock()

# config.send_queue's lock (#665, audit L1). The per-user text lanes are
# written by every request, search and reply thread (oserve.queue_message)
# and drained by the pump (queue_mgr.next_standard_line), and had no lock:
# their correctness rested on where CPython happens to check for a thread
# switch, which differs between 3.10 and 3.11+ and is gone on a
# free-threaded build. The two touches are tiny; both take this. Here, not
# in queue_mgr.py or oserve.py - a !rehash reloads those.
send_queue_lock = threading.Lock()

dcc_send_offers = {}
dcc_send_offers_lock = threading.Lock()

# Alt-nick reconnects (#376) --------------------------------------------------
# Two small, RAM-only registries that together let the List Browser sidebar
# merge a peer bot's two nicks (its usual one, and the alt it fell back to
# after a 433 at connect) into one row, instead of showing what looks like two
# unrelated bots.
#
# recent_departures: nick.lower() -> {"channel", "at"}. Written ONLY by
# irc.py's PART/QUIT handlers, and only for a nick they actually saw removed
# from config.channel_users - never for a nick that merely stopped appearing,
# which absence alone cannot tell apart from "was never in a channel we
# share". Read, briefly, by irc.py's JOIN handler (note_possible_reconnect())
# to decide whether a newly-joining nick is the same connection coming back.
# Self-pruning on a short TTL of its own (ALT_NICK_RECONNECT_WINDOW_SECONDS in
# irc.py) - the inference is only trustworthy for seconds, not minutes.
recent_departures = {}
recent_departures_lock = threading.Lock()

# nick_aliases: alias_nick.lower() -> primary_nick, REAL case, written only when
# note_possible_reconnect() decides a join matches the shape above. DISPLAY
# ONLY - resolve_display_nick() below is the one reader, and
# webserver.build_fetched_bot_list_summaries() is its one caller. Nothing
# here ever reaches config.channel_users, fetched_bot_lists, known_bots or a
# download counter: a wrong guess mis-groups one sidebar row and nothing else,
# which is the whole reason this is allowed to be a heuristic rather than
# something requiring proof.
#
# ONLY GROWS, and deliberately: an alias is one tiny dict entry, the event
# that creates one is rare, and the alternative - expiring it - would mean a
# genuine reconnect eventually un-merging itself for no reason connected to
# anything having changed. Not a memory concern at any realistic uptime.
# What DOES stop a stale alias being trusted forever is webserver.py's own
# `_display_nick()`, which checks CURRENT presence at display time rather
# than relying on this dict's age - see that function's docstring.
#
# SINGLE-HOP ONLY: resolving "SomeBot__" after two collisions in a row lands
# on "SomeBot_" (whichever nick it actually replaced), not on "SomeBot" -
# resolve_display_nick() does one dict lookup, not a walk to a fixed point.
# A bot that collides twice in a row therefore still splits into two sidebar
# identities instead of merging into one - strictly better than today's
# three, but not the full transitive merge the name of this feature might
# suggest.
nick_aliases = {}
nick_aliases_lock = threading.Lock()


def resolve_display_nick(nick):
    """The nick a List Browser row should be grouped and labelled under.

    `nick` itself, unless irc.py's note_possible_reconnect() has aliased it to
    a nick it just replaced - see nick_aliases's own comment above for what
    that is and, as importantly, is not allowed to affect. Pure lookup, no
    lock ordering concerns with anything else: this is the only place
    nick_aliases is read.
    """
    key = str(nick or "").strip().lower()
    if not key:
        return nick
    with nick_aliases_lock:
        primary = nick_aliases.get(key)
    return primary if primary else nick

# Held across the !update re-entrancy check AND the flag it sets (#444).
#
# handle_list_update_request() read config.update_inprogress and did not set
# it until 178 lines later, with a PAUSE_ON_UPDATE wait for any running search
# in between - so two requests arriving in that window both passed the guard
# and both started a rebuild, two subprocesses writing the same .new temp
# paths. The check and the set have to be one step.
#
# list.execute_search() takes the same gate for search_inprogress (#607): a
# thread per @find read that flag and set it twenty lines later, so two @find
# lines from one recv() buffer both walked the master list at once. One lock
# for both flags, so a search and a rebuild cannot slip past each other.
list_update_gate = threading.Lock()

# Live transfer rate ---------------------------------------------------------
# Sampled by stats_mgr.live_speed(); kept here rather than in that module so a
# !rehash cannot reset it, and so readers that must not import the daemon can
# still see it. webserver.py reads these two directly for the dashboard.
# Failures and searches since this PROCESS started (#754), counted where every
# one passes: announce.feed_event(). Here and not in announce.py, which a
# !rehash reloads - a counter there would go back to zero on every Save.
feed_counts = {"FAIL": 0, "SEARCH": 0}
live_speed_bps = 0        # bytes/sec across every sending transfer, summed
live_speed_sampled_at = 0.0


# Outbound pacing ------------------------------------------------------------
#
# There used to be two: queue_mgr.py's queue_worker slept MSG_DELAY after
# every send, and announce.py's debug drain slept DEBUG_MSG_DELAY after every
# send, on its own thread, deaf to the first. The server only ever sees the
# sum of the two - a bot with fourteen channels can burst enough debug lines
# on reconnect to add up past what Undernet allows, while every setting an
# operator can see looks polite in isolation. Two numbers that multiply into
# a third that appears nowhere is not something an operator can reason about.
#
# One clock now, shared by every lane. Each sender still asks for its own
# interval - queue_mgr.py asks for MSG_DELAY, announce.py's debug drain asks
# for whichever of MSG_DELAY and DEBUG_MSG_DELAY is larger, so debug can be
# throttled slower than ordinary traffic if an operator wants that, but never
# faster - and every reservation, from either lane, holds the SAME clock for
# that long before anyone else's next send. The combined rate can never
# exceed one interval's worth of traffic, however the two lanes interleave.
#
# FIRST COME, FIRST SERVED (#655, audit M53). This used to be sleep-and-retry
# with no queue: every waiter slept until the same instant and whoever woke
# first took the slot. Four threads share the clock - queue_worker's VIP and
# standard lanes, the debug drain, the !ping and DCC ACCEPT direct waiters -
# so queue_worker's strict alternation bounded VIP to two of its OWN slots
# while the worker lost each of those to the drain by coin toss. Measured
# with the real threads: a drain backlog gave VIP about a quarter of the
# slots and gaps of ten to fourteen slots (a minute at MSG_DELAY=5) between
# consecutive VIP lines. Tickets, handed out in arrival order and served in
# that order, make every lane's wait bounded by the number of lanes ahead of
# it; a waiter that leaves without its slot (an exception) is stepped over
# rather than blocking the line.
class OutboundPacer:
    def __init__(self):
        self._cond = threading.Condition()
        self._next_allowed = 0.0
        self._next_ticket = 0    # the next ticket to hand out
        self._serving = 0        # the ticket whose turn it is
        self._abandoned = set()  # tickets whose holder left without a slot

    def wait_for_slot(self, min_interval):
        """Block until the shared clock has a slot free AND it is this
        caller's turn, then take it.

        The turn is the ticket order. Only the ticket being served sleeps
        against the clock; everyone behind it waits to be woken, so nothing
        wakes early and races. A waiter that raises while queued (the
        thread is being torn down) marks its ticket abandoned on the way out
        and wakes the others, so the line moves on.
        """
        with self._cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            served = False
            try:
                while True:
                    while self._serving in self._abandoned:
                        self._abandoned.discard(self._serving)
                        self._serving += 1
                    now = time.monotonic()
                    if ticket == self._serving:
                        if now >= self._next_allowed:
                            self._next_allowed = now + min_interval
                            self._serving += 1
                            served = True
                            self._cond.notify_all()
                            return
                        self._cond.wait(self._next_allowed - now)
                    else:
                        self._cond.wait()
            finally:
                if not served:
                    self._abandoned.add(ticket)
                    self._cond.notify_all()


outbound_pacer = OutboundPacer()


def channel_users_lock():
    """The lock every read and write of channel_users must hold.

    A deferred import, not a module-level one: config.py imports this
    module, so importing config back at load time here would cycle. By the
    time this function is actually called, both modules are already fully
    loaded, so the deferred import just looks config up in sys.modules -
    the standard way to break a cycle like this one.
    """
    import defaults as config
    return getattr(config, "channel_users_lock", None) or _channel_users_lock
