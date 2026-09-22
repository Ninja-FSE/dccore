"""477 and the other refusals outside the five were silent and retried for
ever (audit M30, #632).

JOIN_REFUSED_NUMERICS held 405/471/473/474/475. Undernet answers a JOIN to a
+r channel from a nick that is not logged in to X with 477
(ERR_NEEDREGGEDNICK); 476 and 479 say the name is not a channel name at all.
parse_join_refusal() returned None for all three, so nothing was printed
outside DEBUG_MODE, note_join_refused() never counted, and a channel the
watchdog had marked "never confirmed" stayed at zero refusals - which is
what channels_to_rejoin() reads as "worth another JOIN". The advert worker
sent that JOIN every ANNOUNCE_INTERVAL for the life of the process and got
the same 477 back every time, and the operator's only clue was the one-off
"never confirmed via NAMES" line, with no reason in it.

437 stays out on purpose. "Temporarily unavailable" is what a channel is
during a netsplit, and a retry that never gives up is the right answer to a
refusal that says it will pass.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

OURS = "#somechannel"


def server_says(numeric, channel=OURS, text="Cannot join channel"):
    return ":irc.example.net %s SomeBot %s :%s" % (numeric, channel, text)


class TheThreeAreRefusals(unittest.TestCase):

    def test_needs_a_registered_nick(self):
        """Undernet +r. The one that happens on a live install."""
        parsed = irc.parse_join_refusal(
            server_says("477", text="Cannot join channel (+r): this channel requires authentication"))

        self.assertEqual(parsed, (OURS, "477"))

    def test_a_bad_channel_mask_and_an_illegal_name(self):
        for numeric in ("476", "479"):
            with self.subTest(numeric=numeric):
                self.assertEqual(irc.parse_join_refusal(server_says(numeric)), (OURS, numeric))

    def test_temporarily_unavailable_is_not_one(self):
        """437 passes; a bounded retry would give up on a netsplit."""
        self.assertIsNone(irc.parse_join_refusal(
            server_says("437", text="Nick/channel is temporarily unavailable")))

    def test_the_login_numeric_is_the_one_with_its_own_words(self):
        self.assertEqual(irc.JOIN_REFUSED_NEEDS_A_LOGIN, "477")
        self.assertIn(irc.JOIN_REFUSED_NEEDS_A_LOGIN, irc.JOIN_REFUSED_NUMERICS)


class ACountedRefusalEndsInGivingUp(DCCoreTestCase):
    """The failure the audit reproduced: a never-confirmed channel answered
    477 five times stayed at zero refusals and was rejoined for ever."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=OURS, REJOIN_ATTEMPTS=3)
        irc.note_join_unconfirmed(OURS)

    def test_477_counts_like_a_ban_does(self):
        for attempt in range(3):
            channel, _numeric = irc.parse_join_refusal(server_says("477"))
            self.assertEqual(irc.note_join_refused(channel), attempt + 1)

        self.assertEqual(irc.channels_to_rejoin(), [],
                         "still being asked for after the attempts were used up")
        self.assertEqual(irc.gave_up_on(), [OURS])

    def test_and_so_do_the_two_bad_name_numerics(self):
        for numeric in ("476", "479"):
            with self.subTest(numeric=numeric):
                channel, _n = irc.parse_join_refusal(server_says(numeric))
                self.assertGreaterEqual(irc.note_join_refused(channel), 1)


class TheReadLoopSaysWhatToDo(unittest.TestCase):
    """The handler lives inside irc_loop(); as the rest of the rejoin tests
    do, this checks the text. 477's answer is not "wait it out" - the nick
    has to be logged in to services before the JOIN - so the operator is told
    that, and it is still counted so a login that never comes ends in "gave
    up" like any other refusal."""

    def handler(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            code = handle.read()
        block = code.split("count = note_join_refused(refused_chan)", 1)[1][:3000]
        return re.sub(chr(35) + "[^" + chr(10) + "]*", "", block)

    def test_477_has_its_own_branch(self):
        self.assertIn("elif numeric == JOIN_REFUSED_NEEDS_A_LOGIN:", self.handler())

    def test_which_names_the_login_and_where_to_put_it(self):
        branch = self.handler().split("JOIN_REFUSED_NEEDS_A_LOGIN:", 1)[1]
        branch = branch.split("elif count >= limit:", 1)[0]

        self.assertIn("log in to services", branch)
        self.assertIn("On connect", branch)
        self.assertIn("Attempt {count}/{limit}", branch,
                      "the login branch must still show the count - it is bounded")

    def test_it_sits_inside_the_counted_path(self):
        """Before the uncounted `else:` - a 477 at connect time for a channel
        nobody is retrying yet is still just said out loud, like any other."""
        text = self.handler()
        self.assertLess(text.index("JOIN_REFUSED_NEEDS_A_LOGIN:"),
                        text.index("refused us ({numeric})."))


if __name__ == "__main__":
    unittest.main()
