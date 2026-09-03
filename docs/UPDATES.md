# Changelog

## v1.10.0 — General Availability

The first stable release, and the first public one.

**What it is:** an IRC file server. It serves files over DCC, keeps a searchable
master list of what it has, and — unlike the servers it grew out of — can fetch
files and lists *from* other bots as well as send them. Python 3.10+, GPLv3.

**No third-party dependencies.** Everything the daemon needs to talk to IRC,
serve files over DCC, pack albums and run its admin console is in the standard
library; `pip install -r requirements.txt` installs nothing. Flask is optional
and only for the web dashboard.

**Starting it** is a guided run of `python3 configure.py`, then a pre-flight
check that verifies the setup without connecting to IRC. Upgrading from an
earlier build migrates `local_config.py` to `admin_config.py` automatically.

### Security and reliability

- **Two audits.** The first closed 32 issues: forged IRC messages that could trigger admin-only behaviour, a channel-wide ban pattern that could lock out an entire channel, an unbounded fetch queue, and non-ASCII filenames that could corrupt on transfer, among others.
- **A second, deeper audit before publication** closed a further set, including: one `@find` refusing every other user's file requests while it ran; a queued `!rar` folder pack that was never re-dispatched and could wait forever; renaming the bot orphaning its entire master list, invisibly; an escalation ban whose length depended on what time of day the flooding happened; cross-bot fetch history that grew without limit for the life of the process; and a nickname containing `[ ]` — an ordinary IRC convention — making the bot advertise an empty library permanently.
- **The admin console's host gate no longer accepts `*.*`**, which matched essentially any real host. That gate is the first of the two factors deciding who reaches the password prompt at all.
- **`!rehash` no longer breaks mutual exclusion.** Reloading a module rebound every lock defined in it, so a thread already inside a critical section kept an object nothing else could see. It also restored a stale queue snapshot over live state, discarding requests that arrived during the reload and re-sending transfers that had completed during it.
- Several race conditions around shared in-memory state (channel membership, the fetch queue, stats) are fixed.

### Configuration and operation

- **Web dashboard**: a Settings page that writes configuration directly, a Stats page, a light theme, and login required on every route. It is built for a trusted LAN and has no TLS, so a login gate does not stop someone on the same network segment reading the password off the wire — do not expose it to the open internet.
- The master list can be downloaded as `.txt`, `.zip` or `.rar`; live transfer speed is now visible outside the channel advert.
- `NICKNAME`, `CHANNEL` and `ADMIN_NICK` are now required settings — a fresh install can no longer silently start under someone else's identity — and none of them can be blanked from the dashboard afterwards, which used to leave an install that could not start and no dashboard to fix it from.
- **`DEBUG_CHANNEL` ships blank.** It used to default to a real shared channel that every install joined automatically, streaming its internals to everyone else in it.

### Testing

- **Every public function is checked against a profiler**, not just a test count: fifteen of the daemon's 205 public functions were never entered by any test the first time this ran — including `!rehash`, the command that sends a user the file list, and the IRC read loop. Two remain uncovered, both `while True:` loops that own the process, each recorded with the reason.
- Linux and Windows, on two Python versions, on every push and pull request.

## v1.10.0-RC4 — The Web Dashboard Release

- **Web dashboard**: search, queue and file-list views served alongside the IRC daemon. Optional — the daemon runs fine without Flask installed, and it is off by default. Intended for a trusted LAN: there is no TLS.
- **Cross-bot search and fetch**: the dashboard can broadcast `@find` and collect replies from other bots, and can now *receive* files and file lists from other bots, not just send them.
- Fixed: a CRLF/CTCP injection issue found in three separate input paths; a stored XSS issue in the dashboard's result rendering; `!rehash` silently clearing the fetch queue; several path- and archive-handling issues in the cross-bot fetch feature (zip-slip protection, Windows long-path handling, exception classes that could escape the extraction guard).
- Every setting now declares its own type, closing a class of bug where an unset value had no type to infer from.

## v1.10.0-RC3 — The English & Reliability Release

- Every module's comments and log output translated to English.
- Fixed: the daemon could crash on non-Western console code pages; redirected logs could lose everything buffered when the process was killed; concurrent transfers could lose stats under a race; Windows library paths over 260 characters were silently unservable; a memory leak in ban-notification tracking.

## v1.10.0-RC2 — The Admin Console Release

- **Authenticated admin console over DCC CHAT**: replaces a nick-based admin check (which anyone could inherit by taking the admin nick) with two-factor authentication — the operator's IRC services login plus a hashed password compared in constant time.
- Full remote command set: status, queue, slots, bans, uptime, version, ban/unban, clearqueue, rehash, update.
- Fixed: a DCC dial-to-self bug when a client's IP failed to resolve; list side-files not rolling back together with a failed list rebuild; control characters corrupting the master list.

## v1.10.0-RC1 — The Platform & Forgery-Hardening Release

- **Windows support**: every genuine Linux/Windows difference isolated into one module; the daemon now runs on both platforms, verified in CI.
- **Per-machine config overrides**, without editing a tracked file.
- Fixed several message-forgery bugs: channel text could trigger server-numeric or user-event handlers meant only for real IRC events (a chat message containing the word "QUIT" could freeze that user's queue). Also fixed: orphaned temporary archives after a queue removal, a ban file that could be silently truncated on a crash, and a couple of flood-protection gaps that left some commands unmetered.
- **Known open item at this release** (fixed in RC2): the admin check was still nick-based with no host verification.

## v1.9.0 — The Gold & Audio Handshake Release

- Filenames containing an apostrophe now pack correctly into `.rar` archives.
- Multi-disc box sets are now packed as one archive automatically on request.
- The bot resumes sharing automatically after a restart, with no operator command needed.
- Fixed: a duplicate channel announcement per transfer; the advertised per-slot speed was wildly inflated; a case-sensitivity bug that could stall the queue after startup or a reload.

## v1.5.0 — The RAM Dictionary Queue & Inline RAR Packer Update

- The transfer queue moved from flat text to structured in-memory records.
- Albums are now packed into `.rar` on the fly rather than requiring a pre-packed archive, using a working directory that never touches the shared library.
- Locking added to prevent concurrent packs from fighting over CPU and disk.

## v1.4.x — Search, admin tooling and stability

- Search terms are sanitised so punctuation in a query no longer breaks matching, and matching became word-based and order-independent.
- Library reindexing can be triggered from IRC without shell access to the host.
- Permanent wildcard ban patterns, and IRC-based `!ban`/`!unban` commands.
- Live `!rehash` reloads every core module without restarting the process; a channel-sync step keeps joined channels matching configuration.
- Assorted stability fixes: a circular-import deadlock at boot, a name conflict between the bot's own `list` module and Python's built-in type, corrupted debug-log formatting.

## v1.1.0 – v1.2.0 — Early architecture

- A priority send path for system/admin messages that bypasses the normal flood-protection queue.
- The command parser rebuilt to stop duplicate replies in channels.
- Statistics tracking made accurate and durable across restarts.
