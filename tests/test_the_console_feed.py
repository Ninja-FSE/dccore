"""#528, part one: the admin console carries the whole story of a transfer,
not only its end - and the operator picks which parts.

"dccore sends but I can't know until I look at stats in the browser." An
OmenServe operator sees every request, send and served search live in
mIRC. The admin DCC console and the dashboard's Console page already
received the debug stream; it just carried the wrong events - Sent: and
Failed: reached it, and a request, a slot being taken, a resume and a
served search were print() and nothing else.

Now one line per event - REQUEST, QUEUED, SENDING, RESUMED, SENT, FAILED,
SEARCH - with a Settings tickbox per kind (CONSOLE_SHOW_*), and the feed's
new events kept off the IRC debug channel unless DEBUG_CHANNEL_FEED is on:
a channel line costs a MSG_DELAY slot on the same pacer as the adverts and
resume replies, and the console costs nothing.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

FEED = ("REQUEST", "QUEUED", "SENDING", "RESUMED", "SENT", "FAIL", "SEARCH")
NEW = ("REQUEST", "QUEUED", "SENDING", "RESUMED", "SEARCH")


class _Config:
    """A bare config for the two pure policy functions."""

    def __init__(self, **values):
        for name, value in values.items():
            setattr(self, name, value)


class WhichSwitchGovernsWhat(unittest.TestCase):
    """announce.console_wants() / channel_wants() - the policy, on a bare
    config so every combination is cheap to state."""

    def test_every_feed_category_has_a_switch(self):
        for category in FEED:
            self.assertIn(category, announce.FEED_SWITCHES, category)

    def test_the_switches_are_real_settings(self):
        for switch in set(announce.FEED_SWITCHES.values()):
            self.assertTrue(hasattr(config, switch), switch)
            self.assertIs(getattr(config, switch), True, switch + " ships off")

    def test_a_ticked_box_lets_the_line_through(self):
        cfg = _Config(CONSOLE_SHOW_REQUESTS=True)
        self.assertTrue(announce.console_wants("REQUEST", cfg))

    def test_an_unticked_box_declines_it(self):
        cfg = _Config(CONSOLE_SHOW_REQUESTS=False)
        self.assertFalse(announce.console_wants("REQUEST", cfg))

    def test_sends_covers_starting_resuming_and_completing(self):
        cfg = _Config(CONSOLE_SHOW_SENDS=False)
        for category in ("SENDING", "RESUMED", "SENT"):
            self.assertFalse(announce.console_wants(category, cfg), category)

    def test_case_does_not_matter(self):
        cfg = _Config(CONSOLE_SHOW_SEARCHES=False)
        self.assertFalse(announce.console_wants("search", cfg))

    def test_a_category_outside_the_feed_is_never_muted(self):
        """PART, JOIN, QUIT and INFO carry config errors and rejoins as much
        as presence; no tickbox may silence those."""
        cfg = _Config(CONSOLE_SHOW_REQUESTS=False, CONSOLE_SHOW_QUEUE=False,
                      CONSOLE_SHOW_SENDS=False, CONSOLE_SHOW_FAILURES=False,
                      CONSOLE_SHOW_SEARCHES=False)
        for category in ("INFO", "PART", "JOIN", "QUIT", "BAN", "HARDBAN", "MUTE", "TBAN"):
            self.assertTrue(announce.console_wants(category, cfg), category)

    def test_a_missing_switch_means_on(self):
        """A settings.conf from before #528 has none of these; the feed
        must not go dark on upgrade."""
        self.assertTrue(announce.console_wants("REQUEST", _Config()))

    def test_the_new_events_stay_off_the_channel_by_default(self):
        cfg = _Config(DEBUG_CHANNEL_FEED=False)
        for category in NEW:
            self.assertFalse(announce.channel_wants(category, cfg), category)

    def test_the_channel_switch_lets_them_through(self):
        cfg = _Config(DEBUG_CHANNEL_FEED=True)
        for category in NEW:
            self.assertTrue(announce.channel_wants(category, cfg), category)

    def test_sent_and_failed_reach_the_channel_as_they_always_have(self):
        cfg = _Config(DEBUG_CHANNEL_FEED=False)
        for category in ("SENT", "FAIL", "INFO", "PART", "JOIN"):
            self.assertTrue(announce.channel_wants(category, cfg), category)

    def test_the_channel_switch_ships_off(self):
        self.assertIs(config.DEBUG_CHANNEL_FEED, False)


class _RoutingCase(DCCoreTestCase):
    """send_debug() end to end: a sink registered, the channel queue watched,
    stdout captured - the three places a line can land."""

    def setUp(self):
        super().setUp()
        self.set_config(DEBUG_CHANNEL="#dccore-debug", DEBUG_TO_CHANNEL=True,
                        DEBUG_TO_CONSOLE=True, DEBUG_CHANNEL_FEED=False,
                        CONSOLE_SHOW_REQUESTS=True, CONSOLE_SHOW_QUEUE=True,
                        CONSOLE_SHOW_SENDS=True, CONSOLE_SHOW_FAILURES=True,
                        CONSOLE_SHOW_SEARCHES=True)
        self.taken = []
        announce.add_debug_sink(self.sink)
        self.addCleanup(announce.remove_debug_sink, self.sink)
        announce._debug_queue.clear()
        self._real_drain = announce._ensure_debug_drain
        announce._ensure_debug_drain = lambda: None      # no real thread
        self.addCleanup(setattr, announce, "_ensure_debug_drain", self._real_drain)

    def sink(self, text, category):
        self.taken.append((category, text))

    def send(self, text, category):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            announce.send_debug(text, category=category)
        return out.getvalue()

    def on_channel(self):
        return [line for line in announce._debug_queue]


class ALineGoesWhereTheSwitchesSay(_RoutingCase):

    def test_a_request_reaches_the_console_and_not_the_channel(self):
        printed = self.send('dave asked for "Song.flac"', "REQUEST")

        self.assertEqual(self.taken, [("REQUEST", 'dave asked for "Song.flac"')])
        self.assertEqual(self.on_channel(), [], "a feed line went to the IRC channel by default")
        self.assertEqual(printed, "", "the stdout floor fired although the console took it")

    def test_with_the_channel_switch_on_it_reaches_both(self):
        self.set_config(DEBUG_CHANNEL_FEED=True)

        self.send('dave searched "metal" - 12 results', "SEARCH")

        self.assertEqual(len(self.taken), 1)
        self.assertEqual(len(self.on_channel()), 1)
        self.assertIn("[SEARCH]", self.on_channel()[0])

    def test_an_unticked_kind_is_declined_everywhere_and_silently(self):
        """Declined is not undelivered: the stdout floor is for a line
        nobody was there to take, not one the operator asked not to see."""
        self.set_config(CONSOLE_SHOW_REQUESTS=False)

        printed = self.send('dave asked for "Song.flac"', "REQUEST")

        self.assertEqual(self.taken, [])
        self.assertEqual(self.on_channel(), [])
        self.assertEqual(printed, "", "an unticked feed line fell through to stdout")

    def test_an_unticked_sent_still_reaches_the_channel(self):
        """The tickboxes govern the console. Sent: has always gone to the
        debug channel under DEBUG_TO_CHANNEL and still does."""
        self.set_config(CONSOLE_SHOW_SENDS=False)

        self.send('Sent: "Song.flac" to dave', "SENT")

        self.assertEqual(self.taken, [])
        self.assertEqual(len(self.on_channel()), 1)

    def test_the_floor_still_catches_a_line_nobody_took(self):
        """The pre-#528 guarantee, kept: both routing switches off and no
        console attached, the line goes to stdout rather than nowhere."""
        announce.remove_debug_sink(self.sink)
        self.set_config(DEBUG_TO_CHANNEL=False)

        printed = self.send('dave asked for "Song.flac"', "REQUEST")

        self.assertIn('dave asked for "Song.flac"', printed)

    def test_a_category_outside_the_feed_is_untouched_by_the_boxes(self):
        self.set_config(CONSOLE_SHOW_REQUESTS=False, CONSOLE_SHOW_SENDS=False)

        self.send("Rejoined #dccore-test.", "JOIN")

        self.assertEqual(self.taken, [("JOIN", "Rejoined #dccore-test.")])
        self.assertEqual(len(self.on_channel()), 1)

    def test_the_channel_line_carries_the_feed_tag(self):
        self.set_config(DEBUG_CHANNEL_FEED=True)
        for category in NEW:
            announce._debug_queue.clear()
            self.send("x", category)
            self.assertIn(f"[{category}]", self.on_channel()[0], category)


class TheEventsAreEmittedWhereTheyHappen(DCCoreTestCase):
    """The feed lines themselves, from the functions every path goes
    through, captured at send_debug."""

    def setUp(self):
        super().setUp()
        self.lines = []
        self._real = announce.send_debug
        announce.send_debug = lambda text, category="INFO", notice=None: self.lines.append((category, text))
        self.addCleanup(setattr, announce, "send_debug", self._real)
        self.set_config(MAX_DCC_SLOTS=3)
        config.active_transfers[:] = [{"user": "dave", "file": "Song.flac", "bytes_sent": 0}]

    def test_taking_a_slot_says_so_with_the_slot_count(self):
        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_dcc_sending_notice("dave", "Song.flac")

        self.assertEqual(self.lines, [("SENDING", 'Sending "Song.flac" to dave (slot 1/3)')])

    def test_queueing_says_so_with_the_position(self):
        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_dcc_queue_notice("erin", "Other.flac", 2)

        self.assertEqual(self.lines, [("QUEUED", 'Queued "Other.flac" for erin at #2 (1/3 slots busy)')])

    def test_the_request_line_is_in_the_request_path(self):
        """Read from the source: driving handle_download_request() needs a
        library on disk, and the property is which branch the line is on -
        after the file is found and before the send-or-queue decision, so
        every accepted request reports once whichever way it goes."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def handle_download_request("):]
        found = body.index("file_name = os.path.basename(full_path)")
        request = body.index('feed_event("REQUEST"', found)
        decision = body.index("sends_now = (not user_already_transferring", found)

        self.assertLess(found, request)
        self.assertLess(request, decision)

    def test_a_folder_request_is_a_request_too(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('asked for the folder \\"{clean_folder_name}\\"', source)
        self.assertIn('feed_event("REQUEST", f"{user} asked for the folder', source)
        self.assertIn('kind="folder", name=clean_folder_name', source)

    def test_a_resume_says_where_from(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def handle_resume_request("):]
        body = body[:body.index("def resume_transfers(")]

        self.assertIn('"RESUMED",', body)
        self.assertIn("at_bytes=position, total_bytes=size", body)
        accepted = body.index("accepted and will send from there")
        self.assertLess(accepted, body.index('"RESUMED"'),
                        "the RESUMED line must follow the accept, not precede a refusal")

    def test_a_search_reports_the_total_not_the_capped_count(self):
        with io.open(os.path.join(REPO_ROOT, "list.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("def execute_search("):]
        line = body.index('"SEARCH",')
        block = body[line:body.index("term=search_term", line)]

        self.assertIn("total_matches", block)
        self.assertNotIn("len(matches)", block)
        self.assertLess(line, body.index("if matches:"),
                        "a search with no hits must report too")


class TheSettingsPageOffersTheBoxes(DCCoreTestCase):
    def test_the_console_feed_category_carries_the_six(self):
        import webserver
        payload = webserver.build_settings_payload()
        feed = next(c for c in payload["categories"] if c["id"] == "console-feed")
        names = [f["name"] for f in feed["fields"]]

        self.assertEqual(names, ["CONSOLE_SHOW_REQUESTS", "CONSOLE_SHOW_QUEUE",
                                 "CONSOLE_SHOW_SENDS", "CONSOLE_SHOW_FAILURES",
                                 "CONSOLE_SHOW_SEARCHES", "DEBUG_CHANNEL_FEED"])
        for field in feed["fields"]:
            self.assertEqual(field["type"], "bool", field["name"] + " is not a tickbox")


if __name__ == "__main__":
    unittest.main()
