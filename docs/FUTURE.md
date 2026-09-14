# Roadmap

What DCCore does today, and what it does not do yet.

**How to use this file:** everything under *Implemented* is in `main` and working. Everything under *Planned* is not, whatever a comment or an old changelog entry might imply. When something is built it moves up, in the same commit that builds it — a roadmap that is edited afterwards is a roadmap nobody trusts.

---

## Implemented

### Serving files

- **DCC SEND over IRC**, with a per-user and global queue, configurable slot limits, and a DCC port range you control.
- **Album packing** — `!rar <folder>` builds an archive on demand and cleans it up afterwards, bounded by `MAX_RAR_FOLDER_SIZE` so a request cannot ask for an unbounded pack.
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
- **Optional web dashboard** — Search, Queue, List Browser grouped by folder, Downloads, a duplicate-filename verifier (which the build now warns about too, for operators who never open the dashboard), a list rebuilder, a Settings page, and a Console (the DCC CHAT admin console's commands and live log, in the browser — for an operator who wants neither a second IRC client nor a debug channel). Off by default, loopback by default, behind the same password as the DCC CHAT console.
- **Purging a fetched list** - the manual half of the fetched-list purge, in two shapes: one bot at a time from its own list, and every offline bot at once from the toolbar. Removes the entry, the extracted files and the search index rows together. An automatic TTL is still open and deliberately second: `fetched_at` answers list staleness, not "this bot is gone", and a timer that deletes an operator's data by default is a surprise waiting to happen.
- **Messages people send the bot** - a private message that is not a command gets no reply, and now leaves a record: a Messages page with an unread count, throttled per sender. The bot still never answers. Turning it off (`PRIVATE_MESSAGES_ENABLED = false`) keeps nothing, hides the page and its menu entry, and tells the sender once where to go instead - a NOTICE, once per person per day, under a burst ceiling, on the ordinary send lane.
- **A notice badge in the dashboard** - the short list beside the long one. Kicks, channels given up on and failed rebuilds raise a counted, colour-coded notice in the status panel, kept across restarts; everything else stays in the Console's log where it belongs. See [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md).
- **Guided first-run setup** — `python3 configure.py` asks a short series of questions and writes a working configuration.
- **Pre-flight check** — `start-dccore.sh check` verifies the setup without opening a socket.
- **Two configuration mechanisms** — `admin_config.py` for Python, `settings.conf` for plain text; the dashboard and console both write to the latter.
- **`!rehash`** reloads code and settings live, preserving queues and transfer state.
- **Channel adverts** on a timer, with a per-bot theme (five presets, or your own colours).
- **Statistics** — totals, today and yesterday, per-file download counts, a speed record, and a live rate.

### Quality

- **4746 tests**, on Linux and Windows, Python 3.10, 3.12 and 3.14, in CI on every push and pull request.
- **Stdlib-only** — the daemon and its test suite need no third-party packages; Flask is required only for the optional dashboard.
- **No reloaded module owns a lock** — `!rehash` re-executes a module body, so a module-level `threading.Lock()` is rebound while a thread is still inside it. Every lock in a reloaded module is allocated in `runtime.py` and bound by name, and `tests/test_no_reloaded_module_owns_a_lock.py` fails if a new one appears — the class, not the four instances that prompted it.
- **A cross-list search index** — SQLite FTS5, built as each bot list is fetched, so the dashboard can filter every held list live rather than re-reading them at 2-11 seconds a keystroke.
- **Two adversarial audits** — an internal audit (32 defects, all fixed) and a pre-publication sweep before the first public release.

---

## Planned

Ordered by what unblocks what, not by preference.

### Multiple lists, and multiple folders per list

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

**Stage 3 is in.** A request is answered from the list bound to the channel it arrived in. The rule has three parts: an explicitly bound channel gets its list; otherwise the primary answers *if it binds no channels of its own*, which is what every install today is and what stops this being an upgrade that silences every bot; otherwise nothing, which is what makes binding mean something. A private message is always the primary, since it carries nothing to route on. The list request, `@find` and file requests all route; a channel bound to nothing is answered with silence rather than an error, because an error implies something went wrong and nothing did.

**Stage 4 is in.** Each channel advertises the list it actually serves. The advert loop already read the figures once per channel — it just read the same ones every time — so this is the loop asking which list first, and skipping a channel with none bound. The advert is where the multi-list rule is most visible: a bot silently present in a channel it does not serve, rather than one announcing a library it will refuse to send from.

**Stage 5 is in, and #26 is complete.** The Settings page's Paths category defines them: a name, which channels it serves, its folders, and which one is primary. With one list the familiar folder editor stays exactly where it was and a button moves you to the list editor — one editor, never two, because a second place to edit folders that quietly does nothing is worse than either alone. `GET`/`POST /api/lists` validate the whole set and report every fault at once.

Two pieces were worth doing carefully rather than quickly, and one of them turned out the opposite way to what this section used to predict:

- **Containment.** This said `is_safe_path()` would become "inside *any* configured root", and called that the one place a mistake is a security bug rather than an inconvenience. Right about the risk, wrong about the answer: widening it that way is a strictly weaker test. Because a heading names its own folder, resolution returns *which* folder it landed in and the check runs against that one — the same strength as when there was only ever one. `is_safe_path()` itself was never touched.
- **Index identity.** Two folders can hold the same relative path — the same album in flac and in mp3 is the ordinary case — so an entry has to record which folder it came from. That is the label leading every path, and it is what makes the containment answer above possible.

### Test coverage where it is thinnest

The pre-publication audit found **21 daemon functions with no behavioural coverage at all** — `!rehash`, `@<nick>-que`, the advert worker, the IRC read loop, `configure.py`'s entry point. That gap is closed: `scripts/function_coverage.py` reports **2 of 310 uncovered, both on the allowlist with a written reason**, and it fails the build on a third.

What remains is narrower and does not show up in that number. Several "the wiring is in place" guards read the source as text rather than executing it, so a call moved behind a disabled branch would still not be noticed — this file's own history has three such guards that passed against deliberately broken code, and the v1.12.0 work found three more the same way, each caught by mutation rather than by review.

**The largest single gap is closed.** `_handle_rehash_request()` — seven hundred lines that nothing in 120-odd test files had ever executed, while the dashboard triggers it on every settings save — now runs for real in `tests/test_rehash_end_to_end.py`, in a subprocess so the reload cannot touch the runner's own imports. It asserts what survives: the changed setting, a user's queue, the channel lists, a timed ban, a freeze timer, the advert token, and that the bot is not left paused. Re-measured: it was **four**, not nine, and they now have HTTP-level tests (`tests/test_every_route_is_behind_the_login.py`). Their builders had always been well covered — six to twelve tests each — so what was missing was the wiring in front of them: that the path resolves, that the method restriction is real, and that the JSON envelope comes back.

The same file closes a larger gap found alongside it. All 37 dashboard rules sit behind one `before_request` hook and not a single per-route decorator, which is the right design — a decorator is a thing somebody can forget — but it put the whole authentication story on one function that nothing tested as a whole. An audit probed every rule unauthenticated and found none reachable, so it held; now a test walks `url_map` itself, so a route added tomorrow is covered without anyone remembering the test exists.

### From the audits

**The v1.12.0 audit left the list this section used to ask for.** Six independent lenses — security, concurrency, the transfer path, lists, the IRC surface, and persistence — each adversarially refuted before anything counted, followed by a second, pre-publication sweep of the actual public export itself. **44 findings confirmed, and all 44 are closed: 42 fixed, 2 recorded in tests as considered and deliberately not changed.** Every one is written up in `docs/UPDATES.md` with its failure scenario, so the next audit starts from a record rather than from "roughly forty".

Two of the two-not-changed are worth knowing about before somebody "fixes" them: a bot-alone `list` row may claim an offer that was a near-miss for a `file` row from the same bot (refusing the fall-through would reject legitimate list replies), and the passive DCC reply goes through the outbound pacer while the accept clock runs (sending it unpaced is what `queue_mgr` exists to prevent; raising `PASSIVE_LISTEN_TIMEOUT` is the lever with no such risk).

- **The earlier audits' findings still were never written down.** That said "roughly forty" and named two, one of which is fixed. The rest can be neither confirmed nor worked from, and a great deal has been fixed since. Still treated as unknown rather than as a backlog — but it is now the only part of the audit history that is.

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

We have the same problem in the other direction, and it is not hypothetical:
`update_list.py` writes `!{config.NICKNAME}` into every line of our own list at
build time, and `irc.py` rebinds `config.NICKNAME` to the alt nick on a 433.
A rebuild while we are on the alt nick therefore ships a list whose every line
names the alt nick.

Open: what is the stable identity — the services account, the host, an operator
mapping in settings? And is the fix to normalise at fetch time, to rewrite the
request lines on the way out, or only to merge the two rows in the sidebar?

**Some bots we want are not advertising, and some advertising bots are not
reachable.** Three cases, none of which the registry covers today:

- A bot that answers CTCP but never advertises in the channel. It never
  reaches `known_bots`, so it does not appear at all — even though `@botnick`
  and `!botnick <track>` would both work.
- The opposite: a bot that advertises but does not answer. It appears, and
  clicking it starts a fetch that goes nowhere.
- A bot that does neither, where the operator simply knows that `@botname`
  gets the list and `!botname <track>` gets files, and wants to add it by
  hand.

So the source list probably needs a manual entry — an operator-added bot,
persisted separately from the advert-built registry so a prune cannot remove
it, and marked in the sidebar as added rather than seen. Open: whether a
manual entry should be probed to confirm it answers, and what the sidebar
should say about one that has never replied.

### Smaller things worth having

- **PER-LIST file exclusions** (`Exclude = .mpu,.db`) — OmenServe has them per list. `LIST_IGNORED_EXTENSIONS` does this globally; scoping it to one folder is the part still missing.
- **A fetched list keeps only the peer's master.** Since the film-and-series split, a DCCore bot's archive carries two `.txt` files, and `list_fetch` picks one - now the master rather than whichever is larger. The films in the other are dropped from the fetched copy. Reading both into one fetched list changes what `_pick_list_file()` returns and the size ceiling that guards it, so it is a change of its own rather than part of the fix.
- **Stealth channels** — serve a channel while advertising nothing in it.
- **Multi-network** — real in OmenServe, and it would touch every socket path here.

---

## Not planned

- **Multi-network before multi-list.** It touches more and is wanted less.
- **Rewriting the mIRC theme engine.** The colour blocks are what makes a DCCore bot recognisable in a channel.
- **Adapting the DCC packet size during a transfer.** The block size is not what limits a transfer. `scripts/send_benchmark.py` reaches roughly 950 MB/s at 64 KB on loopback - far above any real link - so there is no headroom for a larger write to win back. What actually bounds a fast link to a distant peer is the socket send buffer against the round-trip time, which is what `DCC_SEND_BUFFER` exists for. And a slow receiver is already handled: TCP flow control blocks the send when the far end stops reading, so adapting the write size downward would be reimplementing, badly, something the kernel does correctly. The setting stays a menu the operator picks once.
- **TLS for the dashboard.** It is a loopback tool by design; anyone needing it across a network should put a reverse proxy in front rather than have the daemon grow a certificate story.
