"""A field went into the feed and nothing said so to an old script (audit
M37, #639).

The channel field after <nick> was inserted into seven structured lines
with PROTOCOL_MAJOR left at 1, on the reasoning that the protocol was
unreleased. The script only refuses `$2 != 1`, so an already-loaded older
dccore.mrc connected to the new bot without a word and read the channel as
the position, the slot, the byte count - "slot #mp3/1", "at ##mp3" - and no
document said how to replace a loaded script or what a mismatch looks like.
(The operator hit exactly this the week the field went in.)

HELLO now carries major.minor. The minor goes up every time a field is
inserted into a major-1 line. A script that knows the major but a different
minor keeps parsing and warns which side to update; the pre-minor script's
own `$2 != 1` refuses "1.1" outright and falls back to plain mode with
"Update the script" - the message it was missing. The bot's constant and the
script's own minor are held to each other here, so bumping one without the
other fails before it ships.
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

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_the_bots_window_in_mirc import script_text  # noqa: E402


def hello_handler():
    text = script_text().replace("\r\n", "\n")
    block = text.split("alias dccore.structured {", 1)[1]
    return block[block.index("if (%type == HELLO)"):].split("\n  if (%type ==", 1)[0]


def script_alias(name):
    match = re.search(r"^alias %s \{ return (\S+) \}$" % re.escape(name),
                      script_text().replace("\r\n", "\n"), re.M)
    return match.group(1) if match else None


class TheBotSaysMajorDotMinor(DCCoreTestCase):

    def test_hello_carries_both(self):
        self.set_config(NICKNAME="MusicBot", SCRIPT_VERSION="DCCore v9.9")

        self.assertEqual(adminchat.hello_line().split(" ")[2],
                         "%d.%d" % (adminchat.PROTOCOL_MAJOR, adminchat.PROTOCOL_MINOR))

    def test_the_minor_has_been_bumped_for_the_channel_field(self):
        """Zero would say nothing was ever inserted; the channel field was."""
        self.assertEqual(adminchat.PROTOCOL_MAJOR, 1)
        self.assertGreaterEqual(adminchat.PROTOCOL_MINOR, 1)

    def test_the_pre_minor_script_would_refuse_it(self):
        """The script that shipped before this checked `$2 != 1`, which is a
        numeric comparison in mIRC: "1.1" != 1 is true, so that script falls
        back to plain mode and says "Update the script" instead of parsing
        the channel as a number. That is the behaviour this relies on, so
        the token must never read as exactly 1 again."""
        token = adminchat.hello_line().split(" ")[2]

        self.assertNotEqual(float(token), 1.0)


class TheScriptKnowsItsMinor(unittest.TestCase):

    def test_the_script_and_the_bot_agree(self):
        """Bump one, bump the other - or every loaded script warns against
        the very bot it shipped with."""
        self.assertEqual(script_alias("dccore.protominor"), str(adminchat.PROTOCOL_MINOR))

    def test_the_script_version_moved_with_the_field(self):
        self.assertNotEqual(script_alias("dccore.ver"), "1.0",
                            "the script changed shape and still called itself 1.0")

    def test_the_handler_reads_major_and_minor_apart(self):
        body = hello_handler()

        self.assertIn("var %major = $gettok($2,1,46)", body)
        self.assertIn("var %minor = $gettok($2,2,46)", body)
        self.assertIn("if (%minor == $null) { var %minor = 0 }", body,
                      "a bot that says a bare 1 (before the minor existed) must not crash the compare")

    def test_an_unknown_major_still_means_plain_mode(self):
        body = hello_handler()
        refusal = body.split("if (%major != 1) {", 1)[1].split("}", 1)[0]

        self.assertIn("dccore.plain", refusal)
        self.assertIn("return", refusal)

    def test_an_unknown_minor_warns_and_carries_on(self):
        body = hello_handler()
        warning = body.split("if (%minor != $dccore.protominor) {", 1)[1].split("}", 1)[0]

        self.assertIn("dccore.sys", warning)
        self.assertIn("Update whichever is older", warning)
        self.assertIn("/reload -rs dccore.mrc", warning)
        self.assertNotIn("dccore.plain", warning, "a minor mismatch is a warning, not plain mode")
        self.assertNotIn("return", warning)
        # And the session still becomes structured after the warning.
        self.assertIn("hadd dccore.live mode structured", body.split("if (%minor != $dccore.protominor)", 1)[1])


class TheGuideSaysHowToUpdateAndWhatAMismatchLooksLike(unittest.TestCase):

    def guide(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            return handle.read()

    def test_hello_is_documented_as_major_dot_minor(self):
        guide = self.guide()

        self.assertIn("`DCCORE HELLO 1.1 <botnick> <version>`", guide)
        self.assertIn("refuse a major you do not know; a minor you do not\nknow", guide)

    def test_there_is_an_updating_the_script_section(self):
        section = self.guide().split("### Updating the script", 1)[1].split("\n### ", 1)[0]

        self.assertIn("/reload -rs dccore.mrc", section)
        self.assertIn("/dccore connect", section)
        self.assertIn("`/load` would add a second copy", section)

    def test_the_mismatch_is_in_the_troubleshooting_list(self):
        off = self.guide().split("### If something is off", 1)[1]

        self.assertIn("Channel names or nonsense numbers where the position, slot or size", off)
        self.assertIn("update it", off)


if __name__ == "__main__":
    unittest.main()
