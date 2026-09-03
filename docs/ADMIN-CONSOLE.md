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

A console that has been switched on but is *not connected* counts as nobody
listening, and so does a console whose sink raised. Both fall through to stdout.

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
| Idle timeout once logged in | 30 minutes |
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
[ADMINCHAT] Ignored DCC CHAT from unauthorised host: cpe-91-22-33-44.isp.net
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
