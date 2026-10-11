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


def wrapped_lines(text, width_dbu):
    """How many lines `text` takes in a static text `width_dbu` wide: Windows
    breaks at spaces, so this is a greedy word wrap with the same widths."""
    width = width_dbu * PX_PER_DBU
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

DIALOG_W, DIALOG_H = 420, 298
TABS = (4, 2, 412, 14)
# The pages of the chosen tab: a column of push-style radio buttons, one per
# slot, as many slots as the tab with the most pages has. Not a listbox: a
# listbox's row height cannot be set from a script, and at a display scale
# above 100% its highlight was shorter than the text, which looked cut.
PAGE_SLOT_X, PAGE_SLOT_Y, PAGE_SLOT_W, PAGE_SLOT_H, PAGE_SLOT_GAP = 4, 20, 84, 15, 2
CONTENT_X, CONTENT_Y, CONTENT_W, CONTENT_H = 94, 18, 322, 226
HELP = (94, 246, 322, 24)                   # three lines of the hovered setting's help
CONNECTED = (4, 272, 254, 8)
STATUS = (4, 281, 254, 16)
BUTTONS = (("Reload", 1017, 262), ("Apply", 1014, 302), ("OK", 1015, 341), ("Cancel", 1016, 380))
BUTTON_Y, BUTTON_W, BUTTON_H = 279, 36, 13

# One column: label | control. Two columns (a page too tall for one): the same,
# narrower, side by side.
COLUMN_GAP = 8
CONTROL_W = {1: 132, 2: 66}
LABEL_GAP = 4
BROWSE_W = 14

EDIT_H, COMBO_H, CHECK_H = 11, 11, 10
ROW_PITCH, COMBO_PITCH, CHECK_PITCH = 12, 13, 11
HEADER_PITCH, SECTION_GAP = 10, 2

# Control ids. Every dialog's ids are its own, but these stay clear of the
# options dialog's (1-703) and the download queues' (1-6) all the same, so a
# grep for an id finds one dialog.
TAB_IDS = range(1001, 1007)
FIRST_SLOT_ID = 1020                        # the page buttons: 1020, 1021, ...
HELP_ID, CONNECTED_ID, STATUS_ID = 1012, 1013, 1018
FIRST_TEXT_ID = 1100                        # section headers and notes
FIRST_ITEM_ID, ITEM_STRIDE = 2000, 4        # +0 label, +1 control, +2 browse button / background combo

# The structured controls the hand-written mSL drives by id. Each is placed as
# a block at the next free row of its page; offsets below are from its own
# top-left corner. (id, kind, text, x, y, w, h, style)
WIDGETS = {
    "onconnect": (
        (1500, "text", "Sent once the server has registered the bot, before it joins: one command per line, "
                       "exactly as typed into a client. %nick% is the nickname the server gave it.",
         0, 0, 322, 16, ""),
        (1501, "edit", "", 0, 18, 322, 40, "multi return autohs autovs hsbar vsbar"),
        (1502, "text", "Seconds between commands", 0, 63, 92, 8, ""),
        (1503, "edit", "", 94, 61, 24, 11, "autohs"),
        (1504, "button", "Save on-connect commands", 170, 60, 100, 13, ""),
        (1505, "button", "Resend now", 274, 60, 48, 13, ""),
        # the on-connect commands need the admin password on a token login (#1264)
        (1506, "button", "Unlock", 122, 60, 44, 13, ""),
    ),
    "channels": (
        (1520, "list", "", 0, 0, 200, 50, "vsbar"),
        (1521, "edit", "", 206, 0, 116, 11, "autohs"),
        (1522, "button", "Add channel", 206, 14, 56, 13, ""),
        (1523, "button", "Remove", 206, 30, 56, 13, ""),
    ),
    "served": (
        (1540, "list", "", 0, 0, 322, 84, "hsbar vsbar"),
        (1541, "text", "Name", 0, 91, 44, 8, ""),
        (1542, "edit", "", 46, 89, 150, 11, "autohs"),
        (1543, "check", "Primary", 204, 89, 60, 10, ""),
        (1544, "text", "List", 0, 104, 44, 8, ""),
        (1545, "combo", "", 46, 102, 96, 80, "drop"),
        (1546, "text", "Mode", 150, 104, 26, 8, ""),
        (1547, "combo", "", 178, 102, 96, 60, "drop"),
        (1548, "text", "Folder path", 0, 117, 44, 8, ""),
        (1549, "edit", "", 46, 115, 260, 11, "autohs"),
        (1550, "button", "...", 308, 114, 14, 13, ""),
        (1551, "button", "Add list", 0, 131, 50, 13, ""),
        (1552, "button", "Add channel", 53, 131, 56, 13, ""),
        (1553, "button", "Add folder", 112, 131, 52, 13, ""),
        (1554, "button", "Change", 167, 131, 46, 13, ""),
        (1555, "button", "Remove", 216, 131, 46, 13, ""),
        (1556, "button", "Save lists", 0, 147, 56, 13, ""),
        (1557, "button", "Refresh", 59, 147, 46, 13, ""),
    ),
    "bantimed": (
        (1580, "list", "", 0, 0, 240, 44, "vsbar"),
        (1581, "button", "Lift now", 246, 0, 50, 13, ""),
        (1582, "button", "Refresh", 246, 16, 50, 13, ""),
        (1583, "text", "Nick", 0, 50, 20, 8, ""),
        (1584, "edit", "", 22, 48, 90, 11, "autohs"),
        (1585, "text", "Minutes", 118, 50, 32, 8, ""),
        (1586, "edit", "", 152, 48, 30, 11, "autohs"),
        (1587, "button", "Ignore", 188, 47, 50, 13, ""),
    ),
    "banperm": (
        (1595, "list", "", 0, 0, 240, 44, "vsbar"),
        (1596, "button", "Remove", 246, 0, 50, 13, ""),
        (1597, "edit", "", 0, 48, 182, 11, "autohs"),
        (1598, "button", "Add pattern", 188, 47, 50, 13, ""),
    ),
    "preview": (
        (1610, "button", "Preview", 0, 0, 50, 13, ""),
        (1611, "text", "Draws the sample advert and notice in @DCCore-preview with the colours chosen here, "
                       "before they are saved.", 56, 0, 266, 16, ""),
    ),
    "mircwin": (
        (1615, "text", "What @DCCore shows and in which colours, its side panel, font and background, and "
                       "the bot's nick and pairing are this mIRC's own options, kept in dccore.ini beside "
                       "the script.", 0, 0, 322, 24, ""),
        (1616, "button", "@DCCore window options...", 0, 28, 100, 13, ""),
    ),
}
# The heights the layout reserves for each (the lowest control's bottom, plus a gap).
WIDGET_H = {name: max(y + (12 if kind == "combo" else h) for _i, kind, _t, _x, y, _w, h, _s in parts) + 4
            for name, parts in WIDGETS.items()}
# The widgets that talk to the bot (disabled with the settings when the bot
# cannot be edited); "mircwin" is this mIRC's own.
LOCAL_WIDGETS = ("mircwin",)
# The command each structured page asks with, the first time it is shown.
WIDGET_ASKS = {"onconnect": "onconnect", "served": "served", "bantimed": "banlist", "banperm": "banlist"}

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

    def item_height(self, item, columns):
        width = self.column_width(columns)
        if isinstance(item, dict) and "note" in item:
            return wrapped_lines(item["note"], width) * LINE_DBU + 3
        if isinstance(item, dict) and "widget" in item:
            return WIDGET_H[item["widget"]]
        if isinstance(item, dict) and "local" in item:
            return CHECK_PITCH
        key = item["key"] if isinstance(item, dict) else item
        kind = self.kind_of(key)
        if kind == "bool":
            return CHECK_PITCH
        label_w = width - CONTROL_W[columns] - LABEL_GAP
        lines = wrapped_lines(self.label_of(item), label_w)
        pitch = COMBO_PITCH if kind in ("choice", "tri", "colour") else ROW_PITCH
        return max(pitch, lines * LINE_DBU + 4)

    def column_width(self, columns):
        return CONTENT_W if columns == 1 else (CONTENT_W - COLUMN_GAP) // 2

    def section_height(self, section, columns, first):
        header, items = section
        height = 0
        if header:
            height += (0 if first else SECTION_GAP) + HEADER_PITCH
        return height + sum(self.item_height(item, columns) for item in items)

    # -- a page ----------------------------------------------------------------

    def place_page(self, name, sections):
        has_widget = any(isinstance(i, dict) and "widget" in i for _h, items in sections for i in items)
        one = sum(self.section_height(s, 1, n == 0) for n, s in enumerate(sections))
        if one <= CONTENT_H:
            self.place_column(sections, CONTENT_X, 1)
            return
        if has_widget:
            raise LayoutError("page %s is %d dbu tall and has a widget, so cannot take two columns" % (name, one))
        best = None
        for split in range(1, len(sections)):
            left = sum(self.section_height(s, 2, n == 0) for n, s in enumerate(sections[:split]))
            right = sum(self.section_height(s, 2, n == 0) for n, s in enumerate(sections[split:]))
            if best is None or max(left, right) < best[0]:
                best = (max(left, right), split)
        if best is None or best[0] > CONTENT_H:
            raise LayoutError("page %s does not fit, even in two columns" % name)
        split = best[1]
        self.place_column(sections[:split], CONTENT_X, 2)
        self.place_column(sections[split:], CONTENT_X + self.column_width(2) + COLUMN_GAP, 2)

    def place_column(self, sections, x, columns):
        width = self.column_width(columns)
        y = CONTENT_Y
        for number, (header, items) in enumerate(sections):
            if header:
                if number:
                    y += SECTION_GAP
                self.add("text", self.text_id(), x, y, width, LINE_DBU, header, role="header")
                y += HEADER_PITCH
            for item in items:
                self.place_item(item, x, y, width, columns)
                y += self.item_height(item, columns)

    def place_item(self, item, x, y, width, columns):
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
            for number, (cid, kind, text, dx, dy, w, h, style) in enumerate(WIDGETS[name]):
                self.add(kind, cid, x + dx, y + dy, w, h, text, style,
                         role="local" if name in LOCAL_WIDGETS else "widget",
                         key=keys[0] if keys and number == 0 else "")
            for key in item.get("keys", ()):
                self.keys.append((key, WIDGETS[name][0][0], "chanlist", 1, 0))
                self.labels[key] = self.meta["labels"].get(key, key)
            return
        if isinstance(item, dict) and "local" in item:
            base = self.item_ids()
            self.check_fits(item["label"], width)
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
            self.check_fits(label, width)
            self.add("check", base + 1, x, y, width, CHECK_H, label, role="value", key=key)
            self.keys.append((key, base + 1, kind, factor, base + 1))
            self.labels[key] = label
            return
        control_w = CONTROL_W[columns]
        label_w = width - control_w - LABEL_GAP
        lines = wrapped_lines(label, label_w)
        self.add("text", base, x, y + 2, label_w, lines * LINE_DBU, label, role="label", key=key)
        cx = x + label_w + LABEL_GAP
        if kind in ("choice", "tri"):
            self.add("combo", base + 1, cx, y, control_w, 80, style="drop", role="value", key=key)
        elif kind == "colour":
            half = (control_w - 4) // 2
            self.add("combo", base + 1, cx, y, half, 120, style="drop", role="value", key=key)
            self.add("combo", base + 2, cx + half + 4, y, half, 120, style="drop", role="value", key=key)
        else:
            browse = (key in self.layout.FOLDER_KEYS and "dir") or (key in self.layout.FILE_KEYS and "file")
            edit_w = control_w - (BROWSE_W + 2 if browse else 0)
            self.add("edit", base + 1, cx, y, edit_w, EDIT_H, style="autohs", role="value", key=key)
            if browse:
                self.add("button", base + 2, cx + edit_w + 2, y - 1, BROWSE_W, EDIT_H + 1, "...",
                         role="browse", key=key)
                self.browse.append((base + 2, browse, key))
        self.keys.append((key, base + 1, kind, factor, base))
        self.labels[key] = label

    def check_fits(self, label, width):
        """A check box does not wrap: its label is cut off instead."""
        if text_px(label) + CHECK_BOX_PX > width * PX_PER_DBU:
            raise LayoutError("the check %r does not fit in %d dbu" % (label, width))

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
           '  title "DCCore - Settings"',
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
    data.append(("pages", str(len(builder.pages))))
    for number, _group, name in builder.pages:
        data.append(("p.%d" % number, hash_text(name)))
        ids = [c["id"] for c in builder.controls if c["page"] == number]
        parts = chunks(ids, CHUNK_IDS)
        data.append(("p.%d.n" % number, str(len(parts))))
        for index, part in enumerate(parts, start=1):
            data.append(("p.%d.%d" % (number, index), ",".join(str(i) for i in part)))
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
