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

## The two steps

1. **Download and extract DCCore.**
2. **Double-click `scripts\windows\start-dccore.bat`.** If there is no Python on
   the machine it offers to install it (below). On the first run it opens the
   setup page in your browser - nick, server, channels, your nick, the
   password, the music folder, the dashboard - with an explanation beside each,
   and starts the bot the moment you save. It offers to install Flask first
   (the page and the dashboard need it); say no and the same questions are
   asked in the black window instead. Every answer can be changed later on the
   dashboard's Settings page.

**No Python yet?** The launcher says so and asks:

```
  Python was not found.

  DCCore can download Python 3.14.7 from python.org and install it
  for you: about 32 MB, for your user only (no administrator prompt),
  with "Add python.exe to PATH" and "py launcher" both ticked. The
  download is checked against a fingerprint before it is run.

  Download and install Python now? [Y/N]
```

`Y` fetches python.org's own installer, checks its SHA-256 against the one
written in the launcher, runs it with a progress bar and no questions, and
carries on to the setup questions. A file that does not match the
fingerprint is deleted and not run. `N` - or a 32-bit Windows, or a machine
without `curl` - opens the python.org download page instead; install from
there with **both boxes ticked**, then double-click the launcher again.
(Missed the PATH box? The launcher looks where the installer puts Python
anyway.)

No Command Prompt needed. The window that opens **is** the bot: closing it
stops the bot, so leave it open or minimise it. `Ctrl-C` in it stops the bot
on purpose.

The setup page is `http://127.0.0.1:8420/setup` and answers only to this
PC, only until the form is saved, and only with the one-time code in the
link the window prints and opens - so nothing else on your PC or in your
browser can fill it in for you. If the browser did not open, copy the link
from the window. If the page could not be opened at all because the port is
taken - another DCCore still running in a minimised window, say - the window
says so and asks the questions itself.

If you would rather do the steps by hand - or need to re-run one - they are
still there: `py configure.py` asks the questions, `py -3 -m pip install -r
requirements-web.txt` installs the dashboard's one dependency,
`scripts\windows\start-dccore.bat check` checks the setup and stops.

---

## Did it actually start?

The launcher's window prints a lot. Three lines tell you whether the **web
dashboard** came up, and they are worth knowing apart because they send you to
different places.

**It is running:**

```
[WEBUI] Dashboard starting on http://127.0.0.1:8420/ (login required).
```

The host and port are your own `WEBUI_HOST`/`WEBUI_PORT`. Open that address.

**It is switched off:**

```
[WEBUI] Disabled via config.WEBUI_ENABLED = False.
```

The dashboard is opt-in - it is a network listener, so a missing switch is
never read as consent to open one. Set `WEBUI_ENABLED = true` in
`settings.conf` and restart. That is the file to edit: `py configure.py`
writes your answer to the dashboard question there (`false` if you declined
it), and where `settings.conf` and `admin_config.py` both set a name,
`settings.conf` wins - so a `WEBUI_ENABLED = True` added to `admin_config.py`
changes nothing while `settings.conf` still says `false`, and the daemon
tells you so at startup (`[CONFIG] settings.conf overrides WEBUI_ENABLED
...`). Running `py configure.py` again and answering yes does the same edit.

**Flask is not installed:**

```
[WEBUI] Flask not installed; dashboard disabled.
```

That is the optional Flask install - the launcher offers it, or
`py -3 -m pip install -r requirements-web.txt` by hand (item 4 under "Before
you start", below). The daemon itself needs nothing beyond the standard
library and carries on serving files perfectly well - only the dashboard is
unavailable, which is why a missing Flask is a message rather than a failure.

(A different line, `[WEBUI] Could not import webserver: ...`, means
`webserver.py` itself failed to import - a damaged file rather than a missing
package.)

There is a fourth, and it stops the dashboard rather than the daemon:

```
[WEBUI] ADMIN_PASSWORD_HASH is not set; refusing to start the dashboard
```

Run `python adminchat.py` to set one.

**The IRC side is separate** and reports itself separately - look for the
`[JOIN]` line naming how many channels it asked for. A daemon that is serving
files with no dashboard is a working daemon.

---

## Before you start

**Python 3.10 or newer.** Nothing else is required — the daemon and its test
suite are stdlib-only, which is why they run on a bare machine with no
`pip install` step.

1. **Download Python.** [Python 3.10.0 (64-bit)](https://www.python.org/ftp/python/3.10.0/python-3.10.0-amd64.exe),
   or any later 3.10+ from [python.org](https://www.python.org/downloads/windows/).
   (Or let the launcher do it - see *The two steps* above.)

2. **Tick both boxes in the installer:** *Add Python to PATH* and *py launcher*.
   They are what make steps 3 onwards work from any directory — see the note on
   `py` below for why the launcher in particular matters here.

3. **Check it took.** In a *new* Command Prompt — an open one still has the old
   PATH:

   ```cmd
   python --version
   py --version
   where python
   where py
   ```

   `where` printing a path under `WindowsApps` and nothing else means the
   installer's PATH box was not ticked: that is the App Execution Alias, not
   Python. Re-run the installer and choose *Modify*.

4. **Only if you want the web dashboard:**

   ```cmd
   py -3 -m pip install -r requirements-web.txt
   ```

   `py -3 -m pip`, not a bare `pip`. `start-dccore.bat` runs the daemon with
   `py -3` and only falls back to `python`, while a bare `pip` follows
   whatever `python` resolves to. On a machine with two Pythons installed -
   which step 3 above is precisely how you find out you have - those are
   different interpreters, so `pip install` succeeds, the daemon starts, and
   the dashboard silently never appears. The only clue is
   `[WEBUI] Flask not installed; dashboard disabled.` in the log, after the
   bot has already connected.

   `start-dccore.bat check` now says so before you get that far, and names
   the interpreter it looked in.

   Skip it otherwise. The daemon starts and serves files without Flask; the
   dashboard is the only thing that needs it.

5. **Configure it:**

   ```cmd
   py configure.py
   ```

6. **Check before the first real start:**

   ```cmd
   scripts\windows\start-dccore.bat check
   ```

7. **Start it:**

   ```cmd
   scripts\windows\start-dccore.bat
   ```

   If you did step 4, look for `[WEBUI] Dashboard starting on http://...` in
   the output — that is the dashboard confirming Flask was found and the
   server is up. See [Did it actually start?](#did-it-actually-start) for what
   the other three possible lines mean.

**WinRAR is optional.** Without it, single-file transfers work normally and only
whole-album (`!rar`) packing fails. If you have it, no configuration is needed:
`platform_compat.rar_command()` looks in WinRAR's install directory as well as
on PATH, because WinRAR does not add itself to PATH.

---

## Setup

### The fast path: `py configure.py`

> **`py`, not `python3`.** A python.org install gives you `py` and
> `python`; it does not give you `python3`. Worse, Windows 10 and 11 ship
> an App Execution Alias for that exact name, so typing `python3` opens
> the Microsoft Store or prints *"Python was not found"* even though
> Python is installed and working. `py` is the launcher Windows installs
> for you and is the one to use here. (`python` works too if you ticked
> *Add python.exe to PATH*.)


Asks nickname, IRC server, channel(s), admin nick, the admin console
password, the music directory (optional - easier to set from the web
dashboard once the daemon is running, if you would rather do it there)
and whether to enable the web dashboard, then offers to build the file
list and to import OmenServe totals - the full list, in the order asked,
is in [INSTALL.md](INSTALL.md#what-configure-asks) - and writes them to
`settings.conf` itself, with the password hash (and nothing else) in
`admin_config.py`. Covers everything below;
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

Then read the first few lines it prints - see [Did it actually
start?](#did-it-actually-start) above for the lines that tell you whether the
dashboard came up, and which of them means Flask was never installed.

---

## Why there is a launcher at all

Every data path in `defaults.py` is relative — `./data/bans.txt`, `./lists` — so
they resolve against the **working directory**, not the code. Started from
anywhere other than the repository folder, the daemon quietly creates an empty
`data` folder wherever it happened to start and boots with no bans, no queue and
no list.

`start-dccore.bat` does `cd /d "%~dp0..\.."` before anything else, so it is
correct from a double-click, a shortcut, or any other directory.

**How the launcher installs Python, when it has to.** The version and the
installer's SHA-256 (one per processor, amd64 and arm64) are written at the
top of `start-dccore.bat`, copied from the release page on python.org. The
launcher downloads with the `curl` that ships with Windows 10 1803 and later,
hashes the file with `certutil`, refuses it on any mismatch, and runs it with
python.org's documented unattended options (`/passive InstallAllUsers=0
PrependPath=1 Include_launcher=1 Include_test=0`) - per user, so no
administrator prompt. The launcher then looks for Python where the installer
puts it, since its own window's PATH predates the install. Moving the pin to a
newer Python is three lines: the version and the two hashes.

**This is also why there is no Windows service yet** - and why
`install-autostart.bat` schedules the launcher rather than `oserve.py`. A
service starts in `C:\Windows\System32`, and no launcher is involved to
correct it. Making that
work means anchoring the paths to the code's own location rather than the
working directory — a change worth doing deliberately, not as a side effect of
adding a service wrapper.

---

## Networking

Two things stand between the bot's ports and the people downloading, and
neither is DCCore's to fix - only to name. `start-dccore.bat check` prints
both with your actual port range.

### The Windows firewall

The first time the bot listens for a DCC send, Windows Defender Firewall
asks whether to allow it. **Cancel there means every send from then on times
out with no hint why.** If that happened, or to settle it up front:

```bat
scripts\windows\allow-firewall.bat
```

It adds one inbound rule for TCP `DCC_PORT_START`–`DCC_PORT_END` (55000–55010
unless you changed them) and, if the dashboard is on, one for its port; both
read from your settings, not typed in. Adding a firewall rule needs an
administrator's yes, so the script re-opens itself elevated - the usual
prompt. The ports and the interpreter are worked out before that, as you, and
handed to the elevated copy, so it works when the account that answers the
prompt is not yours (a standard user with a parent's password) and has no
Python of its own. `remove-firewall.bat` takes both rules out again.

Cancel does more than decline: Windows also creates an inbound **Block** rule
for that `python.exe`, and a Block rule wins over any Allow rule, so a port rule
alone would not fix it. The script therefore removes any inbound Block rule for
the interpreter the bot runs on before it adds its own rules, and says so when
it did. It touches no other program's rules and leaves an Allow rule the dialog
made alone.

### Port forwarding

**Forward TCP 55000–55010** to this machine for anyone to download from you.
That range is `DCC_PORT_START`–`DCC_PORT_END` in your settings, and the admin
console borrows a port from it too when it has to listen.

What that means: behind a home router, a connection from the internet reaches
the router, not this PC, until the router is told where to send it. In the
router's admin page (usually `http://192.168.1.1` or `http://192.168.0.1`,
under "Port forwarding" or "Virtual server"), forward that TCP range to this
PC's LAN address (`ipconfig` shows it as *IPv4 Address*). DCCore cannot do
this for you - there is no UPnP in Python's standard library, and the bot
does not know your router's password.

### Testing a download from your own machine will probably fail

This is not a bug. The daemon advertises its **public** IP in every DCC offer,
and most routers will not route a connection from inside the network back to
themselves. Test from a second machine or a phone.

Pinning `MY_IP_OR_DOCK` to this PC's LAN address does **not** work as a way
round it, and this page used to suggest it. `dcc.is_offerable_to_strangers()`
refuses every private, loopback, link-local and reserved address — an offer
carrying one is an offer to nobody — so the send is refused outright rather
than failing to connect. The symptom is the daemon declining to send at all,
which looks like a different fault entirely.

---

## Starting with Windows

Once the bot runs from a double-click, it can start by itself at logon:

```bat
scripts\windows\install-autostart.bat
```

That creates a Task Scheduler entry, "DCCore", that runs `start-dccore.bat`
when you log on - the launcher, so the working directory is right, and so the
bot's window opens as usual (closing it still stops the bot). For your user
only: no administrator, no stored password. `remove-autostart.bat` deletes
the entry. It refuses a tree that has never been set up, since the setup
questions need someone at the keyboard - run the launcher once first.

Only one bot runs from a folder: a second start - the task and a
double-click, or two logon sessions - is refused with "DCCore is already
running from this folder", and does nothing. Under the task the launcher
does not ask questions (the Flask offer prints its command instead), so a
window nobody is watching never waits for a key.

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
