"""Reading an operator's accumulated history out of an OmenServe install.

The single biggest barrier to trying DCCore is not features - it is abandoning
years of totals. That barrier turns out to be three numbers wide.

WHERE THE NUMBERS ACTUALLY LIVE

Not in an OmenServe file. OmenServe keeps nothing of its own: the counters are
mIRC's persistent %variables, in `scripts/vars.ini`, written by whichever
add-ons the operator runs. Mapped from a live install (#69):

    %SD*, %OS.*   OmenServe core        the speed record, list counts
    %OSL.*        the OS-Limits add-on  per-day file counts
    %mx.*         mxrarserver           files and bytes sent

That is the important caveat and the reason nothing here is required: the
totals come from ADD-ONS, not from OmenServe itself. Two operators running
different ones keep their history in different variables, and one running
neither has none to import. A variable that is not there produces an absent
field, never a zero - writing a zero over a real total would be the worst
thing this feature could do.

vars.ini's format is one numbered key per variable, name and value sharing the
value: `n7=%mx.rarsent 45902`.

WHY THE PARSING IS HERE AND NOT IN THE BROWSER

The dashboard reads the file in the page and sends only the lines whose
variable is named below, so an operator's other ~268 variables - their nicks,
channels, paths and add-on settings - never cross the network. That part has
to be in JavaScript, because that is where the file is.

The parsing does not. This repository never executes JavaScript in a test:
tests/test_web_assets.py checks that app.js *parses*, explicitly "not what the
script does", there is no node in CI, and none of the web tests run any of it.
A parser handling hand-edited text, whose output is written to cumulative
totals with no undo, is the last thing that should live where it cannot be
tested. So the page filters and this parses.

Nothing here imports anything from this repository, deliberately: webserver.py
may only pull in six modules (tests/test_import_graph.py) and this is not one
of them, so its handler imports this lazily, the way it already does `list`.
"""

import re


class Field(object):
    """One variable worth reading out of vars.ini."""

    __slots__ = ("variable", "target", "label", "unit")

    def __init__(self, variable, target, label, unit=""):
        self.variable = variable
        self.target = target        # None = shown to the operator, not imported
        self.label = label
        self.unit = unit


# Everything the dashboard reads. `target` names where it lands in DCCore, or
# None for the ones shown in the preview but deliberately left behind.
#
# The day buckets are the None ones, and the reason is that %OSL.Today reads
# "Friday" and %mx.rarday reads "Wednesday" - a weekday NAME, not a date. So
# nothing can tell whether "today" means today or six days ago, and
# db._rotate_day_unlocked() would rotate an imported "today" into "yesterday"
# and then out of existence on the first write of a new day. They are still
# shown, because an operator can see them in their own file and deserves to be
# told why they are not coming across rather than left to notice.
#
# %mx.rarsent and %sdmpxsent both land in total_files, and are SUMMED rather
# than one replacing the other (#414): %mx.rarsent is mxrarserver's count of
# packed (RAR) sends, %sdmpxsent is OmenServe's own count of plain sends, and
# an install running both add-ons has both as genuinely separate real counts.
# Before this, %sdmpxsent was not read at all - on a plain-sends-only install
# that dropped the large majority of real sends from the import entirely.
#
# Bytes has no equivalent second field: OmenServe records no byte total for
# plain sends at all, so %mx.rartsent (also packed-only) is the only bytes
# counter there is to import. read_install() adds a permanent note about that
# gap rather than pretending total_bytes covers everything total_files does.
FIELDS = (
    Field("%mx.rarsent", "total_files", "Files sent (packed)", "files"),
    Field("%sdmpxsent", "total_files", "Files sent (plain)", "files"),
    Field("%mx.rartsent", "total_bytes", "Bytes sent", "bytes"),
    Field("%SDmaxspeed", "speed_record", "Speed record", "bytes/s"),

    Field("%OSL.SentToday", None, "Files today", "files"),
    Field("%OSL.SentYesterday", None, "Files yesterday", "files"),
    Field("%mx.rardsent", None, "Bytes today", "bytes"),
    Field("%mx.rarysent", None, "Bytes yesterday", "bytes"),
    Field("%os.totalsize", None, "Library size", "bytes"),
    Field("%sdlistsent", None, "Lists sent", "lists"),
)


# mIRC variable names are case-insensitive, and a real vars.ini is inconsistent
# about it in practice (%OS.* and %os.* both appear in the same file).
_BY_NAME = {field.variable.lower(): field for field in FIELDS}

# `n12=%Name value`. The number after "n" is mIRC's own ordering and means
# nothing to us.
_LINE_RE = re.compile(r"^\s*n\d+\s*=\s*(%\S+)\s*(.*?)\s*$", re.IGNORECASE)


def variable_names():
    """Every variable the dashboard should keep when it filters the file.

    Served to the page rather than written into app.js, so the filter and this
    parser cannot drift: a field added below is filtered through without the
    JavaScript changing at all.
    """
    return tuple(field.variable for field in FIELDS)


def parse_vars(text):
    """{variable name: raw value} for the variables named in FIELDS.

    Takes either a whole vars.ini or the handful of lines the dashboard kept -
    the format of a line is the same either way, and accepting both means the
    paste-it-in fallback needs no second code path.

    Unknown variables are ignored rather than rejected: this is handed a file
    an operator may have hand-edited, from an add-on mix nobody has seen, and
    the useful behaviour is to take what is recognised.
    """
    found = {}
    for line in str(text or "").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        field = _BY_NAME.get(match.group(1).lower())
        if field is not None:
            found[field.variable] = match.group(2)
    return found


def _as_int(raw):
    """A whole number from a vars.ini value, or None.

    %SDmaxspeed is "<bytes>,<nick>" - the rate and who set it. Only the rate is
    wanted, and splitting on the comma is why this does not simply int().

    Floats round rather than fail: %OSL.MbToday is "34.56", and a preview row
    reading 34 is more useful than one reading "could not read".
    """
    # split(",")[0] already removed everything from the first comma on, which
    # is what %SDmaxspeed's "<bytes>,<nick>" needs - so a later replace(",",
    # "") could never see one and was dead. That mattered: a value written
    # "45,902" by a thousands-separating add-on truncated to 45, silently and
    # plausibly. Separators are stripped BEFORE the split, so both shapes work.
    text = str(raw or "")
    if "," in text:
        head, _sep, tail = text.partition(",")
        # A comma followed by a NICK is the speed record's shape; a comma
        # inside digits is a thousands separator. Told apart by what follows.
        if tail.strip()[:1].isdigit():
            text = text.replace(",", "")
        else:
            text = head
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return None
    try:
        return int(round(number))
    # OverflowError, not ValueError, which is why this escaped: "1e999" parses
    # as a float perfectly well and becomes inf, and int(round(inf)) raises -
    # reaching the preview route as a 500 on a file somebody hand-edited. NaN
    # arrives the same way with a ValueError of its own. Both mean "not a
    # number I can use", which is what None already says here.
    except (OverflowError, ValueError):
        return None


def read_install(text):
    """What an operator's vars.ini offers, ready to preview.

    Returns {"values": {...}, "rows": [...], "notes": [...]}:

      values  what would actually be written, by DCCore's own field names.
              A variable that was absent or unreadable is NOT in here, so a
              caller cannot mistake "missing" for "zero".
      rows    every field in FIELDS with what was found, for the preview -
              including the ones marked as not imported.
      notes   anything the operator should be told before confirming.
    """
    found = parse_vars(text)

    values = {}
    rows = []
    notes = []

    for field in FIELDS:
        raw = found.get(field.variable)
        number = _as_int(raw) if raw is not None else None

        if raw is not None and number is None:
            notes.append(f"{field.variable} is present but not a number "
                         f"({raw!r}) - skipped.")

        rows.append({
            "variable": field.variable,
            "label": field.label,
            "unit": field.unit,
            "value": number,
            # Shown either way; "imported" is the narrower question. A zero
            # appears in the preview with the others and is marked as not
            # imported, because the operator can see it in their own file and
            # a row that quietly vanished would read as a parse failure.
            "imported": bool(field.target) and bool(number),
        })

        # A PRESENT ZERO IS NOT A FIGURE TO IMPORT. `number is not None` let
        # one through, and the whole point of this feature is to carry history
        # ACROSS - a zero carries none and can only destroy what is already
        # here. The note below even promised the opposite ("the total will
        # stay as it is") while a present `%mx.rartsent 0` silently replaced
        # a real one.
        #
        # This is the same rule the module already states for an ABSENT
        # variable, applied to the value that means the same thing: an
        # OmenServe install that has sent nothing has nothing to give.
        #
        # ADDS rather than assigns, because two Fields can share a target
        # (%mx.rarsent and %sdmpxsent both feed total_files) and an install
        # running both add-ons has both as genuinely separate real counts -
        # the second one found must not overwrite the first.
        if field.target and number:
            values[field.target] = values.get(field.target, 0) + number
        elif field.target and number == 0:
            notes.append(f"{field.variable} is present but zero - skipped, "
                         f"so your own {field.label.lower()} is left alone.")

    if not values:
        notes.append("Nothing recognisable was found. This reads mIRC's "
                     "scripts/vars.ini - the counters come from the OmenServe "
                     "add-ons (mxrarserver, OS-Limits), so an install without "
                     "them has no history to bring across.")

    # Permanent, not conditional on a mismatch: OmenServe itself keeps no byte
    # total for plain sends at all, so %mx.rartsent - packed sends only - is
    # the only bytes counter there is to import, ever. An operator running
    # both add-ons would otherwise read total_bytes as a full match for
    # total_files (which DOES cover both kinds) and never learn it does not.
    if "total_bytes" in values:
        notes.append("Bytes sent covers packed (RAR) sends only - OmenServe "
                     "keeps no byte total for plain sends, so this number "
                     "will not include them.")

    # The shape a mis-parse takes, and worth saying rather than silently
    # importing. Not refused: an unusual add-on mix could genuinely produce it.
    files = values.get("total_files")
    total_bytes = values.get("total_bytes")
    if files and not total_bytes:
        notes.append("Files sent was found but bytes sent was not. The total "
                     "size will stay as it is.")
    elif total_bytes and not files:
        notes.append("Bytes sent was found but the file count was not. The "
                     "file total will stay as it is.")

    return {"values": values, "rows": rows, "notes": notes}
