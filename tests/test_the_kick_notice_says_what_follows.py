"""The kick notice says what will happen next (#1281).

With REJOIN_ATTEMPTS at 0 the bot never rejoins, and since #1271 it drops a
kicked channel's member list at once for that reason - but the notice the
operator got still said "Will try to rejoin on the next advert". It now says
no rejoin follows; with rejoining on, it still promises the one that does.

Driven through the real irc.irc_loop() against a scripted socket, with the
harness tests/test_audit_irc_connection.py uses for the same kick.
"""

import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402
from tests.test_audit_irc_connection import (  # noqa: E402
    SERVER, _DrivesTheReadLoop, activated, sent_contains, until)

PROMISE = "Will try to rejoin on the next advert"


class TheNoticeText(DCCoreTestCase):

    def test_rejoining_off_promises_nothing(self):
        self.set_config(REJOIN_ATTEMPTS=0)

        text = irc.kicked_notice("#somewhere", "someop")

        self.assertEqual(text, "Kicked from #somewhere by someop. "
                               "Not rejoining: REJOIN_ATTEMPTS is 0.")

    def test_rejoining_on_promises_the_rejoin(self):
        self.set_config(REJOIN_ATTEMPTS=3)

        self.assertEqual(irc.kicked_notice("#somewhere", "someop"),
                         f"Kicked from #somewhere by someop. {PROMISE}.")

    def test_a_negative_count_is_off_too(self):
        """The rejoin rule itself reads anything below 1 as off."""
        self.set_config(REJOIN_ATTEMPTS=-1)

        self.assertNotIn(PROMISE, irc.kicked_notice("#somewhere", "someop"))
        self.assertEqual(irc.channels_to_rejoin(), [])


class TheReadLoopSendsIt(_DrivesTheReadLoop):

    def kick(self):
        captured = silence_debug(announce)

        def kicked():
            return "#music" in config.kicked_channels

        self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (sent_contains("JOIN #music"), ":SomeBot!s@host.example JOIN #music"),
            (None, SERVER + " 353 SomeBot = #music :SomeBot @someop alfa"),
            (None, SERVER + " 366 SomeBot #music :End of /NAMES list."),
            (activated, ":someop!o@host.example KICK #music SomeBot :out"),
            (until(kicked), SERVER + " NOTICE SomeBot :tick"),
        ])
        return [text for _category, text in captured if text.startswith("Kicked from")]

    def test_with_rejoining_off_the_notice_says_so(self):
        self.set_config(REJOIN_ATTEMPTS=0)

        sent = self.kick()

        self.assertEqual(sent, ["Kicked from #music by someop. "
                                "Not rejoining: REJOIN_ATTEMPTS is 0."])

    def test_with_rejoining_on_it_still_promises_the_rejoin(self):
        self.set_config(REJOIN_ATTEMPTS=3)

        sent = self.kick()

        self.assertEqual(sent, [f"Kicked from #music by someop. {PROMISE}."])


if __name__ == "__main__":
    import unittest
    unittest.main()
