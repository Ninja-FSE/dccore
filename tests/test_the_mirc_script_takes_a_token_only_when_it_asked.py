"""dccore.mrc took a `DCCORE TOKEN` line from whoever held the bot's nick.

The script dials the bot's nick by itself (on a JOIN of it, an IRC connect, a
retry), and anyone can take that nick while the bot is away. Every line in the
chat that started with DCCORE went to the structured-feed reader in any state,
before any login, and its TOKEN branch stored the token it carried and learned
`bothost` - the host the token is only ever sent to - from the nick's current
host. So whoever answered the script's own dial could plant a token and their
own host, and the next `Enter Your Password:` passed the host check (#585) on
the host they had just planted.

The script now:

* reads the structured feed only once logged in (state `in`);
* honours TOKEN only as the answer to a `pair` this script sent in this chat
  (`pairsent`), and only from a peer it had reason to trust - one at the host
  the bot was paired from (`vetted`, set where peerok said yes) or one the
  operator typed the password to (`typed`);
* starts every dial, and ends every chat, with those forgotten.

mIRC is not available here. The statements are read out of the script with
its comment lines removed, and the TOKEN branch's guards are evaluated by a
small evaluator of the conditions as written in the script - so the scenarios
below run the script's own conditions, not a copy of them.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def code():
    """The script without its comment lines (a ';' starts a comment only at
    the start of a line in mIRC), LF line ends."""
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                 encoding="ascii", newline="") as handle:
        text = handle.read().replace("\r\n", "\n")
    return "\n".join(line for line in text.split("\n") if not line.strip().startswith(";"))


def block(opening, text=None):
    """The braces block that starts with `opening`, by brace depth."""
    text = code() if text is None else text
    start = text.index(opening)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(opening)


# ---------------------------------------------------------------------------
# A small evaluator for the if-conditions these guards are written in. It
# knows exactly the shapes used - $dccore.st(<key>), %state == <word>, !, and
# a chain of && or of || - and raises on anything else, so a guard rewritten
# into a shape it does not know fails here rather than passing unread.
# ---------------------------------------------------------------------------

def _split_top(text, op):
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and text.startswith(op, i):
            parts.append(text[start:i])
            start = i + len(op)
            i += len(op)
            continue
        i += 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


def _wraps(text):
    """True when the first "(" is closed by the last ")"."""
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


def _truthy(value):
    return value not in (None, "", "0", 0, "$false")


def evaluate(condition, live):
    """mIRC's truth of `condition` with dccore.live holding `live`."""
    condition = condition.strip()
    for op, combine in (("||", any), ("&&", all)):
        parts = _split_top(condition, op)
        if len(parts) > 1:
            return combine(evaluate(part, live) for part in parts)
    if _wraps(condition):
        return evaluate(condition[1:-1], live)
    if condition.startswith("!"):
        return not evaluate(condition[1:], live)
    match = re.fullmatch(r"\$dccore\.st\((\w+)\)", condition)
    if match:
        return _truthy(live.get(match.group(1)))
    match = re.fullmatch(r"%state == (\w+)", condition)
    if match:
        return live.get("state") == match.group(1)
    raise AssertionError(f"the evaluator does not know this condition: {condition!r}")


def guards(branch, before):
    """[(condition, body)] for each one-line `if (...) { ... }` in `branch`
    above the first `before`."""
    head = branch[:branch.index(before)]
    found = []
    for line in head.split("\n")[1:]:
        match = re.fullmatch(r"\s*if (\(.*\)) \{ (.*) \}", line)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def token_is_taken(live):
    """Whether the TOKEN branch gets as far as storing the token, by the
    script's own guards."""
    branch = block("if (%type == TOKEN) {")
    for condition, body in guards(branch, "dccore.set token $3"):
        if evaluate(condition, live):
            assert body.rstrip().endswith("return"), body
            return False
    return True


def dispatched(live):
    """Whether dccore.line hands a DCCORE line to dccore.structured."""
    gate = block("if ($1 == DCCORE) {", block("alias dccore.line {"))
    match = re.search(r"\n\s*if (\(.*\)) \{ dccore\.structured \$2- \}\n", gate)
    assert match, gate
    return evaluate(match.group(1), live)


class TheEvaluatorReadsWhatItIsGiven(unittest.TestCase):
    """The evaluator first, on conditions of its own: what the scenarios
    below conclude rests on it."""

    def test_the_shapes(self):
        live = {"a": "1", "b": "0", "state": "in"}
        self.assertTrue(evaluate("($dccore.st(a))", live))
        self.assertFalse(evaluate("(!$dccore.st(a))", live))
        self.assertTrue(evaluate("(!$dccore.st(b)) && ($dccore.st(a))", live))
        self.assertFalse(evaluate("(!$dccore.st(a)) && (!$dccore.st(b))", live))
        self.assertTrue(evaluate("(!$dccore.st(a)) || (!$dccore.st(b))", live))
        self.assertTrue(evaluate("(%state == in)", live))
        self.assertFalse(evaluate("(%state == in)", {"state": "banner"}))
        self.assertFalse(evaluate("($dccore.st(missing))", live))

    def test_an_unknown_shape_is_refused(self):
        with self.assertRaises(AssertionError):
            evaluate("($dccore.opt(token) != $null)", {})


class TheTokenScenarios(unittest.TestCase):
    """The script's own guards, run against what each chat leaves in
    dccore.live."""

    def test_an_impostor_that_skips_the_login_is_not_heard_at_all(self):
        """The audit's chat: whoever answered the dial sends DCCORE TOKEN
        straight after the banner - state `banner`, nothing asked."""
        self.assertFalse(dispatched({"state": "banner"}))
        self.assertFalse(dispatched({"state": "auth"}))
        self.assertFalse(dispatched({"state": "password"}))

    def test_an_impostor_that_claims_the_login_is_not_asked_for_a_token(self):
        """It sends `Entering DCC Chat Admin Interface` itself (state `in`)
        and then the token: the script never sent `pair`."""
        live = {"state": "in", "byhand": "0", "vetted": "0", "typed": "0"}
        self.assertTrue(dispatched(live))
        self.assertFalse(token_is_taken(live))

    def test_a_pair_left_pending_does_not_open_the_door_to_an_impostor(self):
        """A /dccore pair that never finished leaves `pairing` set, so the
        script sends `pair` after ANY login line - even one the impostor
        claimed without a prompt. Nobody checked that peer: no token."""
        live = {"state": "in", "pairing": "1", "pairsent": "1", "byhand": "1",
                "vetted": "0", "typed": "0"}
        self.assertFalse(token_is_taken(live))

    def test_the_operators_first_pairing_is_taken(self):
        """/dccore pair: the password typed by hand, then `pair` sent."""
        live = {"state": "in", "pairing": "1", "pairsent": "1", "byhand": "1",
                "vetted": "0", "typed": "1"}
        self.assertTrue(token_is_taken(live))

    def test_pairing_again_in_a_chat_the_token_opened_is_taken(self):
        """Logged in with the stored token - peerok said yes before it went
        out - and /dccore pair typed in that chat."""
        live = {"state": "in", "pairing": "1", "pairsent": "1", "byhand": "0",
                "vetted": "1", "typed": "0"}
        self.assertTrue(token_is_taken(live))

    def test_a_token_nobody_asked_for_is_refused_even_from_the_real_bot(self):
        live = {"state": "in", "vetted": "1", "typed": "1"}
        self.assertFalse(token_is_taken(live))


class TheFlagsAreSetWhereTheyMeanSomething(unittest.TestCase):

    def test_every_pair_request_the_script_sends_is_remembered(self):
        text = code()
        sends = [line for line in text.split("\n") if "dccore.send pair $dccore.client" in line]
        self.assertEqual(len(sends), 2, sends)
        for line in sends:
            self.assertIn("dccore.send pair $dccore.client $dccore.ver | hadd dccore.live pairsent 1", line)

    def test_the_token_branch_forgets_the_request_before_it_stores(self):
        branch = block("if (%type == TOKEN) {")
        self.assertLess(branch.index("hdel dccore.live pairsent"), branch.index("dccore.set token $3"))
        self.assertLess(branch.index("dccore.set token $3"), branch.index("dccore.set bothost"))

    def test_vetted_is_set_only_where_peerok_said_yes(self):
        text = code()
        self.assertEqual(text.count("hadd dccore.live vetted 1"), 2)
        self.assertIn("if (!$dccore.peerok) { return }\n      hadd dccore.live vetted 1\n", text)
        self.assertIn("if (!$dccore.peerok password) { return }\n      hadd dccore.live vetted 1\n", text)

    def test_a_dial_starts_with_nothing_asked_and_nobody_vetted(self):
        body = block("alias dccore.connect {")
        self.assertIn("hadd dccore.live vetted 0", body)
        self.assertIn("hdel dccore.live pairsent", body)
        self.assertLess(body.index("hadd dccore.live vetted 0"), body.index("dcc chat $dccore.bot"))
        self.assertLess(body.index("hdel dccore.live pairsent"), body.index("dcc chat $dccore.bot"))

    def test_a_closed_chat_forgets_them(self):
        """A chat can also open without a dial of the script's (an offer
        from whoever holds the nick); it must not inherit the last one's."""
        body = block("on *:CHATCLOSE: {")
        for key in ("pairsent", "vetted", "typed"):
            self.assertIn(f"hdel dccore.live {key}", body, key)

    def test_the_feed_reader_is_reached_only_through_the_gate(self):
        line = block("alias dccore.line {")
        self.assertEqual(line.count("dccore.structured"), 1)
        gate = block("if ($1 == DCCORE) {", line)
        self.assertIn("return", gate)

    def test_the_version_is_bumped(self):
        self.assertIn("alias dccore.ver { return 1.19.4 }", code())


if __name__ == "__main__":
    unittest.main()
