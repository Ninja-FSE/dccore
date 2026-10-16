"""#1393: DCCore Chat in dccore.mrc keeps a history file, replays it when the
window opens, and counts what arrived while it was not the active window.

mIRC cannot run here. Most of the script's tests read its statements; these
RUN them. A small interpreter below executes the script's own aliases -
dccore.chat.feed, the window, the title, the history and the unread aliases,
the four events - statement by statement, against a temporary log folder
(with a space in its path, as a profile folder often has) and fake windows.
What it does not know it refuses, loudly, rather than guessing: a later
change that uses a new identifier fails here with its name, and the fix is to
teach the interpreter that identifier, not to loosen a test.

It models mIRC where the script leans on it: /loadbuf N loads the LAST N
lines of a file; $read without `n` evaluates the line it read and without
`t` takes a numeric first line as the line count; /write, /mkdir and /loadbuf
take a quoted path, and an unquoted one ends at its first space; a command
that fails is an error that stops the script and jumps to the nearest
:error, in the alias or the aliases that called it. $ctime is fixed, and
$appactive (mIRC in the foreground) is a switch a test turns.

What is held:

- every line the window draws is written, once, after it is drawn - a
  remark from the bot is not; the bot's replay after HELLO of lines already
  in the file draws none of them a second time; an id past the clock
  moves nothing;
- the file is replayed when the window opens, by the live line's rules (own,
  private, or a channel listened on), in grey, without -m, without being
  written again or counted as unread, between its two marker lines; a broken
  row is skipped and a failing replay stops in the replay;
- the window says where the history is kept, while it is kept; a line past
  400 characters is cut the same live and replayed, and written whole;
- recent.txt is cut back to the last 200 lines once it passes 400;
- a line from another bot drawn while the window is not active, or while
  mIRC is in the background, counts in the title, and flashes while that is
  ticked; activating the window, mIRC coming back onto it, typing in it (a
  /command too) or closing it clears the count;
- history off writes nothing and replays nothing; both options are on by
  default; the menu's new items carry no nick and no channel;
- nothing here sends anything (RFC 2812).
"""

import io
import os
import re
import time
import unittest

from tests import support

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

# Placeholders, nobody's real nick or channel.
BOT = "MyBotX"
PEER = "PeerBotA"
HEARD = "#lobby-one"
UNHEARD = "#lobby-two"
CHAT = "@DCCore-Chat"

NOW = 1_800_000_000                       # the fake $ctime, 2027-01-15 08:00 UTC
TODAY_ID = (NOW - 3600) * 1000            # an id (ms) from an hour ago
OLD_ID = (NOW - 3 * 86400) * 1000         # an id from three days ago


def script_text():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def code_body(text, header):
    """The code of the block `header` opens (`alias x`, `on *:X:y:`, `menu
    x`), comment lines dropped BEFORE the braces are counted, so a comment
    naming the block can neither be found as it nor unbalance it."""
    lines = [line for line in text.split("\n") if not line.strip().startswith(";")]
    opening = re.compile(r"^%s \{(.*)$" % re.escape(header))
    for start, line in enumerate(lines):
        match = opening.match(line)
        if match:
            break
    else:
        raise AssertionError("no block: " + header)
    depth, body = 1, []
    chunks = [match.group(1)] + lines[start + 1:]
    for number, chunk in enumerate(chunks):
        for index, ch in enumerate(chunk):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body.append(chunk[:index])
                    return "\n".join(body)
        body.append(chunk)
        if number > 2000:
            break
    raise AssertionError("unbalanced block: " + header)


def statements(body):
    return [line.strip() for line in body.split("\n") if line.strip()]


# ---- the interpreter --------------------------------------------------------

class MircError(Exception):
    """What makes mIRC stop a script and look for :error."""


class _Return(Exception):
    def __init__(self, value):
        super().__init__(value)
        self.value = value


class _Halt(Exception):
    pass


class Frame:
    def __init__(self, args):
        self.args = [a for a in args]
        self.vars = {}
        self.error = None


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_OPS = ("==", "!=", "<=", ">=", "<", ">", "isnum", "!isnum", "isin", "!isin")
_SENDS = {"msg", "notice", "ctcp", "describe", "say", "raw", "quote", "amsg", "ame",
          "dccore.send", "dccore.chat.say"}


def is_number(value):
    return bool(_NUMBER.fullmatch(value))


def truthy(value):
    return value not in ("", "0", "$false")


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


def split_args(raw):
    parts, current, depth = [], [], 0
    for ch in raw:
        if ch == "," and depth == 0:
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
    """[parts], [joins] at the top level of a condition."""
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


def wraps(text):
    if not (text.startswith("(") and text.endswith(")")):
        return False
    depth = 0
    for index, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and index < len(text) - 1:
                return False
    return True


def matching_paren(text, at):
    depth = 0
    for index in range(at, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unbalanced: " + text)


def calc(expression):
    """$calc for numbers, + - * / and parentheses, nothing else."""
    tokens = re.findall(r"\d+(?:\.\d+)?|[-+*/()]|\S", expression)
    at = [0]

    def take():
        at[0] += 1
        return tokens[at[0] - 1]

    def peek():
        return tokens[at[0]] if at[0] < len(tokens) else None

    def atom():
        token = take() if peek() is not None else None
        if token == "(":
            value = total()
            if take() != ")":
                raise AssertionError("$calc: unbalanced " + expression)
            return value
        if token == "-":
            return -atom()
        if token is None or not is_number(token):
            raise AssertionError("the interpreter's $calc does not take: " + expression)
        return float(token)

    def product():
        value = atom()
        while peek() in ("*", "/"):
            value = value * atom() if take() == "*" else value / atom()
        return value

    def total():
        value = product()
        while peek() in ("+", "-"):
            value = value + product() if take() == "+" else value - product()
        return value

    value = total()
    if peek() is not None:
        raise AssertionError("the interpreter's $calc does not take: " + expression)
    return str(int(value)) if value.is_integer() else repr(value)


def pad_groups(text):
    """A $calc argument with its grouping parentheses set apart by spaces,
    so the identifiers inside them are words of their own; the parentheses
    of an identifier's own arguments, $name(...), are left as they are."""
    out, stack = [], []
    for index, ch in enumerate(text):
        if ch == "(":
            grouping = re.search(r"\$[\w.+]+$", text[:index]) is None
            stack.append(grouping)
            out.append(" ( " if grouping else ch)
        elif ch == ")":
            if not stack:
                raise AssertionError("$calc: unbalanced " + text)
            out.append(" ) " if stack.pop() else ch)
        else:
            out.append(ch)
    if stack:
        raise AssertionError("$calc: unbalanced " + text)
    return "".join(out)


def asctime(seconds, fmt):
    moment = time.gmtime(int(seconds))
    names = {"yyyy": "%04d" % moment.tm_year, "mm": "%02d" % moment.tm_mon, "dd": "%02d" % moment.tm_mday,
             "HH": "%02d" % moment.tm_hour, "nn": "%02d" % moment.tm_min, "ss": "%02d" % moment.tm_sec}
    return re.sub(r"yyyy|mm|dd|HH|nn|ss", lambda m: names[m.group(0)], fmt)


def strip_codes(text):
    text = re.sub(r"\x03(\d{1,2}(,\d{1,2})?)?", "", text)
    return re.sub(r"[\x02\x0f\x16\x1d\x1e\x1f]", "", text)


class Parser:
    """A block's code into (kind, condition, body) nodes."""

    def __init__(self, source):
        self.s = source
        self.i = 0

    def parse(self):
        return self.seq(braced=False)

    def seq(self, braced):
        nodes = []
        while True:
            while self.i < len(self.s) and self.s[self.i] in " \t\n|":
                self.i += 1
            if self.i >= len(self.s):
                if braced:
                    raise AssertionError("unclosed {")
                return nodes
            if self.s[self.i] == "}":
                if not braced:
                    raise AssertionError("stray }")
                self.i += 1
                return nodes
            nodes.append(self.statement())

    def statement(self):
        rest = self.s[self.i:]
        for keyword in ("if", "elseif", "while"):
            if rest.startswith(keyword + " ") or rest.startswith(keyword + "("):
                self.i += len(keyword)
                brace = self.condition_end()
                condition = self.s[self.i:brace].strip()
                self.i = brace + 1
                return (keyword, condition, self.seq(braced=True))
        if rest.startswith("else ") or rest.startswith("else{"):
            self.i += 4
            while self.s[self.i] == " ":
                self.i += 1
            if self.s[self.i] != "{":
                raise AssertionError("else without {: " + rest[:60])
            self.i += 1
            return ("else", None, self.seq(braced=True))
        if rest.startswith(":"):
            end = self.s.find("\n", self.i)
            end = len(self.s) if end < 0 else end
            label = self.s[self.i + 1:end].strip()
            self.i = end
            return ("label", label, None)
        start, depth = self.i, 0
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
            elif depth == 0 and ch in "|}" and self.s[self.i - 1] == " ":
                break
            self.i += 1
        return ("cmd", self.s[start:self.i].strip(), None)

    def condition_end(self):
        depth = 0
        for index in range(self.i, len(self.s)):
            ch = self.s[index]
            if ch == "\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "{" and depth == 0:
                return index
        raise AssertionError("a condition without its {: " + self.s[self.i:self.i + 80])


def month(channel, line_id):
    """The history file a line goes to since #1401: a folder per channel, a
    file per month, both relative to the history folder."""
    return os.path.join(channel, asctime(line_id // 1000, "yyyy-mm") + ".log")


class Mirc:
    """dccore.mrc's own aliases, run against fake windows and a real temp folder."""

    STUB_COMMANDS = ("dccore.save", "dccore.chat.peers.redraw", "font")
    STUB_IDENTIFIERS = {"dccore.col": "6", "dccore.fontsize": "9"}

    def __init__(self, text, logs_dir, probes=()):
        self.text = text
        self.logs_dir = logs_dir
        self.logdir = logs_dir + "\\"
        self.tables = {"dccore": {}, "dccore.live": {}, "dccore.chatpeers": {}}
        self.windows = {}
        self.active = "Status Window"
        # mIRC in the foreground ($appactive); a test sets it False for "away".
        self.appactive = True
        # A fault to inject: an echo whose text holds this fails, as a command
        # mIRC cannot carry out does - to drive an alias's :error path.
        self.fail_echo = None
        self.trace = []
        self.sent = []
        self.flashes = []
        self.ran = []
        self.probes = set(probes)
        self.probed = []

    # -- blocks, parsed once per text: keyed on the text itself, so a changed
    # script (a mutation) is never handed the old one's blocks. The nodes are
    # shared and only ever read.
    _NODES = {}
    _ALIASES = {}

    def nodes(self, header):
        key = (self.text, header)
        if key not in self._NODES:
            self._NODES[key] = Parser(code_body(self.text, header)).parse()
        return self._NODES[key]

    def has_alias(self, name):
        if self.text not in self._ALIASES:
            self._ALIASES[self.text] = {match.lower() for match in
                                        re.findall(r"(?m)^alias (\S+) \{", self.text)}
        return name.lower() in self._ALIASES[self.text]

    def call(self, name, args):
        self.trace.append(("call", name, " ".join(args)))
        return self.run_block("alias " + name, args)

    def event(self, header, args=()):
        try:
            self.run_block(header, list(args))
        except _Halt:
            pass

    def run_block(self, header, args):
        nodes = self.nodes(header)
        frame = Frame(args)
        labels = [i for i, node in enumerate(nodes) if node[0] == "label" and node[1].lower() == "error"]
        try:
            try:
                self.run(nodes, frame)
            except MircError as error:
                if not labels:
                    raise
                frame.error = str(error)
                self.run(nodes[labels[0] + 1:], frame)
                if frame.error:
                    raise MircError(frame.error)
        except _Return as done:
            return done.value
        return ""

    def run(self, nodes, frame):
        chain = None
        for kind, condition, body in nodes:
            if kind == "if":
                chain = self.cond(condition, frame)
                if chain:
                    self.run(body, frame)
            elif kind == "elseif":
                if chain is None:
                    raise AssertionError("elseif without if")
                if not chain:
                    chain = self.cond(condition, frame)
                    if chain:
                        self.run(body, frame)
            elif kind == "else":
                if chain is None:
                    raise AssertionError("else without if")
                if not chain:
                    self.run(body, frame)
                chain = None
            else:
                chain = None
                if kind == "while":
                    turns = 0
                    while self.cond(condition, frame):
                        self.run(body, frame)
                        turns += 1
                        if turns > 100000:
                            raise AssertionError("a loop that does not end: " + condition)
                elif kind == "cmd":
                    self.command(condition, frame)

    # -- conditions
    def cond(self, text, frame):
        text = text.strip()
        parts, joins = split_logic(text)
        if len(parts) > 1:
            result = self.cond(parts[0], frame)
            for join, part in zip(joins, parts[1:]):
                value = self.cond(part, frame)
                result = (result and value) if join == "&&" else (result or value)
            return result
        if wraps(text):
            return self.cond(text[1:-1], frame)
        words = split_words(text)
        at = [index for index, word in enumerate(words) if word in _OPS]
        if not at:
            if text.startswith("!"):
                return not truthy(self.eval_text(text[1:], frame))
            return truthy(self.eval_text(text, frame))
        op = words[at[0]]
        left = self.eval_text(" ".join(words[:at[0]]), frame)
        right = self.eval_text(" ".join(words[at[0] + 1:]), frame)
        return self.compare(op, left, right)

    @staticmethod
    def compare(op, left, right):
        if op in ("==", "!="):
            if is_number(left) and is_number(right):
                same = float(left) == float(right)
            else:
                same = left.lower() == right.lower()
            return same if op == "==" else not same
        if op in ("isnum", "!isnum"):
            if right:
                raise AssertionError("isnum with a range is not modelled")
            return is_number(left) if op == "isnum" else not is_number(left)
        if op in ("isin", "!isin"):
            inside = left.lower() in right.lower()
            return inside if op == "isin" else not inside
        if left == "" or right == "":
            return False
        if not (is_number(left) and is_number(right)):
            raise AssertionError("a comparison of text is not modelled: %r %s %r" % (left, op, right))
        a, b = float(left), float(right)
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    # -- evaluation
    def eval_text(self, text, frame):
        out, glue = [], False
        for word in split_words(text):
            if word == "$+":
                glue = True
                continue
            value = self.eval_word(word, frame)
            if glue and out:
                out[-1] += value
            elif value != "":
                out.append(value)
            glue = False
        return " ".join(out)

    def eval_word(self, word, frame):
        if word.startswith("%"):
            if re.fullmatch(r"%[\w.]+", word):
                return frame.vars.get(word.lower(), "")
            return word
        if not word.startswith("$") or len(word) == 1:
            return word
        numbered = re.fullmatch(r"\$(\d+)(-)?(\d+)?", word)
        if numbered:
            first = int(numbered.group(1))
            if first == 0:
                return str(len(frame.args))
            if numbered.group(2) is None:
                return frame.args[first - 1] if first <= len(frame.args) else ""
            last = int(numbered.group(3)) if numbered.group(3) else len(frame.args)
            return " ".join(frame.args[first - 1:last])
        named = re.match(r"\$(\+|[A-Za-z][\w.]*)", word)
        if not named:
            return word
        name, at, raw = named.group(1), named.end(), None
        if at < len(word) and word[at] == "(":
            close = matching_paren(word, at)
            raw = split_args(word[at + 1:close])
            at = close + 1
        prop = None
        if at < len(word) and word[at] == ".":
            prop = word[at + 1:]
            at = len(word)
        if at != len(word):
            raise AssertionError("the interpreter cannot read %r" % word)
        return self.identifier(name, raw, prop, frame)

    def identifier(self, name, raw, prop, frame):
        lower = name.lower()
        if lower == "iif":
            branch = raw[1] if self.cond(raw[0], frame) else (raw[2] if len(raw) > 2 else "")
            return self.eval_text(branch, frame)
        if lower == "calc":
            # Inside $calc a bare ( ) groups; the identifiers in it are read.
            return calc(self.eval_text(pad_groups(",".join(raw)), frame))
        args = [self.eval_text(part, frame) for part in raw] if raw is not None else []
        if prop is not None:
            raise AssertionError("properties are not modelled: $%s().%s" % (name, prop))
        if lower in self.probes:
            self.probed.append(lower)
            return ""
        if lower in self.STUB_IDENTIFIERS:
            return self.STUB_IDENTIFIERS[lower]
        builtin = getattr(self, "id_" + lower.replace(".", "_").replace("+", "plus"), None)
        if builtin is not None and "." not in lower:
            return builtin(*args)
        if self.has_alias(name):
            self.trace.append(("identifier", lower, ",".join(args)))
            return self.run_block("alias " + name, args)
        raise AssertionError("the interpreter does not know $" + name)

    # -- identifiers
    def id_null(self):
        return ""

    def id_true(self):
        return "$true"

    def id_false(self):
        return "$false"

    def id_plus(self, *parts):
        return "".join(parts)

    def id_chr(self, number):
        return chr(int(number))

    def id_left(self, text, count):
        count = int(count)
        return text[:count] if count >= 0 else text[:len(text) + count]

    def id_mid(self, text, start, count=None):
        start = int(re.match(r"-?\d+", start).group(0))
        tail = text[start - 1:]
        return tail if count is None else tail[:int(count)]

    def id_len(self, text):
        return str(len(text))

    def id_strip(self, text):
        return strip_codes(text)

    def id_int(self, number):
        return str(int(float(number)))

    def id_asctime(self, seconds, fmt):
        return asctime(seconds, fmt)

    def id_ctime(self):
        return str(NOW)

    def id_version(self):
        return "7.79"

    def id_active(self):
        return self.active

    def id_appactive(self):
        return "$true" if self.appactive else "$false"

    def id_logdir(self):
        return self.logdir

    def id_qt(self, text):
        return text if text.startswith('"') and text.endswith('"') else '"%s"' % text

    def id_mkfn(self, text):
        return re.sub(r'[\\/:*?"<>|]', "_", text)

    def id_hget(self, table, item=None):
        values = self.tables.get(table.lower())
        if item is None:
            return table if values is not None else ""
        return "" if values is None else values.get(item.lower(), "")

    def id_istok(self, tokens, token, char):
        found = token.lower() in [t.lower() for t in tokens.split(chr(int(char))) if t]
        return "$true" if found else "$false"

    def id_gettok(self, text, number, char):
        tokens = [t for t in text.split(chr(int(char))) if t]
        number = int(number)
        if number == 0:
            return str(len(tokens))
        if number < 0:
            number = len(tokens) + number + 1
        return tokens[number - 1] if 1 <= number <= len(tokens) else ""

    def id_window(self, name):
        return name if name.lower() in self.windows else ""

    def id_line(self, name, number):
        lines = self.window(name)["lines"]
        number = int(number)
        if number == 0:
            return str(len(lines))
        return lines[number - 1]["text"] if number <= len(lines) else ""

    def id_nofile(self, path):
        return path[:path.rfind("\\") + 1]

    def id_nopath(self, path):
        return path[path.rfind("\\") + 1:]

    def id_isfile(self, path):
        return "$true" if os.path.isfile(self.host(path)) else "$false"

    def id_isdir(self, path):
        return "$true" if os.path.isdir(self.host(path)) else "$false"

    def id_lines(self, path):
        return str(len(self.file_lines(path))) if os.path.isfile(self.host(path)) else "0"

    def id_read(self, path, *rest):
        switches, number = "", None
        if len(rest) == 1:
            number = rest[0]
        elif len(rest) == 2:
            switches, number = rest
        else:
            raise AssertionError("this $read is not modelled: %r" % (rest,))
        if re.search(r"[swr]", switches):
            raise AssertionError("$read with a search is not modelled")
        lines = self.file_lines(path)
        if "t" not in switches and lines and is_number(lines[0].strip()):
            lines = lines[1:]
        number = int(number)
        line = lines[number - 1] if 1 <= number <= len(lines) else ""
        if "n" not in switches:
            line = self.eval_text(line, Frame([]))
        return line

    # -- files and windows
    @staticmethod
    def host(path):
        return path.replace("\\", os.sep)

    def file_lines(self, path):
        with io.open(self.host(path), encoding="utf-8", newline="") as handle:
            return handle.read().replace("\r\n", "\n").split("\n")[:-1] or []

    @staticmethod
    def path_and_rest(text):
        """A command's path: quoted, or up to the first space."""
        if text.startswith('"'):
            end = text.index('"', 1)
            return text[1:end], text[end + 1:].lstrip(" ")
        path, _, rest = text.partition(" ")
        return path, rest

    def window(self, name):
        try:
            return self.windows[name.lower()]
        except KeyError:
            raise MircError("invalid window: " + name) from None

    # -- commands
    def command(self, text, frame):
        word, _, rest = text.partition(" ")
        name = word.lstrip(".").lower()
        if name == "var":
            match = re.fullmatch(r"(%[\w.]+)(?: = (.*))?", rest)
            if not match or ", %" in rest:
                raise AssertionError("this /var is not modelled: " + rest)
            frame.vars[match.group(1).lower()] = self.eval_text(match.group(2) or "", frame)
            return
        if name == "inc":
            variable = rest.split(" ")[0].lower()
            frame.vars[variable] = calc("%s + 1" % (frame.vars.get(variable) or "0"))
            return
        if name == "return":
            raise _Return(self.eval_text(rest, frame))
        if name in ("halt", "haltdef"):
            raise _Halt()
        if name == "reseterror":
            frame.error = None
            return
        if name == "tokenize":
            char, _, value = rest.partition(" ")
            frame.args = [t for t in self.eval_text(value, frame).split(chr(int(char))) if t]
            return
        value = self.eval_text(rest, frame)
        if name in _SENDS:
            self.sent.append((name, value))
            return
        if name in self.STUB_COMMANDS:
            self.trace.append(("stub", name, value))
            return
        builtin = getattr(self, "cmd_" + name, None)
        if builtin is not None and "." not in name:
            self.trace.append(("command", name, value))
            builtin(value)
            return
        if self.has_alias(word.lstrip(".")):
            self.call(word.lstrip("."), [t for t in value.split(" ") if t])
            return
        raise AssertionError("the interpreter does not know /" + word)

    def cmd_hadd(self, value):
        words = [w for w in value.split(" ") if not w.startswith("-")] if value.startswith("-") else value.split(" ")
        table, item, data = words[0], words[1], " ".join(words[2:])
        if table.lower() not in self.tables:
            raise MircError("no such table: " + table)
        self.tables[table.lower()][item.lower()] = data

    def cmd_hdel(self, value):
        table, item = value.split(" ")[:2]
        self.tables.get(table.lower(), {}).pop(item.lower(), None)

    def cmd_hinc(self, value):
        table, item = value.split(" ")[:2]
        values = self.tables[table.lower()]
        values[item.lower()] = str(int(values.get(item.lower()) or "0") + 1)

    def cmd_echo(self, value):
        words = value.split(" ")
        colour = words.pop(0) if words[0].isdigit() else None
        switches = words.pop(0)[1:] if words and words[0].startswith("-") else ""
        target = self.active if "a" in switches else words.pop(0)
        if self.fail_echo is not None and self.fail_echo in " ".join(words):
            raise MircError("/echo failed (injected)")
        self.window(target)["lines"].append({"text": " ".join(words), "colour": colour, "switches": switches})

    def cmd_window(self, value):
        words = value.split(" ")
        switches = words.pop(0)[1:] if words[0].startswith("-") else ""
        name = words[0]
        if "c" in switches:
            self.windows.pop(name.lower(), None)
            if self.active.lower() == name.lower():
                self.active = "Status Window"
            return
        if name.lower() not in self.windows:
            self.windows[name.lower()] = {"lines": [], "title": "", "switches": switches,
                                          "hidden": "h" in switches}
            if "n" not in switches and "h" not in switches:
                self.active = name
        if "a" in switches:
            self.active = name

    def cmd_titlebar(self, value):
        name, _, title = value.partition(" ")
        self.window(name)["title"] = title

    def cmd_write(self, value):
        if value.startswith("-"):
            raise AssertionError("/write switches are not modelled")
        path, text = self.path_and_rest(value)
        try:
            with io.open(self.host(path), "a", encoding="utf-8", newline="") as handle:
                handle.write(text + "\r\n")
        except OSError as error:
            raise MircError("/write: unable to open " + path) from error

    def cmd_loadbuf(self, value):
        words = value.split(" ", 1)
        count = None
        if words[0].isdigit():
            count = int(words[0])
            value = words[1]
        if value.startswith("-"):
            raise AssertionError("/loadbuf switches are not modelled")
        name, _, rest = value.partition(" ")
        path, _ = self.path_and_rest(rest)
        window = self.window(name)
        if not os.path.isfile(self.host(path)):
            raise MircError("/loadbuf: unable to open " + path)
        lines = self.file_lines(path)
        for line in (lines[-count:] if count else lines):
            window["lines"].append({"text": line})

    def cmd_savebuf(self, value):
        if value.split(" ", 1)[0].isdigit() or value.startswith("-"):
            raise AssertionError("/savebuf with a count or switches is not modelled")
        name, _, rest = value.partition(" ")
        path, _ = self.path_and_rest(rest)
        lines = [line["text"] for line in self.window(name)["lines"]]
        try:
            with io.open(self.host(path), "w", encoding="utf-8", newline="") as handle:
                handle.write("".join(line + "\r\n" for line in lines))
        except OSError as error:
            raise MircError("/savebuf: unable to open " + path) from error

    def cmd_mkdir(self, value):
        path, _ = self.path_and_rest(value)
        try:
            os.mkdir(self.host(path))
        except OSError as error:
            raise MircError("/mkdir: unable to make " + path) from error

    def cmd_run(self, value):
        self.ran.append(value)

    def cmd_flash(self, value):
        words = value.split(" ")
        target = words.pop(0) if words[0].startswith("@") else None
        self.flashes.append((target, " ".join(words)))


# ---- the scenarios -----------------------------------------------------------

class Session:
    """One mIRC session with the script loaded and the bot connected."""

    def __init__(self, test, text=None, probes=()):
        root = support.temp_dir(test, prefix="dccore-1393-")
        self.logs = os.path.join(root, "mIRC logs")
        self.mirc = Mirc(text if text is not None else script_text(), self.logs, probes)
        self.mirc.tables["dccore"].update({"bot": BOT, "font": "0", "chat.all": "0",
                                           "chat.listen": HEARD, "chat.popup": "1"})
        self.mirc.tables["dccore.live"].update({"state": "in", "mode": "structured",
                                                "chat.chans": HEARD + " " + UNHEARD})
        self.defaults()

    def defaults(self):
        """dccore.init's own `dccore.default` lines, run as they are."""
        for line in statements(code_body(self.mirc.text, "alias dccore.init")):
            if line.startswith("dccore.default "):
                self.mirc.command(line, Frame([]))

    # the folder, as the script names it
    @property
    def folder(self):
        return os.path.join(self.logs, "DCCore-Chat")

    def path(self, name):
        return os.path.join(self.folder, name)

    def lines_of(self, name):
        with io.open(self.path(name), encoding="utf-8", newline="") as handle:
            return handle.read().replace("\r\n", "\n").split("\n")[:-1]

    def seed_recent(self, rows):
        os.makedirs(self.folder, exist_ok=True)
        with io.open(self.path("recent.txt"), "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(" ".join(str(part) for part in row) + "\r\n" for row in rows))

    def feed(self, line_id, channel, nick, text):
        self.mirc.call("dccore.chat.feed", [str(line_id), channel, nick] + text.split(" "))

    def open_window(self):
        self.mirc.call("dccore.chat.window", [])

    @property
    def chat_lines(self):
        return self.mirc.windows[CHAT.lower()]["lines"] if CHAT.lower() in self.mirc.windows else []

    def drawn(self, text):
        """How many lines of the chat window end with `text`."""
        return sum(1 for line in self.chat_lines if line["text"].endswith(text))

    @property
    def title(self):
        return self.mirc.windows[CHAT.lower()]["title"]

    def live(self, key):
        return self.mirc.tables["dccore.live"].get(key, "")

    def calls(self, name):
        return [i for i, entry in enumerate(self.mirc.trace) if entry[0] == "call" and entry[1] == name]


class TheInterpreterReadsWhatItIsGiven(unittest.TestCase):
    """The interpreter first, on code of its own: what the scenarios below
    conclude rests on it."""

    def mirc(self, code):
        text = "alias t.probe {\n%s\n}\nalias t.id { return [ $+ $1 $+ ] }\n" % code
        return Mirc(text, support.temp_dir(self, prefix="dccore-1393-") + os.sep + "logs")

    def test_conditions_chains_and_loops(self):
        mirc = self.mirc("var %i = 1\nvar %s = $null\nwhile (%i <= 3) {\n"
                         "  if (%i == 2) { var %s = %s $+ b }\n  elseif (%i == 1) || (%i == 9) { var %s = a }\n"
                         "  else { var %s = %s $+ c }\n  inc %i\n}\nreturn %s $t.id(x y)")
        self.assertEqual(mirc.call("t.probe", []), "abc [x y]")

    def test_and_binds_left_to_right_and_or_is_not_and(self):
        mirc = self.mirc("if (1 == 2) || (2 == 2) { return yes }\nreturn no")
        self.assertEqual(mirc.call("t.probe", []), "yes")
        mirc = self.mirc("if (1 == 2) && (2 == 2) { return yes }\nreturn no")
        self.assertEqual(mirc.call("t.probe", []), "no")

    def test_an_error_jumps_to_the_callers_error_label(self):
        text = ("alias t.outer {\n  t.inner\n  return fine\n  :error\n  reseterror\n  return caught\n}\n"
                "alias t.inner { window -c @nowhere | echo 14 @nowhere x }\n")
        self.assertEqual(Mirc(text, "unused").call("t.outer", []), "caught")

    def test_calc_and_time(self):
        self.assertEqual(calc("2 * (3 + 4) - 10 / 4"), "11.5")
        self.assertEqual(calc("1800000000123 / 1000"), "1800000000.123")
        self.assertEqual(asctime(NOW, "yyyy-mm-dd HH:nn:ss"), "2027-01-15 08:00:00")
        with self.assertRaises(AssertionError):
            calc("2 ** 3")

    def test_calc_groups_and_reads_the_identifiers_inside_them(self):
        mirc = self.mirc("return $calc(($ctime + 300) * 1000) $calc(2 * ($t.id(1) + 1))")
        mirc.text = mirc.text.replace("alias t.id { return [ $+ $1 $+ ] }", "alias t.id { return $1 }")
        self.assertEqual(mirc.call("t.probe", []), "%d 4" % ((NOW + 300) * 1000))

    def test_appactive_follows_its_switch(self):
        mirc = self.mirc("return $appactive")
        self.assertEqual(mirc.call("t.probe", []), "$true")
        mirc.appactive = False
        self.assertEqual(mirc.call("t.probe", []), "$false")

    def test_an_unknown_identifier_or_command_is_refused(self):
        with self.assertRaises(AssertionError):
            self.mirc("return $nosuchthing(1)").call("t.probe", [])
        with self.assertRaises(AssertionError):
            self.mirc("nosuchcommand 1").call("t.probe", [])

    def test_an_unquoted_path_ends_at_its_first_space(self):
        self.assertEqual(Mirc.path_and_rest('"a b\\c.txt" text'), ("a b\\c.txt", "text"))
        self.assertEqual(Mirc.path_and_rest("a b\\c.txt text"), ("a", "b\\c.txt text"))


class EveryDrawnLineIsWrittenOnce(unittest.TestCase):

    def setUp(self):
        self.s = Session(self)

    def test_a_channel_line_goes_to_the_channel_file_and_recent_txt(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "hello there")
        self.assertEqual(self.s.lines_of(month(HEARD, TODAY_ID)),
                         ["%s %s %s hello there" % (asctime(TODAY_ID // 1000, "yyyy-mm-dd HH:nn:ss"), HEARD, PEER)])
        self.assertEqual(self.s.lines_of("recent.txt"), ["%d %s %s other hello there" % (TODAY_ID, HEARD, PEER)])
        self.assertEqual(self.s.drawn("hello there"), 1)

    def test_the_log_folder_is_made_when_mirc_never_logged_anything(self):
        self.assertFalse(os.path.isdir(self.s.logs))
        self.s.feed(TODAY_ID, HEARD, PEER, "first ever")
        self.assertTrue(os.path.isfile(self.s.path("recent.txt")))

    def test_own_private_and_fan_out_lines_have_their_own_files(self):
        self.s.feed(TODAY_ID, HEARD, BOT, "mine here")
        self.s.feed(TODAY_ID + 1, "@" + PEER, PEER, "just us")
        self.s.feed(TODAY_ID + 2, "-", BOT, "to everyone")
        self.assertTrue(self.s.lines_of(month(HEARD, TODAY_ID))[0].endswith(" %s %s mine here" % (HEARD, BOT)))
        self.assertTrue(self.s.lines_of(month("@" + PEER, TODAY_ID + 1))[0].endswith(" @%s %s just us" % (PEER, PEER)))
        self.assertTrue(self.s.lines_of(month("every-channel", TODAY_ID + 2))[0].endswith(" * %s to everyone" % BOT))
        self.assertEqual([row.split(" ")[3] for row in self.s.lines_of("recent.txt")], ["own", "other", "own"])

    def test_a_channel_name_is_made_safe_for_a_file_name(self):
        self.s.mirc.tables["dccore"]["chat.listen"] = "#lobby|two"
        self.s.feed(TODAY_ID, "#lobby|two", PEER, "piped")
        self.assertEqual(sorted(os.listdir(self.s.folder)), ["#lobby_two", "recent.txt"])
        self.assertTrue(os.path.isfile(self.s.path(month("#lobby_two", TODAY_ID))))

    def test_the_text_is_written_stripped(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "\x0304red\x03 and \x02bold\x02")
        self.assertTrue(self.s.lines_of("recent.txt")[0].endswith(" other red and bold"))

    def test_a_remark_from_the_bot_is_shown_and_never_written(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "before")
        self.s.feed(TODAY_ID + 1, HEARD, "*", "somebody was hidden for flooding")
        self.assertEqual(self.s.drawn("somebody was hidden for flooding"), 1)
        self.assertEqual(len(self.s.lines_of("recent.txt")), 1)
        self.assertEqual(len(self.s.calls("dccore.chat.hist.add")), 1)

    def test_a_line_not_drawn_is_not_written(self):
        self.s.feed(TODAY_ID, UNHEARD, PEER, "not listened")
        self.s.mirc.tables["dccore"]["chat.popup"] = "0"
        self.s.feed(TODAY_ID + 1, HEARD, PEER, "window closed, no popup")
        self.assertFalse(os.path.exists(self.s.folder))
        self.assertEqual(self.s.chat_lines, [])

    def test_the_same_id_twice_is_drawn_and_written_once(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "once only")
        self.s.feed(TODAY_ID, HEARD, PEER, "once only")
        self.assertEqual(self.s.drawn("once only"), 1)
        self.assertEqual(len(self.s.lines_of("recent.txt")), 1)

    def test_a_line_opening_the_window_is_drawn_after_the_replay_and_once(self):
        """Written before it was drawn, the window's replay would draw it
        from the file and the feed again."""
        self.s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "from before")])
        self.s.mirc.tables["dccore.live"]["chat.last"] = str(TODAY_ID)
        self.s.feed(TODAY_ID + 5, HEARD, PEER, "the new one")
        texts = [line["text"] for line in self.s.chat_lines]
        self.assertEqual(self.s.drawn("the new one"), 1)
        self.assertEqual(self.s.drawn("from before"), 1)
        self.assertLess(texts.index("End of the history file."),
                        next(i for i, t in enumerate(texts) if t.endswith("the new one")))
        self.assertEqual(len(self.s.lines_of("recent.txt")), 2)

    def test_written_after_it_is_drawn_and_counted_last(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "in order")
        show = self.s.calls("dccore.chat.show")
        add = self.s.calls("dccore.chat.hist.add")
        unread = self.s.calls("dccore.chat.unread.add")
        self.assertEqual((len(show), len(add), len(unread)), (1, 1, 1))
        self.assertLess(show[0], add[0])
        self.assertLess(add[0], unread[0])

    def test_a_file_that_cannot_be_written_loses_the_line_and_nothing_else(self):
        os.makedirs(self.s.path(month(HEARD, TODAY_ID)))       # a folder where the file goes
        self.s.feed(TODAY_ID, HEARD, PEER, "still shown")
        self.assertEqual(self.s.drawn("still shown"), 1)
        self.assertEqual(self.s.live("chat.unread"), "1", "the feed went on after the failed write")


class TheBotsReplayAfterHelloIsNotDrawnTwice(unittest.TestCase):
    """The first CHAT line of a session takes recent.txt's newest id as
    already drawn - before the line is checked against it."""

    def setUp(self):
        self.s = Session(self)
        self.s.seed_recent([(TODAY_ID + i, HEARD, PEER, "other", "old %d" % i) for i in range(3)])

    def test_a_replayed_line_already_in_the_file_is_not_drawn_or_written_again(self):
        self.s.feed(TODAY_ID + 1, HEARD, PEER, "old 1")
        self.s.feed(TODAY_ID + 2, HEARD, PEER, "old 2")
        self.s.feed(TODAY_ID + 3, HEARD, PEER, "missed while away")
        self.assertEqual(self.s.drawn("old 1"), 1)
        self.assertEqual(self.s.drawn("old 2"), 1)
        self.assertEqual(self.s.drawn("missed while away"), 1)
        ids = [row.split(" ")[0] for row in self.s.lines_of("recent.txt")]
        self.assertEqual(ids, [str(TODAY_ID + i) for i in range(4)])

    def test_the_file_is_read_without_evaluating_it(self):
        """$read without `n` runs the identifiers in the line it reads - and
        the line is text another bot's operator typed."""
        s = Session(self, probes=("dccore.test.probe",))
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "look $dccore.test.probe")])
        s.feed(TODAY_ID + 1, HEARD, PEER, "next")
        self.assertEqual(s.live("chat.last"), str(TODAY_ID + 1))
        self.assertEqual(s.mirc.probed, [])

    def test_with_the_history_off_the_file_decides_nothing(self):
        self.s.mirc.tables["dccore"]["chat.history"] = "0"
        self.s.feed(TODAY_ID + 1, HEARD, PEER, "old 1")
        self.assertEqual(self.s.drawn("old 1"), 1)

    def test_the_read_switches_take_the_line_as_text_and_the_first_line_as_a_line(self):
        """`t`: a first line that is a number is otherwise taken as the line
        count; `n`: above. The interpreter's $read is only as good as its
        model of mIRC, so the switches are held here as well."""
        floor = statements(code_body(script_text(), "alias dccore.chat.hist.floor"))
        reads = re.findall(r"\$read\(%r,(\w+),%n\)", "\n".join(floor))
        self.assertEqual(len(reads), 1)
        self.assertIn("n", reads[0])
        self.assertIn("t", reads[0])


class TheWindowReplaysTheFile(unittest.TestCase):

    def setUp(self):
        self.s = Session(self)
        self.rows = [
            (OLD_ID, UNHEARD, BOT, "own", "alpha own"),
            (TODAY_ID, HEARD, PEER, "other", "bravo heard"),
            (TODAY_ID + 1, UNHEARD, PEER, "other", "charlie unheard"),
            (TODAY_ID + 2, "@" + PEER, PEER, "other", "delta private"),
            (TODAY_ID + 3, "-", BOT, "own", "echo everywhere"),
        ]
        self.s.seed_recent(self.rows)
        self.before = self.s.lines_of("recent.txt")

    def test_by_the_live_lines_rules(self):
        self.s.open_window()
        for row in self.rows:
            expected = 0 if row[4] == "charlie unheard" else 1
            self.assertEqual(self.s.drawn(row[4]), expected, row[4])

    def test_every_channel_listened_on_with_all_ticked(self):
        self.s.mirc.tables["dccore"]["chat.all"] = "1"
        self.s.open_window()
        self.assertEqual(self.s.drawn("charlie unheard"), 1)

    def test_in_grey_without_lighting_the_button_and_between_the_markers(self):
        self.s.open_window()
        texts = [line["text"] for line in self.s.chat_lines]
        start = texts.index("Earlier, from the history file:")
        end = texts.index("End of the history file.")
        replayed = self.s.chat_lines[start + 1:end]
        self.assertEqual(len(replayed), 4)
        for line in replayed:
            self.assertEqual(line["colour"], "14")
            self.assertNotIn("m", line["switches"])
        self.assertTrue(replayed[0]["text"].startswith(asctime(OLD_ID // 1000, "yyyy-mm-dd HH:nn") + "\xa0"))
        self.assertTrue(replayed[1]["text"].startswith(asctime(TODAY_ID // 1000, "HH:nn") + "\xa0" + HEARD))
        self.assertTrue(replayed[3]["text"].startswith(asctime((TODAY_ID + 3) // 1000, "HH:nn") + "\xa0* "))

    def test_never_written_again_never_unread_never_flashed_never_shown_as_live(self):
        self.s.open_window()
        self.assertEqual(self.s.lines_of("recent.txt"), self.before)
        self.assertEqual(sorted(os.listdir(self.s.folder)), ["recent.txt"])
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertEqual(self.s.mirc.flashes, [])
        for name in ("dccore.chat.show", "dccore.chat.hist.add", "dccore.chat.unread.add"):
            self.assertEqual(self.s.calls(name), [], name)

    def test_its_hidden_window_is_gone_afterwards(self):
        self.s.open_window()
        self.assertEqual(sorted(self.s.mirc.windows), [CHAT.lower()])

    def test_the_newest_id_read_counts_as_drawn(self):
        self.s.mirc.tables["dccore.live"]["chat.last"] = "5"
        self.s.open_window()
        self.assertEqual(self.s.live("chat.last"), str(TODAY_ID + 3))
        self.s.feed(TODAY_ID + 2, "@" + PEER, PEER, "delta private")
        self.assertEqual(self.s.drawn("delta private"), 1)

    def test_a_later_id_already_drawn_is_not_moved_back(self):
        self.s.mirc.tables["dccore.live"]["chat.last"] = str(TODAY_ID + 1000)
        self.s.open_window()
        self.assertEqual(self.s.live("chat.last"), str(TODAY_ID + 1000))

    def test_the_last_200_lines(self):
        self.s.seed_recent([(TODAY_ID + i, HEARD, PEER, "other", "row-%d." % i) for i in range(250)])
        self.s.open_window()
        self.assertEqual(self.s.drawn("row-49."), 0)
        self.assertEqual(self.s.drawn("row-50."), 1)
        self.assertEqual(self.s.drawn("row-249."), 1)

    def test_no_markers_when_nothing_is_replayed(self):
        self.s.seed_recent([(TODAY_ID, UNHEARD, PEER, "other", "charlie unheard")])
        self.s.open_window()
        texts = [line["text"] for line in self.s.chat_lines]
        self.assertNotIn("Earlier, from the history file:", texts)
        self.assertNotIn("End of the history file.", texts)

    def test_in_every_way_the_window_opens(self):
        for how in ([], ["quiet"], ["start"]):
            s = Session(self)
            s.seed_recent(self.rows)
            s.mirc.call("dccore.chat.window", how)
            self.assertEqual(s.drawn("bravo heard"), 1, how)


class TheHistoryCanBeTurnedOff(unittest.TestCase):

    def test_it_is_on_by_default_and_a_choice_made_stays(self):
        s = Session(self)
        self.assertEqual(s.mirc.tables["dccore"].get("chat.history"), "1")
        self.assertEqual(s.mirc.tables["dccore"].get("chat.flash"), "1")
        s.mirc.tables["dccore"]["chat.history"] = "0"
        s.mirc.tables["dccore"]["chat.flash"] = "0"
        s.defaults()
        self.assertEqual(s.mirc.tables["dccore"].get("chat.history"), "0")
        self.assertEqual(s.mirc.tables["dccore"].get("chat.flash"), "0")

    def test_off_writes_nothing_and_replays_nothing(self):
        s = Session(self)
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "from before")])
        s.mirc.tables["dccore"]["chat.history"] = "0"
        s.feed(TODAY_ID + 1, HEARD, PEER, "while off")
        self.assertEqual(s.drawn("from before"), 0)
        self.assertEqual(s.drawn("while off"), 1)
        self.assertEqual(sorted(os.listdir(s.folder)), ["recent.txt"])
        self.assertEqual(len(s.lines_of("recent.txt")), 1)

    def test_the_menu_toggle_turns_it_off_and_on_and_says_so(self):
        s = Session(self)
        s.mirc.call("dccore.chat.hist.toggle", [])
        self.assertEqual(s.mirc.tables["dccore"]["chat.history"], "0")
        self.assertTrue(s.chat_lines[-1]["text"].startswith("No longer keeping a history file"))
        s.mirc.call("dccore.chat.hist.toggle", [])
        self.assertEqual(s.mirc.tables["dccore"]["chat.history"], "1")
        self.assertEqual(s.chat_lines[-1]["text"],
                         "Keeping a history file in %s\\DCCore-Chat." % s.logs)
        s.mirc.call("dccore.chat.flash.toggle", [])
        self.assertEqual(s.mirc.tables["dccore"]["chat.flash"], "0")

    def test_open_the_folder_makes_it_and_runs_it_quoted(self):
        s = Session(self)
        s.mirc.call("dccore.chat.hist.open", [])
        self.assertTrue(os.path.isdir(s.folder))
        self.assertEqual(s.mirc.ran, ['"%s\\DCCore-Chat"' % s.logs])


class RecentTxtIsCutBack(unittest.TestCase):

    def fill(self, count):
        s = Session(self)
        s.seed_recent([(TODAY_ID + i, HEARD, PEER, "other", "row-%d." % i) for i in range(count - 1)])
        s.mirc.tables["dccore.live"]["chat.last"] = str(TODAY_ID + count)
        s.open_window()
        s.feed(TODAY_ID + count + 1, HEARD, PEER, "the last row.")
        return s

    def test_to_the_last_200_once_it_passes_400(self):
        s = self.fill(401)
        rows = s.lines_of("recent.txt")
        self.assertEqual(len(rows), 200)
        self.assertTrue(rows[-1].endswith("the last row."))
        self.assertTrue(rows[0].endswith(" row-201."))
        self.assertEqual(sorted(s.mirc.windows), [CHAT.lower()])

    def test_not_at_400(self):
        self.assertEqual(len(self.fill(400).lines_of("recent.txt")), 400)


class TheUnreadCount(unittest.TestCase):

    def setUp(self):
        self.s = Session(self)

    def test_a_line_from_another_bot_counts_in_the_title_and_flashes(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "one")
        self.s.feed(TODAY_ID + 1, HEARD, PEER, "two")
        self.assertEqual(self.s.live("chat.unread"), "2")
        self.assertTrue(self.s.title.startswith("DCCore Chat \xb7 public \xb7 "), self.s.title)
        self.assertTrue(self.s.title.endswith(" \xb7 2 unread"), self.s.title)
        self.assertEqual(self.s.mirc.flashes, [(CHAT, "DCCore Chat \xb7 1 unread"), (CHAT, "DCCore Chat \xb7 2 unread")])

    def test_not_your_own_line_nor_a_remark(self):
        self.s.feed(TODAY_ID, HEARD, BOT, "mine")
        self.s.feed(TODAY_ID + 1, HEARD, "*", "a remark")
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertNotIn("unread", self.s.title)
        self.assertEqual(self.s.mirc.flashes, [])

    def test_not_while_the_window_is_the_active_one(self):
        self.s.open_window()
        self.assertEqual(self.s.mirc.active, CHAT)
        self.s.feed(TODAY_ID, HEARD, PEER, "seen as it comes")
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertEqual(self.s.mirc.flashes, [])

    def test_flash_off_keeps_the_count_without_the_flashing(self):
        self.s.mirc.tables["dccore"]["chat.flash"] = "0"
        self.s.feed(TODAY_ID, HEARD, PEER, "quietly")
        self.assertEqual(self.s.live("chat.unread"), "1")
        self.assertEqual(self.s.mirc.flashes, [])

    def test_activating_the_window_clears_it_and_another_window_does_not(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "waiting")
        self.s.mirc.active = "#elsewhere"
        self.s.mirc.event("on *:ACTIVE:*:")
        self.assertEqual(self.s.live("chat.unread"), "1")
        self.s.mirc.active = CHAT
        self.s.mirc.event("on *:ACTIVE:*:")
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertNotIn("unread", self.s.title)

    def test_typing_in_the_window_clears_it_and_still_sends(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "waiting")
        self.s.mirc.event("on *:INPUT:@DCCore-Chat:", ["hi", "back"])
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertEqual(self.s.mirc.sent, [("dccore.chat.say", "hi back")])

    def test_closing_the_window_clears_it(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "waiting")
        self.s.mirc.call("dccore.chat.unread.add", [])   # a second one
        self.assertEqual(self.s.live("chat.unread"), "2")
        self.s.mirc.event("on *:CLOSE:@DCCore-Chat:")
        self.assertEqual(self.s.live("chat.unread"), "")

    def test_nothing_on_any_of_these_paths_sends_anything(self):
        self.s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "from before")])
        self.s.feed(TODAY_ID + 1, HEARD, PEER, "one")
        self.s.feed(TODAY_ID + 2, HEARD, BOT, "mine")
        self.s.feed(TODAY_ID + 3, "@" + PEER, PEER, "private")
        self.s.mirc.event("on *:ACTIVE:*:")
        self.s.mirc.event("on *:APPACTIVE:")
        self.s.mirc.event("on *:CLOSE:@DCCore-Chat:")
        self.assertEqual(self.s.mirc.sent, [])


class WithMircInTheBackground(unittest.TestCase):
    """$active stays the window you left while mIRC is in the background:
    a line arriving then is unread even when that window is the chat."""

    def setUp(self):
        self.s = Session(self)
        self.s.open_window()
        self.assertEqual(self.s.mirc.active, CHAT)
        self.s.mirc.appactive = False

    def test_a_line_counts_and_flashes_although_the_chat_is_the_active_window(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "while away")
        self.assertEqual(self.s.live("chat.unread"), "1")
        self.assertEqual(self.s.mirc.flashes, [(CHAT, "DCCore Chat \xb7 1 unread")])

    def test_coming_back_clears_it_only_onto_the_chat_window(self):
        self.s.feed(TODAY_ID, HEARD, PEER, "while away")
        self.s.mirc.event("on *:APPACTIVE:")             # mIRC going to the background
        self.assertEqual(self.s.live("chat.unread"), "1")
        self.s.mirc.appactive = True
        self.s.mirc.active = "#elsewhere"
        self.s.mirc.event("on *:APPACTIVE:")             # back, onto another window
        self.assertEqual(self.s.live("chat.unread"), "1")
        self.s.mirc.active = CHAT
        self.s.mirc.event("on *:APPACTIVE:")             # back, onto the chat
        self.assertEqual(self.s.live("chat.unread"), "")
        self.assertNotIn("unread", self.s.title)


CLOCK = (NOW + 300) * 1000                # dccore.chat.hist.clock at the fake $ctime
FUTURE = CLOCK + 60000


class AnIdFromTheFutureMovesNothing(unittest.TestCase):
    """An id past the clock (a bot whose clock ran fast, this PC's put back)
    would hold chat.last above every line to come."""

    def setUp(self):
        self.s = Session(self)

    def test_the_floor_skips_it_and_a_present_line_is_drawn_and_written(self):
        self.s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "from before"),
                            (FUTURE, HEARD, PEER, "other", "from the future")])
        self.s.feed(TODAY_ID + 5, HEARD, PEER, "a present line")
        self.assertEqual(self.s.drawn("from the future"), 1, "still drawn")
        self.assertEqual(self.s.drawn("a present line"), 1)
        self.assertEqual(self.s.lines_of("recent.txt")[-1].split(" ")[0], str(TODAY_ID + 5))
        self.assertEqual(self.s.live("chat.last"), str(TODAY_ID + 5))

    def test_the_replay_draws_it_and_does_not_move_to_it(self):
        self.s.mirc.tables["dccore.live"]["chat.last"] = str(TODAY_ID)
        self.s.seed_recent([(TODAY_ID + 1, HEARD, PEER, "other", "from before"),
                            (FUTURE, HEARD, PEER, "other", "from the future")])
        self.s.open_window()
        self.assertEqual(self.s.drawn("from the future"), 1)
        self.assertEqual(self.s.live("chat.last"), str(TODAY_ID + 1))
        self.s.feed(TODAY_ID + 2, HEARD, PEER, "a present line")
        self.assertEqual(self.s.drawn("a present line"), 1)
        self.assertEqual(self.s.lines_of("recent.txt")[-1].split(" ")[0], str(TODAY_ID + 2))

    def test_an_id_at_the_clock_still_counts(self):
        self.s.seed_recent([(CLOCK, HEARD, PEER, "other", "at the clock")])
        self.s.feed(CLOCK, HEARD, PEER, "at the clock")          # the floor
        self.assertEqual(self.s.live("chat.last"), str(CLOCK))
        self.assertEqual(self.s.chat_lines, [])
        s = Session(self)
        s.seed_recent([(CLOCK, HEARD, PEER, "other", "at the clock")])
        s.open_window()                                          # the replay
        self.assertEqual(s.live("chat.last"), str(CLOCK))


class TheWindowSaysWhereTheHistoryIs(unittest.TestCase):

    def intro(self, history):
        s = Session(self)
        s.mirc.tables["dccore"]["chat.history"] = history
        s.open_window()
        return s, [line["text"] for line in s.chat_lines]

    def test_with_the_history_on_right_after_public_naming_the_folder(self):
        s, lines = self.intro("1")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("Public: "))
        self.assertTrue(lines[1].startswith("What this window shows, private conversations too, is kept in "))
        self.assertIn(s.logs + "\\DCCore-Chat ", lines[1])

    def test_not_with_the_history_off(self):
        _s, lines = self.intro("0")
        self.assertEqual(len(lines), 3)
        self.assertFalse([line for line in lines if "is kept in" in line])


class TheHiddenWindowAndTheCutHaveOneName(unittest.TestCase):

    def test_the_buffer_window_is_named_once(self):
        code = [line.strip() for line in script_text().split("\n")
                if line.strip() and not line.strip().startswith(";")]
        self.assertEqual([line for line in code if "@dccore.chat.buf" in line],
                         ["alias dccore.chat.hist.buf { return @dccore.chat.buf }"])

    def test_a_long_live_line_is_cut_and_written_whole(self):
        s = Session(self)
        s.feed(TODAY_ID, HEARD, PEER, "y" * 400)
        s.feed(TODAY_ID + 1, HEARD, PEER, "x" * 401)
        self.assertEqual(s.drawn(" " + "y" * 400), 1)
        self.assertEqual(s.drawn(" " + "x" * 397 + "..."), 1)
        self.assertTrue(s.lines_of("recent.txt")[-1].endswith(" " + "x" * 401))

    def test_a_long_replayed_line_is_cut_the_same(self):
        s = Session(self)
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "x" * 401)])
        s.open_window()
        self.assertEqual(s.drawn(" " + "x" * 397 + "..."), 1)


class TheReviewersSurvivors(unittest.TestCase):
    """Four mutants the first round of tests let through."""

    def test_a_failing_replay_stops_in_the_replay(self):
        """Its :error resets the error and closes its hidden window, so the
        window's opener goes on."""
        s = Session(self)
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "boom-line")])
        s.mirc.fail_echo = "boom-line"
        s.open_window()
        self.assertEqual(sorted(s.mirc.windows), [CHAT.lower()])
        self.assertIn(("stub", "dccore.chat.peers.redraw", ""), s.mirc.trace)

    def test_a_broken_row_of_recent_txt_is_neither_drawn_nor_counted(self):
        s = Session(self)
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "a whole row"), (TODAY_ID + 9, HEARD, PEER)])
        s.open_window()
        texts = [line["text"] for line in s.chat_lines]
        replayed = texts[texts.index("Earlier, from the history file:") + 1:texts.index("End of the history file.")]
        self.assertEqual(len(replayed), 1)
        self.assertTrue(replayed[0].endswith("a whole row"))
        self.assertEqual(s.live("chat.last"), str(TODAY_ID))

    def test_a_command_typed_in_the_window_clears_the_count_too(self):
        s = Session(self)
        s.feed(TODAY_ID, HEARD, PEER, "waiting")
        s.mirc.event("on *:INPUT:@DCCore-Chat:", ["/clear"])
        self.assertEqual(s.live("chat.unread"), "")
        self.assertEqual(s.mirc.sent, [])

    def test_the_floor_is_read_once_a_session(self):
        """Read again later it would move chat.last back to the file's last
        line, below a line that was counted but never written - and the
        bot's next replay would draw that line after all."""
        s = Session(self)
        s.seed_recent([(TODAY_ID, HEARD, PEER, "other", "from before")])
        s.feed(TODAY_ID + 1, UNHEARD, PEER, "not listened then")
        s.mirc.tables["dccore"]["chat.listen"] = HEARD + " " + UNHEARD
        s.feed(TODAY_ID + 1, UNHEARD, PEER, "not listened then")     # the bot's replay
        s.feed(TODAY_ID + 2, HEARD, PEER, "next")
        self.assertEqual(s.drawn("not listened then"), 0)
        self.assertEqual(len(s.calls("dccore.chat.hist.floor")), 1)


class TheTitle(unittest.TestCase):

    def test_says_public_first_and_the_count_last(self):
        s = Session(self)
        s.open_window()
        self.assertTrue(s.title.startswith("DCCore Chat \xb7 public \xb7 typing sends to "), s.title)
        self.assertNotIn("unread", s.title)
        s.mirc.tables["dccore.live"]["chat.unread"] = "3"
        s.mirc.call("dccore.chat.title", [])
        self.assertTrue(s.title.endswith("every channel with other DCCore bots \xb7 3 unread"), s.title)


class TheMenu(unittest.TestCase):
    """Three items between "Message this bot privately" and Clear window,
    each between separators; a command carries no nick, channel or line."""

    ITEMS = [
        "-",
        "$iif($dccore.opt(chat.history),$style(1)) Keep a history file:dccore.chat.hist.toggle",
        "$iif($dccore.opt(chat.flash),$style(1)) Flash on a new line:dccore.chat.flash.toggle",
        "$dccore.mi(folder) Open the history folder:dccore.chat.hist.open",
        "-",
    ]

    def setUp(self):
        self.menu = statements(code_body(script_text(), "menu @DCCore-Chat"))

    def test_the_group_and_where_it_sits(self):
        private = next(i for i, row in enumerate(self.menu) if "Message this bot privately" in row)
        clear = next(i for i, row in enumerate(self.menu) if "Clear window:" in row)
        self.assertEqual(self.menu[private + 1:clear], self.ITEMS)

    def test_each_command_is_a_bare_alias_that_exists(self):
        text = script_text()
        for row in self.ITEMS[1:-1]:
            command = row.rsplit(":", 1)[1]
            self.assertRegex(command, r"^dccore\.chat\.[a-z.]+$")
            self.assertIsNotNone(re.search(r"(?m)^alias %s \{" % re.escape(command), text), command)

    def test_the_icon_is_one_the_file_has(self):
        names = re.search(r"(?m)^alias dccore\.ico\.names \{ return ([a-z ]+) \}$", script_text()).group(1).split()
        for row in self.menu:
            for mark in re.findall(r"\$dccore\.mi\(([a-z]+)\)", row):
                self.assertIn(mark, names, row)


class WhereThingsLive(unittest.TestCase):

    def test_the_replays_hidden_window_is_not_in_the_window_opener(self):
        """StartDoesNotConnect reads dccore.chat.window's own statements;
        the opener calls the replay, and the replay owns its window."""
        opener = statements(code_body(script_text(), "alias dccore.chat.window"))
        # then the history's limits, once a day (#1401)
        self.assertEqual(opener[-3:], ["dccore.chat.hist.replay", "dccore.chat.hist.daily", "dccore.chat.peers.redraw"])
        self.assertFalse([s for s in opener if s.startswith("window -h") or "loadbuf" in s])

    def test_the_docs_say_where_the_files_are_and_how_to_stop_them(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            doc = handle.read()
        history = doc[doc.index("- **History (#1393):**"):doc.index("- **Unread:**")]
        # where, what (private too), that it is on, how the files are laid out
        # (a file per month since #1401) and how to stop it
        for phrase in ("`DCCore-Chat\\`", "$logdir", "**on by default**", "**private conversations included**",
                       "a file per\n  month", "*Keep a history file*", "*Open the history folder*"):
            self.assertIn(phrase, history)
        unread = doc[doc.index("- **Unread:**"):]
        unread = unread[:unread.index("\n\n")]
        for phrase in ("in the background", "*Flash on a new line*"):
            self.assertIn(phrase, unread)
        # The fragment until a release rolls it, the changelog's bullet after.
        public = support.changelog_entry(self, 1393, public=True,
                                         opening="- **New (mIRC): DCCore Chat keeps a history")
        self.assertTrue(public.startswith("- **"))
        for phrase in ("`DCCore-Chat\\`", "on by default", "private conversations included", "100 days and 100 MB",
                       "*Keep a history file*", "*Flash on a new line*", "/reload -rs dccore.mrc"):
            self.assertIn(phrase, public)


if __name__ == "__main__":
    unittest.main()
