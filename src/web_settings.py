"""The dashboard's Settings page (#1320): which settings it shows and how,
saving them and the rehash that applies them, the theme preview, the
password change, and whether the dashboard and its console are reachable
from anywhere but this machine.

Split out of webserver.py, step 5 of splitting it (docs/FUTURE.md). The
routes that call this are still there, and so is console_settings'
`webserver.apply_settings_changes()`: webserver imports every name back.
"""

import threading

import adminchat
import defaults as config
import runtime
from web_common import WEB_DASHBOARD_SOURCE, json_object


# ==========================================================================
# Settings (mutating - behind the same login as every other route). Pure
# logic here, same reasoning as every other build_*_payload()/apply_*()
# function in this module: no Flask import, fully unit testable.
# ==========================================================================

# Hand-written UX grouping of every setting config.py annotates (see
# declared_types() below) except ADMIN_PASSWORD_HASH, which never appears as
# a field - only the "admin_password_set" boolean does (see
# build_settings_payload()). Every name here is checked against config.py's
# actual annotations by SettingsPayloadTests' completeness guard, so a
# setting added to config.py later and never slotted in here fails a test
# instead of silently never showing up on the page.
# REGROUPED, and the old grouping is worth recording because it is how any
# grouping ends up: by accretion. Each feature put its settings wherever there
# was room, so "Slots & queue" had grown to 25 and "Paths & storage" to 31 -
# between them two thirds of the page - while the two settings that tune a
# transfer sat in DIFFERENT categories.
#
# From the operator: "Settings pages at webpage are a mess. Need better
# grouping, hiding some that are never used like folder locations under
# advanced settings etc. Also why is packet size on different page than buffer
# size."
#
# THREE RULES, applied here and worth keeping:
#
#   * Settings that are read together live together. DCC_BLOCK_SIZE and
#     DCC_SEND_BUFFER are the transfer-tuning pair - the 3 MB/s investigation
#     needed both - and were a category apart.
#   * A category is named for what an operator came looking for, not for the
#     part of the code it configures. The colour theme was under "Advertising
#     & search" because announce.py draws it.
#   * The things set once at install and never again go last, in their own
#     section. Seventeen file paths at the same level as MAX_DCC_SLOTS is
#     seventeen chances to wonder whether you should be changing one.
#
# NOTHING MOVES ON DISK. settings.conf is flat; a category is a grouping for
# this page and nothing else, so this reorders what an operator reads without
# touching a single stored value.
#
# Every name here is checked against config.py's actual annotations by
# SettingsPayloadTests' completeness guard, so a setting added later and never
# slotted in fails a test instead of silently never showing up.
#
# Settings an editor of their own replaces on this page (#1357), and so on no
# category. FILE_DIRECTORY is the folder a bot with no folder list serves.
# Your list's folder rows show it as their first row, and saving them writes
# the list, so a field for it as well put one folder on the page twice - and
# the field's "used only when no folders are set" was a second way to say
# what is shared that the rows silently overrode. It stays a setting: the
# first-time setup writes it, and a bot that never saved a folder list still
# serves it.
EDITED_ELSEWHERE = {
    "FILE_DIRECTORY": "Your list's folder rows",
}

SETTINGS_CATEGORIES = (
    ("identity",      "Identity & network",    ["SERVER", "PORT", "NICKNAME", "ALT_NICKNAME", "REJOIN_ATTEMPTS",
                                                "ON_CONNECT_CHECK_MINUTES",
                                                "ADMIN_NICK", "CHANNEL", "DEBUG_CHANNEL",
                                                "CHECK_FOR_UPDATES"]),
    ("sharing",       "Sharing & queue",       ["MAX_DCC_SLOTS", "LARGE_FILE_MB", "SLOW_SEND_MIN_KB", "MAX_USER_QUEUE",
                                                "MAX_GLOBAL_QUEUE", "SEARCH_ENABLED", "MAX_SEARCH_RESULTS",
                                                "SEARCH_SHOW_FOLDER", "SEARCH_FOLDER_MAX_CHARS",
                                                "PAUSE_ON_UPDATE", "PAUSE_FOR_WHOLE_UPDATE",
                                                "REHASH_TRANSFER_WAIT"]),
    # The speed caps first (#1352), the setting most operators come here for;
    # then the transfer-tuning pair, together. Anyone reaching for one wants
    # the other in front of them.
    ("transfers",    "Transfers",             ["MAX_UPLOAD_KB", "MAX_DOWNLOAD_KB",
                                                "DCC_BLOCK_SIZE", "DCC_SEND_BUFFER",
                                                "DCC_PORT_START", "DCC_PORT_END",
                                                "DCC_ACCEPT_TIMEOUT", "MAX_SEND_FAILS",
                                                "DAILY_SEND_FAILS_LIMIT"]),
    ("your-list",     "Your list",             ["LIST_BASE_NAME",
                                                "LIST_FORMAT", "LIST_IGNORED_EXTENSIONS",
                                                "SEPARATE_VIDEO_LIST", "LIST_VIDEO_EXTENSIONS",
                                                "LIST_VIDEO_COMPANION_EXTENSIONS",
                                                "RAR_ENABLED", "RAR_EXTENSIONS", "RAR_BINARY",
                                                "MAX_RAR_FOLDER_SIZE", "RAR_TIMEOUT",
                                                "LIST_HEADER_FILE",
                                                "LIST_HEADER_MAX_BYTES",
                                                "LIST_SHOW_AUDIO_INFO"]),
    # #776: when the list rebuilds by itself, beside the two limits every
    # rebuild runs under. Its own category because "Your list" had reached
    # the sixteen the grouping test allows before one becomes a dumping ground.
    ("list-rebuild",  "List rebuild",          ["LIST_REBUILD_SCHEDULE",
                                                "LIST_UPDATE_TIMEOUT",
                                                "LIST_UPDATE_STALL_SECONDS",
                                                "LIST_AUDIO_INFO_MINUTES",
                                                "LIST_AUDIO_INFO_THREADS",
                                                "LIST_SCAN_THREADS"]),
    # #926: fetching lists nobody asked for is its own decision, with its
    # own rules - not four more lines at the end of "Fetching from bots".
    ("list-grab",     "Grabbing lists",        ["AUTO_GRAB_LISTS", "AUTO_GRAB_EVERY_MINUTES",
                                                "AUTO_GRAB_MIN_FILES", "AUTO_GRAB_MIN_SPEED_KB",
                                                "AUTO_DISCOVER_CHANNEL_LISTS",
                                                "MULTI_CHANNEL_LIST_STABLE_SECONDS"]),
    ("fetching",      "Fetching from bots",    ["MAX_FETCH_SLOTS", "AUTO_REFETCH_LISTS",
                                                "AUTO_REFETCH_INTERVAL_HOURS",
                                                "AUTO_REFETCH_MAX_PER_RUN",
                                                "FETCH_OFFER_TIMEOUT",
                                                "FETCH_TRANSFER_TIMEOUT",
                                                "FETCH_FOLDER_OFFER_TIMEOUT",
                                                "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED",
                                                "FETCH_FOLDER_TRANSFER_TIMEOUT",
                                                "MAX_FETCH_FILE_SIZE",
                                                "MAX_FETCH_FOLDER_FILE_SIZE",
                                                "MAX_FETCH_LIST_FILE_SIZE",
                                                "MAX_LIST_TEXT_SIZE",
                                                "FETCH_HISTORY_DAYS",
                                                "FETCH_HISTORY_MAX_ROWS",
                                                "LIST_REQUEST_TAG"]),
    # #926: how the fetch queue paces itself with another bot.
    # #1210: a bot that keeps failing is paused for a while.
    ("fetch-queue",   "Fetch queue",           ["FETCH_MAX_PER_BOT", "FETCH_QUEUED_TIMEOUT",
                                                "FETCH_BOT_MAX_FAILS",
                                                "FETCH_BOT_COOLDOWN_MINUTES"]),
    ("advertising",   "Advertising & search",  ["ANNOUNCE_INTERVAL", "ANNOUNCE_TRANSFERS",
                                                "BROADCAST_SEARCH_CHANNEL",
                                                "BROADCAST_SEARCH_COOLDOWN", "CTCP_VERSION_REPLY",
                                                "MSG_DELAY", "DEBUG_MSG_DELAY"]),
    # Its own category, not a corner of "Advertising & search". The theme
    # lived there because announce.py is what draws it - which is a fact about
    # the code, not about what an operator came looking for.
    ("appearance",    "Appearance",            ["THEME", "CUSTOM_THEME_BORDER",
                                                "CUSTOM_THEME_SEPARATOR", "CUSTOM_THEME_TEXTBOX",
                                                "CUSTOM_THEME_VALUE", "CUSTOM_THEME_ALERT",
                                                "CUSTOM_THEME_ACCENT"]),
    ("anti-flood",    "Anti-flood",            ["MAX_REQUESTS", "REQUEST_WINDOW", "MUTE_TIME",
                                                "FLOOD_BAN_SECONDS"]),
    # Its own category for the reason Appearance gives above. The cooldown
    # below is an anti-flood setting by mechanism, but an operator looking
    # for it is thinking about private messages, not about flooding.
    ("private-messages", "Private messages",   ["PRIVATE_MESSAGES_ENABLED",
                                                "PRIVATE_MESSAGE_COOLDOWN_SECONDS",
                                                "PRIVATE_MESSAGE_DECLINE_TEXT",
                                                "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS",
                                                "PRIVATE_MESSAGE_DECLINE_BURST",
                                                "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS",
                                                "CHAT_HISTORY_DAYS", "CHAT_HISTORY_MAX_MB"]),
    ("admin-console", "Admin console",         ["ADMIN_HOSTMASKS", "ADMIN_CHAT_MODE",
                                                "ADMIN_CHANNEL_COMMANDS", "ADMIN_CHAT_COLOURS"]),
    ("web-dashboard", "Web dashboard",         ["WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT",
                                                "WEBUI_ALLOWED_HOSTS", "WEBUI_CONSOLE_ENABLED", "WEBUI_OPEN_BROWSER",
                                                "WEBUI_FOLDER_BROWSER_ENABLED"]),
    ("console-feed",  "Console feed",          ["CONSOLE_SHOW_REQUESTS", "CONSOLE_SHOW_QUEUE",
                                                "CONSOLE_SHOW_SENDS", "CONSOLE_SHOW_FAILURES",
                                                "CONSOLE_SHOW_SEARCHES", "DEBUG_CHANNEL_FEED"]),
    ("debug",         "Debug & logging",       ["DEBUG_MODE", "DEBUG_TO_CHANNEL",
                                                "DEBUG_TO_CONSOLE",
                                                "CONSOLE_TIMESTAMP_FORMAT", "CONSOLE_LOG_FILE",
                                                "CONSOLE_LOG_MAX_MB", "CONSOLE_LOG_KEEP", "BOT_WINDOW",
                                                "PROJECT_URL"]),
    # LAST, and named so nobody opens it by accident. Set once at install, and
    # a wrong value here loses a queue or a statistics file rather than
    # mis-tuning something. They were interleaved with the settings changed
    # weekly.
    ("advanced",      "Advanced: file locations",
                                               ["TMP_ZIP_DIR", "LOCAL_LIST_DIR",
                                                "FETCHED_FILES_DIR", "BANS_FILE", "HARD_BANS_FILE",
                                                "STATS_FILE", "KNOWN_BOTS_FILE",
                                                "FETCHED_BOT_LISTS_FILE", "LIST_INDEX_FILE",
                                                "LIST_AUDIO_INFO_CACHE",
                                                "FETCH_HISTORY_FILE", "DOWNLOAD_COUNTS_FILE",
                                                "TRANSFER_LOG_FILE",
                                                "LIST_SIZE_FILE", "LIST_RAWBYTES_FILE",
                                                "LIST_PROGRESS_FILE", "LIBRARY_FOLDERS_FILE",
                                                "LISTS_FILE", "ADMIN_TOKENS_FILE", "ON_CONNECT_FILE",
                                                "NOTICES_FILE",
                                                "PRIVATE_MESSAGES_FILE", "DCC_QUEUE_FILE"]),
)


# A human-readable label per setting, since the raw config.py name
# (MAX_DCC_SLOTS, DCC_PORT_START, ...) is what an operator edits in a text
# file, not what they expect to read on a form. Checked against
# SETTINGS_CATEGORIES by SettingsPayloadTests' completeness guard, same
# reasoning as that list itself: a setting added to config.py and slotted
# into a category but never given a label here would otherwise silently show
# its raw name instead of failing a test.
SETTINGS_LABELS = {
    "SERVER": "IRC server",
    "PORT": "Port",
    "NICKNAME": "Nickname",
    "ALT_NICKNAME": "Alt nickname",
    "ADMIN_NICK": "Admin nick(s)",
    "CHANNEL": "Channels",
    "DEBUG_CHANNEL": "Debug channel",

    "MAX_DCC_SLOTS": "Max simultaneous sends",
    "SLOW_SEND_MIN_KB": "Stop sends slower than (KB/s, 0 = off)",
    "LARGE_FILE_MB": "One large file at a time, from (MB, 0 = off)",
    "MAX_USER_QUEUE": "Max queue per user",
    "MAX_GLOBAL_QUEUE": "Max global queue",
    "SEARCH_ENABLED": "Answer @find searches",
    "MAX_SEARCH_RESULTS": "Max search results",
    "SEARCH_SHOW_FOLDER": "Name the folder in search replies",
    "SEARCH_FOLDER_MAX_CHARS": "Longest folder shown (characters)",
    "MSG_DELAY": "Message delay (seconds)",
    "DEBUG_MSG_DELAY": "Debug message delay (seconds)",
    "DCC_PORT_START": "DCC port range start",
    "DCC_PORT_END": "DCC port range end",
    "MAX_FETCH_SLOTS": "Max fetch slots",
    "FETCH_HISTORY_DAYS": "Keep finished downloads for (days)",
    "FETCH_HISTORY_MAX_ROWS": "Maximum finished downloads kept",
    "LIST_REQUEST_TAG": "Tag on your list requests",
    "MAX_FETCH_FILE_SIZE": "Max fetch file size",
    "MAX_LIST_TEXT_SIZE": "Largest list text accepted from a peer",
    "DCC_BLOCK_SIZE": "Packet size",
    "DCC_SEND_BUFFER": "Socket send buffer (0 = the default for your platform)",
    "MAX_UPLOAD_KB": "Max upload speed (KB/s, 0 = no limit)",
    "MAX_DOWNLOAD_KB": "Max download speed (KB/s, 0 = no limit)",
    "REHASH_TRANSFER_WAIT": "Seconds a rehash waits for transfers to finish",
    "AUTO_REFETCH_LISTS": "Re-fetch a held list when its bot advertises a new one",
    "AUTO_GRAB_LISTS": "Grab the lists of bots you have no list from",
    "AUTO_GRAB_EVERY_MINUTES": "Minutes between automatic grabs",
    "AUTO_GRAB_MIN_FILES": "Skip bots with fewer files than",
    "AUTO_GRAB_MIN_SPEED_KB": "Skip bots slower than (KB/s)",
    "AUTO_DISCOVER_CHANNEL_LISTS": "Discover a bot's other channel-bound lists",
    "MULTI_CHANNEL_LIST_STABLE_SECONDS": "Hold stable this long first (seconds)",
    "AUTO_REFETCH_INTERVAL_HOURS": "Least time between re-fetches of one bot (hours)",
    "AUTO_REFETCH_MAX_PER_RUN": "Most lists to re-fetch in one sweep",
    "FETCH_TRANSFER_TIMEOUT": "Fetch transfer timeout (seconds)",
    # Named from the operator's side, like the pill in the Downloads table:
    # this is the wait AFTER we send a request, not a timeout on an offer
    # anybody made us.
    "FETCH_QUEUED_TIMEOUT": "Wait for a queued request (s)",
    "FETCH_MAX_PER_BOT": "Files asked of one bot at once",
    "FETCH_BOT_MAX_FAILS": "Failed requests in a row that pause a bot",
    "FETCH_BOT_COOLDOWN_MINUTES": "Minutes a failing bot stays paused",
    "FETCH_OFFER_TIMEOUT": "Wait for a reply to a fetch request (seconds)",
    "FETCH_FOLDER_OFFER_TIMEOUT": "Wait for a reply to a folder (.rar) request (seconds)",
    "FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED":
        "...from a bot that publishes no .rar list (seconds)",
    "MAX_FETCH_FOLDER_FILE_SIZE": "Max folder (.rar) fetch size",
    "MAX_FETCH_LIST_FILE_SIZE": "Max fetched master-list zip size",
    "FETCH_FOLDER_TRANSFER_TIMEOUT": "Folder (.rar) fetch transfer timeout (seconds)",

    "LIST_BASE_NAME": "List base name",
    "PAUSE_ON_UPDATE": "Pause sharing during !update",
    "PAUSE_FOR_WHOLE_UPDATE": "Pause for the whole rebuild",
    # Named for what it now IS. From an operator: "under Paths & Storage, this is not
    # needed anymore" - not quite, it is still the fallback for an install
    # with no folder list, which is most of them. But presenting it as a
    # plain "Music directory" beside a folder editor that overrides it is
    # what made it read as the setting that matters.
    "FILE_DIRECTORY": "Music directory (used only when no folders are set)",
    "LIST_FORMAT": "List delivery format",
    "LIST_IGNORED_EXTENSIONS": "File types to leave out of the list",
    "SEPARATE_VIDEO_LIST": "Publish film and series as a separate list",
    "LIST_VIDEO_EXTENSIONS": "File types that go in the film list",
    "LIST_VIDEO_COMPANION_EXTENSIONS": "File types that follow a film into its list (subtitles, .nfo, .sfv)",
    "RAR_EXTENSIONS": "File types a folder needs to be !rar-packable",
    "RAR_ENABLED": "Enable !rar folder packing",
    "RAR_BINARY": "RAR binary path",
    "TMP_ZIP_DIR": "Temp archive directory",
    "MAX_RAR_FOLDER_SIZE": "Largest folder !rar will pack (0 = no limit)",
    "LOCAL_LIST_DIR": "Master list directory",
    "FETCHED_FILES_DIR": "Fetched files directory",
    "BANS_FILE": "Bans file",
    "STATS_FILE": "Stats file",
    "HARD_BANS_FILE": "Hard bans file",
    "DCC_QUEUE_FILE": "DCC queue file",
    "KNOWN_BOTS_FILE": "Known bots file",
    "LIST_INDEX_FILE": "Cross-list search index",
    "LIST_AUDIO_INFO_CACHE": "Audio info cache",
    "LIST_SHOW_AUDIO_INFO": "Length and quality in the list",
    "LIST_AUDIO_INFO_MINUTES": "Time limit for reading audio files (no longer used)",
    "LIST_AUDIO_INFO_THREADS": "Audio files read at once",
    "LIST_SCAN_THREADS": "Folders scanned at once",
    "DOWNLOAD_COUNTS_FILE": "Download counts file",
    "TRANSFER_LOG_FILE": "Transfer record file",
    "FETCHED_BOT_LISTS_FILE": "Fetched bot lists file",
    "FETCH_HISTORY_FILE": "Fetch history file",
    "NOTICES_FILE": "Operator notices file",
    "PRIVATE_MESSAGES_FILE": "Private messages file",
    "PRIVATE_MESSAGE_COOLDOWN_SECONDS": "Record one private message per sender every (seconds)",
    "PRIVATE_MESSAGES_ENABLED": "Keep private messages (off: keep none, reply once instead)",
    "PRIVATE_MESSAGE_DECLINE_TEXT": "That reply's wording (%admin becomes the admin nick)",
    "PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS": "Reply to the same sender once every (seconds)",
    "PRIVATE_MESSAGE_DECLINE_BURST": "Most replies to send in one burst window",
    "PRIVATE_MESSAGE_DECLINE_BURST_SECONDS": "How long that burst window is (seconds)",
    "CHAT_HISTORY_DAYS": "Keep the chat history in mIRC for (days, 0 = always)",
    "CHAT_HISTORY_MAX_MB": "Largest chat history in mIRC (MB, 0 = no limit)",
    "LIST_SIZE_FILE": "List size file",
    "LIST_PROGRESS_FILE": "List rebuild progress file",
    "LIST_RAWBYTES_FILE": "List raw bytes file",
    "LIST_HEADER_FILE": "List banner file",
    "LIST_HEADER_MAX_BYTES": "List banner size limit",
    "LIBRARY_FOLDERS_FILE": "Served folders file",
    "LISTS_FILE": "Served lists file",
    "ADMIN_TOKENS_FILE": "Paired console scripts file",
    "ON_CONNECT_FILE": "On-connect commands file",

    "THEME": "Colour theme",
    "CUSTOM_THEME_BORDER": "Custom theme: border colour",
    "CUSTOM_THEME_SEPARATOR": "Custom theme: separator colour",
    "CUSTOM_THEME_TEXTBOX": "Custom theme: text box colour",
    "CUSTOM_THEME_VALUE": "Custom theme: value colour",
    "CUSTOM_THEME_ALERT": "Custom theme: alert colour",
    "CUSTOM_THEME_ACCENT": "Custom theme: accent colour",
    "ANNOUNCE_TRANSFERS": "Announce finished transfers in the channel",
    "REJOIN_ATTEMPTS": "Rejoin attempts after a kick (0 = never)",
    "ON_CONNECT_CHECK_MINUTES": "Check the on-connect commands worked every (minutes, 0 = never)",
    "ANNOUNCE_INTERVAL": "Advert interval (seconds)",
    "BROADCAST_SEARCH_CHANNEL": "Broadcast search channel",
    "BROADCAST_SEARCH_COOLDOWN": "Broadcast search cooldown (seconds)",
    "CTCP_VERSION_REPLY": "Answer CTCP VERSION",

    "MAX_REQUESTS": "Max commands per window",
    "REQUEST_WINDOW": "Request window (seconds)",
    "MUTE_TIME": "Mute duration (seconds)",
    "FLOOD_BAN_SECONDS": "Ban after flooding while muted (seconds)",
    "DCC_ACCEPT_TIMEOUT": "Wait for the receiver to connect (seconds)",
    "MAX_SEND_FAILS": "Max send failures",
    "DAILY_SEND_FAILS_LIMIT": "Failed sends in a day before a nick is paused (0 = off)",
    "CHECK_FOR_UPDATES": "Tell me when a new version is out",
    "RAR_TIMEOUT": "RAR pack timeout (seconds)",
    "LIST_UPDATE_TIMEOUT": "List rebuild hard cap (seconds, 0 = none)",
    "LIST_UPDATE_STALL_SECONDS": "Give up if a rebuild reports nothing for (seconds)",
    "LIST_REBUILD_SCHEDULE": "Rebuild the list automatically",

    "ADMIN_HOSTMASKS": "Admin hostmasks",
    "ADMIN_CHAT_MODE": "DCC chat connection mode",
    "ADMIN_CHANNEL_COMMANDS": "Allow admin commands in channel",
    "ADMIN_CHAT_COLOURS": "Colour the tags in the admin DCC chat",

    "WEBUI_ENABLED": "Enable web dashboard",
    "WEBUI_HOST": "Host",
    "WEBUI_PORT": "Port",
    "WEBUI_ALLOWED_HOSTS": "Other names the dashboard answers to",
    "WEBUI_FOLDER_BROWSER_ENABLED": "Folder picker on the Settings page",
    "WEBUI_CONSOLE_ENABLED": "Enable the Console page (remote admin)",
    "WEBUI_OPEN_BROWSER": "Open the dashboard in a browser at startup",

    "DEBUG_MODE": "Debug mode",
    "DEBUG_TO_CHANNEL": "Send debug lines to channel",
    "DEBUG_TO_CONSOLE": "Send debug lines to admin console",
    "CONSOLE_SHOW_REQUESTS": "Show requests (who asked for what)",
    "CONSOLE_SHOW_QUEUE": "Show queue positions",
    "CONSOLE_SHOW_SENDS": "Show transfers starting, resuming and completing",
    "CONSOLE_SHOW_FAILURES": "Show failed transfers",
    "CONSOLE_SHOW_SEARCHES": "Show searches and their result counts",
    "DEBUG_CHANNEL_FEED": "Also send requests, queue positions, starts and searches to the IRC debug channel",
    "CONSOLE_TIMESTAMP_FORMAT": "Time prefix on every console line (strftime; blank = none)",
    "CONSOLE_LOG_FILE": "Log file (blank = none)",
    "CONSOLE_LOG_MAX_MB": "Start a new log file at (MB)",
    "CONSOLE_LOG_KEEP": "Old log files to keep",
    "BOT_WINDOW": "The bot's window on Windows (normal, minimised, hidden)",
    "PROJECT_URL": "Project URL",
}


# A size an operator reads in the unit they think in, while the file keeps
# bytes. Counting the zeros in 10737418240 to check it says ten gigabytes is
# not work anybody should be doing, and the sizes here span 8 KB to 10 GB.
#
# STORED IN BYTES, UNCHANGED. settings.conf, admin_config.py and every reader
# in the daemon keep the number they have always had, so nothing migrates and
# an operator who edits the file by hand sees exactly what they saw before.
# Only the dashboard divides, and only for display.
#
# The unit is per setting rather than picked from the magnitude: a value that
# changed unit as it grew would move under the operator mid-edit, and 0 -
# which several of these use for "no limit" - has no magnitude to read.
# KB where MB would print 0.0078.
SETTINGS_UNITS = {
    "MAX_RAR_FOLDER_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_FILE_SIZE": ("MB", 1024 * 1024),
    "MAX_LIST_TEXT_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_FOLDER_FILE_SIZE": ("MB", 1024 * 1024),
    "MAX_FETCH_LIST_FILE_SIZE": ("MB", 1024 * 1024),
    "DCC_SEND_BUFFER": ("KB", 1024),
    "LIST_HEADER_MAX_BYTES": ("KB", 1024),
}


# DCC_BLOCK_SIZE is a menu, not a number to type, so it keeps its byte values
# as the stored choice and gains readable text beside each one.
CHOICE_LABELS = {
    "DCC_BLOCK_SIZE": {str(1024 * n): f"{n} KB"
                       for n in (4, 8, 16, 32, 64, 128, 256)},
}


def _settings_field(name, declared, value):
    """One field for the settings form.

    "choices" is present only for a setting that has a fixed few - LIST_FORMAT
    is the first. It carries the same tuple settings_file.CHOICES validates
    against, so the page cannot offer a value the save would then refuse, and
    an operator picks a format instead of typing one of three words correctly.
    """
    import settings_file
    field = {"name": name, "label": SETTINGS_LABELS.get(name, name),
             "type": declared.__name__, "value": value}
    if name in settings_file.CHOICES:
        field["choices"] = list(settings_file.CHOICES[name])
        labels = CHOICE_LABELS.get(name)
        if labels:
            field["choice_labels"] = [labels.get(str(c), str(c))
                                      for c in field["choices"]]

    # A COLOUR THE OPERATOR CAN READ, AND PICK. config holds the decoded
    # bytes - a role of "\x0313" is four characters, two of them a control
    # code - and the page was being handed them raw. A text input cannot show
    # 0x03, so the field for that accent read "13": the code was present,
    # invisible, and what the operator could see was not what was set. Typing
    # back what they read would have put a literal "13" in every advert.
    #
    # So the value crosses as the escape text settings.conf.sample documents,
    # which is also what the save writes back - and `irc_colour` tells the
    # page it may offer the sixteen colours as menus instead of asking for a
    # control code to be typed into a box that cannot display one.
    if name.startswith("CUSTOM_THEME_"):
        import settings_file as _settings_file
        field["value"] = _settings_file.encode_irc_escapes(value or "")
        field["irc_colour"] = True

    unit = SETTINGS_UNITS.get(name)
    if unit:
        field["unit"], field["unit_factor"] = unit

    # WHAT THE SETTING MEANS (#528). "A lot of settings are not easy to
    # understand" - and the explanation for every one of them already
    # existed, as the comment block beside it in defaults.py, which
    # settings.conf.sample is generated from. settings_help reads the same
    # block, so the code, the sample and the page can never disagree. The
    # page draws a "?" beside the label that shows this on hover; a setting
    # with nothing to say gets no "?" rather than an empty box.
    import settings_help
    help_text = settings_help.help_text(name)
    if help_text:
        field["help"] = help_text

    # A TRI-STATE NEEDS A THIRD ANSWER. WEBUI_CONSOLE_ENABLED is declared
    # `bool = None`, and None does not mean False: console_is_enabled() reads
    # it as "yes if nobody else can reach it", so a stock loopback install has
    # the Console ON. The page renders a bool as a checkbox from `!!value`,
    # which drew None as unchecked - telling the operator that ban, unban,
    # clearqueue, rehash and update were NOT reachable behind the dashboard
    # password when they were.
    #
    # A note rather than sending the effective value as `value`: that would
    # make the checkbox truthful, but the next save would then write an
    # explicit True, and an operator who later moved the dashboard onto the
    # LAN would keep a Console that should have switched itself off. The
    # value stays unset; what changes is that the page says what unset means
    # here and now.
    if name == "WEBUI_CONSOLE_ENABLED" and value is None:
        field["note"] = ("Not set: on while the dashboard is loopback-only. "
                         "Currently " + ("ON" if console_is_enabled() else "OFF") + ".")
    return field


# The roles a theme has, and the one sample line each of them shows up in.
# Named here so the page can say which setting it is that the operator just
# changed nothing visible with - and so a role added later without a sample is
# a failing test rather than a silent gap.
THEME_PREVIEW_ROLES = {
    "border": "both", "separator": "both", "textbox": "both",
    "value": "both", "alert": "both", "accent": "notice",
}


def build_theme_preview(overrides=None):
    """The two lines a theme is judged by, rendered with `overrides`.

    WHY A PREVIEW EXISTS. The six CUSTOM_THEME_* settings hold raw mIRC colour
    codes, typed into text boxes. Finding out what a change did meant saving
    it, rehashing, and watching the channel for the next advert - and the next
    advert is up to ANNOUNCE_INTERVAL away, on a bot other people are using.

    WHY BOTH LINES. Between them they use all six roles, and neither uses all
    six alone: the advert never touches `accent`, only the completion notice
    does. A single sample would leave one setting looking like it does
    nothing, which is the confusion this is meant to end.

    Built by announce.py's own builders, not by a copy of their templates. A
    copy drifts the first time one of them changes, and then the preview lies
    with a straight face.

    `overrides` is what the operator has typed and not saved. It never reaches
    config: a live daemon is serving a channel while they are choosing
    colours.

    Returns the RAW lines, colour codes and all. Rendering them is the page's
    job - it is the one that knows how wide the box is.
    """
    import announce

    settings = dict(overrides or {})
    nick = str(getattr(config, "NICKNAME", "") or "DCCore")
    channel = (str(getattr(config, "CHANNEL", "") or "#channel")
               .split(",")[0].strip() or "#channel")

    # Plausible rather than real. Reading the live figures would make the
    # preview flicker as transfers come and go, and a sample that changes
    # while you are comparing two colours is a sample you cannot compare.
    advert = announce.build_advert_line(
        channel, nick, "719,041", "5.48 TB", "Sep 7th", "3/3", "0",
        "2.4MB/s", "24.7MB/s", "1,204 Files (8.9 TB)",
        str(config.SCRIPT_VERSION), settings=settings)
    notice = announce.build_transfer_complete_line(
        channel, "someuser", "Some Artist - Some Album - 01 - A Track.flac",
        "1,204 Files (8.9 TB)", "37", "12", "3:04 pm", "24.7MB/s",
        settings=settings)

    # The PRIVMSG envelope is protocol, not something anybody sees in a
    # channel. Showing it would put "PRIVMSG #chan :" at the front of a
    # preview of what the channel looks like.
    def body(line):
        return line.split(" :", 1)[1].rstrip("\r\n") if " :" in line else line

    return {
        "advert": body(advert),
        "notice": body(notice),
        "roles": THEME_PREVIEW_ROLES,
    }


def theme_preview_overrides(payload):
    """The subset of a posted body that may steer a preview.

    A whitelist, not a filter: this reaches theme.palette(), and everything
    else on the settings page has nothing to say about colour. THEME is
    checked against the presets that exist, because an unknown name would
    otherwise pick the configured one and quietly preview the wrong thing.

    AND IT COERCES THE WAY THE FILE WOULD. A colour crosses the wire as the
    escape text `\x0313` - see _settings_field() for why it must, and
    settings_file.encode_irc_escapes() for the other half of that trip - while
    theme.palette() deals in the decoded BYTE, which is what theme.CLASSIC and
    every other preset holds.

    Nothing was decoding it here, so the preview rendered the operator's typed
    text as nine literal characters. The advert sample began with a visible
    `\x0309,07` and overflowed to the right, which is exactly the failure the
    preview exists to show them they are about to cause. Reported against the
    colour picker, but it arrived with the preview itself and would have hit
    anyone who typed a code by hand.

    Through settings_file.coerce(), not a private copy of the rule: the
    preview's whole claim is that it shows what saving would produce, and two
    functions that merely happen to agree today are two functions that can
    stop agreeing.
    """
    import settings_file
    import theme

    body = json_object(payload)
    wanted = {}
    name = str(body.get("THEME", "") or "").strip().lower()
    if name in theme.THEMES:
        wanted["THEME"] = name
    for role in theme.ROLES:
        key = f"CUSTOM_THEME_{role.upper()}"
        if key in body:
            value = body.get(key)
            wanted[key] = (settings_file.coerce(key, value, "", str)
                           if isinstance(value, str) else "")
    # SEARCH_ENABLED (#1249 review): announce.search_state() already reads
    # the unsaved value first - `(settings or {}).get("SEARCH_ENABLED", ...)`
    # - but nothing ever put it in `settings`, since this whitelist stopped
    # at THEME and CUSTOM_THEME_*. The advert sample's "Search: ON/OFF"
    # followed the SAVED setting regardless of what the operator had just
    # toggled on the page, same as every other preview bug this function
    # exists to prevent.
    if "SEARCH_ENABLED" in body:
        wanted["SEARCH_ENABLED"] = bool(body.get("SEARCH_ENABLED"))
    return wanted


def build_settings_payload():
    """GET /api/settings payload: every editable setting, grouped for the
    Settings view's category rail, plus whether an admin password is set.

    Filters through settings_file.declared_types(vars(config)) - names
    config.py itself annotates - rather than vars(config) directly, so a
    runtime-only name (ORIGINAL_NICK, MY_IP_OR_DOCK, ...) can never appear
    here even before settings_file.save() would refuse to write it.

    ADMIN_PASSWORD_HASH never appears as a field's raw value anywhere - only
    the boolean admin_password_set at the top level. A "leftover" category is
    appended for any declared+overridable setting SETTINGS_CATEGORIES above
    forgot to mention, so a config.py addition that nobody categorised is
    still reachable rather than silently missing from the page (see
    SettingsPayloadTests' completeness guard, which currently keeps this at
    zero entries by keeping SETTINGS_CATEGORIES exhaustive).

    Read under runtime.config_reload_lock, because this page is what found the
    reload window (see that lock's comment). Saving a setting from here starts
    a rehash on a background thread and returns immediately; the browser then
    re-fetches this endpoint at once and, without the lock, read config while
    importlib.reload(defaults) was part way through re-executing it - so the
    Settings page rendered Nickname, Admin nick(s) and Channels blank on a
    daemon whose settings.conf had all three. The lock makes this wait for the
    reload it just triggered rather than reporting its halfway state.
    """
    import settings_file
    with runtime.config_reload_lock:
        return _settings_payload_unlocked(settings_file)


def _settings_payload_unlocked(settings_file):
    """build_settings_payload()'s body, split out only so the lock above wraps
    one expression and cannot be left un-taken by a later edit adding an early
    return."""
    types = settings_file.declared_types(vars(config))
    categories = []
    seen = set()
    for cat_id, label, names in SETTINGS_CATEGORIES:
        fields = []
        for name in names:
            if name not in types:
                continue
            value = getattr(config, name, None)
            if not settings_file.is_overridable(name, value):
                continue
            fields.append(_settings_field(name, types[name], value))
            seen.add(name)
        categories.append({"id": cat_id, "label": label, "fields": fields})

    # A leftover has no entry in SETTINGS_LABELS either - by construction, it
    # was never in SETTINGS_CATEGORIES, and SETTINGS_LABELS only ever gets a
    # name added alongside slotting it into a category. Falls back to the raw
    # name rather than raising, matching how it already reaches the page at
    # all despite being uncategorised.
    leftover = [n for n in types if n not in seen and n != "ADMIN_PASSWORD_HASH"
                and n not in EDITED_ELSEWHERE
                and settings_file.is_overridable(n, getattr(config, n, None))]
    if leftover:
        categories.append({"id": "other", "label": "Other", "fields": [
            _settings_field(n, types[n], getattr(config, n, None))
            for n in sorted(leftover)]})

    return {
        "categories": categories,
        "admin_password_set": bool(getattr(config, "ADMIN_PASSWORD_HASH", "")),
    }


# Settings that only take effect on a full daemon restart - webserver.py owns
# a live listening socket and is deliberately excluded from
# commands.py's CORE_MODULES, so a rehash after saving one of these three
# cannot apply it live. Surfaced in the save response's "restart_required" so
# the frontend can tell the operator, rather than implying "rehash" fixed it.
# Settings a running daemon cannot pick up, whatever it reloads.
#
# NOT "everything to do with the network" - most of it IS live, and listing
# those here would train an operator to ignore the notice. The DCC port range
# is read per send, so a rehash applies it to the very next transfer; the
# channel list is compared and JOIN/PARTed by the rehash itself.
#
# What is genuinely stuck is anything read ONCE, against a socket already open:
#
#   WEBUI_*        oserve.startup() starts the dashboard thread once, bound to
#                  the host and port it read then. Changing them from inside
#                  that dashboard cannot move it.
#   SERVER / PORT  irc.py reads these in connect(), and the connection the bot
#                  is on was made with the old ones. #302 named this exactly -
#                  "when a user changes ports ... the changes do not take
#                  effect immediately" - and they were the two missing here, so
#                  the save said nothing and the operator was left to work out
#                  why the bot was still on the old server.
#
# A restart is PROMPTED, not performed. #302 offered either; restarting a
# process from inside itself while it holds live transfers is a different order
# of risk from telling the operator what to do next.
SETTINGS_RESTART_ONLY = {"WEBUI_ENABLED", "WEBUI_HOST", "WEBUI_PORT",
                         "SERVER", "PORT"}


def _save_settings_and_rehash(changes, confirmed_debug_removal=False, source=None, on_applied=None):
    """Write `changes` to settings.conf and dispatch a rehash on its own
    daemon thread. The shared tail of apply_settings_changes() (POST
    /api/settings) and build_password_change_result() (POST
    /api/settings/password): a plain function call does not skip a caller's
    own `if` checks, so build_password_change_result reaches this directly
    rather than through apply_settings_changes() - going through that
    function instead would always hit its own ADMIN_PASSWORD_HASH rejection,
    which exists to stop the *general* settings endpoint from being used to
    change the password, not to block the password endpoint's own write of
    the one setting it exists to change.

    The rehash runs detached rather than inline: commands.handle_rehash_request()
    does real socket I/O and importlib.reload()s several modules, which would
    freeze this request for its duration - the same reason adminchat.py's own
    _cmd_rehash wraps the identical call in _run_detached(). This module
    already has the same shape for a different slow/async action; see
    start_broadcast_search()'s threading.Thread(target=_close_window, ...).

    settings_file.save() holds its own lock around the read-modify-write of
    settings.conf, so two concurrent saves from here cannot race each other -
    see that function's docstring. Both settings_file.SettingsWriteError
    (save()'s own validation failures) and a plain OSError (e.g. an
    unwritable settings directory - the underlying atomic write can raise
    this too) are caught here and turned into a clean 400 JSON error; letting
    an OSError escape would surface as an unhandled 500 with a non-JSON body,
    which the frontend's postJson() cannot parse.

    Returns (http_status, payload_dict).
    """
    import settings_file
    try:
        result = settings_file.save(vars(config), changes)
    except (settings_file.SettingsWriteError, OSError) as err:
        return 400, {"error": str(err)}

    # Imported lazily, exactly like `list`/`dcc_fetch` above - see the module
    # docstring and tests/test_import_graph.py: `commands` must never load at
    # module scope, only from inside a handler that actually needs it.
    import commands
    # `source` is (who, where) for the rehash's log line. The admin console
    # saves through this same function (#1264) and names itself, so a change
    # made from mIRC is not logged as the dashboard's.
    who, where = source or (WEB_DASHBOARD_SOURCE, WEB_DASHBOARD_SOURCE)
    rehash_kwargs = {"authorised": True, "confirmed_debug_removal": confirmed_debug_removal}

    def rehash():
        # `on_applied` is told once the rehash has finished - the console's
        # settings window reloads its page then (#1264), not at the save,
        # when config still holds the old values: the rehash first waits up
        # to REHASH_TRANSFER_WAIT for transfers to finish.
        try:
            commands.handle_rehash_request(who, where, **rehash_kwargs)
        finally:
            if on_applied is not None:
                on_applied()

    threading.Thread(target=rehash, daemon=True).start()

    restart_required = sorted(set(result["written"]) & SETTINGS_RESTART_ONLY)
    return 200, dict(result, rehash="started", restart_required=restart_required)


def apply_settings_changes(changes, source=None, on_applied=None):
    """POST /api/settings's pure logic: validate `changes` (a flat
    {SETTING: "string value"} object - settings_file.save() coerces each
    value the same way settings.conf itself would be read), then hand off to
    _save_settings_and_rehash() for the actual write + dispatched rehash.

    `confirm_debug_channel_removed` (#1008 follow-up) is not a setting - it
    is popped out here, before `changes` ever reaches settings_file.save(),
    and threaded through as its own argument instead. It means nothing on
    its own: sync_channels() only ever reads it in the one case it exists
    for (DEBUG_CHANNEL going from a real channel to blank), so a stray or
    even malicious `true` sent on an unrelated save has no effect - there is
    nothing there for it to gate.

    Returns (http_status, payload_dict).
    """
    if not isinstance(changes, dict):
        return 400, {"error": "Expected a non-empty object of {SETTING: value}."}

    # Popped before the emptiness check below (the #1011 review):
    # a body whose only key is this flag is not a settings change, and used
    # to pass the check, save nothing and still start a rehash - harmless in
    # practice since the dashboard never sends the flag alone, but a body
    # this empty should 400 like any other.
    changes = dict(changes)
    confirmed_debug_removal = bool(changes.pop("confirm_debug_channel_removed", False))

    if not changes:
        return 400, {"error": "Expected a non-empty object of {SETTING: value}."}
    if "ADMIN_PASSWORD_HASH" in changes:
        return 400, {"error": "Use POST /api/settings/password to change the "
                               "admin password."}

    return _save_settings_and_rehash(changes, confirmed_debug_removal=confirmed_debug_removal,
                                     source=source, on_applied=on_applied)


def build_password_change_result(new_password, confirm_password):
    """POST /api/settings/password's pure logic: validate the pair, hash the
    new password the same way `python src/adminchat.py` does, and write it
    through _save_settings_and_rehash() - the same save-then-dispatch-rehash
    tail apply_settings_changes() uses, without its ADMIN_PASSWORD_HASH
    rejection (see that helper's docstring for why this cannot go through
    apply_settings_changes() itself).
    """
    if not new_password:
        return 400, {"error": "Password cannot be empty."}
    if new_password != confirm_password:
        return 400, {"error": "Passwords do not match."}

    new_hash = adminchat.make_password_hash(new_password)
    status, result = _save_settings_and_rehash({"ADMIN_PASSWORD_HASH": new_hash})
    if status == 200:
        # In force NOW, not when the rehash it started gets round to it: a
        # dashboard session is stamped with the password it was opened with
        # (_password_stamp), so this is also the moment every session opened
        # with the old password stops working. The rehash reads the same
        # hash back from settings.conf.
        config.ADMIN_PASSWORD_HASH = new_hash
    return status, result


def dashboard_is_loopback_only(host=None):
    """Is the dashboard reachable only from the machine it runs on?

    The whole question the Console's default turns on. Anything that is not a
    loopback address means somebody else can reach the login page, and from
    there the Console is admin behind one factor.

    Unparseable is treated as NOT loopback. A host this cannot read is a host
    this cannot vouch for, and the safe answer to "is this exposed" is yes.
    """
    import ipaddress

    text = str(host if host is not None
               else getattr(config, "WEBUI_HOST", "127.0.0.1")).strip()
    if not text:
        return False
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def console_is_enabled():
    """Whether the dashboard's Console is available.

    An explicit True or False in the operator's own configuration always wins:
    somebody who has already made this choice does not get it made again for
    them on upgrade.

    With no explicit value the answer is "yes if nobody else can reach it".
    The Console puts ban/unban/clearqueue/rehash/update behind the dashboard
    password alone - one factor, over plain HTTP - which is a real widening
    when the dashboard is on the LAN and no widening at all when it is on
    loopback, where the only person who can reach it already has the files and
    admin_config.py.
    """
    configured = getattr(config, "WEBUI_CONSOLE_ENABLED", None)
    if configured is not None:
        return bool(configured)
    return dashboard_is_loopback_only()
