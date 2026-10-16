"""Where every setting sits in dccore.mrc's settings window (#1264).

PURE DATA. scripts/mirc/build_settings_window.py reads this file, takes the
labels, units, choices, types and help texts from the bot's own metadata
(webserver.SETTINGS_LABELS and friends), lays the controls out and writes the
dialog into a marked block of dccore.mrc. Nothing here is mIRC code and
nothing here is a position: move a setting by moving its line.

The grouping is the settings-window mockup's: six tabs, a column of page
buttons on the left of each, sections inside a page, in the mockup's order. It is NOT the
dashboard's SETTINGS_CATEGORIES order on purpose - the mockup moved the
switches used most onto General Settings, so SEARCH_ENABLED is there and not
on Search.

An item is one of:

    "KEY"                                  a setting, with its dashboard label
    {"key": "KEY", "label": "..."}         the same, with the mockup's shorter label
    {"local": "option", "label": "..."}    one of this mIRC's own switches (dccore.ini)
    {"note": "..."}                        a line or two of explanation
    {"widget": "name", "keys": [...]}      a structured control the script drives
                                           by hand; `keys` are the settings it edits

THE PLACEMENT RULE. Every key in webserver.SETTINGS_CATEGORIES is placed
exactly once - as an item or in a widget's `keys` - or listed in EXCLUDED with
the reason it is not in the window. tests/test_the_mirc_settings_window.py
fails on a key that is in neither, so a setting added to the bot cannot
silently miss the window.

Examples in notes are invented (#music, host.example): this file ships.
"""

# The keys of the File locations page: Apply asks before it saves a change to
# any of them, since a wrong path there can lose a queue or a statistics file.
CONFIRM_PAGES = ("File locations",)

GROUPS = (
    ("General", (
        ("IRC Server", (
            ("Connection", ("SERVER", "PORT", "NICKNAME", "ALT_NICKNAME")),
            ("Reconnection", ("REJOIN_ATTEMPTS", "ON_CONNECT_CHECK_MINUTES")),
            ("On connect", ({"widget": "onconnect"},)),
        )),
        ("General Settings", (
            ("", ({"note": "The on/off switches used most, here from their own pages so there is one "
                           "place to check."},)),
            ("Open when mIRC starts (minimised)", (
                {"local": "start.main", "label": "DCCore window"},
                {"local": "start.chat", "label": "Chat"},
                {"local": "start.downloads", "label": "Downloads"},
            )),
            ("Connection", (
                {"local": "auto", "label": "Reconnect and log in to the bot by itself when it comes back"},
            )),
            ("Sharing & search", ("SEARCH_ENABLED", "ANNOUNCE_TRANSFERS")),
            ("Messaging & dashboard", ("PRIVATE_MESSAGES_ENABLED", "WEBUI_ENABLED", "CTCP_VERSION_REPLY")),
            ("Updates", ("CHECK_FOR_UPDATES",)),
        )),
        ("Channels", (
            ("Admin channels", (
                {"note": "A separate channel the bot sends its own diagnostic lines to - never one it "
                         "also serves files in."},
                "DEBUG_CHANNEL",
            )),
            ("Serving channels", (
                {"note": "Which channels the bot joins and sits in. What each one serves - which list, "
                         "which folders, Normal, Silent or Request only - is set under Sharing > Lists "
                         "and Channel bindings."},
                {"widget": "channels", "keys": ["CHANNEL"]},
            )),
        )),
        ("Operator", (
            ("Admin", ("ADMIN_NICK",)),
        )),
        ("Appearance", (
            ("What the channel sees", (
                "THEME",
                {"widget": "preview"},
            )),
            ("Custom override - surface colours", (
                {"note": "Set any of these and it replaces that one role in the theme above. Keep "
                         "previous keeps whatever background the segment before left."},
                {"key": "CUSTOM_THEME_BORDER", "label": "Border colour"},
                {"key": "CUSTOM_THEME_SEPARATOR", "label": "Separator colour"},
                {"key": "CUSTOM_THEME_TEXTBOX", "label": "Text box colour"},
            )),
            ("Custom override - text colours", (
                {"key": "CUSTOM_THEME_VALUE", "label": "Value colour"},
                {"key": "CUSTOM_THEME_ALERT", "label": "Alert colour"},
                {"key": "CUSTOM_THEME_ACCENT", "label": "Accent colour"},
            )),
        )),
        ("Advertising", (
            ("Advert", ("ANNOUNCE_INTERVAL",)),
            ("Broadcast search", ("BROADCAST_SEARCH_CHANNEL", "BROADCAST_SEARCH_COOLDOWN")),
            ("Timing", ("MSG_DELAY", "DEBUG_MSG_DELAY")),
        )),
    )),
    ("Sharing", (
        ("Lists", (
            ("", (
                {"note": "What each list shares: a name, its folders, and whether it is primary (it "
                         "answers a private message, and any channel bound to none). Add list and Edit "
                         "open a window for one list; Apply sends the whole set to the bot."},
            )),
            ("Lists", ({"widget": "srvlists"},)),
        )),
        ("Channel bindings", (
            ("", (
                {"note": "Which list answers in each channel, and that channel's mode. A change in a "
                         "menu is kept at once; Apply sends the set to the bot."},
            )),
            ("Channel bindings", ({"widget": "srvchans"},)),
            ("", ({"widget": "srvsaveb"},)),
        )),
        ("List settings", (
            ("Library", ("LIST_BASE_NAME", "LIST_FORMAT", "LIST_IGNORED_EXTENSIONS")),
            ("Film & series", ({"key": "SEPARATE_VIDEO_LIST", "label": "Separate film and series list"},
                               "LIST_VIDEO_EXTENSIONS", "LIST_VIDEO_COMPANION_EXTENSIONS")),
            ("!rar packing", ("RAR_ENABLED", "RAR_EXTENSIONS", "RAR_BINARY", "MAX_RAR_FOLDER_SIZE", "RAR_TIMEOUT")),
            ("List banner", ("LIST_HEADER_FILE", "LIST_HEADER_MAX_BYTES", "LIST_SHOW_AUDIO_INFO")),
        )),
        ("Rebuild", (
            ("Automatic rebuild", ("LIST_REBUILD_SCHEDULE", "LIST_UPDATE_TIMEOUT",
                                   "LIST_UPDATE_STALL_SECONDS")),
            ("Performance", ("LIST_AUDIO_INFO_MINUTES", "LIST_AUDIO_INFO_THREADS", "LIST_SCAN_THREADS")),
        )),
        ("Transfers", (
            ("Speed limits", (
                {"note": "Upload is your sends to people; download is everything the bot fetches. "
                         "Each is one limit shared by all transfers that way."},
                "MAX_UPLOAD_KB", "MAX_DOWNLOAD_KB",
            )),
            ("Buffers & packet size", ("DCC_BLOCK_SIZE", "DCC_SEND_BUFFER")),
            ("Ports & reliability", ("DCC_PORT_START", "DCC_PORT_END", "DCC_ACCEPT_TIMEOUT",
                                     "MAX_SEND_FAILS", "DAILY_SEND_FAILS_LIMIT")),
        )),
        ("Search", (
            ("Sending", ("MAX_DCC_SLOTS",
                         {"key": "LARGE_FILE_MB", "label": "One large file from (MB, 0 = off)"},
                         "SLOW_SEND_MIN_KB", "MAX_USER_QUEUE", "MAX_GLOBAL_QUEUE")),
            ("Search (@find)", (
                {"note": "How much the bot says back once it answers (the on/off switch is under "
                         "General > General Settings)."},
                "MAX_SEARCH_RESULTS", "SEARCH_SHOW_FOLDER", "SEARCH_FOLDER_MAX_CHARS",
            )),
            ("During a rebuild", ("PAUSE_ON_UPDATE", "PAUSE_FOR_WHOLE_UPDATE", "REHASH_TRANSFER_WAIT")),
        )),
    )),
    ("Downloads", (
        ("List discovery", (
            ("Auto-grab", ("AUTO_GRAB_LISTS", "AUTO_GRAB_EVERY_MINUTES", "AUTO_GRAB_MIN_FILES",
                           "AUTO_GRAB_MIN_SPEED_KB")),
            ("Discover lists", ("AUTO_DISCOVER_CHANNEL_LISTS", "MULTI_CHANNEL_LIST_STABLE_SECONDS")),
        )),
        ("Fetch tuning", (
            ("Slots & re-fetch", ("MAX_FETCH_SLOTS",
                                  {"key": "AUTO_REFETCH_LISTS", "label": "Re-fetch when a bot has a new list"},
                                  "AUTO_REFETCH_INTERVAL_HOURS", "AUTO_REFETCH_MAX_PER_RUN")),
            ("Timeouts", ("FETCH_OFFER_TIMEOUT", "FETCH_TRANSFER_TIMEOUT", "FETCH_FOLDER_OFFER_TIMEOUT",
                          "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED", "FETCH_FOLDER_TRANSFER_TIMEOUT")),
            ("Size limits", ("MAX_FETCH_FILE_SIZE", "MAX_FETCH_FOLDER_FILE_SIZE", "MAX_FETCH_LIST_FILE_SIZE",
                             "MAX_LIST_TEXT_SIZE")),
            ("History", ("FETCH_HISTORY_DAYS", "FETCH_HISTORY_MAX_ROWS")),
        )),
        ("Queue", (
            ("Fetch queue", ("FETCH_MAX_PER_BOT", "FETCH_QUEUED_TIMEOUT", "FETCH_BOT_MAX_FAILS",
                             "FETCH_BOT_COOLDOWN_MINUTES")),
            ("List requests", ("LIST_REQUEST_TAG",)),
        )),
    )),
    ("Security", (
        ("Anti-flood", (
            ("Flood protection", ("MAX_REQUESTS", "REQUEST_WINDOW", "MUTE_TIME", "FLOOD_BAN_SECONDS")),
        )),
        ("Bans & ignores", (
            ("Temporary (by nick)", (
                {"note": "Flood protection bans a nick by itself; Ignore is the same timed block, "
                         "started by hand."},
                {"widget": "bantimed"},
            )),
            ("Permanent (by hostmask pattern)", (
                {"note": "Wildcard patterns such as *!*@host.example - the same as ban and unban in "
                         "the console."},
                {"widget": "banperm"},
            )),
        )),
        ("Private messages", (
            ("Auto-reply", ("PRIVATE_MESSAGE_COOLDOWN_SECONDS", "PRIVATE_MESSAGE_DECLINE_TEXT",
                            "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS")),
            ("Rate limit", ("PRIVATE_MESSAGE_DECLINE_BURST", "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS")),
        )),
        ("Admin console", (
            ("Access", ("ADMIN_HOSTMASKS", "ADMIN_CHAT_MODE")),
            ("Behaviour", ("ADMIN_CHANNEL_COMMANDS", "ADMIN_CHAT_COLOURS")),
        )),
    )),
    ("Dashboard & Console", (
        ("Web dashboard", (
            ("Server", ("WEBUI_HOST", "WEBUI_PORT", "WEBUI_ALLOWED_HOSTS")),
            ("Console & browser", ("WEBUI_CONSOLE_ENABLED", "WEBUI_OPEN_BROWSER",
                                   "WEBUI_FOLDER_BROWSER_ENABLED")),
        )),
        ("Console feed", (
            ("What shows in @DCCore", ("CONSOLE_SHOW_REQUESTS", "CONSOLE_SHOW_QUEUE", "CONSOLE_SHOW_SENDS",
                                       "CONSOLE_SHOW_FAILURES", "CONSOLE_SHOW_SEARCHES")),
            ("Debug channel", ("DEBUG_CHANNEL_FEED",)),
        )),
    )),
    ("Advanced", (
        ("Debug & logging", (
            ("Debug", ("DEBUG_MODE", "DEBUG_TO_CHANNEL", "DEBUG_TO_CONSOLE")),
            ("Logging", ("CONSOLE_TIMESTAMP_FORMAT", "CONSOLE_LOG_FILE", "CONSOLE_LOG_MAX_MB",
                         "CONSOLE_LOG_KEEP")),
            ("Window & project", ("BOT_WINDOW", "PROJECT_URL")),
        )),
        ("File locations", (
            ("Library & lists", ("TMP_ZIP_DIR", "LOCAL_LIST_DIR", "FETCHED_FILES_DIR", "LIBRARY_FOLDERS_FILE",
                                 "LISTS_FILE")),
            ("Security", ("BANS_FILE", "HARD_BANS_FILE")),
            ("Stats & bots", ("STATS_FILE", "KNOWN_BOTS_FILE", "FETCHED_BOT_LISTS_FILE")),
            ("Search & audio", ("LIST_INDEX_FILE", "LIST_AUDIO_INFO_CACHE")),
            ("Fetch history", ("FETCH_HISTORY_FILE", "DOWNLOAD_COUNTS_FILE", "TRANSFER_LOG_FILE")),
            ("List build", ("LIST_SIZE_FILE", "LIST_RAWBYTES_FILE", "LIST_PROGRESS_FILE")),
            ("Console & messaging", ("ADMIN_TOKENS_FILE", "ON_CONNECT_FILE", "NOTICES_FILE",
                                     "PRIVATE_MESSAGES_FILE", "DCC_QUEUE_FILE")),
        )),
    )),
    # This mIRC's own settings (dccore.ini), in the same window and form as
    # the bot's: what was the Options dialog. The locals are saved by Apply
    # like every other; the widgets by dccore.sw.opt.save. The last group:
    # with DCX it is the window's mIRC side, entered from the start page,
    # with no tab strip - its pages are the list.
    ("mIRC", (
        ("@DCCore window", (
            ("Show in @DCCore", ({"widget": "showin"},)),
            ("Colours", (
                {"note": "Search text is the term searched for. Each of the three can be the same as "
                         "its line; Search text can be the same as File names, and Nicks can be per "
                         "nick, a colour of its own from the name."},
                {"widget": "showcolours"},
            )),
        )),
        ("Window", (
            ("Window", (
                {"local": "panel", "label": "Side panel: slots, queue and totals"},
                {"local": "titlebar", "label": "Slots, queue and speed in the title bar"},
                {"local": "separate", "label": "Console replies in a separate window"},
                {"local": "beep", "label": "Beep on a failed transfer"},
                {"widget": "winlook"},
            )),
            ("Theme", (
                {"note": "Dark colours for the script's windows - the control panel, the list window, the "
                         "download queues - and an icon beside each of the script's menu items and pages. "
                         "Follow mIRC takes mIRC's own dark mode (7.79 and later). The control panel takes a "
                         "change the next time it opens; /dccore theme says what applies and why."},
                {"widget": "theme"},
            )),
            ("Settings window", (
                {"note": "Drawn with DCX (scripts\\mirc\\dcx) when this mIRC can load it, with mIRC's own "
                         "controls when not - /dccore dcx says which and why. Takes effect the next time "
                         "the window opens."},
                {"local": "nodcx", "label": "Use the built-in settings window, not DCX"},
            )),
        )),
        ("Connection", (
            ("Bot", (
                {"note": "The bot this mIRC is paired with. Reconnecting and logging in by itself when it "
                         "comes back is under General > General Settings."},
                {"widget": "botlink"},
            )),
        )),
        ("Chat", (
            ("DCCore Chat (public)", (
                {"local": "chat.all", "label": "Listen on every channel the bot is in, not only the ticked ones"},
                {"local": "chat.popup", "label": "Open the chat window when a line arrives"},
            )),
            # The bot's settings (#1401), here because they limit files on this
            # PC: the bot sends them to the script as DCCORE CHATHIST.
            ("History", (
                {"note": "Whole months of the history file are deleted: those that ended longer ago than "
                         "the days, then the oldest while the folder is over the size. 0 is no limit."},
                {"key": "CHAT_HISTORY_DAYS", "label": "Keep for (days)"},
                {"key": "CHAT_HISTORY_MAX_MB", "label": "At most (MB)"},
            )),
        )),
    )),
)

# Settings the window does not show, and why. Every other key of
# webserver.SETTINGS_CATEGORIES has a place above.
EXCLUDED = {
    "ADMIN_PASSWORD_HASH": "never sent over the console (`settings` leaves it out and `set` refuses it): "
                           "the password is changed on the dashboard's Settings page or with "
                           "python src/adminchat.py",
}

# The settings whose value is a path on the BOT's machine, and whether it is a
# folder or a file: they get a "..." browse button, enabled only when the
# console connection is local (the bot runs on this PC).
FOLDER_KEYS = ("TMP_ZIP_DIR", "LOCAL_LIST_DIR", "FETCHED_FILES_DIR")
FILE_KEYS = ("RAR_BINARY", "LIST_HEADER_FILE", "CONSOLE_LOG_FILE", "LIBRARY_FOLDERS_FILE", "LISTS_FILE",
             "BANS_FILE", "HARD_BANS_FILE", "STATS_FILE", "KNOWN_BOTS_FILE", "FETCHED_BOT_LISTS_FILE",
             "LIST_INDEX_FILE", "LIST_AUDIO_INFO_CACHE", "FETCH_HISTORY_FILE", "DOWNLOAD_COUNTS_FILE",
             "TRANSFER_LOG_FILE", "LIST_SIZE_FILE", "LIST_RAWBYTES_FILE", "LIST_PROGRESS_FILE",
             "ADMIN_TOKENS_FILE", "ON_CONNECT_FILE", "NOTICES_FILE", "PRIVATE_MESSAGES_FILE",
             "DCC_QUEUE_FILE")
