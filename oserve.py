# oserve.py - The central hub that wires every module together
import threading
import time
import sys
import os

# FIRST, before anything else can print. Several modules print at import time,
# and on a console whose code page cannot encode the Swedish log strings
# (cp1253, cp1251, cp932, ascii - anything but Western European) an unguarded
# print() raises UnicodeEncodeError and takes the thread down with it. See
# platform_compat.install_console_encoding_guard for the full explanation.
import platform_compat
platform_compat.install_console_encoding_guard()

# Load the bot's modules
import defaults as config

# Allocate the locks at startup, in memory. This keeps config.py free of
# function calls and imports.
#
# No config.queue_lock here: dcc.py's own module-level `queue_lock` (created at
# dcc.py's import time, before any function that uses it can be called) is THE
# queue lock every part of this codebase means by that name - see dcc.py's own
# comment on it. A separate config.queue_lock used to be allocated here and used
# in exactly one place (announce.py's transfer-speed calculation), guarding the
# same config.active_transfers list dcc.py's mutations guard with dcc.queue_lock -
# two different lock objects for the same data, providing no mutual exclusion
# against each other at all. Fixed by pointing announce.py at dcc.queue_lock
# instead; this allocation is removed rather than left to invite the same mistake
# again.

if not hasattr(config, 'debug_flood_lock'):
    config.debug_flood_lock = threading.Lock()

if not hasattr(config, 'fetch_queue_lock'):
    config.fetch_queue_lock = threading.Lock()

if not hasattr(config, 'fetched_bot_lists_lock'):
    config.fetched_bot_lists_lock = threading.Lock()

import list
import dcc
import db
import update_list
import announce
import irc        # The network connection to Undernet
import queue_mgr  # The flood-protection queue (round-robin)
import security   # User bans and muting
import stats_mgr  # Sizes, speed and uptime
import commands    # Every command a user can type

# Tracks unique users, for flood protection
config.send_queue = {}
bot_joined_channel = False

# Shared network reference, so threads always use the current live connection
irc_connection = None
threads_started = False

# Live traffic statistics, measured in real time by dcc.py
# current_speed_bytes was here: assigned once and never read or written
# anywhere in the repository. Live transfer speed is derived from
# active_downloads/total_sent_bytes below, which are the names dcc.py really
# updates - so anyone tracing speed through this one found nothing (#232).    
active_downloads = 0       
send_fails_count = 0       
total_sent_bytes = 0       

def queue_message(user, message, is_vip=False):
    """The queue's entry point, with a strictly isolated VIP express lane."""
    user_key = user.lower()
    import defaults as config
    
    # VIP GATE: only genuine channel adverts, or messages explicitly flagged
    # is_vip=True, are allowed through here.
    if user_key == "channel_announce" or is_vip:
        config.vip_queue.append(message)
        return
        
    import queue_mgr
    if user_key not in queue_mgr.config.send_queue:
        queue_mgr.config.send_queue[user_key] = []
    queue_mgr.config.send_queue[user_key].append(message)



def startup():
    """Everything the daemon does before it touches the network.

    Split out of __main__ so a test can execute it. This was the one path CI
    could never run: every module was imported and every unit tested, but the
    boot itself was only exercised by starting the real bot, which connects to
    Undernet and joins live channels. It is also the first thing a Windows port
    meets.

    Behaviour is unchanged, sys.exit(1) on a missing music directory included -
    called from __main__ that ends the process exactly as before, and a test can
    assert the SystemExit instead.
    """
    print(f"--- {config.SCRIPT_VERSION} is starting up ---")

    # The hard backstop for #170's RFC: scripts/setup_check.py's pre-flight
    # report is a friendlier, EARLIER warning an operator can choose to run
    # (or a launcher runs for them) - this is what actually stops the daemon
    # itself from ever booting with NICKNAME/CHANNEL/ADMIN_NICK still blank,
    # regardless of how it was started.
    import settings_file
    unconfigured = settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS)
    if unconfigured:
        print("[CRITICAL] The following required setting(s) are still unconfigured "
              "(blank, or still the shipped default):")
        for name in unconfigured:
            print(f"[CRITICAL]   {name}")
        print("[CRITICAL] Set them in admin_config.py or settings.conf before starting - "
              "see admin_config.py.sample / settings.conf.sample.")
        sys.exit(1)

    # FILE_DIRECTORY is deliberately NOT in settings_file.REQUIRED (see its
    # own comment) - a blank value means "not chosen yet", not "misconfigured",
    # and the daemon boots anyway so the dashboard's own Settings page can be
    # the place that sets it, rather than needing it typed blind before the
    # dashboard is even reachable. A value that IS set but wrong (does not
    # exist) still refuses to start - that is a real misconfiguration, not an
    # unmade choice, and is worth catching before anything tries to serve
    # from it.
    if not config.FILE_DIRECTORY:
        print("[WARNING] No music directory configured yet - the daemon will "
              "connect, but cannot search or serve anything. Set FILE_DIRECTORY "
              "from the web dashboard's Settings page, settings.conf, or "
              "admin_config.py.")
    elif not os.path.exists(config.FILE_DIRECTORY):
        print(f"[CRITICAL] Missing directory: {config.FILE_DIRECTORY}")
        sys.exit(1)

    # Before anything reads the side files: carry them across from the old
    # flac-serv-* names if this install predates the rename. A no-op on every
    # run after the first, and on any install that never had them.
    db.migrate_legacy_side_files()

    # Before find_latest_list() below: defaults.py's LIST_BASE_NAME derivation
    # (an untouched value takes NICKNAME's own value once NICKNAME is set)
    # means an existing install's list files can be sitting on disk under the
    # OLD "DCCore-*" name while LIST_BASE_NAME now resolves to something else -
    # see migrate_list_base_name()'s own docstring. A no-op on every run after
    # the first, and on any install that never had a "DCCore-*" list.
    update_list.migrate_list_base_name()

    latest_list = list.find_latest_list()
    if not latest_list:
        print("[WARNING] No file list found in lists/ yet.")
    else:
        print(f"[INFO] Loaded the latest file list: {os.path.basename(latest_list)}")

    if os.path.exists(config.BANS_FILE):
        db.load_bans_from_file()
    else:
        # Same shape as the list-file check above: say so, rather than
        # starting with an empty ban list and no way to tell that apart
        # from "every temporary ban already expired". A wrong working
        # directory (this path is relative - see the launcher scripts'
        # own comments) produces exactly this silently.
        print(f"[WARNING] No {config.BANS_FILE} yet - starting with no active bans.")

    # Read every saved queue slot back from disk at boot.
    db.load_dcc_queue()

    # Other bots we have seen advertising. Rebuilt from channel traffic anyway,
    # so this only spares the wait: without it the dashboard's bot list is empty
    # until every bot has advertised again, which on a five-minute advert cycle
    # is minutes of showing nothing. update() rather than assignment, for the
    # reason runtime.py exists - rebinding leaves config.known_bots pointing at
    # the old dict. load_known_bots() returns {} rather than raising on a file
    # it cannot read, so there is nothing here to catch.
    config.known_bots.update(db.load_known_bots())
    if config.known_bots:
        print(f"[STARTUP] Bot registry: {len(config.known_bots)} bot(s) remembered.")

    # Lists already fetched FROM other bots. The extracted files under
    # FETCHED_FILES_DIR are untouched by a restart - only the daemon's
    # in-memory map of which bots they belong to was, since
    # list_fetch.py only ever writes into it live as a fetch completes.
    # Without this, the File Lists switcher went blank on every restart
    # despite the files still being right there on disk.
    config.fetched_bot_lists.update(db.load_fetched_bot_lists())
    if config.fetched_bot_lists:
        print(f"[STARTUP] Fetched lists: {len(config.fetched_bot_lists)} bot(s) remembered.")

    # Finished cross-bot fetches (complete or failed), same restart-survival
    # reasoning as fetched_bot_lists just above: the actual files under
    # FETCHED_FILES_DIR were untouched by a restart, but the Downloads
    # table's only record of them - a row in config.fetch_queue - was
    # in-memory only until now, so a completed download and its Delete
    # button both silently vanished from the dashboard on every restart.
    config.fetch_queue.update(db.load_fetch_history())
    # #221: a bot that ran for months before retention existed loads all of it
    # back here. Pruning at startup as well as on the persist cycle means an
    # upgrade cleans up once rather than carrying the backlog forever.
    # Non-fatal, for the same reason the dispatcher start further down is:
    # retention is housekeeping, and a bot that cannot import dcc_fetch has a
    # bigger problem than an over-long Downloads list. tests/test_startup.py
    # simulates exactly that by putting None in sys.modules, and an unguarded
    # import here took the whole boot down with it.
    try:
        import dcc_fetch
        dcc_fetch.prune_fetch_history()
    except Exception as prune_err:
        print(f"[STARTUP] Could not prune the fetch history: {prune_err}")
    if config.fetch_queue:
        print(f"[STARTUP] Fetch history: {len(config.fetch_queue)} finished fetch(es) remembered.")

    # ---------------------------------------------------------------------
    # SINGLE START: the queue is started exactly ONCE here, outside every loop,
    # so boot produces exactly one [QUEUE] line.
    # ---------------------------------------------------------------------
    import queue_mgr
    print("[SYSTEM] Starting the flood-protection queue...")
    threading.Thread(target=queue_mgr.queue_worker, daemon=True).start()

    # Cross-bot file fetch storage (dcc_fetch.py). Non-fatal on purpose,
    # unlike the FILE_DIRECTORY check above: FILE_DIRECTORY is a hard
    # precondition for the daemon's core purpose (serving the library), while
    # this is a newer, optional feature - a permissions failure here logs and
    # leaves fetch_feature_disabled set rather than taking the whole daemon
    # down. dcc_fetch/webserver check that flag before accepting an offer or
    # an enqueue request.
    try:
        fetched_dir = getattr(config, "FETCHED_FILES_DIR", "./data/fetched")
        os.makedirs(fetched_dir, exist_ok=True)
        config.fetch_feature_disabled = False
    except Exception as fetch_dir_err:
        print(f"[FETCH] Could not create {fetched_dir}: {fetch_dir_err}. Cross-bot file fetch disabled.")
        config.fetch_feature_disabled = True

    try:
        import dcc_fetch
        threading.Thread(target=dcc_fetch.fetch_dispatcher_worker, daemon=True).start()
    except Exception as fetch_worker_err:
        print(f"[FETCH] Could not start fetch dispatcher: {fetch_worker_err}")

    # Optional web dashboard (mostly read-only status views, plus the
    # cross-bot search/fetch routes - see webserver.py's module docstring).
    # Lazy import (not at module top) so a missing Flask install - the normal
    # case, since it is an optional dependency CI never installs - never
    # affects anything that imports oserve.py itself; only the dashboard
    # feature is unavailable.
    try:
        import webserver
    except Exception as web_err:
        print(f"[WEBUI] Could not import webserver: {web_err}")
    else:
        # False when absent, matching what config.py ships. The dashboard is a
        # network-facing listener (login-gated, but still a surface someone
        # has to opt into), so a missing switch must not be read as consent
        # to open one - see config.WEBUI_ENABLED's own comment.
        if getattr(config, "WEBUI_ENABLED", False):
            threading.Thread(target=webserver.start, daemon=True).start()
        else:
            print("[WEBUI] Disabled via config.WEBUI_ENABLED = False.")


def run_forever():
    """The reconnect loop. Never returns; only the network lives in here.

    The global declaration is load-bearing, not decoration. These two
    assignments used to sit at MODULE level inside __main__, so they rebound
    oserve.irc_connection and oserve.bot_joined_channel - which irc.py and
    dcc.py reach through sys.modules to find the live socket. Inside a function
    without this declaration they would quietly become locals, the reconnect
    cleanup would stop happening, and nothing would say so.
    """
    global irc_connection, bot_joined_channel

    # THE RECONNECT LOOP (network only)
    while True:
        try:
            # Hand the whole network job to the IRC module
            irc.irc_loop()
        except KeyboardInterrupt:
            print("\nShutting down...")
            sys.exit(0)
        except Exception as main_err:
            print(f"[CRITICAL MAIN ERROR] The main loop stopped: {main_err}")

        # If the network dies, clear the socket cleanly before the next attempt
        irc_connection = None
        bot_joined_channel = False

        # Safety catch: if the network dies, make sure the advert knows
        import announce
        announce.is_ready = False

        print("[CONNECT] Lost the connection. Reconnecting to the IRC server in 10 seconds...")
        time.sleep(10)


if __name__ == "__main__":
    startup()
    run_forever()



