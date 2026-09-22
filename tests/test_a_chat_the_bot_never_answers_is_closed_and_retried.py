"""#615: scripts/mirc/dccore.mrc, an offer the bot never answers.

dccore.retry was reached only from raw 401 and on CHATCLOSE. A bot that
gets the CTCP and says nothing (blocked address, unauthorised host, an
offer it could not parse, a listen-back offer the firewall dropped)
produces neither, and mIRC never times out its own outgoing chat, so the
=bot window sat at "Waiting for acknowledgement..." for ever, $chat()
stayed true and every later dccore.connect answered "already open".

CI has no mIRC, so this reads the source: dccore.connect must arm a
one-shot timer after `dcc chat`, the alias it fires must close the window
and retry only while the state is still `opening`, and the timer must be
cancelled wherever the chat comes up or is closed. Each assertion names
the statement, not a token: a timer that fires nothing, or an alias that
retries without closing the window, fails here.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")

BOT_WINDOW_CLOSE = "if ($chat($dccore.bot)) { window -c $+(=,$dccore.bot) }"


def script_lines():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n").split("\n")


def block(header):
    """The code lines of the top-level block whose header line starts
    with `header`, comments and blanks dropped, header and closing brace
    included."""
    lines = script_lines()
    for start, line in enumerate(lines):
        if line.startswith(header):
            break
    else:
        raise AssertionError("no block starts with %r" % header)
    depth = 0
    body = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        body.append(stripped)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return body
    raise AssertionError("block %r never closes" % header)


def one_shot_timer_after_dcc_chat(connect):
    """(timer name, seconds, alias) of the one-shot timer dccore.connect
    starts after its `dcc chat`, or None."""
    after = connect[connect.index("dcc chat $dccore.bot") + 1:]
    for line in after:
        found = re.match(r"^\.(timer\w+) 1 (\d+) (dccore\.\S+)$", line)
        if found:
            return found.group(1), int(found.group(2)), found.group(3)
    return None


class DccoreConnectArmsATimeout(unittest.TestCase):
    def test_a_one_shot_timer_is_started_after_the_offer(self):
        timer = one_shot_timer_after_dcc_chat(block("alias dccore.connect {"))
        self.assertIsNotNone(timer, "dccore.connect issues dcc chat and starts no timer: "
                                    "an unanswered offer is never noticed")

    def test_it_waits_long_enough_for_a_slow_answer_but_not_for_ever(self):
        _, seconds, _ = one_shot_timer_after_dcc_chat(block("alias dccore.connect {"))
        # a 401 or the bot's dial arrive in seconds; the bot's own listen
        # timeout is 60 s, so anything past that is a chat that will not come
        self.assertGreaterEqual(seconds, 60)
        self.assertLessEqual(seconds, 180)


class TheTimeoutAliasClosesTheWindowAndRetries(unittest.TestCase):
    def alias_body(self):
        _, _, alias = one_shot_timer_after_dcc_chat(block("alias dccore.connect {"))
        return block("alias %s {" % alias)

    def test_it_does_nothing_once_the_chat_came_up_or_was_retried(self):
        body = self.alias_body()
        self.assertEqual(body[1], "if ($dccore.st(state) != opening) { return }",
                         "the alias must leave a chat that is in, waiting or closed alone")

    def test_it_closes_the_window_mirc_left_waiting(self):
        self.assertIn(BOT_WINDOW_CLOSE, self.alias_body(),
                      "$chat() stays true until the =bot window is closed, and "
                      "dccore.connect refuses while it is")

    def test_it_then_retries_with_the_backoff_only_if_chatclose_did_not(self):
        body = self.alias_body()
        retry = [line for line in body if "dccore.retry" in line]
        self.assertEqual(len(retry), 1, body)
        self.assertTrue(retry[0].startswith("if ($dccore.st(state) == opening) {"),
                        "closing the window may fire CHATCLOSE, which retries by "
                        "itself; the alias must not retry a second time: %s" % retry[0])
        self.assertLess(body.index(BOT_WINDOW_CLOSE), body.index(retry[0]),
                        "the window must be closed before the retry, or the retried "
                        "connect finds it 'already open'")

    def test_it_tells_the_operator(self):
        body = self.alias_body()
        self.assertTrue(any(line.startswith("dccore.sys ") and "$dccore.bot" in line
                            for line in body), body)


class TheTimeoutIsCancelledWhenItIsNoLongerNeeded(unittest.TestCase):
    def timer_off(self):
        timer, _, _ = one_shot_timer_after_dcc_chat(block("alias dccore.connect {"))
        return ".%s off" % timer

    def test_when_the_first_line_from_the_bot_arrives(self):
        chat = block("on ^*:CHAT:*: {")
        banner = chat.index("hadd dccore.live state banner")
        self.assertIn(self.timer_off(), chat[banner:], "the chat came up; the timer must go")

    def test_when_the_chat_closes(self):
        self.assertIn(self.timer_off(), block("on *:CHATCLOSE: {"))

    def test_when_every_timer_is_stopped(self):
        self.assertIn(self.timer_off(), block("alias dccore.timers.off {"))


if __name__ == "__main__":
    unittest.main()
