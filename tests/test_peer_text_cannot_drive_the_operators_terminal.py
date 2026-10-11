"""Text other people typed never reaches the operator's terminal as a
terminal command.

#670 made the request and search text printable before it is printed. The
disconnect report was missed: it keeps the last lines the server sent - any
channel member's message among them - and prints them on every drop, so a line
like "\\x1b]0;...\\x07\\x1b[2J" typed in a channel the bot sits in retitled the
operator's window and cleared it at the next disconnect. The DEBUG_MODE raw
log and the [SERVER ERROR] line printed server text the same way.

Two layers now. Those three keep only printable text, the way #670's paths
do. And the console stream every line passes through (platform_compat's
timestamp proxy) takes out anything a terminal would act on, so text from
other people is covered wherever some other message happens to include it.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

ESC = "\x1b"
BEL = "\x07"
RETITLE_AND_CLEAR = ESC + "]0;pwned" + BEL + ESC + "[2J"


class _StopTheLoop(BaseException):
    """Raised from the reconnect path's sleep; nothing in irc_loop() catches it."""


class _ScriptedSocket:

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def settimeout(self, *_a): pass
    def setsockopt(self, *_a): pass
    def connect(self, *_a): pass
    def close(self): pass
    def sendall(self, payload): pass

    def send(self, payload):
        return len(payload)

    def recv(self, _n):
        if self.chunks:
            return self.chunks.pop(0)
        raise socket.error("scripted end of link")


class TheReadLoopPrintsNoTerminalCommands(DCCoreTestCase):
    """irc.irc_loop() for real, against a scripted server, until the link
    drops and the disconnect report is printed."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", ALT_NICKNAME="SomeBot_",
                        SERVER="irc.example.invalid", PORT=6667,
                        CHANNEL="#music", DEBUG_CHANNEL="",
                        MY_IP_OR_DOCK="203.0.113.5", DEBUG_MODE=True)
        config.ORIGINAL_NICK = "SomeBot"
        self.addCleanup(setattr, config, "ORIGINAL_NICK", "DCCore")
        silence_debug(announce)
        self.addCleanup(setattr, irc.socket, "socket", irc.socket.socket)
        self.addCleanup(setattr, irc.time, "sleep", irc.time.sleep)
        announce._pm_last_recorded.clear()

    def printed(self, *server_lines):
        chunks = [(line + "\r\n").encode("utf-8") for line in server_lines]
        sock = _ScriptedSocket(chunks)
        irc.socket.socket = lambda *a, **k: sock

        def sleep_stops_the_loop(_seconds):
            raise _StopTheLoop()
        irc.time.sleep = sleep_stops_the_loop
        outcome = {}

        def run():
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    irc.irc_loop()
                except _StopTheLoop:
                    outcome["stopped"] = True
                except BaseException as err:  # noqa: BLE001 - reported below
                    outcome["error"] = repr(err)
            outcome["out"] = out.getvalue()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "irc_loop() did not reach the reconnect path")
        self.assertTrue(outcome.get("stopped"), outcome.get("error"))
        return outcome["out"]

    def test_a_channel_message_in_the_disconnect_report(self):
        out = self.printed(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            ":alfa!a@host.example PRIVMSG #music :" + RETITLE_AND_CLEAR + " hello there")

        report = [line for line in out.splitlines() if "[DISCONNECT]" in line]
        self.assertTrue(any("hello there" in line for line in report), report)
        self.assertNotIn(ESC, out)
        self.assertNotIn(BEL, out)

    def test_the_raw_log_and_a_server_error(self):
        out = self.printed(
            ":irc.example.net NOTICE AUTH :*** Looking up your hostname",
            ":alfa!a@host.example NOTICE SomeBot :" + RETITLE_AND_CLEAR + " raw line",
            "ERROR :Closing Link: SomeBot " + RETITLE_AND_CLEAR + "(Quit)")

        self.assertIn("[RAW IN]", out)
        self.assertIn("raw line", out)
        self.assertIn("[SERVER ERROR]", out)
        self.assertIn("(Quit)", out)
        self.assertNotIn(ESC, out)
        self.assertNotIn(BEL, out)


class TheConsoleStreamTakesThemOut(unittest.TestCase):
    """platform_compat's console proxy, which every printed line goes through
    in the daemon - with timestamps off and on."""

    def stream(self, fmt):
        target = io.StringIO()
        return target, platform_compat._TimestampedStream(target, lambda: fmt)

    def test_a_sequence_never_reaches_the_terminal(self):
        for fmt in ("", "%H:%M:%S"):
            with self.subTest(timestamps=bool(fmt)):
                target, proxy = self.stream(fmt)
                text = "[FETCH] alfa said: " + RETITLE_AND_CLEAR + ESC + "[8mhidden\n"

                written = proxy.write(text)

                self.assertEqual(written, len(text))
                self.assertNotIn(ESC, target.getvalue())
                self.assertNotIn(BEL, target.getvalue())
                self.assertIn("[FETCH] alfa said: hidden\n", target.getvalue())

    def test_every_kind_a_terminal_acts_on(self):
        for sequence in (ESC + "[31m", ESC + "]2;title" + ESC + "\\", ESC + "c", "\x9b2J",
                         "\x08", "\x0c", "\x7f", ESC + "P1$r" + ESC + "\\"):
            with self.subTest(sequence=sequence):
                cleaned = platform_compat.terminal_safe("a" + sequence + "b")
                self.assertTrue(cleaned.startswith("a") and cleaned.endswith("b"), repr(cleaned))
                for ch in cleaned:
                    self.assertTrue(ch.isprintable(), repr(cleaned))

    def test_layout_and_ordinary_text_are_kept(self):
        for text in ("plain line\n", "tab\tseparated\r\n", "progress\r", "Bjork - Joga.flac\n",
                     "Ångström ♫\n"):
            with self.subTest(text=text):
                self.assertIs(platform_compat.terminal_safe(text), text)
                target, proxy = self.stream("")
                proxy.write(text)
                self.assertEqual(target.getvalue(), text)

    def test_an_unterminated_string_keeps_the_rest_of_the_line(self):
        """Taking everything after an unterminated OSC would hide what the
        line went on to say; only its ESC goes."""
        self.assertEqual(platform_compat.terminal_safe("x " + ESC + "]0;still here"),
                         "x ]0;still here")

    def test_bytes_are_still_refused(self):
        """What click probes a stream with - the proxy must keep refusing it."""
        _target, proxy = self.stream("")
        with self.assertRaises(TypeError):
            proxy.write(b"")


if __name__ == "__main__":
    unittest.main()
