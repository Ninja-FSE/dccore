"""#302: "pasting 10 files spams all 10 requests at once."

It does not, and nothing covered that - which is why nobody would have
noticed if it broke. Driven through the real dcc_fetch.check_fetch_queue():
ten pending rows, MAX_FETCH_SLOTS at its default of 3, and exactly three
request lines go out. The other seven wait, and a further tick with nothing
completing sends nothing more.

The three that do go out are handed to oserve.queue_message() rather than
written to the socket, so they are also paced at MSG_DELAY on the wire - not
simultaneous even among themselves.

Two things would produce the reported symptom without any of this being
wrong, and both are worth ruling out before reading a failure here as the
daemon spamming: MAX_FETCH_SLOTS raised above its default, or the Downloads
panel showing all ten ROWS the instant they are created - seven at "Pending"
and three at "Requested" - which is ten lines appearing at once without ten
requests having been sent.
"""

import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "PeerBot"


class TenPastedFilesAreThreeRequests(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.sent = []
        stub = types.ModuleType("oserve")
        stub.irc_connection = object()
        stub.queue_message = lambda user, message, is_vip=False: self.sent.append(message)
        sys.modules["oserve"] = stub
        self.addCleanup(sys.modules.pop, "oserve", None)

        self.set_config(MAX_FETCH_SLOTS=3, CHANNEL="#somechannel",
                        transfers_paused=False, fetch_feature_disabled=False)
        config.fetch_queue.clear()

    def paste(self, count):
        return [dcc_fetch.enqueue_fetch(BOT, "Track %02d.flac" % n)
                for n in range(count)]

    def states(self):
        counted = {}
        for row in config.fetch_queue.values():
            counted[row["state"]] = counted.get(row["state"], 0) + 1
        return counted

    # -- the claim ---------------------------------------------------------

    def test_ten_rows_dispatch_three_requests(self):
        self.paste(10)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.sent), 3, self.sent)
        self.assertEqual(self.states(), {"offered": 3, "pending": 7})

    def test_a_second_tick_sends_nothing_while_the_slots_are_held(self):
        """The half that would actually be spam: dispatching again on the next
        two-second tick because nothing marked the slots as taken."""
        self.paste(10)
        dcc_fetch.check_fetch_queue()

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.sent), 3, self.sent)

    def test_all_ten_rows_are_created_which_is_what_the_panel_shows(self):
        """Not a defect, and the likeliest explanation of the report: the
        Downloads panel fills with ten rows immediately. Seven of them say
        "Pending" and have had nothing sent for them."""
        ids = self.paste(10)

        self.assertEqual(len(ids), 10)
        self.assertEqual(self.states(), {"pending": 10})

    def test_the_requests_go_through_the_pacer_not_the_socket(self):
        """So the three are not simultaneous either. A line written straight
        to the socket has taken no pacer slot."""
        self.paste(10)

        dcc_fetch.check_fetch_queue()

        for line in self.sent:
            with self.subTest(line=line.strip()):
                self.assertTrue(line.startswith("PRIVMSG #somechannel :!%s " % BOT),
                                line)

    def test_the_limit_is_the_setting_and_not_a_hardcoded_three(self):
        """The discriminator. Every assertion above passes against a daemon
        that dispatches a fixed three regardless of what the operator set -
        which would be a different bug wearing the same numbers, and is
        exactly what "raise MAX_FETCH_SLOTS and it still sends three" would
        look like."""
        self.set_config(MAX_FETCH_SLOTS=5)
        self.paste(10)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(len(self.sent), 5, self.sent)

    def test_nothing_is_dispatched_while_transfers_are_paused(self):
        """A rehash quiesces the daemon, and a fetch has its own queue that
        never appears in active_transfers - so without this check the
        dispatcher would keep putting requests into the channel during the
        reload window, each bringing back an inbound send."""
        self.paste(10)
        self.set_config(transfers_paused=True)

        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.sent, [])


class TheWindowsGuideQuotesWhatTheDaemonPrints(unittest.TestCase):
    """#302 asked the Windows guide to say how to verify the dashboard came
    up. The message it asked for - "[WEB ENABLED]" - is not a string this
    daemon has ever printed, so the guide quotes the real ones. These are read
    from the source, because a documented log line that has quietly changed is
    worse than none: it sends the operator looking for something that is not
    there."""

    def quoted(self):
        import io

        with io.open(os.path.join(REPO_ROOT, "docs", "WINDOWS.md"),
                     encoding="utf-8") as handle:
            return handle.read()

    def source(self, name):
        import io

        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_running_line_is_the_one_webserver_prints(self):
        self.assertIn("[WEBUI] Dashboard starting on http://", self.quoted())
        self.assertIn('print(f"[WEBUI] Dashboard starting on http://{host}:{port}/ '
                      '(login required).")', self.source("webserver.py"))

    def test_the_switched_off_line_is_the_one_oserve_prints(self):
        self.assertIn("[WEBUI] Disabled via config.WEBUI_ENABLED = False.",
                      self.quoted())
        self.assertIn('print("[WEBUI] Disabled via config.WEBUI_ENABLED = False.")',
                      self.source("oserve.py"))

    def test_the_missing_flask_line_is_the_one_the_operator_sees(self):
        """There are TWO, and quoting the wrong one sends somebody looking for
        a line that never appears. webserver.py imports flask under a guard,
        so the MODULE imports fine and start() reports it - the other message
        means webserver.py itself is damaged."""
        self.assertIn("[WEBUI] Flask not installed; dashboard disabled.",
                      self.quoted())
        self.assertIn('print("[WEBUI] Flask not installed; dashboard disabled.")',
                      self.source("webserver.py"))

    def test_the_damaged_module_line_is_the_one_oserve_prints(self):
        self.assertIn("[WEBUI] Could not import webserver:", self.quoted())
        self.assertIn('print(f"[WEBUI] Could not import webserver: {web_err}")',
                      self.source("oserve.py"))

    def test_the_port_shown_is_the_shipped_default(self):
        """A worked example with the wrong port sends somebody to a page that
        is not there and reads as the dashboard being broken."""
        shipped = None
        for line in self.source("defaults.py").splitlines():
            if line.startswith("WEBUI_PORT"):
                shipped = line.split("=", 1)[1].strip().split()[0]
                break

        self.assertIsNotNone(shipped, "defaults.py no longer declares WEBUI_PORT")
        self.assertIn("http://127.0.0.1:%s/" % shipped, self.quoted())

    def test_it_does_not_quote_the_message_that_never_existed(self):
        """#302 asked for "[WEB ENABLED]". Nothing prints it, and writing it
        into the guide would be the same class of fault as #465 - documenting
        a route that does not work."""
        self.assertNotIn("[WEB ENABLED]", self.quoted())
