"""What another file server's reply to our request means (#926).

When DCCore fetches from another bot it sends "!<bot> <file>" (or "@<bot>" for
a list) and waits for a DCC SEND. Until now the only reply it understood was
"!rar is disabled"; everything else - "you are number 12 in my queue", "I
don't have that file", "my queue is full" - was ignored, and the request sat
"offered" for FETCH_OFFER_TIMEOUT and failed as "no response". A queued file
that arrived an hour later was then refused as unsolicited.

classify() reads one private NOTICE or message from the bot we asked and
answers with one of four outcomes:

    "queued"     accepted into their queue (position when they say it)
    "duplicate"  already in their queue - keep waiting
    "refused"    will never come: no such file, deleted, "servers only"
    "busy"       not now: queue full, maxed out, rebuilding

or None for anything else, which changes nothing.

THE PHRASES are the ones Autoget 7.40 (eMpTy, 2005) - the download manager
that went with OmeNServE - recognised from the servers of its day, read out of
its script: OmeNServE 1.31 to 2.x, SDFind, SpR Jukebox (English and French)
and BWI; plus DCCore's own wording (announce.py). Each is a sequence of words
that must appear IN ORDER, anything between - the way mIRC's wildcard events
matched them - after colour and formatting codes are removed. Order is
checked, not just presence, so a line that merely contains the same words
scrambled does not match.

A reply only ever acts on a request we sent to THAT bot (dcc_fetch matches
the sender), so nobody else can move or fail our requests; and a busy or
refused verdict on a bot that has nothing outstanding from us does nothing.
"""

import re

# Colour (\x03 with its optional fg,bg digits), bold, reset, reverse, italic,
# underline, strikethrough and monospace.
_FORMATTING = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x16\x1d\x1f\x1e\x11]")


def plain(text):
    """The text with IRC formatting removed and whitespace collapsed."""
    return " ".join(_FORMATTING.sub("", str(text or "")).split())


def _words(pattern):
    """A compiled matcher for words in order, anything between them."""
    parts = [re.escape(word) for word in pattern.split()]
    return re.compile(r"(?<!\w)" + r".*?".join(parts), re.IGNORECASE | re.DOTALL)


# (outcome, words in order, where the queue position is, if anywhere)
# A position rule is a regex applied to the plain text; its group 1 is the
# number. Most specific first: "already in my queue ... position" must be
# tried before plain "already in my queue", and every "denied"/"sorry" line
# before any generic "queue" line.
_RULES = [
    # --- DCCore (announce.py) --------------------------------------------
    ("queued", "added to your personal queue at position", r"position\s*#?\s*(\d+)"),
    ("refused", "Error: File not found", None),
    ("refused", "Error: Invalid path", None),
    ("refused", "Error: Folder packing (!rar) is disabled", None),
    ("refused", "Error: This bot's music library is not configured", None),
    ("refused", "Error: That folder name is served by more than one", None),
    ("busy", "Error: Busy looking up other files", None),
    ("busy", "Error: The server's global queue is full", None),
    ("busy", "Error: You have reached your personal queue limit", None),
    ("busy", "MasterList is currently rebuilding", None),
    ("busy", "The bot is reloading its configuration", None),

    # --- OmeNServE 1.32 - 2.x ---------------------------------------------
    ("duplicate", "Request Denied You Already Have In My Queue Position OmenServE",
     r"position\s*:?\s*#?\s*(\d+)"),
    ("duplicate", "Request Denied You Already Have In My Queue OmenServE", None),
    ("refused", "Request Denied Check Your Spelling OmenServE", None),
    ("refused", "Request Denied You need sharing OmenServE", None),
    ("refused", "Removed From Queue File Has Been Moved Or Deleted OmeNServE", None),
    ("queued", "Request Accepted Position: OmenServE", r"position\s*:\s*#?\s*(\d+)"),

    # --- OmeN 1.31 (its logo characters around the name vary; the words do not)
    ("duplicate", "Reason: You Already Have In My Queue", None),
    ("refused", "Request Denied Reason: Check Your Spelling", None),
    ("queued", "OmeN Request Accepted Position:", r"position\s*:\s*#?\s*(\d+)"),

    # --- SDFind ---------------------------------------------------------------
    ("duplicate", "You already have in my que, has NOT been added to my que.", None),
    ("duplicate", "You already have in my que. Type for more info.", None),
    ("refused", "I don't have Please check your spelling or get my newest list", None),
    ("queued", "I have added in my que. this makes you are allowed", None),

    # --- BWI ------------------------------------------------------------------
    ("duplicate", "You already have in my queue", None),
    ("refused", "Sorry, but is not found", None),
    ("queued", "I have added as in my queue. This is file of allowed in your queue.", None),

    # --- SpR Jukebox, English and French --------------------------------------
    ("duplicate", "You are already in my que list with", None),
    # French written with escapes: the source stays ASCII (test_source_language).
    ("duplicate", "Tu es d\u00e9j\u00e0 dans ma liste d'attente avec", None),
    ("refused", "You got the file name wrong", None),
    ("busy", "I am totally maxed out even in que list. Try it later.", None),
    ("busy", "Available Que are Taken", None),
    ("busy", "Place(s) Disponible(s) Occup\u00e9e(s)", None),
    ("queued", "You are in que now with in rank.", r"rank\.?\s*#?\s*(\d+)|with\s+#?\s*(\d+)\s+in\s+rank"),
    ("queued", "Tu es Maintenant dans liste d'attente comme \u00e9tant num\u00e9ro",
     "num\u00e9ro" r"\s*#?\s*(\d+)"),
]

_COMPILED = [(outcome, _words(words), re.compile(pos, re.IGNORECASE) if pos else None)
             for outcome, words, pos in _RULES]


class Reply:
    __slots__ = ("outcome", "position", "text")

    def __init__(self, outcome, position, text):
        self.outcome = outcome
        self.position = position
        self.text = text

    def __repr__(self):
        return f"Reply({self.outcome!r}, {self.position!r})"


def classify(text):
    """A Reply for a message another file server sends about our request, or
    None for anything that is not one."""
    line = plain(text)
    if not line:
        return None
    for outcome, matcher, position_rule in _COMPILED:
        if not matcher.search(line):
            continue
        position = None
        if position_rule is not None:
            found = position_rule.search(line)
            if found:
                digits = next((g for g in found.groups() if g), None)
                position = int(digits) if digits else None
        return Reply(outcome, position, line)
    return None
