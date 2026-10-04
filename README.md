# DCCore

**v1.15.0** · Python 3.10+ · Linux, Windows and macOS

An IRC DCC file-sharing daemon — a modern reimplementation of OmenServe, the mIRC script that has run these channels for twenty years.

It sits in a channel, advertises a catalogue, and serves files over DCC to whoever asks. It can also fetch files *from* other bots, which OmenServe never could. No third-party packages: everything it needs to talk to IRC, move files, pack albums and run its admin console is in the standard library.

**Getting started:** install Python 3.10+, extract, and run the launcher for your system - `scripts\windows\start-dccore.bat` (double-click), `scripts/linux/start-dccore.sh`, or `scripts/macos/start-dccore.command`. The first run asks the setup questions itself. Details in [docs/INSTALL.md](docs/INSTALL.md) and [docs/WINDOWS.md](docs/WINDOWS.md).

## What it does

- **Serves files over DCC** — per-user and global queues, configurable slots, `!rar` album packing on demand, and DCC RESUME so an interrupted download continues instead of starting over
- **Keeps a searchable master list** in `.txt`, `.zip` and `.rar`, rebuilt atomically so a failed scan never overwrites a good one - and keeps answering from the current list while it rebuilds
- **Fetches from other bots** — request a file or a whole list, or broadcast one `@find` and collect every reply. A queued request waits its turn, a bot that is offline is asked again when it returns, and the lists you hold can keep themselves up to date. In this bot's own search, words in quotes must appear together: `@find "metal church"`
- **Remembers people** — a user who disconnects keeps their queue for five minutes and resumes on rejoin
- **Defends itself** — rolling flood windows, escalating mutes, hostmask bans, and every other bot treated as untrusted
- **Is operable** — an authenticated DCC CHAT console, an optional web dashboard, live `!rehash`, and statistics that survive a restart
- **Runs where you run it** — long paths, non-ASCII filenames, and Linux, Windows and macOS all in CI on every commit

**Every feature, in one place:** the *Implemented* section of [docs/FUTURE.md](docs/FUTURE.md), which also lists what is *not* built yet.

## Quick start

```bash
python3 configure.py                    # a few questions
./scripts/linux/start-dccore.sh check   # verify, without touching the network
./scripts/linux/start-dccore.sh         # go
python3 update_list.py                  # build the first list
```

With track lengths in the list (`LIST_SHOW_AUDIO_INFO`), a hand-run `update_list.py` publishes and then says how to
read the lengths: `update_list.py --read-audio-info`, or the running bot's **Read audio info**.

On Windows, `py` rather than `python3` — a python.org install gives you `py` and `python`, not `python3`, and Windows ships an alias that makes `python3` open the Microsoft Store instead:

```bat
py configure.py
scripts\windows\start-dccore.bat check
scripts\windows\start-dccore.bat
py update_list.py
```

Full guide, including configuring it by hand and upgrading from an older install: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Documentation

| | |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | requirements, setup, configuration, upgrading |
| [FUTURE.md](docs/FUTURE.md) | what is implemented, what is planned |
| [ADMIN-CONSOLE.md](docs/ADMIN-CONSOLE.md) | the authenticated DCC CHAT console, and the mIRC window script that sits on it |
| [WINDOWS.md](docs/WINDOWS.md) | the Windows guide |
| [CONVENTIONS.md](docs/CONVENTIONS.md) | how this codebase is written, if you want to contribute |
| [UPDATES.md](docs/UPDATES.md) | the changelog |
| [SECURITY.md](docs/SECURITY.md) | how to report a vulnerability |

## How it is put together

`oserve.py` wires everything and owns the threads; it, `configure.py` and `update_list.py` are at the top, the modules below are in `src/`, and your own `settings.conf` and `admin_config.py` are in `conf/`. `irc.py` is the network loop and command parser; `dcc.py` sends files and `dcc_fetch.py` receives them. `list.py` and `update_list.py` build and search the catalogue, `announce.py` owns everything the channel sees, and `commands.py` handles what users type. `db.py` persists state, `security.py` decides who is allowed, and `platform_compat.py` holds the handful of genuine Linux/Windows differences so nothing else has to care. `defaults.py` declares every setting; `settings_file.py` lets an operator override them without editing Python.

The optional dashboard is `webserver.py` and `web/`, and disables itself cleanly if Flask is absent.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Or, in four processes at once and in a third of the time, the way CI runs it: `python3 scripts/run_tests_in_parallel.py`.

Thousands of them - the count is kept in [docs/FUTURE.md](docs/FUTURE.md) - stdlib-only, on Linux, Windows and macOS, Python 3.10, 3.12 and 3.14 in CI.

To see what a running bot costs under load, `scripts/stress_test.py` points 40 simulated clients at it - a flood of searches, a storm of adverts, join/part churn, a mass quit, the dashboard - and prints the bot's CPU, memory and threads for each. Linux only, and only ever against a test bot on a private test server: the flood gets its nicks muted and banned. Its docstring says how to run it.

## Responsible use

DCCore is a tool for sharing files **you have the right to share**: your own recordings and creations, public-domain and Creative Commons works, and anything else whose licence allows it to be passed on. It comes with no content, points to none, and is not made for distributing material you have no right to distribute.

As the operator, you decide what goes into your shared folders, and you are responsible for it: the bot offers everything in them to anyone who asks. Before you share a folder, make sure you are allowed to - copyright and related laws differ from country to country, and the ones where you live are the ones that apply to you.

The DCCore contributors do not host, control, monitor or endorse anything any bot shares, and accept no liability for how the software is used. See also the "no warranty" terms of the licence below.

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
