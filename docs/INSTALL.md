# Installing DCCore

From a fresh clone to a bot serving files. Ten minutes, most of it waiting for the first list to build.

## Requirements

**Python 3.10 or newer.** Nothing else is required.

The daemon and its test suite are stdlib-only, which is why they run on a bare machine with no `pip install` step:

```bash
pip install -r requirements.txt      # succeeds, installs nothing
```

Two optional extras:

| | |
|---|---|
| **Web dashboard** | needs Flask — `pip install -r requirements-web.txt`. The bot starts fine without it and says so. |
| **`!rar` album packing** | needs a `rar` binary on `PATH`. Without it, `!rar` refuses with a notice; ordinary transfers are unaffected. |

Developed on Linux (Debian/Ubuntu, including Proxmox LXC) and runs on Windows — the platform differences live in `platform_compat.py`, and CI covers both. Windows operators should also read [WINDOWS.md](WINDOWS.md).

## Guided setup

```bash
python3 configure.py
```

Six questions — nickname, IRC server, channel(s), admin nick, admin console password, and the music directory — written to `settings.conf` and `admin_config.py` for you.

The music directory is optional here. It is usually easier to browse and confirm it from the dashboard's Settings page once the bot is running than to type a path blind. Everything else stays changeable afterwards.

Safe to run again later: every prompt shows what is already configured as its default.

## Configuring it by hand

`configure.py` is a convenience, not a requirement. There are two mechanisms and you can use either or both.

**`settings.conf`** — plain text, no Python syntax. Copy `settings.conf.sample` and edit. This is what the dashboard's Settings page and the admin console both write to.

**`admin_config.py`** — Python. Copy `admin_config.py.sample` and edit. Better for values you would rather keep out of a file other tools rewrite, such as `ADMIN_HOSTMASKS` and `ADMIN_PASSWORD_HASH`.

Both are gitignored. `defaults.py` applies `admin_config.py` first and `settings.conf` second, so a value set in both takes the `settings.conf` one.

### What must be set

The daemon refuses to start until `NICKNAME`, `CHANNEL` and `ADMIN_NICK` have values, no matter how it is launched. Leaving them at their shipped blanks would mean joining somebody else's channels under a name that is not yours.

Three things are deliberately *not* required:

- **`SERVER`** and **`DEBUG_CHANNEL`** — their shipped values are already right for almost every install, so requiring them would only make you retype something correct. `DEBUG_CHANNEL` ships blank, and blank means no debug channel is joined.
- **`FILE_DIRECTORY`** — requiring it would block the daemon from reaching the dashboard, which is the easiest place to set it. A blank one warns, at the pre-flight check and again at boot, and the bot serves nothing until it has one.

## Check before you start

Verify the setup without connecting to IRC. This catches the mistakes that actually cause trouble — most often a music directory that is set but does not exist:

**Linux**
```bash
./scripts/linux/start-dccore.sh check
```

**Windows**
```bat
scripts\windows\start-dccore.bat check
```

## Start it

Once the check reports *Ready to start*, drop the argument:

```bash
./scripts/linux/start-dccore.sh
```

```bat
scripts\windows\start-dccore.bat
```

Both re-run the check first and anchor their own working directory, so they work from a cron job, a systemd unit, a shortcut or a double-click.

To start the daemon directly instead:

```bash
python3 oserve.py
```

## Build the first list

The bot has nothing to serve until its library has been scanned:

```bash
python3 update_list.py
```

or `!update` from IRC, or the dashboard's **Update list** button. On a large library this takes a while; the advert will report the real file count once it finishes.

## Upgrading

Pull and restart. One thing is worth knowing if you are coming from a version before the `config.py` → `defaults.py` rename:

Your own overrides file was called `local_config.py` and is gitignored, so `git pull` cannot rename it for you. **Do not copy `admin_config.py.sample` over the top** — that strands your real settings. Just start the daemon once and it renames the file itself, keeping every setting in it. The launchers and the pre-flight check both say so if they meet that state.

## Next

- [ADMIN-CONSOLE.md](ADMIN-CONSOLE.md) — setting up the authenticated DCC CHAT console
- [WINDOWS.md](WINDOWS.md) — the full Windows guide
- [FUTURE.md](FUTURE.md) — what is built and what is not
- [CONVENTIONS.md](CONVENTIONS.md) — if you plan to contribute
