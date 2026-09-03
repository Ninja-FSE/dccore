# theme.py - one palette for everything DCCore says in a channel or a notice.
#
# DCCore already had a theme. It was just written eight times.
#
# The same five constants - a red border block, a cyan separator block, a white
# text plate, bold and reset - were redefined as literals INSIDE eight separate
# functions across announce.py and list.py, one per outbound message path:
#
#     send_transfer_complete      the channel "Sent:" notice
#     send_dcc_sending_notice     the private "sending you X"
#     announce_worker             the five-minute channel advertisement
#     send_search_result_header   private search results
#     send_dcc_queue_notice       the private queue position
#     send_debug                  the debug channel
#     send_pack_error_notice      the private error notice
#     list.execute_search         the search summary
#
# That list IS the whole outbound surface, which is right: one look everywhere
# is exactly what a file server wants. In a busy channel a dozen bots advertise
# at once and the palette is how a person tells them apart at a glance.
#
# The values had not diverged. The SET had: three of the eight defined only
# four of the five names. None of the three read the name it was missing, so
# nothing was broken - but changing DCCore's look meant editing eight places
# and hoping you had found them all, and the drift had already started.
#
# Roles, not colours
# ------------------
# A theme is chosen by name and read as roles, so a preset says what each part
# of a line is FOR rather than which of the sixteen mIRC colours it happens to
# be:
#
#     border      the outer block that frames a section
#     separator   the block between fields
#     textbox     the plate the text sits on
#     value       a live figure - a count, a speed, a nickname
#     alert       a figure that is meant to catch the eye
#     accent      timestamps and secondary text
#
# bold and reset are not roles. They are IRC control characters with fixed
# meanings, and a theme that redefined them would be redefining the protocol.
#
# What a theme does NOT cover
# --------------------------
# send_debug() colours its category tag by MEANING - purple for QUIT, cyan for
# JOIN, grey for INFO, red for SECURITY. Those are semantic, not decorative: an
# operator scanning the debug channel reads the colour before the word, and a
# theme that mapped them all onto one role would erase the distinction they
# exist for. They stay out of the palette, including under "plain" - the debug
# channel is the operator's own, not one where formatting is banned.
#
# config.C_BOLD and config.C_RESET are likewise untouched wherever they appear.
#
# On the line budget
# ------------------
# Colour codes cost bytes against IRC's 512, and announce.fit_irc_line() keeps
# a 420-byte budget for the pessimistic hostmask. A preset must therefore keep
# the SEGMENT COUNT constant - a decorative extra block would eat into the
# filename budget on every line that carries a filename. Presets here change
# which colours the segments are, never how many there are.

import defaults as config

# The mIRC colour pair for a solid block is "fg,bg" with fg == bg: the
# character cell is filled with the background and the (invisible) foreground
# never shows. That is why the classic separator is "10,10" and not "10".
CLASSIC = {
    "border":    "\x0304,05",   # dark red on maroon
    "separator": "\x0310,10",   # solid cyan
    "textbox":   "\x0301,00",   # black on white
    "value":     "\x0303",      # green
    "alert":     "\x0304",      # red
    "accent":    "\x0312",      # royal blue
}

# Presets are chosen to be distinguishable FROM EACH OTHER AND FROM THE COMMON
# OmenServe and iroffer looks. Two DCCore operators in the same channel who
# both took the default are already indistinguishable, which is the situation
# a theme exists to fix; shipping four presets that all look alike would just
# move the problem.
THEMES = {
    "classic": CLASSIC,

    # Deep blue plates, amber figures. The furthest from classic's red/cyan
    # while staying inside the sixteen colours every client agrees on.
    "midnight": {
        "border":    "\x0302,02",   # solid blue
        "separator": "\x0312,12",   # solid royal blue
        "textbox":   "\x0300,01",   # white on black
        "value":     "\x0311",      # light cyan
        "alert":     "\x0308",      # yellow
        "accent":    "\x0315",      # light grey
    },

    # Green on black, the terminal look. Reads as deliberate rather than as a
    # bot that never got configured.
    "forest": {
        "border":    "\x0303,03",   # solid green
        "separator": "\x0309,09",   # solid light green
        "textbox":   "\x0301,00",   # black on white
        "value":     "\x0303",      # green
        "alert":     "\x0307",      # orange
        "accent":    "\x0314",      # grey
    },

    # Purple and pink. Loud on purpose - the point of a theme is being picked
    # out of a dozen adverts, and an operator who wants that should be able to
    # have it without hand-editing codes.
    "orchid": {
        "border":    "\x0306,06",   # solid purple
        "separator": "\x0313,13",   # solid pink
        "textbox":   "\x0301,00",   # black on white
        "value":     "\x0313",      # pink
        "alert":     "\x0304",      # red
        "accent":    "\x0306",      # purple
    },

    # No codes at all. Deliberately NOT the default: colour is the norm in
    # these channels and it is the identity mechanism, so a plain bot is a bot
    # nobody picks out. It earns its place for a network or a channel that
    # strips or bans formatting.
    "plain": {
        "border":    "",
        "separator": "",
        "textbox":   "",
        "value":     "",
        "alert":     "",
        "accent":    "",
    },
}

DEFAULT_THEME = "classic"

# Fixed IRC control characters, not roles. See the note above.
BOLD = "\x02"
RESET = "\x0f"


def theme_name():
    """The configured theme, normalised, falling back to the default.

    Loud rather than silent, and the same posture list.list_format() takes: an
    unrecognised name is a typo an operator wants to hear about, and the
    alternative to falling back is a bot whose every message carries the empty
    string where a colour should be.
    """
    raw = getattr(config, "THEME", DEFAULT_THEME)
    chosen = str(raw or "").strip().lower()
    if chosen in THEMES:
        return chosen
    print(f"[THEME] THEME={raw!r} is not one of {sorted(THEMES)} "
          f"- using {DEFAULT_THEME!r}.")
    return DEFAULT_THEME


def palette():
    """The six roles for the configured theme, as a dict.

    config.CUSTOM_THEME_<ROLE> overrides one role on top of the chosen
    preset, so somebody who wants one colour changed does not have to restate
    the other five. #170's RFC flattened this from a single CUSTOM_THEME dict
    into six plain strings (see config.py's own comment on that change) - a
    role left at its default of None keeps the preset's own value, exactly
    as an absent key in the old dict did.
    """
    roles = dict(THEMES[theme_name()])
    for role in roles:
        override = getattr(config, f"CUSTOM_THEME_{role.upper()}", None)
        if isinstance(override, str) and override:
            roles[role] = override
    return roles


def blocks():
    """The eight message paths' palette, in the order they bind it.

    Returns (border, separator, textbox, reset, bold, value, alert, accent).

    A tuple rather than the dict because every call site unpacks it into the
    local names its templates already use - which is what makes this a change
    of where the values come from and not a change to a single line of
    outbound text.
    """
    roles = palette()
    return (roles["border"], roles["separator"], roles["textbox"],
            RESET, BOLD, roles["value"], roles["alert"], roles["accent"])
