"""The read loop takes the server's backlog in useful-sized bites.

The socket was read 2048 bytes at a time for as long as this file has existed.
That is one or two IRC lines - RFC 1459 caps a line at 512 bytes, IRCv3 tags
raise it to 8703 - which is fine while the server is trickling channel chatter
and wrong when it is not.

Joining channels is when it is not. Every JOIN is answered with the whole NAMES
list, one 353 per few hundred nicks and then a 366, so adding several channels
at once produces tens of kilobytes in a burst - taken two kilobytes at a time.
An ircd bounds what it will hold for a client that is not keeping up and closes
the link when that fills; to us that arrives as ECONNRESET with nothing to say
why. A beta reported exactly that shape - "[WinError 10054] ... Dropping the
link to reconnect" right after several channels were added - and it would not
reproduce.

THIS IS NOT PROOF THAT WAS THE CAUSE, and the disconnect report that shipped
alongside it will say so directly the next time it happens. It is that reading
a byte stream two kilobytes at a time has no argument for it: recv() returns
whatever is there up to the size asked for and never waits to fill the buffer,
so a larger one costs an allocation and saves syscalls exactly when there is a
backlog to clear.

WHAT MAKES IT SAFE. take_complete_lines() accumulates BYTES and returns only
whole CRLF-terminated lines, so the read size cannot split a line or a UTF-8
character however the boundary lands - and MAX_PENDING_LINE_BYTES still bounds
a peer that never sends CRLF at all. Those are properties the old size relied
on just as much; the tests here pin them at the scale the new one operates at.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402


def names_burst(channels=12, nicks_per_line=60, lines_per_channel=4):
    """What joining several channels actually produces."""
    out = []
    for channel in range(channels):
        for part in range(lines_per_channel):
            nicks = " ".join(f"someuser{channel}_{part}_{n}"
                             for n in range(nicks_per_line))
            out.append(f":irc.example.invalid 353 SomeBot = #chan{channel} "
                       f":{nicks}")
        out.append(f":irc.example.invalid 366 SomeBot #chan{channel} "
                   f":End of /NAMES list.")
    return out


class TheReadIsBigEnoughToBeWorthMaking(unittest.TestCase):

    def test_a_names_burst_is_bigger_than_the_old_read(self):
        """The premise, measured rather than asserted. If a burst fitted in
        2 KB there would be nothing here to fix."""
        burst = ("\r\n".join(names_burst()) + "\r\n").encode()

        self.assertGreater(len(burst), 2048 * 10)

    def test_the_read_size_takes_a_burst_in_a_few_passes_not_dozens(self):
        burst = ("\r\n".join(names_burst()) + "\r\n").encode()

        passes = -(-len(burst) // irc.SOCKET_READ_BYTES)

        self.assertLessEqual(passes, 4)
        self.assertGreater(len(burst) // 2048, 10,
                           "and the old size needed more than ten")

    def test_it_is_larger_than_the_longest_line_a_server_may_send(self):
        """512 by RFC 1459, 8703 with IRCv3 tags. A read smaller than a line
        is not wrong - the buffer handles it - but a read that cannot hold one
        guarantees at least two passes for every long line."""
        self.assertGreater(irc.SOCKET_READ_BYTES, 8703)

    def test_it_is_not_extravagant(self):
        """Allocated per read, on a loop that runs for the life of the
        process."""
        self.assertLessEqual(irc.SOCKET_READ_BYTES, 1024 * 1024)


class ABiggerReadSplitsTheSameWay(unittest.TestCase):
    """The property the read size depends on: whatever arrives in one chunk,
    only whole lines come out."""

    def test_a_whole_burst_in_one_chunk_yields_every_line(self):
        expected = names_burst()
        chunk = ("\r\n".join(expected) + "\r\n").encode()

        leftover, lines = irc.take_complete_lines(b"", chunk)

        self.assertEqual(lines, expected)
        self.assertEqual(leftover, b"")

    def test_a_chunk_ending_mid_line_keeps_the_remainder(self):
        """The case a bigger read makes MORE likely, not less: more lines per
        chunk means the boundary lands inside one nearly every time."""
        whole = ":irc.example.invalid 366 SomeBot #chan :End of /NAMES list.\r\n"
        chunk = (whole + ":irc.example.invalid 353 SomeBot = #chan :half").encode()

        leftover, lines = irc.take_complete_lines(b"", chunk)

        self.assertEqual(lines, [whole.rstrip("\r\n")])
        self.assertTrue(leftover.endswith(b":half"))

    def test_the_remainder_completes_on_the_next_read(self):
        first = b":irc.example.invalid 353 SomeBot = #chan :one two thr"
        second = b"ee\r\n"

        leftover, lines = irc.take_complete_lines(b"", first)
        self.assertEqual(lines, [])
        leftover, lines = irc.take_complete_lines(leftover, second)

        self.assertEqual(lines, [
            ":irc.example.invalid 353 SomeBot = #chan :one two three"])
        self.assertEqual(leftover, b"")

    def test_a_character_split_across_two_reads_survives_whole(self):
        """A UTF-8 character is 2-4 bytes and a read boundary lands wherever
        the kernel had bytes. Decoding per chunk would drop it."""
        text = ":irc.example.invalid PRIVMSG #chan :åäö\r\n"
        raw = text.encode("utf-8")
        cut = raw.index(b"\xc3") + 1

        leftover, lines = irc.take_complete_lines(b"", raw[:cut])
        self.assertEqual(lines, [])
        _leftover, lines = irc.take_complete_lines(leftover, raw[cut:])

        self.assertEqual(lines, [text.rstrip("\r\n")])

    def test_a_peer_that_never_sends_crlf_is_still_bounded(self):
        """A larger read fills the buffer faster, so the ceiling matters more,
        not less."""
        leftover, lines = irc.take_complete_lines(
            b"", b"x" * (irc.MAX_PENDING_LINE_BYTES + 1))

        self.assertEqual(lines, [])
        self.assertEqual(leftover, b"")


class BothLoopsUseIt(unittest.TestCase):
    """The registration loop reads the same stream through the same helper -
    and the server's 001-005 and MOTD arrive there, in a burst of their own."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "irc.py"),
                     encoding="utf-8") as handle:
            return handle.read()

    def recv_calls(self):
        """Every `<something>.recv(...)` in irc.py, as its first argument.

        Read from the SYNTAX TREE, not the text. take_complete_lines()'s own
        docstring quotes the old `s.recv(2048).decode(...)` line to explain
        why bytes are accumulated before decoding - which is prose worth
        keeping, and which a text search reports as a hard-coded read that is
        still there."""
        import ast

        tree = ast.parse(self.source())
        return [node.args[0] for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "recv" and node.args]

    def test_no_read_size_is_hard_coded(self):
        import ast

        offenders = [ast.dump(arg) for arg in self.recv_calls()
                     if isinstance(arg, ast.Constant)]

        self.assertEqual(offenders, [])

    def test_both_reads_take_the_named_size(self):
        import ast

        named = [arg.id for arg in self.recv_calls()
                 if isinstance(arg, ast.Name)]

        self.assertEqual(named, ["SOCKET_READ_BYTES"] * 2)

    def test_that_search_actually_finds_the_reads(self):
        """Guard on the guard: an AST walk that matched nothing would satisfy
        both assertions above."""
        self.assertEqual(len(self.recv_calls()), 2)

if __name__ == "__main__":
    unittest.main()
