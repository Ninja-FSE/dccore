"""Two channel lines from a stranger used to hold the daemon in a reconnect loop.

parse_advert_slots() guarded the size field with str.isdigit() and then called
int() on it. isdigit() is True for characters int() REFUSES - superscript two
is the shortest - so the guard did not guard, and the ValueError escaped the
whole per-line block. The only handler that caught it closed the socket and
dropped into the reconnect loop.

What made it worth fixing before anything else was where the call sits.
_capture_channel_advert() runs on every channel line BEFORE the ban check and
BEFORE the anti-flood gate, deliberately - it observes and dispatches nothing,
and a foreign bot advertising in a channel we sit in is not subject to our ban
list. Correct placement, and exactly what made this unauthenticated, unmetered
and un-bannable.

It also survived a restart. The attacker's own registry entry is the only
precondition, it is written to data/known_bots.json, and it is read back at
startup - so after the first pair of lines, one CTCP line per reconnect was
enough, for as long as they cared to keep typing.

The second face of the same bug is quieter: isdigit() is ALSO True for digits
from other scripts that int() does accept. An Arabic-Indic zero was read as 0.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

T0 = 1_700_000_000.0

# The registration line the attacker sends first. The sender check only
# requires the advertised nick to equal the sender's own, so anybody can
# create their own registry entry.
REGISTER = "Type: @mallory For My List Of: 5 Files"


def slots_line(size_field):
    """A SLOTS reply whose size field is whatever is passed."""
    return f"\x01SLOTS 10 10 NOW 0 999 0 5 {size_field} 0 1 2 OmenServe v2.73\x01"


# Every shape that reaches the size field from a stranger. The first two are
# the exploit; the rest are the neighbourhood it lives in.
HOSTILE_FIELDS = [
    "²",        # superscript two - isdigit() True, int() raises
    "⁵",        # superscript five, same
    "½",        # vulgar one half - isnumeric but not isdigit
    "٠",        # Arabic-Indic zero - isdigit True AND int() accepts it
    "一",        # a CJK character
    "\U0001d7ce",    # mathematical bold digit zero
    "5.0",
    "-1",
    "0x10",
    "",
    " ",
    "9" * 400,       # far past any plausible size
    "²" * 50,
]


class TheGuardsOwnPremise(unittest.TestCase):
    """isdigit() was standing in for "int() will accept this". It does not."""

    def test_isdigit_is_true_for_characters_int_refuses(self):
        self.assertTrue("²".isdigit())
        with self.assertRaises(ValueError):
            int("²")

    def test_plain_int_refuses_them(self):
        for field in ("²", "⁵", "\U0001d7ce"):
            with self.subTest(field=repr(field)):
                self.assertIsNone(irc.plain_int(field))

    def test_and_refuses_a_digit_from_another_script_that_int_accepts(self):
        """The quieter half. int("٠") is 0, so the old code read an
        Arabic-Indic zero as a real size of zero rather than raising. A field
        another bot's script wrote as a decimal number is ASCII."""
        self.assertEqual(int("٠"), 0)

        self.assertIsNone(irc.plain_int("٠"))

    def test_it_still_reads_an_ordinary_number(self):
        self.assertEqual(irc.plain_int("4096"), 4096)
        self.assertEqual(irc.plain_int("0"), 0)

    def test_and_refuses_things_that_are_not_whole_numbers(self):
        for field in ("5.0", "-1", "0x10", "", " ", "1_0"):
            with self.subTest(field=repr(field)):
                self.assertIsNone(irc.plain_int(field))


class TheAttackItself(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        irc._advert_tails.clear()
        self.addCleanup(runtime.known_bots.clear)
        self.addCleanup(irc._advert_tails.clear)
        runtime.known_bots_flushed_at = T0   # suppress disk writes

    def send(self, text):
        irc._capture_channel_advert("mallory", "#dccore-test", text, now=T0)

    def test_the_two_lines_no_longer_take_the_connection_down(self):
        """The whole defect, in the order it was delivered."""
        self.send(REGISTER)
        self.assertIn("mallory", runtime.known_bots, "the setup line did not register")

        self.send(slots_line("²"))

    def test_no_hostile_size_field_raises(self):
        self.send(REGISTER)

        for field in HOSTILE_FIELDS:
            with self.subTest(field=repr(field[:12])):
                self.send(slots_line(field))

    def test_nor_does_the_parser_called_directly(self):
        """Belt and braces: the capture wrapper below would swallow an
        exception, so the parser is also asked on its own."""
        for field in HOSTILE_FIELDS:
            with self.subTest(field=repr(field[:12])):
                irc.parse_advert_slots(slots_line(field), 5)

    def test_a_hostile_field_yields_no_size_rather_than_a_wrong_one(self):
        """Refusing to answer is right. Recording a size the sender did not
        send would put a wrong number on the dashboard, which is worse than a
        missing one - the module's own reasoning for the ambiguity guard."""
        for field in ("²", "٠", "5.0", ""):
            with self.subTest(field=repr(field)):
                parsed = irc.parse_advert_slots(slots_line(field), 5) or {}

                self.assertNotIn("list_bytes", parsed)

    def test_an_ordinary_advert_is_unaffected(self):
        """The control. This is what the parser exists for."""
        parsed = irc.parse_advert_slots(slots_line("4096"), 5)

        self.assertEqual(parsed["list_bytes"], 4096)
        self.assertEqual(parsed["software"], "OmenServe v2.73")

    def test_a_garbage_numeral_does_not_leak_into_the_software_string(self):
        """The suffix scan keeps isdigit(), and that is not an oversight.

        The size field asks "can int() convert this", which isdigit() answered
        wrongly. This asks "is this field number-shaped, marking where the
        numeric region ends" - and a garbage numeral IS part of that region.
        Unifying the two put "² OmenServe v2.73" on the dashboard as the
        software name. Mutation testing is what found it; the first version of
        this test could not tell the difference because an ordinary "2" ended
        the scan before the superscript was ever reached.
        """
        line = "SLOTS 10 10 NOW 0 999 0 5 ² OmenServe v2.73"

        parsed = irc.parse_advert_slots(line, 5)

        self.assertEqual(parsed["software"], "OmenServe v2.73")


class NoCaptureCanBreakTheReadLoop(DCCoreTestCase):
    """Fixing the parser removes the one known way in. This removes the class.

    Both captures run before the ban check and the flood gate, so an exception
    in either escapes the per-line block and reaches the handler that closes
    the socket. Whatever else they learn to read, a line they cannot parse
    must cost exactly that line.
    """

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        runtime.known_bots_flushed_at = T0

    def exploding(self, *args, **kwargs):
        raise RuntimeError("a parser that did not expect this line")

    def test_the_advert_capture_contains_an_exploding_parser(self):
        self.addCleanup(setattr, irc, "parse_advert_slots", irc.parse_advert_slots)
        irc.parse_advert_slots = self.exploding
        irc._capture_channel_advert("mallory", "#dccore-test", REGISTER, now=T0)

        irc._capture_channel_advert("mallory", "#dccore-test", "\x01SLOTS 5\x01", now=T0)

    def test_the_advert_capture_contains_one_in_the_advert_parser_too(self):
        self.addCleanup(setattr, irc, "parse_channel_advert", irc.parse_channel_advert)
        irc.parse_channel_advert = self.exploding

        irc._capture_channel_advert("mallory", "#dccore-test", REGISTER, now=T0)

    def test_the_broadcast_capture_is_guarded_the_same_way(self):
        """It sits four lines above the advert capture in the same loop, and
        an exception in it costs the same connection.

        Driven through an OPEN broadcast window, because it no-ops instantly
        outside one - a test that skipped the window would pass whether the
        guard were there or not."""
        import list as list_mod
        import time as _time
        self.set_config(NICKNAME="DCCoreTest",
                        broadcast_search_inprogress=True,
                        broadcast_search_deadline=_time.time() + 30)
        self.addCleanup(setattr, list_mod, "strip_control_codes",
                        list_mod.strip_control_codes)
        list_mod.strip_control_codes = self.exploding

        irc._capture_broadcast_search_reply("mallory", "DCCoreTest", "a reply")

    def test_and_that_window_really_was_open(self):
        """Fixture invariant for the test above: with the window shut it
        returns before reaching anything that could raise, so the guard would
        not be exercised and the test would pass for the wrong reason."""
        import list as list_mod
        import time as _time
        self.set_config(NICKNAME="DCCoreTest",
                        broadcast_search_inprogress=True,
                        broadcast_search_deadline=_time.time() + 30)
        seen = []
        self.addCleanup(setattr, list_mod, "strip_control_codes",
                        list_mod.strip_control_codes)
        list_mod.strip_control_codes = lambda text: seen.append(text) or text

        irc._capture_broadcast_search_reply("mallory", "DCCoreTest", "a reply")

        self.assertEqual(seen, ["a reply"], "the capture returned early")

    def test_the_wrapper_keeps_the_function_it_wraps_identifiable(self):
        """functools.wraps, so a traceback and the log line below still name
        the real function rather than "guarded"."""
        self.assertEqual(irc._capture_channel_advert.__name__, "_capture_channel_advert")

    def test_it_says_which_line_it_gave_up_on(self):
        """Silently swallowing would trade a crash for an invisible blind
        spot. An operator has to be able to see that a parser is refusing
        input."""
        import contextlib
        import io as _io
        self.addCleanup(setattr, irc, "parse_advert_slots", irc.parse_advert_slots)
        irc.parse_advert_slots = self.exploding
        irc._capture_channel_advert("mallory", "#dccore-test", REGISTER, now=T0)

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            irc._capture_channel_advert("mallory", "#dccore-test", "\x01SLOTS 5\x01", now=T0)

        printed = buffer.getvalue()
        self.assertIn("_capture_channel_advert", printed)
        self.assertIn("RuntimeError", printed)


if __name__ == "__main__":
    unittest.main()
