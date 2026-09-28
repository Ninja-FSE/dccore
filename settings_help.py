"""What every setting means, read from the one place it is written down.

Every setting in defaults.py carries a comment block above it, and an
inline comment after it for the short ones. scripts/gen_settings_sample.py
has parsed those into settings.conf.sample since the sample was first
generated - one source, so the explanation cannot drift from the code. The
dashboard's Settings page is the third reader (#528): "a lot of settings
are not easy to understand", and the text that explains them already
existed, one file away from the page that needed it.

So the parser lives here, and both the generator and the /api/settings
payload call it. Parsed from the SOURCE with ast, never from the imported
module: the comments are not in the module, and the file is small enough
that a parse per change of defaults.py costs nothing (cached on mtime, so
the dashboard does not re-read it per request).
"""

import ast
import io
import os

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults.py")


def assignment_parts(node):
    """(targets, value_node) for a module-level assignment, else (None, None).

    `MAX_DCC_SLOTS = 3` parses to ast.Assign, but `MAX_DCC_SLOTS: int = 3`
    parses to ast.AnnAssign - a different node type, with `.target` rather
    than `.targets`. Matching only Assign makes every annotated setting
    invisible, which for the sample generator means silently emitting a
    sample with nothing in it.

    `value_node` is None for a bare annotation (`NICKNAME: str`), which
    declares a name's type without giving it a value.
    """
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target], node.value
    return None, None


def doc_lines(source_lines, node):
    """The comment block immediately above a setting, plus its inline comment.

    Section rules (`# ---`, `# ===`) end the block rather than joining it: they
    separate settings from the numbered headers above them, and are not
    about any one setting.
    """
    doc = []
    index = node.lineno - 2                  # the line above, 0-based
    block = []
    while index >= 0:
        stripped = source_lines[index].strip()
        if stripped.startswith("#") and not stripped.startswith("# ---") \
                and not stripped.startswith("# ==="):
            block.append(stripped.lstrip("#").strip())
            index -= 1
            continue
        break
    doc.extend(reversed(block))

    # Look for the inline comment only AFTER the value ends. Splitting the
    # whole line on "#" cuts inside a string literal, so
    #     CHANNEL = "#example-one,#example-two,..."
    # produced a junk comment line reading `example-one,...#example-three"` above
    # every channel-valued setting in the generated sample.
    own = source_lines[node.lineno - 1]
    if node.end_lineno == node.lineno:
        tail = own[node.end_col_offset:]
        if "#" in tail:
            inline = tail.split("#", 1)[1].strip()
            if inline:
                doc.append(inline)
    return doc


def parse_help(source):
    """{setting name: [comment lines]} for every module-level assignment in
    `source`. Every assignment, not only overridable ones: which settings an
    operator may change is settings_file's question, and the callers ask it."""
    lines = source.split("\n")
    tree = ast.parse(source)
    found = {}
    for node in tree.body:
        targets, _value = assignment_parts(node)
        if targets is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = doc_lines(lines, node)
    return found


_cache = {"stamp": None, "help": {}}


def help_lines():
    """{name: [lines]} for defaults.py as it is on disk right now.

    Re-parsed only when the file's mtime or size changes - a rehash after an
    edit to defaults.py sees the new text; every other call is a dict lookup.
    A defaults.py that cannot be read (a packaged install with no source, an
    unreadable file) yields an empty map: the page then shows no "?" rather
    than failing to load at all.
    """
    try:
        stat = os.stat(DEFAULTS_PATH)
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    if _cache["stamp"] != stamp:
        try:
            with io.open(DEFAULTS_PATH, encoding="utf-8") as handle:
                _cache["help"] = parse_help(handle.read())
        except (OSError, SyntaxError, UnicodeDecodeError):
            _cache["help"] = {}
        _cache["stamp"] = stamp
    return _cache["help"]


def plain_text(name):
    """The operator's explanation of a setting, or "" when there is none.

    Written for the person running the bot, not the person reading the
    code: what the setting does, when they would change it, what a sensible
    value is. The comment block in defaults.py stays the developer's record
    of WHY - it talks about settings_file.REQUIRED and oserve.startup(),
    which is right for the code and wrong for a tooltip. The two are kept
    apart on purpose; the guard in tests/test_every_setting_explains_itself.py
    requires every setting the dashboard shows to have one of these.
    """
    return PLAIN_HELP.get(name, "")


def developer_text(name):
    """The comment block from defaults.py, joined into paragraphs."""
    lines = help_lines().get(name) or []
    paragraphs, current = [], []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def help_text(name):
    """What the page's "?" shows: the plain explanation when there is one,
    the developer's comment block otherwise. "" when there is neither."""
    return plain_text(name) or developer_text(name)


# ---------------------------------------------------------------------
# THE OPERATOR'S EXPLANATIONS. One per setting the dashboard shows, in
# plain words - what it does, when you would change it, what a sensible
# value is. This is the English source: the page's "?" and the top of each
# entry in settings.conf.sample both come from here. The French and Spanish
# versions live in web/lang/fr.json and es.json under
# settings.field.<NAME>.help (en.json carries none - the page falls back to
# this text, see fieldHelp() in web/app.js). Rules, held by
# tests/test_every_setting_explains_itself.py: every setting on the page has
# one; none of them names a module, a function or an issue number.
# ---------------------------------------------------------------------
PLAIN_HELP = {
    'SERVER': 'The IRC server the bot connects to. For Undernet leave it as irc.undernet.org.',
    'PORT': 'The port on that server. 6667 is the normal one for plain IRC; the bot does not use SSL.',
    'NICKNAME': "The bot's name on IRC. People request files with it (for example @YourBot for the list), so pick something short and easy to type. Required - the bot will not start without it.",
    'ALT_NICKNAME': 'A backup name used if the main one is already taken when the bot connects. If this one is taken too, a digit is added to it. The bot switches back to the main name as soon as it is free.',
    'REJOIN_ATTEMPTS': 'How many times the bot tries to get back into a channel after being kicked before giving up on it. It waits until the next advert is due before each try, so it never looks like it is fighting the kick. 0 means never rejoin.',
    'ADMIN_NICK': 'Your own nick(s) - the people allowed to use the admin commands such as !ban, !rehash and !update. Separate several with commas. Required. If you have set ADMIN_HOSTMASKS, the command must also come from that host.',
    'CHANNEL': 'The channel(s) the bot serves in, separated by commas. The first one is where announcements go unless a request came from another channel. Required.',
    'DEBUG_CHANNEL': 'A channel of your own where the bot reports what it is doing - transfers, joins, bans, problems. Leave blank for none. Do not use a channel other people sit in: everything the bot reports goes there.',
    'MAX_DCC_SLOTS': 'How many files the bot sends at the same time. Everyone else waits in the queue. 3 is a good number for a home connection; raise it only if your upload speed can take it.',
    'MAX_USER_QUEUE': 'The most files one person can have waiting in their queue at once.',
    'MAX_GLOBAL_QUEUE': "The most files that can be waiting across everybody's queues put together.",
    'MAX_SEARCH_RESULTS': 'How many matching files are sent back to somebody who searches with @find. Each result is one line to that person.',
    'PAUSE_ON_UPDATE': 'While a rebuilt list is being swapped in - a few seconds at the end of !update - refuse searches and file requests. The rest of the rebuild, they are answered from the current list, which stays complete until the swap.',
    'PAUSE_FOR_WHOLE_UPDATE': 'The old behaviour: refuse searches and file requests for the whole rebuild, not only while the new list is swapped in. Only needs Pause sharing during !update on too.',
    'REHASH_TRANSFER_WAIT': 'When you rehash (reload settings), how many seconds the bot waits for running transfers to finish first before reloading anyway. 0 reloads straight away.',
    'DCC_BLOCK_SIZE': "How much data the bot hands to the network at a time during a send, in bytes. 65536 (64 KB) is already sixteen times mIRC's default and fine for almost everyone; making it bigger rarely speeds anything up.",
    'DCC_SEND_BUFFER': 'How much data the operating system may hold in flight for one send, in bytes. Leave at 0 to let Windows or Linux decide - that is right for nearly every connection. Only worth experimenting with on a very fast link to a distant downloader.',
    'DCC_PORT_START': 'The first port the bot listens on when sending a file. If you are behind a router, forward this whole range (start to end) to the machine running the bot, or nobody can download from you.',
    'DCC_PORT_END': 'The last port of that range. Each transfer running at the same time needs its own port, so keep at least as many ports as you have send slots.',
    'DCC_ACCEPT_TIMEOUT': 'How many seconds the bot waits for someone to accept a file it has offered before withdrawing the offer and counting one failed attempt. A person who has to click Accept in a dialog often needs more than the default 30.',
    'CHECK_FOR_UPDATES': 'Once a day, ask GitHub whether a newer DCCore has been released, and say so on the dashboard, in the console and in the mIRC window. Only the version numbers are compared; nothing about your bot is sent. Turn it off on a machine that should not go out.',
    'MAX_SEND_FAILS': 'How many times the bot retries sending one queued file if the download does not connect or fails, before dropping it from the queue and telling the person.',
    'FILE_DIRECTORY': 'The folder with the files you share. Used only if you have not added folders on the Library page - if you have, those are used instead and this is ignored.',
    'LIST_BASE_NAME': "The name your list files start with (for example DCCore-2026-09-18.txt). Normally the same as the bot's nickname, which is what happens if you leave it alone.",
    'LIST_FORMAT': 'How the list is sent to somebody who asks for it: as a plain .txt, packed as .zip, or packed as .rar. Zip is what most people can open. Rar needs the rar program installed.',
    'LIST_IGNORED_EXTENSIONS': 'File types to leave out of your list, separated by commas (for example .db, .ini). Everything else under your shared folders is listed and can be downloaded, so keep private files out of those folders.',
    'SEPARATE_VIDEO_LIST': 'Put films and series in their own list file, separate from the music, instead of one list with everything mixed. Both files are sent together when somebody asks for your list.',
    'LIST_VIDEO_EXTENSIONS': 'Which file types count as video and go in the film list (when the separate film list is on). Separated by commas.',
    'LIST_VIDEO_COMPANION_EXTENSIONS': 'File types that belong to a film and should go in the film list with it - subtitles, .nfo, .sfv - when they are in the same folder as a video. In a folder with no video (an album) they stay with the music.',
    'RAR_ENABLED': 'Let people request a whole folder packed as one .rar file (with !rar). Turn off if you do not have the rar program or do not want the bot packing folders. Single-file downloads work either way.',
    'RAR_EXTENSIONS': 'A folder can be requested as a .rar only if it contains one of these file types. The default is music formats, so albums can be packed but a folder with one big film cannot.',
    'RAR_BINARY': "Where the rar program is on this machine. Leave empty and the bot finds it by itself (on the PATH, or in WinRAR's folder on Windows).",
    'MAX_RAR_FOLDER_SIZE': 'The biggest folder the bot will pack as a .rar, in bytes. Stops somebody asking for a folder of hundreds of gigabytes. 10 GB fits any album or box set; 0 means no limit.',
    'RAR_TIMEOUT': 'How many seconds a folder may take to pack before the bot gives up on it.',
    'LIST_UPDATE_TIMEOUT': 'A hard limit in seconds on how long a list rebuild may run. 0 means no limit, which is the right choice: a huge library can genuinely take hours, and the setting below already catches a rebuild that has stopped doing anything.',
    'LIST_REBUILD_SCHEDULE': "Rebuild the list by itself, the same way !update does. Write daily 04:00, weekly sun 04:00, monthly 1 03:30 or every 12h (hours since the last rebuild, including yours). Empty: only when you ask. The bot's own clock; if it was off at that time, it rebuilds when it starts again.",
    'LIST_UPDATE_STALL_SECONDS': 'If a list rebuild reports no progress for this many seconds, it is treated as stuck and stopped. 15 minutes is generous on purpose so a slow network drive is not cut off.',
    'LIST_HEADER_FILE': 'A text file whose contents are printed at the top of your list - a greeting, your channel name, some ASCII art. If the file does not exist, nothing is added.',
    'LIST_HEADER_MAX_BYTES': 'The most of that file that will be used, in bytes, so a wrong file cannot bloat every list.',
    'MAX_FETCH_SLOTS': 'How many downloads FROM other bots you run at the same time. Separate from your own send slots, so your downloading never takes slots away from people downloading from you.',
    'AUTO_REFETCH_LISTS': "When another bot advertises that its list has changed, fetch the new list automatically. A list from a bot whose advert shows no date is fetched again once it is 14 days old. Off by default because it uses the other bot's bandwidth without you asking each time.",
    'AUTO_REFETCH_INTERVAL_HOURS': "The least time between two automatic asks for the same bot's list, in hours. Counted from the last list that arrived or the last time the bot was asked, so a bot that rebuilds hourly - or does not answer - is not asked every hour.",
    'AUTO_GRAB_LISTS': "Fetch the list of each bot that advertises one you do not have, one at a time: after a random wait, not if someone else just asked that bot, and at most 3 tries per bot. A list you remove is not fetched again. Off by default: it uses other bots' bandwidth without you asking.",
    'AUTO_GRAB_EVERY_MINUTES': 'The least time between two automatic list grabs, in minutes.',
    'AUTO_GRAB_MIN_FILES': 'Do not grab the list of a bot that advertises fewer files than this. 0 grabs any size.',
    'AUTO_GRAB_MIN_SPEED_KB': 'Do not grab the list of a bot that advertises a speed below this, in KB/s. A bot that shows no speed is not skipped. 0 turns this off.',
    'AUTO_REFETCH_MAX_PER_RUN': 'The most lists to re-fetch in one go. If many are out of date at once, the rest are picked up on later rounds, oldest first.',
    'FETCH_MAX_PER_BOT': 'How many files to ask one bot for at once. The next one is asked when one arrives. Servers allow each person only a few; asking for more gets "queue full". 0 means no limit.',
    'FETCH_QUEUED_TIMEOUT': 'When another bot puts your request in its queue, how long to wait for your turn before giving up. Busy servers take hours. 0 waits for ever.',
    'FETCH_OFFER_TIMEOUT': 'When you request a file from another bot, how many seconds to wait for it to offer the file before giving up.',
    'FETCH_TRANSFER_TIMEOUT': 'The longest a file download from another bot may take in total, in seconds, before it is abandoned.',
    'FETCH_FOLDER_OFFER_TIMEOUT': 'When you request a whole folder (.rar) from another bot, how many seconds to wait for its offer. Much longer than for a single file, because the other bot has to pack the folder first.',
    'FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED': 'The same wait, but for a bot that does not publish a folder list and probably cannot pack at all - shorter, so a slot is not held for half an hour waiting for nothing.',
    'FETCH_FOLDER_TRANSFER_TIMEOUT': 'The longest a folder (.rar) download from another bot may take in total, in seconds. Larger than the single-file limit because a packed discography is much bigger.',
    'MAX_FETCH_FILE_SIZE': 'The biggest single file you will accept from another bot, in bytes. Anything larger is refused before the download starts. 0 means no limit - except that a fetched list still may not unpack to more than 8 lists of the biggest size allowed, so a booby-trapped list cannot fill your disk.',
    'MAX_FETCH_FOLDER_FILE_SIZE': 'The biggest packed folder (.rar) you will accept from another bot, in bytes. 0 means no limit.',
    'MAX_FETCH_LIST_FILE_SIZE': 'The biggest list archive you will accept from another bot, in bytes. A real list is a few megabytes; this stops a bad offer sending you something huge. 0 means no limit.',
    'MAX_LIST_TEXT_SIZE': 'The biggest unpacked list you will read from another bot, in bytes. Every line of it is kept in memory, so this is a memory limit. 0 uses the default.',
    'FETCH_HISTORY_DAYS': 'How many days a finished download from another bot stays in the Downloads table. The downloaded file itself is kept regardless.',
    'FETCH_HISTORY_MAX_ROWS': 'The most finished downloads kept in that table, whatever their age.',
    'ANNOUNCE_INTERVAL': 'How often the bot posts its advert in the channel, in seconds. 300 is every five minutes. Do not go much lower - channels do not like a bot that advertises constantly.',
    'ANNOUNCE_TRANSFERS': 'Post a line in the channel each time a file has been sent. Everything else about a transfer is private to the person who asked; this is the only public part.',
    'BROADCAST_SEARCH_CHANNEL': 'The one channel used when you search all bots at once from the dashboard. Leave empty to use your first channel.',
    'BROADCAST_SEARCH_COOLDOWN': 'How many seconds must pass between two of those search-all-bots searches, to be polite to the other bots in the channel.',
    'CTCP_VERSION_REPLY': 'Answer when somebody asks the bot what software it runs (a CTCP VERSION request). The answer goes only to the person who asked. Turn off to stay quiet about it.',
    'MSG_DELAY': 'How many seconds the bot waits between the lines it sends to the server. Protects you from being disconnected for flooding. 5 is safe on Undernet; lower is faster but riskier.',
    'DEBUG_MSG_DELAY': 'How long to wait between lines to your debug channel. Never less than MSG_DELAY - every line the bot sends shares one clock, and a smaller number here has no effect. Set it higher than MSG_DELAY to slow the debug channel down further; 0 means the same as MSG_DELAY.',
    'THEME': 'The colour scheme for everything the bot says in the channel - the advert, the notices, the search results. Pick one you like; it is how people tell your bot apart from the others.',
    'CUSTOM_THEME_BORDER': "Override one colour of the chosen theme: the block that frames each message. Leave unset to keep the theme's own colour.",
    'CUSTOM_THEME_SEPARATOR': 'Override one colour of the chosen theme: the block between the parts of a message.',
    'CUSTOM_THEME_TEXTBOX': 'Override one colour of the chosen theme: the background the text sits on.',
    'CUSTOM_THEME_VALUE': 'Override one colour of the chosen theme: the numbers and names in a message, like a file count or a speed.',
    'CUSTOM_THEME_ALERT': 'Override one colour of the chosen theme: the parts meant to stand out.',
    'CUSTOM_THEME_ACCENT': 'Override one colour of the chosen theme: timestamps and secondary text.',
    'MAX_REQUESTS': 'How many commands - searches, queue checks and the like - one person may send within the time window below before they are muted. File requests are not counted: a pasted list is taken one by one, up to the queue limits.',
    'REQUEST_WINDOW': 'The length of that time window, in seconds.',
    'MUTE_TIME': 'How many seconds somebody is ignored after their first flood.',
    'FLOOD_BAN_SECONDS': 'How many seconds somebody is banned if they keep flooding while already muted.',
    'PRIVATE_MESSAGES_ENABLED': 'Keep private messages people send to the bot so you can read them on the Messages page. The bot does not answer them. Turn off to keep none and instead reply once telling the sender where to go.',
    'PRIVATE_MESSAGE_COOLDOWN_SECONDS': 'After recording a message from somebody, ignore further messages from them for this many seconds, so one person cannot fill the page.',
    'PRIVATE_MESSAGE_DECLINE_TEXT': 'The one reply sent when private messages are turned off. %admin is replaced by your admin nick. Leave blank to send nothing at all.',
    'PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS': 'How many seconds before the same person can get that reply again. One day by default - the reply is there so they learn where to go, not to repeat itself.',
    'PRIVATE_MESSAGE_DECLINE_BURST': 'The most of those replies to send in one burst window, across everybody. Stops a wave of messages from turning into a wave of replies that delays the transfers people are waiting on.',
    'PRIVATE_MESSAGE_DECLINE_BURST_SECONDS': 'The length of that burst window, in seconds.',
    'ADMIN_HOSTMASKS': 'Who may open the admin console over DCC chat, by host. On Undernet, log in to X with mode +x and use your yourname.users.undernet.org host - only you can have it. Empty means the console is off. Keep this in admin_config.py rather than here.',
    'ADMIN_CHAT_MODE': "How the admin console's DCC chat is connected. Auto is right for most people. Choose Listen if you are behind a VPN or a router that does not forward ports, so the bot waits for you instead of trying to reach you.",
    'ADMIN_CHANNEL_COMMANDS': 'Let the admin commands (!ban, !rehash, !update...) also work when you type them in the channel or a private message, not only in the console. With ADMIN_HOSTMASKS set they need your host as well as your nick; without it a stolen nick can run them, so turn this off.',
    'ADMIN_CHAT_COLOURS': "Colour the [SENT], [FAIL], [REQUEST] tags in the admin DCC chat the same way they are coloured in the debug channel, using your theme. Turn off if your client shows the colour codes as junk.",
    'WEBUI_ENABLED': 'Turn the web dashboard on. Off by default so nothing opens a web page just because the bot was updated. Needs the Flask package installed.',
    'WEBUI_HOST': 'Which addresses the dashboard listens on. 127.0.0.1 means only this computer can open it. 0.0.0.0 makes it reachable from other devices on your home network - never forward it to the internet, the connection is not encrypted.',
    'WEBUI_PORT': "The dashboard's port. Open http://127.0.0.1:8420 (or whatever you set) in your browser.",
    'WEBUI_CONSOLE_ENABLED': 'Show the Console page in the dashboard, which can run admin commands. Unset means on while the dashboard is reachable only from this computer, off otherwise - because on a network the dashboard is protected by the password alone.',
    'WEBUI_OPEN_BROWSER': 'Open the dashboard in your browser automatically when the bot starts. Only happens when the dashboard is limited to this computer.',
    'WEBUI_FOLDER_BROWSER_ENABLED': 'Show a folder picker on the Library page instead of typing paths. Off by default because it lets a logged-in dashboard user see the names of folders on this machine.',
    'CONSOLE_SHOW_REQUESTS': 'Show in the console who asked for which file.',
    'CONSOLE_SHOW_QUEUE': "Show in the console when a request goes into somebody's queue, and at which position.",
    'CONSOLE_SHOW_SENDS': 'Show in the console when a transfer starts, resumes and completes.',
    'CONSOLE_SHOW_FAILURES': 'Show in the console when a transfer fails, and why.',
    'CONSOLE_SHOW_SEARCHES': 'Show in the console who searched for what, and how many results they got.',
    'DEBUG_CHANNEL_FEED': 'Also send requests, queue positions, transfer starts and searches to your IRC debug channel. Off by default: every line to a channel takes a turn in the same queue as the adverts and replies people are waiting for. Finished and failed transfers go there regardless.',
    'DEBUG_MODE': 'Print every raw line the bot sends to the server in its own window. Very noisy - only for tracking down a connection problem.',
    'DEBUG_TO_CHANNEL': "Send the bot's running report (transfers, joins, bans, problems) to your debug channel.",
    'DEBUG_TO_CONSOLE': "Send that same report to the admin console and the dashboard's Console page.",
    'CONSOLE_TIMESTAMP_FORMAT': "The time shown at the start of every line in the bot's window. %H:%M:%S is hours:minutes:seconds; use %Y-%m-%d %H:%M:%S to include the date; leave blank for no time.",
    'PROJECT_URL': 'Where DCCore comes from. Shown at the top of your list and in the reply to a version request.',
    'TMP_ZIP_DIR': 'Where packed folders and list archives are built before sending. Cleaned up after each transfer.',
    'LOCAL_LIST_DIR': 'Where your published list files are kept.',
    'FETCHED_FILES_DIR': 'Where files you download from other bots are saved. Kept separate from your shared folders so they are not offered to others.',
    'BANS_FILE': 'Where timed bans are saved.',
    'HARD_BANS_FILE': 'Where permanent bans (from !ban) are saved.',
    'DCC_QUEUE_FILE': 'Where the per-user send queue is saved. The single-instance lock lives beside it.',
    'STATS_FILE': 'Where the lifetime totals, the speed record and the daily figures are saved.',
    'KNOWN_BOTS_FILE': 'Where the bot remembers the other bots it has seen advertising.',
    'FETCHED_BOT_LISTS_FILE': "Where the bot remembers which other bots' lists it holds.",
    'LIST_SHOW_AUDIO_INFO': 'Add each MP3 and FLAC file\'s length and quality after its size in your list, e.g. 10.3MB 4m31s 320/44.1/JS. Every audio file has to be read once: on a large library, or one on a network drive, that takes several rebuilds, each limited by the reading time set under List rebuild. After that only new files are read.',
    'LIST_SCAN_THREADS': 'How many folders are listed at once while the list is rebuilt. On a network drive most of the time is waiting, so more at once makes every rebuild shorter - and searches wait while it runs. 1 lists one folder at a time. 1 to 64.',
    'LIST_AUDIO_INFO_THREADS': 'How many audio files are read at once for their length and quality. On a network drive most of the time is waiting, so this is 64 by default; lower it only if a slow or small setup does not benefit. The rebuild says the rate it got, to compare. 1 to 128.',
    'LIST_AUDIO_INFO_MINUTES': 'The longest one rebuild spends reading audio files it has not read before, since searches wait while it runs. Past it, the list is published and the rest are read by the next rebuild. 0 means no limit.',
    'LIST_AUDIO_INFO_CACHE': 'Where the length and quality read from your audio files are kept between rebuilds. Safe to delete; the next rebuild reads every file again.',
    'LIST_INDEX_FILE': 'The search index over every list you have fetched from other bots. Can be large; safe to delete, it is rebuilt at the next fetch.',
    'FETCH_HISTORY_FILE': 'Where finished downloads from other bots are recorded for the Downloads page.',
    'DOWNLOAD_COUNTS_FILE': 'Where the count of how often each file was sent is kept, for the Most downloaded table.',
    'LIST_SIZE_FILE': "The name of the small file written beside your list holding the library's total size, as shown in the advert.",
    'LIST_RAWBYTES_FILE': 'The name of the small file beside your list holding the exact byte total.',
    'LIST_PROGRESS_FILE': 'Where a running list rebuild reports its progress for the dashboard.',
    'LIBRARY_FOLDERS_FILE': 'Where the folders you added on the Library page are saved.',
    'LISTS_FILE': 'Where your list definitions are saved, if you serve more than one list.',
    'ADMIN_TOKENS_FILE': "Where the login tokens of scripts paired with the admin console are kept (hashed, like the password). Made by the console's pair command; remove one with unpair.",
    'ON_CONNECT_FILE': 'Where the commands sent on connect (such as your X login) are saved.',
    'NOTICES_FILE': 'Where the notices shown on the dashboard - a lost connection, a kick or a refused join, a failed list rebuild - are saved.',
    'PRIVATE_MESSAGES_FILE': 'Where private messages to the bot are saved.',
}
