"""The dashboard's JavaScript is not left syntactically broken.

This is the half that runs everywhere; where node is on PATH,
test_the_dashboard_javascript_parses_in_a_real_engine.py parses the same
files for real (#648). The whole dashboard is one script:
a single unterminated string anywhere in web/app.js takes down every tab at
once, silently, with a blank page and one line in a console nobody has open.
That is the same shape as the failure that reached the RC1 beta - the Settings
page rendering headings and no fields, with nothing in the log.

This is not a parser and does not pretend to be one. It checks the invariants
that a hand-edit actually breaks, and it caught the one that put it here: a
"\\n" written into a string through a shell heredoc arrived as a REAL newline,
so the string ran off the end of its line. The file still looked right in a
diff.

WHY A SCANNER AND NOT A REGEX. A quote inside a comment, an apostrophe inside
a double-quoted string, and an escaped quote inside its own kind are all
ordinary here and all defeat counting. So this walks the file once, in the
order a lexer would: block comment, line comment, string, code.

Template literals would legitimately span lines. app.js has no backtick in
CODE (all 26 are prose inside comments), and the file targets the same
ES5-era style as the rest of web/, so one appearing is itself worth knowing
about rather than a case to handle.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WEB_DIR = os.path.join(REPO_ROOT, "web")


def scan(source):
    """Walk `source` once and return (unterminated_strings, depth_by_kind).

    unterminated_strings: [(line_number, quote_character), ...] - a string
    opened and still open when its line ended.
    depth_by_kind: the final nesting depth of (), {} and [] counted outside
    strings and comments; every one should be zero.
    code: the source with every comment and every string body removed, which
    is what a name check has to look at - the walk already knows where those
    are, and a second one that guessed would be the thing being guarded
    against.
    """
    unterminated = []
    code = []
    depth = {"(": 0, "{": 0, "[": 0}
    closers = {")": "(", "}": "{", "]": "["}
    line = 1
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if source.startswith("//", index):
            index = source.find("\n", index)
            if index == -1:
                break
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                index = length
                break
            line += source.count("\n", index, end)
            index = end + 2
            continue
        if char in "\"'":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                if source[index] == "\n":
                    unterminated.append((line, quote))
                    break
                index += 1
            continue
        if char in depth:
            depth[char] += 1
        elif char in closers:
            depth[closers[char]] -= 1
        code.append(char)
        index += 1
    return unterminated, depth, "".join(code)


class TheScannerItself(unittest.TestCase):
    """Guard on the guard. A scanner that never reports anything would pass
    every file below and mean nothing."""

    def test_a_string_running_off_its_line_is_reported(self):
        broken, _, _code = scan('var a = "hello\nvar b = 1;\n')

        self.assertEqual(broken, [(1, '"')])

    def test_an_escaped_newline_sequence_is_not_a_newline(self):
        """The exact case that put this file here: the two characters
        backslash and n are fine; a real newline is not."""
        clean, _, _code = scan('var a = "hello\\nworld";\n')

        self.assertEqual(clean, [])

    def test_a_quote_inside_a_comment_is_not_a_string(self):
        clean, _, _code = scan('// it\'s fine\nvar a = 1;\n')

        self.assertEqual(clean, [])

    def test_an_apostrophe_inside_double_quotes_is_not_an_opener(self):
        clean, _, _code = scan('var a = "it\'s fine";\n')

        self.assertEqual(clean, [])

    def test_an_escaped_quote_does_not_close_its_string(self):
        clean, _, _code = scan('var a = "say \\"hi\\" now";\n')

        self.assertEqual(clean, [])

    def test_a_brace_inside_a_string_is_not_counted(self):
        _, depth, _code = scan('var a = "{{{";\n')

        self.assertEqual(depth["{"], 0)

    def test_an_unclosed_brace_is_counted(self):
        _, depth, _code = scan('function f() {\n')

        self.assertEqual(depth["{"], 1)

    def test_a_block_comment_hides_everything_in_it(self):
        clean, depth, _code = scan('/* " { ( */ var a = 1;\n')

        self.assertEqual(clean, [])
        self.assertEqual(depth, {"(": 0, "{": 0, "[": 0})

    def test_the_line_number_survives_a_multi_line_block_comment(self):
        broken, _, _code = scan('/* one\ntwo\nthree */\nvar a = "x\n')

        self.assertEqual(broken, [(4, '"')])


class EveryScriptInWebIsIntact(unittest.TestCase):

    def scripts(self):
        return sorted(name for name in os.listdir(WEB_DIR)
                      if name.endswith(".js"))

    def read(self, name):
        with io.open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
            return handle.read()

    def test_there_is_something_to_check(self):
        """Guard on the guard: a loop over an empty directory passes."""
        self.assertIn("app.js", self.scripts())

    def test_no_string_runs_off_the_end_of_its_line(self):
        for name in self.scripts():
            broken, _, _code = scan(self.read(name))

            self.assertEqual(
                broken, [],
                f"{name}: a string literal is left open at "
                f"line(s) {[line for line, _ in broken]} - the whole "
                f"dashboard fails to parse, every tab at once")

    def test_brackets_balance(self):
        for name in self.scripts():
            _, depth, _code = scan(self.read(name))

            self.assertEqual(
                {kind: value for kind, value in depth.items() if value},
                {},
                f"{name}: unbalanced brackets")

    def test_no_template_literal_has_appeared_in_code(self):
        """All of app.js's backticks are prose inside comments. One reaching
        code would mean a string CAN legitimately span lines, and the check
        above would need to know about it."""
        for name in self.scripts():
            source = self.read(name)
            without_comments = []
            index = 0
            while index < len(source):
                if source.startswith("//", index):
                    end = source.find("\n", index)
                    index = len(source) if end == -1 else end
                elif source.startswith("/*", index):
                    end = source.find("*/", index + 2)
                    index = len(source) if end == -1 else end + 2
                else:
                    without_comments.append(source[index])
                    index += 1

            self.assertNotIn("`", "".join(without_comments),
                             f"{name}: a template literal in code - see this "
                             f"test's docstring")


# ---------------------------------------------------------------------------
# Calls to something nothing defines
# ---------------------------------------------------------------------------
#
# #515: web/app.js called renderSettingsFields() in four places and no
# function by that name has ever existed, in any commit. The real one is
# renderSettingsCategory().
#
# WHY NOTHING CAUGHT IT. JavaScript resolves a bare name at CALL time, so the
# file parses and every other tab works. All four calls sit inside a Promise
# .then(), two of them followed by a .catch() written to keep the settings
# page usable when a fetch fails - which swallowed the ReferenceError
# completely. The visible symptom was the On-Connect box appearing empty after
# a restart while the commands kept being sent, because the data was fine and
# only the repaint threw. Clicking to another category and back fixed it, since
# the rail button calls the real function - which is why it read as
# intermittent.
#
# The same hole as tests/test_names_resolve.py finds in Python, in the language
# where nothing at all checks it.
#
# DELIBERATELY NARROW. Only calls of the shape `name(` where `name` is not
# preceded by a dot - a bare call this file has to resolve itself. A method
# call belongs to whatever object it is on and is none of this test's business.
# Everything a browser provides is listed rather than guessed at: an
# allowlist that is too short fails loudly and gets one more name, which is
# the direction that cannot hide a real fault.

# Reserved words that are followed by "(" and are not calls.
JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "delete", "void", "in", "of", "do", "else", "try", "throw", "case",
}

# What the browser and the language provide.
JS_GLOBALS = {
    "Array", "Boolean", "Date", "Error", "JSON", "Math", "Number", "Object",
    "Promise", "RegExp", "String", "Symbol", "Map", "Set", "WeakMap",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "escape", "unescape",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame",
    "alert", "confirm", "prompt", "fetch", "XMLHttpRequest", "FormData",
    "URLSearchParams", "URL", "Blob", "FileReader", "EventSource",
    "WebSocket", "Intl", "console", "window", "document", "navigator",
    "location", "history", "localStorage", "sessionStorage", "performance",
}


def bare_calls(code):
    """{name: [offsets]} for every `name(` not preceded by a dot.

    NOT PRECEDED BY A BACKSLASH EITHER, which is about a known limit of the
    scanner rather than about JavaScript. scan() understands strings and
    comments; it does not understand REGEX LITERALS, because telling `/` as a
    regex from `/` as division needs the parser this file deliberately is not.
    So a literal's contents reach the code text, and app.js has one that
    matches an mIRC colour code - in which the escape fragment before its
    capture group reads as a call to something undefined.

    An identifier is never preceded by a backslash in real code, so excluding
    that costs nothing and removes the whole class.
    """
    found = {}
    for match in re.finditer(r"(?<![.\\\w$])([A-Za-z_$][\w$]*)\s*\(", code):
        name = match.group(1)
        if name in JS_KEYWORDS:
            continue
        found.setdefault(name, []).append(match.start(1))
    return found


def defined_names(code):
    """Every name the file itself binds - functions, vars, parameters."""
    names = set()
    names.update(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", code))
    names.update(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)", code))
    # Parameters, including the ones a callback receives - a function passed in
    # and then called is the ordinary shape here.
    for params in re.findall(r"function\s*[A-Za-z_$][\w$]*\s*\(([^)]*)\)", code):
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    for params in re.findall(r"function\s*\(([^)]*)\)", code):
        names.update(re.findall(r"[A-Za-z_$][\w$]*", params))
    # `name: function (...)` - a method on an object literal, called as a
    # property elsewhere but bound here.
    names.update(re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*function", code))
    return names


def undefined_calls(source):
    """Names `source` calls and never binds.

    ONE FUNCTION, so the synthetic cases below and the real files go through
    exactly the same code. A mutation run put this here: with the check written
    inline in the loop, "treat every called name as defined" made the real
    assertion vacuous while the synthetic one - which did its own filtering -
    carried on passing.
    """
    _unterminated, _depth, code = scan(source)
    known = defined_names(code) | JS_GLOBALS
    return sorted(name for name in bare_calls(code) if name not in known)


class EveryCallResolvesToSomething(unittest.TestCase):
    """#515."""

    def test_no_bare_call_names_something_undefined(self):
        offenders = []
        examined = []
        for name in sorted(os.listdir(WEB_DIR)):
            if not name.endswith(".js"):
                continue
            examined.append(name)
            with io.open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
                offenders.extend("%s: %s()" % (name, called)
                                 for called in undefined_calls(handle.read()))

        # IN THIS TEST, not beside it. A mutation run showed why: making the
        # loop skip every file left `offenders` empty and this assertion green,
        # and a coverage check written as its own test opened app.js directly
        # and so never noticed. The proof that the scan ran has to come from
        # the same loop whose result is being asserted.
        self.assertIn("app.js", examined,
                      "the scan looked at no dashboard file at all, so an "
                      "empty result means nothing")
        self.assertEqual(offenders, [],
                         "called and never defined - JavaScript only finds out "
                         "when the line runs, and in a .then() that is a "
                         "swallowed ReferenceError: " + ", ".join(offenders))

    def test_it_actually_looked_at_the_dashboard(self):
        """Guard on the guard, and the one a mutation run showed was missing:
        stopping the loop before it opens anything leaves `offenders` empty and
        the assertion above green. So this asserts it found real work to do,
        and that one name known to be there resolved."""
        with io.open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8") as handle:
            _unterminated, _depth, code = scan(handle.read())

        calls = bare_calls(code)

        self.assertGreater(len(calls), 100,
                           "the scan found almost nothing to check, so passing "
                           "means nothing")
        self.assertIn("renderSettingsCategory", calls,
                      "a function known to be called here was not seen at all")
        self.assertIn("renderSettingsCategory", defined_names(code))

    def test_the_check_finds_the_bug_it_was_written_for(self):
        """Through undefined_calls(), not around it - see its own note."""
        broken = "function realOne() { return 1; }\nfunction go() { missingOne(); }\n"

        self.assertEqual(undefined_calls(broken), ["missingOne"])

    def test_a_function_that_is_defined_is_not_reported(self):
        """The other half. A check that reported everything would make the
        assertion above meaningless in the opposite direction."""
        fine = "function realOne() { return 1; }\nfunction go() { realOne(); }\n"

        self.assertEqual(undefined_calls(fine), [])

    def test_a_method_call_is_not_its_business(self):
        """`thing.send()` belongs to whatever `thing` is. Including those would
        make the allowlist a list of every method in every browser API, which
        is how a guard becomes noise and then gets deleted."""
        _unterminated, _depth, code = scan("state.thing.refresh();\n")

        self.assertEqual(bare_calls(code), {})

    def test_a_name_inside_a_string_is_not_a_call(self):
        """The reason this reads the scanner's output rather than the file: a
        function name mentioned in a message is prose."""
        _unterminated, _depth, code = scan('var s = "call renderSettingsFields() here";\n')

        self.assertEqual(bare_calls(code), {})


if __name__ == "__main__":
    unittest.main()
