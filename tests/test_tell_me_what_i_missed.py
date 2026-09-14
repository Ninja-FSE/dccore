"""The notices badge: the short list of things the operator should be told.

DCCore already logs everything - the Console, the debug channel, the admin
chat all carry the same fan-out from announce.send_debug(). That is a log, and
a log is the wrong shape for "did anything go wrong while I was asleep":
everything is in it, so nothing stands out.

This is the other shape. A handful of call sites that KNOW they are one of
those events raise a notice alongside their log line, and the dashboard shows
a count of the ones nobody has acknowledged yet. Two severities, a read
marker, and a file on disk so a kick at three in the morning is still there at
nine.

Four seams, tested separately because they fail separately:

  * announce.record_notice/unread_notices/mark_notices_read - the store and
    the arithmetic of "how much of this is new".
  * db.load_notices/save_notices - what survives a restart, and what a
    hand-edited or truncated file costs.
  * announce.send_debug(notice=...) - the seam that makes raising one a
    property of the log line rather than a second call to forget.
  * webserver.build_notices_payload/mark_notices_read_result - what the page
    is handed, and who computes the badge.

The page's own structure - the badge element, the view registration, whether
`hidden` actually hides - is checked in tests/test_web_assets.py, where every
other structural check on the dashboard already lives.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheStore(DCCoreTestCase):
    """record_notice() and the two questions asked of what it keeps."""

    def test_a_notice_comes_back_with_what_it_was_given(self):
        entry = announce.record_notice("Kicked from #example.", "warning")

        self.assertEqual(entry["text"], "Kicked from #example.")
        self.assertEqual(entry["severity"], "warning")
        self.assertEqual(config.notices, [entry])

    def test_ids_count_up_and_are_never_reused(self):
        """The dashboard remembers the highest id it has shown and asks for
        anything above it. A count would break the moment two events arrived
        between two polls."""
        first = announce.record_notice("one", "warning")
        second = announce.record_notice("two", "warning")
        third = announce.record_notice("three", "error")

        self.assertEqual([first["id"], second["id"], third["id"]], [1, 2, 3])

    def test_an_id_is_not_reused_after_the_oldest_is_dropped(self):
        """Ids come from the last entry, not from the length - which is the
        same number until the cap starts discarding, and then differs forever.
        Reusing one would make the read marker acknowledge a notice the
        operator has never seen."""
        for index in range(5):
            announce.record_notice("event %d" % index, "warning")
        config.notices.pop(0)

        after = announce.record_notice("next", "warning")

        self.assertEqual(after["id"], 6)

    def test_an_unknown_severity_becomes_a_warning(self):
        """Rather than raising. A notice is already the unhappy path, and a
        caller passing a typo should lose the colour, not the event."""
        entry = announce.record_notice("something", "catastrophe")

        self.assertEqual(entry["severity"], "warning")

    def test_the_severities_are_the_two_that_are_documented(self):
        self.assertEqual(announce.NOTICE_SEVERITIES, ("warning", "error"))

    def test_the_oldest_are_dropped_once_the_cap_is_reached(self):
        for index in range(announce.NOTICES_MAX + 10):
            announce.record_notice("event %d" % index, "warning")

        self.assertEqual(len(config.notices), announce.NOTICES_MAX)
        self.assertEqual(config.notices[-1]["text"],
                         "event %d" % (announce.NOTICES_MAX + 9))

    def test_the_newest_are_the_ones_kept(self):
        """The cap discards from the front. Trimming the other end would keep
        two hundred rows from last month and throw away this morning's."""
        for index in range(announce.NOTICES_MAX + 3):
            announce.record_notice("event %d" % index, "warning")

        texts = [row["text"] for row in config.notices]
        self.assertNotIn("event 0", texts)
        self.assertIn("event %d" % (announce.NOTICES_MAX + 2), texts)

    def test_it_writes_into_the_shared_list_not_a_copy(self):
        """config.notices IS runtime.notices. If record_notice() ever rebound
        the name instead of appending, the daemon and the dashboard would be
        looking at two different lists and the badge would never move."""
        announce.record_notice("something", "warning")

        self.assertIs(config.notices, runtime.notices)


class HowMuchOfThisIsNew(DCCoreTestCase):

    def test_everything_is_unread_before_anybody_looks(self):
        announce.record_notice("one", "warning")
        announce.record_notice("two", "warning")

        self.assertEqual(announce.unread_notices(), (2, "warning"))

    def test_nothing_is_unread_when_there_is_nothing(self):
        count, worst = announce.unread_notices()

        self.assertEqual(count, 0)
        self.assertEqual(worst, "")

    def test_an_error_anywhere_in_the_unread_sets_the_colour(self):
        """The badge is one colour and has to pick. The worse one wins: an
        operator who sees amber and finds red underneath learns not to trust
        the colour."""
        announce.record_notice("kicked, came back", "warning")
        announce.record_notice("gave up rejoining", "error")
        announce.record_notice("kicked, came back", "warning")

        self.assertEqual(announce.unread_notices(), (3, "error"))

    def test_an_error_that_has_been_read_does_not_colour_the_badge(self):
        """The count is over the UNREAD ones, and so is the colour. A badge
        that stays red because of something acknowledged last week is a badge
        nobody looks at."""
        announce.record_notice("gave up rejoining", "error")
        announce.mark_notices_read()
        announce.record_notice("kicked, came back", "warning")

        self.assertEqual(announce.unread_notices(), (1, "warning"))

    def test_marking_read_clears_the_count(self):
        announce.record_notice("one", "warning")
        announce.record_notice("two", "error")

        announce.mark_notices_read()

        self.assertEqual(announce.unread_notices(), (0, ""))

    def test_marking_read_keeps_the_notices_themselves(self):
        """Acknowledged, not deleted. The panel is where you go to read what
        happened, which is exactly the moment they would be gone."""
        announce.record_notice("one", "warning")

        announce.mark_notices_read()

        self.assertEqual(len(config.notices), 1)

    def test_a_notice_arriving_after_the_mark_is_unread(self):
        announce.record_notice("old", "warning")
        announce.mark_notices_read()

        announce.record_notice("new", "error")

        self.assertEqual(announce.unread_notices(), (1, "error"))

    def test_marking_read_with_nothing_recorded_is_harmless(self):
        self.assertEqual(announce.mark_notices_read(), 0)
        self.assertEqual(announce.unread_notices(), (0, ""))

    def test_the_marker_is_the_highest_id_not_the_count(self):
        """Those are the SAME NUMBER until the cap starts discarding, so this
        has to be asked where they differ - otherwise `len(notices)` passes
        it, which is a mutant this suite let through once.

        Once the two diverge, the count is always lower than the highest id,
        so marking read would acknowledge only part of what is on screen and
        the badge would never clear no matter how many times it was clicked.
        """
        for index in range(5):
            announce.record_notice("event %d" % index, "warning")
        # What the cap does once it is full: the oldest go, the ids do not
        # come back. Three rows left, highest id still 5.
        del config.notices[:2]

        announce.mark_notices_read()

        self.assertEqual(len(config.notices), 3)
        self.assertEqual(config.notice_state["seen_id"], 5)

    def test_and_the_badge_actually_clears_when_it_does(self):
        """The consequence, stated separately: the number above is only
        interesting because this is what an operator sees."""
        for index in range(5):
            announce.record_notice("event %d" % index, "warning")
        del config.notices[:2]

        announce.mark_notices_read()

        self.assertEqual(announce.unread_notices(), (0, ""))

    def test_the_marker_lives_inside_the_dict_the_daemon_shares(self):
        """A plain int on config could not be shared by reference, so the
        dashboard marking notices read would update a number the daemon never
        sees - and the badge would come back on the next poll."""
        announce.mark_notices_read()

        self.assertIs(config.notice_state, runtime.notice_state)


class RaisingOneIsPartOfLoggingIt(DCCoreTestCase):
    """send_debug(notice=...) rather than a second call beside it."""

    def test_a_line_with_a_severity_is_recorded(self):
        announce.send_debug("List update FAILED.", category="INFO",
                            notice="error")

        self.assertEqual(len(config.notices), 1)
        self.assertEqual(config.notices[0]["severity"], "error")
        self.assertEqual(config.notices[0]["text"], "List update FAILED.")

    def test_an_ordinary_line_is_not(self):
        """Which is very nearly every line. A badge counting log lines is a
        badge counting nothing."""
        announce.send_debug("Advert sent to #example.", category="INFO")

        self.assertEqual(config.notices, [])

    def test_the_notice_says_what_the_log_line_says(self):
        """One string, used twice. Two calls could word them differently, and
        the panel disagreeing with the Console about the same event is worse
        than either alone."""
        announce.send_debug("Kicked from #example.", category="PART",
                            notice="warning")

        self.assertEqual(config.notices[0]["text"], "Kicked from #example.")

    def test_the_sink_contract_is_unchanged(self):
        """Sinks still receive (text, category) and know nothing about any of
        this. The Console and the admin chat are downstream of send_debug()
        and must not need a change to keep working."""
        seen = []

        def sink(text, category):
            seen.append((text, category))

        announce.add_debug_sink(sink)
        self.addCleanup(announce.remove_debug_sink, sink)

        announce.send_debug("Kicked from #example.", category="PART",
                            notice="warning")

        self.assertEqual(seen, [("Kicked from #example.", "PART")])


class WhatSurvivesARestart(DCCoreTestCase):

    def read_file(self):
        with io.open(db.NOTICES_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_notice_is_on_disk_as_soon_as_it_is_raised(self):
        """Not on a timer and not at shutdown. The events worth a badge
        include the ones immediately before the process dies."""
        announce.record_notice("gave up rejoining #example", "error")

        self.assertEqual(len(self.read_file()["notices"]), 1)

    def test_a_round_trip_returns_what_went_in(self):
        announce.record_notice("one", "warning")
        announce.record_notice("two", "error")
        announce.mark_notices_read()

        rows, state = db.load_notices()

        self.assertEqual([row["text"] for row in rows], ["one", "two"])
        self.assertEqual(state["seen_id"], 2)

    def test_the_read_marker_is_written_with_the_notices(self):
        """One file, because they are one fact. Two files could crash between
        the writes and leave a marker higher than any surviving notice, which
        silently swallows everything in between."""
        announce.record_notice("one", "warning")
        announce.mark_notices_read()

        stored = self.read_file()

        self.assertEqual(stored["state"]["seen_id"],
                         stored["notices"][-1]["id"])

    def test_a_missing_file_is_an_empty_panel(self):
        os.remove(db.NOTICES_FILE) if os.path.exists(db.NOTICES_FILE) else None

        self.assertEqual(db.load_notices(), ([], {"seen_id": 0}))

    def test_a_file_that_will_not_parse_costs_the_panel_and_nothing_else(self):
        """Same posture as every other store here: a corrupt file must never
        stop the daemon serving files."""
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")

        self.assertEqual(db.load_notices(), ([], {"seen_id": 0}))

    def test_a_file_holding_the_wrong_shape_is_ignored(self):
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            json.dump(["not", "a", "mapping"], handle)

        self.assertEqual(db.load_notices(), ([], {"seen_id": 0}))

    def test_one_unusable_row_does_not_take_the_others_with_it(self):
        """Filtered individually. A hand-edited file that lost one entry
        should cost that entry, not the other hundred and ninety-nine."""
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"notices": [
                {"id": 1, "at": 0, "severity": "warning", "text": "kept"},
                "a bare string where a row should be",
                {"no": "id"},
                {"id": 4, "at": 0, "severity": "error", "text": "also kept"},
            ], "state": {"seen_id": 1}}, handle)

        rows, state = db.load_notices()

        self.assertTrue(all(isinstance(row, dict) for row in rows),
                        "a row that is not a mapping reached the panel, where "
                        "rendering it raises rather than skipping it")
        self.assertEqual([row["text"] for row in rows], ["kept", "also kept"])
        self.assertEqual(state["seen_id"], 1)

    def test_a_marker_that_is_not_a_number_starts_at_zero(self):
        """Unread-by-default. The other way round would mark everything read
        because a file was edited badly, which loses events silently."""
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"notices": [], "state": {"seen_id": "yesterday"}},
                      handle)

        _rows, state = db.load_notices()

        self.assertEqual(state["seen_id"], 0)

    def test_a_marker_written_as_a_string_of_digits_is_honoured(self):
        """Hand-edited files and older JSON both produce these, and the number
        is right even though the type is not."""
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"notices": [], "state": {"seen_id": "3"}}, handle)

        _rows, state = db.load_notices()

        self.assertEqual(state["seen_id"], 3)

    def test_a_missing_state_block_is_unread(self):
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"notices": [
                {"id": 1, "at": 0, "severity": "warning", "text": "one"}]},
                handle)

        rows, state = db.load_notices()

        self.assertEqual(len(rows), 1)
        self.assertEqual(state["seen_id"], 0)

    def test_a_write_that_fails_does_not_lose_the_event(self):
        """A notice that cannot be written down is still worth showing until
        the process ends. Persistence must never take the event with it."""
        db.NOTICES_FILE = os.path.join(
            db.NOTICES_FILE, "not-a-directory", "notices.json")

        entry = announce.record_notice("still counted", "error")

        self.assertEqual(config.notices, [entry])
        self.assertEqual(announce.unread_notices(), (1, "error"))


class WhatThePageIsHanded(DCCoreTestCase):

    def test_the_newest_notice_is_first(self):
        """Stored oldest-first because appending is the cheap end to write;
        shown newest-first because a panel is read from the top."""
        announce.record_notice("older", "warning")
        announce.record_notice("newer", "error")

        payload = webserver.build_notices_payload()

        self.assertEqual([row["text"] for row in payload["notices"]],
                         ["newer", "older"])

    def test_the_badge_numbers_come_from_the_server(self):
        """Not worked out in the page. The page would need the read marker to
        do it, and two places deciding whether to light up is two places that
        can disagree."""
        announce.record_notice("one", "warning")
        announce.record_notice("two", "error")

        payload = webserver.build_notices_payload()

        self.assertEqual(payload["unread"], 2)
        self.assertEqual(payload["severity"], "error")

    def test_the_read_marker_is_sent_so_rows_can_be_marked_fresh(self):
        announce.record_notice("read", "warning")
        announce.mark_notices_read()
        announce.record_notice("unread", "warning")

        payload = webserver.build_notices_payload()

        self.assertEqual(payload["seen_id"], 1)

    def test_an_empty_panel_is_a_payload_not_an_error(self):
        payload = webserver.build_notices_payload()

        self.assertEqual(payload["notices"], [])
        self.assertEqual(payload["unread"], 0)
        self.assertEqual(payload["severity"], "")

    def test_marking_read_answers_with_what_a_fresh_read_would_say(self):
        """The same answer clears the badge and redraws the list, so the two
        cannot disagree about what was acknowledged."""
        announce.record_notice("one", "warning")

        result = webserver.mark_notices_read_result()

        self.assertEqual(result["unread"], 0)
        self.assertEqual(result["severity"], "")
        self.assertEqual(len(result["notices"]), 1)
        self.assertEqual(result["seen_id"], 1)

    def test_marking_read_is_the_same_shape_as_reading(self):
        announce.record_notice("one", "warning")

        self.assertEqual(sorted(webserver.mark_notices_read_result()),
                         sorted(webserver.build_notices_payload()))


class TheThingsWorthTellingSomebodyAbout(DCCoreTestCase):
    """Which call sites raise one, and with which severity.

    Read from the source rather than driven, because driving them means an IRC
    loop and a subprocess; what matters here is the EDITORIAL decision - that
    these events and no others are worth a badge, and that "it is still true"
    is an error while "it happened and is over" is a warning.
    """

    @staticmethod
    def source(name):
        """`name`'s source with comments taken out.

        Every window in this class is anchored on the TEXT OF A MESSAGE, so a
        comment quoting that message moves the anchor - which is exactly what
        #510 did: a note explaining why 405 needs different wording quoted
        "gave up after 3 attempts", the split landed on the comment several
        hundred lines above the branch, and the window ran to an unrelated
        `else:` with no notice= in it at all.
        """
        import re as _re

        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return _re.sub(chr(35) + "[^" + chr(10) + "]*", "", handle.read())

    def calls(self, name):
        import re

        body = self.source(name)
        return re.findall(r'notice="(\w+)"', body)

    def test_giving_up_on_a_channel_is_an_error(self):
        """It is STILL TRUE: that channel is not being served and nothing will
        try again without the operator."""
        body = self.source("irc.py")
        # Trimmed at the `else:` below it rather than at the first ')' -
        # the message is an f-string and its own interpolations close
        # parentheses first, so a naive cut lands mid-message.
        gave_up = body.split("gave up after", 1)[1].split("else:", 1)[0]

        self.assertIn('notice="error"', gave_up)

    def test_a_kick_is_a_warning(self):
        """It happened, and a rejoin is already scheduled. Worth knowing,
        nothing to do right now."""
        body = self.source("irc.py")
        kicked = body.split("Kicked from {kicked_chan}", 1)[1]

        self.assertIn('notice="warning"', kicked.split("else:", 1)[0])

    def test_a_rebuild_that_failed_is_an_error(self):
        """A failed rebuild is still failed tomorrow: the list being served is
        the last good one and nothing retries on its own."""
        body = self.source("commands.py")
        failed = body.split("External update_list.py failed", 1)[1]

        self.assertIn('notice="error"', failed.split("last_list_update_ok", 1)[0])

    def test_a_rebuild_that_timed_out_is_an_error(self):
        body = self.source("commands.py")
        timed_out = body.split("List update FAILED", 1)[1]

        self.assertIn('notice="error"',
                      timed_out.split("last_list_update_ok", 1)[0])

    def test_every_severity_raised_anywhere_is_one_of_the_two(self):
        """A typo here is silent: record_notice() falls back to a warning, so
        an event meant to be an error would show amber forever."""
        raised = set()
        for name in ("irc.py", "commands.py", "oserve.py", "announce.py",
                     "dcc.py", "webserver.py"):
            raised.update(self.calls(name))

        self.assertTrue(raised, "nothing raises a notice, so this checked "
                                "nothing")
        self.assertEqual(sorted(raised - set(announce.NOTICE_SEVERITIES)), [])

    def test_user_bans_and_mutes_do_not_raise_one(self):
        """DCCore banning a USER is routine and would flood the badge - which
        is the argument for raising notices explicitly rather than inferring
        them from a log category."""
        body = self.source("dcc.py")

        self.assertEqual(self.calls("dcc.py"), [])
        self.assertIn("send_debug", body)


class TheOperatorsOwnContracts(DCCoreTestCase):
    """The four things a runtime container has to satisfy. Three are checked
    generically elsewhere - tests/test_runtime_state.py walks every container
    in runtime.py - and the fourth is here because it is about these two."""

    def test_both_survive_a_rehash(self):
        """A rehash is not an acknowledgement. Losing these would clear the
        badge without anybody having looked at what it was for."""
        import commands

        self.assertIn("notices", commands.PRESERVE_RUNTIME)
        self.assertIn("notice_state", commands.PRESERVE_RUNTIME)

    def test_the_harness_resets_them_between_tests(self):
        """A leftover is a badge in the next test counting an event from the
        last one."""
        from tests import support

        self.assertEqual(support.RUNTIME_CONTAINERS["notices"], list)
        self.assertEqual(support.RUNTIME_CONTAINERS["notice_state"], dict)


if __name__ == "__main__":
    unittest.main()
