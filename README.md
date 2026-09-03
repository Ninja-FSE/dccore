# DCCore

**v1.10.0** · Python 3.10+ · Linux and Windows

An IRC DCC file-sharing daemon — a modern reimplementation of OmenServe, the mIRC script that has run these channels for twenty years.

It sits in a channel, advertises a catalogue, and serves files over DCC to whoever asks. It can also fetch files *from* other bots, which OmenServe never could. No third-party packages: everything it needs to talk to IRC, move files, pack albums and run its admin console is in the standard library.

## What it does

- **Serves files over DCC** — per-user and global queues, configurable slots, `!rar` album packing on demand
- **Keeps a searchable master list** in `.txt`, `.zip` and `.rar`, rebuilt atomically so a failed scan never overwrites a good one
- **Fetches from other bots** — request a file or a whole list, or broadcast one `@find` and collect every reply
- **Remembers people** — a user who disconnects keeps their queue for five minutes and resumes on rejoin
- **Defends itself** — rolling flood windows, escalating mutes, hostmask bans, and every other bot treated as untrusted
- **Is operable** — an authenticated DCC CHAT console, an optional web dashboard, live `!rehash`, and statistics that survive a restart
- **Runs where you run it** — long paths, non-ASCII filenames, and both platforms in CI on every commit

See [docs/FUTURE.md](docs/FUTURE.md) for the full picture, including what is *not* built yet.

## Quick start

```bash
python3 configure.py                        # a few questions
./scripts/linux/start-dccore.sh check   # verify, without touching the network
./scripts/linux/start-dccore.sh         # go
python3 update_list.py                  # build the first list
```

Windows is the same with `scripts\windows\start-dccore.bat`.

Full guide, including configuring it by hand and upgrading from an older install: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Documentation

| | |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | requirements, setup, configuration, upgrading |
| [FUTURE.md](docs/FUTURE.md) | what is implemented, what is planned |
| [ADMIN-CONSOLE.md](docs/ADMIN-CONSOLE.md) | the authenticated DCC CHAT console |
| [WINDOWS.md](docs/WINDOWS.md) | the Windows guide |
| [CONVENTIONS.md](docs/CONVENTIONS.md) | how this codebase is written, if you want to contribute |
| [UPDATES.md](docs/UPDATES.md) | changelog |
| [SECURITY.md](docs/SECURITY.md) | how to report a vulnerability |

## How it is put together

`oserve.py` wires everything and owns the threads. `irc.py` is the network loop and command parser; `dcc.py` sends files and `dcc_fetch.py` receives them. `list.py` and `update_list.py` build and search the catalogue, `announce.py` owns everything the channel sees, and `commands.py` handles what users type. `db.py` persists state, `security.py` decides who is allowed, and `platform_compat.py` holds the handful of genuine Linux/Windows differences so nothing else has to care. `defaults.py` declares every setting; `settings_file.py` lets an operator override them without editing Python.

The optional dashboard is `webserver.py` and `web/`, and disables itself cleanly if Flask is absent.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

2071 of them, stdlib-only, on Linux and Windows and Python 3.10 and 3.12 in CI. `scripts/preflight.py` runs the same suite twice, once with host tooling hidden, to catch anything that only passes because of what happens to be installed.

## License

DCCore - an IRC DCC file-sharing daemon
Copyright (C) 2026 The DCCore contributors

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the [GNU General Public License](LICENSE) for more
details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

In short: **distributed** forks and modified versions must stay open source
under the same terms. Running a modified copy privately, which is what most
operators do, carries no obligation at all - GPLv3's requirements attach to
distribution, not to use.
