"""Server numerics must not be forgeable by anyone typing in a channel.

The read loop sees every line as raw text, and several handlers used to test for
a bare substring - so a user could type "513 PONG x" in a public channel and the
daemon acted on it as though the server had sent it. Those handlers run BEFORE
the PRIVMSG parser, before security.check_user_status and before the flood gate,
so a banned user could trigger them too.

Each test names the handler it guards and asserts both directions: a genuine
server line is still accepted, and channel text is refused.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


# Real lines, in the shapes Undernet actually sends. The 353 variants matter:
# the symbol between target and channel is "=", "@" or "*" depending on channel
# mode, and the target is the bot's CURRENT nick, which may be the 433 fallback.
REAL_LINES = {
    "001": ":irc.undernet.org 001 DCCore :Welcome to the Undernet IRC Network",
    "376": ":irc.undernet.org 376 DCCore :End of /MOTD command.",
    "352": ":irc.undernet.org 352 DCCore #dccore-test user host server dave H :0 real",
    "353": ":irc.undernet.org 353 DCCore = #dccore-test :dave @alice +bob",
    "366": ":irc.undernet.org 366 DCCore #dccore-test :End of /NAMES list.",
    "433": ":irc.undernet.org 433 * DCCore :Nickname is already in use.",
    "513": ":irc.undernet.org 513 DCCore :To connect type /QUOTE PONG 1234567",
}


class GenuineNumericsStillWork(unittest.TestCase):
    """The guard must not be so strict that the bot stops answering the server."""

    def test_every_real_numeric_is_accepted(self):
        for code, line in REAL_LINES.items():
            with self.subTest(code=code):
                self.assertTrue(irc.is_server_numeric(line, code), line)

    def test_alternate_nick_as_target_is_accepted(self):
        """After a 433 the target is DCCore_, not DCCore."""
        line = ":Ashburn.Va.Us.Undernet.org 353 DCCore_ @ #dccore-test2 :erin dave"
        self.assertTrue(irc.is_server_numeric(line, "353"))

    def test_all_three_names_symbols_are_accepted(self):
        for symbol in ("=", "@", "*"):
            line = f":irc.undernet.org 353 DCCore {symbol} #dccore-test :dave alice"
            with self.subTest(symbol=symbol):
                self.assertTrue(irc.is_server_numeric(line, "353"))

    def test_a_long_server_hostname_is_accepted(self):
        line = ":Ashburn.Va.Us.Undernet.Org 001 DCCore :Welcome"
        self.assertTrue(irc.is_server_numeric(line, "001"))


class ChannelTextCannotForgeANumeric(unittest.TestCase):
    """Every one of these was accepted before the guard."""

    def test_forged_513_is_refused(self):
        """The critical one: 513 writes a raw PONG with no pacing and no ban check."""
        line = ":attacker!user@host PRIVMSG #dccore-test :hey 513 PONG abc"
        self.assertFalse(irc.is_server_numeric(line, "513"))

    def test_forged_353_is_refused(self):
        """353 populates channel_users, which dcc.py trusts to decide who is present."""
        line = ":attacker!u@h PRIVMSG #dccore-test :lol 353 x #dccore-test :victim"
        self.assertFalse(irc.is_server_numeric(line, "353"))

    def test_forged_352_is_refused(self):
        line = ":attacker!u@h PRIVMSG #dccore-test :a b c 352 d e f g victim"
        self.assertFalse(irc.is_server_numeric(line, "352"))

    def test_an_ordinary_track_request_is_not_read_as_001(self):
        """The old test was `"001" in line` - this is a normal music request."""
        line = ":alice!u@h PRIVMSG #dccore-test :!DCCore 001 - Enter Sandman.flac"
        self.assertFalse(irc.is_server_numeric(line, "001"))

    def test_an_ordinary_sentence_is_not_read_as_376(self):
        line = ":alice!u@h PRIVMSG #dccore-test :track 376 is missing from the list"
        self.assertFalse(irc.is_server_numeric(line, "376"))

    def test_a_notice_cannot_forge_one(self):
        line = ":attacker!u@h NOTICE DCCore :513 PONG hijack"
        self.assertFalse(irc.is_server_numeric(line, "513"))

    def test_a_bare_line_with_no_prefix_is_refused(self):
        """Server numerics always carry a prefix; a prefixless line is not one."""
        self.assertFalse(irc.is_server_numeric("513 PONG abc", "513"))

    def test_the_code_must_be_in_the_command_position(self):
        """A numeric appearing later in a real server line is not that numeric."""
        line = ":irc.undernet.org 372 DCCore :the motd mentions 513 PONG"
        self.assertFalse(irc.is_server_numeric(line, "513"))


def _guard_condition_for(marker):
    """Return the ``is_server_numeric`` condition gating the line that holds `marker`.

    The condition is READ OUT OF irc.py and evaluated by the tests below, rather
    than retyped here. tests/test_irc_dispatch.py established this pattern for the
    same reason it matters here: a test that only searches source text agrees with
    itself, not with the daemon. String matching cannot tell that an ``or`` became
    an ``and``, or that two guards were swapped so the 352 body is gated on "353" -
    both of those leave a text-matching test perfectly green.

    Walks OUTWARDS from the marker through the enclosing blocks, so an inner
    ``if len(parts) > 7:`` is stepped over and the numeric guard is what comes back.
    The marker must match exactly one non-comment line: a marker that matches two
    is a test quietly checking whichever handler happened to come first, and one
    that matches a comment is a test satisfied by prose.
    """
    with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
        lines = handle.read().split("\n")

    hits = [i for i, raw in enumerate(lines)
            if marker in raw and not raw.strip().startswith("#")]
    if len(hits) != 1:
        raise AssertionError(
            f"marker {marker!r} matched {len(hits)} lines in irc.py; it must match exactly one")

    index = hits[0]
    indent = len(lines[index]) - len(lines[index].lstrip())
    for previous in range(index - 1, -1, -1):
        raw = lines[previous]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        outer = len(raw) - len(raw.lstrip())
        if outer >= indent:
            continue
        indent = outer
        if stripped.startswith("if ") and stripped.endswith(":") and "is_server_numeric" in stripped:
            return stripped[len("if "):-1].strip()
    raise AssertionError(f"no is_server_numeric guard encloses the line holding {marker!r}")


def _evaluate(condition, line, **names):
    """Evaluate a condition lifted from irc.py against a given raw line."""
    namespace = {"line": line, "irc": irc, "is_server_numeric": irc.is_server_numeric,
                 # "config" is the NAME the lifted condition text uses (irc.py's own
                 # `import defaults as config` alias) - the module behind it is
                 # "defaults" since #170's RFC renamed the file.
                 "config": __import__("defaults"), "re": __import__("re")}
    namespace.update(names)
    return bool(eval(condition, namespace))  # noqa: S307 - source is our own file


class GuardsAreWiredToTheRightHandlers(unittest.TestCase):
    """Evaluate the real conditions, so a swapped or inverted guard fails."""

    def test_the_pong_guard_accepts_a_real_513_and_refuses_channel_text(self):
        condition = _guard_condition_for("parts[-1].strip()")
        self.assertTrue(_evaluate(condition, REAL_LINES["513"]), condition)
        self.assertFalse(
            _evaluate(condition, ":attacker!u@h PRIVMSG #dccore-test :hey 513 PONG abc"),
            condition)

    def test_the_pong_guard_still_requires_the_pong_keyword(self):
        """Anchoring must not have widened it to every 513."""
        condition = _guard_condition_for("parts[-1].strip()")
        other_513 = ":irc.undernet.org 513 DCCore :some other 513 message"
        self.assertFalse(_evaluate(condition, other_513), condition)

    def test_the_registration_guard_accepts_001_and_376_independently(self):
        """Catches an or/and slip, which would stop the bot ever joining a channel."""
        condition = _guard_condition_for("joined = True")
        for code in ("001", "376"):
            with self.subTest(code=code):
                self.assertTrue(_evaluate(condition, REAL_LINES[code], joined=False), condition)

    def test_the_registration_guard_ignores_a_track_named_001(self):
        condition = _guard_condition_for("joined = True")
        track = ":alice!u@h PRIVMSG #dccore-test :!DCCore 001 - Enter Sandman.flac"
        self.assertFalse(_evaluate(condition, track, joined=False), condition)

    def test_the_whois_guard_is_wired_to_352_not_something_else(self):
        """Catches the guards being swapped between the 352 and 353 handlers."""
        condition = _guard_condition_for("config.whois_status[target_nick]")
        self.assertTrue(_evaluate(condition, REAL_LINES["352"]), condition)
        self.assertFalse(_evaluate(condition, REAL_LINES["353"]), condition)

    def test_the_presence_guard_is_wired_to_353_not_something_else(self):
        condition = _guard_condition_for("name_match = re.search")
        self.assertTrue(_evaluate(condition, REAL_LINES["353"]), condition)
        self.assertFalse(_evaluate(condition, REAL_LINES["352"]), condition)

    def test_the_presence_guard_refuses_forged_channel_text(self):
        condition = _guard_condition_for("name_match = re.search")
        forged = ":attacker!u@h PRIVMSG #dccore-test :lol 353 x #dccore-test :victim"
        self.assertFalse(_evaluate(condition, forged), condition)


class DispatchConditionsUseTheGuard(unittest.TestCase):
    """The handlers must actually call it, not keep their own substring test."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            raw = handle.read()
        # Assert on CODE only. The fix's own comments quote the old substring tests
        # to explain why they were wrong, and a naive text search cannot tell an
        # explanation from live code - the first version of this test failed on the
        # very comments describing the thing it was checking for.
        self.source = self._code_only(raw)

    @staticmethod
    def _code_only(raw):
        """Drop comments and docstrings, keeping executable lines."""
        code_lines = []
        in_docstring = False
        for line in raw.split("\n"):
            stripped = line.strip()
            fence_count = stripped.count('"""') + stripped.count("'''")
            if in_docstring:
                if fence_count:
                    in_docstring = False
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # A one-line docstring opens and closes on the same line.
                if fence_count < 2:
                    in_docstring = True
                continue
            if stripped.startswith("#"):
                continue
            code_lines.append(line.split("  #")[0])
        return "\n".join(code_lines)

    def test_no_bare_substring_numeric_tests_remain(self):
        for stale in ('" 513 " in line', '" 352 " in line', '" 353 " in line',
                      '"001" in line', '"376" in line'):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, self.source,
                                 f"{stale} is forgeable by any user in a channel")

    def test_each_handler_calls_the_guard(self):
        for code in ("513", "001", "376", "352", "353"):
            with self.subTest(code=code):
                self.assertIn(f'is_server_numeric(line, "{code}")', self.source)

    def test_the_pong_reply_still_requires_the_pong_keyword(self):
        """Anchoring must not have widened it to every 513."""
        self.assertIn('is_server_numeric(line, "513") and "PONG" in line', self.source)


if __name__ == "__main__":
    unittest.main()
