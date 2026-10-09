# oserve.py - The central hub that wires every module together
import threading
import time
import sys
import os

# Every other daemon module lives in src/ (#959): put it on the path before
# any of them is imported, so every existing "import irc" / "import defaults
# as config" throughout the codebase keeps working unchanged - the modules
# still only ever refer to each other by bare name, never a package prefix.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# FIRST, before anything else can print. Several modules print at import time,
# and on a console whose code page cannot encode the Swedish log strings
# (cp1253, cp1251, cp932, ascii - anything but Western European) an unguarded
# print() raises UnicodeEncodeError and takes the thread down with it. See
# platform_compat.install_console_encoding_guard for the full explanation.
import platform_compat
# NO WINDOW, NO STREAMS (#1065). Started with pythonw - BOT_WINDOW = hidden -
# there is no console, and sys.stdout and sys.stderr are None: print() quietly
# does nothing, but sys.stdout.write() raises, and the dashboard's Flask writes
# that way. A real null file stands in, so every write works; what is written
# still reaches the log file, through the timestamp wrapper installed on it.
WINDOWLESS = __name__ == "__main__" and (sys.stdout is None or sys.stderr is None)
if WINDOWLESS:
    _no_console = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _no_console
    if sys.stderr is None:
        sys.stderr = _no_console
# ONLY WHEN THIS FILE IS THE PROGRAM (#707, audit L43). list.py imports
# oserve, so every test process - and every script that imports announce -
# used to run these two installs at import time and wrap the runner's own
# stdout and stderr for the rest of the run: unittest's summaries came out
# timestamped, and a test asserting an exact printed line saw a prefix that
# depended on which module was imported first. `__name__` is "__main__"
# here, at the top of the file, exactly when `python oserve.py` is what is
# running - so the daemon's first lines are still stamped and guarded, and
# an import of this module touches nothing.
if __name__ == "__main__":
    platform_compat.install_console_encoding_guard()
    # Timestamps go on at the same moment, with the built-in format, so the
    # config-loading lines that print next are stamped too. The operator's
    # own format is applied the line after config exists.
    platform_compat.install_console_timestamps()

# Load the bot's modules
import defaults as config
if __name__ == "__main__":
    platform_compat.set_console_timestamp_format(
        getattr(config, "CONSOLE_TIMESTAMP_FORMAT", "%H:%M:%S"))

    # `oserve.py --stop` (#1065): ask the bot running from this folder to stop,
    # wait until it has, and exit - before anything below starts, and before
    # the log file is opened, since this process is not the bot.
    if "--stop" in sys.argv[1:]:
        import stopping
        sys.exit(stopping.stop_from_outside())
    # `oserve.py --running`: exit 0 when a bot holds this folder, 1 when not.
    # start-dccore.bat asks before starting one with no window or a minimised
    # one, since it is not there afterwards to read the "already running" exit.
    if "--running" in sys.argv[1:]:
        import stopping
        sys.exit(0 if stopping.running_pid()[0] else 1)

    # And to a file (#1065), from here on. Read on every line through
    # sys.modules: a settings save reloads defaults, and a changed or emptied
    # path then takes effect without a restart.
    def _console_log_settings():
        current = sys.modules.get("defaults") or config
        try:
            megabytes = max(0, int(getattr(current, "CONSOLE_LOG_MAX_MB", 5)))
        except (TypeError, ValueError):
            megabytes = 5
        try:
            keep = max(1, int(getattr(current, "CONSOLE_LOG_KEEP", 5)))
        except (TypeError, ValueError):
            keep = 5
        path = str(getattr(current, "CONSOLE_LOG_FILE", "") or "").strip()
        if not path and WINDOWLESS:
            # With no window the file is the only place anything is said: a
            # windowless bot keeps the default log even when it is turned off.
            path = os.path.join("data", "logs", "dccore.log")
        return path, megabytes * 1024 * 1024, keep

    platform_compat.install_console_log(_console_log_settings)

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

# The exit code of a first run whose setup page could not finish (#617): the
# port was taken - another DCCore in a minimised window, another program - or
# Ctrl-C in the wait. Distinct from the 1 of every other refusal so that the
# launchers can tell "ask the questions in the terminal instead" from "stop",
# which is the one road out of a tree that has no config and a taken port.
EXIT_SETUP_IN_THE_TERMINAL = 3
# Another DCCore already holds this data folder (#710). Its own number, so
# a launcher can say "already running" rather than "failed".
EXIT_ALREADY_RUNNING = 4

# Pass as is_vip to put a line in the lane that is sent before everything else:
# the requests for files from other bots (#1028). The advert alone is two lines
# for every channel, a couple of minutes of the pacer on a bot in many of them,
# and a request behind it sat that long while someone waited for a download.
FETCH_LANE = "fetch"


def queue_message(user, message, is_vip=False):
    """The queue's entry point, with a strictly isolated VIP express lane."""
    user_key = user.lower()
    import defaults as config
    
    # VIP GATE: only genuine channel adverts, or messages explicitly flagged
    # is_vip=True, are allowed through here.
    if is_vip == FETCH_LANE:
        config.fetch_request_queue.append(message)
        return
    if user_key == "channel_announce" or is_vip:
        config.vip_queue.append(message)
        return
        
    import queue_mgr
    import runtime
    # One step, under the pump's lock (#665): the pump drops an emptied
    # user's key, and a create-then-append that straddled that lost the
    # line or raised KeyError here.
    with runtime.send_queue_lock:
        queue_mgr.config.send_queue.setdefault(user_key, []).append(message)



def startup(setup_page=None):
    """Everything the daemon does before it touches the network.

    `setup_page`: what to do when nothing is configured yet (#547, Proposal
    4). None means the default - webserver.run_setup_until_configured when
    Flask is there, which serves one page on 127.0.0.1 until the operator
    has filled the form, then returns here to carry on; False means never
    serve it, exit 1 as before (the tests of that refusal pass this; a
    machine without Flask gets the same). A callable is a stand-in for the
    page. Either way this function stays one straight line: the page is
    a blocking call at the top, not a second phase.

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

    # ONE INSTANCE PER DATA FOLDER, before anything is read or written
    # (#710). The lock lives beside the queue file, so two trees with two
    # data folders are two bots, as they should be, and two starts of one
    # tree are refused. Held until the process ends.
    lock_path = os.path.join(os.path.dirname(os.path.abspath(config.DCC_QUEUE_FILE)), "dccore.lock")
    try:
        platform_compat.take_instance_lock(lock_path)
    except platform_compat.AlreadyRunning as running:
        who = f" (pid {running.pid})" if running.pid else ""
        print(f"[CRITICAL] DCCore is already running on this folder{who} - "
              f"a second copy would share its queue, its stats file and its DCC ports.")
        print("[CRITICAL] Stop the other one first (its own window, or the autostart task), "
              "or run a second bot from a second folder.")
        sys.exit(EXIT_ALREADY_RUNNING)

    # A stop file left from before (#1065) asked an earlier bot to stop, not
    # this one: gone before the watcher could read it.
    import stopping
    stopping.clear_stale_stop_file()

    # A background audio reading the bot's last run started (#1182) outlives a
    # bot that ended without its shutdown - its window closed, a kill, a
    # crash. Asked to stop now, it saves what it read.
    try:
        import commands as _commands_orphan
        _commands_orphan.stop_orphaned_reading()
    except Exception as orphan_err:
        print(f"[AUDIO-INFO] Could not look for a reading left running: {orphan_err}")

    # The hard backstop for #170's RFC: scripts/setup_check.py's pre-flight
    # report is a friendlier, EARLIER warning an operator can choose to run
    # (or a launcher runs for them) - this is what actually stops the daemon
    # itself from ever booting with NICKNAME/CHANNEL/ADMIN_NICK still blank,
    # regardless of how it was started.
    import settings_file
    unconfigured = settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS)
    if unconfigured and setup_page is not False:
        # SET IT UP IN THE BROWSER (#547, Proposal 4). A blank config on a
        # machine with Flask is a first run, not a mistake: serve the setup
        # page until the form has written settings.conf and admin_config.py,
        # then re-ask the same question with the settings it wrote applied.
        serve = setup_page
        if serve is None:
            try:
                import webserver
                serve = (webserver.run_setup_until_configured
                         if webserver.setup_page_is_possible() else None)
            except Exception as web_err:  # a broken Flask install is not fatal
                print(f"[SETUP] The setup page is not available ({web_err}).")
                serve = None
        if serve is not None:
            print("[SETUP] Nothing is configured yet - "
                  + ", ".join(sorted(unconfigured)) + " - opening the setup page.")
            serve()
            unconfigured = settings_file.unconfigured_required(vars(config), config.SHIPPED_DEFAULTS)
            if unconfigured:
                # The page was tried and could not finish - a taken port, or
                # Ctrl-C. This is a first run, so "copy the sample" is the
                # step the launchers exist to spare a first-timer, and every
                # run took the same road (#617): name the terminal questions,
                # and exit with the code the launchers map to asking them.
                print("[CRITICAL] The setup page could not finish, so nothing is configured "
                      "yet: " + ", ".join(sorted(unconfigured)) + ".")
                print("[CRITICAL] Answer the questions in the terminal instead: "
                      "python3 configure.py (the launcher does this itself now).")
                sys.exit(EXIT_SETUP_IN_THE_TERMINAL)
    if unconfigured:
        print("[CRITICAL] The following required setting(s) are still unconfigured "
              "(blank, or still the shipped default):")
        for name in unconfigured:
            print(f"[CRITICAL]   {name}")
        # The launcher or configure.py, not "see the sample" (#685): copying
        # the sample by hand is the step both exist to spare a first-timer.
        print("[CRITICAL] Run the launcher (start-dccore - it asks the questions, or opens "
              "the setup page in your browser), or python3 configure.py, or set them in "
              "settings.conf or admin_config.py by hand before starting.")
        sys.exit(1)

    # FILE_DIRECTORY is deliberately NOT in settings_file.REQUIRED (see its
    # own comment) - a blank value means "not chosen yet", not "misconfigured",
    # and the daemon boots anyway so the dashboard's own Settings page can be
    # the place that sets it, rather than needing it typed blind before the
    # dashboard is even reachable. A value that IS set but wrong (does not
    # exist) still refuses to start - that is a real misconfiguration, not an
    # unmade choice, and is worth catching before anything tries to serve
    # from it.
    # ASKED OF THE LIBRARY, not of FILE_DIRECTORY. That setting is the
    # fallback for an install with no folder list, and this check treated it as
    # the only truth - so an operator who had configured folders (or, since
    # #26, lists) and left it blank was told at every start that the daemon
    # "cannot search or serve anything", which was simply untrue.
    #
    # Worse the other way round: a STALE FILE_DIRECTORY pointing at a drive
    # that is no longer there made the daemon refuse to boot, while the folders
    # it actually serves from sat there perfectly readable. A setting nothing
    # reads any more was able to stop the bot starting.
    import library

    configured = library.folders()
    if not configured:
        print("[WARNING] No music directory configured yet - the daemon will "
              "connect, but cannot search or serve anything. Set it from the "
              "web dashboard's Settings page, settings.conf, or "
              "admin_config.py.")
    elif not any(os.path.exists(folder.path) for folder in configured):
        # EVERY one missing, not any one. A single unavailable folder is a
        # scan-time condition the build already skips with a warning; only a
        # library with nothing readable at all is a real misconfiguration, and
        # that is what is worth refusing to start for. Same rule update_list's
        # own entry point applies.
        print("[CRITICAL] None of the configured music folders exist: "
              + ", ".join(folder.path for folder in configured))
        sys.exit(1)

    # The side-file migration that used to run here was removed before the
    # public release: it renamed two files whose old name was one operator's
    # own, and every install that could have had them had already run it.
    #
    # Before anything serves: the counters are read
    # by the dashboard and -stats, and both would show a half-migrated table.
    db.migrate_download_counts_to_labels()
    # And drop what the master list left in those counters before it stopped
    # being counted - one row per rebuild, sitting at the top of a table meant
    # for files. Runs every boot rather than once: it is a no-op the moment
    # there is nothing to remove, and an operator restoring an old data
    # directory should not get the rows back for good. Both this and the
    # migration above work on download_counts.db, and whichever reaches it
    # first imports an older download_counts.json into it (#1133), so the
    # import comes first and these two see every row it brought.
    db.prune_list_artifact_download_counts()

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

        # And index any of them the search index has never seen. Only a fetch
        # writes that index, while these lists survive restarts - so an
        # operator upgrading with lists already held had a full map and an
        # empty index, and the dashboard's filter stated positively that no
        # list matched anything. Once per start, and only for what is
        # missing; a list already indexed costs one quick question to the
        # index (#1071 - it used to read the whole index to find out). The
        # one start that pays in full is the first after an upgrade that
        # rebuilt the index for its prefix index and folder ids (#1130,
        # #1135): every held list is indexed again here, once.
        try:
            import list_index
            list_index.backfill_missing(config.fetched_bot_lists)
        except Exception as err:
            print(f"[STARTUP] Could not check the search index ({err}); the "
                  f"dashboard's cross-list filter may be incomplete until the "
                  f"next fetch.")

    # Finished cross-bot fetches (complete or failed), same restart-survival
    # reasoning as fetched_bot_lists just above: the actual files under
    # FETCHED_FILES_DIR were untouched by a restart, but the Downloads
    # table's only record of them - a row in config.fetch_queue - was
    # in-memory only until now, so a completed download and its Delete
    # button both silently vanished from the dashboard on every restart.
    config.fetch_queue.update(db.load_fetch_history())
    # And the bots whose fetching is paused (#926), kept beside it.
    try:
        import dcc_fetch
        dcc_fetch.load_paused_bots()
    except Exception as paused_err:
        print(f"[FETCH] Could not read the paused bots: {paused_err}")
    # The notices survive a restart, which is the whole point of them: an
    # event worth a badge is by definition one that happened while nobody was
    # looking, and a kick at three in the morning that is gone by nine is a
    # badge that never did its job.
    #
    # extend/update rather than assignment, like every other container loaded
    # here - config.notices IS runtime.notices, and rebinding the name would
    # leave the daemon writing into a list the dashboard cannot see.
    try:
        _notices, _notice_state = db.load_notices()
        config.notices.extend(_notices)
        config.notice_state.update(_notice_state)
        if _notices:
            print(f"[STARTUP] Notices: {len(_notices)} kept, "
                  f"{announce.unread_notices()[0]} unread.")
        _pms, _pm_state = db.load_private_messages()
        config.private_messages.extend(_pms)
        config.private_message_state.update(_pm_state)
        if _pms and getattr(config, "PRIVATE_MESSAGES_ENABLED", True):
            print(f"[STARTUP] Private messages: {len(_pms)} kept, "
                  f"{announce.unread_private_messages()} unread.")
        # Loaded either way - who has already been told is only useful across
        # a restart - but said out loud only when it is about to matter.
        if not getattr(config, "PRIVATE_MESSAGES_ENABLED", True):
            if not str(getattr(config, "ADMIN_NICK", "") or "").strip():
                print("[STARTUP] Private messages are off and ADMIN_NICK is "
                      "not set, so anyone who messages this bot will be sent "
                      "to \"the bot's owner\" by name. Set ADMIN_NICK to "
                      "point them at you.")
    except Exception as notices_err:
        # A panel that cannot be restored is a panel; the bot still serves
        # files. Nothing here is worth refusing to boot over.
        print(f"[STARTUP] Could not restore the notices: {notices_err}")
    # A hostmask that admits the network's whole logged-in population is
    # accepted - "*.example.org" is legitimate - but said out loud (#669).
    try:
        import adminchat as _adminchat_boot
        _adminchat_boot.report_broad_host_patterns()
    except Exception as hostmask_err:
        print(f"[STARTUP] Could not check ADMIN_HOSTMASKS: {hostmask_err}")
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
    # Temp files a killed run left in data/ (#692). Housekeeping, like the
    # pruning above, and no reason to refuse to boot.
    try:
        db.discard_stale_swaps()
    except Exception as swap_err:
        print(f"[STARTUP] Could not sweep leftover temp files: {swap_err}")
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

    # Only when it is switched on: a thread that would sleep for an hour and
    # then find the feature disabled is a thread nobody needs. The same call
    # runs again after every rehash (#625), so switching it on live starts
    # the worker then - once, guarded in runtime.py - and no restart is
    # needed.
    try:
        import list_fetch
        list_fetch.ensure_auto_refetch_worker()
    except Exception as refetch_err:
        print(f"[LIST-FETCH] Could not start the automatic refresh: "
              f"{refetch_err}")
    # Automatic list grabbing (#926), the same way.
    try:
        import list_grab
        list_grab.ensure_worker()
    except Exception as grab_err:
        print(f"[LIST-GRAB] Could not start automatic list grabbing: {grab_err}")
    # Automatic discovery of a bot's other channel-bound lists (#1240).
    try:
        import list_grab as _list_grab_secondary
        _list_grab_secondary.ensure_secondary_channel_worker()
    except Exception as secondary_err:
        print(f"[LIST-GRAB] Could not start automatic channel-list discovery: {secondary_err}")

    # The list rebuild schedule (#776): started the same way, also re-armed by
    # every rehash, so setting it on the dashboard needs no restart.
    try:
        import commands
        commands.ensure_rebuild_schedule_worker()
    except Exception as schedule_err:
        print(f"[SCHEDULE] Could not start the rebuild schedule: {schedule_err}")

    # The daily version check (#572). Said at EVERY start while it is on, so an
    # install that upgraded into it - where nobody ticked a box - is told, and
    # told how to turn it off. Started once, guarded in runtime.py; a rehash
    # starts it too, when the setting is ticked on later.
    try:
        import version_check
        if getattr(config, "CHECK_FOR_UPDATES", True):
            print("[UPDATE] Checking once a day for a new version of DCCore (one request to "
                  "GitHub; nothing about this bot is sent). CHECK_FOR_UPDATES = false, or "
                  "the Settings page, turns it off.")
        version_check.ensure_worker()
    except Exception as update_err:
        print(f"[UPDATE] Could not start the version check: {update_err}")

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
            _shut_down()
        except Exception as main_err:
            print(f"[CRITICAL MAIN ERROR] The main loop stopped: {main_err}")

        # If the network dies, clear the socket cleanly before the next attempt
        irc_connection = None
        bot_joined_channel = False

        # Safety catch: if the network dies, make sure the advert knows
        import announce
        announce.is_ready = False

        print("[CONNECT] Lost the connection. Reconnecting to the IRC server in 10 seconds...")
        # Inside a try of its own (#1065 review): a stop landing in these ten
        # seconds escaped as a traceback and skipped the flush below.
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            _shut_down()


def _shut_down():
    """Ctrl-C, and every other way to stop (#1065): flush, then exit 0.

    A second interrupt while this runs - Ctrl-C pressed twice, a stop from the
    dashboard and from `start-dccore stop` together - is swallowed rather than
    escaping as a traceback that skips the flush and exits non-zero, which
    launchd restarts (#1065 review). Not by ignoring SIGINT: a test drives
    run_forever() in-process and its runner must keep Ctrl-C."""
    try:
        print("\nShutting down...")
        # The stop file is the request just answered (#1065); a copy left
        # behind would stop the next start too.
        try:
            import stopping
            stopping.clear_stale_stop_file()
        except Exception:
            pass
        # One last flush of the bot registry (#691): it is written on a
        # 30 s interval, and a Ctrl-C inside that window lost the last
        # adverts and a source the dashboard had just added. Never
        # fatal on the way out.
        try:
            import irc as _irc_flush
            _irc_flush._flush_known_bots(force=True)
        except Exception:
            pass
        # A background audio reading (#1182) is the rebuild's own process
        # and would outlive the bot: asked to stop, it saves what it read.
        try:
            import commands as _commands_stop
            _commands_stop.stop_audio_reading(wait=10.0)
        except Exception:
            pass
        # Every send and the pack still running end with the bot (#1203):
        # each goes into the transfer record as cancelled, last, so one
        # that finished while the steps above ran is recorded as it ended.
        try:
            import dcc as _dcc_stop
            _dcc_stop.record_transfers_cut_off()
        except Exception:
            pass
    except KeyboardInterrupt:
        pass   # asked twice: still stopping, and still exit 0
    sys.exit(0)


if __name__ == "__main__":
    startup()
    # The other ways to stop (#1065) all end in run_forever()'s Ctrl-C
    # KeyboardInterrupt: the watcher for `start-dccore stop` starts here, in
    # the program only - a test that drives run_forever() in-process must
    # never have its own runner interrupted by it.
    import stopping
    stopping.restore_interrupt()
    stopping.ensure_watcher()
    run_forever()



