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

import collections
import re
import threading
import time

# The thread running the folder packer, while one runs (#651). dcc.py sets
# it when it starts inline_rar_packer and clears it in that thread's finally;
# dcc.a_pack_is_running() reads it. Here and not in dcc.py because a !rehash
# reloads dcc.py, and this is exactly the moment the rehash needs the answer:
# config.rar_inprogress is a scalar the reload resets to False, so on its own
# it cannot say whether a pack is still running or merely left a stale flag.
packer_thread = None

# The folder pack that is running, while one runs (#1202): a dict with the
# `rar` process handle (what a cancel terminates - that process, never an
# image name), who it is for, the folder's name as the list shows it, the
# archive's path, when it started and the folder's size. dcc.py owns the
# fields; the lock is here for the same reason the thread is - a !rehash
# reloads dcc.py and the new copy must see the pack the old one started.
pack_job = None
pack_lock = threading.Lock()

# Who the folder packer holds user_processing_lock for, from the moment it
# claims a nick until it releases it or hands the archive to its send (#1268):
# {"nick": ...}. irc.note_nick_change() moves the lock to the new nick and
# renames this holder under queue_lock in the same step, so the packer
# releases the key the lock was moved to - not the nick it started with, which
# left the new nick locked out for good. Here, with pack_job, because a
# !rehash reloads dcc.py while a pack runs.
pack_owner = None

# The folder packs waiting for room on the disk TMP_ZIP_DIR is on (#1376):
# a tuple of {"row", "total", "needed", "free"}, one per queue row whose
# files did not fit - dcc.py's pack_hold_reason(row) reads it, and that row
# waits while other packs go ahead. A tuple, rebound under queue_lock, not a
# list kept in place: nothing outside dcc.py holds on to it. And the watcher
# that looks at held packs again (dcc._watch_held_packs()), while one runs,
# so a second is not started. Here because a !rehash reloads dcc.py while a
# pack waits, and the new copy must see both.
pack_space_holds = ()
pack_hold_watch = None

# The stop-file watcher (#1065, stopping.py): one per process, kept here so a
# !rehash cannot start a second.
stop_watcher_thread = None

# When the bot's own link went down, while it is down (#652). The freeze
# box's clock - frozen_queues holds the moment each absent user was frozen -
# must not run during the bot's own outage, so on the way back every frozen
# timestamp is moved forward by the time spent here. Kept in this module so a
# !rehash during the outage cannot lose it; None while the bot is up.
freeze_clock_paused_at = None

# CHANNEL SYNC THAT ARRIVES LATE (#1271). irc.claim_channel_sync() sets
# config.bot_joined_channel once per connection, and two threads can get
# there: the activation (after its five-second settle) and the read loop,
# when a rejoin's NAMES fills channel_users on a connection whose activation
# found it empty. `channel_sync_waiting` is the epoch of the connection whose
# activation found nobody and is still waiting for a member list; None
# otherwise. The lock keeps the two from claiming twice.
channel_sync_lock = threading.Lock()
channel_sync_waiting = None

# HOW THIS SERVER FOLDS CASE (#1271): the CASEMAPPING token of its 005 line,
# reset to the RFC 1459 default at every connection. irc.irc_lower() reads it.
server_casemapping = "rfc1459"

# THE DCC ADDRESS THE BOT DETECTED (#1271), kept apart from MY_IP_OR_DOCK,
# which is the operator's pin and nothing else. Writing the detection into
# MY_IP_OR_DOCK made the first answer look like a pin to every later lookup,
# so the address was found once per process: a lookup that failed at boot,
# or a public address the ISP changed, stayed wrong until a restart.
# `dcc_address_found_at` is the time.monotonic() of the last lookup that
# found one; the lock keeps two lookups from running at once.
dcc_address_lock = threading.Lock()
dcc_address_detected = ""
dcc_address_found_at = None

# Per-user bookkeeping -------------------------------------------------------
# Failed sends per nick today (#1389, DAILY_SEND_FAILS_LIMIT), keyed like
# dcc_queue: {nick_key: {"day": date, "fails": n, "paused": bool}}. Only
# dcc.py's send-fail functions touch it, under failed_transfers_lock.
failed_transfers = {}    # Failed sends per nick today (#1389)
failed_transfers_lock = threading.Lock()
# The DAILY_SEND_FAILS_LIMIT circuit breaker (#1389), all under
# send_connected_lock: whether any DCC send has connected since start-up; the
# never-connected sends in a row since the last one that did, and the nicks
# they were for (dcc.never_connected_counts()); and whether this streak's
# "not counted" line has been logged. dcc.note_send_connected() resets the
# streak. Here so a !rehash keeps them.
send_connected_lock = threading.Lock()
send_connected_since_start = False
never_connected_streak = 0
never_connected_streak_nicks = set()
never_connected_streak_logged = False
channel_users    = {}    # Users currently seen in the channels
banned_users     = {}    # Currently banned users, in memory
user_requests    = {}    # Command timestamps per user, for anti-flood
muted_until      = {}    # Timers for temporarily muted users
whois_status     = {}    # Online status via WHO reply (True = online)
frozen_queues    = {}    # Saved timestamps for users in the freezer
queue_waiting_since = {} # {nick: when it began waiting for a slot}; the longest-waiting nick gets the next free one

# The central queue structures ----------------------------------------------
dcc_queue        = {}    # The main sharing queue, as {username: [files]}
vip_queue        = []    # Isolated express queue for search headers and adverts
fetch_request_queue = []  # The requests for files from other bots (#1028), sent ahead of everything else
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
told_queue_full_lock = threading.Lock()  # announce.py's queue-full notice memory (#888)
banned_users_lock  = threading.Lock()  # security.py's config.banned_users (#1248 review)

# dcc.py's library lookup (#580, #886), moved here in #749. They were built in
# dcc.py as `x = globals().get("x") or threading.Lock()` - kept across a reload
# only for as long as the old object is found - and the lock guard could not
# see the `or` form (it now can). The memories they protect stay in dcc.py:
# they are that module's own cache, not the configuration state the
# containers in this file are.
MAX_CONCURRENT_LIBRARY_SCANS = 2
library_scans      = threading.BoundedSemaphore(MAX_CONCURRENT_LIBRARY_SCANS)  # library scans at once
lookup_memory_lock = threading.Lock()  # dcc.py's lookup memories - misses, hits, folders
library_scan_turns = threading.Condition()  # dcc.py: one library scan per nick at a time (#969)

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

# When irc.reclaim_main_nick() last asked the server for the main nick, so
# the asks are spaced however often its holder leaves. A plain value, not a
# container: kept here so a !rehash of irc.py does not forget it.
nick_reclaim_asked_at = None

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


class SaveRehashOutcome:
    """How the rehash the latest settings save started went (#1462).

    A save writes settings.conf and starts a rehash on a thread of its own,
    so its answer can only say "started". The dashboard's Settings page asks
    here afterwards, by the number begin() gave the save, whether that rehash
    reloaded the settings or failed (a syntax error in admin_config.py, say).

    One record, not one per save: begin() moves it on to the newer save, and
    a page waiting for an older number takes the newer one's answer. That
    rehash started after both saves were written, so it read both. finish()
    for an older number than the record's is dropped for the same reason -
    the record already belongs to a rehash that read that save as well.

    Here, not in web_settings.py, so a rehash does not forget the answer
    while it reloads modules - this module is never reloaded.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._seq = 0              # the latest save's number; 0 = none yet
        self._ok = None            # True/False once its rehash finished; None while it runs
        self._finished_at = None   # time.time() when it finished

    def begin(self):
        """A save has been written and its rehash is about to start: its
        number, one more than the last."""
        with self._lock:
            self._seq += 1
            self._ok = None
            self._finished_at = None
            return self._seq

    def finish(self, seq, ok):
        """Record that save `seq`'s rehash finished; `ok` True only when it
        reloaded. Ignored when a later save has begun since."""
        with self._lock:
            if seq != self._seq:
                return
            self._ok = ok is True
            self._finished_at = time.time()

    def snapshot(self):
        """{"seq", "ok", "finished_at"}, read together."""
        with self._lock:
            return {"seq": self._seq, "ok": self._ok, "finished_at": self._finished_at}


save_rehash = SaveRehashOutcome()


# The cross-list search index's connection cache -----------------------------
# list_index.py keeps one sqlite3 connection open and reuses it; this guards
# that cache, not the database (sqlite3 serialises writers itself). Here for
# the reason every other lock in this file is: !rehash re-executes the module
# that would otherwise construct it, and a fresh Lock() on every reload lets
# two callers both believe they hold it.
list_index_lock = threading.Lock()

# The cross-list index's READ connection (#1129). The dashboard's filter
# queries use a second connection of their own, so a list being indexed no
# longer holds every keystroke for the whole write; this guards that one.
# Lock order: list_index_lock first, then this one. A reader never takes
# list_index_lock while it holds this, or a write closing the reader and a
# reader opening one would wait on each other for ever.
list_index_read_lock = threading.Lock()

# Set while no startup backfill of the index is running (#1296). The
# backfill runs on a thread of its own so the bot connects without waiting
# for it, and the dashboard's readers wait on this before they answer: an
# index still being filled would report every list it has not reached yet as
# holding no match. Set from the start, so a process that never runs one
# never waits.
list_index_backfill_done = threading.Event()
list_index_backfill_done.set()

# list_index's count of the backfills running now (#1367), changed under
# this; read without it.
list_index_backfills_lock = threading.Lock()

# transfer_log.py's database (#1068). One writer at a time, so two transfers
# ending together never meet a locked file; here, not in that module, for the
# reason every other lock in this file is.
transfer_log_lock = threading.Lock()

# speed_cap.py's clocks (#1352): per direction, the cap the clock runs at and
# the moment the line is next free. One clock for every transfer that way, so
# here: a second one after a reload would let the old transfers and the new
# ones each take the whole cap.
speed_cap_lock = threading.Lock()
speed_cap_clocks = {}

# db.py's download counters database (#1133). One writer at a time - the
# sends, the startup migrations and an import of the old JSON - so two
# transfers ending together never meet a busy file, and a damaged file is only
# moved aside while nothing here is writing it. Deliberately not disk_lock: a
# send's count must not wait behind every other file db.py writes. Here, not
# in db.py, because !rehash reloads db.py.
download_counts_lock = threading.Lock()

# list.count_request_lines()'s cache. Same reason as every other lock here:
# list.py is reloaded by !rehash, and a lock constructed there would be a new
# object after every reload while a counter mid-read still held the old one -
# two threads walking a 460 MB list at once, which is the exact cost the cache
# exists to remove. The cache dict itself stays in list.py: rebinding it on
# reload costs one recount, which is harmless, where rebinding the lock is not.
list_count_lock = threading.Lock()

# list.py's folder tables for the List Browser's pages of our own list
# (#1128). Here for the same reason as list_count_lock just above; the dict
# stays in list.py, where a reload costs one rebuild of a table. Taken INSIDE
# list_fetch's own lock for a fetched list's page, never around it.
list_folder_table_lock = threading.Lock()

# The automatic list refresh's start guard (#625). list_fetch.ensure_auto_
# refetch_worker() starts the hourly loop from wherever AUTO_REFETCH_LISTS is
# found on - boot, or the rehash a dashboard save fires - and must start it
# ONCE. Both halves of "once" live here: the lock for the reason every other
# lock in this file does, and the flag because a flag in a module a rehash
# reloads is reset by the very rehash that is about to consult it, and every
# Settings save would then start one more worker.
auto_refetch_guard   = threading.Lock()
auto_refetch_started = False

# The list rebuild schedule (#776), for the same reasons as the two above -
# a start guard a rehash cannot reset - plus the time the schedule last
# STARTED a rebuild. The list file's age says when one last finished; this is
# what stops a rebuild that fails (a folder whose disk is not mounted) from
# being started again every minute: it is retried at the next slot instead.
rebuild_schedule_guard        = threading.Lock()
rebuild_schedule_started      = False
rebuild_schedule_last_attempt = None

# The background audio reading (#1182): after a rebuild has published, the
# same update_list.py process goes on reading audio lengths, and the daemon
# stops waiting for it. audio_reading is that process's record while it runs
# (an object with .process, .kind, .started and .done, set by commands.py), or
# None; audio_reading_last is the last reading's result, for the dashboard.
# Here because a !rehash reloads commands.py while a reading can run for
# hours, and a reload must neither forget the process a new rebuild has to
# stop nor build a second lock beside the one a waiting thread holds.
audio_reading_lock = threading.Lock()
audio_reading      = None
audio_reading_last = None
# True while commands.retry_audio_rewrite_when_free() waits for the list
# downloads to finish, so one wait at a time (#1182 audit).
audio_retry_waiting = False
# The tokens this bot gave the update_list.py runs it started (#1365), newest
# last, at most RUN_TOKENS_KEPT: the shutdown asks those runs, and only those,
# to stop. Here so a !rehash does not forget a rebuild that is still going. A
# tuple, rebound as a whole by commands.audio_run_env(): nothing else keeps a
# reference to it, so it is not one of the containers defaults.py shares.
run_tokens = ()
RUN_TOKENS_KEPT = 16

# Automatic list grabbing (#926 item 5), list_grab.py. Here for the same
# reasons: a start guard a rehash cannot reset, and a wait in progress that a
# reload of list_grab.py must not forget (or it would plan a second grab).
# list_grab_plan: {"key", "nick", "planned", "at"} or None. list_grab_last:
# when the last automatic grab was made. list_grab_state: the per-bot tries and
# the removed-by-hand set, loaded from db.LIST_GRABS_FILE on first use.
# list_grab_others_asked: bot key -> when someone else typed "@Bot".
list_grab_guard        = threading.Lock()
list_grab_started      = False
list_grab_lock         = threading.Lock()
list_grab_plan         = None
list_grab_last         = None
list_grab_state        = None
list_grab_others_asked = {}

# Automatic discovery of a bot's OTHER channel-bound lists (#1240), a
# separate, much rarer pass from the grab above (that one finds a bot we hold
# nothing from yet; this one finds a SECOND list for one we already do).
# Its own guard/lock/timer so the two workers never block each other.
secondary_channel_guard = threading.Lock()
secondary_channel_started = False
secondary_channel_lock = threading.Lock()
secondary_channel_last = None
secondary_channel_tries = None

# DCCore Chat, relayed by the bot (#371) - serverschat.py. IN MEMORY ONLY:
# chat_recent is what other people said in the channels, and none of it is
# ever written to disk; a restart forgets it. Here so a rehash that reloads
# serverschat.py keeps the recent lines and the limits.
# chat_recent: the last serverschat.RECENT_MAX lines, oldest first, each
# {"id", "chan", "nick", "text"}. chat_rate / chat_outbound: key ->
# [window_start, count] for the arriving (per nick) and sent (per session)
# limits. chat_muted: nick -> until when it is hidden. chat_last_id: the
# last line id handed out.
chat_recent = []
chat_rate = {}
chat_outbound = {}
chat_muted = {}
chat_last_id = 0
# chat_peers: nick -> {channel: last seen in a WHO reply}, the other DCCore bots
# (their realname carries serverschat.REALNAME_MARK). chat_peers_meta["last"]:
# when the channels were last asked WHO.
chat_peers = {}
chat_peers_meta = {"last": 0.0}
# chat_who_round: channel -> the peers (lowercase nicks) actually seen in the
# WHO round currently in flight for it, from the moment refresh_peers() asks
# to the server's own "End of /WHO list" for that channel - which is also
# the one place a peer that quietly vanished (a QUIT or PART the read loop
# missed, a net split) is ever caught: WHO only ever ADDS a sighting, so
# without this, a peer WHO no longer finds would simply never be removed.
chat_who_round = {}
chat_lock = threading.Lock()

# What the on-connect commands achieved on THIS connection (#1066): the user
# modes the server last listed for us (221, None until it has), how many such
# listings have arrived (a query waits for this to move), whether the server
# said our host is hidden (396), and how often the commands were sent again.
# on_connect.reset_state() starts it over on every connect.
on_connect_state = {"modes": None, "listings": 0, "hidden": False,
                    "resends": 0, "gave_up": False}
on_connect_lock = threading.Condition()

update_check_guard        = threading.Lock()  # the version check's start guard (#572)
update_check_started      = False
update_check_last_attempt = None   # when the last check (daily or manual) began
update_check_last_manual  = None   # for the manual check's cooldown
update_check_at           = None   # when the last SUCCESSFUL check finished
update_check_error        = None   # why the last check failed, until one succeeds
update_check_error_at     = None
update_check_latest       = None   # the latest full release's tag
update_check_url          = None
update_check_newer        = False
update_check_announced    = None   # the release already said in the feed

# Other bots advertising in our channels ------------------------------------
# nick.lower() -> {"nick", "channel", "files", "list_date", "list_size",
#                  "last_seen"}, built from the periodic advert every
# file-serving bot sends. Here rather than in config.py for the reason this
# module exists: a !rehash re-executes config.py's body and would empty it.
# Persisted to data/known_bots.json by irc._flush_known_bots().
known_bots = {}
known_bots_flushed_at = 0.0
# When irc._record_bot() last let _prune_known_bots() expire old entries.
known_bots_pruned_at = 0.0

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

# announce.py's save of the private messages, which is made from a thread of
# its own a moment after a message is recorded - off the IRC read thread - and
# shared by every message recorded before it runs. Whether one is on its way,
# and that thread with the Event that cuts its wait short. Here rather than in
# announce.py because !rehash reloads that module.
private_messages_save_guard = threading.Lock()
private_messages_save_pending = False
private_messages_saver = None   # (thread, wake event) while one is waiting

kicked_channels = {}
kicked_channels_lock = threading.Lock()
# Channels whose JOIN the server refused with "target change too fast" (439/707,
# #1415): {channel_key: the time.time() it may be asked for again}. Under
# kicked_channels_lock. join_wait_seconds is the last wait the server gave,
# for the channels a cut-short JOIN burst left unasked.
join_waits = {}
join_wait_seconds = None
# Channels the server echoed the bot's own JOIN to on this connection, less
# the ones it was kicked from or parted since: the proof it is in them that a
# NAMES reply is not (irc.note_own_join()). Under kicked_channels_lock, and
# emptied with join_waits for each new connection.
joined_channels = set()

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
# recent_departure_bases: bare nick -> {departed nick.lower(): None}, for the
# departures in recent_departures whose nick is a bare nick plus a collision
# suffix. Kept beside it, under the same lock, so a JOIN looks up the few
# nicks it could be a retry of instead of walking every departure.
recent_departure_bases = {}

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

# The same bot under another nick, by its IDENT (#376, option B). Both RAM
# ONLY, and that is the decision this rests on: never written to disk, never
# logged, gone on restart - not a registry of other operators' addresses. No
# host and no IP are kept in any form; the ident is the part before the "@"
# that the user or their client picks.
#
# bot_idents: nick.lower() -> {"ident", "first_seen"}, for a known bot we
# heard in a channel this session (irc._capture_bot_ident()). first_seen is
# when we first SAW that nick: its JOIN, if we saw one in the last
# IDENT_MERGE_WINDOW_SECONDS, else its first channel message - which is what
# both "never advertising at the same time" and the time window are
# measured against.
#
# recent_joins: nick.lower() -> when we saw it JOIN. A nick and a time, no
# ident and no host, and only for IDENT_MERGE_WINDOW_SECONDS: a bot's first
# advert can come many minutes after it joined, and the join is when it
# actually appeared.
#
# bot_departures: nick.lower() -> when a QUIT, PART, KICK or NICK of that bot
# was OBSERVED (irc.note_observed_departure()), never just its absence -
# absence also covers "was never in a channel we share".
#
# webserver._ident_merges() is the one reader, and like nick_aliases it is
# display only: fetched_bot_lists, known_bots and the counters stay keyed per
# nick.
bot_idents = {}
bot_departures = {}
recent_joins = {}
bot_idents_lock = threading.Lock()


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

# The dashboard login (webserver.py) --------------------------------------
#
# web_login_lock guards webserver._web_bad_ips and _web_logins_in_flight,
# and it makes "may this address try again?" and "this attempt counts" ONE
# step. They used to be two: the login checked the address, spent a tenth
# of a second hashing the password, and only then counted the failure - so
# a burst of parallel POSTs from one address all passed the check before
# any of them was counted, and the limit of MAX_PASSWORD_ATTEMPTS guesses
# per block window became as many guesses as could be sent at once.
#
# web_password_checks caps how many dashboard passwords are hashed at the
# same time, across every address. Each check is a deliberately slow PBKDF2
# run, and a flood of logins - even from many addresses, each within its
# own limit - must not take the CPU from the transfers.
#
# web_sessions_lock guards the dashboard's table of live sessions (kept per
# app inside webserver.create_app()), which is what lets a logout end a
# session on the server and not only in the browser that clicked it.
WEB_PASSWORD_CHECKS_AT_ONCE = 2
web_login_lock = threading.Lock()
web_password_checks = threading.BoundedSemaphore(WEB_PASSWORD_CHECKS_AT_ONCE)
web_sessions_lock = threading.Lock()

# The DCC console's login and token store. adminchat.py is not reloaded by
# !rehash, but db.py is, and both locks are taken around db.py's own calls;
# here for the same reason as every other lock in this file.
#
# admin_bad_ip_lock: adminchat._bad_ips, the per-address count of failed
# console passwords. A login attempt is checked against the block AND counted
# under one hold of it, before the password is verified - checked first and
# counted only after the (slow, PBKDF2) verify, parallel logins from one
# address each passed the check before any of them was counted, and got more
# guesses than MAX_PASSWORD_ATTEMPTS between them.
#
# admin_tokens_lock: the paired-client token store's load-modify-save in the
# console's `pair` and `unpair`. Without it a `pair` in one session could
# write back the store it loaded before an `unpair` in another removed a
# token, and the revoked token worked again. Lock order: this one first, then
# disk_lock (db.save_admin_tokens() takes it inside).
admin_bad_ip_lock = threading.Lock()
admin_tokens_lock = threading.Lock()

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

    def hold_for(self, seconds):
        """Push the next free slot to at least `seconds` from now (#1386).

        The later of that and the slot already booked, so a hold never
        brings a send forward. Every lane waits on this one clock, so the
        hold stops them all; the waiters are woken to see the new time
        rather than send at the one they were sleeping towards.
        """
        with self._cond:
            self._next_allowed = max(self._next_allowed,
                                     time.monotonic() + seconds)
            self._cond.notify_all()


outbound_pacer = OutboundPacer()


# Target change too fast (#1386) ---------------------------------------------
#
# ircu limits how fast a client may start messaging new targets, and answers
# a line over the limit with 439 (707 on hybrid, ratbox and charybdis): the
# line was DROPPED. irc.py holds the pacer above when one arrives, and puts
# the refused line back at the front of the lane it came from, once. For
# that it has to know which line went to that target and from which lane,
# and that is what the paced senders record here, as each line goes out.
#
# Bounded both ways: the last PACED_LINES_KEPT lines, and only lines sent
# within PACED_LINE_WINDOW seconds count. A refusal arrives within a second
# or two of the line it refuses, so anything older is not what it means.
PACED_LINES_KEPT = 30
PACED_LINE_WINDOW = 60.0
recent_paced_lines = collections.deque(maxlen=PACED_LINES_KEPT)
recent_paced_lock = threading.Lock()

_PACED_LINE_TARGET = re.compile(r"^(?:PRIVMSG|NOTICE)\s+(\S+)\s", re.IGNORECASE)


class ResentLine(str):
    """A refused line on its way out a second time (#1386). The lanes hold
    plain strings; this is one that carries the mark, so the record it makes
    when it is sent says so, and a second refusal does not put it back."""


def _paced_line_target(line):
    """The target of a PRIVMSG or NOTICE line, or None for any other line."""
    match = _PACED_LINE_TARGET.match(str(line or ""))
    return match.group(1) if match else None


def note_paced_line(line, lane, owner=None):
    """Record a paced line that has just been sent (#1386): its target, the
    lane it came from and, for the standard lane, whose queue it was in.

    `lane` is "fetch", "vip", "standard", "debug", or None for a line that
    no lane can take back - a DCC ACCEPT, and the unpaced DCC SEND handshake,
    send error NOTICEs and admin DCC CHAT offer: such a line is recorded,
    so that a refusal for it is not taken for an older line to the same
    target. A line that is not a PRIVMSG or NOTICE is not recorded.
    """
    target = _paced_line_target(line)
    if target is None:
        return
    entry = {"at": time.monotonic(), "target": target, "line": line,
             "lane": lane, "owner": owner,
             "resent": isinstance(line, ResentLine)}
    with recent_paced_lock:
        recent_paced_lines.append(entry)


def take_paced_line(target, fold=str.lower):
    """The record of the most recent paced line sent to `target` within
    PACED_LINE_WINDOW seconds, removed so that it is answered once; None when
    there is none. `fold` is the server's case folding (irc.irc_lower())."""
    wanted = fold(target)
    cutoff = time.monotonic() - PACED_LINE_WINDOW
    with recent_paced_lock:
        for index in range(len(recent_paced_lines) - 1, -1, -1):
            entry = recent_paced_lines[index]
            if entry["at"] < cutoff:
                # Appended in time order: everything before it is older still.
                return None
            if fold(entry["target"]) == wanted:
                del recent_paced_lines[index]
                return entry
    return None


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
