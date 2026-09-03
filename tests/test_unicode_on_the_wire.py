"""Non-ASCII filenames have to survive two byte-boundaries to reach a user.

Long paths were already handled - platform_compat.long_path() wraps the library
scan, the request's existence check, the size probe and the send's open(), and
tests/test_long_paths.py covers that end to end. Non-ASCII was handled in the
library and the list (update_list.py's _sanitise(), and the UTF-8 writes around
it) but not on the wire, at either end:

  IN   irc.py decoded each recv() chunk before appending it to the line buffer.
       A UTF-8 character is 2-4 bytes and recv() splits wherever the kernel had
       a boundary, so any character the split landed inside was destroyed - the
       decoder was handed half of one and could only drop it. The request then
       matched nothing and the user was told the file did not exist.

  OUT  dcc.py built the DCC SEND handshake as a raw f-string with no length
       check. At 2-3 bytes per character a long Greek or CJK filename pushes the
       line past IRC's 512 bytes, and the fields the transfer actually needs
       (address, port, size) sit AFTER the name - so the server's truncation
       takes those, and the receiver gets a handshake it cannot act on.

Both are silent, and both only bite non-ASCII, which is why a green suite and a
working ASCII library never surfaced them.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import irc  # noqa: E402

GREEK = "Ελληνικά Τραγούδια.flac"
CYRILLIC = "Русская Музыка.flac"
CHINESE = "中文歌曲.flac"
JAPANESE = "日本語の曲.flac"


def request_line(filename):
    return f":dave!~d@isp.net PRIVMSG #dccore-test :!dccore {filename}\r\n"


def feed(chunks):
    """Push byte chunks through take_complete_lines() the way irc_loop does."""
    buffer = b""
    lines = []
    for chunk in chunks:
        buffer, complete = irc.take_complete_lines(buffer, chunk)
        lines += complete
    return buffer, lines


def split_inside_a_character(raw, needle):
    """Cut `raw` one byte into the multi-byte character `needle`."""
    encoded = needle.encode("utf-8")
    assert len(encoded) > 1, f"{needle!r} is single-byte, nothing to split"
    at = raw.index(encoded) + 1
    return [raw[:at], raw[at:]]


class ACharacterSplitAcrossTwoReadsSurvives(unittest.TestCase):
    """The defect. Every one of these is a filename a real library holds."""

    def test_each_script_arrives_intact(self):
        for name in (GREEK, CYRILLIC, CHINESE, JAPANESE):
            with self.subTest(name=name):
                line = request_line(name)
                raw = line.encode("utf-8")
                # Split inside the FIRST non-ASCII character of the filename.
                first = next(ch for ch in name if len(ch.encode("utf-8")) > 1)

                _left, lines = feed(split_inside_a_character(raw, first))

                self.assertEqual(lines, [line[:-2]])
                self.assertIn(name, lines[0])

    def test_the_character_is_not_merely_replaced(self):
        """A decoder that produced U+FFFD here would still lose the request -
        "not corrupted" is the claim, not "did not raise"."""
        raw = request_line(GREEK).encode("utf-8")

        _left, lines = feed(split_inside_a_character(raw, "λ"))

        self.assertNotIn("�", lines[0])

    def test_every_possible_split_point_is_safe(self):
        """Not just one boundary: the kernel can hand back any prefix, so the
        property has to hold at every byte offset in the line."""
        line = request_line(CHINESE)
        raw = line.encode("utf-8")

        for at in range(1, len(raw)):
            with self.subTest(at=at):
                _left, lines = feed([raw[:at], raw[at:]])

                self.assertEqual(lines, [line[:-2]])

    def test_a_character_split_across_three_reads_survives(self):
        """A 3-byte CJK character can straddle two boundaries at once on a slow
        or heavily fragmented link."""
        line = request_line(CHINESE)
        raw = line.encode("utf-8")
        at = raw.index("中".encode("utf-8"))

        _left, lines = feed([raw[:at + 1], raw[at + 1:at + 2], raw[at + 2:]])

        self.assertEqual(lines, [line[:-2]])


class TheFramingItselfStillWorks(unittest.TestCase):
    """The control. A helper that returned everything, or nothing, would pass
    the tests above and break the bot."""

    def test_two_whole_lines_come_back_separately(self):
        left, lines = feed([b"PING :one\r\nPONG :two\r\n"])

        self.assertEqual(lines, ["PING :one", "PONG :two"])
        self.assertEqual(left, b"")

    def test_a_partial_line_is_held_back_not_dispatched(self):
        """Dispatching a half-received line is how a truncated command gets
        parsed as a different one."""
        left, lines = feed([b"PING :one\r\nPART #chan"])

        self.assertEqual(lines, ["PING :one"])
        self.assertEqual(left, b"PART #chan")

    def test_the_held_back_line_arrives_once_completed(self):
        buffer, lines = irc.take_complete_lines(b"", b"PART #cha")
        self.assertEqual(lines, [])

        _buffer, lines = irc.take_complete_lines(buffer, b"n\r\n")

        self.assertEqual(lines, ["PART #chan"])

    def test_an_empty_chunk_yields_nothing_and_keeps_the_buffer(self):
        buffer, lines = irc.take_complete_lines(b"half", b"")

        self.assertEqual(lines, [])
        self.assertEqual(buffer, b"half")

    def test_bytes_that_are_not_utf8_at_all_become_a_visible_placeholder(self):
        """A client sending CP1251 is a different situation from a character cut
        in half, and "ignore" would make the two indistinguishable - both would
        arrive as silence."""
        _buffer, lines = irc.take_complete_lines(b"", b"PRIVMSG #c :\xff\xfe\r\n")

        self.assertIn("�", lines[0])
        self.assertTrue(lines[0].startswith("PRIVMSG #c :"))

    def test_the_buffer_stays_bytes(self):
        """A str buffer is the whole defect: it means a decode already happened
        on something that was not a complete line."""
        buffer, _lines = irc.take_complete_lines(b"", b"PART #cha")

        self.assertIsInstance(buffer, bytes)


class TheDccOfferFitsTheIrcLine(unittest.TestCase):

    def build(self, name, user="dave"):
        return (f"PRIVMSG {user} :\x01DCC SEND {name} "
                f"3232235777 50000 123456789\x01\r\n")

    def offered_name(self, line):
        return line.split("DCC SEND ")[1].rsplit(" 3232235777", 1)[0]

    def test_a_long_cjk_filename_no_longer_overruns(self):
        """200 CJK characters is 651 bytes of handshake before this - 139 over
        the protocol's own hard limit, never mind the prefix the server adds."""
        name = "中" * 196 + ".flac"

        line = announce.fit_irc_filename(self.build, name)

        self.assertLessEqual(len(line.encode("utf-8")), announce.IRC_LINE_BUDGET)

    def test_every_script_fits_at_length(self):
        for label, char in (("greek", "α"), ("cyrillic", "я"),
                            ("chinese", "中"), ("japanese", "の")):
            with self.subTest(script=label):
                line = announce.fit_irc_filename(self.build, char * 300 + ".flac")

                self.assertLessEqual(len(line.encode("utf-8")),
                                     announce.IRC_LINE_BUDGET)

    def test_the_extension_survives_the_trim(self):
        """The point of not reusing fit_irc_line(): the receiver SAVES this
        name, so an ellipsis where the suffix was produces a file the operating
        system will not open."""
        for char in ("α", "中"):
            with self.subTest(char=char):
                line = announce.fit_irc_filename(self.build, char * 300 + ".flac")

                self.assertTrue(self.offered_name(line).endswith(".flac"),
                                self.offered_name(line)[-12:])

    def test_a_name_that_already_fits_is_untouched(self):
        """The control. A fitter that trimmed everything would pass every test
        above and quietly rename the whole library."""
        for name in (GREEK, CYRILLIC, CHINESE, "Plain ASCII Track.flac"):
            with self.subTest(name=name):
                line = announce.fit_irc_filename(self.build, name)

                self.assertEqual(self.offered_name(line), name)

    def test_a_long_ascii_name_is_also_left_alone(self):
        """200 ASCII characters is only 259 bytes - well inside budget. Trimming
        it would be a regression dressed as a fix."""
        name = "A" * 196 + ".flac"

        self.assertEqual(self.offered_name(announce.fit_irc_filename(self.build, name)),
                         name)

    def test_a_name_with_no_extension_gains_no_spurious_one(self):
        name = "中" * 300

        offered = self.offered_name(announce.fit_irc_filename(self.build, name))

        self.assertNotIn(".", offered)
        self.assertTrue(name.startswith(offered))

    def test_the_trim_keeps_as_much_of_the_name_as_it_can(self):
        """A fitter that cut to the bone would fit trivially and be useless -
        the shortened name still has to identify the track."""
        line = announce.fit_irc_filename(self.build, "中" * 196 + ".flac")

        self.assertGreater(len(self.offered_name(line)), 100)

    def test_the_fields_after_the_name_are_never_what_gets_cut(self):
        """The actual failure being prevented: address, port and size sit after
        the filename, so an over-long line loses THOSE."""
        line = announce.fit_irc_filename(self.build, "中" * 300 + ".flac")

        self.assertTrue(line.endswith("3232235777 50000 123456789\x01\r\n"))

    def test_a_multibyte_character_is_never_cut_in_half(self):
        """Trimming by bytes rather than characters would put half a character
        on the wire - the same class of bug as the read loop's."""
        line = announce.fit_irc_filename(self.build, "中" * 300 + ".flac")

        line.encode("utf-8").decode("utf-8")  # raises if a character was split
        self.assertNotIn("�", line)


class TheHandshakeActuallyUsesIt(unittest.TestCase):
    """dcc.py is where the offer is built; a fitter nothing calls fixes nothing."""

    def dcc_source(self):
        import io
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_offer_is_built_through_the_fitter(self):
        source = self.dcc_source()
        start = source.index("ctcp_handshake = ")
        window = source[start:start + 400]

        self.assertIn("fit_irc_filename", window)

    def test_the_operator_is_told_when_a_name_was_shortened(self):
        """Silently renaming somebody's file is how a support question becomes
        unanswerable - the operator cannot find the track in their own library
        under the name the receiver reports."""
        self.assertIn("Offered filename shortened", self.dcc_source())


if __name__ == "__main__":
    unittest.main()
