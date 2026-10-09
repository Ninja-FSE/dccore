"""An mxrarserver 2.x advert puts the bot in the registry (#1209).

mxrarserver advertises both of its lists in one sentence:

    Type: @<trigger> to get list(s) of Files:(N) + Folders:(N) <sep> Slots:
    free/max <sep> Queue: q <sep> Sent: N (bytes) <sep> Speed: x <sep>
    Updated: dd/mmm/yy <sep> [ mxrarserver vX ]

None of the three advert parsers matched it, so such a bot never entered the
registry: no List Browser row, no auto-grab, its SLOTS CTCP ignored (that is
read only for a bot that has advertised), and a request to it while it was
away refused as a request to a stranger.

What it has to get right:

  * the word after "@" is a TRIGGER the operator chose, not the bot's nick -
    the sender stays the identity, exactly as for the RAR-folder advert, and
    the advert is not dropped for the two differing;
  * Files is its loose-file list and Folders the list of folders it packs, so
    they go to `files` and `rar_folders`, and only the types it serves are
    there;
  * counts carry thousands separators, and the separator between fields is
    whichever glyph the operator picked;
  * its SLOTS CTCP counts every entry it serves, which is Files + Folders,
    or Folders alone.

Every nick, trigger and channel here is invented.
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

T0 = 1000000.0
CTCP = chr(1)
SENDER = "SomeServer"
TRIGGER = "SomeTrigger"
CHANNEL = "#somechannel"

# Every separator mxrarserver 2.1.5 offers in its settings, as escapes so the
# source stays ASCII: black square, bullet, right guillemet, middle dot,
# asterisk, biohazard, black diamond, dark shade, sun, snowflake, four-pointed
# star, lightning.
SEPARATORS = ("■", "•", "»", "·", "*", "☣",
              "◆", "▓", "☀", "❄", "✦", "⚡")


def advert(sep="■", lists="Files:(1,234) + Folders:(56)", trigger=TRIGGER):
    return (f"Type: @{trigger} to get list(s) of {lists} {sep} Slots: 3/5 {sep} "
            f"Queue: 2 {sep} Sent: 1,099 (12.3 GB) {sep} Speed: 120KB/s {sep} "
            f"Updated: 05/Oct/26 {sep} [ mxrarserver v2.1.5 ]")


class ReadingTheAdvert(unittest.TestCase):

    def test_both_lists(self):
        found = irc.parse_channel_advert(advert())

        self.assertEqual(found["family"], "mx")
        self.assertEqual(found["files"], 1234)
        self.assertEqual(found["rar_folders"], 56)
        self.assertEqual(found["trigger"], TRIGGER)
        self.assertEqual(found["list_date"], "05/Oct/26")
        self.assertEqual(found["software"], "mxrarserver v2.1.5")
        self.assertEqual((found["slots_free"], found["slots_total"]), (3, 5))
        self.assertEqual(found["queued"], 2)
        self.assertEqual(found["speed"], "120KB/s")

    def test_files_only(self):
        found = irc.parse_channel_advert(advert(lists="Files:(12,345,678)"))

        self.assertEqual(found["files"], 12345678)
        self.assertNotIn("rar_folders", found)

    def test_folders_only(self):
        found = irc.parse_channel_advert(advert(lists="Folders:(9,876)"))

        self.assertEqual(found["rar_folders"], 9876)
        self.assertNotIn("files", found)

    def test_neither_list_is_not_this_advert(self):
        """An op telling somebody what to type is not a bot advertising."""
        self.assertIsNone(irc.parse_channel_advert(
            f"Type: @{TRIGGER} to get list(s) of things from that bot, it is great"))

    def test_every_separator(self):
        for sep in SEPARATORS:
            with self.subTest(sep=sep):
                found = irc.parse_channel_advert(advert(sep=sep))
                self.assertEqual((found["files"], found["rar_folders"]), (1234, 56))
                self.assertEqual(found["list_date"], "05/Oct/26")
                self.assertEqual(found["queued"], 2)
                self.assertEqual(found["software"], "mxrarserver v2.1.5")

    def test_colour_and_formatting_around_every_part(self):
        text = ("\x0304,01Type:\x03 \x02@SomeTrigger\x02 \x1dto get list(s) of\x1d "
                "\x0309Files:\x03(\x0308\x16 1,234 \x16\x03) + \x1fFolders:(56)\x1f "
                "\x0311•\x03 Slots: \x0303\x1e3/5\x1e • Updated: \x0305 05/Oct/26\x0f "
                "\x11[ mxrarserver v2.1.5 ]\x11")

        found = irc.parse_channel_advert(text)

        self.assertEqual(found["trigger"], TRIGGER)
        self.assertEqual((found["files"], found["rar_folders"]), (1234, 56))
        self.assertEqual((found["slots_free"], found["slots_total"]), (3, 5))
        self.assertEqual(found["list_date"], "05/Oct/26")
        self.assertEqual(found["software"], "mxrarserver v2.1.5")

    def test_the_trigger_makes_no_claim_about_the_sender(self):
        self.assertIsNone(irc.parse_channel_advert(advert())["nick"])

    def test_a_trigger_that_cannot_be_sent_is_dropped_and_the_advert_kept(self):
        found = irc.parse_channel_advert(advert(trigger="x" * 80))

        self.assertNotIn("trigger", found)
        self.assertEqual(found["files"], 1234)

    def test_the_other_wordings_still_read_as_before(self):
        omen = irc.parse_channel_advert(
            "Type: @SomeBot For My List Of: 719,041 Files <> Slots: 10/10 <> Queued: 0")
        rar = irc.parse_channel_advert(
            "Type @SomeBot^ to get my list of 39,454 (5.48 TB) RAR folders")

        self.assertEqual((omen["family"], omen["nick"], omen["files"]), ("omenserve", "SomeBot", 719041))
        self.assertEqual((rar["family"], rar["rar_folders"], rar["rar_trigger"]), ("rar", 39454, "SomeBot^"))


class TheRegistry(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        irc._advert_tails.clear()
        self.addCleanup(runtime.known_bots.clear)
        self.addCleanup(irc._advert_tails.clear)
        runtime.known_bots_flushed_at = T0      # no disk writes

    def capture(self, text, nick=SENDER, now=T0):
        irc._capture_channel_advert(nick, CHANNEL, text, now=now)

    def test_a_trigger_unlike_the_nick_is_recorded_under_the_sender(self):
        self.capture(advert(trigger="Totally-Different"))

        entry = runtime.known_bots[SENDER.lower()]
        self.assertEqual(entry["nick"], SENDER)
        self.assertEqual(entry["trigger"], "Totally-Different")
        self.assertEqual(entry["files"], 1234)
        self.assertEqual(entry["rar_folders"], 56)
        self.assertEqual(entry["software"], "mxrarserver v2.1.5")

    def test_a_request_to_it_while_it_is_away_is_not_a_stranger_s(self):
        import dcc_fetch
        self.assertFalse(dcc_fetch.bot_is_known(SENDER))

        self.capture(advert())

        self.assertTrue(dcc_fetch.bot_is_known(SENDER))

    def test_its_folder_list_says_it_packs(self):
        import list_fetch
        self.capture(advert(lists="Folders:(56)"))

        self.assertTrue(list_fetch.bot_publishes_a_rar_list(SENDER))

    def slots(self, entries):
        return (f"{CTCP}SLOTS 5 3 NOW 0 999 12000 {entries} 123456789012 0 "
                f"1790000000 3600 mxrarserver v2.1.5{CTCP}")

    def test_slots_counting_files_and_folders(self):
        self.capture(advert())

        self.capture(self.slots(1290), now=T0 + 3)

        entry = runtime.known_bots[SENDER.lower()]
        self.assertEqual(entry["list_bytes"], 123456789012)
        self.assertEqual(entry["software"], "mxrarserver v2.1.5")

    def test_slots_counting_folders_alone(self):
        self.capture(advert(lists="Folders:(56)"))

        self.capture(self.slots(56), now=T0 + 3)

        self.assertEqual(runtime.known_bots[SENDER.lower()]["list_bytes"], 123456789012)

    def test_slots_counting_files_alone(self):
        self.capture(advert(lists="Files:(1,234)"))

        self.capture(self.slots(1234), now=T0 + 3)

        self.assertEqual(runtime.known_bots[SENDER.lower()]["list_bytes"], 123456789012)

    def test_slots_that_agree_with_nothing_are_not_read(self):
        self.capture(advert())

        self.capture(self.slots(777), now=T0 + 3)

        self.assertNotIn("list_bytes", runtime.known_bots[SENDER.lower()])

    def test_the_first_count_that_agrees_is_the_answer(self):
        """A line that agrees with the file count and carries no size beside
        it published no size. It is not read again against the next count,
        where another field could happen to agree and a stranger's number be
        taken for the size."""
        self.capture(advert())

        self.capture(f"{CTCP}SLOTS 5 3 NOW 0 999 1290 99999999 1234{CTCP}", now=T0 + 3)

        self.assertNotIn("list_bytes", runtime.known_bots[SENDER.lower()])

    def test_another_family_is_calibrated_by_its_file_count_alone(self):
        """A bot that publishes both a file count and a RAR-folder count but
        does not run mxrarserver: a SLOTS field that happens to equal their
        sum is not read as its entries."""
        self.capture("Type: @SomeServer For My List Of: 1,234 Files <> Slots: 10/10")
        self.capture("Type @SomeServer^ to get my list of 56 (5.48 TB) RAR folders", now=T0 + 1)

        self.capture(f"{CTCP}SLOTS 5 3 NOW 0 999 0 1290 123456789012 0 1790000000 3600 "
                     f"OmenServe v2.73{CTCP}", now=T0 + 3)

        self.assertNotIn("list_bytes", runtime.known_bots[SENDER.lower()])


if __name__ == "__main__":
    unittest.main()
