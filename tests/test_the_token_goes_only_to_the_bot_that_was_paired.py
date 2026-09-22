"""#585: the script sent its stored token to whoever held the bot's nick.

Auto-reconnect dials the bot's nick on every JOIN, every IRC connect and every
two minutes while it is away, and on the first `Enter Your Password:` the script
transmitted the token. Anyone who takes the nick while the bot is offline
(Undernet does not own nicks) and accepts the DCC chat received it in clear,
without anything happening on the operator's side. (The bot's ADMIN_HOSTMASKS
gate limits what the token is worth to that person, but the credential the
script promises to keep private has left the machine.)

The script now compares the host the nick has NOW with the one the bot had when
the token was stored, and sends the token only if they match. mIRC is not
available here, so this reads the script and pins the shape of the check, the
order (the check comes before the token goes out) and every branch of it.

#608 is the other half: with no token to send (a password-only install, after
`unpair`, or while pairing with the bot away) the same automatic dial put "Type
the admin password here" in front of the operator, for whoever held the nick.
The prompt is now taken up only in a chat the operator opened (`/dccore connect`
or `pair` - their own act) or, in one the script dialled by itself, from the
host the bot is known to have.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def alias_body(name):
    text = script()
    start = text.index("alias " + name + " {")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(name)


class TheTokenIsChecked(unittest.TestCase):

    def prompt_branch(self):
        text = script()
        start = text.index("if (%text == Enter Your Password:) {")
        return text[start:text.index("hadd dccore.live state password", start)]

    def test_the_peer_is_checked_before_the_token_is_sent(self):
        branch = self.prompt_branch()
        self.assertIn("if (!$dccore.peerok) { return }", branch)
        self.assertLess(branch.index("$dccore.peerok"), branch.index("dccore.send $dccore.opt(token)"))
        self.assertLess(branch.index("$dccore.peerok"), branch.index("hadd dccore.live tokentried 1"))

    def test_the_prompt_itself_is_not_gated(self):
        """Typing the password into a chat the operator opened is their own act."""
        text = script()
        after_prompt = text[text.index("hadd dccore.live state password"):][:400]
        self.assertNotIn("peerok", after_prompt)

    def test_a_different_host_is_refused_and_says_who(self):
        body = alias_body("dccore.peerok")
        self.assertIn("if (%now == %known) { return $true }", body)
        refused = body[body.index("if (%now == %known)"):]
        self.assertIn("NOT %what $+ :", refused)
        self.assertIn("Someone else may hold its nick", refused)
        self.assertIn("dccore.abandon", refused)
        self.assertIn("return $false", refused)

    def test_nothing_stored_and_nothing_known_sends_nothing(self):
        body = alias_body("dccore.peerok")
        unknown = body[body.index("if (%known == $null) {"):body.index("dccore.set bothost %now")]
        self.assertIn("if (%now == $null) {", unknown)
        self.assertIn("return $false", unknown)
        self.assertIn("dccore.abandon", unknown)

    def test_nothing_stored_but_a_host_known_learns_it_and_sends(self):
        body = alias_body("dccore.peerok")
        learn = body[body.index("dccore.set bothost %now"):]
        self.assertIn("return $true", learn.split("}")[0] + "}")

    def test_the_host_is_the_nicks_host_not_its_ident_or_nick(self):
        """Type 2 is *!*@host: nick and ident are the parts anyone can choose."""
        body = alias_body("dccore.peerok")
        self.assertIn("$address($dccore.bot,2)", body)

    def test_refusing_stops_the_automatic_redial(self):
        body = alias_body("dccore.abandon")
        self.assertIn("dccore.set wantopen 0", body)
        self.assertIn("dccore.timers.off", body)
        self.assertIn("window -c $+(=,$dccore.bot)", body)


class AnAutomaticDialAsksNoStrangerForThePassword(unittest.TestCase):
    """#608: the half #585 left - the prompt in a chat the script dialled itself."""

    def password_fallthrough(self):
        """From the token branch's end to the prompt: what runs with no token."""
        text = script()
        start = text.index("dccore.send $dccore.opt(token)")
        return text[start:text.index("hadd dccore.live state password", start)]

    def test_the_operators_own_commands_dial_by_hand(self):
        text = script()
        for command in ("connect", "pair"):
            block = text[text.index("if (%cmd == " + command + ") {"):]
            block = block[:block.index("\n  }")]
            self.assertIn("dccore.connect byhand", block, command)

    def test_the_timers_dial_without_it(self):
        """The retry, the JOIN of the bot's nick and an IRC connect are not the operator."""
        text = script()
        self.assertEqual(text.count("dccore.connect byhand"), 2)
        self.assertEqual(len(re.findall(r"timerdccoreRetry 1 \S+ dccore\.connect\n", text)), 3)

    def test_connect_remembers_who_dialled(self):
        body = alias_body("dccore.connect")
        self.assertIn("hadd dccore.live byhand $iif($1 == byhand,1,0)", body)
        self.assertLess(body.index("hadd dccore.live byhand"), body.index("dcc chat $dccore.bot"))

    def test_a_chat_the_script_dialled_checks_the_peer_before_the_prompt(self):
        fallthrough = self.password_fallthrough()
        self.assertIn("if (!$dccore.st(byhand)) {", fallthrough)
        self.assertIn("if (!$dccore.peerok password) { return }", fallthrough)

    def test_the_check_is_nested_so_a_hand_dial_never_runs_it(self):
        """mIRC evaluates every identifier on an if-line, and peerok closes the
        chat when it refuses: on one line with && the operator's own chat would
        be checked (and possibly closed) too."""
        fallthrough = self.password_fallthrough()
        self.assertNotIn("&& (!$dccore.peerok", fallthrough)
        self.assertIn("if (!$dccore.st(byhand)) {", fallthrough)
        guard = fallthrough[fallthrough.index("if (!$dccore.st(byhand)) {"):]
        first_line = guard.split("\n")[0]
        self.assertNotIn("peerok", first_line)
        self.assertIn("\n    }", guard)
        inner = guard[len(first_line):guard.index("\n    }")]
        self.assertIn("if (!$dccore.peerok password) { return }", inner)

    def test_peerok_says_what_it_withholds(self):
        body = alias_body("dccore.peerok")
        self.assertIn("var %what = $iif($1 == password,asking for the password,sending the token)", body)
        self.assertIn("Not %what yet:", body)
        self.assertIn("NOT %what $+ :", body)


class TheHostIsKept(unittest.TestCase):

    def test_pairing_stores_the_host(self):
        text = script()
        block = text[text.index("if (%type == TOKEN) {"):]
        block = block[:block.index("\n  }")]
        self.assertIn("dccore.set bothost $address($dccore.bot,2)", block)

    def test_unpairing_forgets_it(self):
        text = script()
        block = text[text.index("if (%cmd == unpair) {"):]
        block = block[:block.index("\n  }")]
        self.assertIn("dccore.forget bothost", block)

    def test_trust_takes_the_current_host_and_needs_one(self):
        text = script()
        block = text[text.index("if (%cmd == trust) {"):]
        block = block[:block.index("\n  }")]
        self.assertIn("if (%now == $null)", block)
        self.assertIn("dccore.set bothost %now", block)

    def test_the_help_lists_trust_and_so_do_the_docs(self):
        self.assertIn("/dccore trust", script())
        with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
            docs = handle.read()
        self.assertIn("/dccore trust", docs)
        self.assertIn("only sends the token to the bot it paired with", docs)


if __name__ == "__main__":
    unittest.main()
