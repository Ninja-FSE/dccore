#!/usr/bin/env python3
"""Write the settings window's dialog into dccore.mrc (#1264).

dccore.mrc's settings window has a control for every setting the dashboard's
Settings page offers - about a hundred and fifty of them, on twenty-odd pages.
Kept by hand, that table would drift the first time a setting was added to the
bot, renamed, or given a unit, and nobody would notice until an operator went
looking for it. So it is generated, the way conf/settings.conf.sample is:

    python scripts/mirc/build_settings_window.py          rewrite the block
    python scripts/mirc/build_settings_window.py --check  exit 1 if it is stale

WHAT GOES IN. One marked block of dccore.mrc, between the BEGIN and END lines
below: the `dialog dccore.set` table - every control with its id and its
position, computed here - and `alias dccore.sw.data`, which fills the hash
table dccore.swm with what the hand-written code needs to drive it: which
control holds which setting and of what kind, which controls make up each
page, the choices, the units and the help texts.

WHERE IT COMES FROM. The grouping is scripts/mirc/settings_window_layout.py
(the mockup's tabs, pages and sections). Everything else is the bot's own:
the labels (webserver.SETTINGS_LABELS), units (SETTINGS_UNITS), choices and
their labels (settings_file.CHOICES, webserver.CHOICE_LABELS), the declared
types (settings_file.declared_types), the help text the Settings page shows
on hover (settings_help, #528) and the dashboard's own words for the colours
and channel modes (web/lang/en.json). Nothing reads a VALUE: the block is the
same on every machine, whatever its settings.conf says.

WHAT STAYS HAND-WRITTEN. Everything the window does - opening, loading,
dirty tracking, Apply / OK / Cancel, the structured pages, the preview - is
ordinary mSL outside the block, in dccore.mrc's "Settings window" section.

mIRC CANNOT RUN HERE, so the layout is measured the way
tests/test_the_options_dialog_labels_fit_their_controls.py measures the
options dialog: Tahoma 8pt at 96 DPI, one dialog unit = 1.5 px, a check box
spending 17 px on its square. A label too long for its control wraps onto a
second line when it is a text, and is an error when it is a check (a check
box does not wrap).
"""

import argparse
import ast
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_DIR = os.path.join(REPO_ROOT, "src")
for path in (SRC_DIR, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

SCRIPT = os.path.join(HERE, "dccore.mrc")
LANG_FILE = os.path.join(REPO_ROOT, "web", "lang", "en.json")
DEFAULTS_FILE = os.path.join(SRC_DIR, "defaults.py")

BEGIN = "; ==== BEGIN GENERATED settings window (scripts/mirc/build_settings_window.py) ===="
END = "; ==== END GENERATED settings window ===="

# ---------------------------------------------------------------------------
# Measuring (the same table as the options dialog's label test)
# ---------------------------------------------------------------------------

_BY_WIDTH = {
    2: "'il",
    3: " j",
    4: "!\"(),-./:;I[\\]frt|",
    5: "?JLcksz{}",
    6: "$*0123456789BEFKPSTVXYZ_`abdeghnopquvxy",
    7: "&ACDGHNRU",
    8: "#+<=>MOQ^mw~",
    10: "@W",
    11: "%",
}
CHAR_PX = {c: w for w, chars in _BY_WIDTH.items() for c in chars}
PX_PER_DBU = 1.5
CHECK_BOX_PX = 17
BUTTON_PAD_PX = 12      # a push button's frame and the space either side of its text
LINE_DBU = 8            # one line of dialog text


def text_px(text):
    return sum(CHAR_PX[c] for c in text)


# The real dialog font renders wider than the table (#782: a label the table
# gave 10% to spare ran into its neighbour), so a label is wrapped, and a
# check box held to fit, as if its control were a fifth narrower. Every
# label on one line then has that fifth to spare, which
# tests/test_the_options_dialog_labels_have_room_to_spare.py holds it to.
FONT_SLACK = 0.8


def wrapped_lines(text, width_dbu):
    """How many lines `text` takes in a static text `width_dbu` wide: Windows
    breaks at spaces, so this is a greedy word wrap with the same widths."""
    width = width_dbu * PX_PER_DBU * FONT_SLACK
    lines, current = 1, 0
    space = CHAR_PX[" "]
    for word in text.split(" "):
        need = text_px(word)
        if current == 0:
            current = need
        elif current + space + need <= width:
            current += space + need
        else:
            lines += 1
            current = need
    return lines


# ---------------------------------------------------------------------------
# Geometry, in dialog units (option dbu)
# ---------------------------------------------------------------------------

DIALOG_W, DIALOG_H = 488, 364
# The tab control frames the whole body - the page list, the page and the
# help line - as a tabbed dialog does: its page border runs under every
# tab, open under the chosen one, and round the body. A strip of tabs
# only had Windows draw that border cut off under the first tab (#1288).
TABS = (4, 2, 480, 338)
TAB_HEADER_H = 14                           # the tabs themselves; the body starts under them
# The pages of the chosen tab: a column of push-style radio buttons, one per
# slot, as many slots as the tab with the most pages has. Not a listbox: a
# listbox's row height cannot be set from a script, and at a display scale
# above 100% its highlight was shorter than the text, which looked cut.
# The column sits inside the frame, clear of its edge, and ends 12 units
# before the page starts: the title beside its edge looked squeezed.
PAGE_SLOT_X, PAGE_SLOT_Y, PAGE_SLOT_W, PAGE_SLOT_H, PAGE_SLOT_GAP = 10, 24, 88, 15, 2
CONTENT_X, CONTENT_Y, CONTENT_W, CONTENT_H = 110, 38, 370, 274
HELP = (110, 314, 370, 24)                  # three lines of the hovered setting's help
CONNECTED = (10, 342, 310, 8)
STATUS = (10, 351, 310, 10)
BUTTONS = (("Reload", 1017, 331), ("Apply", 1014, 370), ("OK", 1015, 409), ("Cancel", 1016, 448))
BUTTON_Y, BUTTON_W, BUTTON_H = 346, 36, 13
# With DCX (#1288) the mockup's navigation: the tab strip stays, and a list
# of the chosen tab's pages takes the left column where the page buttons
# are, clear of the page and of the connection line below; the page's name
# goes, in bold, into the band between the tabs and the page, a little
# below the list's top edge. The script turns both into pixels with the
# scale $dialog().cw / DIALOG_W gives.
DCX_PAGES = (10, 24, 88, 286)
DCX_PAGES_ID = 900
DCX_TITLE = (110, 26, 370, 10)
DCX_TITLE_ID = 901
# With DCX every line of the window is the script's own, in one colour
# (#1288): Windows draws the tab strip and the frames in light greys of its
# own, the first tab with no left edge at all, and none of it can be
# recoloured. So in DCX mode the native tabs and frames are hidden, and in
# their place: the tab strip - an outline panel, a panel on the face and a
# text for each tab, TAB_GAP apart from ROUND in from the tab control's
# left edge (clear of the body frame's rounded corner), the chosen one from
# the strip's top down onto the body frame's top line, the others TAB_LIFT
# lower and ending above it; the body frame, a ring a pixel wide round the
# tab control's area under the tabs; and every section's frame, a ring
# LINE_DROP under the frame's top (through the middle of its title) and the
# title over its top line, from TITLE_X, TITLE_H tall, TITLE_PAD of face
# either side of its text. A ring is one panel shaped by its window region,
# so nothing else of it is drawn or clicked and the controls inside stay
# Windows' own. Every corner is round, ROUND_PX across: the pixels of the
# midpoint circle, as GDI draws one with a pen, and the regions are polygons
# on pixel edges, which are exact - a ring cut from GDI's rounded rectangle
# came out as dots, and the frame of one as an uneven stair. The colours
# are the system's, so a theme is followed: the lines COLOR_3DSHADOW, the
# faces COLOR_3DFACE and 3DLIGHT. DCX ids: the frames from FRAME_DCX_FIRST,
# FRAME_DCX_STRIDE each (the ring, the title), a page's frames consecutive
# so one id range shows or hides them; the tabs from TAB_DCX_FIRST, three
# each (the outline, the face, the text); the body frame BODY_DCX_FIRST.
# All below the page list's.
FRAME_DCX_FIRST, FRAME_DCX_STRIDE = 100, 2
TAB_DCX_FIRST, TAB_DCX_STRIDE = 800, 3
BODY_DCX_FIRST = 830
ROUND = 4
ROUND_PX = 6
TAB_PAD = 5
TAB_GAP = 1
TAB_LIFT = 2
LINE_DROP = 4
TITLE_X = 6
TITLE_H = 9
TITLE_PAD = 2
# With DCX the window opens on a start page: a pill for each side, Bot (the
# groups but the last, with the tab strip) and mIRC (the last group, its
# pages the list and no strip), each with a line on what is there. A side
# has the way back at the strip's left end - a pill with an arrow and the
# side's name beside it - and the bot's tabs start after that. A pill is an
# outline, a face and its text, shaped like the frames, PILL_STRIDE DCX ids.
BACK_DCX_FIRST, CRUMB_DCX = 840, 843
HOME_DCX_FIRST, PILL_STRIDE = 844, 4
BACK = (TABS[0] + ROUND, TABS[1] + 1, 14, 12)
CRUMB = (BACK[0] + BACK[2] + 4, TABS[1] + 3, 22, 9)
TAB_START = CRUMB[0] + CRUMB[2] + 2
HOME_PILL_W, HOME_PILL_H, HOME_PILL_GAP = 140, 24, 12
HOME_PILL_Y = TABS[1] + TAB_HEADER_H + 12
HOME = (("Bot", "The bot's settings, sent with Apply"), ("mIRC", "This mIRC's own: window, link, chat"))
# The start page's figures, under the pills: frames like a page's, in two
# columns, with rows of a key and a value ("kv") or lines of fixed-width
# text ("full"), filled by the script from what the bot sends for the side
# panel. (column, title, kind, the keys or the number of lines); the
# Sending frame's title grows to "Sending n/m". DCX ids from
# STATS_DCX_FIRST: a ring and a title a frame, then two texts a row.
STATS_DCX_FIRST = 300
STATS_X, STATS_W, STATS_GAP = TABS[0] + 8, 228, 8
STATS_Y = HOME_PILL_Y + HOME_PILL_H + 10
STATS_ROW, STATS_DOWN, STATS_KEY_W = 9, 6, 52
STATS = (
    (0, "Bot", "kv", ("Nick", "Network", "Up since", "Connection")),
    (1, "Sending", "full", 6),
    (0, "Today", "kv", ("Sent", "Queue", "Speed record", "Failed", "Searches")),
    (1, "Library", "kv", ("Lists", "Files", "Size", "Last rebuild", "Search index", "Rebuild", "Packing")),
)


def dbu_for(px):
    """Dialog units enough for `px` pixels of the dialog font at 96 DPI."""
    return int(-(-px // PX_PER_DBU))


def arc_pixels(radius):
    """The pixels of a quarter circle of `radius`: the midpoint algorithm's,
    as GDI draws one with a pen. A frame's top-left corner, from (0, radius)
    on its left edge to (radius, 0) on its top edge."""
    pixels = set()
    x, y, d = radius, 0, 1 - radius
    while x >= y:
        pixels.add((radius - x, radius - y))
        pixels.add((radius - y, radius - x))
        y += 1
        if d < 0:
            d += 2 * y + 1
        else:
            x -= 1
            d += 2 * (y - x) + 1
    return pixels


def corner_traces(radius):
    """A frame's top-left corner as two staircases on pixel edges, from the
    left edge up to the top edge: round the outside of the ring, and round
    the inside. A polygon region on pixel edges holds exactly the pixels in
    it, so the outside less the inside is exactly the arc and the lines."""
    arc = arc_pixels(radius)
    outer = [min(x for x, y in arc if y == row) for row in range(radius + 1)]
    inner = [None] + [max(x for x, y in arc if y == row) + 1 for row in range(1, radius + 1)]

    def trace(edge, last_row):
        points = [(edge[radius], radius + 1)]
        for row in range(radius, last_row, -1):
            if edge[row - 1] != edge[row]:
                points += [(edge[row], row), (edge[row - 1], row)]
        points.append((edge[last_row], last_row))
        return points

    return trace(outer, 0), trace(inner, 1)

# A page is its sections, each in a titled frame (a section with no title is
# bare), down one column - or two side by side when one is too tall. Inside a
# frame the mockup's form, ROOMY: every label on a line of its own above a
# control the width of the frame. A page too tall for that even in two
# columns (File locations, with its twenty-odd paths) falls back to COMPACT,
# the first version's form: label | control on one line.
STYLES = (("roomy", 1), ("roomy", 2), ("compact", 1), ("compact", 2))
COLUMN_GAP = 8
BOX_PAD, BOX_TOP, BOX_BOTTOM = 6, 10, 4     # a frame: its sides, its title line, under its last item
SECTION_GAP = 4
CHOICE_W, COLOUR_W, COLOUR_GAP = 120, 80, 4 # roomy: a menu, and the two menus of a colour
ROOMY_GAP, ROOMY_AFTER = 1, 3               # roomy: label to control, control to the next item
CONTROL_W = {1: 132, 2: 84}                 # compact: the control, by columns
LABEL_GAP = 4
BROWSE_W = 14

EDIT_H, COMBO_H, CHECK_H = 11, 11, 10
ROW_PITCH, COMBO_PITCH, CHECK_PITCH = 12, 13, 11

# Control ids. Every dialog's ids are its own, but these stay clear of the
# options dialog's (1-703) and the download queues' (1-6) all the same, so a
# grep for an id finds one dialog.
TAB_IDS = range(1001, 1008)
FIRST_SLOT_ID = 1020                        # the page buttons: 1020, 1021, ...
HELP_ID, CONNECTED_ID, STATUS_ID = 1012, 1013, 1018
FIRST_TEXT_ID = 1100                        # frames and notes
FIRST_ITEM_ID, ITEM_STRIDE = 2000, 4        # +0 label, +1 control, +2 browse button / background combo

# The structured controls the hand-written mSL drives by id. Each is placed as
# a block at the next free row of its page; offsets below are from its own
# top-left corner, and its width is a frame's inside, 358. (id, kind, text,
# x, y, w, h, style)
WIDGETS = {
    "onconnect": (
        (1500, "text", "Sent once the server has registered the bot, before it joins: one command per line, "
                       "exactly as typed into a client. %nick% is the nickname the server gave it.",
         0, 0, 358, 16, ""),
        (1501, "edit", "", 0, 18, 358, 40, "multi return autohs autovs hsbar vsbar"),
        (1502, "text", "Seconds between commands", 0, 63, 120, 8, ""),
        (1503, "edit", "", 124, 61, 24, 11, "autohs"),
        # the on-connect commands need the admin password on a token login
        # (#1264); Apply saves them, with everything else
        (1506, "button", "Unlock", 156, 60, 44, 13, ""),
        (1505, "button", "Resend now", 208, 60, 56, 13, ""),
    ),
    # The tables (the "list" controls) are each a native list, and with DCX a
    # listview with the columns LISTVIEWS gives it, over the same rectangle.
    "channels": (
        (1520, "list", "", 0, 0, 250, 168, "vsbar"),
        (1521, "edit", "", 256, 0, 102, 11, "autohs"),
        (1522, "button", "Add channel", 256, 14, 56, 13, ""),
        (1523, "button", "Remove", 256, 30, 56, 13, ""),
    ),
    # Lists: a table with a row per list, as the mockup draws it. Add list
    # and Edit open the list window (dccore.setl) for one list's name,
    # primary flag and folders; Delete takes the selected list.
    "srvlists": (
        (1540, "list", "", 0, 0, 358, 64, "hsbar vsbar"),
        (1551, "button", "Add list", 0, 68, 50, 13, ""),
        (1554, "button", "Edit", 53, 68, 46, 13, ""),
        (1555, "button", "Delete", 102, 68, 46, 13, ""),
        (1557, "button", "Refresh", 151, 68, 46, 13, ""),
    ),
    # Channel bindings: the mockup's rows, below; Refresh and the count under
    # them. Apply saves the lists and the bindings, with everything else.
    "srvchans": None,
    "srvsaveb": (
        (1567, "button", "Refresh", 0, 0, 46, 13, ""),
        (1568, "text", "", 52, 2, 200, 8, ""),
    ),
    "bantimed": (
        (1580, "list", "", 0, 0, 358, 60, "vsbar"),
        (1583, "text", "Nick", 0, 66, 20, 8, ""),
        (1584, "edit", "", 22, 64, 90, 11, "autohs"),
        (1585, "text", "Minutes", 118, 66, 32, 8, ""),
        (1586, "edit", "", 152, 64, 30, 11, "autohs"),
        (1587, "button", "Ignore", 188, 63, 50, 13, ""),
        (1581, "button", "Lift now", 0, 79, 50, 13, ""),
        (1582, "button", "Refresh", 53, 79, 46, 13, ""),
    ),
    "banperm": (
        (1595, "list", "", 0, 0, 358, 60, "vsbar"),
        (1597, "edit", "", 0, 64, 250, 11, "autohs"),
        (1598, "button", "Add pattern", 254, 63, 56, 13, ""),
        (1596, "button", "Remove", 0, 79, 50, 13, ""),
    ),
    "preview": (
        (1610, "button", "Preview", 0, 0, 50, 13, ""),
        (1611, "text", "Draws the sample advert and notice in @DCCore-preview with the colours chosen here, "
                       "before they are saved.", 56, 0, 302, 16, ""),
    ),
    # The Script tab: this mIRC's own settings, from the Options dialog that
    # was (dccore.sw.opt.fill fills them from dccore.ini, dccore.sw.opt.save
    # reads them back on Apply). "showin" is built below: a check and a
    # colour menu per line type @DCCore shows, then the status line.
    "showin": None,
    "showcolours": (
        (1660, "text", "File names", 0, 2, 110, 8, ""),
        (1640, "combo", "", 114, 0, 96, 80, "drop"),
        (1661, "text", "Console replies", 0, 15, 110, 8, ""),
        (1641, "combo", "", 114, 13, 96, 80, "drop"),
        (1662, "text", "Panel headings", 0, 28, 110, 8, ""),
        (1642, "combo", "", 114, 26, 96, 80, "drop"),
        (1663, "text", "Search text", 0, 41, 110, 8, ""),
        (1643, "combo", "", 114, 39, 96, 80, "drop"),
        (1664, "text", "Nicks", 0, 54, 110, 8, ""),
        (1644, "combo", "", 114, 52, 96, 80, "drop"),
        (1665, "text", "Channels", 0, 67, 110, 8, ""),
        (1645, "combo", "", 114, 65, 96, 80, "drop"),
    ),
    "winlook": (
        (1670, "check", "Fixed-width font, size", 0, 1, 110, 10, ""),
        (1671, "edit", "", 134, 0, 24, 11, "autohs"),
        (1672, "text", "Background", 0, 15, 120, 8, ""),
        (1673, "combo", "", 134, 13, 96, 80, "drop"),
        (1674, "text", "Finished downloads to show", 0, 28, 120, 8, ""),
        (1675, "edit", "", 134, 26, 24, 11, "autohs"),
    ),
    # The theme and the icons (#1368): auto / dark / light on the menu's
    # lines 1 to 3, as dccore.sw.opt.fill fills it.
    "theme": (
        (1690, "text", "Theme", 0, 2, 120, 8, ""),
        (1691, "combo", "", 134, 0, 96, 80, "drop"),
        (1692, "check", "Icons in the menus and the control panel's page list", 0, 15, 358, 10, ""),
    ),
    "botlink": (
        (1680, "text", "Bot nick", 0, 2, 36, 8, ""),
        (1681, "edit", "", 40, 0, 80, 11, "autohs"),
        (1682, "text", "", 126, 2, 232, 8, ""),
        (1683, "button", "Pair again...", 0, 14, 56, 13, ""),
        (1684, "button", "Forget token", 59, 14, 56, 13, ""),
        (1685, "check", "Console feed on (also requests and sends - not just status)", 0, 31, 358, 10, ""),
    ),
}

# @DCCore's line types, in dccore.groups' order: a check and a colour menu
# each (1620 + 2r, 1621 + 2r), then "Status every [ ] min, 0 = off".
SHOW_LINES = ("Requests (who asked for what)", "Queue positions", "Sends: starting, resuming, done",
              "Failed transfers", "Searches and result counts", "Joins, parts, quits of queued users",
              "Bans, mutes, floods", "Other log lines")
SHOW_FIRST_ID = 1620


def show_rows():
    parts = []
    for r, label in enumerate(SHOW_LINES):
        y = 13 * r
        parts += [(SHOW_FIRST_ID + 2 * r, "check", label, 0, y + 1, 176, 10, ""),
                  (SHOW_FIRST_ID + 2 * r + 1, "combo", "", 188, y, 96, 80, "drop")]
    y = 13 * len(SHOW_LINES)
    parts += [(1636, "text", "Status every", 0, y + 2, 60, 8, ""),
              (1637, "edit", "", 62, y, 24, 11, "autohs"),
              (1638, "text", "min, 0 = off", 94, y + 2, 70, 8, "")]
    return tuple(parts)


WIDGETS["showin"] = show_rows()
# The Channel bindings page: the mockup's rows. A row per channel, each with
# its own List and Mode menus and a Remove button, a scrollbar beside them
# for the channels beyond the BIND_ROWS shown, then the row that adds one.
# Row R's controls are BIND_FIRST_ID + 4(R-1): its channel, +1 its List
# menu, +2 its Mode menu, +3 its Remove. The script fills the rows from the
# scrollbar's position and hides the spare ones (dccore.sw.srv.drawrows):
# the rows never move, their contents do.
BIND_ROWS, BIND_FIRST_ID, BIND_PITCH = 15, 1700, 13
BIND_ADD_ID = 1770                          # the row that adds one: edit, List, Mode, Add
BIND_SCROLL_ID = 1774


def binding_rows():
    parts = []
    for r in range(BIND_ROWS):
        base, y = BIND_FIRST_ID + 4 * r, BIND_PITCH * r
        parts += [(base, "text", "", 0, y + 2, 98, 8, ""),
                  (base + 1, "combo", "", 102, y, 84, 80, "drop"),
                  (base + 2, "combo", "", 190, y, 84, 60, "drop"),
                  (base + 3, "button", "Remove", 284, y - 1, 56, 13, "")]
    parts.append((BIND_SCROLL_ID, "scroll", "", 347, 0, 11, BIND_PITCH * BIND_ROWS - 1, "range 0 0 vertical"))
    y = BIND_PITCH * BIND_ROWS + 4
    parts += [(BIND_ADD_ID, "edit", "", 0, y, 98, 11, "autohs"),
              (BIND_ADD_ID + 1, "combo", "", 102, y, 84, 80, "drop"),
              (BIND_ADD_ID + 2, "combo", "", 190, y, 84, 60, "drop"),
              (BIND_ADD_ID + 3, "button", "Add", 284, y - 1, 56, 13, "")]
    return tuple(parts)


WIDGETS["srvchans"] = binding_rows()

# The heights the layout reserves for each (the lowest control's bottom, plus a gap).
WIDGET_H = {name: max(y + (12 if kind == "combo" else h) for _i, kind, _t, _x, y, _w, h, _s in parts) + 4
            for name, parts in WIDGETS.items()}
# The widgets that talk to the bot (disabled with the settings when the bot
# cannot be edited); these are this mIRC's own, editable whatever the bot says.
LOCAL_WIDGETS = ("showin", "showcolours", "winlook", "theme", "botlink")
# The command each structured page asks with, the first time it is shown.
WIDGET_ASKS = {"onconnect": "onconnect", "srvlists": "served", "srvchans": "served", "srvsaveb": "served",
               "bantimed": "banlist", "banperm": "banlist"}

# The tables' columns, for DCX (#1288): each native list above, by id, gets a
# listview over its rectangle with these columns - (width in dialog units,
# title), 0 for the rest of the width - and the extra listview styles named.
# The script reads or writes a table through dccore.sw.lv.*, which addresses
# the listview when there is one and the native list when there is not.
LISTVIEWS = {
    1520: (910, ((0, "Channel"),), "noheader"),
    1540: (911, ((120, "Name"), (44, "Primary"), (0, "Folders")), ""),
    1580: (913, ((160, "Nick"), (0, "Time left")), ""),
    1595: (914, ((0, "Pattern"),), ""),
}

# Help for this mIRC's own switches, which have no dashboard help text.
LOCAL_HELP = "This mIRC's own setting, kept in dccore.ini beside the script - not the bot's. Saved by Apply or OK."

COLOUR_KEYS = ("settings.colourWhite", "settings.colourBlack", "settings.colourBlue", "settings.colourGreen",
               "settings.colourRed", "settings.colourMaroon", "settings.colourPurple", "settings.colourOrange",
               "settings.colourYellow", "settings.colourLightGreen", "settings.colourCyan",
               "settings.colourLightCyan", "settings.colourRoyalBlue", "settings.colourPink",
               "settings.colourGrey", "settings.colourLightGrey")
MODE_KEYS = (("normal", "settings.channelModeNormal"), ("quiet", "settings.channelModeQuiet"),
             ("request_only", "settings.channelModeRequestOnly"))
TRI_LABELS = ("Not set (automatic)", "On", "Off")

CHUNK_IDS = 50          # ids per hash entry: a mIRC line stays far under 900 characters
CHUNK_KEYS = 15


class LayoutError(Exception):
    pass


# ---------------------------------------------------------------------------
# The bot's metadata
# ---------------------------------------------------------------------------

def tri_state_keys():
    """Booleans DECLARED with None as their default (`X: bool = None`): a
    third answer, "not set". Read from defaults.py's source, never from the
    imported module, whose values a local settings.conf may have changed."""
    with io.open(DEFAULTS_FILE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = set()
    for node in tree.body:
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and isinstance(node.annotation, ast.Name) and node.annotation.id == "bool"
                and isinstance(node.value, ast.Constant) and node.value.value is None):
            found.add(node.target.id)
    return found


def metadata():
    import defaults
    import settings_file
    import settings_help
    import webserver
    with io.open(LANG_FILE, encoding="utf-8") as handle:
        lang = json.load(handle)
    return {
        "categories": webserver.SETTINGS_CATEGORIES,
        "labels": webserver.SETTINGS_LABELS,
        "units": webserver.SETTINGS_UNITS,
        "choice_labels": webserver.CHOICE_LABELS,
        "choices": settings_file.CHOICES,
        "types": settings_file.declared_types(vars(defaults)),
        "help": settings_help.help_text,
        "tri": tri_state_keys(),
        "colours": [lang[key] for key in COLOUR_KEYS],
        "theme_default": lang["settings.themeDefault"],
        "keep_previous": lang["settings.keepPrevious"],
        "modes": [(mode, lang[key]) for mode, key in MODE_KEYS],
    }


# ---------------------------------------------------------------------------
# Text for mIRC
# ---------------------------------------------------------------------------

# What a generated `hadd` line may carry as it is. Everything else - "$" and
# "%" (evaluated), "|" (a command separator), braces, brackets, "#", "," and
# "~" itself - is written ~HH and decoded by $dccore.sw.untext.
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .:;'\"!?()/-_+=*<>@&^\\")


def hash_text(text):
    text = " ".join(str(text).split())
    out = []
    for ch in text:
        if ch in _SAFE:
            out.append(ch)
        elif ord(ch) < 128:
            out.append("~%02X" % ord(ch))
        else:
            raise LayoutError("not ASCII: %r" % text)
    return "".join(out)


def static(text):
    """Text for a static or check control: a lone & would underline the next letter."""
    return text.replace("&", "&&")


def table_safe(text):
    """Whether `text` can be written into the dialog table as it is."""
    return all(ord(c) < 128 for c in text) and not any(c in text for c in '"$%|')


def help_for(text):
    """The help text cut to what HELP shows: three lines, whole sentences where
    they fit, else whole words and an ellipsis."""
    text = " ".join(str(text or "").split())
    width = HELP[2]
    lines_max = HELP[3] // LINE_DBU
    if wrapped_lines(text, width) <= lines_max:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for sentence in sentences:
        attempt = (kept + " " + sentence).strip()
        if wrapped_lines(attempt, width) > lines_max:
            break
        kept = attempt
    if kept:
        return kept
    words = text.split(" ")
    while words and wrapped_lines(" ".join(words) + "...", width) > lines_max:
        words.pop()
    return " ".join(words) + "..."


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

class Builder:

    def __init__(self, layout, meta):
        self.layout = layout
        self.meta = meta
        self.controls = []          # dicts: kind text id x y w h style page role key
        self.data = []              # (name, value) for dccore.swm
        self.next_text = FIRST_TEXT_ID
        self.next_item = FIRST_ITEM_ID
        self.page = 0
        self.keys = []
        self.placed = {}
        self.locals = []
        self.browse = []
        self.page_widgets = {}
        self.page_layout = {}       # page -> "roomy 1", "compact 2", ...: the form it was given
        self.labels = {}            # KEY -> its label in the window, for the status line's errors

    # -- one control -------------------------------------------------------

    def add(self, kind, cid, x, y, w, h, text="", style="", role="", key=""):
        self.controls.append({"kind": kind, "id": cid, "x": x, "y": y, "w": w, "h": h, "text": text,
                              "style": style, "page": self.page, "role": role, "key": key})

    def text_id(self):
        cid = self.next_text
        self.next_text += 1
        if self.next_text >= 1500:
            raise LayoutError("too many headers and notes")
        return cid

    def item_ids(self):
        base = self.next_item
        self.next_item += ITEM_STRIDE
        return base

    # -- measuring an item without placing it --------------------------------

    def kind_of(self, key):
        if key in self.meta["tri"]:
            return "tri"
        if key.startswith("CUSTOM_THEME_"):
            return "colour"
        if key in self.meta["choices"]:
            return "choice"
        declared = self.meta["types"].get(key)
        if declared is None:
            raise LayoutError("%s is not a declared setting" % key)
        name = declared.__name__
        if name not in ("bool", "int", "float", "str", "list"):
            raise LayoutError("%s has a type the window cannot edit: %s" % (key, name))
        return name

    def label_of(self, item):
        if isinstance(item, dict) and "label" in item and "key" in item:
            label = item["label"]
            key = item["key"]
        else:
            key = item
            label = self.meta["labels"].get(key)
            if not label:
                raise LayoutError("%s has no label in SETTINGS_LABELS" % key)
        unit = self.meta["units"].get(key)
        if unit:
            label = "%s (%s)" % (label, unit[0])
        return label

    def item_height(self, item, columns, style, boxed):
        width = self.item_width(columns, boxed)
        if isinstance(item, dict) and "note" in item:
            return wrapped_lines(item["note"], width) * LINE_DBU + 3
        if isinstance(item, dict) and "widget" in item:
            return WIDGET_H[item["widget"]]
        if isinstance(item, dict) and "local" in item:
            # a check box does not wrap: one its column cuts is "too tall",
            # so the page is tried in the next form before it is refused
            return CHECK_PITCH if self.check_fits(item["label"], width) else CONTENT_H + 1
        key = item["key"] if isinstance(item, dict) else item
        kind = self.kind_of(key)
        if kind == "bool":
            return CHECK_PITCH if self.check_fits(self.label_of(item), width) else CONTENT_H + 1
        if style == "roomy":
            return wrapped_lines(self.label_of(item), width) * LINE_DBU + ROOMY_GAP + EDIT_H + ROOMY_AFTER
        label_w = width - CONTROL_W[columns] - LABEL_GAP
        lines = wrapped_lines(self.label_of(item), label_w)
        pitch = COMBO_PITCH if kind in ("choice", "tri", "colour") else ROW_PITCH
        return max(pitch, lines * LINE_DBU + 4)

    def column_width(self, columns):
        return CONTENT_W if columns == 1 else (CONTENT_W - COLUMN_GAP) // 2

    def item_width(self, columns, boxed):
        """What an item may take: the column, less a frame's sides."""
        return self.column_width(columns) - (2 * BOX_PAD if boxed else 0)

    def section_height(self, section, columns, style, first):
        header, items = section
        inner = sum(self.item_height(item, columns, style, bool(header)) for item in items)
        if header:
            inner += BOX_TOP + BOX_BOTTOM
        return (0 if first else SECTION_GAP) + inner

    # -- a page ----------------------------------------------------------------

    def place_page(self, name, sections):
        has_widget = any(isinstance(i, dict) and "widget" in i for _h, items in sections for i in items)
        for style, columns in STYLES:
            if columns == 1:
                if sum(self.section_height(s, 1, style, n == 0) for n, s in enumerate(sections)) <= CONTENT_H:
                    self.place_column(sections, CONTENT_X, 1, style)
                    self.page_layout[self.page] = "%s %d" % (style, columns)
                    return
                continue
            if has_widget:
                # a widget is one block, which a column split could cut
                continue
            best = None
            for split in range(1, len(sections)):
                left = sum(self.section_height(s, 2, style, n == 0) for n, s in enumerate(sections[:split]))
                right = sum(self.section_height(s, 2, style, n == 0) for n, s in enumerate(sections[split:]))
                if best is None or max(left, right) < best[0]:
                    best = (max(left, right), split)
            if best is not None and best[0] <= CONTENT_H:
                split = best[1]
                self.place_column(sections[:split], CONTENT_X, 2, style)
                self.place_column(sections[split:], CONTENT_X + self.column_width(2) + COLUMN_GAP, 2, style)
                self.page_layout[self.page] = "%s %d" % (style, columns)
                return
        for header, items in sections:
            for item in items:
                label = (item.get("label") if isinstance(item, dict) and "local" in item
                         else self.label_of(item) if self.is_check(item) else None)
                if label and not self.check_fits(label, self.item_width(1, bool(header))):
                    raise LayoutError("the check %r does not fit in %d dbu" % (label, self.item_width(1, bool(header))))
        raise LayoutError("page %s does not fit, even compact in two columns" % name)

    def is_check(self, item):
        if isinstance(item, dict) and ("note" in item or "widget" in item or "local" in item):
            return False
        return self.kind_of(item["key"] if isinstance(item, dict) else item) == "bool"

    def place_column(self, sections, x, columns, style):
        width = self.column_width(columns)
        y = CONTENT_Y
        for number, (header, items) in enumerate(sections):
            if number:
                y += SECTION_GAP
            boxed = bool(header)
            if boxed:
                # the frame first: a control listed after it is drawn over it
                inner = sum(self.item_height(item, columns, style, True) for item in items)
                self.add("box", self.text_id(), x, y, width, BOX_TOP + inner + BOX_BOTTOM, header, role="header")
                ix, iy = x + BOX_PAD, y + BOX_TOP
            else:
                ix, iy = x, y
            for item in items:
                self.place_item(item, ix, iy, self.item_width(columns, boxed), columns, style)
                iy += self.item_height(item, columns, style, boxed)
            y = iy + (BOX_BOTTOM if boxed else 0)

    def place_item(self, item, x, y, width, columns, style):
        if isinstance(item, dict) and "note" in item:
            lines = wrapped_lines(item["note"], width)
            self.add("text", self.text_id(), x, y, width, lines * LINE_DBU, item["note"], role="note")
            return
        if isinstance(item, dict) and "widget" in item:
            name = item["widget"]
            self.page_widgets.setdefault(self.page, []).append(name)
            for key in item.get("keys", ()):
                self.claim(key)
            keys = list(item.get("keys", ()))
            for number, (cid, kind, text, dx, dy, w, h, wstyle) in enumerate(WIDGETS[name]):
                self.add(kind, cid, x + dx, y + dy, w, h, text, wstyle,
                         role="local" if name in LOCAL_WIDGETS else "widget",
                         key=keys[0] if keys and number == 0 else "")
            for key in item.get("keys", ()):
                self.keys.append((key, WIDGETS[name][0][0], "chanlist", 1, 0))
                self.labels[key] = self.meta["labels"].get(key, key)
            return
        if isinstance(item, dict) and "local" in item:
            base = self.item_ids()
            self.add("check", base + 1, x, y, width, CHECK_H, item["label"], role="local",
                     key="@" + item["local"])
            self.locals.append((item["local"], base + 1))
            return
        key = item["key"] if isinstance(item, dict) else item
        self.claim(key)
        kind = self.kind_of(key)
        label = self.label_of(item)
        base = self.item_ids()
        unit = self.meta["units"].get(key)
        factor = unit[1] if unit else 1
        if kind == "bool":
            self.add("check", base + 1, x, y, width, CHECK_H, label, role="value", key=key)
            self.keys.append((key, base + 1, kind, factor, base + 1))
            self.labels[key] = label
            return
        if style == "roomy":
            # the label on its own line, the control under it, as wide as the frame
            lines = wrapped_lines(label, width)
            self.add("text", base, x, y, width, lines * LINE_DBU, label, role="label", key=key)
            cx, cy, control_w = x, y + lines * LINE_DBU + ROOMY_GAP, width
            menu_w, colour_w = min(CHOICE_W, width), COLOUR_W
        else:
            control_w = CONTROL_W[columns]
            label_w = width - control_w - LABEL_GAP
            lines = wrapped_lines(label, label_w)
            self.add("text", base, x, y + 2, label_w, lines * LINE_DBU, label, role="label", key=key)
            cx, cy = x + label_w + LABEL_GAP, y
            menu_w, colour_w = control_w, (control_w - COLOUR_GAP) // 2
        if kind in ("choice", "tri"):
            self.add("combo", base + 1, cx, cy, menu_w, 80, style="drop", role="value", key=key)
        elif kind == "colour":
            self.add("combo", base + 1, cx, cy, colour_w, 120, style="drop", role="value", key=key)
            self.add("combo", base + 2, cx + colour_w + COLOUR_GAP, cy, colour_w, 120, style="drop", role="value",
                     key=key)
        else:
            browse = (key in self.layout.FOLDER_KEYS and "dir") or (key in self.layout.FILE_KEYS and "file")
            edit_w = control_w - (BROWSE_W + 2 if browse else 0)
            self.add("edit", base + 1, cx, cy, edit_w, EDIT_H, style="autohs", role="value", key=key)
            if browse:
                self.add("button", base + 2, cx + edit_w + 2, cy - 1, BROWSE_W, EDIT_H + 1, "...",
                         role="browse", key=key)
                self.browse.append((base + 2, browse, key))
        self.keys.append((key, base + 1, kind, factor, base))
        self.labels[key] = label

    def check_fits(self, label, width):
        """Whether a check box's label fits, with the fifth to spare: a check
        does not wrap, its label is cut off instead."""
        return text_px(label) + CHECK_BOX_PX <= width * PX_PER_DBU * FONT_SLACK

    def claim(self, key):
        if key in self.placed:
            raise LayoutError("%s is placed twice (pages %d and %d)" % (key, self.placed[key], self.page))
        self.placed[key] = self.page

    # -- the whole window ------------------------------------------------------

    def build(self):
        layout = self.layout
        pages = []
        for gnum, (group, group_pages) in enumerate(layout.GROUPS, start=1):
            numbers = []
            for name, sections in group_pages:
                self.page += 1
                pages.append((self.page, gnum, name))
                numbers.append(self.page)
                self.place_page(name, sections)
            self.data.append(("g.%d" % gnum, group))
            self.data.append(("g.%d.pages" % gnum, " ".join(str(n) for n in numbers)))
        self.check_placement()
        self.pages = pages
        return self

    def check_placement(self):
        every = [key for _cid, _label, keys in self.meta["categories"] for key in keys]
        missing = [k for k in every if k not in self.placed and k not in self.layout.EXCLUDED]
        if missing:
            raise LayoutError("no place in the window and not in EXCLUDED: %s" % ", ".join(missing))
        both = [k for k in self.layout.EXCLUDED if k in self.placed]
        if both:
            raise LayoutError("placed and excluded at once: %s" % ", ".join(both))
        unknown = [k for k in self.placed if k not in every]
        if unknown:
            raise LayoutError("not a setting the Settings page offers: %s" % ", ".join(unknown))


# ---------------------------------------------------------------------------
# Writing the block
# ---------------------------------------------------------------------------

def chunks(values, size):
    return [values[i:i + size] for i in range(0, len(values), size)] or [[]]


def control_line(control, inits):
    kind, cid = control["kind"], control["id"]
    pos = "%d %d %d %d" % (control["x"], control["y"], control["w"], control["h"])
    style = (", " + control["style"]) if control["style"] else ""
    if kind in ("combo", "list", "edit"):
        lead = '"", ' if kind == "edit" else ""
        return "  %s %s%d, %s%s" % (kind, lead, cid, pos, style)
    text = control["text"]
    if text and not table_safe(text):
        inits.append((cid, text))
        text = ""
    return '  %s "%s", %d, %s%s' % (kind, static(text), cid, pos, style)


def render(builder):
    meta = builder.meta
    layout = builder.layout
    out = [BEGIN,
           "; Generated - do not edit by hand. Change scripts/mirc/settings_window_layout.py (or the",
           "; bot's settings metadata) and run: python scripts/mirc/build_settings_window.py",
           "dialog dccore.set {",
           '  title "DCCore - Control Panel"',
           "  size -1 -1 %d %d" % (DIALOG_W, DIALOG_H),
           "  option dbu"]
    groups = [group for group, _pages in layout.GROUPS]
    for number, (cid, group) in enumerate(zip(TAB_IDS, groups)):
        if number == 0:
            out.append('  tab "%s", %d, %d %d %d %d' % ((static(group), cid) + TABS))
        else:
            out.append('  tab "%s", %d' % (static(group), cid))
    slots = max(len(pages) for _group, pages in layout.GROUPS)
    for number in range(slots):
        y = PAGE_SLOT_Y + number * (PAGE_SLOT_H + PAGE_SLOT_GAP)
        if y + PAGE_SLOT_H > CONNECTED[1] - 2 or FIRST_SLOT_ID + number >= FIRST_TEXT_ID:
            raise LayoutError("%d pages in one tab do not fit in the column of page buttons" % slots)
        out.append('  radio "", %d, %d %d %d %d, push%s' % (FIRST_SLOT_ID + number, PAGE_SLOT_X, y, PAGE_SLOT_W,
                                                         PAGE_SLOT_H, " group" if number == 0 else ""))
    body_top, body_bottom = TABS[1] + TAB_HEADER_H, TABS[1] + TABS[3]
    if not (TABS[0] < PAGE_SLOT_X and TABS[0] < CONTENT_X and CONTENT_X + CONTENT_W < TABS[0] + TABS[2]
            and HELP[1] + HELP[3] < body_bottom < CONNECTED[1]):
        raise LayoutError("the tab control %r does not frame the body" % (TABS,))
    pages_x, pages_y, pages_w, pages_h = DCX_PAGES
    if (pages_x + pages_w > CONTENT_X - 2 or pages_y < body_top or pages_y + pages_h > body_bottom - 2
            or DCX_PAGES_ID >= min(TAB_IDS)):
        raise LayoutError("the DCX page list %r (id %d) overlaps the tabs, the page or another control"
                          % (DCX_PAGES, DCX_PAGES_ID))
    title_x, title_y, title_w, title_h = DCX_TITLE
    if (title_x < CONTENT_X or title_x + title_w > CONTENT_X + CONTENT_W or title_y < body_top
            or title_y + title_h > CONTENT_Y):
        raise LayoutError("the DCX title %r is not between the tabs and the page" % (DCX_TITLE,))
    if not DCX_PAGES_ID < DCX_TITLE_ID < min(LISTVIEWS[n][0] for n in LISTVIEWS):
        raise LayoutError("the DCX ids collide: page list %d, title %d" % (DCX_PAGES_ID, DCX_TITLE_ID))
    # the lines the script draws with DCX: the tab strip, side by side from
    # the tab control's left edge; every frame, its DCX ids in page order
    tabs = []
    x = TAB_START
    for group in groups[:-1]:
        tabs.append((x, dbu_for(text_px(group)) + 2 * TAB_PAD))
        x += tabs[-1][1] + TAB_GAP
    if x - TAB_GAP > TABS[0] + TABS[2]:
        raise LayoutError("the tab strip is wider than the tab control: %d > %d" % (x - TAB_GAP - TABS[0], TABS[2]))
    if not (TABS[0] < BACK[0] and BACK[0] + BACK[2] < CRUMB[0] and CRUMB[0] + CRUMB[2] <= TAB_START
            and TABS[1] <= BACK[1] and BACK[1] + BACK[3] <= TABS[1] + TAB_HEADER_H):
        raise LayoutError("the way back %r and the side's name %r are not at the strip's left end" % (BACK, CRUMB))
    if dbu_for(text_px("mIRC")) + 2 * TITLE_PAD > CRUMB[2]:
        raise LayoutError("the side's name does not fit %r" % (CRUMB,))
    pills_w = len(HOME) * HOME_PILL_W + (len(HOME) - 1) * HOME_PILL_GAP
    if pills_w > TABS[2] - 2 * ROUND or HOME_PILL_Y + HOME_PILL_H > CONTENT_Y + CONTENT_H:
        raise LayoutError("the start page's pills do not fit the body")
    for name, line in HOME:
        if text_px(line) + 2 * TITLE_PAD * PX_PER_DBU > HOME_PILL_W * PX_PER_DBU or set(name + line) - set(CHAR_PX):
            raise LayoutError("the start page's line %r does not fit its pill" % line)
    if not (BACK_DCX_FIRST + 3 <= CRUMB_DCX < HOME_DCX_FIRST
            and HOME_DCX_FIRST + len(HOME) * PILL_STRIDE <= DCX_PAGES_ID and BODY_DCX_FIRST + 1 <= BACK_DCX_FIRST):
        raise LayoutError("the DCX ids of the way back, the side's name and the start page collide")
    # the start page's figures: a frame's place in its column, its rows' places
    stats = []
    next_id = STATS_DCX_FIRST
    column_y = [STATS_Y, STATS_Y]
    for column, title, kind, rows in STATS:
        keys = list(rows) if kind == "kv" else [""] * rows
        x = STATS_X + column * (STATS_W + STATS_GAP)
        y = column_y[column]
        h = BOX_TOP + len(keys) * STATS_ROW + BOX_BOTTOM
        column_y[column] = y + h + STATS_DOWN
        room = title + " 99/99" if kind == "full" else title
        if set(room + "".join(keys)) - set(CHAR_PX):
            raise LayoutError("the figures' frame %r has characters the width table lacks" % title)
        if TITLE_X + dbu_for(text_px(room)) + 2 * TITLE_PAD > STATS_W - TITLE_X or any(
                dbu_for(text_px(key)) > STATS_KEY_W for key in keys):
            raise LayoutError("the figures' frame %r or a key of it does not fit" % title)
        frame = {"ring": next_id, "title": next_id + 1, "name": title, "kind": kind,
                 "ring_rect": (x, y + LINE_DROP, STATS_W, h - LINE_DROP),
                 "title_rect": (x + TITLE_X, y, dbu_for(text_px(room)) + 2 * TITLE_PAD, TITLE_H), "rows": []}
        next_id += 2
        for number, key in enumerate(keys):
            row_y = y + BOX_TOP + number * STATS_ROW
            if kind == "kv":
                frame["rows"].append((next_id, next_id + 1, key,
                                      (x + BOX_PAD, row_y, STATS_KEY_W, LINE_DBU),
                                      (x + BOX_PAD + STATS_KEY_W + 4, row_y, STATS_W - 2 * BOX_PAD - STATS_KEY_W - 4,
                                       LINE_DBU)))
                next_id += 2
            else:
                frame["rows"].append((next_id, 0, "", (x + BOX_PAD, row_y, STATS_W - 2 * BOX_PAD, LINE_DBU),
                                      (0, 0, 0, 0)))
                next_id += 1
        stats.append(frame)
    if max(column_y) - STATS_DOWN > TABS[1] + TABS[3] - 4 or STATS_X + 2 * STATS_W + STATS_GAP > TABS[0] + TABS[2] - 4:
        raise LayoutError("the start page's figures do not fit the body")
    boxes = sum(1 for c in builder.controls if c["kind"] == "box")
    if not (FRAME_DCX_FIRST + boxes * FRAME_DCX_STRIDE <= STATS_DCX_FIRST and next_id <= TAB_DCX_FIRST):
        raise LayoutError("the DCX ids of the start page's figures collide with the frames' or the tabs'")
    frames = [c for c in builder.controls if c["kind"] == "box"]
    for index, control in enumerate(frames):
        missing = sorted(set(control["text"]) - set(CHAR_PX))
        if missing:
            raise LayoutError("the frame title %r has characters the width table lacks: %s"
                              % (control["text"], missing))
        control["dcx"] = FRAME_DCX_FIRST + index * FRAME_DCX_STRIDE
        if TITLE_X + dbu_for(text_px(control["text"])) + 2 * TITLE_PAD > control["w"] - TITLE_X:
            raise LayoutError("the frame title %r does not fit its frame" % control["text"])
        if control["h"] - LINE_DROP < 2 * (ROUND_PX + 1) + 1 or control["w"] < 2 * (ROUND_PX + 1) + 1:
            raise LayoutError("the frame %r is too small for its rounded corners" % control["text"])
    if (FRAME_DCX_FIRST + len(frames) * FRAME_DCX_STRIDE > TAB_DCX_FIRST
            or TAB_DCX_FIRST + len(groups) * TAB_DCX_STRIDE > BODY_DCX_FIRST or BODY_DCX_FIRST + 1 > DCX_PAGES_ID):
        raise LayoutError("the DCX ids of the frames (%d), the tabs, the body frame and the page list collide"
                          % len(frames))
    # every table has its columns, and every set of columns its table
    lists = [c for c in builder.controls if c["kind"] == "list"]
    if sorted(c["id"] for c in lists) != sorted(LISTVIEWS):
        raise LayoutError("the lists in the window are %s, LISTVIEWS names %s"
                          % (sorted(c["id"] for c in lists), sorted(LISTVIEWS)))
    dcx_ids = [LISTVIEWS[n][0] for n in LISTVIEWS]
    if len(set(dcx_ids)) != len(dcx_ids) or max(dcx_ids) >= min(TAB_IDS):
        raise LayoutError("the listviews' DCX ids are not unique, or not below the dialog's own: %s" % dcx_ids)
    for native, (_dcx_id, columns, extra) in LISTVIEWS.items():
        for _width, title in columns:
            if "," in title or set(title) - _SAFE:
                raise LayoutError("the column title %r of list %d cannot be written as it is" % (title, native))
        if [w for w, _t in columns].count(0) != 1 or columns[-1][0] != 0:
            raise LayoutError("list %d: exactly the last column takes the rest of the width (0)" % native)
    for _group, pages in layout.GROUPS:
        for name, _sections in pages:
            if text_px(name) + BUTTON_PAD_PX > PAGE_SLOT_W * PX_PER_DBU:
                raise LayoutError("the page name %r does not fit its button" % name)
    out.append('  text "", %d, %d %d %d %d' % ((HELP_ID,) + HELP))
    out.append('  text "", %d, %d %d %d %d' % ((CONNECTED_ID,) + CONNECTED))
    out.append('  text "", %d, %d %d %d %d' % ((STATUS_ID,) + STATUS))
    for label, cid, x in BUTTONS:
        style = ", cancel" if label == "Cancel" else ""
        out.append('  button "%s", %d, %d %d %d %d%s' % (label, cid, x, BUTTON_Y, BUTTON_W, BUTTON_H, style))
    inits = []
    for control in builder.controls:
        out.append(control_line(control, inits))
    out.append("}")

    # The lookup data. Built into a hash table once per opening of the window.
    data = list(builder.data)
    data.insert(0, ("groups", str(len(groups))))
    data.insert(1, ("slots", str(max(len(pages) for _group, pages in layout.GROUPS))))
    data.insert(2, ("size", "%d %d" % (DIALOG_W, DIALOG_H)))
    data.insert(3, ("dcxpages", "%d %d %d %d %d" % ((DCX_PAGES_ID,) + DCX_PAGES)))
    data.insert(4, ("dcxtitle", "%d %d %d %d %d" % ((DCX_TITLE_ID,) + DCX_TITLE)))
    # the tab strip (first id, stride, top, height, lift, how many - the bot's
    # groups; each tab's x and width), the body frame (id, rectangle), the way
    # back and the side's name (id, rectangle) and the start page's pills
    # (first id, rectangle; the name; the line under it), in dialog units; a
    # rounded corner's two staircases, in pixels
    data.insert(5, ("dcxtabs", "%d %d %d %d %d %d" % (TAB_DCX_FIRST, TAB_DCX_STRIDE, TABS[1], TAB_HEADER_H, TAB_LIFT,
                                                      len(tabs))))
    data.insert(6, ("dcxbody", "%d %d %d %d %d" % (BODY_DCX_FIRST, TABS[0], TABS[1] + TAB_HEADER_H, TABS[2],
                                                   TABS[3] - TAB_HEADER_H)))
    outer, inner = corner_traces(ROUND_PX)
    data.insert(7, ("dcxcorner.o", " ".join("%d,%d" % point for point in outer)))
    data.insert(8, ("dcxcorner.i", " ".join("%d,%d" % point for point in inner)))
    data.insert(9, ("dcxback", "%d %d %d %d %d" % ((BACK_DCX_FIRST,) + BACK)))
    data.insert(10, ("dcxcrumb", "%d %d %d %d %d" % ((CRUMB_DCX,) + CRUMB)))
    data.insert(11, ("home.n", str(len(HOME))))
    pill_x = TABS[0] + (TABS[2] - pills_w) // 2
    for number, (name, line) in enumerate(HOME, start=1):
        data.insert(11 + number, ("home.%d" % number, "%d %d %d %d %d" % (
            HOME_DCX_FIRST + (number - 1) * PILL_STRIDE, pill_x, HOME_PILL_Y, HOME_PILL_W, HOME_PILL_H)))
        data.append(("home.%d.t" % number, name))
        data.append(("home.%d.s" % number, line))
        pill_x += HOME_PILL_W + HOME_PILL_GAP
    # the figures: every frame's ring and title (ids, rectangles, kind,
    # title) and every row's texts (ids, rectangles, the key); and the one
    # range of ids that shows or hides them all
    data.append(("home.f.n", str(len(stats))))
    data.append(("home.f.ids", "%d-%d" % (STATS_DCX_FIRST, next_id - 1)))
    for number, frame in enumerate(stats, start=1):
        data.append(("home.f.%d" % number, "%d %d %d %d %d %d %d %d %d %d %s %s" % (
            (frame["ring"], frame["title"]) + frame["ring_rect"] + frame["title_rect"] + (frame["kind"], frame["name"]))))
        data.append(("home.f.%d.n" % number, str(len(frame["rows"]))))
        for row_number, (key_id, value_id, key, key_rect, value_rect) in enumerate(frame["rows"], start=1):
            data.append(("home.f.%d.r.%d" % (number, row_number), ("%d %d %d %d %d %d %d %d %d %d %s" % (
                (key_id, value_id) + key_rect + value_rect + (key,))).rstrip()))
    # the tables: each native list, the DCX listview over its rectangle, and
    # that listview's columns and extra styles
    data.insert(12 + len(HOME), ("lvs", " ".join(str(c["id"]) for c in lists)))
    for number, (tab_x, tab_w) in enumerate(tabs, start=1):
        data.append(("tab.%d" % number, "%d %d" % (tab_x, tab_w)))
    # every frame: its DCX ids' first, the rectangle of its ring, the
    # rectangle of its title, and the title
    for control in frames:
        data.append(("fr.%d" % control["id"], "%d %d %d %d %d %d %d %d %d %s" % (
            control["dcx"], control["x"], control["y"] + LINE_DROP, control["w"], control["h"] - LINE_DROP,
            control["x"] + TITLE_X, control["y"], dbu_for(text_px(control["text"])) + 2 * TITLE_PAD, TITLE_H,
            control["text"])))
    for control in lists:
        dcx_id, columns, extra = LISTVIEWS[control["id"]]
        data.append(("lv.%d" % control["id"], "%d %d %d %d %d"
                     % (dcx_id, control["x"], control["y"], control["w"], control["h"])))
        data.append(("lvc.%d" % control["id"], ",".join("%d %s" % (width, title) for width, title in columns)))
        if extra:
            data.append(("lvx.%d" % control["id"], extra))
    data.append(("pages", str(len(builder.pages))))
    for number, _group, name in builder.pages:
        data.append(("p.%d" % number, hash_text(name)))
        data.append(("p.%d.layout" % number, builder.page_layout[number]))
        ids = [c["id"] for c in builder.controls if c["page"] == number]
        parts = chunks(ids, CHUNK_IDS)
        data.append(("p.%d.n" % number, str(len(parts))))
        for index, part in enumerate(parts, start=1):
            data.append(("p.%d.%d" % (number, index), ",".join(str(i) for i in part)))
        page_lists = [c["id"] for c in lists if c["page"] == number]
        if page_lists:
            data.append(("p.%d.lv" % number, " ".join(str(i) for i in page_lists)))
        # the page's frames: the native boxes (hidden with DCX) and the one
        # range of DCX ids that shows or hides their lines and titles
        page_frames = [c for c in frames if c["page"] == number]
        if page_frames:
            first, last = page_frames[0]["dcx"], page_frames[-1]["dcx"]
            if [c["dcx"] for c in page_frames] != list(range(first, last + 1, FRAME_DCX_STRIDE)):
                raise LayoutError("page %d: its frames' DCX ids are not consecutive" % number)
            data.append(("p.%d.bx" % number, ",".join(str(c["id"]) for c in page_frames)))
            data.append(("p.%d.fr" % number, "%d-%d" % (first, last + FRAME_DCX_STRIDE - 1)))
        widgets = builder.page_widgets.get(number)
        if widgets:
            data.append(("p.%d.w" % number, " ".join(widgets)))
            asks = []
            for widget in widgets:
                ask = WIDGET_ASKS.get(widget)
                if ask and ask not in asks:
                    asks.append(ask)
            if asks:
                data.append(("p.%d.ask" % number, " ".join(asks)))
    keys = [key for key, _cid, _kind, _factor, _label in builder.keys]
    parts = chunks(keys, CHUNK_KEYS)
    data.append(("keys.n", str(len(parts))))
    for index, part in enumerate(parts, start=1):
        data.append(("keys.%d" % index, " ".join(part)))
    for key, cid, kind, factor, label_id in builder.keys:
        data.append(("k." + key, "%d %s %d %d" % (cid, kind, factor, label_id)))
        data.append(("pg." + key, str(builder.placed[key])))
        data.append(("n." + key, hash_text(builder.labels[key])))
        if kind in ("choice",):
            choices = list(meta["choices"][key])
            data.append(("ch." + key, " ".join(str(c) for c in choices)))
            labels = meta["choice_labels"].get(key, {})
            for index, choice in enumerate(choices, start=1):
                data.append(("cl.%s.%d" % (key, index), hash_text(labels.get(str(choice), str(choice)))))
        text = meta["help"](key)
        if text:
            data.append(("h." + key, hash_text(static(help_for(text)))))
    for control in builder.controls:
        if control["key"]:
            data.append(("i.%d" % control["id"], control["key"]))
    for option, cid in builder.locals:
        data.append(("lo." + option, str(cid)))
        data.append(("h.@" + option, hash_text(static(help_for(LOCAL_HELP)))))
    data.append(("locals", " ".join(option for option, _cid in builder.locals)))
    for cid, what, key in builder.browse:
        data.append(("br.%d" % cid, "%s %s" % (what, key)))
    bot = [c["id"] for c in builder.controls if c["role"] in ("value", "browse", "widget")]
    parts = chunks(bot, CHUNK_IDS)
    data.append(("bot.n", str(len(parts))))
    for index, part in enumerate(parts, start=1):
        data.append(("bot.%d" % index, ",".join(str(i) for i in part)))
    confirm = [key for key, _cid, _kind, _factor, _label in builder.keys
               if builder.layout_page_of(key) in layout.CONFIRM_PAGES]
    data.append(("confirm", " ".join(confirm)))
    for cid, text in inits:
        data.append(("t.%d" % cid, hash_text(static(text))))
    data.append(("colours", hash_text(",".join("%02d %s" % (n, name) for n, name in enumerate(meta["colours"])))))
    data.append(("themedefault", hash_text(meta["theme_default"])))
    data.append(("keepprevious", hash_text(meta["keep_previous"])))
    data.append(("tri", hash_text(",".join(TRI_LABELS))))
    data.append(("modes", " ".join(mode for mode, _label in meta["modes"])))
    data.append(("modelabels", hash_text(",".join(label for _mode, label in meta["modes"]))))

    out.append("alias dccore.sw.data {")
    out.append("  if ($hget(dccore.swm)) { hfree dccore.swm }")
    out.append("  hmake dccore.swm 100")
    for name, value in data:
        if value == "":
            value = "-"
        out.append("  hadd dccore.swm %s %s" % (name, value))
    out.append("}")
    out.append(END)
    return out


def _page_of(builder):
    names = {number: name for number, _g, name in builder.pages}

    def page_of(key):
        return names.get(builder.placed.get(key))
    return page_of


def generate():
    import settings_window_layout as layout
    builder = Builder(layout, metadata()).build()
    builder.layout_page_of = _page_of(builder)
    return render(builder), builder


def splice(script_text, block_lines):
    """`script_text` with its generated block replaced by `block_lines`."""
    newline = "\r\n" if "\r\n" in script_text else "\n"
    start = script_text.find(BEGIN)
    end = script_text.find(END)
    if start < 0 or end < start:
        raise LayoutError("dccore.mrc has no generated block (%s ... %s)" % (BEGIN, END))
    end += len(END)
    return script_text[:start] + newline.join(block_lines) + script_text[end:]


def shown_path(path):
    """`path` relative to the repository when it can be, else as it is.

    os.path.relpath() raises on Windows when the two are on different
    drives - and a CI Windows runner checks the repository out on D: with
    its temporary folder on C:, where a test points SCRIPT at a copy.
    """
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the block in dccore.mrc is stale")
    args = parser.parse_args(argv)
    block, _builder = generate()
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        current = handle.read()
    wanted = splice(current, block)
    if args.check:
        if wanted != current:
            print("dccore.mrc's settings window block is stale: run python scripts/mirc/build_settings_window.py")
            return 1
        print("dccore.mrc's settings window block is up to date.")
        return 0
    if wanted != current:
        with io.open(SCRIPT, "w", encoding="ascii", newline="") as handle:
            handle.write(wanted)
        print("Rewrote the settings window block in %s." % shown_path(SCRIPT))
    else:
        print("The settings window block was already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
