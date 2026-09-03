"""JOIN, PART, QUIT, NICK and 433 must not be forgeable by anyone typing.

Companion to test_server_numerics.py, which anchored the numerics. These are the
USER events, and they had the same defect: the read loop sees each line as raw
text, and the handlers tested for a bare substring - so the command name matched
anywhere, including inside a PRIVMSG body, a PART or QUIT reason, or a TOPIC.

Unlike the numerics, one of these was reachable by accident rather than only by
attack. `" QUIT " in line` matched an ordinary search - "@find QUIT PLAYING
GAMES" - and the QUIT handler then removed the searcher from every channel in
config.channel_users, which freezes their queue and hands it to the five-minute
delete timer. The user loses their queue for looking up a song.

The conditions under test are READ OUT OF irc.py and evaluated, not retyped, for
the reason tests/test_irc_dispatch.py and tests/test_server_numerics.py already
give: a test that only searches source text agrees with itself, not with the
daemon. It cannot see an ``or`` become an ``and``, or two guards get swapped.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


# Real lines in the shapes Undernet actually sends. JOIN appears both with and
# without the leading colon on the channel, and a netsplit QUIT reason is two
# server names - which is exactly the sort of free text the old guards trusted.
GENUINE = {
    "JOIN": ":dave!~u@host.example.com JOIN #dccore-test",
    "JOIN_COLON": ":dave!~u@host.example.com JOIN :#dccore-test",
    "PART": ":dave!~u@host.example.com PART #dccore-test",
    "PART_REASON": ":dave!~u@host.example.com PART #dccore-test :Leaving",
    "QUIT": ":dave!~u@host.example.com QUIT :Ping timeout: 245 seconds",
    "QUIT_SPLIT": ":dave!~u@host.example.com QUIT :*.net *.split",
    "NICK": ":dave!~u@host.example.com NICK :dave2",
    "NICK_BARE": ":dave!~u@host.example.com NICK dave2",
    "433": ":irc.undernet.org 433 * DCCore :Nickname is already in use.",
    "432": ":irc.undernet.org 432 * DC-Core! :Erroneous Nickname",
}


class GenuineEventsStillWork(unittest.TestCase):
    """The guard must not be so strict that the bot stops tracking the channel."""

    def test_every_real_event_is_accepted(self):
        for key, command in (("JOIN", "JOIN"), ("JOIN_COLON", "JOIN"),
                             ("PART", "PART"), ("PART_REASON", "PART"),
                             ("QUIT", "QUIT"), ("QUIT_SPLIT", "QUIT"),
                             ("NICK", "NICK"), ("NICK_BARE", "NICK")):
            with self.subTest(line=key):
                self.assertTrue(irc.is_user_event(GENUINE[key], command), GENUINE[key])

    def test_a_part_with_no_reason_is_accepted(self):
        """PART often arrives with nothing after the channel; (\\s|$) covers that."""
        self.assertTrue(irc.is_user_event(":dave!u@h PART #dccore-test", "PART"))

    def test_an_admin_style_hostmask_is_accepted(self):
        line = ":SysOp!sysop@Undernet.CoolGuy.Users QUIT :Quit: leaving"
        self.assertTrue(irc.is_user_event(line, "QUIT"))


class ChannelTextCannotForgeAnEvent(unittest.TestCase):
    """Every one of these was accepted before the guard."""

    def test_a_search_for_a_song_is_not_a_quit(self):
        """The regression that cost real users their queues."""
        line = ":dave!u@h PRIVMSG #dccore-test :@find QUIT PLAYING GAMES"
        self.assertFalse(irc.is_user_event(line, "QUIT"))

    def test_talking_about_quitting_is_not_a_quit(self):
        line = ":attacker!u@h PRIVMSG #dccore-test :i will QUIT now"
        self.assertFalse(irc.is_user_event(line, "QUIT"))

    def test_talking_about_parting_is_not_a_part(self):
        line = ":attacker!u@h PRIVMSG #dccore-test :should i PART #dccore-test ?"
        self.assertFalse(irc.is_user_event(line, "PART"))

    def test_talking_about_joining_is_not_a_join(self):
        """A forged JOIN thawed the speaker's own frozen queue on demand."""
        line = ":attacker!u@h PRIVMSG #dccore-test :hello JOIN #dccore-test"
        self.assertFalse(irc.is_user_event(line, "JOIN"))

    def test_a_forged_nick_change_is_refused(self):
        """This one overwrote another user's send_queue entry."""
        line = ":attacker!u@h PRIVMSG #dccore-test :hey NICK :victim"
        self.assertFalse(irc.is_user_event(line, "NICK"))

    def test_a_part_reason_cannot_forge_a_quit(self):
        """Reasons are free text, and the old test read the whole line."""
        line = ":attacker!u@h PART #dccore-test :bye everyone QUIT soon"
        self.assertFalse(irc.is_user_event(line, "QUIT"))

    def test_a_quit_reason_cannot_forge_a_join(self):
        line = ":attacker!u@h QUIT :i will JOIN #other later"
        self.assertFalse(irc.is_user_event(line, "JOIN"))

    def test_a_topic_cannot_forge_a_part(self):
        line = ":attacker!u@h TOPIC #dccore-test :do not PART #dccore-test please"
        self.assertFalse(irc.is_user_event(line, "PART"))

    def test_the_motd_cannot_forge_one(self):
        """A server-sourced line has no nick!user@host prefix at all."""
        line = ":irc.undernet.org 372 DCCore :the motd mentions QUIT here"
        self.assertFalse(irc.is_user_event(line, "QUIT"))

    def test_a_prefixless_line_is_refused(self):
        self.assertFalse(irc.is_user_event("QUIT :bye", "QUIT"))

    def test_a_server_sourced_line_is_not_a_user_event(self):
        """These handlers key off who the event came from, so demand nick!user@host."""
        self.assertFalse(irc.is_user_event(":irc.undernet.org QUIT :netsplit", "QUIT"))
        self.assertFalse(irc.is_user_event(":services.undernet.org PART #c", "PART"))

    def test_a_command_must_not_merely_start_the_word(self):
        """QUITTING is not QUIT; (\\s|$) is what stops the prefix match."""
        line = ":dave!u@h QUITTING #dccore-test"
        self.assertFalse(irc.is_user_event(line, "QUIT"))


class EventSourceNickReadsThePrefix(unittest.TestCase):
    """The old code searched the whole line for ":<nick>!", body included."""

    def test_the_nick_comes_from_the_prefix(self):
        self.assertEqual(irc.event_source_nick(":dave!~u@h QUIT :bye"), "dave")

    def test_a_quit_reason_cannot_impersonate_the_bot(self):
        """`":DCCore!" in line.lower()` was true here, and drove nick recovery."""
        line = ":attacker!u@h QUIT :bye :DCCore!x@y"
        self.assertEqual(irc.event_source_nick(line), "attacker")

    def test_the_result_is_lowercased(self):
        self.assertEqual(irc.event_source_nick(":DCCore!bot@h QUIT :restarting"), "dccore")

    def test_a_line_with_no_user_prefix_gives_none(self):
        self.assertIsNone(irc.event_source_nick("PING :cookie"))
        self.assertIsNone(irc.event_source_nick(":irc.undernet.org 001 DCCore :Welcome"))

    def test_a_hostmask_in_the_body_is_not_the_source(self):
        """Anchoring at the start is what separates the prefix from the text.

        Searching instead of anchoring would read ``dave`` out of the ban notice
        below - a nick the line is ABOUT, not the one it came from. Today every
        caller has already passed is_user_event, so nothing reaches this with a
        server prefix; the assertion keeps the helper honest for the next one.
        """
        for line in (":irc.undernet.org 372 DCCore :please mail admin!host.com for help",
                     ":irc.undernet.org NOTICE DCCore :Ban set on :dave!*@*"):
            with self.subTest(line=line):
                self.assertIsNone(irc.event_source_nick(line))


def _guard_condition_for(marker, helper=("is_user_event", "is_server_numeric",
                                         "event_source_nick")):
    """Return the guard condition gating the line that holds `marker`.

    Walks OUTWARDS from the marker through the enclosing blocks and returns the
    first ``if``/``elif`` whose condition mentions one of `helper`, so inner
    bookkeeping tests like ``if quit_match:`` are stepped over. Narrowing
    `helper` picks a specific level of a nested handler - the nick-recovery
    block has an outer is_user_event guard and an inner event_source_nick one,
    and both are worth testing separately.

    The marker must match exactly one non-comment line. A marker that matches
    two is a test quietly checking whichever handler came first; a marker that
    matches a comment is a test satisfied by prose, which is how the first draft
    of the sibling suite fooled itself.
    """
    with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
        lines = handle.read().split("\n")

    hits = [i for i, raw in enumerate(lines)
            if marker in raw and not raw.strip().startswith("#")]
    if len(hits) != 1:
        raise AssertionError(
            f"marker {marker!r} matched {len(hits)} lines in irc.py; it must match exactly one")

    index = hits[0]
    indent = len(lines[index]) - len(lines[index].lstrip())
    for previous in range(index - 1, -1, -1):
        raw = lines[previous]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        outer = len(raw) - len(raw.lstrip())
        if outer >= indent:
            continue
        indent = outer
        for keyword in ("if ", "elif "):
            if (stripped.startswith(keyword) and stripped.endswith(":")
                    and any(name in stripped for name in helper)):
                return stripped[len(keyword):-1].strip()
    raise AssertionError(
        f"no guard mentioning {helper} encloses the line holding {marker!r}")


def _evaluate(condition, line, **names):
    """Evaluate a condition lifted from irc.py against a given raw line."""
    import defaults as config
    import re
    namespace = {"line": line, "irc": irc, "config": config, "re": re,
                 "is_user_event": irc.is_user_event,
                 "is_server_numeric": irc.is_server_numeric,
                 "event_source_nick": irc.event_source_nick}
    namespace.update(names)
    return bool(eval(condition, namespace))  # noqa: S307 - source is our own file


class GuardsAreWiredToTheRightHandlers(unittest.TestCase):
    """Evaluate the real conditions, so a swapped or inverted guard fails."""

    # --- the three membership handlers -----------------------------------

    def test_the_quit_handler_is_gated_on_quit(self):
        condition = _guard_condition_for("q_user = quit_match.group(1).lower()")
        self.assertTrue(_evaluate(condition, GENUINE["QUIT"]), condition)
        self.assertFalse(
            _evaluate(condition, ":dave!u@h PRIVMSG #dccore-test :@find QUIT PLAYING GAMES"),
            condition)

    def test_the_quit_handler_does_not_fire_on_a_part(self):
        """Catches the QUIT and PART guards being swapped."""
        condition = _guard_condition_for("q_user = quit_match.group(1).lower()")
        self.assertFalse(_evaluate(condition, GENUINE["PART"]), condition)

    def test_the_part_handler_is_gated_on_part(self):
        condition = _guard_condition_for("p_user = part_match.group(1).lower()")
        self.assertTrue(_evaluate(condition, GENUINE["PART"]), condition)
        self.assertFalse(_evaluate(condition, GENUINE["QUIT"]), condition)
        self.assertFalse(
            _evaluate(condition, ":attacker!u@h PRIVMSG #c :should i PART #c ?"), condition)

    def test_the_join_handler_is_gated_on_join(self):
        condition = _guard_condition_for("joined_user = join_match.group(1)")
        self.assertTrue(_evaluate(condition, GENUINE["JOIN"]), condition)
        self.assertTrue(_evaluate(condition, GENUINE["JOIN_COLON"]), condition)
        self.assertFalse(_evaluate(condition, GENUINE["PART"]), condition)
        self.assertFalse(
            _evaluate(condition, ":attacker!u@h PRIVMSG #c :hello JOIN #c"), condition)

    def test_the_join_handler_still_ignores_the_bots_own_join(self):
        """Losing that half would make the bot thaw queues off its own JOIN."""
        import defaults as config
        condition = _guard_condition_for("joined_user = join_match.group(1)")
        own = f":{config.NICKNAME}!bot@host JOIN #dccore-test"
        self.assertFalse(_evaluate(condition, own), condition)

    # --- the nick handlers ------------------------------------------------

    def test_the_nick_change_handler_is_gated_on_nick(self):
        condition = _guard_condition_for("old_nick = nick_match.group(1).lower()")
        self.assertTrue(_evaluate(condition, GENUINE["NICK"]), condition)
        self.assertTrue(_evaluate(condition, GENUINE["NICK_BARE"]), condition)
        self.assertFalse(
            _evaluate(condition, ":attacker!u@h PRIVMSG #c :hey NICK :victim"), condition)

    def test_nick_recovery_accepts_quit_and_part_independently(self):
        """Catches an or/and slip, which would disable recovery entirely."""
        condition = _guard_condition_for("main nick {main_nick} logged out",
                                         helper=("is_user_event",))
        for key in ("QUIT", "PART"):
            with self.subTest(event=key):
                self.assertTrue(_evaluate(condition, GENUINE[key]), condition)

    def test_nick_recovery_ignores_channel_chatter(self):
        condition = _guard_condition_for("main nick {main_nick} logged out",
                                         helper=("is_user_event",))
        self.assertFalse(
            _evaluate(condition, ":attacker!u@h PRIVMSG #c :i will QUIT now"), condition)

    def test_nick_recovery_identifies_the_quitter_by_prefix(self):
        """The inner guard: a QUIT reason must not be able to name the bot."""
        condition = _guard_condition_for("main nick {main_nick} logged out",
                                         helper=("event_source_nick",))
        real = ":DCCore!bot@host QUIT :Quit: leaving"
        self.assertTrue(_evaluate(condition, real, main_nick="DCCore"), condition)
        forged = ":attacker!u@h QUIT :bye :DCCore!x@y"
        self.assertFalse(_evaluate(condition, forged, main_nick="DCCore"), condition)

    def test_the_433_handler_accepts_433_and_432_independently(self):
        """432 is ERR_ERRONEUSNICKNAME - previously matched by English wording."""
        condition = _guard_condition_for("[LIVE NICK COLLISION]")
        for key in ("433", "432"):
            with self.subTest(numeric=key):
                self.assertTrue(_evaluate(condition, GENUINE[key]), condition)

    def test_the_433_handler_refuses_a_forged_part_reason(self):
        """The old PRIVMSG/NOTICE exclusion did not cover PART or QUIT reasons."""
        condition = _guard_condition_for("[LIVE NICK COLLISION]")
        for forged in (":attacker!u@h PART #dccore-test :bye 433 all",
                       ":attacker!u@h QUIT :erroneous nickname lol",
                       ":attacker!u@h TOPIC #dccore-test :read 433 rules"):
            with self.subTest(line=forged):
                self.assertFalse(_evaluate(condition, forged), condition)

    def test_a_genuine_433_mentioning_privmsg_is_still_accepted(self):
        """The dropped exclusion was not merely dead - it could reject a real one."""
        condition = _guard_condition_for("[LIVE NICK COLLISION]")
        line = ":irc.undernet.org 433 * DCCore :Nickname PRIVMSG is already in use"
        self.assertTrue(_evaluate(condition, line), condition)


class ListTriggerIsCaseInsensitive(unittest.TestCase):
    """The flood gate metered !list case-insensitively; dispatch did not.

    irc.py's gate has always used ``msg_lower in ("!list", "!debugnames",
    "!ping")``, and the !ping and !debugnames handlers next to it compare with
    ``.lower()`` - only !list compared the raw message. So "!LIST" was charged
    against the sender's flood budget and then silently did nothing.
    """

    def test_the_list_trigger_accepts_any_casing(self):
        condition = _guard_condition_for("list.send_list_trigger_info(s, user)",
                                         helper=("msg_lower", "msg =="))
        for typed in ("!list", "!LIST", "!List"):
            with self.subTest(typed=typed):
                self.assertTrue(
                    _evaluate(condition, "", msg=typed, msg_lower=typed.lower()), condition)

    def test_the_dispatch_compares_the_lowercased_message(self):
        """The gate itself is already asserted by tests/test_irc_dispatch.py."""
        condition = _guard_condition_for("list.send_list_trigger_info(s, user)",
                                         helper=("msg_lower", "msg =="))
        self.assertNotIn("msg ==", condition,
                         "!list must compare msg_lower, like !ping and !debugnames")


class NoBareSubstringTestsRemain(unittest.TestCase):
    """The handlers must call the guards, not keep their own substring tests."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "irc.py"), encoding="utf-8") as handle:
            raw = handle.read()
        # Assert on CODE only. The fix's own comments quote the old tests to
        # explain why they were wrong, and a naive text search cannot tell an
        # explanation from live code.
        self.source = self._code_only(raw)

    @staticmethod
    def _code_only(raw):
        code_lines = []
        in_docstring = False
        for line in raw.split("\n"):
            stripped = line.strip()
            fence_count = stripped.count('"""') + stripped.count("'''")
            if in_docstring:
                if fence_count:
                    in_docstring = False
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if fence_count < 2:
                    in_docstring = True
                continue
            if stripped.startswith("#"):
                continue
            code_lines.append(line.split("  #")[0])
        return "\n".join(code_lines)

    # assertFalse rather than assertNotIn throughout: on failure assertNotIn
    # prints the whole container, and the container here is all of irc.py.

    def test_no_bare_substring_event_tests_remain_in_the_read_loop(self):
        for stale in ('" JOIN " in line', '" PART " in line', '" QUIT " in line',
                      '" 433 " in line', '"erroneous nickname" in line'):
            with self.subTest(stale=stale):
                self.assertFalse(stale in self.source,
                                 f"{stale} is forgeable by any user in a channel")

    def test_the_nick_regex_no_longer_floats(self):
        self.assertFalse('r"^:([^!]+)!.* NICK :(.+)$"' in self.source,
                         "'.* NICK :' matches the command anywhere in the line")

    def test_the_recovery_no_longer_searches_the_whole_line_for_the_nick(self):
        """The DEBUG_MODE line legitimately lowercases `line`; this one did not."""
        self.assertFalse('f":{main_nick.lower()}!" in line.lower()' in self.source,
                         "a QUIT reason is free text; read the nick from the prefix")

    def test_each_handler_calls_a_guard(self):
        for command in ("JOIN", "PART", "QUIT", "NICK"):
            with self.subTest(command=command):
                self.assertTrue(f'is_user_event(line, "{command}")' in self.source,
                                f"the {command} handler is not calling is_user_event")
        for code in ("433", "432"):
            with self.subTest(code=code):
                self.assertTrue(f'is_server_numeric(line, "{code}")' in self.source,
                                f"the {code} handler is not calling is_server_numeric")


if __name__ == "__main__":
    unittest.main()
