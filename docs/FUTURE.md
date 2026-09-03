# Roadmap

What DCCore does today, and what it does not do yet.

**How to use this file:** everything under *Implemented* is in `main` and working. Everything under *Planned* is not, whatever a comment or an old changelog entry might imply. When something is built it moves up, in the same commit that builds it — a roadmap that is edited afterwards is a roadmap nobody trusts.

---

## Implemented

### Serving files

- **DCC SEND over IRC**, with a per-user and global queue, configurable slot limits, and a DCC port range you control.
- **Album packing** — `!rar <folder>` builds an archive on demand and cleans it up afterwards.
- **Freeze box** — a user who parts or quits keeps their queue for five minutes; rejoining thaws it instantly rather than losing their place.
- **Anti-flood** — a rolling request window, temporary mutes, and escalation to a day-ban for anyone who keeps going while muted.
- **Ban list** — hard bans by hostmask pattern, timed bans, and a guard that refuses a pattern matching everyone.
- **Long paths and non-ASCII filenames** work on both platforms: Windows `MAX_PATH` is handled throughout, and Greek, Cyrillic and CJK filenames survive both ends of the IRC connection.

### The file list

- **One master list**, rebuilt by `!update` or on a schedule, published atomically so a failed scan never overwrites a good index.
- **Three formats** — `.txt`, `.zip` and `.rar`, all built every time; `LIST_FORMAT` picks which one is offered.
- **Search** — `@find <words>` against the master list, with results fitted to the IRC line limit.
- **A partially unreadable library fails the rebuild** rather than silently publishing a truncated list.

### Receiving files from other bots

- **Cross-bot fetch** — request a file with `!<bot> <filename>` or a whole list with `@<botnick>`, and track it from the dashboard.
- **Broadcast search** — send one `@find` to a channel and collect every bot's reply, grouped under each bot's own parsed header.
- **Both DCC directions** — active and passive/reverse SEND, since bots behind NAT use the latter.
- **Hostile-input handling** — every other bot is treated as untrusted: admission control, size caps, and zip-slip / zip-bomb guards on any archive received.

### Operating it

- **Authenticated admin console over DCC CHAT**, gated on the operator's services host *and* a PBKDF2-hashed password. Read-only commands (`status`, `queue`, `slots`, `bans`, `uptime`, `version`) and action commands (`ban`, `unban`, `clearqueue`, `rehash`, `update`) — see [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md).
- **Optional web dashboard** — Search, Queue, File Lists grouped by folder, Downloads, a duplicate-filename verifier, a list rebuilder, and a Settings page. Off by default, loopback by default, behind the same password as the console.
- **Guided first-run setup** — `python3 configure.py` asks a short series of questions and writes a working configuration.
- **Pre-flight check** — `start-dccore.sh check` verifies the setup without opening a socket.
- **Two configuration mechanisms** — `admin_config.py` for Python, `settings.conf` for plain text; the dashboard and console both write to the latter.
- **`!rehash`** reloads code and settings live, preserving queues and transfer state.
- **Channel adverts** on a timer, with a per-bot theme (five presets, or your own colours).
- **Statistics** — totals, today and yesterday, per-file download counts, a speed record, and a live rate.

### Quality

- **2071 tests**, on Linux and Windows, Python 3.10 and 3.12, in CI on every push and pull request.
- **Stdlib-only** — the daemon and its test suite need no third-party packages; Flask is required only for the optional dashboard.
- **Two adversarial audits** — an internal audit (32 defects, all fixed) and a pre-publication sweep before the first public release.

---

## Planned

Ordered by what unblocks what, not by preference.

### Multiple lists, and multiple folders per list

The largest gap against OmenServe, which has had both since long before this project started. DCCore serves **one** directory into **one** list.

The design is settled:

- One trigger, unchanged. `@<botnick>` everywhere; no new syntax for anybody to learn.
- A list has an operator-facing name and a set of directories. Names are never typed in a channel.
- A channel is bound to exactly one list; a list may serve several channels. A request uses the list bound to the channel it arrived in.
- A private message uses the list marked primary, since a PM carries no channel.
- A channel with no list bound gets nothing: no advert, no requests answered.

It ships in three steps, each useful on its own:

1. Make "the list" an explicit object with exactly one instance — a pure refactor, no behaviour change.
2. Give that object a directory set. **Multi-folder ships here**, still with one list.
3. Allow more than one instance. **Multi-list ships here**, with per-channel adverts falling out nearly free.

Two pieces are worth doing carefully rather than quickly: containment (`is_safe_path` becomes "inside *any* configured root", and that is the one place a mistake is a security bug rather than an inconvenience), and index identity (two roots can hold the same relative path, so an entry has to record which root it came from).

### Test coverage where it is thinnest

The pre-publication audit found that **21 daemon functions have no behavioural coverage at all** — including `!rehash`, `@<nick>-que`, the advert worker, the IRC read loop and `configure.py`'s entry point. Each can be replaced with a statement that raises while all tests still pass.

Several "the wiring is in place" guards read the source as text rather than executing it, so a call moved behind a disabled branch would not be noticed. Nine dashboard routes are never requested by any test.

This matters most as a prerequisite: step 1 above refactors seven modules that call `find_latest_list()`, and doing that across code the suite does not exercise is how a regression ships quietly.

### From the audits, not yet done

- **`!rehash` rebinds every module-level lock**, so a thread inside a critical section loses mutual exclusion. `runtime.py` already solves this for the containers; the locks need the same treatment.
- **Timed bans grow without bound** — the flood sweep covers two of the three tracking structures.
- Roughly forty further verified findings, from a false "MasterList missing" during a concurrent search to a queued `!rar` pack that is never re-dispatched.

### Smaller things worth having

- **Per-list file exclusions** (`Exclude = .mpu,.db`) — OmenServe has them; everything under the root is currently offered.
- **A "what's new" list** — files added in the last N days, generated alongside the main one.
- **A list validator** run at build time, reporting anything a requester will not actually be able to get — duplicate filenames across folders being the common case.
- **An OmenServe migration path** offered on first run, since almost everyone this is aimed at is running OmenServe today.
- **Stealth channels** — serve a channel while advertising nothing in it.
- **Multi-network** — real in OmenServe, and it would touch every socket path here.

---

## Not planned

- **Multi-network before multi-list.** It touches more and is wanted less.
- **Rewriting the mIRC theme engine.** The colour blocks are what makes a DCCore bot recognisable in a channel.
- **TLS for the dashboard.** It is a loopback tool by design; anyone needing it across a network should put a reverse proxy in front rather than have the daemon grow a certificate story.
