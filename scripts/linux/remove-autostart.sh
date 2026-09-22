#!/usr/bin/env bash
#
# The twin of install-autostart.sh: stops the DCCore user unit, disables
# it and deletes the unit file, so the bot no longer starts with the
# system. Portable sh apart from the shebang.

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/dccore.service"

if [ ! -f "$UNIT" ]; then
    echo
    echo "  There is no $UNIT - nothing to remove."
    echo
    exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now dccore.service
fi
rm -f "$UNIT"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
fi

echo
echo "  Done: DCCore no longer starts with the system, and was stopped if it"
echo "  was running under systemd. (loginctl enable-linger, if you set it,"
echo "  is left as it was - it is not DCCore's to undo.)"
echo
