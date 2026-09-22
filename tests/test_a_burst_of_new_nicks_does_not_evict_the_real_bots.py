"""The known-bots size cap evicted the oldest-seen entries first, so a
burst of fresh nicks pushed out the genuine bots - the opposite of what
its docstring claimed (audit L7, #671).

Any channel member can register a "bot" with one unauthenticated line
("Type: @<theirnick> For My List Of: 1 Files"; the RAR wording needs no
identity claim at all). _prune_known_bots() evicted by last_seen ascending
once the registry passed KNOWN_BOTS_MAX, and 2001 fresh nicks carried the
newest last_seen of all - so the genuine bots that had advertised minutes
earlier were the ones dropped, gone from the List Browser until their next
advert, while the junk sat there for up to a week.

Every capture now counts the adverts an entry is built from, and eviction
goes by that count first, then by last_seen: a nick that said it once is
what goes; a bot that advertises every few minutes has said it more than
once by the time a flood of that size can arrive.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402
import runtime  # noqa: E402

from tests import test_audit_low_batch as bounded  # noqa: E402

T0 = bounded.T0


class TheAuditsBurst(bounded.TheBotRegistryIsBounded):
    """The auditor's own recipe, end to end through the capture path."""

    def real_bots(self, count=5):
        # Advertised twice, minutes ago - the way a real bot looks.
        for n in range(count):
            self.advertise(f"RealBot{n}", now=T0 - 600)
            self.advertise(f"RealBot{n}", now=T0 - 300)

    def test_a_burst_of_one_off_nicks_evicts_the_one_off_nicks(self):
        self.real_bots()
        for n in range(irc.KNOWN_BOTS_MAX + 1):
            self.advertise(f"spam{n}", now=T0)

        self.assertEqual(len(runtime.known_bots), irc.KNOWN_BOTS_MAX)
        for n in range(5):
            self.assertIn(f"realbot{n}", runtime.known_bots, "a genuine bot was evicted by the burst")

    def test_the_rar_wording_needs_no_identity_claim_and_still_does_not_win(self):
        self.real_bots()
        for n in range(irc.KNOWN_BOTS_MAX + 1):
            irc._capture_channel_advert(
                f"spam{n}", "#dccore-test",
                f"Type @spam{n} to get my list of 1 (1 MB) RAR folders", now=T0)

        for n in range(5):
            self.assertIn(f"realbot{n}", runtime.known_bots)

    def test_among_one_offs_the_least_recently_seen_still_goes_first(self):
        """What the old order got right is kept: two nicks seen once each,
        the older one goes."""
        self.advertise("once-old", now=T0 - 3600)
        self.advertise("once-new", now=T0)
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", irc.KNOWN_BOTS_MAX)
        irc.KNOWN_BOTS_MAX = 1

        irc._prune_known_bots(T0)

        self.assertEqual(sorted(runtime.known_bots), ["once-new"])

    def test_an_entry_from_an_older_file_counts_as_seen_once(self):
        """No `adverts` field: it predates the count, and is not more
        established than a nick seen once now - but not less either."""
        runtime.known_bots["oldfile"] = {"nick": "oldfile", "files": 5, "last_seen": T0 - 60}
        self.advertise("fresh", now=T0)
        self.advertise("regular", now=T0 - 120)
        self.advertise("regular", now=T0)
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", irc.KNOWN_BOTS_MAX)
        irc.KNOWN_BOTS_MAX = 2

        irc._prune_known_bots(T0)

        self.assertEqual(sorted(runtime.known_bots), ["fresh", "regular"])

    def test_every_capture_is_counted(self):
        self.advertise("counted", now=T0 - 200)
        self.advertise("counted", now=T0 - 100)
        self.advertise("counted", now=T0)

        self.assertEqual(runtime.known_bots["counted"]["adverts"], 3)
        self.assertEqual(runtime.known_bots["counted"]["last_seen"], T0)

    def test_a_hand_entered_bot_is_still_never_a_candidate(self):
        runtime.known_bots["byhand"] = {"nick": "byhand", "hand_entered": True, "last_seen": 0}
        for n in range(3):
            self.advertise(f"spam{n}", now=T0)
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", irc.KNOWN_BOTS_MAX)
        irc.KNOWN_BOTS_MAX = 2

        irc._prune_known_bots(T0)

        self.assertIn("byhand", runtime.known_bots)


for _name in [n for n in dir(bounded.TheBotRegistryIsBounded) if n.startswith("test")]:
    setattr(TheAuditsBurst, _name, None)


if __name__ == "__main__":
    unittest.main()
