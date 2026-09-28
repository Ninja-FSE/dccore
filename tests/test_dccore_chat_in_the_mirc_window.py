"""#371: DCCore Chat in dccore.mrc - the window, relayed by the bot.

mIRC cannot run here, so these read the script, statement by statement, the
way the script's other tests do. The bot does the checking
(tests/test_servers_chat_is_relayed_by_the_bot.py); the script is held to
what is left for it:

- it only ever sends what the operator typed, and only through the bot;
- nothing in the message handler, or in anything drawing a CHAT line, sends
  anything (RFC 2812);
- a raw tagged message is hidden only while the relay is up and would draw
  it - otherwise it shows in the channel as usual, so nothing is lost;
- a line is drawn once, stripped, only for channels the operator chose;
- it says it is public;
- a menu row never puts a channel name into a command.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

# A command that puts something on the wire, at the start of a statement.
SENDS = re.compile(r"(?:^|[{|]\s*)\.?(?:notice|msg|ctcp|describe|say|raw|quote|amsg|ame|"
                   r"dccore\.send|dccore\.chat\.say)\b", re.IGNORECASE)


def script():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def block(text, header):
    """The body of the block that `header` opens, braces balanced."""
    start = text.index(header)
    at = text.index("{", start + len(header) - 1)
    depth = 0
    for i in range(at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[at + 1:i]
    raise AssertionError("unbalanced block: " + header)


def statements(body):
    return [line.strip() for line in body.split("\n")
            if line.strip() and not line.strip().startswith(";")]


class TheTag(unittest.TestCase):
    def test_it_is_neutral_and_built_without_evaluation_brackets(self):
        self.assertIn("alias dccore.chat.tag { return $+($chr(91),ServersChat,$chr(93)) }", script())

    def test_it_is_the_first_word_and_the_first_check(self):
        body = statements(block(script(), "on ^*:TEXT:*:#:"))
        self.assertEqual(body[0], "if ($1 != $dccore.chat.tag) { return }")


class ARawNoticeIsHiddenOnlyWhenItWillBeDrawn(unittest.TestCase):
    def setUp(self):
        self.handler = statements(block(script(), "on ^*:TEXT:*:#:"))

    def test_every_condition_comes_before_haltdef(self):
        halt = self.handler.index("haltdef")
        for condition in ("if ($2 == $null) { return }",
                          "if (!$dccore.chat.relaying) { return }",
                          "if (!$dccore.here) { return }",
                          "if (!$istok($dccore.st(chat.chans),$chan,32)) { return }",
                          "if ($nick != $dccore.bot) && (!$istok($hget(dccore.chatpeers,$lower($chan)),$nick,32)) { return }",
                          "if (!$dccore.chat.listens($chan)) { return }",
                          "if (!$window($dccore.chat.win)) && (!$dccore.opt(chat.popup)) { return }"):
            self.assertLess(self.handler.index(condition), halt, condition)
        self.assertEqual(self.handler[-1], "haltdef", "it hides, and draws nothing itself")

    def test_the_relay_is_up_only_when_connected_and_structured(self):
        self.assertIn("alias dccore.chat.relaying { return $iif(($dccore.st(state) == in) && "
                      "($dccore.st(mode) == structured),$true,$false) }", script())

    def test_only_a_line_the_bot_will_actually_relay_is_hidden(self):
        """#982 audit finding 1: the tag, relay state, channel and listen
        settings do not say whether the bot's capture() will actually treat
        the line as chat - a person, a non-DCCore script (the tag is
        neutral on purpose), or a DCCore bot WHO has not found yet all say
        the same tagged text, and none of it ever arrives as a CHAT line.
        Hiding it anyway made it vanish for the operator while everyone
        else in the channel still read it. Checked against the same peer
        list the side-listbox is built from, so only a sender the bot
        already knows is a DCCore bot is ever hidden."""
        self.assertIn("$hget(dccore.chatpeers,$lower($chan))", "\n".join(self.handler))

    def test_an_empty_tagged_line_is_never_hidden_either(self):
        """A bare tag with nothing after it strips to nothing and capture()
        drops it - so it never arrives as a CHAT line no matter who sent
        it, and hiding it would be the same silent loss."""
        empty_check = self.handler.index("if ($2 == $null) { return }")
        halt = self.handler.index("haltdef")
        self.assertLess(empty_check, halt)

    def test_the_bots_own_relayed_line_is_hidden_too(self):
        """Seen live, right after the #982 audit fix shipped: the bot's own
        line - the operator's own text, or another peer's, said back to the
        channel - was no longer hidden, because dccore.chatpeers never
        contains the bot's own nick (peers are OTHER DCCore bots by
        definition - see serverschat.note_who_reply()'s self-exclusion).
        $nick == $dccore.bot has to short-circuit the peer check, the same
        nick comparison the reconnect-detection hooks already use."""
        joined = "\n".join(self.handler)
        self.assertIn("$nick != $dccore.bot", joined)
        peer_check = self.handler.index(
            "if ($nick != $dccore.bot) && (!$istok($hget(dccore.chatpeers,$lower($chan)),$nick,32)) { return }")
        self.assertLess(peer_check, self.handler.index("haltdef"))


class NothingAnswers(unittest.TestCase):
    """RFC 2812 - checked in the handler AND in every alias drawing a line,
    since a send one call away is still an automatic reply."""

    REACHED = ["on ^*:TEXT:*:#:", "alias dccore.chat.feed", "alias dccore.chat.channels",
               "alias dccore.chat.listens", "alias dccore.chat.show", "alias dccore.chat.window",
               "alias dccore.chat.sys", "alias dccore.chat.title"]

    def test_no_send_anywhere_it_reaches(self):
        text = script()
        for header in self.REACHED:
            for line in statements(block(text, header)):
                self.assertIsNone(SENDS.search(line), f"{header}: {line}")

    def test_the_guard_does_see_a_send(self):
        """So the test above cannot pass by matching nothing."""
        self.assertIsNotNone(SENDS.search(".notice $nick hello"))
        self.assertIsNotNone(SENDS.search("if (x) { msg $chan hi }"))
        self.assertIsNotNone(SENDS.search("dccore.send chat %to %text"))


class WhatComesFromTheBot(unittest.TestCase):
    def setUp(self):
        self.text = script()
        self.feed = statements(block(self.text, "alias dccore.chat.feed"))

    def test_both_lines_are_read(self):
        self.assertIn("if (%type == CHAT) { dccore.chat.feed $2- | return }", self.text)
        self.assertIn("if (%type == CHANNELS) { dccore.chat.channels $2- | return }", self.text)

    def test_a_line_is_drawn_once(self):
        """The bot replays its recent lines after every HELLO."""
        self.assertIn("if ($dccore.st(chat.last) isnum) && ($1 <= $dccore.st(chat.last)) { return }",
                      self.feed)
        self.assertIn("hadd dccore.live chat.last $1", self.feed)

    def test_only_listened_channels_and_stripped(self):
        self.assertIn("if ($3 != $dccore.bot) && ($left($2,1) != @) && (!$dccore.chat.listens($2)) { return }",
                      self.feed)
        self.assertTrue(all("$strip($4-)" in s for s in self.feed if s.startswith("dccore.chat.show")))

    def test_it_listens_everywhere_by_default(self):
        """Only lines from other DCCore bots arrive at all, so there is little
        to filter - and with nothing ticked, nothing would ever show."""
        self.assertIn("dccore.default chat.all 1", self.text)

    def test_your_own_line_always_shows(self):
        """One said with `chat *` comes back with "-" for its channel; it must
        not be filtered out as an unlistened channel, and it reads as "*"."""
        listen = self.feed.index("if ($3 != $dccore.bot) && ($left($2,1) != @) && (!$dccore.chat.listens($2)) "
                                  "{ return }")
        self.assertLess(listen, next(i for i, s in enumerate(self.feed) if s.startswith("dccore.chat.show")))
        self.assertTrue(any(s.startswith("dccore.chat.show $iif($2 == -,*,$2) ") for s in self.feed))

    def test_a_private_line_always_shows_too(self):
        """A private line's "@<nick>" channel was never something to tick in
        the Listen on menu, so it must not be filtered out either (#371
        follow-up)."""
        listen = self.feed.index("if ($3 != $dccore.bot) && ($left($2,1) != @) && (!$dccore.chat.listens($2)) "
                                  "{ return }")
        self.assertLess(listen, next(i for i, s in enumerate(self.feed) if s.startswith("dccore.chat.show")))

    def test_its_own_lines_are_marked_as_its_own(self):
        self.assertTrue(any("$iif($3 == $dccore.bot,own,other)" in s for s in self.feed))

    def test_somebody_else_s_line_names_where_to_reply(self):
        """Typing with no channel picked replies where the conversation is,
        not to wherever a set-cover happens to land (#958 follow-up)."""
        self.assertIn('if ($3 != $dccore.bot) && ($2 != $null) && ($2 != -) '
                      '{ hadd dccore.live chat.replyto $iif($left($2,1) == @,$mid($2,2-),$2) | dccore.chat.title }',
                      self.feed)

    def test_the_title_follows_the_reply_target_the_moment_it_changes(self):
        """Seen live: dccore.chat.title only ran on login, a send-target
        pick, or a connection-state change - never when an incoming line
        moved the auto-reply target to a different channel or peer - so it
        could go on naming the PREVIOUS one for the rest of the session."""
        record = [s for s in self.feed if "chat.replyto" in s][0]
        self.assertIn("dccore.chat.title", record)

    def test_a_private_line_names_the_peer_to_reply_to_not_the_at_sign(self):
        """A private line's channel is "@<nick>" - replying there means
        privately to that nick, so the "@" itself must not end up in
        chat.replyto (#371 follow-up)."""
        record = [s for s in self.feed if "chat.replyto" in s][0]
        self.assertIn("$iif($left($2,1) == @,$mid($2,2-),$2)", record)

    def test_never_recorded_from_a_fan_out_own_line(self):
        """"-" and "*" are only ever an OWN line's channel (a `chat *` fan-out
        and its "somebody was hidden" remark); neither is a real channel to
        reply into."""
        record = [s for s in self.feed if "chat.replyto" in s][0]
        self.assertIn("($2 != -)", record)
        # $3 != $dccore.bot already excludes the own-line "-"/"*" cases,
        # since those only ever arrive with $3 == $dccore.bot or $3 == *.
        self.assertIn("$3 != $dccore.bot", record)


class WhatIsSent(unittest.TestCase):
    def setUp(self):
        self.say = statements(block(script(), "alias dccore.chat.say"))

    def test_through_the_bot_only(self):
        self.assertEqual(self.say[-1], "dccore.send chat %to %text")
        self.assertFalse([s for s in self.say if re.search(r"(^|[\s|{])\.?(notice|msg)\b", s)],
                         "never straight from this client")

    def test_stripped_and_only_when_the_relay_is_up(self):
        send = self.say.index("dccore.send chat %to %text")
        self.assertLess(self.say.index("var %text = $strip($1-)"), send)
        self.assertLess(next(i for i, s in enumerate(self.say) if s.startswith("if (!$dccore.chat.relaying)")), send)
        self.assertLess(next(i for i, s in enumerate(self.say) if s.startswith("if (%to == $null)")), send)

    def test_auto_mode_replies_where_the_conversation_is(self):
        """No channel picked (or "every channel" picked): reply to the
        channel the last line from somebody else arrived on, and only
        broadcast to reach everyone when nobody has said anything back yet."""
        line = [s for s in self.say if s.startswith("if (%to == $null) || (%to == *)")][0]
        self.assertIn("$dccore.st(chat.replyto)", line)
        self.assertIn(",*)", line, "falls back to * with nothing heard yet")
        self.assertLess(self.say.index(line), self.say.index("dccore.send chat %to %text"))

    def test_typing_in_the_window_sends(self):
        body = statements(block(script(), "on *:INPUT:@DCCore-Chat:"))
        self.assertIn("dccore.chat.say $1-", body)
        self.assertEqual(body[-1], "halt")


class ItSaysItIsPublic(unittest.TestCase):
    def test_the_title_and_the_first_lines(self):
        text = script()
        title = statements(block(text, "alias dccore.chat.title"))
        self.assertTrue(any("DCCore Chat $dccore.dot public" in s for s in title))
        self.assertIn("dccore.chat.sys Public: everyone in the channel reads what is typed here", text)
        self.assertIn('box "DCCore Chat (public)", 600,', text)

    def test_an_arriving_line_does_not_take_the_focus(self):
        body = statements(block(script(), "alias dccore.chat.window"))
        self.assertIn("if ($1 == quiet) { window -enl16 $dccore.chat.win }", body)
        self.assertEqual(statements(block(script(), "alias dccore.chat.show"))[0],
                         "dccore.chat.window quiet")


class TheTitleFollowsEveryRelayStateChange(unittest.TestCase):
    """#982 audit finding 3: dccore.chat.title only ran when the chat window
    was created or its send target changed, never on HELLO, CHATCLOSE, or
    giving up on the connection - so it could say "not connected to the
    bot" for a whole session after the window was opened before HELLO
    landed, or keep claiming a live relay after the console closed."""

    def test_after_hello(self):
        text = script()
        hello = text.index("if (%type == HELLO) {")
        next_branch = text.index("if (%type == STATUS)", hello)
        self.assertIn("dccore.chat.title", text[hello:next_branch])

    def test_after_entering_the_admin_chat(self):
        text = script()
        entered = text.index("if (%text == Entering DCC Chat Admin Interface) {")
        self.assertIn("dccore.chat.title", text[entered:text.index("\n  }", entered)])

    def test_on_chatclose(self):
        body = "\n".join(statements(block(script(), "on *:CHATCLOSE:")))
        self.assertIn("dccore.chat.title", body)

    def test_giving_up_on_the_connection(self):
        body = "\n".join(statements(block(script(), "alias dccore.abandon")))
        self.assertIn("dccore.chat.title", body)

    def test_switching_to_plain_mode(self):
        body = "\n".join(statements(block(script(), "alias dccore.plain")))
        self.assertIn("dccore.chat.title", body)


class TheWindowIsTheInterface(unittest.TestCase):
    def test_its_menu_picks_from_the_bot_s_channels(self):
        text = script()
        menu = block(text, "menu @DCCore-Chat")
        self.assertIn(".$submenu($dccore.chat.sendrow($1))", menu)
        self.assertIn(".$submenu($dccore.chat.listenrow($1))", menu)
        self.assertIn("alias dccore.chat.chan { return $gettok($dccore.st(chat.chans),$1,32) }", text)

    def test_a_menu_row_runs_a_number_never_a_channel_name(self):
        """mIRC parses a row's command text on the click, and a channel name
        can hold | or $. The name may be the label; the command carries only
        the row number, resolved inside the alias."""
        text = script()
        for row in ("alias dccore.chat.sendrow", "alias dccore.chat.listenrow"):
            ret = [s for s in statements(block(text, row)) if s.startswith("return $iif(")][0]
            _label, command = ret.split(":", 1)
            self.assertNotIn("%c", command, f"{row}: {command}")
            self.assertTrue(command.endswith(" $1"), command)
        self.assertIn("alias dccore.chat.to.n { if ($1 isnum) && ($dccore.chat.chan($1) != $null) "
                      "{ dccore.chat.to $dccore.chat.chan($1) } }", text)

    def test_picking_where_to_send_also_listens_there(self):
        body = statements(block(script(), "alias dccore.chat.to"))
        self.assertIn("dccore.set chat.to $1", body)
        self.assertTrue(any("$addtok($dccore.opt(chat.listen),$1,32)" in s for s in body))

    def test_the_options_dialog_keeps_both_boxes(self):
        text = script()
        init = block(text, "on *:dialog:dccore.opt:init:0:")
        ok = block(text, "on *:dialog:dccore.opt:sclick:1:")
        self.assertIn("if ($dccore.opt(chat.all)) { did -c dccore.opt 601 }", init)
        self.assertIn("hadd dccore chat.popup $did(dccore.opt,602).state", ok)

    def test_no_flood_table_of_its_own(self):
        """The bot limits what arrives; a second limit here would only hide
        lines the bot already decided to show."""
        self.assertNotIn("dccore.chatrate", script())

    def test_it_is_in_the_command_list_and_the_menus(self):
        text = script()
        self.assertIn("/dccore chat [text]", text)
        self.assertIn(".Open DCCore Chat:dccore chat", text)
        self.assertIn("alias dccore.ver { return 1.7 }", text)


class TheDefaultIsEveryChannelWithOtherDccoreBots(unittest.TestCase):
    """Typing in the window replies where the conversation is, or (nobody
    having said anything back yet) says it where the bot has seen another
    DCCore bot (`chat *`), unless one channel was picked from the menu."""

    def test_no_pick_means_reply_or_star(self):
        body = "\n".join(statements(block(script(), "alias dccore.chat.say")))
        self.assertIn("if (%to == $null) || (%to == *) { var %to = "
                      "$iif($dccore.st(chat.replyto) != $null,$dccore.st(chat.replyto),*) }", body)
        self.assertIn("dccore.send chat %to %text", body)

    def test_star_is_not_checked_against_the_channel_list(self):
        body = "\n".join(statements(block(script(), "alias dccore.chat.say")))
        self.assertIn("if (%to != *) && ($left(%to,1) isin #&+!) && ($dccore.st(chat.chans) != $null)", body)

    def test_a_nick_target_is_not_checked_against_the_channel_list_either(self):
        """A private target (#371 follow-up) is never one of chat.chans -
        the bot is the one that knows who it has actually seen, so the
        script must not refuse it itself."""
        body = "\n".join(statements(block(script(), "alias dccore.chat.say")))
        line = [s for s in body.split("\n") if "is not in" in s][0]
        self.assertIn("$left(%to,1) isin #&+!", line)

    def test_a_menu_row_goes_back_to_it(self):
        body = "\n".join(statements(block(script(), "alias dccore.chat.to.all")))
        self.assertIn("dccore.set chat.to *", body)
        self.assertIn("dccore.chat.to.all", script())

    def test_the_title_also_follows_the_reply_to_channel(self):
        title = "\n".join(statements(block(script(), "alias dccore.chat.title")))
        self.assertIn("$dccore.st(chat.replyto)", title)


class ThePeerList(unittest.TestCase):
    """The side-listbox of DCCore bots WHO has found (#371 follow-up)."""

    def test_the_window_has_a_side_listbox(self):
        body = statements(block(script(), "alias dccore.chat.window"))
        self.assertIn("if ($1 == quiet) { window -enl16 $dccore.chat.win }", body)
        self.assertTrue(any("window -el16 $dccore.chat.win" in s for s in body))

    def test_a_peers_line_is_dispatched(self):
        self.assertIn("if (%type == PEERS) { dccore.chat.peerline $2- | return }", script())

    def test_peerline_stores_per_channel_and_redraws(self):
        body = statements(block(script(), "alias dccore.chat.peerline"))
        self.assertTrue(any("hadd dccore.chatpeers $1 $2-" in s for s in body))
        self.assertTrue(any("hdel dccore.chatpeers $1" in s for s in body))
        self.assertEqual(body[-1], "dccore.chat.peers.redraw")

    def test_redraw_only_looks_at_the_bot_s_own_channels(self):
        """The merged sidebar is built from chat.chans, not by scanning every
        key dccore.chatpeers happens to hold - a channel the bot has left
        must not linger in the list."""
        body = "\n".join(statements(block(script(), "alias dccore.chat.peers.redraw")))
        self.assertIn("$gettok($dccore.st(chat.chans),%i,32)", body)
        self.assertIn("$hget(dccore.chatpeers,", body)

    def test_a_peer_seen_in_two_channels_is_not_doubled(self):
        """$addtok() itself never appends a token already in the list -
        documented behaviour, and not something to re-verify by hand with a
        second hash table, whose own create/free lifecycle was where the
        real bug turned out to be (seen live: three rows for one peer seen
        in three channels, with that version)."""
        body = "\n".join(statements(block(script(), "alias dccore.chat.peers.redraw")))
        self.assertIn("var %sorted = $addtok(%sorted,$gettok(%have,%j,32),32)", body)
        self.assertNotIn("chatpeers.seen", body)
        self.assertNotIn("istok", body)

    def test_redraw_clears_before_it_rebuilds(self):
        """dline -l <win> 1-N - one call, a hyphenated range - turned out to
        be a silent no-op on the side-listbox: rows a peer had already left
        stayed at their old count and only ever grew, redraw after redraw.
        One dline -l ... 1 per line, looped until the listbox reports 0,
        depends on nothing but a single-line deletion, which the peer-picker
        already proves this listbox honours."""
        body = statements(block(script(), "alias dccore.chat.peers.redraw"))
        clear = next(i for i, s in enumerate(body) if s.startswith("while ($line($dccore.chat.win,0,1) > 0)"))
        add = next(i for i, s in enumerate(body) if s.startswith("aline -l $dccore.chat.win"))
        self.assertLess(clear, add)
        self.assertIn("dline -l $dccore.chat.win 1 }", body[clear])
        self.assertNotIn("1-$line", body[clear])

    def test_the_hash_table_is_made_on_load(self):
        body = statements(block(script(), "alias dccore.init"))
        self.assertIn("if (!$hget(dccore.chatpeers)) { hmake dccore.chatpeers 32 }", body)


class MessagingAPeerPrivately(unittest.TestCase):
    """Double-click, or right-click, a nick in the side-listbox (#371
    follow-up)."""

    def test_dclick_is_a_built_in_event_placed_first(self):
        """Built-in mouse events have to sit above the custom items in the
        same menu block, or mIRC does not recognise them."""
        menu = statements(block(script(), "menu @DCCore-Chat"))
        self.assertEqual(menu[0], "dclick:dccore.chat.pickpeer $1")

    def test_dclick_and_the_menu_row_carry_a_line_number_never_the_nick(self):
        """The same reason a channel picker carries a row number, never the
        channel's name (#955 review): the side-listbox can hold a nick with
        a "|" in it - valid on IRC - and mIRC parses the command text on the
        click. $1 (dclick) and $sline(...).ln (the menu row) are both plain
        numbers; the nick itself is only ever read back with $line() INSIDE
        the alias, never re-parsed as a command."""
        menu = statements(block(script(), "menu @DCCore-Chat"))
        row = [s for s in menu if "Message this bot privately" in s][0]
        _label, command = row.split(":", 1)
        self.assertNotIn("$sline($dccore.chat.win,1) ", row, "the label must not carry the raw nick either")
        self.assertTrue(command.strip().endswith("$sline($dccore.chat.win,1).ln"))

    def test_pickpeer_only_accepts_a_number_and_resolves_it_itself(self):
        body = statements(block(script(), "alias dccore.chat.pickpeer"))
        self.assertEqual(body[0], "if ($1 !isnum) { return }")
        self.assertIn("var %nick = $line($dccore.chat.win,$1,1)", body)
        self.assertIn("dccore.set chat.to %nick", body)

    def test_the_title_says_privately_for_a_nick_target(self):
        title = "\n".join(statements(block(script(), "alias dccore.chat.title")))
        self.assertIn("$left(%to,1) !isin #&+!", title)
        self.assertIn("privately to", title)


if __name__ == "__main__":
    unittest.main()
