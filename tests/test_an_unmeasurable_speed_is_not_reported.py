"""A rate nobody should act on is not stated as a fact.

FROM THE BETA, on a list zip:

    Sent: "SomeBotDCCore-2026-09-07.zip" to IndexBot [138.63MB/s]
    "this seems to high to be true"

It was. sendall() returns once the bytes are in the KERNEL, not once the peer
has them. For a file LARGER than the socket send buffer the two converge - the
kernel blocks once the buffer is full, so the send paces itself against the
network and the clock measures something real. For a file that fits INSIDE the
buffer they do not converge at all: the whole thing is handed over in one go
and the clock measures a memory copy.

CAUSED BY A CHANGE IN THIS RELEASE, and predicted by it. Raising the default
send buffer to 4 MB - which took a real transfer from 3.0 to 24.7 MB/s - also
widened the window of files this applies to from under 1 MB to under 4 MB.
That is most list archives and many single tracks. The change said so at the
time and pinned the guard on the speed RECORD; it did not pin the guard on
what gets SAID, and there was not one.

MIN_RECORD_SECONDS is the floor the record has always applied, for exactly
this reason. A number too unreliable to keep is too unreliable to say, so the
same judgement now decides both - and the line goes into the CHANNEL, where a
figure nobody should act on is worse published than absent.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import stats_mgr  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


class DecidingWhetherItCanBeMeasured(unittest.TestCase):

    def test_a_transfer_that_took_real_time_can_be(self):
        self.assertTrue(stats_mgr.speed_is_measurable(12.0))

    def test_one_that_vanished_into_the_buffer_cannot(self):
        """The reported case: several megabytes copied into a 4 MB kernel
        buffer in a few milliseconds."""
        self.assertFalse(stats_mgr.speed_is_measurable(0.02))

    def test_the_floor_is_the_one_the_record_already_uses(self):
        """Not a second threshold with its own opinion. A number too
        unreliable to keep is too unreliable to say."""
        self.assertTrue(stats_mgr.speed_is_measurable(stats_mgr.MIN_RECORD_SECONDS))
        self.assertFalse(
            stats_mgr.speed_is_measurable(stats_mgr.MIN_RECORD_SECONDS - 0.001))

    def test_nothing_at_all_is_not_measurable(self):
        self.assertFalse(stats_mgr.speed_is_measurable(None))
        self.assertFalse(stats_mgr.speed_is_measurable("ages"))


class TheChannelLineSaysSoRatherThanGuessing(DCCoreTestCase):

    def sent_text(self, speed):
        sock = RecordingSocket()
        self.set_config(NICKNAME="SomeBot")
        real = announce.oserve_queue_message if hasattr(
            announce, "oserve_queue_message") else None
        announce.send_transfer_complete(
            "#somechannel", "someuser", "Album.zip", 5_000_000,
            __import__("time").time() - 30, speed)
        # The notice is queued through oserve; the stub the harness installs
        # records it.
        return "".join(message for _user, message, _vip in self.oserve.queued)

    def test_a_real_rate_is_still_shown(self):
        text = self.sent_text(2_500_000)

        self.assertNotIn("n/a", text)
        self.assertIn("Speed:", text)

    def test_an_unmeasurable_one_says_so(self):
        """Rather than a figure, and rather than "0k/s" - which would be a
        different false claim, not an absence of one."""
        text = self.sent_text(None)

        self.assertIn("n/a", text)
        self.assertNotIn("0k/s", text)

    def test_it_does_not_raise_on_none(self):
        """`None > 0` is a TypeError in Python 3, and this runs on the
        transfer-completion path where an exception costs the notice."""
        self.sent_text(None)

    def test_zero_is_still_zero(self):
        """A genuinely measured zero is a different thing from an unmeasured
        one, and only one of them is unknown."""
        text = self.sent_text(0)

        self.assertIn("0k/s", text)


class TheSendPathDecidesIt(unittest.TestCase):
    """start_dcc_send() needs a live socket, so the wiring is read from the
    source - the arithmetic itself is covered above."""

    def source(self):
        with open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_reported_figure_is_gated(self):
        self.assertIn("speed_is_measurable(acute_duration)", self.source())

    def test_the_channel_gets_the_gated_one(self):
        """Not final_calc_speed, which is the raw division and is what
        produced 138 MB/s for a list zip."""
        # The CALL, not every mention. dcc.py explains this notice in prose
        # too, and a line-contains match reads the explanation as the code.
        source = self.source()
        call = [line.strip() for line in source.splitlines()
                if "announce_mod.send_transfer_complete(" in line]

        self.assertEqual(len(call), 1, f"expected one call site, got {call}")
        self.assertIn("reported_speed", call[0])
        self.assertNotIn("final_calc_speed", call[0])

    def test_the_record_still_gets_the_raw_one(self):
        """update_speed_record() applies the same floor itself, and is handed
        the duration to do it with - so gating twice would be one guard
        depending on another rather than on the facts."""
        source = self.source()

        self.assertIn("update_speed_record(final_calc_speed, acute_duration)",
                      source)


if __name__ == "__main__":
    unittest.main()
