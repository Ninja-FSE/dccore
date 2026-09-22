#!/usr/bin/env bash
#
# DCCore launcher for Linux.
#
# The important line is the cd below. Every data path in defaults.py is
# relative - ./data/bans.txt, ./lists - so they resolve against the
# working directory. Running this from a cron job, a systemd unit with
# no WorkingDirectory set, or a symlink on PATH would otherwise start
# the daemon with a working directory that is not the repository, and
# it would quietly create an empty data folder somewhere else and boot
# with no bans, no queue and no list.
#
# Usage:
#   ./scripts/linux/start-dccore.sh          first run: ask the setup questions;
#                                            then check the setup and start the daemon
#   ./scripts/linux/start-dccore.sh check    check the setup and stop
#
# THE LAUNCHER IS THE INSTALL (#547, Proposal 1). With no config yet it runs
# configure.py right here rather than telling the operator to copy a sample,
# and before every start it offers to install Flask if the dashboard is on
# and Flask is missing. Nothing an already-configured install does changes.
#
# Also the macOS launcher: scripts/macos/start-dccore.command is a one-line
# wrapper that runs this file, so it must stay portable sh - no bashisms, and
# no `readlink -f`, which macOS did not have before 12.3.

cd "$(cd "$(dirname "$0")" && pwd -P)/../.." || exit 1

# --- find an interpreter ------------------------------------------------
# The first candidate that actually RUNS, not the first that exists. macOS
# ships a python3 stub at /usr/bin/python3 that pops the Xcode installer and
# exits non-zero, and Windows (under Git Bash) ships one that opens the
# Microsoft Store; both pass `command -v`. Asking each to import sys is the
# only test that tells a Python from a shortcut to one.
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo
    echo "  Python was not found."
    echo
    case "$(uname -s 2>/dev/null)" in
        Darwin)
            echo "  Install Python 3.10 or newer - from python.org, or with Homebrew:"
            echo "      brew install python"
            echo "  (the python3 that comes with macOS is only an installer stub)"
            ;;
        *)
            echo "  Install Python 3.10 or newer - on most distributions your package"
            echo "  manager's \"python3\" package, e.g."
            echo "      sudo apt install python3        (Debian, Ubuntu)"
            echo "      sudo dnf install python3        (Fedora)"
            ;;
    esac
    echo "  then run this again."
    echo
    exit 1
fi

# --- check-only mode ------------------------------------------------------
if [ "$1" = "check" ]; then
    "$PY" scripts/linux/check-setup.py
    exit $?
fi

# --- the setup questions, in this terminal ------------------------------------
# Used on a first run when the browser page was declined or is out of reach,
# and again when the daemon could not serve it (below). Fails when setup did
# not finish, so that the bot is not started on a half-written config.
ask_the_questions_here() {
    if ! "$PY" configure.py; then
        echo
        echo "  Setup did not finish, so DCCore was not started. Run this file"
        echo "  again to pick it up where it stopped."
        echo
        return 1
    fi
    echo
}

# --- refuse to start without a local config --------------------------------
# settings.conf is fully first-class (see scripts/setup_check.py's own note) -
# the daemon starts fine from it alone, so this only refuses when NEITHER
# override exists.
if [ ! -f "admin_config.py" ] && [ ! -f "settings.conf" ]; then
    # An upgrading install has neither, but is NOT unconfigured: #170 renamed
    # local_config.py to admin_config.py, and that file is gitignored, so the
    # pull renamed defaults.py for them and could not touch theirs.
    # defaults.migrate_local_config_to_admin_config() does that rename at import
    # time - but this check runs before any Python has been imported, so
    # without this branch the operator is told to copy the sample, and doing so
    # is exactly the condition that makes the migration skip for good.
    if [ -f "local_config.py" ]; then
        echo
        echo "  Found local_config.py, which #170 renamed to admin_config.py."
        echo
        echo "  Nothing to copy - start the daemon once and it renames the file"
        echo "  for you, keeping every setting in it:"
        echo
        echo "      python3 oserve.py"
        echo
        echo "  Do NOT copy admin_config.py.sample over the top: that leaves your"
        echo "  real settings stranded in local_config.py."
        echo
        exit 1
    fi
    # --- first run: ask the questions here -------------------------------
    # No config at all means a first run, not a mistake. configure.py asks
    # everything in the right order and writes settings.conf and
    # admin_config.py; it used to be a separate terminal step this file then
    # told people to go and do. A tree without configure.py (a broken
    # extract) still gets an instruction, so nothing is worse than before.
    if [ ! -f "configure.py" ]; then
        echo
        echo "  No admin_config.py and no settings.conf found, and no configure.py"
        echo "  to create them with - this does not look like a complete DCCore"
        echo "  folder. Extract the download again, then run this file."
        echo
        exit 1
    fi
    echo
    echo "  Welcome to DCCore. This looks like the first run - a few questions"
    echo "  and it will be set up. You can change every answer later on the"
    echo "  dashboard's Settings page."
    echo
    # SET IT UP IN THE BROWSER (#547, Proposal 4). With Flask - installed
    # here on a yes if it is missing - the daemon is started straight away
    # and serves its own setup page on 127.0.0.1; it carries on into the
    # real bot once the form is saved. The setup check below is skipped on
    # that path, since it would refuse the blank tree the page exists to
    # fill in; the daemon runs its own checks after the form. A no, no pip,
    # or nobody at the keyboard means the questions are asked here, as
    # before.
    if "$PY" configure.py --setup-in-browser; then
        BROWSER_SETUP=1
    else
        ask_the_questions_here || exit 1
    fi
fi

# A loop only for the browser path's fallback below; every other start goes
# round exactly once.
while :; do

# --- refuse to start on a broken or dangerous config ------------------------
# check-setup.py fails on a missing music directory, and on a config still
# pointing at the production bot's nick or channels. That second one is worth
# blocking: it would put a near-identical second bot into live trading
# channels, which can get the other operator banned too.
if [ -z "$BROWSER_SETUP" ] && ! "$PY" scripts/linux/check-setup.py >/dev/null 2>&1; then
    echo
    echo "  Setup check failed - not starting. Details:"
    echo
    "$PY" scripts/linux/check-setup.py
    echo
    exit 1
fi

# --- the dashboard's one dependency, offered before it is missed -------------
# Silent when the dashboard is off or Flask is already there; otherwise the
# same offer configure.py makes during setup. Never stops the start.
[ -z "$BROWSER_SETUP" ] && "$PY" configure.py --flask

# --- go ---------------------------------------------------------------------
echo
echo "  Starting DCCore.  Press Ctrl-C to stop it."
echo "  Closing this terminal stops the bot too - leave it open."
echo
"$PY" oserve.py
RC=$?

# THE PAGE COULD NOT BE SERVED (#617). oserve.py exits 3 when the setup page
# was this first run's path and could not finish - the port was taken
# (another DCCore in a minimised window, another program), or Ctrl-C. Flask
# being there sent every run down the same road, and the questions above were
# unreachable from here: ask them now, then check the setup and start, as the
# terminal path does. Once they are answered no later run comes this way.
if [ -n "$BROWSER_SETUP" ] && [ "$RC" -eq 3 ]; then
    BROWSER_SETUP=
    echo
    echo "  The setup page could not be opened, so the questions follow here."
    echo
    ask_the_questions_here || exit 1
    continue
fi
break

done

echo
if [ "$RC" -eq 0 ]; then
    echo "  DCCore exited normally."
else
    echo "  DCCore exited with code $RC."
fi
echo
exit $RC
