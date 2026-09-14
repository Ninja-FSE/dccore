"""Two changes to what the bot says, both asked for directly.

    "Should be able to hide that message if you don't want to send public
    messages. In settings. Also theme shouldn't have bold in any location of
    the message. That's for everywhere bot advertisement answers to find
    requests etc. No bolds"

ONE PUBLIC MESSAGE, AND NOW IT IS OPTIONAL. A send produces four messages and
only one of them is public:

    queue position   NOTICE to the requester        private
    "Sending"        NOTICE to the requester        private
    DCC SEND         CTCP PRIVMSG to the requester  private
    "Sent: ... To:"  PRIVMSG to the CHANNEL         public   <- this one

So ANNOUNCE_TRANSFERS gates that one and nothing else. Turning it off does not
make a request go unanswered - the person who asked still gets told everything
they were told before. It only stops the channel being told afterwards.

AND IT DOES NOT TAKE THE OPERATOR'S OWN LOG WITH IT. send_transfer_complete()
also writes the debug line that records the send. An operator who does not want
the channel told is not an operator who wants to stop seeing their own
transfers, so the gate is around the channel send alone.

NO BOLD, ANYWHERE. Fifty markers across eight outbound paths - the advert, the
completion notice, @find results, the private notices, the debug channel. The
golden fixture moved with them, by stripping \\x02 and by nothing else, after
proving that stripping it from each old line reproduced the new line exactly.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import theme  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOLD = "\x02"


class NoOutboundPathUsesBold(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")

    def test_the_advert_has_none(self):
        line = announce.build_advert_line(
            "#somechannel", "SomeBot", "1", "1MB", "Sep 7th", "1/1", "0",
            "1MB/s", "1MB/s", "1 File", "v1")

        self.assertNotIn(BOLD, line)

    def test_the_completion_notice_has_none(self):
        line = announce.build_transfer_complete_line(
            "#somechannel", "someone", "A.flac", "1 File", "0", "1",
            "3:04 pm", "1MB/s")

        self.assertNotIn(BOLD, line)

    def test_no_template_in_announce_py_carries_one(self):
        """The property, over every path at once - including the ones that
        need a socket and cannot be driven from here."""
        with open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as f:
            source = f.read()

        self.assertNotIn("{B}", source)

    def test_bold_is_still_defined_because_it_is_still_a_protocol_constant(self):
        """Removed from the templates, not from the file. theme.py's own note
        is that bold and reset are IRC control characters with fixed meanings,
        and that remains true whether or not anything uses them."""
        self.assertEqual(theme.BOLD, BOLD)

    def test_and_blocks_still_returns_eight_values(self):
        """Every call site unpacks eight. Dropping the name to tidy up would
        have touched all of them to say nothing."""
        self.assertEqual(len(theme.blocks()), 8)


class TheChannelNoticeCanBeTurnedOff(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic", NICKNAME="SomeBot",
                        CHANNEL="#somechannel")

    def sent_to_channel(self):
        """Every line the fake oserve was asked to queue for a channel."""
        oserve = sys.modules.get("oserve")
        return [msg for kind, msg in getattr(oserve, "queued", [])
                if kind == "channel_announce"]

    def test_it_ships_on(self):
        """Every install today announces, and an upgrade must not silently
        stop."""
        self.assertIs(config.SHIPPED_VALUES["ANNOUNCE_TRANSFERS"], True)

    def test_it_is_a_setting_the_page_offers(self):
        names = set()
        for _cid, _label, fields in webserver.SETTINGS_CATEGORIES:
            names |= set(fields)

        self.assertIn("ANNOUNCE_TRANSFERS", names)

    def test_it_has_a_label_rather_than_showing_its_own_name(self):
        self.assertIn("ANNOUNCE_TRANSFERS", webserver.SETTINGS_LABELS)

    def test_the_gate_is_around_the_channel_send_only(self):
        """Asserted on the source because the alternative - gating the whole
        function - would also take the operator's own debug line, and both
        versions pass any test that only checks the channel went quiet."""
        with open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as f:
            source = f.read()
        body = source.split("def send_transfer_complete(", 1)[1] \
                     .split("\ndef ", 1)[0]

        gated = body.split('if getattr(config, "ANNOUNCE_TRANSFERS", True):', 1)[1]
        self.assertIn('oserve.queue_message("channel_announce", msg)',
                      gated.split("\n    else:", 1)[0])
        self.assertIn("send_debug(", body)
        self.assertNotIn("send_debug(", gated.split("\n    else:", 1)[0])

    def test_off_still_says_so_in_the_log(self):
        """Silence in the channel is wanted; silence in the operator's own
        console is how a setting gets blamed for a bug."""
        with open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as f:
            source = f.read()

        self.assertIn("the \"\n              f\"channel notice is off (ANNOUNCE_TRANSFERS).",
                      source)

    def test_the_private_notices_are_not_affected(self):
        """The point of the setting. Whoever asked still gets told - it is the
        channel that stops being told."""
        with open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as f:
            source = f.read()

        for name in ("send_dcc_sending_notice", "send_dcc_queue_notice"):
            with self.subTest(path=name):
                body = source.split("def %s(" % name, 1)[1].split("\ndef ", 1)[0]
                self.assertNotIn("ANNOUNCE_TRANSFERS", body)

    def test_nor_is_the_dcc_offer_itself(self):
        with open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as f:
            self.assertNotIn("ANNOUNCE_TRANSFERS", f.read())


class TheSampleAndTheSettingAgree(unittest.TestCase):

    def test_the_shipped_sample_carries_it(self):
        with open(os.path.join(REPO_ROOT, "settings.conf.sample"),
                  encoding="utf-8") as f:
            sample = f.read()

        self.assertIn("ANNOUNCE_TRANSFERS", sample)

    def test_and_explains_what_stays_on(self):
        """An operator reading only the sample should not have to guess
        whether turning this off stops answering requests."""
        with open(os.path.join(REPO_ROOT, "settings.conf.sample"),
                  encoding="utf-8") as f:
            sample = f.read()
        note = sample.split("ANNOUNCE_TRANSFERS", 1)[0].rsplit("\n\n", 1)[-1]

        self.assertIn("private", note)
        self.assertIn("debug", note)


if __name__ == "__main__":
    unittest.main()
