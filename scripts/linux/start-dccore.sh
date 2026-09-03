#!/usr/bin/env bash
#
# DCCore launcher for Linux.
#
# The important line is the cd below. Every data path in config.py is
# relative - ./data/bans.txt, ./lists - so they resolve against the
# working directory. Running this from a cron job, a systemd unit with
# no WorkingDirectory set, or a symlink on PATH would otherwise start
# the daemon with a working directory that is not the repository, and
# it would quietly create an empty data folder somewhere else and boot
# with no bans, no queue and no list.
#
# Usage:
#   ./scripts/linux/start-dccore.sh          check the setup, then start the daemon
#   ./scripts/linux/start-dccore.sh check    check the setup and stop

cd "$(dirname "$(readlink -f "$0")")/../.." || exit 1

# --- find an interpreter ------------------------------------------------
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
fi

if [ -z "$PY" ]; then
    echo
    echo "  Python was not found."
    echo
    echo "  Install Python 3.10 or newer (most distributions: your package"
    echo "  manager's \"python3\" package), then run this again."
    echo
    exit 1
fi

# --- check-only mode ------------------------------------------------------
if [ "$1" = "check" ]; then
    "$PY" scripts/linux/check-setup.py
    exit $?
fi

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
    echo
    echo "  No admin_config.py and no settings.conf found."
    echo
    echo "  Copy admin_config.py.sample to admin_config.py, or"
    echo "  settings.conf.sample to settings.conf, and fill one in."
    echo "  Without either the daemon uses the defaults in defaults.py, which"
    echo "  point at somebody else's live bot and channels."
    echo
    exit 1
fi

# --- refuse to start on a broken or dangerous config ------------------------
# check-setup.py fails on a missing music directory, and on a config still
# pointing at the production bot's nick or channels. That second one is worth
# blocking: it would put a near-identical second bot into live trading
# channels, which can get the other operator banned too.
if ! "$PY" scripts/linux/check-setup.py >/dev/null 2>&1; then
    echo
    echo "  Setup check failed - not starting. Details:"
    echo
    "$PY" scripts/linux/check-setup.py
    echo
    exit 1
fi

# --- go ---------------------------------------------------------------------
echo
echo "  Starting DCCore.  Press Ctrl-C to stop it."
echo
"$PY" oserve.py
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "  DCCore exited normally."
else
    echo "  DCCore exited with code $RC."
fi
echo
exit $RC
