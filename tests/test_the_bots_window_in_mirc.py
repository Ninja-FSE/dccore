"""#550, step 4: scripts/mirc/dccore.mrc, the client the structured feed
was designed for.

CI has no mIRC, so this is what CAN be checked from here: the file ships
in the shape mIRC 6 reads (ASCII, CRLF via .gitattributes, no tabs,
balanced braces, every top-level block a header), the constraints the
header promises hold (no local aliases, since timers cannot reach them;
6.10 stated), every line type the bot can send is handled by name, and -
the one that matters - the $N positions the script reads for each kind
are the positions structured_line() and status_lines() actually emit.
A field that moves in the bot without moving here would draw the wrong
number in the window, and nothing but this test would notice.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402

SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")


def script_text():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read()


def code_lines(text):
    """The script without its comment lines and blank lines."""
    return [line for line in text.replace("\r\n", "\n").split("\n")
            if line.strip() and not line.strip().startswith(";")]


class TheFileShipsInTheShapeMircReads(unittest.TestCase):
    def test_it_exists_and_is_pure_ascii(self):
        self.assertTrue(os.path.isfile(SCRIPT))
        script_text()  # encoding="ascii" raises on anything above 127

    def test_gitattributes_stores_it_crlf(self):
        """mIRC 6 shows an LF-only file as one long line in its editor."""
        with io.open(os.path.join(REPO_ROOT, ".gitattributes"), encoding="utf-8") as handle:
            self.assertRegex(handle.read(), r"(?m)^\*\.mrc\s+text eol=crlf$")

    def test_no_tabs(self):
        self.assertNotIn("\t", script_text())

    def test_braces_balance_and_never_go_negative(self):
        depth = 0
        for line in code_lines(script_text()):
            depth += line.count("{") - line.count("}")
            self.assertGreaterEqual(depth, 0, line)
        self.assertEqual(depth, 0)

    def test_every_top_level_line_is_a_block_header(self):
        """Text outside a block is a syntax error mIRC reports on load."""
        depth = 0
        for line in code_lines(script_text()):
            if depth == 0:
                self.assertRegex(line, r"^(alias |on |raw |menu |dialog |\})", line)
            depth += line.count("{") - line.count("}")

    def test_the_header_states_the_floor(self):
        head = script_text()[:4000]
        self.assertIn("mIRC 6.10 or later", head)
        self.assertIn("/load -rs dccore.mrc", head)
        self.assertIn("/dccore pair", head)


class TheConstraintsTheHeaderPromises(unittest.TestCase):
    def test_no_local_alias(self):
        """A timer runs outside the script's scope and cannot call an
        alias -l; every alias is global and namespaced."""
        self.assertNotIn("alias -l", script_text())

    def test_every_alias_is_namespaced(self):
        for name in re.findall(r"(?m)^alias (\S+)", script_text()):
            self.assertTrue(name == "dccore" or name.startswith("dccore."), name)

    def test_every_timer_target_is_a_defined_alias(self):
        text = script_text()
        defined = set(re.findall(r"(?m)^alias (\S+)", text))
        targets = set(re.findall(r"\.timer\w+ (?:-m )?\d+ \S+ (\S+)", text))
        self.assertTrue(targets, "no timers found")
        for target in targets:
            self.assertIn(target, defined)

    def test_no_literal_bracket_inside_an_identifier(self):
        """[ ] are evaluation brackets in mIRC; a literal one is $chr(91)."""
        for line in code_lines(script_text()):
            self.assertNotRegex(line, r"\$\+\(\[", line)

    def test_padding_uses_the_non_breaking_space(self):
        """$1- collapses runs of spaces, so $str($chr(32),N) padding
        would arrive as one space."""
        self.assertNotIn("$str($chr(32)", script_text())
        self.assertIn("$chr(160)", script_text())

    def test_the_wire_is_ascii_the_script_sends(self):
        """hello names the client TYPE; pair and unpair name this copy's own
        pairing name, $dccore.client (#649), which is dccore.mrc plus a tail."""
        for must in ("hello dccore.mrc", "pair $dccore.client", "unpair $dccore.client",
                     "alias dccore.client { return dccore.mrc- $+ "):
            self.assertIn(must, script_text())


class EveryLineTypeIsHandled(unittest.TestCase):
    """The kinds the bot can send, by name, in dccore.structured."""

    def test_every_feed_kind_and_every_control_line(self):
        handled = set(re.findall(r"%type == (\w+)", script_text()))
        expected = set(adminchat.FEED_KINDS) | {"HELLO", "LOG", "OUT", "DROPPED", "PING",
                                                "STATUS", "SLOT", "QUEUE", "TOKEN"}
        self.assertEqual(expected - handled, set())

    def test_the_prompts_the_bot_sends_are_matched_verbatim(self):
        text = script_text()
        for prompt in ("Enter Your Password:", "Entering DCC Chat Admin Interface",
                       "Incorrect Password.", "Session taken over from",
                       "Unknown command: hello"):
            self.assertIn(prompt, text)
        with io.open(os.path.join(REPO_ROOT, "adminchat.py"), encoding="utf-8") as handle:
            source = handle.read()
        for prompt in ("Enter Your Password:", "Entering DCC Chat Admin Interface",
                       "Incorrect Password.", "Session taken over from"):
            self.assertIn(prompt, source, "the bot no longer says this")


class TheFieldPositionsMatchTheBot(unittest.TestCase):
    """For each kind, the bot renders a line from named fields with
    distinct marker values; the script's handler is read for which $N it
    uses for what; the two must agree. dccore.structured is called with
    $2- of the DCCORE line, so its $1 is the TYPE and its $N is the line's
    token N+1."""

    def handler(self, kind):
        text = script_text().replace("\r\n", "\n")
        block = text.split("alias dccore.structured {", 1)[1]
        start = block.index(f"if (%type == {kind})")
        return block[start:].split("\n  if (%type ==", 1)[0]

    def token_of(self, line, n):
        """$N of the DCCORE line as dccore.structured sees it ($1 = TYPE)."""
        tokens = line.split(" ")[1:]
        return tokens[n - 1]

    def rest_from(self, line, n):
        return " ".join(line.split(" ")[1:][n - 1:])

    def check(self, kind, fields, expectations):
        """expectations: {expression as the handler uses it: the marker
        value the $N inside it must carry}. Naming the consumer - the
        $dccore.dur() around the $4 - is what catches two fields swapped;
        checking that $4 merely appears did not (a mutant proved it)."""
        line = adminchat.structured_line(kind, fields)
        body = self.handler(kind)
        for expr, marker in expectations.items():
            self.assertIn(expr, body, f"{kind}: handler does not contain {expr}")
            match = re.search(r"\$(\d+)(-?)", expr)
            n, tail = int(match.group(1)), match.group(2)
            got = self.rest_from(line, n) if tail else self.token_of(line, n)
            self.assertEqual(got, marker, f"{kind}: {expr} carries {got!r}, expected {marker!r}")

    CHAN = {"channel": "#chan"}

    def test_request(self):
        self.check("REQUEST", {"nick": "N1", **self.CHAN, "kind": "folder", "name": "The Name"},
                   {"$2 $+ $dccore.in($3) asked for": "N1", "$dccore.in($3)": "#chan",
                    "$iif($4 == folder": "folder", "$dccore.name($5-)": "The Name"})

    def test_queued(self):
        self.check("QUEUED", {"nick": "N1", **self.CHAN, "pos": 7, "busy": 2, "slots": 3, "name": "The Name"},
                   {"for $2 $+ $dccore.in($3) at": "N1", "$dccore.in($3)": "#chan",
                    "# $+ $4": "7", "( $+ $5 $+ / $+ $6 slots busy)": "2",
                    "$6 slots busy": "3", "$dccore.name($7-)": "The Name"})

    def test_sending(self):
        self.check("SENDING", {"nick": "N1", **self.CHAN, "slot": 2, "slots": 3, "bytes": 999, "name": "The Name"},
                   {"to $2 $+ $dccore.in($3) (slot": "N1", "$dccore.in($3)": "#chan",
                    "(slot $4 $+ /": "2", "/ $+ $5 $+ ,": "3",
                    "$dccore.bytes($6)": "999", "$dccore.name($7-)": "The Name"})

    def test_resumed(self):
        self.check("RESUMED", {"nick": "N1", **self.CHAN, "at_bytes": 10, "total_bytes": 20, "name": "The Name"},
                   {"for $2 $+ $dccore.in($3) at": "N1", "$dccore.in($3)": "#chan",
                    "at $dccore.bytes($4) of": "10",
                    "of $dccore.bytes($5)": "20", "$dccore.name($6-)": "The Name"})

    def test_sent(self):
        self.check("SENT", {"nick": "N1", **self.CHAN, "bytes": 5, "seconds": 7.5, "bytes_per_s": 9, "name": "The Name"},
                   {"to $2 $+ $dccore.in($3) $+ :": "N1", "$dccore.in($3)": "#chan",
                    "$dccore.bytes($4) in": "5", "in $dccore.dur($5)": "7.5",
                    "at $dccore.speed($6)": "9", "$dccore.name($7-)": "The Name"})

    def test_fail(self):
        self.check("FAIL", {"nick": "N1", **self.CHAN, "acked": 3, "total": 4, "name": "The Name", "reason": "why"},
                   {"to $2 $+ $dccore.in($3) -": "N1", "$dccore.in($3)": "#chan",
                    "( $+ $dccore.bytes($4) of": "3",
                    "of $dccore.bytes($5) arrived)": "4", "var %rest = $6-": "The Name :: why"})
        self.assertIn("::", self.handler("FAIL"), "the name/reason split")

    def test_search(self):
        self.check("SEARCH", {"nick": "N1", **self.CHAN, "results": 12, "term": "the term"},
                   {"$2 $+ $dccore.in($3) searched": "N1", "$dccore.in($3)": "#chan",
                    "-> $4 result(s)": "12", "$dccore.name($5-)": "the term"})

    def test_a_channel_less_event_says_dash_and_the_script_prints_nothing_for_it(self):
        """A request by private message has no channel: the bot sends "-" so
        the token count does not move, and dccore.in answers nothing for it."""
        line = adminchat.structured_line("SEARCH", {"nick": "N1", "results": 1, "term": "x"})
        self.assertEqual(self.token_of(line, 3), "-")
        alias = script_text().replace("\r\n", "\n").split("alias dccore.in {", 1)[1].split("\n", 1)[0]
        self.assertIn("$1 == -", alias)
        self.assertIn("$1 == $null", alias)

    def test_log(self):
        self.check("LOG", {"category": "JOIN", "text": "the prose"},
                   {"var %cat = $2": "JOIN", "$dccore.tag(%cat,%group) $3-": "the prose"})

    def test_hello(self):
        line = adminchat.hello_line()
        body = self.handler("HELLO")
        self.assertEqual(self.token_of(line, 2),
                         "%s.%s" % (adminchat.PROTOCOL_MAJOR, adminchat.PROTOCOL_MINOR))
        self.assertIn("$gettok($2,1,46)", body, "the major is read out of major.minor")
        self.assertIn("%major != 1", body, "the major is checked")
        self.assertIn("%minor != $dccore.protominor", body, "and the minor (#639)")
        self.assertIn("$3", body)  # the bot's nick
        self.assertIn("$4-", body)  # the version

    def test_status_slot_and_queue(self):
        """STATUS is handed on as $2- to dccore.status, whose $1..$8 are
        the eight figures in the documented order; SLOT keeps $2- whole
        and the panel reads it by $gettok; QUEUE is keyed by $2 (pos)."""
        text = script_text().replace("\r\n", "\n")
        status = text.split("alias dccore.status {", 1)[1].split("\n}", 1)[0]
        for n, name in enumerate(("used", "slots", "qfiles", "qusers", "sent", "bytes", "bps", "record",
                                  "started", "failed", "searches"), start=1):
            self.assertIn(f"st.{name} ${n}", status, name)
        body = self.handler("STATUS")
        self.assertIn("dccore.status $2-", body)
        self.assertIn("$2-", self.handler("SLOT"))
        self.assertIn("queue. $+ $2 $3-", self.handler("QUEUE"))
        panel = text.split("alias dccore.panel {", 1)[1].split("\n}", 1)[0]
        # SLOT <nick> <sent> <total> <bps> <name>: percent is sent/total, speed is bps
        self.assertIn("$gettok(%l,2,32) * 100 / $gettok(%l,3,32)", panel)
        self.assertIn("$dccore.speed($gettok(%l,4,32))", panel)
        # QUEUE <pos> <nick> <files> <frozen_secs_left>, stored without pos
        self.assertIn("$gettok(%l,3,32) > 0", panel)  # frozen
        line = adminchat.status_lines()[0]
        self.assertEqual(len(line.split()), 13, "STATUS has eight figures and three since the bot started")

    def test_token(self):
        body = self.handler("TOKEN")
        self.assertIn("dccore.set token $3", body)  # DCCORE TOKEN <name> <token>


class TheDocsPointAtIt(unittest.TestCase):
    def test_admin_console_doc_has_the_section(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            doc = handle.read()
        self.assertIn("## The window, in mIRC", doc)
        self.assertIn("scripts/mirc/dccore.mrc", doc)
        self.assertIn("mIRC 6.10", doc)
        self.assertIn("/dccore pair", doc)

    def test_the_commands_in_the_doc_are_the_commands_in_the_script(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            doc = handle.read()
        documented = set(re.findall(r"(?m)^/dccore (\w+)", doc))
        implemented = set(re.findall(r"%cmd == (\w+)", script_text()))
        self.assertEqual(documented - implemented, set())
        self.assertEqual(implemented - documented - {"version"}, set())


if __name__ == "__main__":
    unittest.main()
