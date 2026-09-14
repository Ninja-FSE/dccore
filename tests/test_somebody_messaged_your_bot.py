"""Private messages the bot does not answer are written down.

An unrecognised private message was dropped in the read loop: no reply, and no
record either. Verified before this was built - the whole PRIVMSG block
contains three logging calls and all three are error handlers, nothing writes
to disk, and the Console buffer only carries send_debug() output. So an
operator could never find out that anybody had tried.

THE SILENCE STAYS WHILE THE MESSAGES ARE KEPT. A bot that answers every stray
line is one that can be made to flood itself off the network, which is why the
rate limiter upstream exists. Only the record changes.

Turning the feature OFF is the one case that speaks - it keeps nothing and
tells the sender once where to go instead, under four separate brakes. That is
a different contract with the person who typed, and it lives in
test_a_bot_that_does_not_take_messages.py.

It is kept apart from the notices, and that is a design decision rather than a
filing one. A notice is something that went WRONG and carries one of two
severities; a message is neither wrong nor right, and giving it a severity
would mean inventing a third that nobody can tell apart at a glance - which
the notices design says in as many words it will not do.
"""

import io
import json
import os
import sys
import time
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


class WritingItDown(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        # The cooldown is about one PERSON repeating themselves; most tests
        # here are about different senders, so it only gets in the way.
        announce._pm_last_recorded.clear()

    def test_a_message_is_recorded_with_who_and_what(self):
        entry = announce.record_private_message("SomeUser", "are you there?")

        self.assertEqual(entry["nick"], "SomeUser")
        self.assertEqual(entry["text"], "are you there?")
        self.assertEqual(config.private_messages, [entry])

    def test_the_text_is_kept_not_just_a_count(self):
        """"Three people messaged you" is not something an operator can act
        on. "can you send me the new album" is."""
        announce.record_private_message("SomeUser",
                                        "can you send me the new album")

        self.assertIn("new album", config.private_messages[0]["text"])

    def test_ids_count_up_and_are_never_reused(self):
        first = announce.record_private_message("One", "hello")
        second = announce.record_private_message("Two", "hello")

        self.assertEqual([first["id"], second["id"]], [1, 2])

    def test_an_id_is_not_reused_after_the_oldest_is_dropped(self):
        """Ids come from the last entry, not from the length - the same number
        until the cap starts discarding, and different forever after."""
        for index in range(4):
            announce.record_private_message("User%d" % index, "hello")
        del config.private_messages[:2]

        self.assertEqual(announce.record_private_message("Next", "hi")["id"], 5)

    def test_a_very_long_message_is_trimmed_not_dropped(self):
        """Somebody pasting is still somebody trying to ask something, and the
        first part of it says what they wanted."""
        entry = announce.record_private_message("SomeUser", "x" * 5000)

        self.assertEqual(len(entry["text"]), 400)

    def test_an_empty_message_is_not_recorded(self):
        self.assertIsNone(announce.record_private_message("SomeUser", "   "))
        self.assertIsNone(announce.record_private_message("", "hello"))
        self.assertEqual(config.private_messages, [])

    def test_the_oldest_go_once_the_cap_is_reached(self):
        for index in range(announce.PRIVATE_MESSAGES_MAX + 5):
            announce._pm_last_recorded.clear()
            announce.record_private_message("User%d" % index, "hello")

        self.assertEqual(len(config.private_messages),
                         announce.PRIVATE_MESSAGES_MAX)

    def test_it_writes_into_the_shared_list_not_a_copy(self):
        announce.record_private_message("SomeUser", "hello")

        self.assertIs(config.private_messages, runtime.private_messages)


class OnePersonRepeatingThemselves(DCCoreTestCase):
    """Somebody typing four lines because the first got no answer is one
    person trying to ask something, not four events - and four rows of it
    buries the next person who tries."""

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()
        self.set_config(PRIVATE_MESSAGE_COOLDOWN_SECONDS=300)

    def test_the_second_message_from_one_sender_is_dropped(self):
        announce.record_private_message("SomeUser", "hello?")

        self.assertIsNone(announce.record_private_message("SomeUser", "anyone?"))
        self.assertEqual(len(config.private_messages), 1)

    def test_the_cooldown_is_per_sender_not_global(self):
        """The control that matters: a throttle which silenced everybody after
        one message would hide exactly the person worth hearing from."""
        announce.record_private_message("SomeUser", "hello?")

        second = announce.record_private_message("Another", "hello?")

        self.assertIsNotNone(second)
        self.assertEqual(len(config.private_messages), 2)

    def test_the_match_is_case_insensitive(self):
        """Nicks are, on IRC. Otherwise one person switching case bypasses
        their own cooldown."""
        announce.record_private_message("SomeUser", "hello?")

        self.assertIsNone(announce.record_private_message("SOMEUSER", "?"))

    def test_they_are_heard_again_once_the_cooldown_passes(self):
        """It silences a burst, not a person."""
        announce.record_private_message("SomeUser", "hello?")
        announce._pm_last_recorded["someuser"] = time.time() - 600

        self.assertIsNotNone(announce.record_private_message("SomeUser", "?"))

    def test_a_cooldown_of_zero_records_everything(self):
        self.set_config(PRIVATE_MESSAGE_COOLDOWN_SECONDS=0)
        announce.record_private_message("SomeUser", "one")

        self.assertIsNotNone(announce.record_private_message("SomeUser", "two"))


class HowManyHaveNotBeenSeen(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()

    def test_everything_is_unread_before_anybody_looks(self):
        announce.record_private_message("One", "hello")
        announce.record_private_message("Two", "hello")

        self.assertEqual(announce.unread_private_messages(), 2)

    def test_marking_read_clears_the_count(self):
        announce.record_private_message("One", "hello")

        announce.mark_private_messages_read()

        self.assertEqual(announce.unread_private_messages(), 0)

    def test_marking_read_keeps_the_messages(self):
        """Acknowledged, not deleted - the panel is where you go to read
        them."""
        announce.record_private_message("One", "hello")

        announce.mark_private_messages_read()

        self.assertEqual(len(config.private_messages), 1)

    def test_one_arriving_after_the_mark_is_unread(self):
        announce.record_private_message("One", "hello")
        announce.mark_private_messages_read()

        announce.record_private_message("Two", "hello")

        self.assertEqual(announce.unread_private_messages(), 1)

    def test_the_marker_is_the_highest_id_not_the_count(self):
        """The same number until the cap discards, and a mutant using len()
        would leave the count stuck above zero forever."""
        for index in range(4):
            announce._pm_last_recorded.clear()
            announce.record_private_message("User%d" % index, "hello")
        del config.private_messages[:2]

        announce.mark_private_messages_read()

        self.assertEqual(config.private_message_state["seen_id"], 4)
        self.assertEqual(announce.unread_private_messages(), 0)


class WhatSurvivesARestart(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()

    def test_a_message_is_on_disk_as_soon_as_it_arrives(self):
        announce.record_private_message("SomeUser", "hello")

        with io.open(db.PRIVATE_MESSAGES_FILE, encoding="utf-8") as handle:
            stored = json.load(handle)

        self.assertEqual(len(stored["messages"]), 1)

    def test_a_round_trip_returns_what_went_in(self):
        announce.record_private_message("SomeUser", "hello")
        announce.mark_private_messages_read()

        rows, state = db.load_private_messages()

        self.assertEqual(rows[0]["nick"], "SomeUser")
        self.assertEqual(state["seen_id"], 1)

    def test_a_file_that_will_not_parse_costs_the_panel_and_nothing_else(self):
        with io.open(db.PRIVATE_MESSAGES_FILE, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertEqual(db.load_private_messages(), ([], {"seen_id": 0}))

    def test_one_unusable_row_does_not_take_the_others(self):
        with io.open(db.PRIVATE_MESSAGES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"messages": [
                {"id": 1, "at": 0, "nick": "One", "text": "kept"},
                "a bare string where a row should be",
                {"id": 3, "at": 0, "nick": "Two", "text": "also kept"},
            ], "state": {"seen_id": 1}}, handle)

        rows, _state = db.load_private_messages()

        self.assertEqual([r["text"] for r in rows], ["kept", "also kept"])

    def test_a_marker_that_is_not_a_number_starts_unread(self):
        with io.open(db.PRIVATE_MESSAGES_FILE, "w", encoding="utf-8") as handle:
            json.dump({"messages": [], "state": {"seen_id": "yesterday"}},
                      handle)

        self.assertEqual(db.load_private_messages()[1]["seen_id"], 0)

    def test_a_write_that_fails_does_not_lose_the_message(self):
        db.PRIVATE_MESSAGES_FILE = os.path.join(
            db.PRIVATE_MESSAGES_FILE, "not-a-directory", "pm.json")

        entry = announce.record_private_message("SomeUser", "hello")

        self.assertEqual(config.private_messages, [entry])


class WhatTheCaptureDecides(unittest.TestCase):
    """Where in the read loop it happens, and what that placement rules out.

    Read from the source: reaching this by driving the loop means a socket,
    and the placement is the whole point - each of the four conditions below
    excludes a different thing that is not a person typing.
    """

    @staticmethod
    def capture():
        """The capture block, CODE ONLY.

        Split on the call with its arguments, not on the function name: the
        comment above it names the function too, so splitting on the name
        alone cut the extract off before any of the conditions and left three
        guards asserting against prose. Comments are then stripped for the
        same reason - they explain each condition by naming what it excludes,
        so a search over them matches the explanation rather than the code.
        """
        import re as _re

        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as f:
            body = f.read()
        block = body.split("announce.record_private_message(user, msg)", 1)[0]
        block = block.rsplit(
            "if is_bot_command and security.is_flooding(user):", 1)[1]
        # chr(35) is "#": written this way so no editing tool can mangle
        # the escape out of the pattern, which has happened before.
        return _re.sub(chr(35) + "[^" + chr(10) + "]*", "", block)

    def test_only_when_it_is_not_a_command(self):
        self.assertIn("not is_bot_command", self.capture())

    def test_only_when_it_was_sent_privately(self):
        """A channel line is one the operator can already see, and recording
        every channel message would be a log of other people's conversations
        rather than of anybody talking to us."""
        self.assertIn("target_chan.lower() == config.NICKNAME.lower()",
                      self.capture())

    def test_never_a_ctcp(self):
        """A CTCP is a client talking to a client - VERSION, a DCC offer - not
        a person typing something they expect an answer to."""
        self.assertIn('not msg.startswith("\\x01")', self.capture())

    def test_it_happens_after_the_flood_check(self):
        """So a flood cannot fill the panel. Asserted by WHERE the capture is
        - everything this reads sits after that gate in the file."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as f:
            body = f.read()

        self.assertLess(body.index("if is_bot_command and security.is_flooding"),
                        body.index("record_private_message("))

    def test_it_happens_after_the_ban_check(self):
        """A banned user's message is dropped before this, so a ban silences
        them in the panel too - not just in the channel."""
        with io.open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as f:
            body = f.read()

        self.assertLess(body.index("if not security.check_user_status(user"),
                        body.index("record_private_message("))

    def test_the_bot_still_says_nothing(self):
        """The behaviour that must not change ON THE RECORDING ARM. Nothing
        between the flood gate and the call sends, queues or notices anything
        - it writes the message down and moves on.

        The other arm of that branch DOES speak, and deliberately: a bot with
        PRIVATE_MESSAGES_ENABLED off keeps nothing and tells the sender once
        where to go instead. That is a different contract and is guarded
        separately, in test_a_bot_that_does_not_take_messages.py. This extract
        stops at the recording call, so it covers only the arm it names.
        """
        capture = self.capture()

        for outbound in ("send_notice", "queue_message", "send_debug",
                         "sendall", "PRIVMSG"):
            with self.subTest(outbound=outbound):
                self.assertNotIn(outbound, capture)


class WhatThePageIsHanded(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        announce._pm_last_recorded.clear()

    def test_the_newest_message_is_first(self):
        announce.record_private_message("Older", "first")
        announce.record_private_message("Newer", "second")

        payload = webserver.build_messages_payload()

        self.assertEqual([r["nick"] for r in payload["messages"]],
                         ["Newer", "Older"])

    def test_the_unread_count_comes_from_the_server(self):
        announce.record_private_message("One", "hello")

        self.assertEqual(webserver.build_messages_payload()["unread"], 1)

    def test_an_empty_inbox_is_a_payload_not_an_error(self):
        payload = webserver.build_messages_payload()

        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["unread"], 0)

    def test_marking_read_answers_with_what_a_fresh_read_would_say(self):
        announce.record_private_message("One", "hello")

        result = webserver.mark_messages_read_result()

        self.assertEqual(result["unread"], 0)
        self.assertEqual(len(result["messages"]), 1)

    def test_the_payload_offers_no_way_to_reply(self):
        """The bot has no conversation path at all, so an API that looked like
        it could answer would be a promise the daemon cannot keep."""
        announce.record_private_message("One", "hello")

        payload = webserver.build_messages_payload()

        for row in payload["messages"]:
            self.assertEqual(sorted(row), ["at", "id", "nick", "text"])


class ThePageSaysItDoesNotReply(unittest.TestCase):
    """The one thing somebody could get wrong here is expecting the bot to
    answer. Everything on that page looks like a conversation and is not
    one."""

    @staticmethod
    def markup():
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                     encoding="utf-8") as handle:
            return handle.read()

    def section(self):
        return self.markup().split('id="view-messages"', 1)[1].split(
            "</section>", 1)[0]

    def test_it_says_so_before_the_list(self):
        section = self.section()
        said = section.index("never replies")
        listed = section.index('id="message-list"')

        self.assertLess(said, listed,
                        "the list is read before the warning that nothing "
                        "answers it")

    def test_there_is_no_reply_field(self):
        """Not an oversight to be fixed later - there is nothing behind it."""
        section = self.section()

        self.assertNotIn("<textarea", section)
        self.assertNotIn('type="text"', section)

    def test_the_unread_count_hides_when_there_is_nothing(self):
        markup = self.markup()
        tag = markup.split('id="messages-nav-count"', 1)[1].split(">", 1)[0]

        self.assertIn("hidden", tag)

    def test_hidden_actually_hides_it(self):
        """.nav-count sets display, which outranks the `hidden` attribute's UA
        rule. Without the pair the count sits in the nav showing nothing."""
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                     encoding="utf-8") as handle:
            css = handle.read()

        self.assertIn(".nav-count[hidden]", css)


if __name__ == "__main__":
    unittest.main()
