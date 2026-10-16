"""The control panel opens faster, and leaks less (#1382, #1381).

dccore.mrc 1.20 made the control panel's open some 3 s: hundreds of menus
filled a line at a time, every page's DCX frames and every table's listview
made at open (each listview some GDI objects DCX never gives back, #1381),
and then every DCX text asked for its font and given it again without
ClearType. Now:

- a menu's lines go in with one didtok, and come out exactly as before -
  line N still means what it meant, since Apply reads the line selected;
- a page's DCX frames are made the first time it is shown, a table's
  listview the first time anything needs it, and a page or table never made
  is never touched (a DCX command on a missing id halts the script);
- a DCX text gets its final font as it is made.

mIRC cannot run here. Rather than search the script for the statements,
these run them: a small evaluator below reads the aliases out of the script
and executes them - variables, $gettok and friends, if/while, did, didtok
and DCX's commands against a pretend DCX that halts, as DCX does, on an id
that does not exist or one made twice. What it cannot run (geometry, the
dlls) is replaced by stubs that say what they were asked.
"""

import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_the_mirc_settings_window import code, generated_data, untext  # noqa: E402



# ---------------------------------------------------------------------------
# A small mSL evaluator: enough of mIRC's scripting language to run the
# aliases these tests are about, nothing more. Anything it does not know is
# an error, so a change of shape in the script shows up as a failure here
# rather than as a silently wrong answer.
# ---------------------------------------------------------------------------

class Halt(Exception):
    """An error in mIRC: the script that met it stops, with what called it."""


class _Return(Exception):
    def __init__(self, value):
        Exception.__init__(self, value)
        self.value = value


def aliases_of(text):
    """{name: body between its braces} for every `alias NAME {` of the code."""
    found = {}
    for match in re.finditer(r"(?m)^alias (\S+) \{", text):
        start = text.index("{", match.start())
        found[match.group(1)] = text[start + 1:_close(text, start, "{", "}")]
    return found


def _close(text, start, opening, closing):
    depth = 0
    for j in range(start, len(text)):
        depth += text[j] == opening
        depth -= text[j] == closing
        if depth == 0:
            return j
    raise AssertionError("unbalanced %s at %d" % (opening, start))


def split_top(text, separator):
    """`text` split on `separator` wherever no parenthesis is open."""
    parts, depth, current, i = [], 0, [], 0
    while i < len(text):
        ch = text[i]
        if depth == 0 and text.startswith(separator, i):
            parts.append("".join(current))
            current = []
            i += len(separator)
            continue
        depth += ch == "("
        depth -= ch == ")"
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def parse(text):
    """mSL text as nodes: ("cmd", text), ("if"|"elseif"|"while", condition,
    nodes), ("else", None, nodes)."""
    nodes, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n|":
            i += 1
        if i >= n:
            break
        head = re.match(r"(if|elseif|while) *\(", text[i:])
        if head:
            k, depth = i + len(head.group(1)), 0
            while not (text[k] == "{" and depth == 0):
                depth += text[k] == "("
                depth -= text[k] == ")"
                k += 1
            end = _close(text, k, "{", "}")
            nodes.append((head.group(1), text[i + len(head.group(1)):k].strip(), parse(text[k + 1:end])))
            i = end + 1
            continue
        if re.match(r"else *\{", text[i:]):
            k = text.index("{", i)
            end = _close(text, k, "{", "}")
            nodes.append(("else", None, parse(text[k + 1:end])))
            i = end + 1
            continue
        k, depth = i, 0
        while k < n and text[k] != "\n" and not (text[k] == "|" and depth == 0):
            depth += text[k] == "("
            depth -= text[k] == ")"
            k += 1
        nodes.append(("cmd", text[i:k].strip()))
        i = k
    return nodes


_PARSED = {}


def parsed(body):
    if body not in _PARSED:
        _PARSED[body] = parse(body)
    return _PARSED[body]


def walk(nodes, conditions=()):
    """Every command with the conditions of the blocks around it, and
    whether a while loop is one of them."""
    for node in nodes:
        if node[0] == "cmd":
            yield node[1], conditions
        else:
            yield from walk(node[2], conditions + ((node[0], node[1]),))


def tokens(text, separator):
    return [t for t in text.split(chr(separator)) if t != ""]


def gettok(text, n, separator):
    toks = tokens(text, separator)
    n = n.strip()
    if n == "0":
        return str(len(toks))
    if "-" in n[1:]:
        first, last = n.split("-", 1)
        first = int(first)
        last = int(last) if last else len(toks)
        return chr(separator).join(toks[first - 1:last])
    n = int(n)
    if n < 0:
        n = len(toks) + n + 1
    return toks[n - 1] if 1 <= n <= len(toks) else ""


def number(text):
    try:
        return float(text)
    except ValueError:
        return None


def show(value):
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def truthy(value):
    return value not in ("", "0", "$false")


class Control(object):
    def __init__(self):
        self.lines, self.sel, self.state, self.flags = [], 0, 0, []


class Mirc(object):
    """mIRC and DCX as far as the aliases under test need them.

    `data` is dccore.swm (the generated layout), `sws` dccore.sws, `opts`
    $dccore.opt, `stubs` {alias: function(args)} for what is not run."""

    FONT = "+d ansi 8 Segoe UI"

    def __init__(self, source=None, data=None, sws=None, opts=None, stubs=None):
        self.aliases = dict(ALIASES) if source is None else aliases_of(source)
        self.data = DATA if data is None else data
        self.sws = dict(sws or {})
        self.opts = dict(opts or {})
        self.stubs = dict(stubs or {})
        self.tables = {"dccore": {}}
        self.controls = {}
        self.dcx = set()           # DCX ids made
        self.kinds = {}            # their types
        self.open = True           # $dialog(dccore.set)
        self.dcx_log = []          # (id, what) for every DCX command
        self.font_asked = []
        self.fonts = {}
        self.timers = []
        self.did_a = 0
        self.didtoks = []
        self.calls = []
        self.stack = []            # the aliases running
        self.selections = []       # (control, flags, stack) of did -c/-u/-d
        self.sel_reads = []        # (id, stack) of $did(dccore.set,id).sel
        self.regml = {}

    # -- what the script asks ---------------------------------------------------

    def control(self, cid):
        return self.controls.setdefault(int(cid), Control())

    def lines(self, cid):
        return list(self.control(cid).lines)

    def selected(self, cid):
        c = self.control(cid)
        return c.lines[c.sel - 1] if 1 <= c.sel <= len(c.lines) else None

    def run(self, name, args=""):
        """/name args, as a command."""
        return self.call(name, args.split(" ") if args else [], " ")

    def ident(self, text, local=None):
        """`text` evaluated, as mIRC evaluates a command's parameters."""
        return self.evaluate(text, local or {"args": [], "sep": " ", "vars": {}})

    def statement(self, text, local=None):
        self.execute(parse(text), local or {"args": [], "sep": " ", "vars": {}})

    # -- aliases --------------------------------------------------------------

    def call(self, name, args, separator):
        self.calls.append(name)
        self.stack.append(name)
        try:
            return self.call_one(name, args, separator)
        finally:
            self.stack.pop()

    def call_one(self, name, args, separator):
        if name in self.stubs:
            result = self.stubs[name](args)
            return "" if result is None else str(result)
        if name not in self.aliases:
            raise AssertionError("no alias %s in the script, and no stub for it" % name)
        local = {"args": args, "sep": separator, "vars": {}}
        try:
            self.execute(parsed(self.aliases[name]), local)
        except _Return as done:
            return done.value
        return ""

    def arg(self, local, spec):
        args, sep = local["args"], local["sep"]
        if spec.endswith("-"):
            return sep.join(args[int(spec[:-1]) - 1:])
        n = int(spec)
        if n == 0:
            return str(len(args))
        return args[n - 1] if n <= len(args) else ""

    # -- statements -----------------------------------------------------------

    def execute(self, nodes, local):
        taken = None
        for node in nodes:
            kind = node[0]
            if kind == "cmd":
                taken = None
                self.command(node[1], local)
            elif kind == "if":
                taken = self.condition(node[1], local)
                if taken:
                    self.execute(node[2], local)
            elif kind == "elseif":
                if taken is None:
                    raise AssertionError("elseif with no if before it")
                if not taken and self.condition(node[1], local):
                    taken = True
                    self.execute(node[2], local)
            elif kind == "else":
                if taken is None:
                    raise AssertionError("else with no if before it")
                if not taken:
                    self.execute(node[2], local)
                taken = None
            elif kind == "while":
                taken = None
                rounds = 0
                while self.condition(node[1], local):
                    self.execute(node[2], local)
                    rounds += 1
                    if rounds > 10000:
                        raise AssertionError("a loop that does not end: %s" % node[1])

    def command(self, text, local):
        word, _, rest = text.partition(" ")
        if word.startswith("%"):
            if not rest.startswith("="):
                raise AssertionError("not an assignment: %s" % text)
            local["vars"][word] = self.evaluate(rest[1:].strip(), local)
            return
        if word == "var":
            parts = split_top(rest, ",")
            merged = []
            for part in parts:
                if part.strip().startswith("%") or not merged:
                    merged.append(part)
                else:
                    merged[-1] += "," + part
            for part in merged:
                name, eq, value = part.strip().partition(" = ")
                local["vars"][name.strip()] = self.evaluate(value.strip(), local) if eq else ""
            return
        if word == "inc":
            name = rest.split()[0]
            local["vars"][name] = show(float(local["vars"].get(name) or 0) + 1)
            return
        if word == "return":
            raise _Return(self.evaluate(rest, local))
        if word in ("noop", "reseterror"):
            self.evaluate(rest, local)
            return
        if word.startswith(".timer"):
            self.timers.append((word[len(".timer"):], self.evaluate(rest, local)))
            return
        line = self.evaluate(rest, local)
        if word == "hadd":
            table, key, value = (line.split(" ", 2) + ["", ""])[:3]
            if not value:
                raise Halt("hadd with nothing to add: %s" % text)
            (self.sws if table == "dccore.sws" else self.tables.setdefault(table, {}))[key] = value
        elif word == "hdel":
            table, key = line.split(" ", 1)
            (self.sws if table == "dccore.sws" else self.tables.setdefault(table, {})).pop(key, None)
        elif word == "did":
            self.did(line, text)
        elif word == "didtok":
            dialog, cid, separator, items = (line.split(" ", 3) + [""])[:4]
            if dialog != "dccore.set":
                raise AssertionError(text)
            if items == "":
                # /didtok with no list is "insufficient parameters": it halts
                raise Halt("didtok with nothing to add: %s" % text)
            self.didtoks.append(int(cid))
            self.control(cid).lines.extend(tokens(items, int(separator)))
        elif word == "dccore.dcx":
            self.dcx_command(line, text)
        elif word in self.aliases or word in self.stubs:
            self.call(word, line.split(" ") if line else [], " ")
        else:
            raise AssertionError("the evaluator does not know the command: %s" % text)

    def did(self, line, text):
        flags, dialog, cids, *rest = line.split(" ", 3)
        if dialog != "dccore.set":
            raise AssertionError(text)
        rest = rest[0] if rest else ""
        for cid in tokens(cids, 44):
            self.did_one(flags, self.control(cid), rest, text)

    def did_one(self, flags, c, rest, text):
        if any(f in flags for f in "cud"):
            self.selections.append((c, flags, tuple(self.stack)))
        if "r" in flags:
            c.lines, c.sel = [], 0
        if "a" in flags:
            if rest == "":
                raise Halt("did -a with nothing to add: %s" % text)
            self.did_a += 1
            c.lines.append(rest)
        if "c" in flags:
            if rest == "":
                c.state = 1
            else:
                # Windows' CB_SETCURSEL: a line the menu does not have
                # clears the selection
                n = int(float(rest))
                c.sel = n if 1 <= n <= len(c.lines) else 0
        if "u" in flags:
            c.state, c.sel = 0, 0
        if "d" in flags:
            n = int(rest)
            del c.lines[n - 1]
            c.sel = 0 if c.sel == n else c.sel - (c.sel > n)
        c.flags.append(flags)

    # -- DCX ------------------------------------------------------------------

    def ids(self, spec):
        if re.match(r"^\d+-\d+$", spec):
            first, last = (int(n) for n in spec.split("-"))
            return list(range(first, last + 1))
        if not re.match(r"^\d+$", spec):
            raise AssertionError("not a DCX id: %r" % spec)
        return [int(spec)]

    def dcx_command(self, line, text):
        what, dialog, rest = (line.split(" ", 2) + [""])[:3]
        if dialog != "dccore.set":
            raise AssertionError(text)
        if what == "Mark":
            self.dcx_log.append((None, line))
            return
        if what == "xdialog":
            parts = rest.split(" ")
            if parts[0] != "-c":
                self.dcx_log.append((None, rest))
                return
            cid = int(parts[1])
            if cid in self.dcx:
                raise Halt("DCX: id %d is in use (%s)" % (cid, text))
            self.dcx.add(cid)
            self.kinds[cid] = parts[2]
            self.dcx_log.append((cid, "made " + parts[2]))
            return
        if what != "xdid":
            raise AssertionError(text)
        spec, _, flags = rest.partition(" ")
        for cid in self.ids(spec):
            if cid not in self.dcx:
                raise Halt("DCX: no id %d (%s)" % (cid, text))
            self.dcx_log.append((cid, flags))
            if flags.startswith("-f "):
                self.fonts[cid] = flags[3:]

    def dcx_ident(self, args):
        if args[0] != "_xdid":
            raise AssertionError("$dccore.dcx(%s)" % ",".join(args))
        dialog, cid, prop = args[1].split(" ", 2)
        cid = int(cid)
        if cid not in self.dcx:
            raise Halt("DCX: no id %d ($dccore.dcx(%s))" % (cid, ",".join(args)))
        self.dcx_log.append((cid, "asked " + prop))
        if prop == "font":
            self.font_asked.append(cid)
            return self.fonts.get(cid, self.FONT)
        return {"num": "0", "sel": ""}.get(prop, "")

    # -- conditions -----------------------------------------------------------

    OPS = ("==", "!=", "<=", ">=", "<", ">", "isnum", "!isnum", "isincs", "!isincs", "isin", "!isin")

    def condition(self, text, local):
        groups, operators, depth, start, i = [], [], 0, 0, 0
        while i < len(text):
            ch = text[i]
            if depth == 0 and text[i:i + 2] in ("&&", "||"):
                groups.append(text[start:i])
                operators.append(text[i:i + 2])
                i += 2
                start = i
                continue
            depth += ch == "("
            depth -= ch == ")"
            i += 1
        groups.append(text[start:])
        # mIRC works out every part of a condition, whatever the first gives
        values = [self.group(g.strip(), local) for g in groups]
        result = values[0]
        for operator, value in zip(operators, values[1:]):
            result = (result and value) if operator == "&&" else (result or value)
        return result

    def group(self, text, local):
        if text.startswith("(") and _close(text, 0, "(", ")") == len(text) - 1:
            text = text[1:-1].strip()
        if split_top(text, "&&")[1:] or split_top(text, "||")[1:]:
            return self.condition(text, local)
        words = split_top(text, " ")
        at = next((n for n, w in enumerate(words) if w in self.OPS), None)
        if at is None:
            negate = text.startswith("!")
            value = truthy(self.evaluate(text[1:] if negate else text, local))
            return not value if negate else value
        op = words[at]
        left = self.evaluate(" ".join(words[:at]), local)
        right = self.evaluate(" ".join(words[at + 1:]), local)
        negate = op.startswith("!") and op != "!="
        op = op.lstrip("!") if negate else op
        a, b = number(left), number(right)
        if op in ("==", "!="):
            same = (a == b) if a is not None and b is not None else left.lower() == right.lower()
            return same if op == "==" else not same
        if op in ("<", ">", "<=", ">="):
            if a is None or b is None:
                return False
            result = {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        elif op == "isnum":
            result = a is not None
            if result and right:
                low, high = right.split("-", 1)
                result = float(low) <= a and (high == "" or a <= float(high))
        elif op == "isincs":
            result = left in right
        elif op == "isin":
            result = left.lower() in right.lower()
        return not result if negate else result

    # -- evaluation -----------------------------------------------------------

    def evaluate(self, text, local):
        words = split_top(text, " ")
        out, glue = [], False
        for word in words:
            if word == "$+":
                glue = True
                continue
            value = self.word(word, local)
            if glue and out:
                out[-1] += value
            else:
                out.append(value)
            glue = False
        return " ".join(out)

    def word(self, word, local):
        out, i = [], 0
        while i < len(word):
            ch = word[i]
            # a word inside parentheses keeps its spaces: "(12 - %th)"
            at_start = i == 0 or word[i - 1] in "( "
            if ch == "%" and at_start:
                m = re.match(r"%[^\s,()]+", word[i:])
                out.append(local["vars"].get(m.group(0), ""))
                i += len(m.group(0))
            elif ch == "$" and at_start:
                value, i = self.identifier(word, i, local)
                out.append(value)
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    def identifier(self, word, i, local):
        m = re.match(r"\$(\+|\d+-?|[A-Za-z_][A-Za-z0-9_.]*)", word[i:])
        if not m:
            return "$", i + 1
        name = m.group(1).rstrip(".")
        j = i + 1 + len(name)
        args = None
        if j < len(word) and word[j] == "(":
            end = _close(word, j, "(", ")")
            args = split_top(word[j + 1:end], ",")
            j = end + 1
        prop = None
        p = re.match(r"\.([a-z]+)", word[j:])
        if p and args is not None:
            prop = p.group(1)
            j += len(p.group(0))
        return self.value(name, args, prop, local), j

    def value(self, name, args, prop, local):
        if re.match(r"^\d+-?$", name):
            return self.arg(local, name)
        raw = args or []
        if name == "iif":
            if self.condition(raw[0], local):
                return self.evaluate(raw[1], local)
            return self.evaluate(raw[2], local) if len(raw) > 2 else ""
        a = [self.evaluate(x, local) for x in raw]
        if name in ("null",):
            return ""
        if name in ("true", "false"):
            return "$" + name
        if name == "+":
            return "".join(a)
        if name == "chr":
            return chr(int(a[0]))
        if name == "calc":
            expression = a[0]
            if not re.match(r"^[0-9.+\-*/() ]*$", expression):
                raise AssertionError("$calc(%s)" % expression)
            expression = re.sub(r"(?<![\d.])0+(?=\d)", "", expression)
            return show(float(eval(expression or "0", {"__builtins__": {}})))
        if name == "round":
            return show(math.floor(float(a[0]) + 0.5))
        if name == "int":
            return show(float(int(float(a[0]))))
        if name == "gettok":
            return gettok(a[0], a[1], int(a[2]))
        if name == "numtok":
            return str(len(tokens(a[0], int(a[1]))))
        if name == "findtok":
            toks = [t.lower() for t in tokens(a[0], int(a[3]))]
            hits = [n + 1 for n, t in enumerate(toks) if t == a[1].lower()]
            return str(hits[int(a[2]) - 1]) if len(hits) >= int(a[2]) else ""
        if name == "istok":
            return "$true" if a[1].lower() in [t.lower() for t in tokens(a[0], int(a[2]))] else "$false"
        if name == "mid":
            start = int(a[1])
            s = a[0][start - 1:]
            return s[:int(a[2])] if len(a) > 2 else s
        if name == "left":
            return a[0][:int(a[1])]
        if name == "remove":
            s = a[0]
            for r in a[1:]:
                s = s.replace(r, "")
            return s
        if name == "replace":
            s = a[0]
            for old, new in zip(a[1::2], a[2::2]):
                s = s.replace(old, new)
            return s
        if name == "hget":
            if len(a) == 1:
                return "$true" if a[0] in ("dccore.swm", "dccore.sws") else ""
            return (self.sws if a[0] == "dccore.sws" else self.data if a[0] == "dccore.swm"
                    else self.tables.get(a[0], {})).get(a[1], "")
        if name == "did":
            c = self.control(a[1])
            if len(a) > 2:
                return c.lines[int(a[2]) - 1]
            if prop == "sel":
                self.sel_reads.append((int(a[1]), tuple(self.stack)))
            return {"sel": str(c.sel), "lines": str(len(c.lines)), "state": str(c.state),
                    "text": self.selected(a[1]) or ""}[prop]
        if name == "window":
            return ""
        if name == "regex":
            pattern = re.match(r"^/(.*)/$", raw[2]).group(1)
            m = re.search(pattern, a[1])
            self.regml[a[0]] = m.groups() if m else ()
            return "1" if m else "0"
        if name == "regml":
            groups = self.regml.get(a[0], ())
            return groups[int(a[1]) - 1] if int(a[1]) <= len(groups) else ""
        if name == "base":
            return str(int(float(a[0]))).zfill(int(a[3]) if len(a) > 3 else 0)
        if name == "isfile":
            return "$false"
        if name == "dialog":
            # the control panel is open
            return "$true" if a[0] == "dccore.set" and self.open else ""
        if name == "dccore.sw.m":
            return self.data.get(a[0], "")
        if name == "dccore.sw.s":
            return self.sws.get(a[0], "")
        if name == "dccore.sw.untext":
            # $regsubex: ~HH as its character, as the generator wrote it
            return untext(a[0])
        if name == "dccore.opt":
            return self.opts.get(a[0], "")
        if name == "dccore.dcx":
            return self.dcx_ident(a)
        if name in self.stubs or name in self.aliases:
            return self.call(name, a, ",")
        raise AssertionError("the evaluator does not know $%s" % name)


# ---------------------------------------------------------------------------

CODE = code()
ALIASES = aliases_of(CODE)
DATA = generated_data()
COLOURS = untext(DATA["colours"]).split(",")
# what dccore.sw.dcx makes at open that a page's showing speaks to: the page
# list and the page's title
OPEN = {int(DATA["dcxpages"].split()[0]), int(DATA["dcxtitle"].split()[0])}
# the tables, as the window's data lists them
TABLES = tuple(int(t) for t in DATA["lvs"].split())
SPAN_LABELS = {"name": "same as File names", "per": "per nick", "-1": "same as the line"}


def colour_list():
    """The sixteen colours as the old fill wrote them, one $gettok each."""
    listed = re.search(r"(?m)^alias dccore\.colours \{ return (.*) \}$", CODE).group(1)
    return [gettok(listed, str(n), 44) for n in range(1, 17)]


def calls_in(body):
    """The script's own aliases a body runs: a command's first word, or an
    identifier anywhere in it or in a condition around it. A name given as
    a parameter (DCX's Mark names the callback) is not a call."""
    found = set()
    for command, around in walk(parse(body)):
        first = command.split(" ", 1)[0]
        if first in ALIASES:
            found.add(first)
        for text in [command] + [condition or "" for _kind, condition in around]:
            found.update(name for name in re.findall(r"\$([A-Za-z_][A-Za-z0-9_.]*[A-Za-z0-9_])", text)
                         if name in ALIASES)
    return found


def reachable(name):
    seen, todo = set(), [name]
    while todo:
        current = todo.pop()
        for callee in calls_in(ALIASES[current]) - seen:
            seen.add(callee)
            todo.append(callee)
    return seen


def callers(name):
    return {n for n, body in ALIASES.items() if n != name and name in calls_in(body)}


def panel(**sws):
    """The control panel with DCX, its theme as dccore.sw.dcx found it."""
    state = {"dcx": "1", "dark": "0", "noct": "0", "face": "FACE", "line": "LINE", "light": "LIGHT"}
    state.update({k: str(v) for k, v in sws.items()})
    stubs = {
        "dccore.sw.dcxrect": lambda args: " ".join(args),
        "dccore.sw.dcxpoly": lambda args: "POLY",
        "dccore.theme.rgb": lambda args: "RGB." + args[0],
        "dccore.theme.dialogsdark": lambda args: "$true" if state["dark"] == "1" else "$false",
        "dccore.sw.untok": lambda args: args[0],
        "dccore.sw.local": lambda args: "$true",
    }
    mirc = Mirc(sws=state, stubs=stubs)
    mirc.dcx.update(OPEN)
    return mirc


def page_with(predicate):
    for page in range(1, int(DATA["pages"]) + 1):
        if predicate(page):
            return page
    raise AssertionError("no such page")


MIRC_SIDE_MENUS = tuple(range(1621, 1636, 2)) + tuple(range(1640, 1646)) + (1673,)
# this mIRC's settings, as dccore.ini might hold them
OPTS = {"statusmin": "5", "bg": "3", "col.name": "2", "col.console": "6", "col.head": "2", "col.term": "name",
        "col.nick": "per", "col.chan": "11", "show.request": "1", "col.request": "10", "col.queued": "7",
        "col.sends": "3", "col.fail": "4", "col.search": "12", "col.joins": "14", "col.bans": "5", "col.info": "0"}


def ident(value):
    return lambda args: value


def settings_window(opts=None, data=None, dcx=False):
    """The control panel as dccore.sw.init leaves it - every menu given its
    recipe or its lines (dccore.sw.combos, dccore.sw.opt.fill) - with no
    page shown yet."""
    stubs = {
        "dccore.fontsize": ident("9"), "dccore.dl.rows": ident("15"), "dccore.theme.mode": ident("auto"),
        "dccore.theme.icons": ident("$true"), "dccore.bot": ident(""), "dccore.st": ident(""),
        "dccore.sw.savelocal": ident("0"), "dccore.save": ident(None), "dccore.send": ident(None),
        "dccore.sw.dec": lambda args: args[0], "dccore.sw.enc": lambda args: args[0],
        "dccore.sw.local": ident("$true"),
    }
    mirc = Mirc(data=data, opts=dict(OPTS, **(opts or {})), stubs=stubs, sws={"dcx": "1"} if dcx else {})
    mirc.run("dccore.sw.combos")
    mirc.run("dccore.sw.opt.fill")
    return mirc


def lazy_ids(mirc):
    return {int(k[3:]) for k in mirc.sws if k.startswith("cb.")}


def page_ids(page):
    ids = []
    for part in range(1, int(DATA["p.%d.n" % page]) + 1):
        ids.extend(int(i) for i in DATA["p.%d.%d" % (page, part)].split(",") if i)
    return ids


PAGES = range(1, int(DATA["pages"]) + 1)


def show_all(mirc):
    for page in PAGES:
        mirc.run("dccore.sw.cb.page", str(page))


def menu_keys():
    """(key, id, kind) of every setting with a menu."""
    for name, value in sorted(DATA.items()):
        if name.startswith("k.") and value.split()[1] in ("choice", "tri", "colour"):
            yield name[2:], int(value.split()[0]), value.split()[1]


# values the bot may send for those: one a choice has and one it has not,
# true / false / unset, colours with and without a background, and ones a
# colour menu can only show as its 18th line, "As set"
VARIANTS = {
    "choice": lambda key, n: (DATA["ch." + key].split() + ["nosuch"])[n % (len(DATA["ch." + key].split()) + 1)],
    "tri": lambda key, n: ("true", "false", "")[n % 3],
    "colour": lambda key, n: ("", "\\x0305", "\\x0304,02", "\\x02bold", "\\x0399", "\\x03,05", "\\x0315,15")[n % 7],
}


def bot_values(mirc, round_):
    """The bot's values into the menus, as a settings snapshot puts them."""
    for n, (key, _cid, kind) in enumerate(menu_keys()):
        mirc.sws["v." + key] = "=" + VARIANTS[kind](key, n + round_)
        mirc.run("dccore.sw.fill", key)


def readers(mirc):
    """Everything that reads a menu's selection, as it answers now."""
    out = {"sig": mirc.ident("$dccore.sw.opt.sig")}
    for cid, own in ((1643, "name -1"), (1644, "-1 per"), (1645, "-1")):
        out["span.%d" % cid] = mirc.ident("$dccore.spanval(%d,%s)" % (cid, own))
    for key, _cid, _kind in menu_keys():
        for reader in ("shown", "shownenc", "value", "wire"):
            out["%s.%s" % (reader, key)] = mirc.ident("$dccore.sw.%s(%s)" % (reader, key))
    saved = dict(mirc.tables["dccore"])
    mirc.run("dccore.sw.opt.save")
    out.update(("save." + k, v) for k, v in mirc.tables["dccore"].items() if k.startswith("col.") or k == "bg")
    mirc.tables["dccore"] = saved
    return out


def menus(mirc, ids):
    return {cid: (mirc.lines(cid), mirc.control(cid).sel) for cid in ids}


class TheMenusAreFilledInOneGo(unittest.TestCase):
    """Every line where it was, so line N still means what it meant: Apply
    reads the line selected (col.X = sel - 1, bg = sel - 2, spanval) - and
    the same whether the menu was filled yet or not."""

    def test_the_colour_list_is_the_sixteen_colours_numbered(self):
        listed = colour_list()
        self.assertEqual(len(listed), 16)
        self.assertEqual([c.split(" ", 1)[0] for c in listed], ["%02d" % n for n in range(16)])
        self.assertEqual(Mirc().ident("$numtok($dccore.colours,44)"), "16")

    def saved(self, mirc, key):
        back = next(s.strip() for s in ALIASES["dccore.sw.opt.save"].split("\n")
                    if s.strip().startswith("hadd dccore %s " % key))
        mirc.statement(back)
        return mirc.tables["dccore"].pop(key)

    def test_a_colour_menu_has_the_sixteen_and_comes_back_as_it_was_saved(self):
        for colour in range(16):
            mirc = Mirc(opts={"col.name": str(colour)})
            mirc.statement("dccore.fillcombo 1640 $dccore.opt(col.name)")
            self.assertEqual(mirc.lines(1640), [], "filled when its page is shown")
            self.assertEqual(self.saved(mirc, "col.name"), str(colour), "never shown")
            mirc.run("dccore.sw.cb.fill", "1640")
            self.assertEqual(mirc.lines(1640), colour_list())
            self.assertTrue(mirc.selected(1640).startswith("%02d " % colour), colour)
            self.assertEqual(mirc.did_a, 0, "a line at a time")
            self.assertEqual(self.saved(mirc, "col.name"), str(colour))

    def test_the_background_menu_is_none_then_the_sixteen(self):
        for colour in range(-1, 16):
            mirc = Mirc(opts={"bg": str(colour)})
            mirc.run("dccore.fillbg", "1673")
            self.assertEqual(mirc.lines(1673), [])
            self.assertEqual(self.saved(mirc, "bg"), str(colour), "never shown")
            mirc.run("dccore.sw.cb.fill", "1673")
            self.assertEqual(mirc.lines(1673), ["none (mIRC's)"] + colour_list())
            self.assertEqual(mirc.did_a, 1, "only the none line is a line of its own")
            if colour < 0:
                self.assertEqual(mirc.selected(1673), "none (mIRC's)")
            else:
                self.assertTrue(mirc.selected(1673).startswith("%02d " % colour), colour)
            self.assertEqual(self.saved(mirc, "bg"), str(colour))

    def test_a_span_menu_has_its_own_lines_then_the_sixteen(self):
        fill = [s.strip() for s in ALIASES["dccore.sw.opt.fill"].split("\n")
                if s.strip().startswith("dccore.fillspan ")]
        self.assertEqual(len(fill), 3)
        for statement in fill:
            _cmd, cid, opt, own = statement.split(" ", 3)
            key = re.match(r"\$dccore\.opt\((.*)\)", opt).group(1)
            own = own.split()
            for saved in own + [str(c) for c in range(16)]:
                mirc = Mirc(opts={key: saved})
                mirc.statement(statement)
                self.assertEqual(mirc.lines(cid), [])
                self.assertEqual(self.saved(mirc, key), saved, "never shown")
                mirc.run("dccore.sw.cb.fill", cid)
                self.assertEqual(mirc.lines(cid), [SPAN_LABELS[o] for o in own] + colour_list(), statement)
                self.assertEqual(mirc.did_a, 0)
                if saved in own:
                    self.assertEqual(mirc.selected(cid), SPAN_LABELS[saved])
                else:
                    self.assertTrue(mirc.selected(cid).startswith("%02d " % int(saved)), saved)
                self.assertEqual(self.saved(mirc, key), saved, statement)

    def test_a_span_menu_with_no_lines_of_its_own_is_the_sixteen(self):
        mirc = Mirc()
        mirc.run("dccore.fillspan", "1645 7")
        mirc.run("dccore.sw.cb.fill", "1645")
        self.assertEqual(mirc.lines(1645), colour_list())
        self.assertTrue(mirc.selected(1645).startswith("07 "))

    def combos(self, data=None):
        """dccore.sw.combos, then every menu it gave a recipe filled."""
        mirc = Mirc(data=data)
        mirc.run("dccore.sw.combos")
        self.assertEqual(mirc.did_a, 0, "a line at a time")
        for cid in sorted(lazy_ids(mirc)):
            self.assertEqual(mirc.lines(cid), [], cid)
            mirc.run("dccore.sw.cb.fill", str(cid))
        return mirc

    def test_every_combo_of_the_bots_settings_has_the_lines_it_had(self):
        mirc = self.combos()
        self.assertEqual(mirc.did_a, 0, "a line at a time")
        kinds = {}
        for name, value in DATA.items():
            if not name.startswith("k."):
                continue
            key, (cid, kind) = name[2:], value.split()[:2]
            cid = int(cid)
            kinds[kind] = kinds.get(kind, 0) + 1
            if kind == "choice":
                choices = DATA["ch." + key].split()
                labels = [untext(DATA["cl.%s.%d" % (key, n)]) for n in range(1, len(choices) + 1)]
                self.assertEqual(mirc.lines(cid), labels, key)
                self.assertTrue(all(labels), key)
            elif kind == "tri":
                self.assertEqual(mirc.lines(cid), ["Not set (automatic)", "On", "Off"], key)
            elif kind == "colour":
                # line 1 the theme's own, colour C on line C + 2: the 18th
                # line is the "As set" one dccore.sw.colourfill adds
                self.assertEqual(mirc.lines(cid), [untext(DATA["themedefault"])] + COLOURS, key)
                self.assertEqual(mirc.lines(cid + 1), [untext(DATA["keepprevious"])] + COLOURS, key)
            else:
                self.assertEqual(mirc.lines(cid), [], key)
        self.assertEqual((kinds["choice"], kinds["tri"], kinds["colour"]), (5, 1, 6))
        self.assertEqual(len(COLOURS), 16)
        self.assertEqual([c.split(" ", 1)[0] for c in COLOURS], ["%02d" % n for n in range(16)])
        # one didtok a menu
        self.assertEqual(len(mirc.didtoks), len(set(mirc.didtoks)))

    def test_the_mode_menus_have_the_three_modes_at_open(self):
        mirc = Mirc()
        mirc.run("dccore.sw.combos")
        modes = untext(DATA["modelabels"]).split(",")
        self.assertEqual(modes, ["Normal", "Quiet (no advert)", "Request only (silent)"])
        for row in range(1, 16):
            self.assertEqual(mirc.lines(1700 + 4 * (row - 1) + 2), modes, row)
        self.assertEqual(mirc.lines(1772), modes)

    def test_a_label_with_a_comma_stays_one_line(self):
        data = dict(DATA)
        data["cl.THEME.2"] = "dark~2C midnight"
        mirc = self.combos(data)
        cid = int(DATA["k.THEME"].split()[0])
        self.assertEqual(mirc.lines(cid)[1], "dark, midnight")
        self.assertEqual(len(mirc.lines(cid)), len(DATA["ch.THEME"].split()))

    def test_an_empty_list_halts_nothing(self):
        # didtok with nothing to add is an error that would stop the window
        # being made: a list that can be empty is not given to it
        data = dict(DATA, tri="", modelabels="", colours="")
        data["ch.THEME"] = ""
        mirc = self.combos(data)
        self.assertEqual(mirc.lines(int(DATA["k.THEME"].split()[0])), [])
        cid = next(int(v.split()[0]) for k, v in DATA.items() if k.startswith("k.") and v.split()[1] == "colour")
        self.assertEqual(mirc.lines(cid), [untext(DATA["themedefault"])])
        Mirc().run("dccore.sw.fillfrom", "1772")

    def test_no_fill_adds_a_line_at_a_time_in_a_loop(self):
        for name in ("dccore.fillcombo", "dccore.fillbg", "dccore.fillspan", "dccore.sw.fillfrom", "dccore.sw.combos",
                     "dccore.sw.cb.fill"):
            for command, around in walk(parse(ALIASES[name])):
                if command.startswith("did ") and "a" in command.split()[1]:
                    self.assertNotIn("while", [kind for kind, _c in around], (name, command))

    def test_every_didtok_has_a_list_that_cannot_be_empty(self):
        seen = 0
        for name, body in ALIASES.items():
            for command, around in walk(parse(body)):
                if not command.startswith("didtok "):
                    continue
                seen += 1
                items = " ".join(split_top(command, " ")[4:])
                guarded = any(kind == "if" and (condition.strip("() ") == "%s != $null" % items
                                                or "$mid(%s,2)" % condition.strip("() ").split(" ")[0] == items)
                              for kind, condition in around)
                constant = items == "$dccore.colours"
                # a colour menu: its own first line, then the colours
                labelled = items.startswith("$+($dccore.sw.untext($dccore.sw.m($gettok(%r,2,32))),$chr(7),")
                self.assertTrue(guarded or constant or labelled, (name, command))
        self.assertGreaterEqual(seen, 6)
        # the first line a colour recipe names is never empty
        mirc = Mirc()
        mirc.run("dccore.sw.combos")
        firsts = {v.split()[1] for k, v in mirc.sws.items() if k.startswith("cb.") and v.startswith("colour ")}
        self.assertEqual(firsts, {"themedefault", "keepprevious"})
        for first in firsts:
            self.assertNotEqual(untext(DATA[first]), "")

    def test_no_label_holds_the_separator(self):
        for name, value in DATA.items():
            if name.startswith("cl.") or name in ("colours", "themedefault", "keepprevious"):
                self.assertNotIn(chr(7), untext(value), name)


class AMenuIsFilledWhenItsPageIsFirstShown(unittest.TestCase):
    """#1382 part 2: a menu's lines cost mIRC some 0.6 ms each, so a page's
    menus get theirs the first time it is shown; till then the line selected
    is kept as a number. Nothing the operator sees or saves may differ."""

    def test_the_open_gives_recipes_and_fills_nothing_lazy(self):
        mirc = settings_window()
        lazy = lazy_ids(mirc)
        expected = set(MIRC_SIDE_MENUS)
        for _key, cid, kind in menu_keys():
            expected |= {cid, cid + 1} if kind == "colour" else {cid}
        self.assertEqual(lazy, expected)
        for cid in lazy:
            self.assertEqual(mirc.lines(cid), [], cid)
        # left as they were: the Mode menus, the row adding one, the theme
        for cid in [1700 + 4 * (row - 1) + 2 for row in range(1, 16)] + [1772, 1691]:
            self.assertEqual(len(mirc.lines(cid)), 3, cid)

    def test_every_lazy_menu_is_on_one_page(self):
        lazy = lazy_ids(settings_window())
        for cid in lazy:
            pages = [p for p in PAGES if cid in page_ids(p)]
            self.assertEqual(len(pages), 1, (cid, pages))

    def test_showing_a_page_fills_exactly_its_menus_once(self):
        mirc = settings_window()
        lazy = lazy_ids(mirc)
        for page in PAGES:
            before = {cid: mirc.lines(cid) for cid in lazy}
            mirc.run("dccore.sw.showpage", "%d -v" % page)
            mine = set(page_ids(page)) & lazy
            for cid in lazy:
                if cid in mine:
                    self.assertNotEqual(mirc.lines(cid), [], (page, cid))
                else:
                    self.assertEqual(mirc.lines(cid), before[cid], (page, cid))
            # again: nothing added, and the page's controls not looked
            # through a second time
            filled = {cid: mirc.lines(cid) for cid in lazy}
            calls, made = len(mirc.calls), len(mirc.didtoks)
            mirc.run("dccore.sw.showpage", "%d -h" % page)
            mirc.run("dccore.sw.showpage", "%d -v" % page)
            self.assertEqual({cid: mirc.lines(cid) for cid in lazy}, filled)
            self.assertEqual(len(mirc.didtoks), made)
            self.assertNotIn("dccore.sw.cb.waits", mirc.calls[calls:], page)

    def test_a_menu_is_filled_once_whatever_asks(self):
        mirc = settings_window()
        mirc.run("dccore.sw.cb.fill", "1640")
        mirc.run("dccore.sw.cb.fill", "1640")
        self.assertEqual(mirc.lines(1640), colour_list())

    def test_the_bindings_page_keeps_its_mode_menus(self):
        mirc = settings_window()
        page = next(p for p in PAGES if 1772 in page_ids(p))
        rows = [1700 + 4 * (row - 1) + 2 for row in range(1, 16)] + [1772]
        for cid in rows:
            mirc.run("dccore.sw.pickline", "%d %d" % (cid, 2))
        before = menus(mirc, rows)
        mirc.run("dccore.sw.showpage", "%d -v" % page)
        self.assertEqual(menus(mirc, rows), before)

    def test_the_built_in_window_gets_them_too_and_hiding_fills_nothing(self):
        for dcx in (False, True):
            mirc = settings_window(dcx=dcx)
            if dcx:
                mirc.dcx.update(OPEN)
                mirc.stubs.update({"dccore.sw.dcxrect": lambda args: " ".join(args),
                                   "dccore.sw.dcxpoly": lambda args: "POLY"})
            page = next(p for p in PAGES if 1640 in page_ids(p))
            mirc.run("dccore.sw.showpage", "%d -h" % page)
            self.assertEqual(mirc.lines(1640), [], dcx)
            mirc.run("dccore.sw.showpage", "%d -v" % page)
            self.assertEqual(mirc.lines(1640), colour_list(), dcx)

    def test_a_selection_kept_and_one_shown_say_the_same(self):
        mirc = Mirc()
        mirc.run("dccore.sw.cb.lazy", "1640 colours")
        self.assertEqual(mirc.ident("$dccore.sw.cb.sel(1640)"), "0", "nothing selected yet")
        for line, kept in (("5", "5"), ("0", "0"), ("", "0"), ("3", "3")):
            mirc.run("dccore.sw.cb.select", ("1640 " + line).strip())
            self.assertEqual(mirc.sws["cbsel.1640"], kept)
            self.assertEqual(mirc.ident("$dccore.sw.cb.sel(1640)"), kept)
        self.assertEqual(mirc.selections, [], "no did on a menu not filled")
        mirc.run("dccore.sw.cb.fill", "1640")
        self.assertEqual(mirc.control(1640).sel, 3)
        for line, shown in (("7", "7"), ("0", "0"), ("2", "2"), ("", "0")):
            mirc.run("dccore.sw.cb.select", ("1640 " + line).strip())
            self.assertEqual(mirc.ident("$dccore.sw.cb.sel(1640)"), shown)
            self.assertEqual(mirc.control(1640).sel, int(shown))

    def test_the_as_set_line_is_the_18th_either_way(self):
        key, cid, _kind = next(k for k in menu_keys() if k[2] == "colour")
        for fill_first in (True, False):
            mirc = settings_window()
            if fill_first:
                show_all(mirc)
            mirc.sws["v." + key] = "=\\x02bold"
            mirc.run("dccore.sw.fill", key)
            show_all(mirc)
            self.assertEqual(mirc.lines(cid)[17:], ["As set: \\x02bold"], fill_first)
            self.assertEqual(mirc.control(cid).sel, 18)
            self.assertEqual(mirc.ident("$dccore.sw.wire(%s)" % key), "\\x02bold")
            # a colour picked over it, from the bot: the line goes
            mirc.sws["v." + key] = "=\\x0305,02"
            mirc.run("dccore.sw.fill", key)
            self.assertEqual(len(mirc.lines(cid)), 17)
            self.assertEqual((mirc.control(cid).sel, mirc.control(cid + 1).sel), (7, 4))

    def test_a_page_shown_late_shows_what_one_shown_early_does(self):
        """Filled first and set after - what the old window did, every menu
        filled at open - against set first and filled after."""
        lazy = lazy_ids(settings_window())
        for round_ in range(7):
            early = settings_window(opts={"col.term": str(round_ + 3), "bg": str(round_ - 1)})
            show_all(early)
            bot_values(early, round_)
            late = settings_window(opts={"col.term": str(round_ + 3), "bg": str(round_ - 1)})
            bot_values(late, round_)
            show_all(late)
            self.assertEqual(menus(late, lazy), menus(early, lazy), round_)

    def test_what_is_read_from_a_page_never_shown_is_what_it_would_show(self):
        """Apply, the dirty checks and the bot's values say the same for a
        page never shown as once it is."""
        for round_ in range(7):
            opts = {"col.request": str(15 - round_), "col.name": str(round_), "bg": str(round_ - 1),
                    "col.nick": ("per", "-1", "9")[round_ % 3], "col.term": ("name", "-1", "4")[round_ % 3]}
            early = settings_window(opts)
            show_all(early)
            bot_values(early, round_)
            late = settings_window(opts)
            bot_values(late, round_)
            unshown = readers(late)
            show_all(late)
            self.assertEqual(unshown, readers(late), round_)
            self.assertEqual(unshown, readers(early), round_)
            # and Apply keeps what dccore.ini had
            for name, value in opts.items():
                self.assertEqual(unshown["save." + name], value, (round_, name))
            self.assertEqual(late.ident("$dccore.sw.opt.sig"), late.sws["opt.sig"], "not dirty")

    def test_a_line_the_menu_will_not_have_is_kept_as_none(self):
        """dccore.ini edited by hand: a colour past the sixteen. The old
        window's did -c selected nothing there (Windows clears the selection
        for a line a menu does not have), so Apply wrote -1 (-2 for the
        background), and a page shown changed nothing the dirty check sees."""
        for key, value, old in (("col.name", "16", "-1"), ("bg", "16", "-2"), ("col.request", "99", "-1"),
                                ("bg", "15", "15"), ("col.name", "15", "15")):
            mirc = settings_window({key: value})
            base = mirc.sws["opt.sig"]
            mirc.run("dccore.sw.opt.save")
            self.assertEqual(mirc.tables["dccore"][key], old, (key, value))
            mirc.sws["opt.sig"] = base
            show_all(mirc)
            self.assertEqual(mirc.ident("$dccore.sw.opt.sig"), base, "nothing was touched: %s %s" % (key, value))

    def test_a_menu_knows_how_many_lines_it_will_have(self):
        """$dccore.sw.cb.lines against what dccore.sw.cb.fill adds, for every
        recipe - and with the layout's lists of other lengths, so neither
        side can hold a number of its own."""
        other = dict(DATA, tri="a~2Cb~2Cc~2Cd", colours="00 white~2C01 black~2C02 blue")
        other["ch.THEME"] = "classic plain"
        for data in (DATA, other):
            recipes = []
            for key, cid, kind in menu_keys():
                if kind == "colour":
                    recipes += [(cid, "colour themedefault", None), (cid + 1, "colour keepprevious", None),
                                (cid, "colour themedefault", "As set: \\x02bold")]
                else:
                    recipes.append((cid, "%s %s" % (kind, key) if kind == "choice" else kind, None))
            recipes += [(1640, "colours", None), (1673, "bg", None), (1643, "span name -1", None),
                        (1644, "span -1 per", None), (1645, "span -1", None), (1645, "span", None)]
            for cid, recipe, extra in recipes:
                mirc = Mirc(data=data)
                mirc.run("dccore.sw.cb.lazy", "%d %s" % (cid, recipe))
                if extra:
                    mirc.run("dccore.sw.cb.extra", "%d %s" % (cid, extra))
                expect = mirc.ident("$dccore.sw.cb.lines(%d)" % cid)
                mirc.run("dccore.sw.cb.fill", str(cid))
                self.assertEqual(int(expect), len(mirc.lines(cid)), (recipe, extra, data is DATA))
                # the last line can be kept; one past it is kept as none
                for line, kept in ((int(expect), expect), (int(expect) + 1, "0")):
                    waiting = Mirc(data=data)
                    waiting.run("dccore.sw.cb.lazy", "%d %s" % (cid, recipe))
                    if extra:
                        waiting.run("dccore.sw.cb.extra", "%d %s" % (cid, extra))
                    waiting.run("dccore.sw.cb.select", "%d %d" % (cid, line))
                    self.assertEqual(waiting.ident("$dccore.sw.cb.sel(%d)" % cid), kept, (recipe, line))
                    waiting.run("dccore.sw.cb.fill", str(cid))
                    self.assertEqual(waiting.ident("$dccore.sw.cb.sel(%d)" % cid), kept, (recipe, line))

    def test_only_the_helpers_select_or_read_a_lazy_menu(self):
        # run: the open, the bot's values, every reader, Apply, every page
        mirc = settings_window()
        lazy = lazy_ids(mirc)
        bot_values(mirc, 3)
        readers(mirc)
        show_all(mirc)
        bot_values(mirc, 4)
        readers(mirc)
        touched = [(cid, stack) for cid, stack in mirc.sel_reads if cid in lazy]
        touched += [(cid, stack) for c, _flags, stack in mirc.selections
                    for cid in [next(i for i, ctl in mirc.controls.items() if ctl is c)] if cid in lazy]
        self.assertTrue(touched)
        for cid, stack in touched:
            self.assertTrue(stack[-1].startswith("dccore.sw.cb."), (cid, stack))
        # read: what the run did not reach - a .sel, did -c / -u / -d on a
        # menu's id - only where the ids are never a lazy menu's
        others = {"dccore.sw.opt.fill", "dccore.sw.opt.sig", "dccore.sw.opt.save", "dccore.sw.init",
                  "dccore.sw.page", "dccore.sw.stay", "dccore.sw.lv.sel", "dccore.sw.lv.del", "dccore.sw.fill",
                  "dccore.sw.srv.mode", "dccore.sw.srv.chosen", "dccore.sw.pickline", "dccore.sw.srv.drawrows",
                  "dccore.sw.scrolled"}
        found = set()
        for name, body in ALIASES.items():
            if name.startswith("dccore.sw.cb."):
                continue
            for command, around in walk(parse(body)):
                for text in [command] + [c or "" for _k, c in around]:
                    for target in re.findall(r"\$did\(dccore\.set,([^)]*\)?)\)\.sel|\bdid -[a-z]*[cud][a-z]* "
                                             r"dccore\.set (\S+)", text):
                        target = target[0] or target[1]
                        if re.match(r"^\d+$", target):
                            self.assertNotIn(int(target), lazy, (name, command))
                        else:
                            found.add(name)
        self.assertEqual(found, others & found)
        self.assertLessEqual(found, others)
        # sw.fill selects only its checks itself
        for command, around in walk(parse(ALIASES["dccore.sw.fill"])):
            if re.match(r"did -[cu] ", command):
                self.assertIn(("if", "(%kind == bool)"), around, command)


class APagesFramesAreMadeWhenItIsFirstShown(unittest.TestCase):

    def test_the_window_makes_no_page_frames_and_no_tables_at_open(self):
        made = reachable("dccore.sw.dcx")
        for name in ("dccore.sw.dcxframes", "dccore.sw.lv.make", "dccore.sw.lv.need", "dccore.sw.lv.id"):
            self.assertFalse(name in made, "dccore.sw.dcx runs %s" % name)
        self.assertEqual(callers("dccore.sw.dcxframes"), {"dccore.sw.showpage"})
        self.assertEqual(callers("dccore.sw.lv.make"), {"dccore.sw.lv.need"})
        self.assertEqual(callers("dccore.sw.lv.need"), {"dccore.sw.lv.id"})

    def frames_of(self, page):
        return [int(DATA["fr." + box].split()[0]) for box in DATA["p.%d.bx" % page].split(",")]

    def test_a_page_never_shown_is_hidden_without_a_dcx_command(self):
        mirc = panel()
        for page in range(1, int(DATA["pages"]) + 1):
            mirc.run("dccore.sw.showpage", "%d -h" % page)
        self.assertEqual(mirc.dcx_log, [])

    def test_a_page_shown_makes_its_frames_once(self):
        page = page_with(lambda p: "p.%d.bx" % p in DATA)
        bases = self.frames_of(page)
        mirc = panel()
        for _ in range(3):
            mirc.run("dccore.sw.showpage", "%d -v" % page)
            mirc.run("dccore.sw.showpage", "%d -h" % page)
        self.assertEqual(mirc.dcx - OPEN, set(bases) | {b + 1 for b in bases} | self.tables_of(page))
        first, last = (int(n) for n in DATA["p.%d.fr" % page].split("-"))
        shown = [what for cid, what in mirc.dcx_log if cid == first and what in ("-s", "-h")]
        self.assertEqual(shown[:2], ["-h", "-s"], "made hidden, then shown")
        self.assertEqual(shown[-1], "-h")
        # another page shown makes its own, and none of these again
        other = page_with(lambda p: p != page and "p.%d.bx" % p in DATA)
        mirc.run("dccore.sw.showpage", "%d -v" % other)
        self.assertTrue(set(self.frames_of(other)) <= mirc.dcx)

    def tables_of(self, page):
        return {int(DATA["lv.%s" % lv].split()[0]) for lv in DATA.get("p.%d.lv" % page, "").split()}

    def test_a_frames_title_is_light_in_the_dark_theme_as_it_is_made(self):
        page = page_with(lambda p: "p.%d.bx" % p in DATA)
        title = self.frames_of(page)[0] + 1
        for dark in (0, 1):
            mirc = panel(dark=dark)
            mirc.run("dccore.sw.dcxframes", str(page))
            said = [what for cid, what in mirc.dcx_log if cid == title]
            self.assertIn("-C +b FACE", said)
            self.assertEqual("-C +t RGB.fg" in said, bool(dark), dark)
            if dark:
                self.assertLess(said.index("-C +b FACE"), said.index("-C +t RGB.fg"))
        # and the texts made at open no longer reach for frames' ids
        self.assertNotIn("fr.", ALIASES["dccore.theme.dcxtexts"])


class ATableIsMadeWhenFirstNeeded(unittest.TestCase):

    def lv(self, native):
        return int(DATA["lv.%d" % native].split()[0])

    def test_every_helper_makes_it_first_and_only_once(self):
        for helper, args in (("dccore.sw.lv.clear", "%d"), ("dccore.sw.lv.add", "%d a b c"),
                             ("dccore.sw.lv.del", "%d 1")):
            for native in TABLES:
                mirc = panel()
                mirc.run(helper, args % native)
                mirc.run(helper, args % native)
                self.assertEqual(mirc.dcx - OPEN, {self.lv(native)}, (helper, native))
        for helper in ("dccore.sw.lv.sel", "dccore.sw.lv.count", "dccore.sw.lv.text"):
            mirc = panel()
            mirc.ident("$%s(1540,1) $%s(1540,1)" % (helper, helper))
            self.assertEqual(mirc.dcx - OPEN, {self.lv(1540)}, helper)

    def test_without_dcx_nothing_is_made(self):
        mirc = Mirc(sws={})
        self.assertEqual(mirc.ident("$dccore.sw.lv.id(1540)"), "1540")
        mirc.run("dccore.sw.lv.need", "1540")
        self.assertEqual((mirc.dcx, mirc.dcx_log), (set(), []))

    def test_every_dcx_command_on_a_table_goes_through_lv_id(self):
        # the ids of the tables are looked up in two places only: where a
        # table is made, and lv.id, which makes it first
        uses = {name for name, body in ALIASES.items() if "$dccore.sw.m(lv. $+" in body}
        self.assertEqual(uses, {"dccore.sw.lv.make", "dccore.sw.lv.id"})
        for name, body in ALIASES.items():
            for command, _around in walk(parse(body)):
                for target in re.findall(r"dccore\.dcx(?: |\()_?xdid,? ?dccore\.set (\S+)", command):
                    if "lv" in target and name != "dccore.sw.lv.make":
                        self.assertTrue(target.startswith("$dccore.sw.lv.id("), (name, command))
        mirc = panel()
        self.assertEqual(mirc.ident("$dccore.sw.lv.id(1580)"), str(self.lv(1580)))
        self.assertEqual(mirc.dcx - OPEN, {self.lv(1580)})

    def test_a_page_with_tables_shows_them_and_hides_none_never_made(self):
        page = page_with(lambda p: "1540" in DATA.get("p.%d.lv" % p, "").split())
        mirc = panel()
        mirc.run("dccore.sw.showpage", "%d -h" % page)
        self.assertNotIn(self.lv(1540), mirc.dcx)
        mirc.run("dccore.sw.showpage", "%d -v" % page)
        said = [what for cid, what in mirc.dcx_log if cid == self.lv(1540)]
        self.assertEqual(said[0], "made listview")
        self.assertEqual(said[-2:], ["-h", "-s"])
        mirc.run("dccore.sw.showpage", "%d -h" % page)
        self.assertEqual(mirc.dcx_log[-1][1], "-h")

    def test_the_enable_state_reaches_a_table_made_later(self):
        mirc = panel()
        mirc.run("dccore.sw.lv.clear", "1520")
        mirc.run("dccore.sw.enable", "0")
        # the one made takes it now, one not made is left alone
        self.assertIn((self.lv(1520), "-b"), mirc.dcx_log)
        self.assertEqual({cid for cid, _w in mirc.dcx_log}, {self.lv(1520)})
        mirc.run("dccore.sw.lv.clear", "1540")
        self.assertIn((self.lv(1540), "-b"), mirc.dcx_log)
        mirc.run("dccore.sw.enable", "1")
        self.assertIn((self.lv(1540), "-e"), mirc.dcx_log)
        mirc.run("dccore.sw.lv.clear", "1580")
        self.assertNotIn((self.lv(1580), "-b"), mirc.dcx_log)

    def test_in_the_dark_theme_a_table_is_enabled_and_dark_as_it_is_made(self):
        for dark in (0, 1):
            mirc = panel(dark=dark)
            mirc.run("dccore.sw.enable", "0")
            mirc.run("dccore.sw.lv.clear", "1595")
            said = [what for cid, what in mirc.dcx_log if cid == self.lv(1595)]
            self.assertEqual("-b" in said, not dark, dark)
            self.assertEqual("-i +bk RGB.panel" in said and "-i +t RGB.fg" in said, bool(dark), dark)
            # made hidden, -h the last thing it was told before the clear
            self.assertEqual(said[said.index("-h") + 1:], ["-r"])

    def test_a_table_made_after_the_window_has_the_dll_once_more_only_where_it_draws(self):
        for inited in ("", "1"):
            for noct in ("0", "1"):
                mirc = panel(noct=noct)
                if inited:
                    mirc.sws["inited"] = inited
                mirc.run("dccore.sw.lv.clear", "1540")
                mirc.run("dccore.sw.lv.clear", "1580")
                timers = [t for t in mirc.timers if t[0] == "dccoreThemeLv"]
                if inited and noct == "1":
                    # one name: tables made close together share one pass
                    self.assertEqual(set(timers), {("dccoreThemeLv", "-m 1 250 dccore.sw.lv.dark")})
                else:
                    self.assertEqual(timers, [], (inited, noct))
        self.assertEqual([s.strip() for s in ALIASES["dccore.sw.init"].split("\n") if s.strip()][-1],
                         "hadd dccore.sws inited 1")

    def lv_dark(self, command, open_=True, noct="1", dark=True, mircdark=False, ui=True):
        """`command` run as the timer runs it, the theme as it is by then:
        whether the dll was applied to the control panel."""
        applied = []
        stubs = {
            "dccore.theme.dark": lambda args: "$true" if dark else "$false",
            "dccore.theme.mircdark": lambda args: "$true" if mircdark else "$false",
            "dccore.theme.ui": lambda args: "$true" if ui else "$false",
            "dccore.theme.dialog": lambda args: applied.append(args),
        }
        mirc = Mirc(sws={"dcx": "1", "noct": noct}, stubs=stubs)
        mirc.open = open_
        mirc.statement(command)
        self.assertIn(applied, ([], [["dccore.set"]]))
        return bool(applied)

    def test_the_dll_is_applied_again_only_while_the_theme_the_panel_opened_with_holds(self):
        mirc = panel(noct=1, inited=1)
        mirc.run("dccore.sw.lv.clear", "1540")
        command = mirc.timers[-1][1].split(" ", 3)[3]
        # as it opened: dark through the dll
        self.assertTrue(self.lv_dark(command))
        # /dccore theme light since: noct still 1, the dll would turn mIRC's
        # controls light under DCX parts that stay dark until it opens again
        self.assertFalse(self.lv_dark(command, dark=False))
        # the window closed before the timer ran, mIRC's own dark mode on
        # since, the dll no longer the pinned one, or opened without noct
        self.assertFalse(self.lv_dark(command, open_=False))
        self.assertFalse(self.lv_dark(command, mircdark=True))
        self.assertFalse(self.lv_dark(command, ui=False))
        self.assertFalse(self.lv_dark(command, noct="0"))
        for open_ in (False, True):
            for noct in ("0", "1"):
                for dark in (False, True):
                    for mircdark in (False, True):
                        for ui in (False, True):
                            expect = open_ and noct == "1" and dark and ui and not mircdark
                            self.assertEqual(self.lv_dark(command, open_, noct, dark, mircdark, ui), expect,
                                             (open_, noct, dark, mircdark, ui))

    def test_the_theme_colours_only_the_page_list_at_open(self):
        mirc = panel(dark=1)
        mirc.run("dccore.theme.dcxlists")
        self.assertEqual({cid for cid, _w in mirc.dcx_log}, {int(DATA["dcxpages"].split()[0])})


class ADcxTextHasItsFontAsItIsMade(unittest.TestCase):

    def test_the_flags_say_a_only_where_the_dll_draws_the_window_dark(self):
        for noct, expect in (("0", ("+b", "+")), ("1", ("+ab", "+a"))):
            mirc = panel(noct=noct)
            self.assertEqual((mirc.ident("$dccore.sw.fontflags(b)"), mirc.ident("$dccore.sw.fontflags")), expect)

    def theme(self, dark, mircdark, ui):
        """dark and noct as dccore.sw.dcx works them out, its own statements
        run, the theme asked through the real dccore.theme.dialogsdark."""
        stubs = {
            "dccore.theme.dark": lambda args: "$true" if dark else "$false",
            "dccore.theme.mircdark": lambda args: "$true" if mircdark else "$false",
            "dccore.theme.ui": lambda args: "$true" if ui else "$false",
        }
        mirc = Mirc(sws={}, stubs=stubs)
        for line in ALIASES["dccore.sw.dcx"].split("\n"):
            line = line.strip()
            if line.startswith("hadd dccore.sws dark ") or line.startswith("hadd dccore.sws noct "):
                mirc.statement(line)
        return mirc.sws.get("dark"), mirc.sws.get("noct")

    def test_no_cleartype_only_where_the_dll_draws_dark_not_in_mircs_own_dark_mode(self):
        for dark in (False, True):
            for mircdark in (False, True):
                for ui in (False, True):
                    got = self.theme(dark, mircdark, ui)
                    dialogsdark = dark and (mircdark or ui)
                    # the old dccore.theme.dcxfonts ran in the dark dialogs
                    # only, and not in mIRC's dark mode nor without the dll
                    self.assertEqual(got, ("1" if dialogsdark else "0", "1" if dark and ui and not mircdark else "0"),
                                     (dark, mircdark, ui))

    def test_the_theme_is_known_before_anything_is_made(self):
        lines = [s.strip() for s in ALIASES["dccore.sw.dcx"].split("\n") if s.strip()]
        noct = next(n for n, s in enumerate(lines) if s.startswith("hadd dccore.sws noct "))
        dark = next(n for n, s in enumerate(lines) if s.startswith("hadd dccore.sws dark "))
        made = next(n for n, s in enumerate(lines) if "xdialog dccore.set -c" in s or s.startswith("dccore.sw.dcx"))
        self.assertLess(dark, noct)
        self.assertLess(noct, made)

    def test_no_font_is_written_with_its_flags_by_hand(self):
        flags = re.findall(r"xdid dccore\.set \S+ -f (\S+)", CODE)
        self.assertGreaterEqual(len(flags), 5)
        self.assertEqual(set(flags), {"$dccore.sw.fontflags(b)", "$dccore.sw.fontflags", "%f"})
        self.assertEqual([n for n, b in ALIASES.items() if "-f %f" in b], ["dccore.sw.dcxaa"])

    def opened(self, dark, mircdark):
        """The control panel made by dccore.sw.dcx, then every page shown and
        every table needed: the DCX texts and listviews made, and the font
        each ended with ("" for DCX's own, never set)."""
        stubs = {
            "dccore.dcx.why": lambda args: "",
            "dccore.theme.sys": lambda args: "SYS." + args[0],
            "dccore.theme.dialogsdark": lambda args: "$true" if dark else "$false",
            "dccore.theme.mircdark": lambda args: "$true" if mircdark else "$false",
        }
        mirc = panel()
        mirc.sws = {}
        mirc.stubs.update(stubs)
        mirc.dcx = set()
        mirc.run("dccore.sw.dcx")
        for page in range(1, int(DATA["pages"]) + 1):
            mirc.run("dccore.sw.showpage", "%d -v" % page)
            mirc.run("dccore.sw.showpage", "%d -h" % page)
        for native in TABLES:
            mirc.run("dccore.sw.lv.count", str(native))
        return {cid: mirc.fonts.get(cid, "") for cid, kind in mirc.kinds.items() if kind in ("text", "listview")}

    def test_every_dcx_text_and_listview_has_its_font_without_cleartype_as_it_is_made(self):
        fonts = self.opened(dark=True, mircdark=False)
        frames = sum(len(DATA["p.%d.bx" % p].split(",")) for p in range(1, int(DATA["pages"]) + 1)
                     if "p.%d.bx" % p in DATA)
        self.assertGreater(len(fonts), frames + len(TABLES))
        for cid, font in fonts.items():
            self.assertTrue(font.startswith("+") and "a" in font.split(" ", 1)[0], (cid, font))
        # the bold ones keep their own face and size, now with a
        title = int(DATA["dcxtitle"].split()[0])
        self.assertEqual(fonts[title], "+ab default 9 Tahoma")
        self.assertEqual(fonts[int(DATA["dcxcrumb"].split()[0])], "+ab default 8 Tahoma")
        # every table, every frame's title
        for native in TABLES:
            self.assertEqual(fonts[int(DATA["lv.%d" % native].split()[0])], "+a ansi 8 Segoe UI")
        for box in (k for k in DATA if k.startswith("fr.")):
            self.assertEqual(fonts[int(DATA[box].split()[0]) + 1], "+a ansi 8 Segoe UI", box)

    def test_with_cleartype_kept_no_font_has_a(self):
        # the light theme, and mIRC's own dark mode: DCX's fonts as before
        for dark, mircdark in ((False, False), (True, True)):
            fonts = self.opened(dark, mircdark)
            for cid, font in fonts.items():
                self.assertNotIn("a", font.split(" ", 1)[0], (dark, mircdark, cid, font))
            self.assertEqual(fonts[int(DATA["dcxtitle"].split()[0])], "+b default 9 Tahoma")

    def test_a_default_font_is_asked_once_a_type_and_given_again_with_a(self):
        mirc = panel(noct=1)
        mirc.dcx.update({1, 2, 3})
        for cid, kind in ((1, "text"), (2, "text"), (3, "listview")):
            mirc.run("dccore.sw.dcxaa", "%d %s" % (cid, kind))
        self.assertEqual(mirc.font_asked, [1, 3])
        self.assertEqual(mirc.fonts, {1: "+a ansi 8 Segoe UI", 2: "+a ansi 8 Segoe UI", 3: "+a ansi 8 Segoe UI"})
        # with ClearType kept, nothing is asked or said
        mirc = panel(noct=0)
        mirc.dcx.add(1)
        mirc.run("dccore.sw.dcxaa", "1 text")
        self.assertEqual(mirc.dcx_log, [])
        # one already anti-aliased, or a font with no name, stays as it is
        for font in ("+ad ansi 8 Segoe UI", "+d ansi 8"):
            mirc = panel(noct=1)
            mirc.dcx.add(1)
            mirc.fonts[1] = font
            mirc.run("dccore.sw.dcxaa", "1 text")
            self.assertEqual(mirc.fonts[1], font)
            self.assertEqual([w for _c, w in mirc.dcx_log], ["asked font"])

    def test_nothing_sets_the_fonts_again_after_open(self):
        self.assertFalse("dccore.theme.dcxfonts" in ALIASES, "dccore.theme.dcxfonts is back")
        stubs = {"dccore.theme.icons": lambda args: "$false", "dccore.theme.iconfile": lambda args: "x.dll",
                 "dccore.theme.dialogsdark": lambda args: "$true"}
        mirc = Mirc(sws={"dcx": "1"}, stubs=stubs)
        mirc.run("dccore.theme.dcxset")
        self.assertEqual([t[1] for t in mirc.timers],
                         ["-m 1 0 dccore.theme.dcxbg", "-m 1 0 dccore.theme.dcxtexts", "-m 1 0 dccore.theme.dcxlists"])


if __name__ == "__main__":
    unittest.main()
