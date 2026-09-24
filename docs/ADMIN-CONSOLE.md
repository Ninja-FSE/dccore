# DCCore Admin Console (DCC CHAT)

Administrative access over an authenticated DCC CHAT session instead of channel
commands.

**Status: complete.** The console authenticates, runs the full admin command
set, and can receive the daemon's runtime reports instead of the debug channel.
Everything stays as it was until you choose otherwise: with `ADMIN_HOSTMASKS`
empty, every DCC CHAT request is ignored and the daemon behaves exactly as
before.

---

## Why

`is_admin()` compares a nick against `ADMIN_NICK`. On Undernet a nick is not owned
without services auth, so anyone can take the admin nick while you are offline and
inherit every admin command, including the destructive `!clearqueue`. With
`ADMIN_HOSTMASKS` set, the channel and private-message commands check the sender's
host as well as the nick (see "The admin commands typed in a channel" below); with
it empty they are still checked on the nick alone.

The console replaces that with two independent factors:

1. **Your Undernet services login**, proved by your host.
2. **A password**, which never travels through the IRC network.

Stealing your nick gets an attacker nothing without your X account. Learning the
password gets them nothing from the wrong host.

---

## How the host proves your login

When you log into **X** and set usermode `+x`, the Undernet server replaces your
host with:

```
<your-account>.users.undernet.org
```

Only the server can issue that host, and only to someone holding that account. So
matching the host *is* verifying your services login — no password is shared with
this bot, and there is nothing in its config for an attacker to steal.

> **The ident is deliberately ignored.**
> In `nick!ident@host` the ident half is supplied by your client — anyone can set
> theirs to `flac`. Only the host is issued by the server. DCCore discards the
> nick and ident parts of any configured mask on purpose: constraining them would
> grant no security while breaking the moment your client's ident setting changes.

---

## Setup

### 1. Make sure you are authed and hidden

In your IRC client:

```
/msg X@channels.undernet.org login <youraccount> <yourpassword>
/mode <yournick> +x
```

Then check what the network now sees:

```
/whois <yournick>
```

You are looking for a host ending in `.users.undernet.org`. If you still see your
ISP hostname, `+x` did not take and the console will not let you in.

### 2. Generate a password hash

`python3 configure.py` does this step for you - the same password prompt as
below, writing the resulting hash straight into `admin_config.py` - if you
have not already run it. **It also offers step 3**, optionally: your services
host, straight after the admin nick question, blank to skip (#811). Answer it
and `ADMIN_HOSTMASKS` is set for you, in `settings.conf`; leave it blank and
the console ignores every DCC CHAT without a word until you set it by hand as
step 3 shows, or on the dashboard's **Settings → Admin console** page. To do
the hash by hand instead:

From the DCCore directory, on either platform:

```
python adminchat.py
```

It prompts twice, then prints a line ready to paste:

```
ADMIN_PASSWORD_HASH = "pbkdf2_sha256$200000$3f0a...$91c4..."
```

The password itself is never stored — only a salted PBKDF2-SHA256 digest, and it
is compared in constant time. Two hashes of the same password look different,
which is the salt doing its job.

### 3. Put both values in `admin_config.py`

Create `admin_config.py` next to `defaults.py` if it does not exist. **It is
gitignored**, so nothing here reaches GitHub. Do not put these in `defaults.py`.

```python
# admin_config.py

ADMIN_HOSTMASKS = ["*!*@operator.users.undernet.org"]

ADMIN_PASSWORD_HASH = "pbkdf2_sha256$200000$3f0a...$91c4..."

# Optional. "auto" (default), "listen", or "connect" - see "Using it" below.
# Behind a VPN or a router that does not forward the port, use "listen".
ADMIN_CHAT_MODE = "auto"

# Optional. Set False to retire the in-channel admin commands - see below.
ADMIN_CHANNEL_COMMANDS = True
```

The mask may be written either way — both mean the same thing, because only the
part after the last `@` is used:

```python
ADMIN_HOSTMASKS = ["operator.users.undernet.org"]        # bare host
ADMIN_HOSTMASKS = ["*!*@operator.users.undernet.org"]    # familiar IRC form
```

Wildcards work, and more than one entry is allowed:

```python
ADMIN_HOSTMASKS = ["operator.users.undernet.org", "operator2.users.undernet.org"]
```

A pattern that reduces to bare `*` is refused and logged — it would admit the
whole network and make the gate decorative. A pattern that is accepted but
names far more than one operator is warned about at start-up, on `!rehash` and
by `setup_check.py`, and still works as written: `*.users.undernet.org` puts
the wildcard where your account name goes, so every X-authenticated user on
the network reaches the password prompt; `*.org` names a top-level domain. The
documented shape is your own services host in full.

### 4. Restart the daemon

`admin_config.py` is read at import. `!rehash` reloads `config`, so it picks up
changes too, but a restart is the sure thing while you are setting this up.

---

## Using it

**Using mIRC?** `scripts/mirc/dccore.mrc` turns the console into one
window - the feed coloured per kind, a side panel with what is sending and
who is waiting, the slots and today's totals in the title bar - and logs in
by itself. See [The window, in mIRC](#the-window-in-mirc) below; the rest
of this section is what happens underneath it.

**Would rather not open a second IRC client at all?** The web dashboard's
Console page runs the exact same command set and shows the exact same live
log, in the browser, behind the same password — see the Console entry in
the dashboard's own nav once `WEBUI_ENABLED` is on. Everything below still
applies to it except the connection steps, which do not exist over HTTP.

From your IRC client:

```
/dcc chat DCCore
```

There are two ways the connection gets made, and the daemon picks whichever can
work:

1. **Your client listens, the bot dials in.** The normal path, and iroffer's
   non-passive branch. Needs no port on the bot's side at all.
2. **The bot listens and offers back.** Used when your client's offer cannot be
   dialled — it asked for **passive (reverse) DCC**, or it does not know its own
   address and sent `0.0.0.0`. The bot takes a port from
   `DCC_PORT_START`–`DCC_PORT_END`, the same range already forwarded for DCC
   SEND, and advertises the public IP it resolved at startup. A passive request
   carries a token, and the bot echoes it back so your client can match the
   reply to the request it is waiting on. Your client then shows an incoming
   chat request to accept.

If the dial fails for any reason — refused, or timed out — the bot falls back to
path 2 automatically. Nothing is lost except the connect timeout.

`ADMIN_CHAT_MODE` in `admin_config.py` controls this:

| value | behaviour |
|---|---|
| `"auto"` | dial, and listen instead if that fails *(default)* |
| `"listen"` | always listen and offer back — never dial |
| `"connect"` | only ever dial; never listen |

**Set it to `"listen"` if your client is behind a VPN**, a router that does not
forward the port, or a firewall that drops rather than rejects. All three show up
as a `timed out` on the dial, and `"auto"` then pays that full timeout on every
single login before falling back. The bot's own listener needs no such luck — it
is the same one every DCC SEND already uses.

Path 1 is tidier when it works (one dialog instead of two). In mIRC:
**Options → Connect → Local Info**, tick *On connect, always get IP address*, and
set the lookup method to **Server**.

```
Chat with DCCore
Waiting for acknowledgement...
DCC Chat connection established

Welcome to DCCore
DCCore v1.13.1 - platform=posix python=3.10 rar=/usr/bin/rar

Enter Your Password:
```

Type the password and press enter:

```
Entering DCC Chat Admin Interface
For help type "help"
```

Commands are bare words — there is no channel to disambiguate from, so no `!`
prefix.

### What you can see

| Command | Effect |
|---|---|
| `status` | everything at a glance — slots, queue, bans, list, uptime |
| `queue [nick]` | queued files, all users (in the order they are served) or one |
| `slots` | what is sending right now, and how far along |
| `bans` | permanent and timed bans |
| `uptime` | how long the daemon has been running |
| `version` | build and platform |
| `checkversion` | ask GitHub now whether a newer DCCore is out; works with `CHECK_FOR_UPDATES` off, once a minute |
| `verify` | filenames that appear in two folders |

### What you can do

| Command | Effect |
|---|---|
| `ban <pattern>` | add a permanent wildcard ban |
| `unban <pattern>` | remove one |
| `clearqueue <nick>` | force-clear another user's queue |
| `rehash` | reload modules in place |
| `update` | rebuild the MasterList |
| `lists` | the bots' lists we hold, whether each has changed since we took our copy, how big and how old |
| `fetch [<bot>]` | ask every held bot whose list has changed (up to 10 at a time, skipping offline ones), or one bot whatever its freshness |
| `help` | the command list |
| `hello <client> <version>` | switch this session to the structured feed (below) |
| `pair <client> <version>` | mint a login token for a script (below) |
| `unpair [<client>]` | list the paired scripts, or revoke one |
| `quit` | close the session |

`lists` and `fetch` are the console side of the List Browser's freshness check and
its automatic refresh. `fetch` goes through the same enqueue as the dashboard's
button, so the slot limits, the duplicate guard and the queue ceiling are the same,
and the lists arrive as their transfers finish - a structured client is told with
a `LISTFETCH` line when one is asked for automatically, arrives, or cannot be used.

`rehash` and `update` run in the background — `update` walks the whole library
and can take minutes — so the console stays usable while they work. Their
progress arrives in the session as it happens, because an authenticated console
receives the daemon's runtime log alongside the debug channel.

### Your nick does not matter here

The five admin commands normally check the caller's nick against `ADMIN_NICK`.
A console session skips that check, on purpose: it has already proved your
services login through your `+x` host **and** a password, which is a stronger
claim than a nick anyone can take while you are offline. So the console works
even when your current nick is not in `ADMIN_NICK` — after a `433` collision,
for instance.

---

## Where the runtime reports go

`send_debug` is the daemon's running commentary — transfers, joins, bans, pack
failures. It has two destinations, both on by default:

| | |
|---|---|
| `DEBUG_TO_CHANNEL` | the coloured line in `DEBUG_CHANNEL`, as always |
| `DEBUG_TO_CONSOLE` | the plain text in an attached admin console - and, for `dccore.mrc`, the structured feed's event lines too: off means the window goes quiet, not just its `LOG` lines |

Once the console is doing the job, in `admin_config.py`:

```python
DEBUG_TO_CHANNEL = False
```

The daemon's internals then stop being published to a channel other people can
sit in.

**Neither switch can lose a line.** If the channel is off and no console happens
to be connected, `send_debug` falls back to stdout — so the LXC console and the
journal always have it. That case, something going wrong while nobody is
watching, is the one worth protecting. It is a floor, not a third destination:
when the channel or a console did take the line, nothing extra is printed.

### In colour

The chat window is an IRC client, so the tag on each line - `[SENT]`,
`[FAIL]`, `[REQUEST]`, `[SECURITY]` - is coloured the same way it is in the
debug channel, in whatever theme the bot uses. `ADMIN_CHAT_COLOURS`
(**Settings → Admin console**) turns that off for a client that shows the
codes as junk; off gives plain `[TAG] text`. The dashboard's Console page is
never coloured.

### The transfer feed

The console tells the whole story of a transfer, one line per event, the way
an OmenServe operator sees it inside mIRC:

```
[REQUEST] dave asked for "Song.flac"
[QUEUED]  Queued "Song.flac" for dave at #2 (3/3 slots busy)
[SENDING] Sending "Song.flac" to dave (slot 2/3)
[RESUMED] Resumed "Song.flac" for dave at 1.0GB of 1.5GB
[SENT]    Sent: "Song.flac" to dave [1.5 MB/s]
[FAIL]    Failed: "Song.flac" to dave - the receiver stopped acknowledging at 1,200,000 of 2,700,000 bytes ...
[SEARCH]  dave searched "metal" - 12 results
```

**Settings → Console feed** has a tickbox per kind — requests, queue positions,
sends (starting, resuming, completing), failures, searches — all on by default.
An unticked kind is dropped, not diverted: it does not fall through to the
stdout floor, because the floor is for a line nobody was there to take, not one
you asked not to see. The tickboxes govern the console and the dashboard's
Console page only. Everything that is not one of those kinds — joins, parts,
bans, config warnings — is never affected by them.

The IRC debug channel is deliberately separate. `Sent:` and `Failed:` go there
under `DEBUG_TO_CHANNEL` as they always have; the feed's other events
(requests, queue positions, starts, resumes, searches) go there only with
`DEBUG_CHANNEL_FEED` on, which ships **off**: every channel line takes a
`MSG_DELAY` slot on the same pacer as the adverts, the resume replies and the
queue notices, so on a busy bot a chatty feed there delays the things people
are waiting for. The console has no such cost.

A console that has been switched on but is *not connected* counts as nobody
listening, and so does a console whose sink raised. Both fall through to stdout.

### The short list, next to the long one

The Console and the debug channel are a **log**: everything the daemon does,
in order. That is the right shape for reading back what happened, and the
wrong shape for *"did anything go wrong while I was asleep?"* - everything is
in it, so nothing stands out.

The dashboard's status panel carries the other shape. When something happens
that changed the bot's ability to do its job, a coloured badge appears under
the queue counts saying how many things are waiting to be looked at. Clicking
it opens **What happened**, which lists them newest first, and **Mark all
read** clears the badge.

Two severities, and the difference is whether it is over:

| | |
|---|---|
| amber | it happened, and it is finished. Kicked from a channel and rejoined. |
| red | it is still true. Gave up rejoining a channel; a list rebuild that failed. |

The badge takes the worse of the two, and it counts only what has not been
marked read - so a red that was acknowledged last week does not keep it lit.

**There is no badge at all when there is nothing unread.** It is not a panel
that sits there showing a zero; it means something by appearing.

What raises one, and nothing else does:

- being kicked from a channel (amber - a rejoin is already scheduled)
- giving up on a channel after the configured number of rejoin attempts (red)
- a list rebuild that failed or timed out (red)

DCCore banning or muting a **user** does not, and that is deliberate: it is
routine, it happens often, and a badge that counts routine events is a badge
nobody reads. The Console still logs every one of them.

The notices are kept in `data/notices.json` and survive a restart, which is
the point of them - the events worth a badge are the ones that happen while
nobody is looking. The last 200 are kept; `NOTICES_FILE` moves the file, and
deleting it is safe.

### Getting rid of a downloaded list

Nothing used to remove one. A bot's list, once fetched, stayed in the List
Browser forever - including for a bot that had renamed, left, or would never
reconnect. Open the list and press **Purge this list** under the file table.

It removes three things together, because leaving any one of them behind makes
the operation a lie:

- the entry, or the browser keeps offering a list whose files are gone
- the extracted files under `FETCHED_FILES_DIR/lists/<bot>/`, which is the
  disk you were trying to get back
- the bot's rows in the cross-list search index, which is roughly as large
  again as the lists it describes

The button appears only where it can do something: not for your own lists,
whose files are your library, and not for a bot you have merely seen
advertising. It asks first, because **fetching the list again is the only way
back** and that needs the bot to still be around.

It refuses while a fetch from that bot is running or queued - that fetch would
write into the directory being deleted, and it would put back what you just
removed. Wait for it to finish, or delete the fetch from Downloads first.

**To clear out several at once**, use **Purge offline bots' lists** in the
toolbar above the bot list instead. That one takes every bot showing the red
dot - offline right now - and leaves alone anything grey ("cannot tell yet",
which is what every bot looks like before the daemon has finished joining its
channels). Use the per-list button when you want a specific one gone whatever
its dot: a bot that renamed, a list fetched by mistake, or one of a pair of
rows left by a bot that reconnected under its alternate nickname.

### Messages people send the bot

The bot answers commands. Anything else sent to it privately - *"are you
there?"*, *"how do I get X?"* - gets **no reply**, and that is deliberate: a
bot that answers every stray line is one that can be made to flood itself off
the network.

It is written down now, which it never used to be. The dashboard's
**Messages** page lists who wrote and what they said, newest first, with an
unread count on the tab. **Mark all read** clears it.

What is recorded, and nothing else:

- **private** messages only - a channel line is one you can already see;
- that are **not** a recognised command;
- that are **not** a CTCP (a DCC offer or a VERSION reply is a client talking
  to a client, not a person);
- from somebody who is **not banned** - a ban silences them here too.

**One message per person every five minutes.** Somebody typing four lines
because the first got no answer is one person asking one thing, and four rows
of it buries the next person who writes. `PRIVATE_MESSAGE_COOLDOWN_SECONDS`
changes it; 0 records everything.

The last 50 are kept, in `data/private_messages.json`, and they survive a
restart. **You cannot reply from the page** - the bot has no conversation
path, and a reply box would be a promise it cannot keep. Message them from
your own client.

## Retiring the channel commands

`!ban`, `!unban`, `!rehash`, `!update` and `!clearqueue` still work when typed in
a channel. That is deliberate for now — locking yourself out of every admin
command because a hostmask has a typo in it would be a poor introduction.

Once the console has proved itself, in `admin_config.py`:

```python
ADMIN_CHANNEL_COMMANDS = False
```

Admin authority then rests entirely on the services host plus the password, and
no longer on a nick. You do not have to turn them off to be safe from a stolen
nick, though: with `ADMIN_HOSTMASKS` set, `!ban`, `!unban`, `!rehash`, `!update`,
`!clearqueue`, `!ping` and `!debugnames` typed in a channel or a private message
are honoured only when the nick is in `ADMIN_NICK` **and** the sender's host
matches one of the masks - the same test the console uses, so a host that lets you
into the console lets you use these too, and a nick somebody else has taken does
not. A line that does not say where it came from is refused. With
`ADMIN_HOSTMASKS` empty the check is the nick alone, as it always was. The user commands — `!list`, `@find`, the queue triggers —
are not affected either way. `!ping` and `!debugnames` are the operator's
diagnostics rather than user commands: they answer only a nick in `ADMIN_NICK`
(and, being channel commands, keep doing so with `ADMIN_CHANNEL_COMMANDS` off).

## The structured feed, for a script

A client that draws a window - `dccore.mrc` is the one this exists for - wants
fields, not prose. After logging in, send one console command:

```
hello dccore.mrc 1.1
```

The bot answers `DCCORE HELLO 1.1 <botnick> <version>` and, from then on, every
line it sends on this session starts with `DCCORE`. The second word of your
`hello` is your client's own version, and the bot reads it: one older than
the oldest script that reads this bot's lines right (`1.1`, the first to know
the channel field) is answered, right after `HELLO`, with a plain line saying
to update the script - the feed still switches on, since the major is the
same, but a field will read wrong until you do. A bot without this feature
answers `Unknown command: hello` instead - stay in prose mode. The number in
`HELLO` is the protocol version as `major.minor` (a bot from before the minor
was added says a bare `1`): refuse a major you do not know; a minor you do not
know means a fixed field has been inserted on one side - the lines still
parse, but a field is not where you expect it - so warn, and update whichever
side is older. The minor goes up every time a field is inserted; the free-text
field is always last, so appending nothing ever moves.

Every line is **space-separated positional tokens, with the one free-text
field last** - so in mIRC it is `$1`, `$2`, ... and `$N-`. Numbers are raw
bytes and seconds; you format them. Tabs and control characters in any field
have been replaced with spaces.

| line | fixed fields | free text (last) |
|---|---|---|
| `DCCORE HELLO 1.1 <botnick>` | protocol major.minor, nick | the version string |
| `DCCORE REQUEST <nick> <channel> <file\|folder>` | | the name |
| `DCCORE QUEUED <nick> <channel> <pos> <busy> <slots>` | position, slots busy / total | the name |
| `DCCORE SENDING <nick> <channel> <slot> <slots> <bytes>` | slot n / m, size | the name |
| `DCCORE RESUMED <nick> <channel> <at_bytes> <total_bytes>` | | the name |
| `DCCORE SENT <nick> <channel> <bytes> <seconds> <bytes_per_s>` | | the name |
| `DCCORE FAIL <nick> <channel> <acked_bytes> <total_bytes>` | what arrived, of what | the name, then ` :: `, then the reason |
| `DCCORE SEARCH <nick> <channel> <results>` | count (the total, not the capped reply) | the term |
| `DCCORE LOG <CATEGORY>` | JOIN, PART, QUIT, BAN, HARDBAN, MUTE, TBAN, INFO | the prose, as the plain console shows it |
| `DCCORE OUT` | | one line of a console command's reply |
| `DCCORE DROPPED <n>` | lines the bot had to drop for a slow client | |
| `DCCORE TAKEN <ip>` | the address that took the console over | |
| `DCCORE LISTFETCH <bot> <action>` | `auto` (asked again automatically), `arrived`, `unusable` | one line of prose that names the bot |
| `DCCORE STATUS <used> <slots> <qfiles> <qusers> <sent_today> <bytes_today> <bps_now> <record_bps> <started> <failed> <searches>` | slots in use / total, files and users queued, today's sends and bytes, speed now, the record; then when the bot started (epoch) and the failures and searches it has seen since | |
| `DCCORE SLOT <nick> <sent> <total> <bps>` | one per active transfer: bytes so far, size, speed from its own clock | the name |
| `DCCORE QUEUE <pos> <nick> <files> <frozen_secs_left>` | one per queued user, the first 20 in the order they are served: position, files waiting, seconds until a frozen queue is dropped (0 = not frozen) | |
| `DCCORE TOKEN <name>` | the reply to `pair` | the token, shown once |
| `DCCORE PING` | stands in for a status burst the bot could not compute in time; a client treats it as any other line and shows nothing | |

`<channel>` is always exactly one token, straight after the nick: the channel
the request or search was made in, or `-` when there is none (a request by
private message, a resume, or a transfer that no longer knows where it was
asked for) - so a client can count on the position of everything after it and
print nothing for `-`.

Whatever you did not tick in **Settings → Console feed** is not sent in either
mode. A session that never says `hello` is the console described above,
unchanged.

### The live picture

`STATUS`, then a `SLOT` line per transfer, then a `QUEUE` line per waiting
user, is one **burst**, and it is what a client's title bar and side panel are
drawn from. It arrives:

- right after `HELLO`, so the window is filled before the first event;
- after any event that moved a slot or the queue (`SENDING`, `SENT`, `FAIL`,
  `QUEUED`, `RESUMED`), so the picture never waits for the timer;
- every 30 seconds while the session is quiet. That is also the heartbeat: a
  client that has heard nothing for a minute or so knows the link is dead,
  not merely idle. The figures are read on a helper thread with a two-second
  deadline; if they are not in by then (a queue lock or a stats database held
  for that long by some slow disk operation) the bot sends `DCCORE PING`
  instead, so a busy-but-alive bot is still heard from, and the feed keeps
  flowing behind it. The burst follows once the figures can be read.

The timer fills silence only. A client that is behind is already receiving
lines, and a burst on top of a backlog would only push more of them off the
500-line outbox, so the writer drains what is queued before the timer speaks.
`bps_now` is the daemon's own live speed; a `SLOT` line's `bps` is that
transfer's bytes over its own elapsed time, and reads `0` for the first half
second. Today's figures are the rolled ones, the same the advert shows.

### Pairing: a credential that is not the password

A script has to log in without a person typing, which means a credential
stored on disk. That should not be the admin password: the same string opens
the dashboard, and a `.mrc` file is not where it belongs. So a script is
**paired** instead:

```
pair dccore.mrc 1.0
```

The bot mints a random token (43 characters), stores only its PBKDF2 hash in
`data/adminchat_tokens.json` under the client's name, and sends the token back
once - as `DCCORE TOKEN dccore.mrc <token>` on a structured session, as a
plain line otherwise. Paste it into the script's settings; it is not shown
again. From then on the script answers `Enter Your Password:` with the token
and is logged in exactly as with the password: the same hostmask check
first, the same three attempts, the same IP block.

**The script only sends the token to the bot it paired with.** It dials the
bot's nick by itself, and on Undernet anyone can take a nick while the bot is
away, so before answering `Enter Your Password:` it compares the host the nick
has now with the one the bot had when the token was stored (`bothost` in
`dccore.ini`, learned when the token arrives). A different host is not sent the
token: the window says so and the script stops reconnecting by itself. If the bot
really has moved, `/dccore trust` accepts its current host. A script paired
before this check learns the host the first time the bot's is known; if it is
not known yet (you share no channel with it) the token waits for `/dccore trust`.

The same check guards the password prompt in a chat the script opened by itself.
With no token to send (you never paired, ran `unpair`, or are pairing while the
bot is away) the script used to put "Type the admin password here" in the window
for whoever held the nick. Now a chat you opened yourself with `/dccore connect`
or `/dccore pair` asks as before - that is your own act - but one the script
dialled after a JOIN, an IRC connect or a retry asks only if the nick is at the
host the bot is known to have (`bothost`); otherwise the window says so and the
script stops reconnecting until you `/dccore connect`. A script with no
`bothost` yet learns the host the first time it is known, as for the token.

What a token does **not** do is open the dashboard. The web login checks the
admin password hash and nothing else - the token store is never read there -
so a stolen token costs you a console session and nothing more, and one
`unpair` ends even that. For `dccore.mrc` the file that holds it is
`dccore.ini` beside the script, and it is clear text: mIRC's hash-table save
writes the token readable. The `.mrc` itself carries nothing. Keep `dccore.ini`
as you would a password file - a copied mIRC folder or a shared PC is where it
travels - and `/dccore unpair` the moment you think it has. Pairing the same name again replaces the old token -
which is why the script does not pair as the literal `dccore.mrc`: it pairs as
`dccore.mrc-<8 hex>`, the tail derived from the mIRC folder it is loaded from,
so a second machine (or a second mIRC on the same one) gets a name and a token
of its own instead of silently revoking the first. The same copy pairing again
still replaces its own token, which is how a lost one is rotated.

The web login also refuses a password sent by a page on another site: a
browser names the sending page in the `Origin` (or `Referer`) header, and when
that is not the address the dashboard was opened at, the attempt is answered
403 and not counted. Without this, any website open in the same browser could
post three wrong passwords to `127.0.0.1:8420` and lock you out of your own
dashboard for fifteen minutes, again and again. If you reach the dashboard
through a reverse proxy, the proxy must pass the `Host` header through
unchanged (`proxy_set_header Host $host;` in nginx), or every login is refused
with "This login was sent by another site".

```
unpair                    list the paired clients and when they were paired
unpair dccore.mrc-3f9a12c0   revoke one; its next login is a wrong password
```

`pair` and `unpair` are console commands: you have to be logged in - with
the password or with a token - to mint or revoke one. The file lives where
**Settings → Advanced → Paired console scripts file** points.

## The window, in mIRC

`scripts/mirc/dccore.mrc` is the client the feed above was designed for:
the bot's whole life in one mIRC window, so that running DCCore feels no
different from running a script inside mIRC. It needs **mIRC 6.10 or
later** - everything it uses dates from mIRC 6.x - and a bot that answers
`hello`: the DCCore this script ships with, or a later one. On an older bot
it still works as a plain console, without the panel.

### First time

Save the file anywhere (your mIRC folder is fine) and, in mIRC:

```
/load -rs dccore.mrc
/dccore pair MusicBot
```

with your bot's nick in place of `MusicBot`. The `@DCCore` window opens,
the chat is offered exactly as `/dcc chat` would (path 1 or 2 above, as
the bot decides), and when the bot asks for the password you **type it in
the window, once**. The script then sends `pair dccore.mrc-<id> 1.1` - the
id is this mIRC install's own, see "What a token does not do" above - keeps the
token the bot answers with in `dccore.ini` beside the script (in clear
text - see "What a token does not do" above), and from then on connects
and logs in without you: on `/dccore connect`, when mIRC connects to IRC,
and whenever the bot's nick joins a channel you share. The token opens the
console and nothing else; the password never touches the disk.

If your client cannot be dialled and the bot offers the chat back (path
2), mIRC shows its usual incoming-chat dialog the first time - accept it,
or add the bot with `/dcc trust <botnick>` and set **Options → DCC → On
Chat request** to auto-accept so it never asks again.

### What you see

| where | what |
|---|---|
| the text | one line per event, mIRC's own timestamp, a bold coloured tag - `[REQUEST]`, `[SENDING]`, `[SENT]`, `[FAILED]`, `[QUEUED]`, `[SEARCH]`, `[JOIN]`, `[BAN]`... - then the event in plain words, the file name in its own colour |
| the side panel | **Sending n/m**: each running transfer with its size, percentage and speed; **Queue n**: who is waiting, in order, with `frozen m:ss` on a queue that is counting down; **Today**: files and bytes sent, the speed record; and what this window has seen since it opened |
| the title bar | `MusicBot on Undernet · slots 2/3 · queue 14 · today 38 files / 12.4GB · 1.5MB/s`, updated with every status burst |
| the editbox | anything you type is a console command - `status`, `queue helen`, `clearqueue ivan`, `ban *!*@bad.host` - and the reply comes back as `[CONSOLE]` lines, or into a second `@DCCore-console` window if you prefer |
| right-click | the common commands; on a panel line, that user's queue or clearing it; in any channel's nick list, **DCCore → Queue of / Clear the queue of** that nick |
| the window's button | on the switchbar or treebar, like any channel's: the **message** colour when there is new activity - a request, a queue position, a send, a search - and the **highlight** colour (the one mIRC uses when somebody says your nick) on a failed transfer or dropped lines, so a failure stands out. The `[STATUS]` line, joins, parts and bans do not light it, as they would not in a channel. mIRC 7 or later |
| a beep | on a failed transfer, if you leave that on |

Every five minutes a `[STATUS]` line summarises the numbers in the text
too, so scrolling back shows how the day went. A bot that goes quiet for
90 seconds is treated as gone and the chat is reopened; a chat that
cannot be opened is retried after 5 s, 15 s, 60 s and then every two
minutes. An offer the bot never answers - mIRC's own `Waiting for
acknowledgement...` never gives up - is closed after 75 seconds and
retried the same way. Closing the window closes the chat and stops the
retries; `/dccore connect` starts them again.

### Options

`/dccore options` (or right-click → Options...):

- a tickbox and a colour for each kind of event - requests, queue
  positions, sends, failures, searches, joins/parts/quits, bans, other log
  lines - plus the colour of file names, of console replies and of the side
  panel's headings, and how
  often the `[STATUS]` line is written (0 = never);
- the side panel, the title bar figures, console replies in a separate
  window, the beep, the fixed-width font and its size (the Status window's
  size until you set one - on a high-resolution screen you may want a
  bigger number), and the window's background colour (one of mIRC's sixteen,
  or "none" to leave the window as mIRC has it). mIRC has no per-window
  colour setting, so the script writes a one-pixel picture of the colour
  beside itself (`dccore-bg-<n>.bmp`) and tiles it behind the text;
- the bot's nick, whether the script reconnects by itself, the pairing
  state with **Pair again...** and **Forget token**.

These are the script's own filters, kept by mIRC in `dccore.ini`. The
bot's **Settings → Console feed** tickboxes remain the ceiling on what is
sent at all: what is off there never reaches the script.

### Commands

```
/dccore pair <botnick>       first time: connect, log in once by hand, keep a token
/dccore connect [botnick]    open the window and the chat (logs in with the token)
/dccore disconnect           close the chat and stop reconnecting
/dccore unpair               forget the token here and revoke it on the bot
/dccore trust                accept the bot's current host as the one to send the token to
/dccore options              what to show, colours, panel, title bar, beep
/dccore window               open or focus @DCCore
/dccore status               ask the bot for its status
/dccore lists                the bots' lists we hold, and which have changed
/dccore fetch [bot]          ask the bots whose lists changed, or one bot
/dccore raw <command>        send any console command
/dccore panel on|off         the side panel
/dccore font <size>          the window's font size, e.g. /dccore font 14
```

### Updating the script

A newer bot may send a line with a field the loaded script does not know
about. Save the new `dccore.mrc` over the old one, then in mIRC:

```
/reload -rs dccore.mrc
/dccore connect
```

`/reload` re-reads the file in place and keeps your settings and the stored
token (they live in `dccore.ini` beside it); `/load` would add a second copy.
The window says so itself when the two sides disagree: *"speaks feed 1.2 and
this script was written for 1.1"* means update the script; the same line the
other way round means update the bot. The bot checks in the other direction
too: a script older than the one its lines were written for is told
*"Update the script"* right after `hello`.

### If something is off

- **Channel names or nonsense numbers where the position, slot or size
  should be** - the bot and the script disagree on where a field sits; the
  script and the bot ship together, and one of them is older. The window
  says which when it connects (see "Updating the script" above). A script
  from before the channel field was added does not say so: it just shows
  the channel as the next number - update it.
- **Non-ASCII file names look garbled** - mIRC 6 shows text in your
  Windows code page and the bot sends UTF-8. mIRC 7 decodes the chat as
  UTF-8 and shows them correctly; the script is the same file on both.
- **"Plain mode" in the window** - the bot is from before `hello` and
  does not answer it; the window shows the chat as it comes, with no
  panel. Or the bot speaks a newer protocol than the script - a script
  from before the version carried a minor refuses `1.1` this way and says
  "Update the script": do that (see "Updating the script" above).
- **The stored token is refused** - it was revoked on the bot (`unpair`),
  replaced by pairing the same name again (the same mIRC install; another
  machine pairs under its own name and leaves this one alone), or the token
  file was moved; `/dccore pair` again, typing the password once. The script does
  not send a refused token again and does not redial by itself until you
  log in or pair again: a refusal counts as a wrong password, three of them
  block your address for 15 minutes, and left to itself the redial would
  reach that in about two minutes.
- **You are on more than one network** - the script remembers which
  network the bot is on from the moment you type `/dccore connect` or
  `/dccore pair` there (or from the bot's own join), and every later dial -
  on connect, on the bot's join, on a retry, or `/dccore connect` typed in
  a window on another network - goes to that network. Pairing again from
  another network moves it. `/dccore version` says which network it holds.
- **Two people with the script** - the console is one session, and a
  login replaces the one before it. The client that was replaced says so
  and does not reconnect by itself, so the two of you take turns rather
  than trading it every few seconds.
- **"No answer from <bot> in 75 seconds"** - the bot got the offer and
  said nothing back. It refuses in silence when your host is not in
  `ADMIN_HOSTMASKS`, when your address is blocked for 15 minutes after
  three wrong passwords, or when it could not reach your client and its
  own offer back was dropped (see `ADMIN_CHAT_MODE`); the bot's own log
  says which. The script keeps retrying with the usual backoff.

## Limits and timeouts

| | |
|---|---|
| Time to enter the password | 60 seconds, then the socket closes |
| Password attempts | 3, then the socket closes and your IP is blocked |
| IP block after failed attempts | 15 minutes |
| Idle timeout once logged in | none — the console stays open until you close it, log in again from elsewhere, or the connection drops |
| Sessions at once | 1 |
| Lines queued for a slow client | 500, then the oldest are dropped (a structured session is told how many) |
| Structured status burst | every 30 seconds while quiet, and after any slot or queue change |

**A second login replaces the first.** If you left a session open on another
machine, or your client froze and the server has not timed the nick out yet, just
connect again — the old session is told it was taken over and closed. The
replacement happens only *after* the new session authenticates, so nobody who
merely matches the host can drop your live console without the password.

---

## When it does not work

**The bot ignores you completely — no reply, nothing.**
This is by design: an unrecognised host gets no answer at all, so a stranger
cannot learn whether they guessed the mask correctly. It also means a wrong mask
looks identical to a broken bot. Check the daemon log:

```
[ADMINCHAT] Ignored DCC CHAT from unauthorised host: cpe-198-51-100-7.isp.net
```

That line tells you the host the server actually saw. Usually it means `+x` is not
set, or `ADMIN_HOSTMASKS` has a typo - or was never set at all: `configure.py`'s
services-host question is optional, and an install where it was left blank at
setup is still missing step 3.

**The log says the password is not set.**

```
[ADMINCHAT] DCC CHAT from an authorised host refused: ADMIN_PASSWORD_HASH is not set.
```

Step 2 was skipped, or the value did not reach `admin_config.py`. A console with
no password refuses everyone rather than letting anyone in.

**The log says the connection to you timed out.**

```
[ADMINCHAT] Could not connect to operator at 203.0.113.41:55101 (timed out);
falling back to listening.
```

A **timeout** rather than a refusal means the packets left and nothing came back:
the address your client advertised is not reachable from the daemon. Usual
causes, most likely first:

1. **A VPN.** Your client reports the VPN's exit address, but inbound
   connections to it are not forwarded back to you.
2. A router not forwarding the port to your machine.
3. A firewall that drops rather than rejects.
4. The daemon's own outbound blocked to high ports.

A port-checker saying "open" is weaker evidence than it looks: your client's DCC
listener exists only while a request is pending, so a check run at any other
moment is testing something else — usually a forwarding rule rather than a live
listener.

You do not have to work out which it is. Set `ADMIN_CHAT_MODE = "listen"` and the
bot stops dialling you altogether. The listener it opens answers only a connection
from the address your client advertised in its CTCP, or from any private-network
address (#881) - if you and the bot share one home router, your client advertises
that router's public IP, but your own connection can arrive at the bot with a
private LAN address instead (a NAT hairpin), which the exact match alone would
reject as a stranger. Anything else that reaches the port during the window - a
public address that is neither one - is dropped without a banner, logged as
`Dropped a connection from <ip> ... Still waiting.`, and the port stays open for
you. (A passive request advertises no address, so there the first connection is
taken.)

**The log says it could not connect to you at `0.0.0.0`.**

```
[ADMINCHAT] Could not connect to operator at 0.0.0.0:11283 ([Errno 111] Connection refused).
```

Your client did not know its own address and offered `0.0.0.0`. That is not a
harmless blank: on Linux, connecting to `0.0.0.0` means "this host", so the
daemon dialled *itself* and found nothing listening. Newer builds detect this and
fall back to listening on the DCC port range instead, so it should now just work.
Fix it properly in mIRC under **Options → Connect → Local Info** as above.

**The log says "Unusable DCC CHAT offer".**

```
[ADMINCHAT] Unusable DCC CHAT offer from operator: 'DCC CHAT chat 3405803861 0 350'
```

That particular one was a parser bug, now fixed — the offer above is a valid
passive request (`0` for the port, `350` as the token) and is handled. If you
still see this line, the text after the colon is what the client actually sent;
anything that is not `DCC CHAT chat <ip> <port>` or `DCC CHAT chat <ip> 0
<token>` is genuinely unusable.

**"No free port in 55000-55010 for the console."**
Every port in the range is busy with transfers. Wait for one to finish, or widen
`DCC_PORT_START`/`DCC_PORT_END` — remembering to forward the wider range too.

**"address is temporarily blocked."**
Three wrong passwords from that IP. Wait 15 minutes, or restart the daemon — the
block lives in memory only.

**Locked out entirely.**
Edit `admin_config.py` and restart. While `ADMIN_CHANNEL_COMMANDS` is on (it
ships on), the channel commands still work, so you are never without a way in.

---

## What this does and does not protect

**Protects against:** someone taking your nick while you are offline; a hostmask
alone without the password; a stolen password used from the wrong host; brute
force, via the attempt limit and the IP block.

**Does not protect against:** anyone who has compromised your Undernet X account
*and* knows the password. The DCC CHAT connection is plaintext, so the password
crosses the wire in clear — the host check is the real gate, and the password is
depth behind it. Optional TLS is on the list for a later phase.

---

## Coming next

Optional, and not built: TLS on the chat (Python's `ssl` is stdlib, and iroffer
supports it), and iroffer's second restricted admin tier (`hadminhost`).
