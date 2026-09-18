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
inherit every admin command, including the destructive `!clearqueue`.

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

`python3 configure.py` does steps 2 and 3 together - the same password prompt as
below, writing the resulting hash straight into `admin_config.py` - if you
have not already run it. To do it by hand instead:

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
whole network and make the gate decorative.

### 4. Restart the daemon

`admin_config.py` is read at import. `!rehash` reloads `config`, so it picks up
changes too, but a restart is the sure thing while you are setting this up.

---

## Using it

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
DCCore v1.10.0-RC1 - platform=posix python=3.10 rar=/usr/bin/rar

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
| `queue [nick]` | queued files, all users or one |
| `slots` | what is sending right now, and how far along |
| `bans` | permanent and timed bans |
| `uptime` | how long the daemon has been running |
| `version` | build and platform |
| `verify` | filenames that appear in two folders |

### What you can do

| Command | Effect |
|---|---|
| `ban <pattern>` | add a permanent wildcard ban |
| `unban <pattern>` | remove one |
| `clearqueue <nick>` | force-clear another user's queue |
| `rehash` | reload modules in place |
| `update` | rebuild the MasterList |
| `help` | the command list |
| `quit` | close the session |

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
| `DEBUG_TO_CONSOLE` | the plain text in an attached admin console |

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
no longer on a nick. The user commands — `!list`, `!ping`, `!debugnames`,
`@find`, the queue triggers — are not affected either way.

## Limits and timeouts

| | |
|---|---|
| Time to enter the password | 60 seconds, then the socket closes |
| Password attempts | 3, then the socket closes and your IP is blocked |
| IP block after failed attempts | 15 minutes |
| Idle timeout once logged in | none — the console stays open until you close it, log in again from elsewhere, or the connection drops |
| Sessions at once | 1 |

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
set, or `ADMIN_HOSTMASKS` has a typo.

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
bot stops dialling you altogether.

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
Edit `admin_config.py` and restart. Until phase 2 flips the switch, the channel
commands still work, so you are never without a way in.

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
- **Phase 4, optional** — TLS on the chat, and a second restricted admin tier.
