#!/bin/sh
#
# DCCore launcher for macOS.
#
# A .command file is what Finder runs in Terminal on a double-click, which
# is the whole reason this exists: the Linux launcher already does
# everything right on macOS (it is portable sh and probes for the Xcode
# python3 stub), but a first-timer will not open Terminal to run it.
#
# Gatekeeper will refuse a .command downloaded from the internet the first
# time. On macOS 14 and earlier: right-click it, choose Open, confirm once.
# On macOS 15 (Sequoia) and later that override is gone: after the refusal,
# open System Settings > Privacy & Security, scroll to the message about
# this file and click "Open Anyway", then double-click it again. Either way
# it is once; after that a double-click works. From Terminal, this does the
# same without the dialogs:
#
#   xattr -d com.apple.quarantine scripts/macos/*.command
#
# Usage:
#   double-click                 first run: ask the setup questions;
#                                then check the setup and start the daemon
#   ./start-dccore.command check check the setup and stop

cd "$(cd "$(dirname "$0")" && pwd -P)" || exit 1
exec /bin/sh ../linux/start-dccore.sh "$@"
