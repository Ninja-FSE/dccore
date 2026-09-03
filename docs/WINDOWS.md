# Running DCCore on Windows

This covers the Windows packaging — a launcher and a setup check, in
`scripts/windows/`, alongside their Linux counterparts in `scripts/linux/`.
Nothing here changes how the daemon behaves; the platform differences that do
exist live in `platform_compat.py` and are covered by CI on both operating
systems.

The daemon itself already runs on Windows. Its whole boot sequence was verified
before this packaging existed: master list generated, every `data/` file
round-tripped, DCC listener bound, WinRAR found at its install path.

---

## Before you start

**Python 3.10 or newer.** Install from python.org and tick *Add python.exe to
PATH*. Nothing else is required — the daemon and its test suite are stdlib-only,
which is why they run on a bare machine with no `pip install` step.

**WinRAR is optional.** Without it, single-file transfers work normally and only
whole-album (`!rar`) packing fails. If you have it, no configuration is needed:
`platform_compat.rar_command()` looks in WinRAR's install directory as well as
on PATH, because WinRAR does not add itself to PATH.

---

## Setup

### The fast path: `python3 configure.py`

Asks nickname, IRC server, channel(s), admin nick, the admin console
password, the music directory (optional - easier to set from the web
dashboard once the daemon is running, if you would rather do it there),
and whether to enable the web dashboard - and writes them to
`settings.conf` and `admin_config.py` itself. Covers everything below;
skip to step 2 if you use it. The rest of this section is the manual
equivalent, for anyone who would rather edit the files by hand.

### 1. Create `admin_config.py`

Copy `admin_config.py.sample` to `admin_config.py` and fill it in. That file is
gitignored and never leaves your machine.

> **The one line that matters most is `CHANNEL`.**
>
> `NICKNAME`, `CHANNEL` and `ADMIN_NICK` ship blank on purpose - `oserve.startup()`
> refuses to boot while any of them is still unset, naming every one that is
> still unconfigured. Set your own nickname and your own test channel here
> (or in `settings.conf`) before the daemon can start at all.
>
> `SERVER` and `DEBUG_CHANNEL` are not part of that check - their shipped
> defaults are already correct for almost every install. Neither is
> `FILE_DIRECTORY`: requiring it would block the daemon from ever reaching
> the web dashboard, the one place that is genuinely easier to set it from -
> a blank one only warns.

Set at minimum:

| setting | why |
|---|---|
| `NICKNAME`, `ALT_NICKNAME` | must not collide with the live bot |
| `CHANNEL`, `DEBUG_CHANNEL` | your own test channel |
| `ADMIN_NICK` | your nick |
| `FILE_DIRECTORY` | your music folder — start with a small one |

`LIST_BASE_NAME` (names your generated list files) is not in that list -
it automatically takes NICKNAME's own value once NICKNAME is set, so only
set it explicitly if you want the list named differently from the bot.

`admin_config.py.sample` has commented-out placeholders for all seven
(`LIST_BASE_NAME` included, for the rare case you want it), in a section
near the top.

**Prefer plain text?** Every setting above can also go in `settings.conf`
instead (copy `settings.conf.sample` to `settings.conf`) — no Python syntax,
and it's what the web dashboard's Settings page writes to as well. The setup
check in step 2 accepts either file; `admin_config.py` still owns
`ADMIN_HOSTMASKS`/`ADMIN_PASSWORD_HASH` most naturally, since those come from
running `python adminchat.py`, but they work in `settings.conf` too.

### 2. Check the setup

```
scripts\windows\start-dccore.bat check
```

This loads the same config the daemon will and reports what it resolved. It
never opens a connection to a server and never joins anything. It fails on a
music directory that is set but does not exist, among other things - see the
full report it prints for what else it checks.

### 3. Start it

```
scripts\windows\start-dccore.bat
```

Ctrl-C in that window stops it. The launcher runs the setup check first and
refuses to start if it fails.

---

## Why there is a launcher at all

Every data path in `defaults.py` is relative — `./data/bans.txt`, `./lists` — so
they resolve against the **working directory**, not the code. Started from
anywhere other than the repository folder, the daemon quietly creates an empty
`data` folder wherever it happened to start and boots with no bans, no queue and
no list.

`start-dccore.bat` does `cd /d "%~dp0..\.."` before anything else, so it is
correct from a double-click, a shortcut, or any other directory.

**This is also why there is no Windows service yet.** A service starts in
`C:\Windows\System32`, and no launcher is involved to correct it. Making that
work means anchoring the paths to the code's own location rather than the
working directory — a change worth doing deliberately, not as a side effect of
adding a service wrapper.

---

## Networking

**Forward TCP 55000–55010** to this machine for anyone to download from you.
That range is `DCC_PORT_START`–`DCC_PORT_END` in `defaults.py`, and the admin
console borrows a port from it too when it has to listen.

### Testing a download from your own machine will probably fail

This is not a bug. The daemon advertises its **public** IP in every DCC offer,
and most routers will not route a connection from inside the network back to
themselves. Either test from a second machine or a phone, or set
`MY_IP_OR_DOCK` to this PC's LAN address in `admin_config.py` for local testing
only — and remove it before real use.

---

## The admin console

Optional, and off until configured. See [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md) for
the full guide. In short: it is gated on your Undernet `+x` host plus a
password, and the setup check will tell you if you have set one without the
other.

Generate the password hash with:

```
python adminchat.py
```

Run that yourself. The password never needs to leave this machine.

---

## What is different from Linux

Very little, and all of it is already handled:

| | |
|---|---|
| DCC listener socket option | `SO_EXCLUSIVEADDRUSE` here, `SO_REUSEADDR` there — on Windows the latter would let another process steal the port |
| rar binary | looked up in WinRAR's install directory as well as PATH |
| long paths | `\\?\` prefix applied past 260 characters, which a deep music library reaches easily |
| TCP keepalive | the tuning knobs that only exist on Linux are skipped rather than raising |
| batch files | CRLF, per `.gitattributes`; everything else in the repo is LF |
