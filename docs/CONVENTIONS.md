# Conventions

Rules this project has learned the hard way. Each one is here because breaking
it cost something real — a bug in production, a reviewer's afternoon, or a
security hole — not because it sounded tidy.

Written down so they are decided once, rather than argued per pull request.

---

## 1. Anything that listens on a network ships OFF, and binds loopback

A new feature that opens a port defaults to **disabled**, and when enabled binds
`127.0.0.1` unless the operator deliberately widens it. Warnings in comments do
not count: people pull and restart without reading `config.py`, and a default is
the only thing that protects them.

`adminchat.py` is the pattern. `ADMIN_HOSTMASKS = []` ships empty, so the console
is inert until somebody opts in from `admin_config.py`, and every DCC CHAT
request is ignored until then.

**Why it matters here:** the daemon runs 24/7 on machines that are not always
behind a firewall the author was thinking about. "It's read-only" is not a
defence — a read-only endpoint still discloses the queue, who is downloading
what, and the shape of the library.

---

## 2. Never write one fact in two places

If the same value, list, or rule appears twice, the two copies will disagree —
not might, will. Derive it, import it, or read it from one source.

This project has now been bitten **four times**:

| | |
|---|---|
| Issue #34 | `get_speed_record()` read a hardcoded path while `save_speed_record()` used the `SPEED_RECORD_FILE` constant. They agreed only because the daemon happened to start from the repo root. |
| List side files | `dccore-size.txt` was a literal in both `list.py` and `update_list.py`, under its old `flac-serv-` name. `db.migrate_legacy_side_files()` carries the old files across at startup. |
| `!list` | Metered case-insensitively by the flood gate, dispatched case-sensitively — so `!LIST` was charged against your flood budget and then did nothing. |
| Import checks | CI listed 11 modules, `preflight.py` listed 12, the project had 14. `adminchat.py` and `oserve.py` were in neither, so a check called *"every module imports cleanly"* had never looked at the admin console. |

---

## 3. Files that matter are written atomically

Temp file, `os.fsync`, then `os.replace`. Never truncate-then-write.

`db._atomic_write()` exists for this. Use it.

**Why:** `!unban` used to truncate `hard_bans.txt` and write the survivors back
line by line. A crash in between left it short or empty — and that fails **open**,
because `security.check_user_status` cannot tell a truncated file from one with
no bans in it. Every hard-banned user would have been admitted.

`os.replace` rather than `os.rename`: `rename` raises on Windows when the
destination exists.

---

## 4. Match IRC protocol by position, never by substring

A server numeric or a user event is identified by what sits in the **command
position**, right after the prefix. Testing `" QUIT " in line` matches the word
anywhere — including inside a PRIVMSG body, a part reason, or a channel topic.

Use `is_server_numeric()` and `is_user_event()` in `irc.py`.

**Why:** `" 513 " in line and "PONG" in line` let anyone in any channel make the
daemon emit unpaced raw PONGs — a one-paste Excess Flood disconnect. And
`" QUIT " in line` matched the ordinary search `@find QUIT PLAYING GAMES`, after
which the QUIT handler removed that user from every channel, froze their queue,
and the five-minute timer deleted it. For looking up a song.

Read the source nick from the **prefix** too, never by searching the line —
a QUIT reason is free text and can name anybody.

---

## 5. A test must evaluate the real condition, not grep for it

Reading a guard out of the source and running it catches an `or` becoming an
`and`, or two guards being swapped. Searching the source for a string catches
neither, and agrees with itself forever.

`tests/test_server_numerics.py` and `tests/test_irc_dispatch.py` show the shape.

**Why:** an `ADMIN_CHANNEL_COMMANDS` test asserted the flag's *name* appeared in
`irc.py`. A mutation short-circuiting the gate to `True or ...` left that test
perfectly green. Every guard in this project is now asserted by evaluation, and
every fix ships with mutations proving the old broken version turns the suite
red.

---

## 6. A check that cries wolf gets ignored

If a check fails for reasons that are not the code's fault, people learn to skip
its output — and then it stops catching the things it exists for. Skip with an
explanation instead.

**Why:** `preflight.py` strips `PATH` to prove the suite passes without host
tooling. On a machine with `/usr/bin/rar`, the environment cannot be made hostile
from inside the script — and it printed `PREFLIGHT FAILED - do not push` over
what was only ever a bonus check. Likewise `tests/test_adminchat.py` binds real
sockets, and a sandbox forbidding that produced nine red tests and a reviewer
reasonably concluding the codebase was broken. It was not.

A skip says *"not here"*, which is true. A failure says *"broken"*, which was not.

---

## 7. English, in code and comments

New code, comments and commit messages are written in English, including where
the surrounding code is Swedish. The existing Swedish strings are being
translated separately; do not add more.
