# Roadmap

What DCCore does today, and what it does not do yet.

**How to use this file:** everything under *Implemented* is in `main` and working. Everything under *Planned* is not, whatever a comment or an old changelog entry might imply. When something is built it moves up, in the same commit that builds it — a roadmap that is edited afterwards is a roadmap nobody trusts.

---

## Implemented

### Serving files

- **DCC SEND over IRC**, with a per-user and global queue, configurable slot limits, and a DCC port range you control.
- **The list goes first** — `@nick` takes the next free slot ahead of queued files and folders, so a newcomer does not wait behind everyone's albums just to see what the bot has. It never interrupts a send, a nick still has one send at a time, and only one list at a time may pass nicks that waited longer, so a flood of list requests cannot starve the files. A list is not its nick's turn: the files that nick was waiting for keep their place (#1205).
- **Album packing** — `!rar <folder>` builds an archive on demand and cleans it up afterwards, bounded by `MAX_RAR_FOLDER_SIZE` so a request cannot ask for an unbounded pack.
- **Requests by private message** — `!<nick> <file>` sent privately is answered only for somebody in one of the channels the bot serves, whether a slot is free or not. It belongs to the first such channel they share with the bot: the console, the `@DCCore` window and the debug channel name it, and its channel mode decides the notices. Nothing about it is announced in any channel, and it never appears on the Messages page (#1242).
- **Freeze box** — a user who parts or quits keeps their queue for five minutes; rejoining thaws it instantly rather than losing their place.
- **Anti-flood** — a rolling window on searches and other commands, temporary mutes, and escalation to a timed ban (`FLOOD_BAN_SECONDS`, one hour by default) for anyone who keeps going while muted. File requests are not metered: a pasted list is taken one by one, bounded by the queue limits instead.
- **Ban list** — hard bans by hostmask pattern, timed bans, and a guard that refuses a pattern matching everyone.
- **Long paths and non-ASCII filenames** work on both platforms: Windows `MAX_PATH` is handled throughout, and Greek, Cyrillic and CJK filenames survive both ends of the IRC connection.

### The file list

- **One master list**, rebuilt by `!update`, from the dashboard, or by itself on a schedule (`LIST_REBUILD_SCHEDULE`: daily, weekly, monthly or every N hours), published atomically so a failed scan never overwrites a good index.
- **Three formats** — `.txt`, `.zip` and `.rar`, all built every time; `LIST_FORMAT` picks which one is offered.
- **Search** — `@find <words>` against the master list, with results fitted to the IRC line limit. Words in quotes must appear together, in that order: `@find "metal church" 1986`. With `SEARCH_SHOW_FOLDER` on (off by default), the reply names each result's folder on a `From:` line of its own above its files: one line per folder, cut from the left past `SEARCH_FOLDER_MAX_CHARS`, with the result lines unchanged.
- **Searching and downloading go on while the list rebuilds.** The new list is built beside the one people already have, and the bot pauses only for the few seconds it takes to swap it in (`PAUSE_ON_UPDATE`; `PAUSE_FOR_WHOLE_UPDATE` brings back the old whole-rebuild pause).
- **Several folders scanned at once** (`LIST_SCAN_THREADS`, 16 by default), which is what makes a rebuild over a network drive shorter.
- **Length and quality in the list, if you want them** (`LIST_SHOW_AUDIO_INFO`) — every MP3 and FLAC row gets its duration and bitrate after the size, `::INFO:: 10.3MB 4m31s 320/44.1/JS`, read once per file and remembered.
- **Every folder heading says what it holds** — `14 files, 1.20GB` on its own line under the heading, placed so that every program that reads these lists (other DCCore bots, AutoQ, DCCore's own request handling) ignores it. Companion files (`.srt`, `.nfo`, `.sfv`…) travel with the film they belong to when the video list is split out, and stay with an album otherwise.
- **A partially unreadable library fails the rebuild** rather than silently publishing a truncated list.

### Multiple lists, and multiple folders per list

Done, all five stages - kept here with the design it was built to, because the reasoning is what a later change has to argue with.

The largest gap against OmenServe, which has had both since long before this project started. DCCore now serves **several** directories into **one** list, and **several** lists, each bound to its own channels — #26 below is complete.

`SEPARATE_VIDEO_LIST` is not that feature and does not pre-empt it: it splits one scan's output by content type, where this splits by folder set and binds each list to a channel. An operator whose film and music already live in separate folders wants this one, and turns that switch off.

The design is settled:

- One trigger, unchanged. `@<botnick>` everywhere; no new syntax for anybody to learn.
- A list has an operator-facing name and a set of directories. Names are never typed in a channel.
- A channel is bound to exactly one list; a list may serve several channels. A request uses the list bound to the channel it arrived in.
- A private message uses the list marked primary, since a PM carries no channel.
- A channel with no list bound gets nothing: no advert, no requests answered.

**Multi-folder is done.** `library.py` answers which folders and in what order, resolution reads a folder's label out of a heading, and the scan builds one list from all of them. The Settings page landed with it — a reorderable list with a validated add and a folder browser (`GET`/`POST /api/folders`, `GET /api/folders/browse`), so `data/library_folders.json` no longer needs hand-editing.

Multi-list then follows: allow more than one list object, with per-channel adverts falling out nearly free. The folder set moves inside a list at that point, which is why every caller goes through one accessor rather than reading a setting directly — the move rebinds the accessor instead of touching 54 call sites a second time.

**Stage 1 is in.** `library.ServedList` is the list object — a name, the folders it is built from, the channels it answers in, and which one is primary — stored in `data/lists.json` and read through `lists()`, `primary_list()`, `list_for_channel()` and `list_by_name()`. `folders()` now takes an optional list name and defaults to the primary's, so all thirteen existing callers are untouched and, with no `lists.json` on disk, every install resolves to one implicit list over exactly the folders it served before. Nothing the daemon does has changed yet.

**Stage 2 is in.** A list's files live in its own directory: the primary keeps `LOCAL_LIST_DIR` itself — so nothing moves and no upgrade migrates anything — and every other list gets a subdirectory named after it. The list is in the *path*, not the filename, so the `-RAR-`/`-VIDEO-`/`-FULL-` markers and everything that parses them are untouched. `generate_master_list()` takes a list name and `generate_all_lists()` builds every one, each independently: one failing does not stop the rest, and the failures are named.

**Stage 3 is in.** A request is answered from the list bound to the channel it arrived in. The rule has three parts: an explicitly bound channel gets its list; otherwise the primary answers *if it binds no channels of its own*, which is what every install today is and what stops this being an upgrade that silences every bot; otherwise nothing, which is what makes binding mean something. A private message is the primary - it carries no channel to route on - except that a `!rar` row copied from another list's advert carries its folder label, and that routes it to the list the label belongs to (#653). The list request, `@find` and file requests all route; a channel bound to nothing is answered with silence rather than an error, because an error implies something went wrong and nothing did.

**Stage 4 is in.** Each channel advertises the list it actually serves. The advert loop already read the figures once per channel — it just read the same ones every time — so this is the loop asking which list first, and skipping a channel with none bound. The advert is where the multi-list rule is most visible: a bot silently present in a channel it does not serve, rather than one announcing a library it will refuse to send from.

**Stage 5 is in, and #26 is complete.** The Settings page's Paths category defines them: a name, which channels it serves, its folders, and which one is primary. With one list the familiar folder editor stays exactly where it was and a button moves you to the list editor — one editor, never two, because a second place to edit folders that quietly does nothing is worse than either alone. `GET`/`POST /api/lists` validate the whole set and report every fault at once.

Two pieces were worth doing carefully rather than quickly, and one of them turned out the opposite way to what this section used to predict:

- **Containment.** This said `is_safe_path()` would become "inside *any* configured root", and called that the one place a mistake is a security bug rather than an inconvenience. Right about the risk, wrong about the answer: widening it that way is a strictly weaker test. Because a heading names its own folder, resolution returns *which* folder it landed in and the check runs against that one — the same strength as when there was only ever one. `is_safe_path()` itself was never touched.
- **Index identity.** Two folders can hold the same relative path — the same album in flac and in mp3 is the ordinary case — so an entry has to record which folder it came from. That is the label leading every path, and it is what makes the containment answer above possible.

### Receiving files from other bots

- **Cross-bot fetch** — request a file with `!<bot> <filename>` or a whole list with `@<botnick>`, and track it from the dashboard.
- **Broadcast search** — send one `@find` to a channel and collect every bot's reply, grouped under each bot's own parsed header.
- **Bots running mxrarserver 2.x** (#1209) — its advert puts the bot in the List Browser with both list counts, and requests go to the trigger it advertises (or that its list's rows carry, for a bot in "request only" mode), not necessarily its nick. Its RAR lists are opened and named "Files" and "Folders"; a row of its Folders list is offered as **Get folder as RAR**, asked for exactly as the list wrote it, and the RAR it sends back is recognised by the name mxrarserver gives it, so several can be waiting on one bot at once. A list it takes longer than a minute to build is still taken when it arrives.
- **Both DCC directions** — active and passive/reverse SEND, since bots behind NAT use the latter.
- **Hostile-input handling** — every other bot is treated as untrusted: admission control, size caps, and zip-slip / zip-bomb guards on any archive received.
- **Their answers are understood** — what OmeNServE, SDFind, SpR, BWI, mxrarserver 2.x and DCCore itself reply to a request. A queued request shows its place in their queue on the Downloads page and waits for its turn (`FETCH_QUEUED_TIMEOUT`, 12 hours by default); "I don't have that file" or "queue full" ends it at once, in the server's own words.
- **The download queue looks after itself.** A file queued from a bot that is offline waits, and is asked for a minute after the bot comes back. Only `FETCH_MAX_PER_BOT` (3) are asked of one bot at a time, the next going when one arrives, so a big selection does not earn "queue full". A "busy" answer is asked again, three times, ten minutes apart. Unfinished downloads survive a restart.
- **A bot that cannot be reached is paused**, after three failed connections in a row, with a **Resume** button on the Downloads page, instead of failing file after file. A bot that keeps failing in other ways - no answer, a download that breaks off - rests for a while (#1210): after `FETCH_BOT_MAX_FAILS` (3) failed requests in a row it is paused for `FETCH_BOT_COOLDOWN_MINUTES` (15), its requests waiting rather than failing, and the Downloads page says "paused until 14:32 after 3 failures", with **Resume now**. "Not found" and your own cancels do not count, a finished download starts the count again, and the pause survives a restart. When the drive fetched files go to has less than 200 MB free, downloads wait and carry on by themselves once there is room.
- **The List Browser** — every list you have fetched, one row per bot, with a tab for each list its archive holds (music, RAR, video) rather than only the largest. A light says whether the bot's advert shows a newer list than yours; a list you have not opened yet says *New*; click an online bot and its advertised free slots, queue and speed appear under it. One filter searches every held list at once, from an index built as each list arrives, and **Online only** takes the bots that are not here off both the sidebar and the search. A bot that never advertises can be added by hand, and a list can be fetched again, or removed, from its own row.
- **Held lists can keep themselves up to date** (`AUTO_REFETCH_LISTS`, off by default): a list is fetched again when its bot's advert shows a different date or file count, or - for a bot whose advert shows no date - once it is 14 days old. Never more often than `AUTO_REFETCH_INTERVAL_HOURS`, and at most `AUTO_REFETCH_MAX_PER_RUN` at a time; only bots in one of your channels are asked, so lists of bots that have left never hold up the rest.
- **Lists can be grabbed by themselves** (`AUTO_GRAB_LISTS`, off by default), on AutoGet's rules: the list of a bot you have none from is asked for one at a time, at most one every 10 minutes, after a random 5-360 second wait, not at all if someone else just asked that bot, and at most three times per bot, 30 minutes apart - counting only requests that went out, and starting over once a list arrives. Small, slow and "servers only" bots can be skipped, and a list you removed is not grabbed back until you fetch it yourself.

### Operating it

- **Authenticated admin console over DCC CHAT**, gated on the operator's services host *and* a PBKDF2-hashed password. Read-only commands (`status`, `queue`, `slots`, `bans`, `uptime`, `version`) and action commands (`ban`, `unban`, `clearqueue`, `rehash`, `update`) — see [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md). The feed's tags are in the bot's own theme colours (`ADMIN_CHAT_COLOURS`), and the diagnostic channel commands (`!ping`, `!debugnames`) answer only the bot's own admin, so two DCCore bots in one channel never answer each other's operator.
- **A structured feed for scripts, and the bot's own window in mIRC.** A console session that says `hello <client> <version>` gets every event as one `DCCORE <TYPE> <fields…> <free text>` line — requests, queue positions, sends, resumes, completions, failures, searches, the rest as `LOG` — plus a `STATUS`/`SLOT`/`QUEUE` burst after every change and every 30 s (the heartbeat). `pair` mints a login token for a script that opens the console and nothing else. `scripts/mirc/dccore.mrc` (mIRC 6.10+) draws it all: the feed coloured per kind, a side panel with what is sending and who is waiting, the slots and today's totals in the title bar, console commands typed in the window, an options dialog, a Downloads window with what the bot is fetching from other bots, and a list rebuild's progress however it was started; `/dccore pair <bot>` once, then it logs in by itself.
- **DCCore Chat** - public chat with other operators in the channels the bot is in, over an ordinary channel message (not a NOTICE, which channel bots kick for) starting with a neutral `[ServersChat]` tag, relayed by the bot: the operator types in the mIRC window script's chat window, the bot says it (`chat * text` on the console) in the fewest channels that reach the other DCCore bots, which it finds by their `DCCore/sc` realname with WHO, and tagged lines from those bots come back over the structured feed. Its own window, which says it is public; channels picked from its right-click menu; a per-nick flood limit and a cap on what is sent; colours stripped; the last 50 lines in memory only for a window that reconnects; nothing ever sent automatically, and nothing written to disk. See [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md).
- **Optional web dashboard** — Search, Queue, List Browser grouped by folder, Downloads (sorted, paged, cleared in a click, with a count of the download queue and Cancel on every request not yet started), Live Transfers (speed, slots, the queue and when the next slot is likely to free up, as they happen), Stats with the transfer record and one period for the whole page, a duplicate-filename verifier (which the build now warns about too, for operators who never open the dashboard), a list rebuilder, a Settings page, and a Console (the DCC CHAT admin console's commands and live log, in the browser — for an operator who wants neither a second IRC client nor a debug channel). Off by default, loopback by default, behind the same password as the DCC CHAT console.
- **Purging a fetched list** - the manual half of the fetched-list purge, in two shapes: one bot at a time from its own list, and every offline bot at once from the toolbar. Removes the entry, the extracted files and the search index rows together. An automatic TTL is still open and deliberately second: `fetched_at` answers list staleness, not "this bot is gone", and a timer that deletes an operator's data by default is a surprise waiting to happen.
- **Messages people send the bot** - a private message that is not a command gets no reply, and now leaves a record: a Messages page with an unread count, throttled per sender. The bot still never answers. Turning it off (`PRIVATE_MESSAGES_ENABLED = false`) keeps nothing, hides the page and its menu entry, and tells the sender once where to go instead - a NOTICE, once per person per day, under a burst ceiling, on the ordinary send lane.
- **A notice badge in the dashboard** - the short list beside the long one. Kicks, channels given up on and failed rebuilds raise a counted, colour-coded notice in the status panel, kept across restarts; everything else stays in the Console's log where it belongs. See [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md).
- **The launcher is the install.** Extract, double-click (`start-dccore.bat`, `start-dccore.sh`, `start-dccore.command`): the first run opens a setup page in the browser — nickname, server, channels, your nick, the password, the music folder, the dashboard, each with the same **?** explanation the Settings page has, in English, French or Spanish — loopback-only, one-shot, behind a one-time code in the link, and the bot starts the moment you save. No Flask, or no browser: the same questions in the terminal (`configure.py`). On Windows with no Python at all, the launcher offers to download python.org's installer, checks it against a fingerprint pinned in the script, and runs it with both boxes ticked. Every run after that checks the setup and starts the bot.
- **Starting with the system, the firewall, the router.** `install-autostart` scripts for Windows (Task Scheduler), Linux (a systemd user unit) and macOS (launchd), each with a remover, each running the launcher so the working directory is right; `allow-firewall.bat` adds the Windows rule for the bot's ports and the setup check names the `ufw`/`firewall-cmd` lines on Linux; port forwarding explained in plain words in both guides.
- **Pre-flight check** — `start-dccore.sh check` verifies the setup without opening a socket, and says what stands between the bound ports and the outside on this OS.
- **It tells you when a new version is out.** Once a day (`CHECK_FOR_UPDATES`) it asks GitHub for the latest release - one request, carrying nothing about your bot - and says so in the dashboard's sidebar, the console's `status` and the mIRC window, each with a way to check now.
- **Every setting explains itself.** The **?** beside every setting on the dashboard — and the comment above it in `settings.conf.sample` — is written for the person running the bot, in English, French and Spanish; the dashboard itself is translated the same three ways.
- **Two configuration mechanisms** — `admin_config.py` for Python, `settings.conf` for plain text; the dashboard and console both write to the latter.
- **`!rehash`** reloads code and settings live, preserving queues and transfer state.
- **Channel adverts** on a timer, with a per-bot theme (five presets, or your own colours).
- **Statistics** — totals, today and yesterday, per-file download counts, a speed record, and a live rate.
- **A record of every finished transfer** (`data/transfers.db`), with the nick it went to or came from and no host or channel: shown on the Stats page for 24 hours, 7 days, 30 days or all time - the files sent most, speeds, the wait in the queue, the nicks sent to and received from most, and how transfers ended - completed, failed or cancelled, with a success rate per kind - with one nick or everyone forgotten on request (wiped from the file, not just unlinked) and a CSV export.
- **History brought across** from OmenServe and KeepTrack: lifetime totals from mIRC's `vars.ini` (where both counted the same sends, the operator picks one) and KeepTrack's per-nick history from `KTData.txt`, hosts never kept.
- **Running without its window.** `BOT_WINDOW` starts it minimised or with no window at all on Windows; everything the window would show also goes to a log file (`data/logs/dccore.log`, rotated); and it can be stopped without the window - `start-dccore stop`, the dashboard's Tools page, the console's `shutdown now`, the mIRC menu - each the same clean shutdown as Ctrl-C.
- **The on-connect commands are checked.** If they set a user mode such as `+x`, the bot checks after joining that it took (for `+x`, that the host really is hidden) and sends them again if not - a net split can swallow an X login; **Resend commands** on the dashboard sends them at once.

### Quality

- **9036 tests**, on Linux, Windows and macOS, Python 3.10, 3.12 and 3.14, in CI on every push and pull request — and a preflight script that runs the whole suite twice, the second time with the host's own tooling hidden, so a test that only passes on a developer's machine fails before it is pushed.
- **Stdlib-only** — the daemon and its test suite need no third-party packages; Flask is required only for the optional dashboard.
- **No reloaded module owns a lock** — `!rehash` re-executes a module body, so a module-level `threading.Lock()` is rebound while a thread is still inside it. Every lock in a reloaded module is allocated in `runtime.py` and bound by name, and `tests/test_no_reloaded_module_owns_a_lock.py` fails if a new one appears — the class, not the four instances that prompted it.
- **A cross-list search index** — SQLite FTS5, built as each bot list is fetched, so the dashboard can filter every held list live rather than re-reading them at 2-11 seconds a keystroke.
- **Seven adversarial audits so far** — an internal audit (32 defects), a pre-publication sweep, the v1.12.0 audit (44 findings), the 2026-09-20 audit (145 findings, twelve independent lenses), and three since over the new work: 2026-09-27 (26 findings), 2026-10-01 and 2026-10-02 (11) — all fixed or otherwise resolved. See "From the audits" below for the two most recent.

---

## Planned

Ordered by what unblocks what, not by preference.

### Test coverage where it is thinnest

The pre-publication audit found **21 daemon functions with no behavioural coverage at all** — `!rehash`, `@<nick>-que`, the advert worker, the IRC read loop, `configure.py`'s entry point. That gap is closed: `scripts/function_coverage.py` reports **2 of 310 uncovered, both on the allowlist with a written reason**, and it fails the build on a third.

What remains is narrower and does not show up in that number. Several "the wiring is in place" guards read the source as text rather than executing it, so a call moved behind a disabled branch would still not be noticed — this file's own history has three such guards that passed against deliberately broken code, and the v1.12.0 work found three more the same way, each caught by mutation rather than by review.

**The largest single gap is closed.** `_handle_rehash_request()` — seven hundred lines that nothing in 120-odd test files had ever executed, while the dashboard triggers it on every settings save — now runs for real in `tests/test_rehash_end_to_end.py`, in a subprocess so the reload cannot touch the runner's own imports. It asserts what survives: the changed setting, a user's queue, the channel lists, a timed ban, a freeze timer, the advert token, and that the bot is not left paused. Re-measured: it was **four**, not nine, and they now have HTTP-level tests (`tests/test_every_route_is_behind_the_login.py`). Their builders had always been well covered — six to twelve tests each — so what was missing was the wiring in front of them: that the path resolves, that the method restriction is real, and that the JSON envelope comes back.

The same file closes a larger gap found alongside it. All 37 dashboard rules sit behind one `before_request` hook and not a single per-route decorator, which is the right design — a decorator is a thing somebody can forget — but it put the whole authentication story on one function that nothing tested as a whole. An audit probed every rule unauthenticated and found none reachable, so it held; now a test walks `url_map` itself, so a route added tomorrow is covered without anyone remembering the test exists.

### From the audits

**The 2026-09-20 audit is the largest yet, and it too is fully closed.** Twelve independent auditing lenses over `main`, each finding attacked by an independent skeptic told to refute it: 145 findings after dedup, 0 critical (27 high, 62 medium, 53 low, after severity correction), 3 refuted outright. The other 142 - 139 confirmed and 3 the skeptic could not rule out either way - each got its own issue and its own fix: 140 closed by a direct fix on `main`, and 2 closed as duplicates once tracing them down turned up that a different auditor had already found and fixed the same bug under a different number (the write-up on each says which one). Every fix is written up in `docs/UPDATES.md` with its failure scenario, the same discipline the v1.12.0 audit below established - and one of the duplicate write-ups spawned its own follow-up (#811, the setup page asking for the operator's `+x` host), filed separately so the part beyond the original finding is not lost.

**The v1.12.0 audit left the list this section used to ask for.** Six independent lenses — security, concurrency, the transfer path, lists, the IRC surface, and persistence — each adversarially refuted before anything counted, followed by a second, pre-publication sweep of the actual public export itself. **44 findings confirmed, and all 44 are closed: 42 fixed, 2 recorded in tests as considered and deliberately not changed.** Every one is written up in `docs/UPDATES.md` with its failure scenario, so the next audit starts from a record rather than from "roughly forty".

Two of the two-not-changed are worth knowing about before somebody "fixes" them: a bot-alone `list` row may claim an offer that was a near-miss for a `file` row from the same bot (refusing the fall-through would reject legitimate list replies), and the passive DCC reply goes through the outbound pacer while the accept clock runs (sending it unpaced is what `queue_mgr` exists to prevent; raising `PASSIVE_LISTEN_TIMEOUT` is the lever with no such risk).

- **The earlier audits' findings still were never written down.** That said "roughly forty" and named two, one of which is fixed. The rest can be neither confirmed nor worked from, and a great deal has been fixed since. Still treated as unknown rather than as a backlog — but it is now the only part of the audit history that is.

### Splitting the largest functions

The 2026-10-03 performance audit found three places that have grown past what one review can hold. None of them is slow. The cost is that most changes land in the same few hundred lines, and many tests can only check them by reading the source as text. Kept out of 1.15.0 so that release stays calm, and to be done later as small extractions that change no behaviour, one per pull request (was #1150).

- **`irc_loop` in `irc.py`**, about 1,450 lines: every server line goes through one loop body full of closures over loop locals. The proposal is a dispatcher: a connection-state object holding the socket, the epoch and the joined channels, and one top-level handler per kind of line (nick refused, welcome, names, join/part/quit, private message and CTCP). The order of today's if/elif checks is part of the behaviour, and a dispatch table must keep it. Start with one leaf handler (the 433/432/437 block) and see whether it pays before committing to the rest.
- **`dcc.py`'s three handlers**: `start_dcc_send`, `handle_download_request` and `check_queue_and_send` are half the file. First step, inside `dcc.py` so `!rehash` and the tests' patches keep working: pull out the path resolver in `handle_download_request` (where a real bug sat), the inline RAR packer, and the choice of the next user. A later, optional move of the lookup memory into a module of its own must add it to `commands.CORE_MODULES` before `dcc`, and its locks stay in `runtime.py`.
- **`webserver.py`**, about 5,800 lines and the most-edited Python file, splits along its own sections: stats, lists, settings, the web login with the console, and the setup page. `webserver.py` keeps `start()`, `create_app()` and the routes.

To settle before each step: the tests that would guard it are the source-reading tests it has to rewrite, so each extraction turns them into real calls in the same pull request and mutation-checks them. Patching `webserver.x` does not reach code moved to another module, so each move rewrites its patch targets too. And each piece waits for a moment when no open pull request edits the same function, since a moved block conflicts with every open edit to it.

### Knowing which bots are out there — open questions

The List Browser's source list is built from two things: lists we have
fetched, and `runtime.known_bots`, which is filled from the periodic advert
every serving bot sends in the channel. Both of those assumptions have holes,
and neither has been decided yet.

**A bot's nick is not a stable identity.** When a bot loses its connection and
comes back on its alt nick it advertises under the alt nick, so it appears in
the registry — and in the sidebar — as a second bot. The freshness comparison
then has nothing to compare: the advert we recorded at fetch time is filed
under one nick and the advert arriving now under another, so a list that is
perfectly current reads as "cannot tell", and re-fetching under the alt nick
gives a second copy of the same library.

The list *contents* do not follow the nick either. A request line inside a
fetched list is `!nickname <track>`, written by that bot when it built the
list, so it still names whichever nick was current at build time. Copying that
line into the channel sends the request to a nick that may not be there.

We had the same problem in the other direction, and it was not hypothetical:
`update_list.py` wrote `!{config.NICKNAME}` into every line of our own list at
build time, and `irc.py` rebinds `config.NICKNAME` to the alt nick on a 433, so
a rebuild while we were on the alt nick shipped a list whose every line named
the alt nick. **This half is fixed (#376):** the list now names the configured
nick (`ORIGINAL_NICK`, falling back to `NICKNAME`) however it is built, live or
from a subprocess, so it survives the bot being on its alt nick at rebuild
time.

**The sidebar half is done, for display only.** A bot seen under two nicks is
one row, shown under the nick it has now, with the other named in the row's
tooltip. Two kinds of evidence count. A NICK message from a known bot is proof.
The other is its **ident**, together with the same advertised file count, an
old nick whose QUIT, PART or NICK was actually seen, and the two never
advertising at the same time. The ident is held in memory only: never written,
never logged, gone on restart. No host or IP is kept at all. Anything short of
all of that leaves two rows. Saved lists, the registry and the counters stay
keyed per nick, so a wrong merge could only ever mis-group a row.

**A bot that never advertises can now be added by hand.** A bot that answers
`@nick` and `!nick <track>` perfectly well but never advertises in a shared
channel used to have no row at all — `known_bots` is built only from
adverts, so there was no way to fetch its list from the dashboard. The List
Browser's sidebar now has an **Add a bot that does not advertise** box: the
nick goes into the same registry, flagged `hand_entered`, so every reader of
the sidebar treats it exactly like an advert-built row — presence and
freshness both read "cannot tell" until a NAMES sync or an advert says
otherwise (no WHOIS probe, on purpose, the same rule everywhere else in the
sidebar follows), a prune never ages it out, and if the bot ever does start
advertising the two rows merge into one rather than staying doubled.
Deliberately not probed for reachability at add time — the opposite case
this used to also name, a bot that advertises but does not answer, is left
exactly as unaddressed as it was: adding a nick by hand does not claim it
works, only that the operator says it exists.

### Installing and updating

What is left of the first-timer install work (was #547). The launcher, the Python download on Windows, the setup page in the browser, and the firewall and autostart scripts are in (see *Operating it* above). With them a first-timer on Windows extracts, double-clicks, says yes twice and fills in one page. The three items below were parked on purpose, not refused: the next step is to see how a real first-timer gets on with what is there, and to reopen these with what that run showed.

- **One release zip with the launchers at its root.** GitHub's automatic source zip puts the launchers inside a versioned folder, a level down. Instead, one `DCCore-<version>.zip`, the same file for every platform, with the three launchers and a ten-line README-FIRST at its root. Release-process work only, no daemon code.
- **A Windows zip with Python inside**, as an extra download beside the universal one, never instead of it. Python's own embeddable package keeps a real `python.exe` and the source on disk, so `!rehash`, `!update` and `admin_config.py` keep working where a frozen executable would break them. It costs vendoring Flask and its dependencies at release time, a release-build script, and tests that prove those three hold in that layout. Windows only, because python.org publishes the embeddable build for Windows only. A project decision before it is code.
- **Updating from the dashboard.** An update today loses nothing, since everything the operator owns is gitignored, but every step is manual: new settings go unnoticed, a release that changes the list output keeps serving the old list until a rebuild, and the backup is the step that gets skipped. The check for a new version is already there (`CHECK_FOR_UPDATES`); the idea is an Update card that acts on it. It would back up `data/`, `settings.conf` and `admin_config.py`, unpack the release (or `git pull` in a clone, refusing a clone with local changes), restart through the launcher, then name the settings that are new and offer a list rebuild when the release changed the list. Rollback from the backup, and never while a transfer is running ("update when idle"). To settle: how a release marks that the list changed (a version constant in the list builder is the honest source), and a restart that does not replace the process in place, which on Windows leaves the launcher thinking the bot stopped.

### Scripts that drive the bot

The structured feed, `pair` tokens and the mIRC window script are in (see *Operating it* above). The idea behind them is that an operator's own scripts drive DCCore over the admin console, from any client and in any language, rather than DCCore running their scripts. What is left was never decided, and the open question for all of it is whether it is worth doing before anyone asks (was #568):

- **A stability contract.** The protocol has a major number in `HELLO` and a rule that a field added at the end of a line is a minor change, but no document says which commands and lines a script may rely on. Writing one is a promise that a script written today still works in a year, which is the expensive half.
- **A second client**, small and not mIRC (a page of Python, or shell over `nc`), to prove that the protocol is the surface and not the one script.
- **Token scope.** A paired token opens the whole console, `ban`, `rehash` and `update` included. A read-only token (the feed and `status`, no action commands) would let a stats script or a phone widget hold a credential that can do no harm. This one has a security argument of its own, so it is the first to reopen.
- **A narrow interpreter** for simple `on TEXT` triggers, ranked well below the rest: it covers only the scripts that are easiest to rewrite anyway, and if it is ever built it must refuse loudly whatever it does not understand rather than skip it.

**The mIRC window script with more than one bot** (was #1111). `dccore.mrc` serves one bot at a time: one bot alias, one pair of hash tables, one `@DCCore` window, global timer names and one client token. The bot side needs nothing, since several bots connected from one mIRC are just several clients to them. A way in without one big rewrite:

1. No behaviour change: route the direct writes to the live-state table through a setter, and name the windows and timers through helpers.
2. Per-bot storage, still with one bot: the helpers read a current-bot context set from the event or the window, the tables and the settings become per bot, and the old settings file is migrated the first time it is read.
3. Several bots: a bot list, a window and timers per bot, a menu to pick where a typed command goes, and a client token per bot so one bot's token never reaches another.

To settle first: whether the bots share a network, which decides whether a bot is keyed by its nick alone or by network and nick. The real cost is testing, not code: CI has no mIRC, so every step needs a run in a real one, best by someone who runs two bots. Worth it if two-bot operators turn out to be common, or alongside the second client above; step 1 alone only if the script is being refactored anyway.

### Smaller things worth having

- **PER-LIST file exclusions** (`Exclude = .mpu,.db`) — OmenServe has them per list. `LIST_IGNORED_EXTENSIONS` does this globally; scoping it to one folder is the part still missing.
- **Multi-network** — real in OmenServe, and it would touch every socket path here.
- **Choosing the list's folder prefix** (was #1116). Every folder heading in the list starts with the fixed `D:\MEDIA\` (`list.LIST_FOLDER_PREFIX`), then the folder's label and the path below it. Real paths stay out on purpose: they show the account name and the disk layout to everyone who downloads the list, and requests are resolved by label. Making the fixed part a setting is safe for AutoQ, which reads only the last part of a folder, but the read side must keep recognising every prefix the bot has used (remembered in `data/`), or every `!rar` row in lists already out there stops resolving the moment it changes. A drive letter, `:\` and plain ASCII segments only, taking effect at the next rebuild. Not decided: whether a cosmetic change is worth it at all, and whether to offer presets.
- **A short DCCore tag on list requests** (was #1051). `@<nick>  ::DCCore::` instead of a bare `@<nick>`, the way AutoQ's list request carries `::AutoQ::`, so a channel can see what is fetching. Many server scripts match the trigger exactly and would silently never send the list, and a line repeated by the automatic grabber can read as advertising in a channel that bans for it. So if it is built: a setting (`LIST_REQUEST_TAG`), empty out of the box; when set, sent only to bots that are known DCCore peers (`serverschat.is_known_peer()`), which accept it by construction; plain text with no colour codes, which some channels strip or kick for; and only on list requests, never on `!<nick> !rar <folder>` or file request lines, which AutoQ copies verbatim. Not built for 1.15.0: it is not a speed item.
- **A one-page site** (was #875): what DCCore is, why it is different, three steps to start, a few screenshots, and links to the repository and the releases. One static page, no build step. Agreed in principle, and the shape is settled: a `site/` folder published by a GitHub Pages workflow in the public repository, so the raw docs are not served beside it, with the deploy job guarded by the repository name so it does nothing in the development repository. It starts on the github.io address and moves to a subdomain of the project's own domain once reviewed. In the same pull request: `.html` joins the identifier sweep's file types, the screenshots come from a test install and are scrubbed like the docs, and every link on the page resolves in both trees. It waits until after 1.15.0 because it is not release work.

---

## Not planned

- **Multi-network before multi-list.** It touches more and is wanted less.
- **Rewriting the mIRC theme engine.** The colour blocks are what makes a DCCore bot recognisable in a channel.
- **Adapting the DCC packet size during a transfer.** The block size is not what limits a transfer. `scripts/send_benchmark.py` reaches roughly 950 MB/s at 64 KB on loopback - far above any real link - so there is no headroom for a larger write to win back. What actually bounds a fast link to a distant peer is the socket send buffer against the round-trip time, which is what `DCC_SEND_BUFFER` exists for. And a slow receiver is already handled: TCP flow control blocks the send when the far end stops reading, so adapting the write size downward would be reimplementing, badly, something the kernel does correctly. What the block size does change is the CPU spent per gigabyte: 128-256 KB cost less than 64 KB and are worth it on a fast seedbox, at the price of a live speed that moves in steps of one block, so a slow transfer reads 0 or jumps (#1139). That is weighed once for a link, not adapted mid-transfer. The setting stays a menu the operator picks once.
- **TLS for the dashboard.** It is a loopback tool by design; anyone needing it across a network should put a reverse proxy in front rather than have the daemon grow a certificate story.
