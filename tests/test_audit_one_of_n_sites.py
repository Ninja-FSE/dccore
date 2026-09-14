"""Three defects that are all the same shape: a correct fix applied at one of
N sites.

The completeness critic of the six-lens audit named this as the pattern
underneath a third of the findings, and it is worth stating plainly, because
it is not a knowledge gap. In each case somebody found a real problem, wrote
the right fix, wrote a comment explaining it - and applied it to the one place
the bug had been reported from.

  * `db.load_known_bots()` filters malformed entries out per ENTRY, with a
    comment explaining that every reader treats a row as a mapping. The two
    sibling loaders check only that the whole file is a dict, even though both
    docstrings say "same posture as load_known_bots()".

  * `is_server_numeric()` exists precisely because unanchored substring tests
    let a user forge server messages by typing them in a channel - its
    docstring cites the 513/PONG line as the bug it was written for. It is
    applied to the 513 handler and not to the PONG handler three lines above.

  * `dcc.is_offerable_to_strangers()` refuses to ADVERTISE an address a
    stranger cannot dial. Nothing tested the address arriving from the other
    direction at all - the only check was that the integer fits in 32 bits.

    This third one also shows the limit of the pattern, and the fix here is
    NOT simply "call the existing predicate at the missing site". That
    predicate asks whether an address is reachable from the public internet,
    which is the right question about OUR OWN address and the wrong one about
    a peer's: two bots on one LAN, or a machine talking to itself, are
    ordinary. Reusing it wholesale refused every local transfer and broke 32
    existing tests. The dial side gets the narrower question instead - can
    this name a peer at all - and loopback and private ranges stay allowed.

The consequences are not uniform: one kills a background dispatcher for the
life of the process, one lets a user forge an operator-facing reading, and one
lets whoever holds a nick choose an address this daemon will connect to.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class OneBadRowDoesNotTakeTheWholeRegistry(DCCoreTestCase):
    """`load_known_bots()` had this right, with the reasoning written down.
    Its two siblings never got the same line."""

    def write(self, path, payload):
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_the_fetch_history_drops_only_the_bad_row(self):
        self.write(db.FETCH_HISTORY_FILE,
                   {"good": {"state": "complete", "bot": "B"}, "bad": "oops"})

        loaded = db.load_fetch_history()

        self.assertEqual(list(loaded), ["good"])

    def test_the_fetched_list_registry_drops_only_the_bad_row(self):
        self.write(db.FETCHED_BOT_LISTS_FILE,
                   {"goodbot": {"entry_count": 5}, "badbot": "not-a-dict"})

        loaded = db.load_fetched_bot_lists()

        self.assertEqual(list(loaded), ["goodbot"])

    def test_the_dispatcher_survives_what_used_to_kill_it(self):
        """The consequence, not just the shape.

        oserve loads the history straight into config.fetch_queue, and
        check_fetch_queue() walks that dict with row.get("state") every two
        seconds. One string value therefore did not degrade a view - it raised
        AttributeError on every tick and killed the cross-bot fetch dispatcher
        for the life of the process.
        """
        self.write(db.FETCH_HISTORY_FILE,
                   {"good": {"state": "pending", "bot": "B", "filename": "f",
                             "requested_at": 1},
                    "bad": "oops"})
        self.set_config(fetch_queue={}, fetch_feature_disabled=False,
                        transfers_paused=False, CHANNEL="#chan")
        config.fetch_queue.update(db.load_fetch_history())

        dcc_fetch.check_fetch_queue()  # must not raise

    def test_a_whole_file_that_is_not_a_dict_is_still_refused(self):
        """Unchanged behaviour: the outer check stays."""
        self.write(db.FETCH_HISTORY_FILE, ["not", "a", "dict"])

        self.assertEqual(db.load_fetch_history(), {})

    def test_a_good_file_is_untouched(self):
        rows = {"a": {"state": "complete"}, "b": {"state": "failed"}}
        self.write(db.FETCH_HISTORY_FILE, rows)

        self.assertEqual(db.load_fetch_history(), rows)


class ALatencyReadingCannotBeForgedFromAChannel(DCCoreTestCase):
    """`is_server_numeric()` was written for exactly this class - its docstring
    names the PONG line - and was applied to the handler three lines below."""

    REAL = ":irc.example.net PONG irc.example.net :OSERVE_LATENCY_CHECK"
    FORGED = ":someuser!u@h PRIVMSG #chan :oops PONG OSERVE_LATENCY_CHECK"

    def test_a_genuine_server_pong_is_still_recognised(self):
        self.assertTrue(irc.is_server_numeric(self.REAL, "PONG"))

    def test_a_channel_message_cannot_forge_one(self):
        self.assertFalse(irc.is_server_numeric(self.FORGED, "PONG"))

    def test_the_old_unanchored_test_would_have_accepted_it(self):
        """Pins that the forgery is real rather than hypothetical - otherwise
        the test above proves only that a regex works."""
        self.assertTrue(" PONG " in self.FORGED
                        and "OSERVE_LATENCY_CHECK" in self.FORGED)

    def test_the_handler_uses_the_anchor(self):
        """Scoped to the read loop's own source: the defect is the test used
        at that one site, and it is not reachable from outside a live IRC
        session."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn('is_server_numeric(line, "PONG") and "OSERVE_LATENCY_CHECK"',
                      source)
        self.assertNotIn('if " PONG " in line and "OSERVE_LATENCY_CHECK" in line:',
                         source)


class AnOfferCannotNameAnAddressWeShouldNotDial(DCCoreTestCase):
    """The offer side has refused these since #162. The dial side accepted
    anything that fit in 32 bits."""

    def offer(self, ip_long, port=5000):
        return dcc_fetch.parse_dcc_send_offer(
            f"DCC SEND file.mp3 {ip_long} {port} 1024")

    def test_a_public_address_is_still_accepted(self):
        parsed = self.offer(0x08080808)  # 8.8.8.8

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["ip"], "8.8.8.8")

    def test_zero_is_refused(self):
        """0.0.0.0 is what connect() resolves to localhost on Linux, so an
        offer of "0 22" points the fetcher at the host's own SSH port."""
        self.assertIsNone(self.offer(0))

    def test_loopback_is_still_allowed(self):
        """Deliberately NOT refused, and this is the half worth pinning.

        dcc.is_offerable_to_strangers() refuses loopback for OUR OWN offer,
        correctly - a stranger cannot dial it. Applying the same rule to an
        address arriving FROM a peer would be wrong: there it is the peer's
        address, and one machine talking to itself is how every local transfer
        in this suite works. The first version of this fix reused that
        predicate wholesale and broke 32 existing tests."""
        parsed = self.offer(0x7F000001)  # 127.0.0.1

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["ip"], "127.0.0.1")

    def test_a_private_address_is_still_allowed(self):
        """Two DCCore bots on one LAN exchanging lists over 192.168.x.y is an
        ordinary setup, and the operator running both is a stranger to
        neither."""
        parsed = self.offer(0xC0A80101)  # 192.168.1.1

        self.assertIsNotNone(parsed)

    def test_a_multicast_address_is_refused(self):
        """No bot is offering a file from 224.0.0.1."""
        self.assertIsNone(self.offer(0xE0000001))

    def test_a_passive_offer_is_not_broken_by_this(self):
        """A passive/reverse offer carries port 0 and we do the listening, so
        there is no address to dial - it must keep parsing."""
        parsed = dcc_fetch.parse_dcc_send_offer(
            "DCC SEND file.mp3 3232235777 0 1024 999")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["port"], 0)


if __name__ == "__main__":
    unittest.main()
