"""Turning the Messages page off, and what the bot says instead.

Keeping private messages and declining them are two different contracts with
the person who typed, not one feature with its panel hidden:

    on  - the message is recorded, the page shows it, the bot says nothing
    off - nothing is recorded, the page is gone, and the sender is told once
          where to go instead

The second one is the only mode that tells them anything, which is the whole
reason it is worth having - somebody messaging a file server is usually
somebody who wants something from it and does not know the syntax. Until now
their choices were "be recorded and get no answer" or "be dropped and get no
answer".

BUT IT PUTS A LINE ON THE WIRE, and an auto-reply to anybody who messages you
is the classic way for a bot to be flooded off a network by strangers. Four
separate brakes, each covering something the others do not, and one of them -
NOTICE rather than PRIVMSG - is structural rather than a check that somebody
has to remember to write.
"""

import io
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

from tests.support import DCCoreTestCase, install_fake_oserve  # noqa: E402


class DeclineTestCase(DCCoreTestCase):
    """Private messages off, an admin named, and the send path captured.

    install_fake_oserve() rather than the real module: oserve is the process
    entry point and importing it starts worker threads. It records every call
    as (user, message, is_vip), so the "never the VIP lane" claim is asserted
    from what was actually asked for rather than inferred from which queue a
    line landed in.
    """

    def setUp(self):
        super().setUp()
        self.oserve = install_fake_oserve()
        config.PRIVATE_MESSAGES_ENABLED = False
        config.ADMIN_NICK = "TheOperator"

    def sent_to(self, nick):
        return [call for call in self.oserve.queued
                if call[0].lower() == nick.lower()]

    def line_to(self, nick):
        return self.sent_to(nick)[0][1]


class TheReplyItself(DeclineTestCase):

    def test_somebody_who_messages_is_told_where_to_go(self):
        self.assertTrue(announce.decline_private_message("Stranger"))

        self.assertEqual(len(self.sent_to("Stranger")), 1)

    def test_the_admin_nick_is_substituted_into_it(self):
        announce.decline_private_message("Stranger")

        self.assertIn("TheOperator", self.line_to("Stranger"))

    def test_it_is_a_notice_and_never_a_privmsg(self):
        """The brake that cannot be forgotten. RFC 1459 forbids a client
        auto-replying to a NOTICE, and irc.py's own parser matches PRIVMSG
        alone - so two bots both running this cannot answer each other into a
        loop. A PRIVMSG here would make that loop possible again."""
        announce.decline_private_message("Stranger")

        line = self.line_to("Stranger")
        self.assertTrue(line.startswith("NOTICE Stranger :"), line)
        self.assertNotIn("PRIVMSG", line)

    def test_it_waits_its_turn_like_everything_else(self):
        """The ordinary lane, not the VIP one. A decline is the least urgent
        thing the bot ever says and must not overtake the transfer notices
        somebody is actually waiting on."""
        announce.decline_private_message("Stranger")

        self.assertEqual(self.sent_to("Stranger")[0][2], False)

    def test_nothing_is_recorded_when_messages_are_off(self):
        """Off means kept nowhere, not kept quietly."""
        announce.decline_private_message("Stranger")

        self.assertEqual(config.private_messages, [])

    def test_an_empty_nick_is_not_replied_to(self):
        self.assertFalse(announce.decline_private_message("   "))

        self.assertEqual(self.oserve.queued, [])


class WhenNobodyIsNamedAsAdmin(DeclineTestCase):
    """ADMIN_NICK defaults to None, so the substitution has to survive it."""

    def setUp(self):
        super().setUp()
        config.ADMIN_NICK = None

    def test_a_stranger_is_never_told_to_message_none(self):
        """The bug this exists to stop: "Please message None instead", sent
        to somebody who now knows less than before they asked."""
        announce.decline_private_message("Stranger")

        line = self.line_to("Stranger")
        self.assertNotIn("None", line)

    def test_the_sentence_still_reads(self):
        announce.decline_private_message("Stranger")

        self.assertIn("the bot's owner", self.line_to("Stranger"))

    def test_a_blank_text_means_the_bot_says_nothing_at_all(self):
        """A real third position: no record, no page, and no line on the wire
        either. An operator who wants complete silence should be able to ask
        for it without editing code."""
        config.PRIVATE_MESSAGE_DECLINE_TEXT = "   "

        self.assertFalse(announce.decline_private_message("Stranger"))
        self.assertEqual(self.oserve.queued, [])


class OnlyOnceADay(DeclineTestCase):
    """Brake two: one reply per sender per interval."""

    def test_the_second_message_from_one_sender_gets_no_reply(self):
        announce.decline_private_message("Stranger", now=1000.0)

        self.assertFalse(announce.decline_private_message("Stranger", now=1100.0))
        self.assertEqual(len(self.sent_to("Stranger")), 1)

    def test_the_interval_is_per_sender_not_global(self):
        """Somebody else asking is a different person who has not been told."""
        announce.decline_private_message("Stranger", now=1000.0)

        self.assertTrue(announce.decline_private_message("Another", now=1100.0))

    def test_the_match_is_case_insensitive(self):
        announce.decline_private_message("Stranger", now=1000.0)

        self.assertFalse(announce.decline_private_message("STRANGER", now=1100.0))

    def test_they_are_told_again_once_a_day_has_passed(self):
        announce.decline_private_message("Stranger", now=1000.0)

        self.assertTrue(announce.decline_private_message(
            "Stranger", now=1000.0 + config.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS + 1))

    def test_who_has_been_told_survives_a_restart(self):
        """RAM alone would mean the bot repeating itself to everybody every
        time the operator restarts it, which is most of what this prevents."""
        announce.decline_private_message("Stranger", now=1000.0)

        _rows, state = db.load_private_messages()

        self.assertIn("stranger", state.get("declined", {}))

    def test_a_stale_entry_is_pruned_rather_than_kept_for_ever(self):
        """Bounded, because an entry older than the interval can never stop a
        reply again - keeping it is keeping a fact that stopped meaning
        anything. State that only grows is state nobody ever looks at."""
        announce.decline_private_message("Stranger", now=1000.0)
        later = 1000.0 + config.PRIVATE_MESSAGE_DECLINE_INTERVAL_SECONDS + 1

        announce.decline_private_message("Another", now=later)

        self.assertNotIn("stranger", config.private_message_state["declined"])
        self.assertIn("another", config.private_message_state["declined"])


class WhenEverybodyMessagesAtOnce(DeclineTestCase):
    """Brake three, and the one the per-sender rule cannot provide: two
    hundred nicks messaging within a minute are two hundred FIRST messages,
    every one of them individually owed a reply."""

    def setUp(self):
        super().setUp()
        config.PRIVATE_MESSAGES_ENABLED = False
        config.ADMIN_NICK = "TheOperator"
        config.PRIVATE_MESSAGE_DECLINE_BURST = 3
        config.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS = 600

    def flood(self, count, now):
        return [announce.decline_private_message("Nick%d" % n, now=now)
                for n in range(count)]

    def test_replies_stop_once_the_ceiling_is_reached(self):
        sent = self.flood(5, now=1000.0)

        self.assertEqual(sent, [True, True, True, False, False])

    def test_the_ones_past_the_ceiling_are_dropped_silently(self):
        """Nobody is owed an explanation of why they did not get one - a
        second line saying "too busy to answer" would be the same flood."""
        self.flood(5, now=1000.0)

        self.assertEqual(self.sent_to("Nick4"), [])

    def test_the_window_moves_on_and_the_bot_speaks_again(self):
        self.flood(3, now=1000.0)

        self.assertTrue(announce.decline_private_message(
            "Later", now=1000.0 + config.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS + 1))

    def test_the_ceiling_counts_recent_replies_not_every_reply_ever(self):
        """Pruned to the window before it is measured. Counting every reply
        the bot has ever sent would silence it permanently after the first
        busy day."""
        self.flood(3, now=1000.0)
        moved_on = 1000.0 + config.PRIVATE_MESSAGE_DECLINE_BURST_SECONDS + 1
        announce.decline_private_message("Later", now=moved_on)

        self.assertEqual(len(config.private_message_decline_sends), 1)


class WhichOneHappens(unittest.TestCase):
    """The read loop does one or the other, never both. Read from the source
    for the reason the recording guards beside it give: reaching this by
    driving the loop means a socket."""

    @staticmethod
    def branch():
        """The two-way branch, CODE ONLY - comments name both calls while
        explaining them, so a search over them matches the explanation."""
        import re as _re

        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as f:
            body = f.read()
        block = body.split("announce.decline_private_message(user)", 1)[0]
        block = block.rsplit("if is_bot_command and security.is_flooding(user):", 1)[1]
        return _re.sub(chr(35) + "[^" + chr(10) + "]*", "", block)

    def test_the_setting_is_what_decides(self):
        self.assertIn("PRIVATE_MESSAGES_ENABLED", self.branch())

    def test_recording_is_the_other_arm_of_the_same_branch(self):
        """Not two independent ifs, which could both run and both record AND
        reply to the same message."""
        branch = self.branch()

        self.assertIn("announce.record_private_message(user, msg)", branch)
        self.assertIn("else:", branch.rsplit(
            "announce.record_private_message(user, msg)", 1)[1])


class WhatThePageIsToldWhenItIsOff(DeclineTestCase):

    def test_the_api_says_there_is_no_such_page(self):
        """404, not an empty list. An empty list means "nobody has messaged
        you", which is a fact about the world; this is a fact about the bot,
        and the page hides its own nav item on exactly this status."""
        _payload, status = webserver.messages_payload_or_404()

        self.assertEqual(status, 404)

    def test_marking_read_says_the_same_thing(self):
        """Turned off while somebody had the page open: the button must not
        silently do nothing."""
        _payload, status = webserver.messages_read_or_404()

        self.assertEqual(status, 404)

    def test_nothing_is_acknowledged_by_a_refused_read(self):
        """The marker must not move for a request that was turned away."""
        config.private_messages.append({"id": 7, "at": 1.0, "nick": "A", "text": "b"})

        webserver.messages_read_or_404()

        self.assertEqual(config.private_message_state.get("seen_id", 0), 0)

    def test_it_is_a_payload_again_once_it_is_switched_back_on(self):
        config.PRIVATE_MESSAGES_ENABLED = True

        payload, status = webserver.messages_payload_or_404()

        self.assertEqual(status, 200)
        self.assertIn("messages", payload)


class ThePageHidesItself(unittest.TestCase):
    """web/app.js. The Console learned this the hard way on a real install:
    deleting the section instead of hiding it made activateView() throw on
    every view switch and took the rest of the navigation with it."""

    @staticmethod
    def app_js():
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def disable_body():
        source = ThePageHidesItself.app_js()
        body = source.split("function disableMessagesUi()", 1)[1]
        return body.split(chr(10) + "  }", 1)[0]

    def test_the_nav_item_is_hidden_rather_than_removed(self):
        body = self.disable_body()

        self.assertIn("hidden = true", body)
        self.assertNotIn("remove()", body)

    def test_somebody_looking_at_the_page_is_moved_off_it(self):
        """Otherwise they are left on a section whose nav item has gone.

        The whole STATEMENT, not the call on its own: a bare
        activateView("search") is still present when the condition around it
        has been changed to something that can never be true, so asserting
        the call alone passes on a page that strands its reader."""
        self.assertIn(
            'if (state.active === "messages") { activateView("search"); }',
            self.disable_body())

    def test_only_a_404_hides_it_and_not_any_failure(self):
        """A network blip must not quietly delete a page that is still
        there - the page comes back on the next poll, the nav item would
        not."""
        source = self.app_js()
        loader = source.split("function loadMessages(", 1)[1]
        loader = loader.split("function renderMessageList(", 1)[0]

        self.assertIn('String(err.message) === "HTTP 404"', loader)
        self.assertIn("disableMessagesUi()", loader)
