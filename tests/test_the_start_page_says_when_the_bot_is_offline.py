"""#1396: the control panel's start page says when the bot is offline.

With the bot stopped, the footer said "Not connected to the bot" while the
start page said "connected (structured console)" and showed the stopped
bot's start time and the old session's figures: the page read `state` from
dccore.live, which still said `in` with no chat open.

mIRC cannot run here. Most of the script's tests read its statements; these
RUN the aliases this fix is made of - dccore.linked, dccore.unstale, the
start page's fill, frame, row and ink, dccore.sw.start and sw.lost,
dccore.plain and dccore.theme.dcxtexts - statement by statement, with a
small evaluator below, against fake tables and fake chat windows, and the
start page's real layout (the generated dccore.sw.data). Identifiers the
evaluator does not know are refused loudly, not guessed: a later change
that uses a new one fails here with its name, and the fix is to teach the
evaluator that identifier (or give it a stub, below), not to loosen a test.
Commands that are not run are recorded, with their arguments evaluated, so
a test can say what was and was not done (no timer, no redial).

What is held:

- linked is "state is in AND a chat to the bot is open", and nothing else;
- not linked, Up since, Today and Library read `-`, Sending has no counts
  and says (bot offline); Nick and Network stay;
- the Connection row is green "connected (...)", amber "connecting: <step>"
  for each dialling step, red "offline" otherwise - coloured before its
  text is written, and the colour kept for dccore.theme.dcxtexts, which
  sets it again after giving every value the theme's colour;
- a stale `in` is reset as on CHATCLOSE (the same keys) and the titles and
  the panel redrawn, with no timer, no redial and no message, and only when
  stale; dccore.sw.start does it before its footer test, so the footer and
  the page agree;
- the page is refilled when the link drops or comes up (sw.lost, after
  sw.start; dccore.plain for a bot without HELLO), and on every step of a
  dial: dccore.title sets the fill's timer while the panel is open, even
  with @DCCore closed - on a timer, so the two never run each other at
  once, and the one way back from the fill to the title (its library ask
  with no chat) goes round once.
"""

import fnmatch
import io
import os
import re
import sys
import unittest

from tests import support  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_the_mirc_settings_window import block, code, generated_data  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
try:
    import roll_changelog  # noqa: E402
finally:
    sys.path.remove(os.path.join(REPO_ROOT, "scripts"))

# Placeholders, nobody's real nick or network.
BOT = "ExampleBotQ"
OTHER = "SomeoneElseZ"
NETWORK = "ExampleNetQ"
NOW = 1_800_000_000
STARTED = NOW - 2 * 86400
DOT = chr(9679)
DIALLING = ("opening", "waiting", "banner", "auth", "password")


# ---- the evaluator ----------------------------------------------------------

class Unknown(AssertionError):
    """An identifier the evaluator was not taught."""


class _Return(Exception):
    def __init__(self, value):
        super().__init__(value)
        self.value = value


_OPS = ("==", "===", "!=", "<", ">", "<=", ">=")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def truthy(value):
    return value not in ("", "0", "$false")


def is_number(value):
    return bool(_NUMBER.fullmatch(value))


def matching_paren(text, start):
    depth = 0
    for j in range(start, len(text)):
        depth += text[j] == "("
        depth -= text[j] == ")"
        if depth == 0:
            return j
    raise AssertionError("unbalanced: " + text)


def split_words(text):
    """Space-separated, but an identifier's (...) is one word. A "(" that
    starts a word opens nothing: it is text, or a $calc group."""
    words, current, depth = [], [], 0
    for ch in text:
        if ch == " " and depth == 0:
            if current:
                words.append("".join(current))
                current = []
            continue
        if ch == "(" and current:
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        current.append(ch)
    if current:
        words.append("".join(current))
    return words


def split_top(text, sep):
    """`text` split on `sep` outside any (...)."""
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        current.append(ch)
    parts.append("".join(current))
    return parts


def split_logic(text):
    """[parts], [joins] of a condition, at its top level."""
    parts, joins, depth, start, i = [], [], 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0 and text.startswith((" && ", " || "), i):
            parts.append(text[start:i])
            joins.append(text[i + 1:i + 3])
            i += 4
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts], joins


def parse(text, pos=0):
    """(nodes, pos) of a block's code: ("cmd", text), ("while", cond, body),
    ("if", [(cond, body), ...], else_body)."""
    nodes = []
    while True:
        while pos < len(text) and (text[pos] in " \t\n" or text.startswith("| ", pos)):
            pos += 1
        if pos >= len(text):
            return nodes, pos
        if text[pos] == "}":
            return nodes, pos + 1
        keyword = next((k for k in ("elseif ", "else ", "if ", "while ") if text.startswith(k, pos)), None)
        if keyword:
            j, depth = pos + len(keyword), 0
            while not (text[j] == "{" and depth == 0):
                depth += text[j] == "("
                depth -= text[j] == ")"
                j += 1
            cond = text[pos + len(keyword):j].strip()
            body, pos = parse(text, j + 1)
            if keyword == "if ":
                nodes.append(["if", [(cond, body)], None])
            elif keyword == "while ":
                nodes.append(["while", cond, body])
            elif keyword == "elseif ":
                assert nodes and nodes[-1][0] == "if", "elseif without if"
                nodes[-1][1].append((cond, body))
            else:
                assert nodes and nodes[-1][0] == "if" and not cond, "else without if"
                nodes[-1][2] = body
            continue
        j, depth = pos, 0
        while j < len(text):
            ch = text[j]
            if ch == "\n" or (depth == 0 and (ch == "}" or text.startswith(" | ", j))):
                break
            depth = max(0, depth + (ch == "(") - (ch == ")"))
            j += 1
        nodes.append(["cmd", text[pos:j].strip()])
        pos = j


_SOURCE = {}


def source():
    if "code" not in _SOURCE:
        _SOURCE["code"] = code()
    return _SOURCE["code"]


def body_of(header):
    """The parsed statements of the block that `header` opens."""
    key = ("body", header)
    if key not in _SOURCE:
        text = block(header, source())
        inner = text[text.index("{") + 1:text.rindex("}")]
        _SOURCE[key] = parse(inner)[0]
    return _SOURCE[key]


class Mirc:
    """Just enough mIRC to run the aliases this fix is made of."""

    def __init__(self, real=(), stubs=None):
        self.tables = {"dccore": {}, "dccore.live": {}, "dccore.sws": {}, "dccore.swm": {}}
        self.chats = set()
        self.dialogs = set()
        self.windows = set()
        self.network = NETWORK
        self.server = "irc.example.invalid"
        self.real = set(real) | {"dccore.st", "dccore.bot", "dccore.linked", "dccore.sw.m", "dccore.sw.s",
                                 "dccore.opt", "dccore.win", "dccore.dot"}
        self.stubs = dict(STUBS)
        self.stubs.update(stubs or {})
        self.calls = []

    # tables
    def table(self, name):
        return self.tables.setdefault(name.lower(), {})

    def live(self, **items):
        for key, value in items.items():
            self.table("dccore.live")[key.replace("_", ".").lower()] = str(value)

    # running
    def call(self, name, args):
        try:
            self.run(body_of("alias %s {" % name), {"args": list(args), "vars": {}})
        except _Return as done:
            return done.value
        return ""

    def run(self, nodes, frame):
        for node in nodes:
            if node[0] == "cmd":
                self.command(node[1], frame)
            elif node[0] == "while":
                for _ in range(1000):
                    if not self.cond(node[1], frame):
                        break
                    self.run(node[2], frame)
                else:
                    raise AssertionError("a loop that does not end: " + node[1])
            else:
                for cond, body in node[1]:
                    if self.cond(cond, frame):
                        self.run(body, frame)
                        break
                else:
                    if node[2]:
                        self.run(node[2], frame)

    def command(self, text, frame):
        head, _, rest = text.partition(" ")
        if head == "return":
            raise _Return(self.value(rest, frame))
        if head == "var":
            for part in split_top(rest, ","):
                name, _, value = part.strip().partition(" = ")
                frame["vars"][name.strip()] = self.value(value, frame) if value else ""
            return
        if head.startswith("%") and rest.startswith("= "):
            frame["vars"][head] = self.value(rest[2:], frame)
            return
        if head == "inc":
            name, _, by = rest.partition(" ")
            now = frame["vars"].get(name, "") or "0"
            frame["vars"][name] = str(int(now) + int(self.value(by, frame) or 1))
            return
        if head.startswith("."):
            self.calls.append(("timer", text[1:]))
            return
        evaluated = self.value(rest, frame)
        if head == "hadd":
            table, key, value = (evaluated.split(" ", 2) + [""])[:3]
            self.table(table)[key.lower()] = value
        elif head == "hdel":
            words = evaluated.split(" ")
            if words[0] == "-w":
                found = self.table(words[1])
                for key in [k for k in found if fnmatch.fnmatchcase(k, words[2].lower())]:
                    del found[key]
            else:
                self.table(words[0]).pop(words[1].lower(), None)
        elif head in self.real:
            self.calls.append((head, evaluated))
            self.call(head, evaluated.split())
        else:
            self.calls.append((head, evaluated))

    def value(self, text, frame):
        out, glue = [], False
        for word in split_words(text.strip()):
            if word == "$+":
                glue = True
                continue
            got = self.word(word, frame)
            if glue and out:
                out[-1] += got
            else:
                out.append(got)
            glue = False
        return " ".join(part for part in out if part != "")

    def word(self, word, frame):
        opened = len(word) - len(word.lstrip("("))
        inner = word[opened:]
        if inner.startswith("%"):
            name = re.match(r"%[^\s()]+", inner).group(0)
            tail = inner[len(name):]
            if tail.strip(")"):
                raise Unknown("a variable followed by text: " + word)
            return "(" * opened + frame["vars"].get(name, "") + tail
        if inner.startswith("$") and len(inner) > 1:
            numbered = re.fullmatch(r"\$(\d+)(-?)(\)*)", inner)
            if numbered:
                args = frame["args"]
                index = int(numbered.group(1)) - 1
                got = " ".join(args[index:]) if numbered.group(2) else (args[index] if index < len(args) else "")
                return "(" * opened + got + numbered.group(3)
            named = re.match(r"\$(\+|[A-Za-z0-9_.]+)", inner)
            if not named:
                raise Unknown(word)
            name, end, raw = named.group(1), named.end(), None
            if end < len(inner) and inner[end] == "(":
                close = matching_paren(inner, end)
                raw, end = inner[end + 1:close], close + 1
            tail = inner[end:]
            if tail.strip(")"):
                raise Unknown("an identifier followed by text: " + word)
            return "(" * opened + self.identifier(name, raw, frame) + tail
        return word

    def identifier(self, name, raw, frame):
        args = split_top(raw, ",") if raw is not None else []
        if name == "iif":
            yes = args[1] if len(args) > 1 else ""
            no = args[2] if len(args) > 2 else ""
            return self.value(yes if self.cond(args[0], frame) else no, frame)
        if name in ("null", "true", "false") and raw is None:
            return {"null": "", "true": "$true", "false": "$false"}[name]
        if name == "ctime":
            return str(NOW)
        if name == "network":
            return self.network
        if name == "server":
            return self.server
        values = [self.value(a, frame) for a in args]
        if name == "+":
            return "".join(values)
        if name == "chr":
            return chr(int(values[0]))
        if name == "istok":
            tokens = [t.lower() for t in values[0].split(chr(int(values[2]))) if t]
            return "$true" if values[1].lower() in tokens else "$false"
        if name == "gettok":
            tokens = [t for t in values[0].split(chr(int(values[2]))) if t]
            if values[1].endswith("-"):
                return chr(int(values[2])).join(tokens[int(values[1][:-1]) - 1:])
            index = int(values[1]) - 1
            return tokens[index] if 0 <= index < len(tokens) else ""
        if name == "calc":
            if not re.fullmatch(r"[\d\s+\-*/().]+", values[0]):
                raise Unknown("$calc of " + values[0])
            result = eval(values[0], {"__builtins__": {}})  # digits and operators only
            return str(int(result)) if float(result).is_integer() else str(result)
        if name == "int":
            return str(int(float(values[0] or 0)))
        if name == "rgb":
            r, g, b = (int(v) for v in values)
            return str(r + g * 256 + b * 65536)
        if name == "chat":
            return values[0] if values[0].lower() in {c.lower() for c in self.chats} else ""
        if name == "dialog":
            return values[0] if values[0] in self.dialogs else ""
        if name == "window":
            return values[0] if values[0] in self.windows else ""
        if name == "hget":
            found = self.tables.get(values[0].lower())
            if len(values) == 1:
                return values[0] if found is not None else ""
            return (found or {}).get(values[1].lower(), "")
        if name == "hfind":
            keys = sorted(k for k in self.table(values[0]) if fnmatch.fnmatchcase(k, values[1].lower()))
            index = int(values[2])
            return str(len(keys)) if index == 0 else (keys[index - 1] if index <= len(keys) else "")
        if name == "asctime":
            return "AT" + values[0]
        if name in self.real:
            return self.call(name, values)
        if name in self.stubs:
            return self.stubs[name](*values)
        raise Unknown("$" + name)

    def cond(self, text, frame):
        parts, joins = split_logic(text)
        # every part is evaluated, as mIRC evaluates every identifier in an if-line
        results = [self.part(p, frame) for p in parts]
        result = results[0]
        for join, later in zip(joins, results[1:]):
            result = (result and later) if join == "&&" else (result or later)
        return result

    def part(self, text, frame):
        text = text.strip()
        if text.startswith("(") and matching_paren(text, 0) == len(text) - 1:
            text = text[1:-1].strip()
        words = split_words(text)
        for k, word in enumerate(words[1:], 1):
            if word in _OPS:
                left = self.value(" ".join(words[:k]), frame)
                right = self.value(" ".join(words[k + 1:]), frame)
                return compare(left, word, right)
        if text.startswith("!"):
            return not truthy(self.value(text[1:], frame))
        return truthy(self.value(text, frame))

    # what the page shows
    def dcx(self):
        """[(id, flag, text)] of every DCX text and colour set, in order."""
        events = []
        for head, args in self.calls:
            if head == "dccore.dcx" and args.startswith("xdid dccore.set "):
                words = args.split(" ", 4)
                events.append((words[2], words[3], words[4] if len(words) > 4 else ""))
        return events

    def texts(self):
        shown = {}
        for ident, flag, text in self.dcx():
            if flag == "-t":
                shown[ident] = text
        return shown


def compare(left, op, right):
    if op in ("==", "!="):
        same = (float(left) == float(right)) if is_number(left) and is_number(right) else left.lower() == right.lower()
        return same if op == "==" else not same
    if op == "===":
        return left == right
    if not (is_number(left) and is_number(right)):
        return False
    a, b = float(left), float(right)
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


# The aliases the start page uses that this fix did not touch: a marker
# each, so a test can see which one a row came from.
STUBS = {
    "dccore.nbsp": lambda: "<nbsp>",
    "dccore.dur": lambda s: "<dur %s>" % s,
    "dccore.bytes": lambda n: "<bytes %s>" % n,
    "dccore.speed": lambda n: "<speed %s>" % n,
    "dccore.fit": lambda text, width: text,
    "dccore.rfit": lambda text, width: text,
    "dccore.sw.home.lists": lambda: "<lists>",
    "dccore.sw.home.lib": lambda what: "<lib %s>" % what,
    "dccore.rebuild.short": lambda: "<rebuild>",
    "dccore.sw.can": lambda part: "$false",
    "dccore.theme.dialogsdark": lambda: "$false",
    "dccore.theme.rgb": lambda which: "<theme %s>" % which,
    "dccore.waiting": lambda: "<waiting>",
}

PAGE = ("dccore.sw.home.fill", "dccore.sw.home.frame", "dccore.sw.home.row", "dccore.sw.home.ink")


# ---- the start page's layout, from the generated data -----------------------

def layout():
    """{frame title: {"title": its title's DCX id, "rows": [(key, value id)]}}."""
    data = generated_data()
    frames = {}
    for n in range(1, int(data["home.f.n"]) + 1):
        words = data["home.f.%d" % n].split(" ")
        rows = []
        for r in range(1, int(data["home.f.%d.n" % n]) + 1):
            row = data["home.f.%d.r.%d" % (n, r)].split(" ", 10)
            rows.append((row[10] if len(row) > 10 else "line %d" % r, row[1] if row[1] != "0" else row[0]))
        frames[" ".join(words[11:])] = {"title": words[1], "rows": rows}
    return frames


LAYOUT = layout()
CONNECTION = dict(LAYOUT["Bot"]["rows"])["Connection"]


def page(state=None, chat=True, mode="structured", dark=False, bot=BOT, **extra):
    """A Mirc whose start page has been filled once, from a session that
    left the given state and figures behind."""
    mirc = Mirc(real=PAGE, stubs={"dccore.theme.dialogsdark": lambda: "$true" if dark else "$false"})
    for key, value in generated_data().items():
        mirc.table("dccore.swm")[key.lower()] = value
    mirc.dialogs.add("dccore.set")
    mirc.table("dccore.sws").update({"dcx": "1", "side": "0"})
    if bot:
        mirc.table("dccore")["bot"] = bot
    if chat and bot:
        mirc.chats.add(bot)
    figures = dict(st_started=STARTED, st_used=1, st_slots=3, slot_1="PeerNickA 100 200 50 some file.bin",
                   st_sent=7, st_bytes=1000, st_qusers=2, st_qfiles=5, st_record=300, st_failed=1,
                   st_searches=4)
    figures.update(extra)
    if state is not None:
        figures["state"] = state
    if mode is not None:
        figures["mode"] = mode
    mirc.live(**figures)
    mirc.call("dccore.sw.home.fill", [])
    return mirc


def rows(mirc, frame):
    shown = mirc.texts()
    return {key: shown.get(ident) for key, ident in LAYOUT[frame]["rows"]}


def colour(value):
    n = int(value)
    return n & 255, (n >> 8) & 255, (n >> 16) & 255


def ink_of(mirc):
    """The last text colour set on the Connection row."""
    inks = [text for ident, flag, text in mirc.dcx() if ident == CONNECTION and flag == "-C"]
    return inks[-1] if inks else None


# ---- the tests ---------------------------------------------------------------

class OneTestForLinked(unittest.TestCase):

    def linked(self, state, chats=(BOT,), bot=BOT):
        mirc = Mirc()
        if bot:
            mirc.table("dccore")["bot"] = bot
        mirc.chats.update(chats)
        if state is not None:
            mirc.live(state=state)
        return mirc.call("dccore.linked", [])

    def test_logged_in_with_the_chat_open_is_linked(self):
        self.assertEqual(self.linked("in"), "$true")

    def test_logged_in_with_no_chat_is_not(self):
        """The issue: a state the stopped bot's session left behind."""
        self.assertEqual(self.linked("in", chats=()), "$false")

    def test_a_chat_to_another_nick_is_not_the_bots(self):
        self.assertEqual(self.linked("in", chats=(OTHER,)), "$false")

    def test_a_chat_open_while_dialling_or_after_is_not_linked(self):
        for state in DIALLING + ("closed", "taken", None):
            with self.subTest(state=state):
                self.assertEqual(self.linked(state), "$false")

    def test_with_no_bot_nick_set_nothing_is_linked(self):
        self.assertEqual(self.linked("in", chats=(OTHER,), bot=None), "$false")


class OfflineNothingFromTheBotIsCurrent(unittest.TestCase):
    """state=in, no chat: the stopped bot's figures are all still there."""

    def setUp(self):
        self.mirc = page("in", chat=False)

    def test_the_connection_row_says_offline_in_red(self):
        self.assertEqual(rows(self.mirc, "Bot")["Connection"], DOT + " offline")
        r, g, b = colour(ink_of(self.mirc).split()[-1])
        self.assertGreater(r, g)
        self.assertGreater(r, b)

    def test_up_since_is_a_dash_and_nick_and_network_stay(self):
        bot = rows(self.mirc, "Bot")
        self.assertEqual(bot["Up since"], "-")
        self.assertEqual(bot["Nick"], BOT)
        self.assertEqual(bot["Network"], NETWORK)

    def test_today_and_library_are_dashes_on_every_row(self):
        for frame in ("Today", "Library"):
            with self.subTest(frame=frame):
                shown = rows(self.mirc, frame)
                self.assertEqual(set(shown.values()), {"-"}, shown)
                self.assertEqual(len(shown), len(LAYOUT[frame]["rows"]))

    def test_sending_says_bot_offline_and_has_no_counts(self):
        self.assertEqual(self.mirc.texts()[LAYOUT["Sending"]["title"]], "Sending")
        lines = [text for _key, text in sorted(rows(self.mirc, "Sending").items())]
        self.assertEqual(lines[0], "(bot offline)")
        self.assertEqual(set(lines[1:]), {"<nbsp>"})

    def test_nothing_on_the_page_comes_from_the_old_session(self):
        shown = " ".join(self.mirc.texts().values())
        for old in ("AT%d" % STARTED, "PeerNickA", "<bytes", "<speed", "<lists>", "<lib", "1/3", "free)"):
            self.assertNotIn(old, shown)


class OnlineTheFiguresAreShown(unittest.TestCase):

    def setUp(self):
        self.mirc = page("in", chat=True)

    def test_the_connection_row_says_connected_in_green(self):
        self.assertEqual(rows(self.mirc, "Bot")["Connection"], DOT + " connected (structured console)")
        r, g, b = colour(ink_of(self.mirc).split()[-1])
        self.assertGreater(g, r)
        self.assertGreater(g, b)

    def test_a_plain_console_is_named(self):
        self.assertEqual(rows(page("in", mode="plain"), "Bot")["Connection"], DOT + " connected (plain console)")

    def test_the_figures_are_the_sessions(self):
        self.assertEqual(rows(self.mirc, "Bot")["Up since"], "AT%d (<dur %d>)" % (STARTED, NOW - STARTED))
        today = rows(self.mirc, "Today")
        self.assertEqual(today["Sent"], "7 files / <bytes 1000>")
        self.assertEqual(today["Searches"], "4")
        self.assertEqual(rows(self.mirc, "Library")["Lists"], "<lists>")
        self.assertEqual(rows(self.mirc, "Library")["Files"], "<lib files>")

    def test_sending_has_its_counts_and_its_lines(self):
        self.assertEqual(self.mirc.texts()[LAYOUT["Sending"]["title"]], "Sending 1/3")
        lines = [text for _key, text in sorted(rows(self.mirc, "Sending").items())]
        self.assertTrue(lines[0].startswith("> PeerNickA "), lines[0])
        self.assertEqual(lines[1], "(2 free)")


class DiallingIsAmberAndNamed(unittest.TestCase):

    def test_each_step(self):
        for state in DIALLING:
            for chat in (True, False):
                with self.subTest(state=state, chat=chat):
                    mirc = page(state, chat=chat)
                    self.assertEqual(rows(mirc, "Bot")["Connection"], DOT + " connecting: " + state)
                    r, g, b = colour(ink_of(mirc).split()[-1])
                    self.assertGreater(r, b)
                    self.assertGreater(g, b)
                    self.assertGreaterEqual(r, g)
                    # dialling is not linked: nothing current either
                    self.assertEqual(rows(mirc, "Bot")["Up since"], "-")

    def test_any_other_state_is_offline(self):
        for state in ("closed", "taken", None):
            with self.subTest(state=state):
                self.assertEqual(rows(page(state), "Bot")["Connection"], DOT + " offline")


class TheColourIsSetFirstAndKept(unittest.TestCase):

    CASES = (("in", True, "on"), ("waiting", False, "wait"), ("in", False, "off"))

    def test_every_branch_colours_the_row_before_writing_it(self):
        for state, chat, _ink in self.CASES:
            for dark in (False, True):
                with self.subTest(state=state, chat=chat, dark=dark):
                    mirc = page(state, chat=chat, dark=dark)
                    flags = [flag for ident, flag, _t in mirc.dcx() if ident == CONNECTION]
                    self.assertEqual(flags, ["-C", "-t"])

    def test_three_colours_on_each_face_and_the_colour_is_kept_for_the_theme(self):
        for dark in (False, True):
            seen = set()
            for state, chat, _ink in self.CASES:
                mirc = page(state, chat=chat, dark=dark)
                rgb = ink_of(mirc).split()[-1]
                self.assertEqual(ink_of(mirc), "+t " + rgb)
                self.assertEqual(mirc.table("dccore.sws")["home.ink"], CONNECTION + " " + rgb)
                seen.add(rgb)
            self.assertEqual(len(seen), 3, "dark=%s" % dark)

    def test_the_kept_colour_follows_the_state(self):
        mirc = page("in", chat=True)
        on = mirc.table("dccore.sws")["home.ink"]
        mirc.chats.clear()
        mirc.call("dccore.sw.home.fill", [])
        self.assertNotEqual(mirc.table("dccore.sws")["home.ink"], on)
        self.assertEqual(rows(mirc, "Bot")["Connection"], DOT + " offline")


class TheDarkThemeSetsTheColourAgain(unittest.TestCase):
    """dccore.theme.dcxtexts gives every value the theme's text colour; the
    Connection row's own colour is set after it."""

    def texts(self, ink):
        mirc = Mirc(real=("dccore.theme.dcxtext",))
        for key, value in generated_data().items():
            mirc.table("dccore.swm")[key.lower()] = value
        mirc.dialogs.add("dccore.set")
        if ink:
            mirc.table("dccore.sws")["home.ink"] = ink
        mirc.call("dccore.theme.dcxtexts", [])
        return [text for ident, flag, text in mirc.dcx() if ident == CONNECTION and flag == "-C"]

    def test_the_last_colour_on_the_row_is_its_ink(self):
        inks = self.texts(CONNECTION + " 192")
        self.assertIn("+t <theme fg>", inks)
        self.assertEqual(inks[-1], "+t 192")

    def test_with_no_ink_kept_it_has_the_themes(self):
        self.assertEqual(self.texts(None)[-1], "+b <theme face>")
        self.assertNotIn("+t 192", self.texts(None))


def chatclose_resets():
    """The dccore.live keys on CHATCLOSE deletes, read from the event: the
    stale state's repair is held to reset the session as it does."""
    return sorted(cmd[1].split()[-1] for cmd in body_of("on *:CHATCLOSE: {")
                  if cmd[0] == "cmd" and cmd[1].startswith("hdel dccore.live "))


class AStaleStateIsPutRight(unittest.TestCase):

    def unstale(self, state, chat):
        mirc = Mirc(real=("dccore.unstale",))
        mirc.table("dccore")["bot"] = BOT
        if chat:
            mirc.chats.add(BOT)
        mirc.live(mode="structured", pairsent=1, vetted=1, typed=0, sw_unlocked=1, tries=2, st_started=STARTED)
        if state is not None:
            mirc.live(state=state)
        before = dict(mirc.table("dccore.live"))
        mirc.call("dccore.unstale", [])
        return mirc, before

    def test_in_with_no_chat_is_closed_as_chatclose_closes_it(self):
        mirc, before = self.unstale("in", chat=False)
        after = mirc.table("dccore.live")
        session = {"mode", "pairsent", "typed", "vetted", "sw.unlocked"}
        expected = {k: v for k, v in before.items() if k not in session}
        expected["state"] = "closed"
        self.assertEqual(after, expected)
        # the session's keys, exactly as CHATCLOSE resets them
        self.assertEqual(session, set(chatclose_resets()))

    def test_it_redraws_and_starts_no_timer_sends_nothing_and_does_not_redial(self):
        mirc, _before = self.unstale("in", chat=False)
        self.assertEqual(mirc.calls, [("dccore.title", ""), ("dccore.chat.title", ""), ("dccore.panel", "")])
        heads = {head for head, _args in mirc.calls}
        self.assertNotIn("timer", heads)
        self.assertFalse(heads & {"dccore.retry", "dccore.connect", "dccore.send", "dccore.sys", "msg"})

    def test_nothing_it_calls_redials_or_sends(self):
        """What it calls, and what they call, to the end: no redial, no
        message to the bot, and not the settings window's restart."""
        reached = reachable("dccore.unstale")
        self.assertTrue({"dccore.title", "dccore.chat.title", "dccore.panel"} <= reached)
        self.assertFalse(reached & {"dccore.retry", "dccore.connect", "dccore.send", "dccore.sw.lost"},
                         sorted(reached))

    def test_a_live_link_is_left_alone(self):
        mirc, before = self.unstale("in", chat=True)
        self.assertEqual(mirc.table("dccore.live"), before)
        self.assertEqual(mirc.calls, [])

    def test_any_other_state_is_left_alone(self):
        for state in DIALLING + ("closed", "taken", None):
            with self.subTest(state=state):
                mirc, before = self.unstale(state, chat=False)
                self.assertEqual(mirc.table("dccore.live"), before)
                self.assertEqual(mirc.calls, [])


_NAMED = re.compile(r"(?<![\w.])\$?(dccore(?:\.\w+)+)")


def alias_calls():
    """{alias: (the aliases it runs now, the ones it leaves to a timer)},
    from the comment-free source. A name on a line after a `.timer` is the
    timer's command: it runs later, from a fresh stack."""
    bodies, current = {}, None
    for line in source().split("\n"):
        start = re.match(r"alias (\S+) \{", line)
        if start:
            current = start.group(1)
            bodies[current] = [line[start.end():]]
        elif line[:1] not in ("", " ", "\t", "}"):
            current = None
        elif current:
            bodies[current].append(line)
    names, graph = set(bodies), {}
    for name, lines in bodies.items():
        now, later = set(), set()
        for line in lines:
            for statement in line.split(" | "):
                timer = statement.find(".timer")
                for found in _NAMED.finditer(statement):
                    if found.group(1) in names and found.group(1) != name:
                        (later if 0 <= timer < found.start() else now).add(found.group(1))
        graph[name] = (now, later)
    return graph


def reachable(start, timers=False, without=()):
    """The aliases `start` runs, and they run, to the end - now only, or
    with what their timers run later too; never through `without`."""
    if "graph" not in _SOURCE:
        _SOURCE["graph"] = alias_calls()
    graph = _SOURCE["graph"]
    seen, todo = set(), [start]
    while todo:
        now, later = graph.get(todo.pop(), (set(), set()))
        for callee in (now | later) if timers else now:
            if callee not in seen and callee not in without:
                seen.add(callee)
                todo.append(callee)
    return seen


class TheTitleRedrawsTheStartPage(unittest.TestCase):
    """dccore.title runs on every step of a dial: with the control panel
    open, it refills the start page too, on the timer dccore.panel.soon
    uses, so the two coalesce into one fill."""

    def title(self, dialog, window, state="waiting"):
        mirc = Mirc(real=("dccore.title",))
        mirc.table("dccore")["bot"] = BOT
        mirc.live(state=state)
        if dialog:
            mirc.dialogs.add("dccore.set")
        if window:
            mirc.windows.add("@DCCore")
        mirc.call("dccore.title", [])
        return mirc.calls

    def test_with_the_panel_open_the_page_is_refilled_even_with_no_window(self):
        calls = self.title(dialog=True, window=False)
        self.assertEqual(calls, [("timer", "timerdccoreSwHome -m 1 300 dccore.sw.home.fill")])

    def test_with_both_open_the_fill_is_set_and_the_title_written(self):
        calls = self.title(dialog=True, window=True)
        self.assertEqual(calls, [("timer", "timerdccoreSwHome -m 1 300 dccore.sw.home.fill"),
                                 ("titlebar", "@DCCore %s %s waiting" % (BOT, chr(183)))])

    def test_with_no_panel_no_timer_starts(self):
        for window in (True, False):
            with self.subTest(window=window):
                calls = self.title(dialog=False, window=window)
                self.assertEqual([c for c in calls if c[0] == "timer"], [])
                self.assertEqual(calls, [("titlebar", "@DCCore %s %s waiting" % (BOT, chr(183)))] if window else [])

    def test_it_is_the_timer_the_panel_uses(self):
        mirc = Mirc(real=("dccore.panel.soon",))
        mirc.dialogs.add("dccore.set")
        mirc.call("dccore.panel.soon", [])
        timers = [c for c in self.title(dialog=True, window=False) if c[0] == "timer"]
        self.assertEqual(len(timers), 1)
        self.assertIn(timers[0], mirc.calls)

    def test_the_title_and_the_fill_never_run_each_other_at_once(self):
        """No loop on the stack: the title leaves the fill to a timer, and
        nothing the fill runs at once runs the fill again."""
        self.assertIn("dccore.sw.home.fill", reachable("dccore.title", timers=True))
        self.assertNotIn("dccore.sw.home.fill", reachable("dccore.title"))
        self.assertNotIn("dccore.sw.home.fill", reachable("dccore.sw.home.fill"))

    def test_the_fill_reaches_the_title_only_through_its_one_library_ask(self):
        """The fill's `dccore.send library` with no chat open says "Not
        connected" (dccore.sys), which opens @DCCore (dccore.window) and so
        writes the title - and the title sets the fill's timer again. That
        is the only way round, and it goes round once: the ask is marked
        before it is sent, so the second fill asks nothing."""
        self.assertIn("dccore.title", reachable("dccore.sw.home.fill", timers=True))
        self.assertNotIn("dccore.title", reachable("dccore.sw.home.fill", timers=True, without={"dccore.send"}))
        mirc = Mirc(real=PAGE, stubs={"dccore.sw.can": lambda part: "$true"})
        for key, value in generated_data().items():
            mirc.table("dccore.swm")[key.lower()] = value
        mirc.dialogs.add("dccore.set")
        mirc.table("dccore.sws").update({"dcx": "1", "side": "0", "live": "1"})
        mirc.table("dccore")["bot"] = BOT
        mirc.live(state="in", mode="structured")
        mirc.call("dccore.sw.home.fill", [])
        mirc.call("dccore.sw.home.fill", [])
        self.assertEqual([c for c in mirc.calls if c[0] == "dccore.send"], [("dccore.send", "library")])


def window(state, chat, mode="structured"):
    mirc = Mirc(real=("dccore.sw.start", "dccore.unstale") + PAGE)
    for key, value in generated_data().items():
        mirc.table("dccore.swm")[key.lower()] = value
    mirc.dialogs.add("dccore.set")
    mirc.table("dccore.sws").update({"dcx": "1", "side": "0"})
    mirc.table("dccore")["bot"] = BOT
    if chat:
        mirc.chats.add(BOT)
    mirc.live(state=state, st_started=STARTED)
    if mode is not None:
        mirc.live(mode=mode)
    return mirc


class TheFooterAndThePageAgree(unittest.TestCase):

    def test_start_repairs_the_state_before_its_footer(self):
        mirc = window("in", chat=False)
        mirc.call("dccore.sw.start", [])
        self.assertEqual(mirc.table("dccore.live")["state"], "closed")
        self.assertNotIn("mode", mirc.table("dccore.live"))
        heads = [c for c in mirc.calls if c[0] in ("dccore.unstale", "dccore.sw.put")]
        self.assertEqual(heads, [("dccore.unstale", ""), ("dccore.sw.put", "1013 Not connected to the bot")])
        self.assertEqual(mirc.table("dccore.sws")["phase"], "off")

    def test_a_live_link_starts_as_before(self):
        mirc = window("in", chat=True)
        mirc.call("dccore.sw.start", [])
        self.assertEqual(mirc.table("dccore.live")["state"], "in")
        self.assertIn(("dccore.send", "consolecaps"), mirc.calls)

    def test_after_lost_the_page_says_what_the_footer_says(self):
        mirc = window("in", chat=False)
        mirc.real.add("dccore.sw.lost")
        mirc.call("dccore.sw.lost", [])
        self.assertIn(("dccore.sw.put", "1013 Not connected to the bot"), mirc.calls)
        self.assertEqual(rows(mirc, "Bot")["Connection"], DOT + " offline")
        self.assertEqual(rows(mirc, "Bot")["Up since"], "-")


class ThePageIsRedrawnWhenTheLinkChanges(unittest.TestCase):

    def test_lost_fills_the_page_after_starting_again(self):
        mirc = Mirc(real=("dccore.sw.lost",))
        mirc.dialogs.add("dccore.set")
        mirc.call("dccore.sw.lost", [])
        self.assertEqual([c[0] for c in mirc.calls], ["dccore.sw.start", "dccore.sw.home.fill"])

    def test_lost_with_no_window_does_nothing(self):
        mirc = Mirc(real=("dccore.sw.lost",))
        mirc.call("dccore.sw.lost", [])
        self.assertEqual(mirc.calls, [])

    def test_lost_on_a_new_console_shows_it_connected(self):
        mirc = window("in", chat=True)
        mirc.real.add("dccore.sw.lost")
        mirc.call("dccore.sw.lost", [])
        self.assertEqual(rows(mirc, "Bot")["Connection"], DOT + " connected (structured console)")

    def test_a_plain_console_fills_the_page_once_its_mode_is_known(self):
        mirc = window("in", chat=True, mode=None)
        mirc.real.add("dccore.plain")
        mirc.call("dccore.plain", [])
        self.assertEqual(mirc.table("dccore.live")["mode"], "plain")
        self.assertEqual(rows(mirc, "Bot")["Connection"], DOT + " connected (plain console)")

    def test_plain_does_not_fill_for_a_structured_console(self):
        mirc = window("in", chat=True)
        mirc.real.add("dccore.plain")
        mirc.call("dccore.plain", [])
        self.assertNotIn("dccore.sw.home.fill", [c[0] for c in mirc.calls])


class TheEvaluatorRefusesWhatItDoesNotKnow(unittest.TestCase):
    """The evaluator is only as good as what it refuses."""

    def test_an_unknown_identifier_is_an_error(self):
        with self.assertRaises(Unknown):
            Mirc().value("$dccore.nosuchthing(1)", {"args": [], "vars": {}})

    def test_a_condition_is_read_as_mirc_reads_it(self):
        mirc, frame = Mirc(), {"args": ["1", "301", "Bot"], "vars": {"%on": "$false", "%n": "3"}}
        self.assertTrue(mirc.cond("(!%on) && ($istok(Today Bot,$3,32))", frame))
        self.assertFalse(mirc.cond("(%on)", frame))
        self.assertTrue(mirc.cond("(%n == $calc(1 + 2))", frame))
        self.assertFalse(mirc.cond("($null > 0)", frame))


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class TheDocs(unittest.TestCase):

    def test_both_entries_are_written_and_rolled(self):
        """Both entries, as fragments until a release rolls them and in the
        two changelogs after (support.changelog_entry)."""
        fragments, problems = roll_changelog.load(REPO_ROOT)
        self.assertEqual([p for p in problems if p.startswith("1396")], [])
        internal = support.changelog_entry(self, 1396, public=False)
        public = support.changelog_entry(
            self, 1396, public=True, opening="- **Fixed (mIRC): the control panel's start page said")
        self.assertTrue(internal.startswith("### "))
        self.assertIn("(#1396)", internal.split("\n", 1)[0])
        self.assertTrue(public.startswith("- **"))
        self.assertEqual(len(public.splitlines()), 1)
        self.assertIn("(bot offline)", public)

    def test_the_console_guide_describes_the_row(self):
        text = read("docs", "ADMIN-CONSOLE.md")
        at = text.index("(#1396)")
        paragraph = text[at - 200:at + 600]
        for said in ("`connected (structured console)`", "`connecting: <step>`", "`offline`", "`(bot offline)`"):
            self.assertIn(said, paragraph)


if __name__ == "__main__":
    unittest.main()
