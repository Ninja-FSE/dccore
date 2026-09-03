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


# Other bots advertising in our channels ------------------------------------
# nick.lower() -> {"nick", "channel", "files", "list_date", "list_size",
#                  "last_seen"}, built from the periodic advert every
# file-serving bot sends. Here rather than in config.py for the reason this
# module exists: a !rehash re-executes config.py's body and would empty it.
# Persisted to data/known_bots.json by irc._flush_known_bots().
known_bots = {}
known_bots_flushed_at = 0.0

# Live transfer rate ---------------------------------------------------------
# Sampled by stats_mgr.live_speed(); kept here rather than in that module so a
# !rehash cannot reset it, and so readers that must not import the daemon can
# still see it. webserver.py reads these two directly for the dashboard.
live_speed_bps = 0        # bytes/sec across every sending transfer
live_speed_sampled_at = 0.0


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
