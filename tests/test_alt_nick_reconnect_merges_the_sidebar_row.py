"""#376, part 1: a peer bot's alt-nick reconnect merges into one sidebar row.

A bot that falls back to its alt nick after a 433 at connect time has no NICK
event to prove the rename - the client has not joined anything yet, so there
is nothing for irc.note_nick_change() to see. Without something else, its old
nick's List Browser row and its new nick's row read as two unrelated bots.

THE DECISION, recorded on the issue rather than guessed: infer it from
presence instead - a nick disappearing and a collision-shaped variant of it
appearing shortly after is the same signal a person watching would use. Three
safeguards came out of review, because the two failure modes are not
symmetric: a false NEGATIVE leaves today's duplicate row (visibly odd,
harmless); a false POSITIVE merges two different operators' libraries into
one (invisible - it looks exactly like a correct merge).

  1. The departure must be OBSERVED - a real PART/QUIT this daemon actually
     saw remove the nick from config.channel_users - never inferred from mere
     absence, which cannot tell "just left" from "was never in a channel we
     share".
  2. The gap must be tight (ALT_NICK_RECONNECT_WINDOW_SECONDS) - a 433 retry
     is seconds, and anything further out is a different session.
  3. The two nicks must fit the ordinary collision shape - one is the other
     plus a trailing run of "_"/digits - not merely a shared prefix.

And the result is DISPLAY ONLY: it can only ever change which nick a List
Browser row is grouped and labelled under (runtime.nick_aliases, read solely
by runtime.resolve_display_nick()). It never reaches config.channel_users,
fetched_bot_lists, known_bots, or a download counter - a wrong guess costs one
mis-grouped sidebar row, not a wrongly-attributed transfer.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheCollisionShapeIsNarrow(unittest.TestCase):
    """irc._is_collision_variant(): the same base nick, one copy carrying an
    ordinary retry suffix the other lacks - not merely two similar nicks."""

    def test_a_trailing_underscore_matches(self):
        self.assertTrue(irc._is_collision_variant("somebot", "somebot_"))

    def test_a_trailing_digit_matches(self):
        self.assertTrue(irc._is_collision_variant("somebot", "somebot1"))

    def test_an_underscore_and_digits_together_match(self):
        self.assertTrue(irc._is_collision_variant("somebot", "somebot_2"))

    def test_it_does_not_care_which_side_carries_the_suffix(self):
        self.assertTrue(irc._is_collision_variant("somebot_", "somebot"))

    def test_it_is_case_insensitive(self):
        self.assertTrue(irc._is_collision_variant("SomeBot", "somebot_"))

    def test_two_unrelated_nicks_do_not_match(self):
        self.assertFalse(irc._is_collision_variant("somebot", "otherbot"))

    def test_two_nicks_that_both_carry_a_suffix_do_not_match(self):
        """The concrete risk raised on the issue: "bot1" and "bot2" are just
        as likely two different people whose nicks happen to end in a digit,
        and neither is the other's bare base."""
        self.assertFalse(irc._is_collision_variant("bot1", "bot2"))

    def test_a_leading_digit_or_underscore_is_not_a_suffix(self):
        """Anchored to the end only - a nick starting with a digit is
        somebody's actual nick, not a client's retry."""
        self.assertFalse(irc._is_collision_variant("bot", "2bot"))

    def test_identical_nicks_are_not_a_variant_of_each_other(self):
        self.assertFalse(irc._is_collision_variant("somebot", "somebot"))
        self.assertFalse(irc._is_collision_variant("SomeBot", "somebot"))

    def test_empty_input_is_handled(self):
        self.assertFalse(irc._is_collision_variant("", "somebot"))
        self.assertFalse(irc._is_collision_variant("somebot", ""))
        self.assertFalse(irc._is_collision_variant("", ""))


class OnlyAnObservedDepartureIsRecorded(DCCoreTestCase):
    """irc.note_observed_departure(): the write path note_possible_reconnect()
    later trusts. Called only from PART/QUIT, only after they actually found
    and removed the nick - never for a nick that merely stopped appearing."""

    def test_it_records_the_nick_lowercased_as_the_key(self):
        irc.note_observed_departure("SomeBot", "#chan", now=1000.0)

        self.assertIn("somebot", runtime.recent_departures)

    def test_it_keeps_the_real_case_for_display_later(self):
        """A merged row must read "SomeBot", not "somebot" - see
        note_possible_reconnect()'s own comment on why this is kept."""
        irc.note_observed_departure("SomeBot", "#chan", now=1000.0)

        self.assertEqual(runtime.recent_departures["somebot"]["nick"], "SomeBot")

    def test_an_empty_nick_is_a_no_op(self):
        irc.note_observed_departure("", "#chan", now=1000.0)

        self.assertEqual(runtime.recent_departures, {})

    def test_a_later_departure_of_the_same_nick_overwrites_the_timestamp(self):
        irc.note_observed_departure("somebot", "#chan", now=1000.0)
        irc.note_observed_departure("somebot", "#chan", now=1005.0)

        self.assertEqual(runtime.recent_departures["somebot"]["at"], 1005.0)


class DeparturesArePrunedOnTheirOwnTightWindow(DCCoreTestCase):

    def test_a_recent_departure_survives_a_prune(self):
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        irc._prune_recent_departures(1000.0 + irc.ALT_NICK_RECONNECT_WINDOW_SECONDS - 1)

        self.assertIn("somebot", runtime.recent_departures)

    def test_an_old_departure_is_pruned(self):
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        irc._prune_recent_departures(1000.0 + irc.ALT_NICK_RECONNECT_WINDOW_SECONDS + 1)

        self.assertNotIn("somebot", runtime.recent_departures)


class ReconnectInferenceRequiresAllThreeThings(DCCoreTestCase):
    """irc.note_possible_reconnect(): the decision itself."""

    def test_no_recent_departure_at_all_infers_nothing(self):
        result = irc.note_possible_reconnect("somebot_", now=1000.0)

        self.assertIsNone(result)
        self.assertEqual(runtime.nick_aliases, {})

    def test_an_observed_departure_of_a_collision_shaped_nick_is_aliased(self):
        irc.note_observed_departure("SomeBot", "#chan", now=1000.0)

        result = irc.note_possible_reconnect("SomeBot_", now=1001.0)

        self.assertEqual(result, "SomeBot")
        self.assertEqual(runtime.nick_aliases["somebot_"], "SomeBot")

    def test_a_departure_of_an_unrelated_shaped_nick_is_not_aliased(self):
        """The old nick left, but the new one is not a collision variant of
        it - an ordinary, unrelated join, which must not merge anything."""
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        result = irc.note_possible_reconnect("otherbot", now=1001.0)

        self.assertIsNone(result)
        self.assertEqual(runtime.nick_aliases, {})

    def test_outside_the_window_is_not_aliased(self):
        """A 433 retry is seconds. A join that shows up long after the
        departure is a different session, not the same reconnect."""
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        result = irc.note_possible_reconnect(
            "somebot_", now=1000.0 + irc.ALT_NICK_RECONNECT_WINDOW_SECONDS + 1)

        self.assertIsNone(result)

    def test_rejoining_under_its_own_name_is_not_an_alt_nick_story(self):
        """An ordinary reconnect under the SAME name - nothing to merge."""
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        result = irc.note_possible_reconnect("somebot", now=1001.0)

        self.assertIsNone(result)
        self.assertEqual(runtime.nick_aliases, {})

    def test_rejoining_under_its_own_name_clears_the_stale_record(self):
        """The concrete risk this guards against: without clearing it, the
        departure timestamp could sit there and wrongly match some later,
        unrelated collision-shaped join."""
        irc.note_observed_departure("somebot", "#chan", now=1000.0)
        irc.note_possible_reconnect("somebot", now=1001.0)

        result = irc.note_possible_reconnect("somebot_", now=1002.0)

        self.assertIsNone(result)
        self.assertNotIn("somebot", runtime.recent_departures)

    def test_a_used_departure_cannot_be_matched_a_second_time(self):
        """One departure aliases at most one nick - otherwise a bot that
        departs once and gets guessed at by two different later joins would
        merge two unrelated nicks into the same primary."""
        irc.note_observed_departure("somebot", "#chan", now=1000.0)
        first = irc.note_possible_reconnect("somebot_", now=1001.0)

        second = irc.note_possible_reconnect("somebot1", now=1002.0)

        self.assertEqual(first, "somebot")
        self.assertIsNone(second)

    def test_an_empty_new_nick_is_a_no_op(self):
        irc.note_observed_departure("somebot", "#chan", now=1000.0)

        result = irc.note_possible_reconnect("", now=1001.0)

        self.assertIsNone(result)


class ResolveDisplayNickIsPureAndDisplayOnly(DCCoreTestCase):
    """runtime.resolve_display_nick(): the one reader of nick_aliases."""

    def test_a_nick_with_no_alias_resolves_to_itself(self):
        self.assertEqual(runtime.resolve_display_nick("somebot"), "somebot")

    def test_an_aliased_nick_resolves_to_its_primary(self):
        runtime.nick_aliases["somebot_"] = "SomeBot"

        self.assertEqual(runtime.resolve_display_nick("somebot_"), "SomeBot")

    def test_the_lookup_is_case_insensitive_on_the_input(self):
        runtime.nick_aliases["somebot_"] = "SomeBot"

        self.assertEqual(runtime.resolve_display_nick("SomeBot_"), "SomeBot")

    def test_an_empty_nick_is_handled(self):
        self.assertEqual(runtime.resolve_display_nick(""), "")

    def test_it_never_writes_anything(self):
        """Pure lookup - a caller resolving a nick that happens to look
        alt-nick-shaped must not itself create an alias."""
        runtime.resolve_display_nick("somebot_")

        self.assertEqual(runtime.nick_aliases, {})


class TheSidebarPayloadUsesTheResolvedNickOnly(DCCoreTestCase):
    """webserver.build_fetched_bot_list_summaries(): the one caller of
    resolve_display_nick(), and the boundary "display only" actually means
    something at - every OTHER field must still read the real nick."""

    def test_a_held_lists_nick_field_is_resolved(self):
        runtime.nick_aliases["somebot_"] = "SomeBot"
        config.fetched_bot_lists["somebot_"] = {
            "bot": "SomeBot_", "fetched_at": 1000, "entry_count": 5,
        }

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nick"], "SomeBot")

    def test_the_real_bot_key_is_unaffected_by_the_alias(self):
        """The property the whole feature rests on: re-fetching, purging and
        the online check all still target the REAL nick, never the alias."""
        runtime.nick_aliases["somebot_"] = "SomeBot"
        config.fetched_bot_lists["somebot_"] = {
            "bot": "SomeBot_", "fetched_at": 1000, "entry_count": 5,
        }

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual(rows[0]["bot"], "SomeBot_")

    def test_an_advertise_only_bots_nick_field_is_also_resolved(self):
        runtime.nick_aliases["somebot_"] = "SomeBot"
        runtime.known_bots["somebot_"] = {
            "nick": "SomeBot_", "channel": "#chan", "last_seen": 1000.0,
        }

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nick"], "SomeBot")
        self.assertEqual(rows[0]["bot"], "SomeBot_")

    def test_a_bot_with_no_alias_is_unaffected(self):
        config.fetched_bot_lists["somebot"] = {
            "bot": "SomeBot", "fetched_at": 1000, "entry_count": 5,
        }

        rows = webserver.build_fetched_bot_list_summaries()

        self.assertEqual(rows[0]["nick"], "SomeBot")
        self.assertEqual(rows[0]["bot"], "SomeBot")


class ThePresentNetworkCanDisproveAnOldAlias(DCCoreTestCase):
    """webserver._display_nick(): nick_aliases never expires, so the 15-second
    window only bounds how long a departure is TRUSTED, not how long the
    resulting alias is APPLIED. Found on review: two nicks online at the SAME
    instant are two connections - one bot cannot be both - so that is not
    weaker evidence than the departure/rejoin pattern that created the alias,
    it is the opposite arriving later, and it must win."""

    def setUp(self):
        super().setUp()
        runtime.nick_aliases["somebot_"] = "SomeBot"
        config.fetched_bot_lists["somebot"] = {
            "bot": "SomeBot", "fetched_at": 1000, "entry_count": 5,
        }
        config.fetched_bot_lists["somebot_"] = {
            "bot": "SomeBot_", "fetched_at": 2000, "entry_count": 7,
        }

    def rows_by_bot(self):
        return {row["bot"]: row for row in webserver.build_fetched_bot_list_summaries()}

    def test_both_nicks_online_at_once_splits_the_rows_back_apart(self):
        """The concrete failure: two visibly-online bots merged into one
        sidebar identity, when the network itself is proving they cannot be
        the same connection."""
        config.channel_users["#chan"] = {"somebot", "somebot_"}

        rows = self.rows_by_bot()

        self.assertEqual(rows["SomeBot"]["nick"], "SomeBot")
        self.assertEqual(rows["SomeBot_"]["nick"], "SomeBot_")

    def test_only_the_alt_online_is_the_real_reconnect_and_stays_merged(self):
        """The ordinary case this whole feature exists for: the old nick is
        gone, the alt is the one actually connected - nothing here disproves
        the alias, so it still applies."""
        config.channel_users["#chan"] = {"somebot_"}

        rows = self.rows_by_bot()

        self.assertEqual(rows["SomeBot"]["nick"], "SomeBot")
        self.assertEqual(rows["SomeBot_"]["nick"], "SomeBot")

    def test_neither_online_falls_back_to_the_alias(self):
        """Both have signed off. Nothing currently disproves the merge, so it
        is left as the best evidence there is."""
        rows = self.rows_by_bot()

        self.assertEqual(rows["SomeBot_"]["nick"], "SomeBot")

    def test_an_empty_presence_mirror_does_not_split_anything(self):
        """An empty config.channel_users means "still joining", not "nobody
        is here" - present_nicks() returns it as falsy for exactly that
        reason, and this must not mistake it for a disproof."""
        self.assertEqual(dict(config.channel_users), {})

        rows = self.rows_by_bot()

        self.assertEqual(rows["SomeBot_"]["nick"], "SomeBot")

    def test_an_advertised_only_row_is_disproved_too(self):
        """The SECOND row site. build_fetched_bot_list_summaries() builds two
        kinds of row - one for a bot whose list we hold, one for a bot we have
        only seen advertising - and applies _display_nick() at both. Every
        other test above only exercises the held kind, which left the
        advertised-only site free to regress back to a bare
        resolve_display_nick() with the whole suite still green."""
        config.fetched_bot_lists.clear()
        runtime.known_bots["somebot"] = {"nick": "SomeBot", "files": 10}
        runtime.known_bots["somebot_"] = {"nick": "SomeBot_", "files": 12}
        config.channel_users["#chan"] = {"somebot", "somebot_"}

        rows = self.rows_by_bot()

        self.assertFalse(rows["SomeBot_"]["held"],
                         "Meant to exercise the advertised-only row site.")
        self.assertEqual(rows["SomeBot"]["nick"], "SomeBot")
        self.assertEqual(rows["SomeBot_"]["nick"], "SomeBot_")


class TheHandlersActuallyCallTheseFunctions(unittest.TestCase):
    """Structural, for the same reason test_a_rename_carries_the_users_state.py's
    own TheHandlerActuallyCallsIt class is: the read loop is not run here, and
    the whole point of extracting these as standalone functions was that they
    could be tested directly - this just closes the gap of "tested in
    isolation, but is anything inside irc_loop() actually wired to call it"."""

    @staticmethod
    def source():
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as f:
            return f.read()

    def test_the_part_branch_calls_it_only_after_actually_removing_someone(self):
        block = self.source().split('elif is_user_event(line, "PART"):', 1)[1]
        block = block.split("# Anchored", 1)[0]

        self.assertIn("note_observed_departure(", block)
        # Inside the "actually found and removed them" branch, not merely
        # inside the handler - see the OBSERVED-ONLY contract on the function
        # itself for why an unconditional call here would defeat the point.
        removal_and_after = block.split("config.channel_users[p_chan].remove(p_user)", 1)[1]
        self.assertIn("note_observed_departure(", removal_and_after[:300])

    def test_the_quit_branch_calls_it_only_when_something_was_actually_removed(self):
        block = self.source().split('elif is_user_event(line, "QUIT"):', 1)[1]
        block = block.split("# Cross-bot search broadcast capture", 1)[0]

        self.assertIn("note_observed_departure(", block)
        self.assertIn("if quit_chan is not None:", block)

    def test_the_join_branch_calls_the_reconnect_check(self):
        block = self.source().split('elif is_user_event(line, "JOIN")', 1)[1]
        block = block.split("# Anchored: as JOIN", 1)[0]

        self.assertIn("note_possible_reconnect(", block)


if __name__ == "__main__":
    unittest.main()
