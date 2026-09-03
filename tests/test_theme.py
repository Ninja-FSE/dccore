"""One palette for everything DCCore says, instead of eight copies of it.

The same five constants - a red border block, a cyan separator, a white text
plate, bold and reset - were literals inside eight separate functions across
announce.py and list.py, one per outbound message path. That list is the whole
outbound surface, which is right: one look everywhere is what a file server
wants, because in a busy channel a dozen bots advertise at once and the palette
is how a person tells them apart.

The values had not diverged. The SET had - three of the eight defined only four
of the five names. None of the three read the name it was missing, so nothing
was broken; the crack was real and had not yet cut anyone.

Two things this has to get right, and they pull against each other:

  * a refactor of the look must not CHANGE the look. Every path is pinned to
    the bytes it produced before theme.py existed;
  * and a theme has to actually reach every path, or the operator who picks
    one gets a bot that is half one colour scheme and half another.
"""

import io
import os
import re
import sys
import time
import unittest

BS = chr(92)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import list as list_mod  # noqa: E402
import theme  # noqa: E402

from tests._golden_palette import GOLDEN  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402

# The three block codes as they have always been. Modules spell them as the
# ESCAPE TEXT "\\x0304,05", not as the character it stands for, so a scan that
# looked only for the character would find nothing anywhere - including in a
# ninth copy somebody had just added. Both spellings are checked.
BLOCK_CODES = ("0304,05", "0310,10", "0301,00")
BLOCK_LITERALS = (tuple(chr(3) + code for code in BLOCK_CODES)
                  + tuple(BS + "x" + code for code in BLOCK_CODES))

FROZEN = 1700000000

# Every outbound path that carries the palette, except announce_worker - a
# while-loop thread with no callable seam. NoNinthCopy's source scan is what
# covers that one instead.
PATHS = (
    "send_transfer_complete",
    "send_dcc_sending_notice",
    "send_search_result_header",
    "send_dcc_queue_notice",
    "send_pack_error_notice",
    "send_debug",
    "list.send_list_trigger_info",
    "list.execute_search",
)


class ThemedPathCase(DCCoreTestCase):
    """Drives every outbound path that carries the palette."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        # DEBUG_CHANNEL is named explicitly rather than inherited: these are
        # byte-identical golden fixtures, and send_debug's line embeds the
        # channel. Leaning on the shipped default made the palette fixtures
        # hostage to an unrelated decision about where debug output goes -
        # which is exactly what changed when it started shipping blank.
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", SCRIPT_VERSION="vTest", MAX_DCC_SLOTS=5,
                        STATS_FILE=os.path.join(self.tree.root, "stats.txt"),
                        DEBUG_CHANNEL="#dccore-debug", THEME="classic")
        self.addCleanup(setattr, db, "SPEED_RECORD_FILE", db.SPEED_RECORD_FILE)
        db.SPEED_RECORD_FILE = os.path.join(self.tree.root, "speed.txt")
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write("100 200 3 4 5 6 2026-08-30")
        db._advanced_stats_cache = None
        with io.open(os.path.join(self.tree.lists, "DCCoreTest-2026-08-30.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("head\n!DCCoreTest Metallica - Enter Sandman.flac  ::INFO:: 4.0MB\n")

        # These templates stamp the time, so a capture taken a minute later
        # differs for a reason that is not the code.
        #
        # gmtime, not localtime: freezing the EPOCH alone is not enough,
        # because the templates render it in the machine's zone. The first
        # version of this froze the timestamp and rendered it locally, which
        # baked the author's UTC+2 into the golden file and failed on CI at
        # UTC - on all four platforms, which is at least how it announced
        # itself. time.tzset() would fix it on POSIX and does not exist on
        # Windows, so the conversion is pinned instead of the environment.
        real = time.strftime
        self.addCleanup(setattr, time, "strftime", real)
        time.strftime = lambda fmt, t=None: real(fmt, time.gmtime(FROZEN))

        # list.py bound `oserve` at import, so replacing sys.modules is not
        # enough for the paths that use the module global rather than a
        # sys.modules.get() lookup.
        self.addCleanup(setattr, list_mod, "oserve", list_mod.oserve)
        list_mod.oserve = self.oserve

    def drive(self, label):
        """Run one path and return the lines it queued."""
        self.oserve.queued.clear()
        if label == "send_transfer_complete":
            announce.send_transfer_complete("#dccore-test", "dave",
                                            "Enter Sandman.flac", 4096, 1000.0, 512000)
        elif label == "send_dcc_sending_notice":
            announce.send_dcc_sending_notice("dave", "Enter Sandman.flac")
        elif label == "send_search_result_header":
            announce.send_search_result_header("dave", "metallica", 3, "#dccore-test")
        elif label == "send_dcc_queue_notice":
            announce.send_dcc_queue_notice("dave", "Enter Sandman.flac", 2)
        elif label == "send_pack_error_notice":
            announce.send_pack_error_notice(None, "dave")
        elif label == "send_debug":
            # send_debug does not go through oserve at all: it appends to a
            # deque that a drain thread writes to a raw socket. Read the deque
            # rather than starting the thread.
            announce._debug_queue.clear()
            announce.send_debug("a debug line", "INFO")
            return list(announce._debug_queue)
        elif label == "list.send_list_trigger_info":
            list_mod.send_list_trigger_info(None, "dave")
        elif label == "list.execute_search":
            config.search_inprogress = False
            list_mod.execute_search(None, "dave", "metallica", "#dccore-test")
        else:
            raise AssertionError("unknown path " + label)
        return [message for _user, message, _vip in self.oserve.queued]


class TheLookDidNotChange(ThemedPathCase):
    """A refactor of the palette that alters one byte of outbound text is not a
    refactor. Every path is pinned to what it produced before theme.py."""

    def test_every_path_is_byte_identical_under_the_classic_theme(self):
        for label in PATHS:
            with self.subTest(path=label):
                self.assertEqual(self.drive(label), GOLDEN[label])

    def test_the_golden_file_carries_no_timezone(self):
        """The failure that reached CI on all four platforms.

        Freezing the epoch is not enough: these templates render it in the
        machine's zone, so the first version of this file baked the author's
        UTC+2 into the fixture and every platform at UTC disagreed. The harness
        pins the CONVERSION now, and this says so - so an edit that goes back
        to localtime fails here, on the machine that made it, rather than
        twenty minutes later on somebody else's.
        """
        stamped = [line for lines in GOLDEN.values() for line in lines
                   if "as of " in line]
        self.assertTrue(stamped, "no fixture line carries a timestamp any more")

        expected = time.strftime("%I:%M %p", time.gmtime(FROZEN)).lower().lstrip("0")
        for line in stamped:
            self.assertIn(expected, line,
                          "the golden file was captured in a local timezone")

    def test_the_fixture_covers_every_path_that_reads_the_palette(self):
        """Control. A golden file that quietly lost an entry would let a change
        to that path through, and the pass would read the same either way."""
        source = "".join(io.open(os.path.join(REPO_ROOT, name), encoding="utf-8").read()
                         for name in ("announce.py", "list.py"))
        readers = source.count("theme.blocks()") + source.count("theme.palette()")

        # announce_worker is the one reader with no fixture: it is a while-loop
        # thread, and the source scan below is what covers it instead.
        self.assertEqual(len(PATHS), readers - 1,
                         "a path reads the palette but is not pinned by the golden file")
        self.assertEqual(sorted(GOLDEN), sorted(PATHS),
                         "the golden file and the path list have drifted apart")


class NoNinthCopy(unittest.TestCase):
    """The defect was eight copies of one palette. A test that only pinned the
    output would pass just as happily on nine."""

    def modules(self):
        for name in sorted(os.listdir(REPO_ROOT)):
            if name.endswith(".py") and name not in ("theme.py", "admin_config.py"):
                yield name

    def test_no_module_but_theme_contains_a_block_code(self):
        offenders = []
        for name in self.modules():
            with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    for literal in BLOCK_LITERALS:
                        if literal in line:
                            offenders.append(f"{name}:{number}")
        self.assertEqual(offenders, [], "a palette literal is back outside theme.py: "
                                        + ", ".join(offenders))

    def test_the_scan_would_notice_one(self):
        """Fixture invariant: theme.py holds all three, so a scan that found
        nothing anywhere would be broken rather than reassuring. This control
        is what caught the scan looking for the character when every module
        spells it as an escape."""
        with io.open(os.path.join(REPO_ROOT, "theme.py"), encoding="utf-8") as handle:
            body = handle.read()

        for code in BLOCK_CODES:
            with self.subTest(code=code):
                spellings = (chr(3) + code, BS + 'x' + code)
                self.assertTrue(any(s in body for s in spellings),
                                f"theme.py has no {code} in either spelling")


class EveryPresetIsComplete(unittest.TestCase):
    """The actual defect, in the form it would come back. Three of the eight
    copies defined four of the five names; a preset missing a role is the same
    hole one layer up, and it would raise KeyError in blocks() at the moment
    somebody selected it - on the running bot, not here."""

    def test_every_theme_defines_every_role(self):
        roles = set(theme.CLASSIC)
        for name, palette in theme.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(palette), roles,
                                 f"{name} does not define the same roles as classic")

    def test_the_default_exists(self):
        self.assertIn(theme.DEFAULT_THEME, theme.THEMES)

    def test_classic_is_the_default(self):
        """No existing install may silently change identity. The palette is how
        people tell one bot from another in a channel."""
        self.assertEqual(theme.DEFAULT_THEME, "classic")
        self.assertEqual(config.THEME, "classic")

    def test_the_presets_are_distinguishable_from_each_other(self):
        """Shipping four presets that look alike would move the problem rather
        than fix it: two operators who both picked from the list would be as
        indistinguishable as two who both took the default."""
        seen = {}
        for name, palette in theme.THEMES.items():
            signature = (palette["border"], palette["separator"], palette["textbox"])
            self.assertNotIn(signature, seen,
                             f"{name} and {seen.get(signature)} look the same")
            seen[signature] = name

    def test_bold_and_reset_are_not_themeable(self):
        """They are IRC control characters with fixed meanings. A theme that
        redefined them would be redefining the protocol, and half the clients
        in the channel would disagree about what the line said."""
        for palette in theme.THEMES.values():
            self.assertNotIn("bold", palette)
            self.assertNotIn("reset", palette)


class ChoosingATheme(ThemedPathCase):

    def blocks_in(self, lines):
        return [code for code in re.findall(chr(3) + r"\d{1,2},\d{1,2}", " ".join(lines))]

    def test_a_different_theme_reaches_every_path(self):
        """The point of the whole change. A theme that only reached some paths
        would leave the operator with a bot that is half one scheme and half
        another - which is worse than not offering the choice."""
        for label in PATHS:
            with self.subTest(path=label):
                self.set_config(THEME="midnight")
                changed = self.drive(label)

                self.assertNotEqual(changed, GOLDEN[label],
                                    "this path ignored the theme")

    def test_plain_sends_no_colour_selection_at_all(self):
        """A bare chr(3) is a colour RESET, which is protocol rather than
        palette; what plain must not emit is a colour being SELECTED.

        send_debug is exempt on purpose. Its category tag is coloured by
        meaning - purple QUIT, cyan JOIN, grey INFO - and an operator reads
        that colour before the word. The debug channel is the operator's own,
        not one where formatting is banned."""
        self.set_config(THEME="plain")
        selects = re.compile(chr(3) + r'\\d')

        for label in [p for p in PATHS if p != "send_debug"]:
            with self.subTest(path=label):
                for line in self.drive(label):
                    self.assertIsNone(selects.search(line), line)

    def test_the_debug_category_colours_are_deliberately_not_themed(self):
        """Stated as a test so it reads as a decision and not an oversight."""
        self.set_config(THEME="plain")

        line = self.drive("send_debug")[0]

        self.assertIn(chr(3) + "14", line, "the INFO tag lost its colour")

    def test_plain_still_sends_the_words(self):
        """Stripping the colour must not strip the message. "plain" is for a
        channel that bans formatting, not for a bot that says nothing."""
        self.set_config(THEME="plain")

        self.assertIn("Enter Sandman.flac", self.drive("send_transfer_complete")[0])

    def test_the_segment_count_does_not_change_with_the_theme(self):
        """Colour codes cost bytes against IRC's 512, and announce.py keeps a
        420-byte budget for the pessimistic hostmask. A preset that added a
        decorative block would eat into the FILENAME budget on every line that
        carries a filename - so presets change which colours the segments are,
        never how many there are."""
        self.set_config(THEME="classic")
        baseline = len(self.blocks_in(self.drive("send_transfer_complete")))

        for name in theme.THEMES:
            if name == "plain":
                continue
            with self.subTest(theme=name):
                self.set_config(THEME=name)

                self.assertEqual(len(self.blocks_in(self.drive("send_transfer_complete"))),
                                 baseline)

    def test_every_themed_line_still_fits_the_budget(self):
        for name in theme.THEMES:
            self.set_config(THEME=name)
            for label in PATHS:
                for line in self.drive(label):
                    with self.subTest(theme=name, path=label):
                        self.assertLess(len(line.encode("utf-8")),
                                        announce.IRC_LINE_BUDGET)


class AThemeNameThatIsNotOne(ThemedPathCase):

    def test_it_falls_back_to_classic(self):
        """The alternative is a bot whose every message carries the empty string
        where a colour should be."""
        self.set_config(THEME="chartreuse")

        self.assertEqual(theme.theme_name(), "classic")

    def test_it_says_so_rather_than_silently_using_the_default(self):
        import contextlib
        self.set_config(THEME="chartreuse")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            theme.theme_name()

        self.assertIn("chartreuse", buffer.getvalue())

    def test_an_empty_or_missing_setting_is_the_default_too(self):
        for value in ("", None, "  "):
            with self.subTest(value=repr(value)):
                self.set_config(THEME=value)

                self.assertEqual(theme.theme_name(), "classic")

    def test_case_and_spacing_are_not_part_of_the_name(self):
        self.set_config(THEME="  MIDNIGHT ")

        self.assertEqual(theme.theme_name(), "midnight")


class OverridingOneRole(ThemedPathCase):
    """config.CUSTOM_THEME_<ROLE>, for an operator who would rather be unique
    than pick from a list. #170's RFC flattened this from a single
    CUSTOM_THEME dict into six plain strings, one per role - see config.py's
    own comment on that change for why."""

    def test_it_changes_only_the_role_it_names(self):
        self.set_config(THEME="classic", CUSTOM_THEME_BORDER="\x0306,06")
        palette = theme.palette()

        self.assertEqual(palette["border"], "\x0306,06")
        self.assertEqual(palette["separator"], theme.CLASSIC["separator"])

    def test_it_reaches_the_wire(self):
        self.set_config(THEME="classic", CUSTOM_THEME_BORDER="\x0306,06")

        self.assertIn("\x0306,06", self.drive("send_transfer_complete")[0])

    def test_every_role_has_its_own_setting(self):
        """All six, not just border - each name maps to exactly the role
        theme.palette() reads it back into."""
        overrides = {
            "CUSTOM_THEME_BORDER": "\x0306,06",
            "CUSTOM_THEME_SEPARATOR": "\x0313,13",
            "CUSTOM_THEME_TEXTBOX": "\x0301,00",
            "CUSTOM_THEME_VALUE": "\x0304",
            "CUSTOM_THEME_ALERT": "\x0307",
            "CUSTOM_THEME_ACCENT": "\x0308",
        }
        self.set_config(THEME="classic", **overrides)
        palette = theme.palette()

        self.assertEqual(palette["border"], overrides["CUSTOM_THEME_BORDER"])
        self.assertEqual(palette["separator"], overrides["CUSTOM_THEME_SEPARATOR"])
        self.assertEqual(palette["textbox"], overrides["CUSTOM_THEME_TEXTBOX"])
        self.assertEqual(palette["value"], overrides["CUSTOM_THEME_VALUE"])
        self.assertEqual(palette["alert"], overrides["CUSTOM_THEME_ALERT"])
        self.assertEqual(palette["accent"], overrides["CUSTOM_THEME_ACCENT"])

    def test_the_default_of_none_keeps_the_presets_own_value(self):
        """None (the shipped default of every CUSTOM_THEME_<ROLE> setting) is
        "not overridden", not "set to nothing" - the same convention
        RAR_BINARY already uses for "unset means look on PATH"."""
        self.set_config(THEME="classic")

        self.assertEqual(theme.palette(), dict(theme.CLASSIC))

    def test_an_empty_string_also_keeps_the_presets_own_value(self):
        """A settings.conf line uncommented and left blank coerces to "" for
        a str-typed setting whose default is None - see coerce()'s own
        handling of that case - so this must behave the same as never having
        set it, not as "blank this role out"."""
        self.set_config(THEME="classic", CUSTOM_THEME_BORDER="")

        self.assertEqual(theme.palette()["border"], theme.CLASSIC["border"])

    def test_a_non_string_value_is_ignored(self):
        """It goes straight into an f-string on the wire. Only reachable via
        admin_config.py (Python) - settings.conf's coerce() always produces a
        string or None for a str-typed setting."""
        self.set_config(CUSTOM_THEME_BORDER=6)

        self.assertEqual(theme.palette()["border"], theme.CLASSIC["border"])


if __name__ == "__main__":
    unittest.main()
