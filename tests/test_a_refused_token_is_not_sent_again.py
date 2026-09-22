"""#613: a refused stored token plus auto-reconnect burned the bot's three
attempts and blocked the operator's own address for 15 minutes.

When the bot answered the token with "Incorrect Password." the script only
changed its message and waited. The bot closes the unanswered session after
60 s, CHATCLOSE redialled (5 s, since every session's first line resets the
backoff), dccore.connect reset tokentried, and the revoked token went out
again. The bot counts refusals per address across sessions, so the third
session - about two minutes after the first, with the operator away - blocked
the address, and every login was refused for 15 minutes with no word why.

The script now marks the token bad on that refusal: it is not sent again and
the automatic redial waits for the operator (a login, or a new token) instead.
A password typed by hand and mistyped is not the token, so it is told apart.
mIRC is not available here: this reads the script and pins each branch.
"""

import contextlib
import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"), encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def block(opening):
    """The braces block that starts with `opening`, taken by brace depth."""
    text = script()
    start = text.index(opening)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError(opening)


def code(text):
    return "\n".join(line for line in text.split("\n") if not line.strip().startswith(";"))


class TheRefusalMarksTheToken(unittest.TestCase):

    def refusal(self):
        return block("if (%text == Incorrect Password.) {")

    def test_only_the_stored_token_is_marked_bad_not_a_mistyped_password(self):
        branch = code(self.refusal())
        self.assertIn("if (%state == auth) && (!$dccore.st(typed)) {", branch)
        token = block("if (%state == auth) && (!$dccore.st(typed)) {")
        self.assertIn("hadd dccore.live tokenbad 1", token)
        self.assertIn("hadd dccore.live state password", token)
        self.assertIn("return", token)
        typed = branch[branch.index("Wrong password."):]
        self.assertNotIn("tokenbad", typed)

    def test_the_operator_is_told_what_happens_and_about_the_block(self):
        token = block("if (%state == auth) && (!$dccore.st(typed)) {")
        self.assertIn("It is not sent again, and the script does not redial by itself", token)
        self.assertIn("type the password now, or /dccore pair again", token)
        self.assertIn("Three refusals block this address for 15 minutes", token)

    def test_a_typed_password_is_recorded_as_typed_and_a_dial_forgets_it(self):
        typed = block("on *:INPUT:@DCCore: {")
        typed = typed[typed.index("if ($dccore.st(state) == password) {"):]
        self.assertIn("hadd dccore.live state auth\n    hadd dccore.live typed 1\n    dccore.send $1-", typed)
        connect = block("alias dccore.connect {")
        self.assertIn("hadd dccore.live tokentried 0\n  hadd dccore.live typed 0", connect)


class ABadTokenIsNotSentAgain(unittest.TestCase):

    def prompt(self):
        return block("if (%text == Enter Your Password:) {")

    def test_the_token_goes_out_only_while_it_is_not_known_bad(self):
        prompt = self.prompt()
        self.assertIn(
            "if ($dccore.opt(token) != $null) && (!$dccore.st(tokentried)) && (!$dccore.st(tokenbad)) && (!$dccore.st(pairing)) {",
            prompt)
        self.assertLess(prompt.index("(!$dccore.st(tokenbad))"), prompt.index("dccore.send $dccore.opt(token)"))

    def test_the_prompt_says_why_the_token_was_not_used(self):
        prompt = self.prompt()
        hint = prompt[prompt.index("hadd dccore.live state password"):]
        self.assertIn("$iif($dccore.st(tokenbad),(The stored token was refused and is not sent again; /dccore pair replaces it.),(No token stored; /dccore pair keeps one.))", hint)


class TheRedialWaitsForTheOperator(unittest.TestCase):

    def test_retry_returns_before_arming_the_timer(self):
        retry = block("alias dccore.retry {")
        guard = "if ($dccore.st(tokenbad)) { dccore.sys Not redialing by itself: the stored token was refused. /dccore pair again, or /dccore connect and type the password. | return }"
        self.assertIn(guard, retry)
        self.assertLess(retry.index(guard), retry.index("hadd dccore.live tries"))
        self.assertLess(retry.index(guard), retry.index(".timerdccoreRetry 1 %delay dccore.connect"))

    def test_chatclose_still_goes_through_retry_so_the_guard_applies(self):
        close = block("on *:CHATCLOSE: {")
        self.assertIn("dccore.retry", close)
        self.assertNotIn(".timerdccoreRetry", close)


class TheMarkIsCleared(unittest.TestCase):

    def test_a_login_clears_it_and_says_the_token_is_still_the_refused_one(self):
        login = block("if (%text == Entering DCC Chat Admin Interface) {")
        self.assertIn("if ($dccore.st(tokenbad)) && (!$dccore.st(pairing)) { dccore.sys The stored token is still the one the bot refused: /dccore pair replaces it. }", login)
        self.assertIn("hdel dccore.live tokenbad", login)

    def test_a_new_token_clears_it(self):
        token = block("if (%type == TOKEN) {")
        self.assertIn("dccore.set token $3", token)
        self.assertIn("hdel dccore.live tokenbad", token)

    def test_unpairing_clears_it_with_the_token(self):
        unpair = block("if (%cmd == unpair) {")
        self.assertIn("dccore.forget token", unpair)
        self.assertIn("hdel dccore.live tokenbad", unpair)

    def test_connect_does_not_clear_it(self):
        """A dial the operator asks for prompts for the password; it does not
        spend another of the bot's three attempts on the refused token."""
        text = script()
        connect = text[text.index("if (%cmd == connect) {"):text.index("if (%cmd == pair) {")]
        self.assertNotIn("tokenbad", connect)
        self.assertNotIn("tokenbad", block("alias dccore.connect {"))


class TheBotsSideStillHoldsWhatTheScriptAssumes(unittest.TestCase):
    """The message and the reasoning rest on the bot's numbers."""

    def test_three_attempts_and_fifteen_minutes(self):
        import adminchat
        self.assertEqual(adminchat.MAX_PASSWORD_ATTEMPTS, 3)
        self.assertEqual(adminchat.BAD_IP_BLOCK_SECONDS, 900.0)

    def test_the_count_is_per_address_across_sessions(self):
        """A refusal in one session counts against the next: that is why one
        refusal per automatic redial reached the block."""
        import adminchat
        with adminchat._bad_lock:
            adminchat._bad_ips.pop("203.0.113.7", None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                adminchat.note_bad_ip("203.0.113.7")
                adminchat.note_bad_ip("203.0.113.7")
                self.assertFalse(adminchat.is_bad_ip("203.0.113.7"))
                adminchat.note_bad_ip("203.0.113.7")
            self.assertTrue(adminchat.is_bad_ip("203.0.113.7"))
        finally:
            adminchat.clear_bad_ip("203.0.113.7")


if __name__ == "__main__":
    unittest.main()
