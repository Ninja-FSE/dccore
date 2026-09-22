#!/bin/sh
#
# The twin of install-autostart.command: unloads the DCCore launchd agent
# and deletes its plist, so the bot no longer starts at login. Stops it if
# launchd was running it.

PLIST="$HOME/Library/LaunchAgents/com.dccore.bot.plist"

if [ ! -f "$PLIST" ]; then
    echo
    echo "  There is no $PLIST - nothing to remove."
    echo
    exit 0
fi

if command -v launchctl >/dev/null 2>&1; then
    launchctl unload -w "$PLIST"
fi
rm -f "$PLIST"

echo
echo "  Done: DCCore no longer starts at login. ~/Library/Logs/dccore.log is"
echo "  left where it is."
echo
