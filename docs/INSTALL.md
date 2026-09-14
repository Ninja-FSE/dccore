# Installing DCCore

From a fresh clone to a bot serving files. Ten minutes, most of it waiting for the first list to build.

## Requirements

**Python 3.10 or newer.** Nothing else is required.

The daemon and its test suite are stdlib-only, which is why they run on a bare machine with no `pip install` step:

```bash
pip install -r requirements.txt      # succeeds, installs nothing
```

Two optional extras:

| | |
|---|---|
| **Web dashboard** | needs Flask. Install it with **the same interpreter that runs the daemon**: `python3 -m pip install -r requirements-web.txt` on Linux, `py -3 -m pip install -r requirements-web.txt` on Windows. A bare `pip` follows whatever `python` resolves to, which on a machine with more than one Python is not necessarily the one the launcher picks — and the result is a dashboard that silently never starts. The bot itself starts fine without Flask and says so; `start-dccore` `check` now reports this before you get that far. |
| **`!rar` album packing** | needs a `rar` binary on `PATH`. Without it, `!rar` refuses with a notice; ordinary transfers are unaffected. |

> **`pip` may not be installed yet.** Debian and Ubuntu package it separately from Python itself - `python3` does not pull in `python3-pip` - so a minimal install, and especially a fresh Proxmox LXC template, commonly has one without the other. If the command above says something like `No module named pip`, run `sudo apt install python3-pip` (or `python3 -m ensurepip --upgrade` where `apt` is not available) first. Only the web dashboard extra above needs pip at all - the daemon itself installs nothing.

Developed on Linux (Debian/Ubuntu, including Proxmox LXC) and runs on Windows — the platform differences live in `platform_compat.py`, and CI covers both. Windows operators should also read [WINDOWS.md](WINDOWS.md).

## Guided setup

```bash
python3 configure.py
```

On Windows that command is **`py configure.py`**. A python.org install gives you `py` and `python`, not `python3` — and Windows 10 and 11 ship an App Execution Alias for that exact name, so `python3` opens the Microsoft Store or reports *"Python was not found"* even when Python is installed and working. Every `python3` below has the same Windows form; [WINDOWS.md](WINDOWS.md) uses it throughout.

Six questions — nickname, IRC server, channel(s), admin nick, admin console password, and the music directory — written to `settings.conf` and `admin_config.py` for you.

The music directory is optional here. It is usually easier to browse and confirm it from the dashboard's Settings page once the bot is running than to type a path blind. Everything else stays changeable afterwards.

Safe to run again later: every prompt shows what is already configured as its default.

## Configuring it by hand

`configure.py` is a convenience, not a requirement. There are two mechanisms and you can use either or both.

**`settings.conf`** — plain text, no Python syntax. Copy `settings.conf.sample` and edit. This is what the dashboard's Settings page and the admin console both write to.

**`admin_config.py`** — Python. Copy `admin_config.py.sample` and edit. Better for values you would rather keep out of a file other tools rewrite, such as `ADMIN_HOSTMASKS` and `ADMIN_PASSWORD_HASH`.

Both are gitignored. `defaults.py` applies `admin_config.py` first and `settings.conf` second, so a value set in both takes the `settings.conf` one.

### What must be set

The daemon refuses to start until `NICKNAME`, `CHANNEL` and `ADMIN_NICK` have values, no matter how it is launched. Leaving them at their shipped blanks would mean joining somebody else's channels under a name that is not yours.

Three things are deliberately *not* required:

- **`SERVER`** and **`DEBUG_CHANNEL`** — their shipped values are already right for almost every install, so requiring them would only make you retype something correct. `DEBUG_CHANNEL` ships blank, and blank means no debug channel is joined.
- **`FILE_DIRECTORY`** — requiring it would block the daemon from reaching the dashboard, which is the easiest place to set it. A blank one warns, at the pre-flight check and again at boot, and the bot serves nothing until it has one.

### Disk the dashboard uses

The List Browser's filter searches every bot list you have downloaded at once, which needs a search index at `data/list_index.db`. It is built as each list is fetched and is roughly the size of the lists again — ten large lists can mean several hundred megabytes. `LIST_INDEX_FILE` moves it. Deleting it is safe: the filter stops working until the next fetch rebuilds it, and nothing else uses it.

## Check before you start

Verify the setup without connecting to IRC. This catches the mistakes that actually cause trouble — most often a music directory that is set but does not exist:

**Linux**
```bash
./scripts/linux/start-dccore.sh check
```

**Windows**
```bat
scripts\windows\start-dccore.bat check
```

## Start it

Once the check reports *Ready to start*, drop the argument:

```bash
./scripts/linux/start-dccore.sh
```

```bat
scripts\windows\start-dccore.bat
```

Both re-run the check first and anchor their own working directory, so they work from a cron job, a systemd unit, a shortcut or a double-click.

To start the daemon directly instead:

```bash
python3 oserve.py
```

## Build the first list

The bot has nothing to serve until its library has been scanned:

```bash
python3 update_list.py
```

or `!update` from IRC, or the dashboard's **Update list** button. On a large library this takes a while; the advert will report the real file count once it finishes.

**Everything under `FILE_DIRECTORY` goes into the list** — every format, and files with no extension at all. `LIST_IGNORED_EXTENSIONS` names what to leave out; write it however you like, since dots and spacing are optional and case does not matter (`db,ini,tmp` and `.DB, .INI, .TMP` are the same list). It ships skipping only what is never a real file: `.db`, `.ini`, `.lnk`, `.url`, and the `.tmp`/`.part`/`.crdownload`/`.!ut` suffixes of downloads still in flight. The scan prints what it is skipping before it starts.

Because everything is listed, **anything you leave in that directory is offered to anyone who asks.** It is the public face of the bot — keep out of it whatever should not leave.

Two settings decide how the result is split up:

- **`SEPARATE_VIDEO_LIST`** — publishes film and series as their own list rather than mixing them in with the music. Both travel in the same archive people get by typing your bot's name, so there is no second command to learn. `LIST_VIDEO_EXTENSIONS` says which formats count. Turn it off if your films and music are already in separate folders and you would rather split by folder.
- **`RAR_EXTENSIONS`** — which formats make a folder packable with `!rar`. A folder needs one of these to get a row in the album list. Everything else stays listed and directly requestable; this only decides what can be packed. **`MAX_RAR_FOLDER_SIZE`** bounds how large a folder `!rar` will pack — 10 GB by default, which passes a large box set and refuses the folder somebody names hoping it is a library. Set it to 0 for no limit.

### If your users queue with AutoQ

AutoQ (the mIRC queue script most of these channels use) pastes list rows into
a queue window and sends them one at a time. Two things about it are worth
knowing when you decide which file types to serve.

**It only queues rows whose extension is in mIRC's own accept list.** AutoQ
adds `*.mp3` and `*.rar` to that list when it loads, and nothing else. A row
for any other type — `.flac`, `.mkv`, `.m4a`, `.zip` — is **silently dropped**
when pasted: no line appears in the queue, and nothing says why. The row on
your list is correct; the client discards it.

Since DCCore lists every file in your library by default (minus
`LIST_IGNORED_EXTENSIONS`), tell your users to add the types you actually
serve. In mIRC that is **Options → DCC → the accept list**, or by hand in
`mirc.ini` under `[text] accept`, as a comma-separated list of `*.ext`
patterns. If most of your library is `.flac`, this is the difference between
your list working for them and half of it appearing to do nothing.

**Requesting your list from AutoQ's menu works.** Its "Get Listfile" item
sends `@yourbotname` with a tag appended, and DCCore answers a list request
with anything after the nickname. Its "Que Status" item sends
`yourbotname-que` without the leading `@`, which DCCore does **not** answer —
`@yourbotname-que` typed by hand is the form that works.

### Putting your own banner on it

Every generated list starts with the file count, the request instructions, and a line naming the bot and this project. Below that you can put anything you like — a greeting, your channel, ASCII art — by creating one file:

```bash
nano data/list_header.txt
```

Whatever it contains is copied into the top of the `.txt`, `.zip` and `.rar` lists on the next `!update`. There is nothing to enable: the file not existing is the normal state, and means no banner.

Three things worth knowing:

- It is copied **verbatim**, so box-drawing characters and ASCII art survive intact. Nothing is reformatted or stripped.
- Keep lines to roughly **80 characters**. The folder headings below are drawn to the width of the folder they frame, so a much wider banner reads as broken next to them.
- It is capped at `LIST_HEADER_MAX_BYTES` (8 KB by default). Past that the banner is truncated and the run says so, rather than quietly stapling a large file onto every list request.
- **Do not start a line with `!yourbotname`.** The list is read by scripts as well as people — AutoQ copies request lines straight out of it and sends them — so a banner line beginning with your own trigger reads as a request for a file that does not exist. Anything else is fine; it is only the first word of a line that matters.

mIRC colour codes work if your audience reads the list in mIRC, but they show as stray characters in a plain text editor — worth deciding which of the two matters more for your channel.

### Choosing the advert's colours

The colours DCCore uses in the channel are a **theme** — one of five presets,
plus six settings that override a single role each. All of them live under
**Appearance** on the Settings page.

The six roles are `border`, `separator`, `textbox`, `value`, `alert` and
`accent`, set as `CUSTOM_THEME_BORDER` and so on. A role left empty keeps
whatever the chosen preset uses, so changing one colour does not mean restating
the other five.

**You pick each one from two menus** — a foreground and an optional background
— with a swatch beside them showing what the two make together. A background
cannot be chosen on its own: `\x03,05` is not a colour code, so that menu stays
disabled until a foreground is set.

Underneath, a role is still an mIRC colour code written as `\x0300,01` (white
on black), and you can still set one by hand in `settings.conf`. If you write
something the menus cannot express — a code with bold in it, or anything past
the sixteen colours — **the dashboard leaves it exactly as you wrote it** and
shows it as text rather than replacing it with the nearest colour a dropdown
can offer.

You do not have to guess what a code looks like. The Appearance section draws
**two sample lines above the fields and redraws them as you type**, before
anything is saved: the periodic advert and the notice posted when a send
finishes. Both are shown because between them they use all six roles and
neither uses all six alone — the advert never uses `accent`. They are built by
the same code that builds the real lines, so the sample is what the channel
gets.

The preview changes nothing. Until you press **Save**, the daemon is still
advertising in the colours it started with.

## If your library is very large

Nothing to configure — this is here because the default changed and the old
one was a trap.

DCCore used to abandon a list rebuild after 30 minutes. That is comfortable for
a few terabytes and impossible for eighty, and the failure looked like
`Failed: timed out after 1800s` with the previous list left in place, every
time.

It now watches the rebuild's **progress** instead of a clock. While the rebuild
is still reporting — it does so on every folder it enters — it runs for as long
as it needs. It is abandoned only if it goes completely silent for
`LIST_UPDATE_STALL_SECONDS` (15 minutes by default), which is what a drive that
has gone away mid-scan looks like.

**There is no time cap by default.** If you want one regardless, set
`LIST_UPDATE_TIMEOUT` (Settings → Advanced, or `settings.conf`) to a number of
seconds; `0` means none. You do not need one for safety — the silence check is
the guard, and it reacts twice as fast as the old limit did.

If a rebuild is abandoned, the reason says which of the two happened. *"Nothing
reported for 20m 00s"* points at the library — a mount that dropped. *"Timed
out"* points at a cap you set.

## Upgrading

Your settings and data are never touched by an upgrade: `settings.conf`, `admin_config.py` and everything under `data/` are gitignored, so updating the code cannot overwrite them. That is also the one thing to watch — see step 4.

**1. Stop the daemon.** A transfer in progress will be cut off, so a quiet moment is kinder than mid-queue.

**2. Back up `data/` and your config.** It holds your stats, ban list, download counts and speed record — none of it recoverable if something goes wrong.

```bash
cp -r data data.backup && cp settings.conf admin_config.py data.backup/
```

**3. Get the new version.**

```bash
git pull
```

If you installed from a downloaded release rather than a clone, download the new release and unpack it over the top — your gitignored files are not in the archive, so they survive. Keep `data/` where it is.

**4. Check for new settings.** This is the step people miss. `settings.conf` is gitignored, so `git pull` updates `settings.conf.sample` but never your own file. New settings do not appear in it, and you will not hear about them.

```bash
comm -23 <(grep -oE '^#?[A-Z_]+ *=' settings.conf.sample | tr -d '# =' | sort) \
         <(grep -oE '^[A-Z_]+ *=' settings.conf | tr -d ' =' | sort)
```

That lists every setting the sample knows about and your file does not. Most of them will be settings you were happy to leave at their defaults, so read it as "what exists", not as a to-do list.

Nothing breaks if you skip this — every setting has a working default and the daemon runs fine without any of them being present. You simply will not know what became available. The changelog is the readable version of the same information.

**5. Read the changelog.** [UPDATES.md](UPDATES.md) says what changed and, where it matters, what you have to do about it.

**6. Verify before going live.**

```bash
./scripts/linux/start-dccore.sh check
```

This checks the configuration without connecting to IRC, so a mistake surfaces before your channel sees it. Windows: `scripts\windows\start-dccore.bat check`.

**7. Start it, and rebuild the list if the changelog says the list output changed.**

```bash
./scripts/linux/start-dccore.sh
```

The master list is only regenerated when you ask. If a release changes what the list contains, the file you are serving keeps its old content until the next `!update` — which looks like the upgrade did nothing.

### Coming from v1.10.0 or earlier

v1.11.0 was never published as its own release, so this section covers
everything between the last one you actually ran and this one.

The list build changed what it puts in the list, so three things are worth knowing before you restart.

**Your list will not change until you rebuild it.** The daemon keeps serving the list already on disk; run `!update` (or press **Update list** on the dashboard) when you are ready. That is your chance to look at the result before anyone else does.

**It will then contain everything under `FILE_DIRECTORY`, not just `.mp3` and `.flac`.** Video, `.m4a`, artwork, cue sheets, text files — all of it. Check what is actually in that directory first: anything sitting there is offered to anyone who asks. `LIST_IGNORED_EXTENSIONS` names what to leave out, and ships skipping only what is never a real file (`.db`, `.ini`, `.lnk`, `.url`, and half-finished downloads).

**Some folders will stop being `!rar`-packable.** A folder used to become packable by containing anything in the list; now it needs a file in `RAR_EXTENSIONS`, which ships as the audio formats. So a folder of video or documents no longer gets a row in the album list — deliberately, since packing has no size cap and a film folder is a request to compress tens of gigabytes. Individual files in those folders are still listed and still requestable by name.

If you keep video, it also gets its own list from now on, travelling in the same archive people already receive. `SEPARATE_VIDEO_LIST = No` puts everything back in one list.

**One thing to know if this install is very old:** a startup step that renamed two side files left over from before the daemon had its current name has been removed. It only ever did anything on an install that had never rebuilt its list since that rename shipped, years ago — if `!update` has run successfully even once since then, this does not apply to you.

**Check your admin password if you ever changed it with `configure.py`.** On any install whose password had also been changed from the dashboard at some point, `configure.py` was writing the new hash to `admin_config.py` — which `settings.conf` overrides, so the change never took effect and the old password kept working. It says so plainly now, but it did not before, so a rotation you believe you did may not have happened. Log in with the password you think you removed; if it still works, set it again from the dashboard.

**If you wrote a `CUSTOM_THEME_*` override, look at your next advert.** The documented form — a code like `\x0306,06` — was previously sent to the channel as those literal characters rather than as a colour. It is decoded now, so a value you had given up on will start working, and one you had worked around by other means may now be applied twice.

**Your advert's `Speed:` figure will read higher.** It was showing the average across your sending slots rather than their total, so three transfers at 2 MB/s each advertised 2.0MB/s. Nothing about your transfers changed — only the number, which was understating the bot by however many slots were in use.

### Coming from a version before the `config.py` → `defaults.py` rename

Your own overrides file was called `local_config.py` and is gitignored, so `git pull` cannot rename it for you. **Do not copy `admin_config.py.sample` over the top** — that strands your real settings. Just start the daemon once and it renames the file itself, keeping every setting in it. The launchers and the pre-flight check both say so if they meet that state.

## Next

- [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md) — setting up the authenticated DCC CHAT console
- [WINDOWS.md](WINDOWS.md) — the full Windows guide
- [FUTURE.md](FUTURE.md) — what is built and what is not
- [CONVENTIONS.md](CONVENTIONS.md) — if you plan to contribute
