#!/bin/sh
#
# Start DCCore when you log in, on macOS (#547, Proposal 6).
#
# Writes a launchd agent, ~/Library/LaunchAgents/com.dccore.bot.plist, that
# runs start-dccore.sh - the launcher, not oserve.py directly, because the
# launcher is what puts the working directory right - and registers it for
# every login. Its output goes to ~/Library/Logs/dccore.log.
# remove-autostart.command takes it out again. No administrator needed.
#
# The agent is written and enabled, not loaded: `launchctl load` starts it at
# once (RunAtLoad, which KeepAlive/SuccessfulExit implies anyway), the docs say
# to run the bot by hand first, and nothing refuses a second instance, so the
# operator who ran this with that bot still up got a twin - a second daemon on
# the alternate nick, in the same channels, writing the same data/ files
# (#619). Like the Windows installer, this one registers the start and leaves
# starting it now to the operator, once the hand-run bot is stopped.
#
# Double-click it in Finder, or run it from Terminal. The first time
# Gatekeeper refuses it because it came from the internet: right-click >
# Open on macOS 14 and earlier, System Settings > Privacy & Security >
# "Open Anyway" on macOS 15 and later - see the note at the top of
# start-dccore.command. Plain sh: macOS's default shell for .command files
# is zsh, but /bin/sh is always there.

cd "$(cd "$(dirname "$0")" && pwd -P)/../.." || exit 1
ROOT="$(pwd -P)"

if [ ! -f "admin_config.py" ] && [ ! -f "settings.conf" ]; then
    echo
    echo "  DCCore is not set up yet. Run start-dccore.command once first - it"
    echo "  asks the setup questions - then this file."
    echo
    exit 1
fi

if ! command -v launchctl >/dev/null 2>&1; then
    echo
    echo "  launchctl was not found - this does not look like macOS."
    echo
    exit 1
fi

AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/com.dccore.bot.plist"
LOG="$HOME/Library/Logs/dccore.log"
mkdir -p "$AGENTS" "$HOME/Library/Logs" || exit 1

# The path goes into XML; the three characters XML cares about are escaped.
ROOT_XML=$(printf '%s' "$ROOT" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
LOG_XML=$(printf '%s' "$LOG" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')

# A launchd agent does not get the PATH of a shell: it gets
# /usr/bin:/bin:/usr/sbin:/sbin. The Python this was installed and tested with
# (python.org's in /usr/local/bin or /Library/Frameworks, Homebrew's in
# /opt/homebrew/bin) is on the PATH of the Terminal this is run from and on no
# other, so the launcher found only Apple's stub, said "Python was not found",
# and launchd restarted it every ten seconds for ever (#588). The agent is given
# this shell's PATH, then the places those installers use, then launchd's own.
AGENT_PATH="$PATH:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PATH_XML=$(printf '%s' "$AGENT_PATH" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dccore.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>$ROOT_XML/scripts/linux/start-dccore.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT_XML</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$PATH_XML</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_XML</string>
    <key>StandardErrorPath</key>
    <string>$LOG_XML</string>
</dict>
</plist>
EOF

# Unload first so a re-run after moving the folder picks up the new path at
# the next login. `unload -w` also marks the label disabled, as an earlier
# remove-autostart.command did, and launchd would then skip the plist at
# login: `enable` clears that without loading (starting) the agent now.
launchctl unload -w "$PLIST" >/dev/null 2>&1
if ! launchctl enable "gui/$(id -u)/com.dccore.bot"; then
    echo
    echo "  launchctl could not enable the agent. It is written; if it does not"
    echo "  start at your next login, run the load command below once."
fi

echo
echo "  Done: DCCore starts the next time you log in. It was not started now,"
echo "  so a bot you are running by hand is not doubled. To have launchd run"
echo "  it already: stop the hand-run bot (Ctrl-C in its terminal), then"
echo "      launchctl load -w \"$PLIST\"      start it now"
echo "      tail -f \"$LOG\"                       its output"
echo "      launchctl unload -w \"$PLIST\"    stop it until next login"
echo "  remove-autostart.command undoes this."
echo
